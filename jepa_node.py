#!/usr/bin/env python3
"""逐节点 JEPA：给出「哪个节点在哪个时刻异常」，直接可与 VAE 的逐节点分对比。

与 jepa_ts.py 的区别
--------------------
jepa_ts.py 把整个区域的状态（9 节点 x 12 指标 = 108 维）当成一条序列，
预测误差是**区域级**的 —— 只回答"何时异常"，节点归因只能靠启发式
（上一版用 robust z-score 挑该时刻最异常的节点）。

本脚本改成**逐节点**建窗：输入是单个节点的 12 维指标序列，9 个节点的窗口
混在一个 batch 里、共享同一套编码器权重。于是：

    输出 = (节点, 时刻) 的异常分  -> 与 VAE 的逐节点分**同构**，可直接对比

校验方式（无需真值）
------------------
用人工核对过的 fw CPU 注入尖峰：若 JEPA 在这些时刻**对 fw 这个节点**给出
高排名（而不是对别的节点），说明它真的在做节点级归因，而不是区域级糊判。
"""
from __future__ import annotations

import argparse
import glob
import os
import time

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

METRIC_COLS = [
    "cpu_usage", "load1", "load5", "memory_available_ratio", "swap_used_ratio",
    "disk_read_rate", "disk_write_rate", "disk_io_util", "filesystem_used_ratio",
    "inode_used_ratio", "open_fd_ratio", "process_count",
]


class Enc(nn.Module):
    def __init__(self, d_in, d_model=96, n_layers=2, n_heads=4, d_out=48):
        super().__init__()
        self.proj = nn.Linear(d_in, d_model)
        self.pos = nn.Parameter(torch.zeros(1, 512, d_model))
        nn.init.trunc_normal_(self.pos, std=0.02)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_heads, dim_feedforward=d_model * 4,
            dropout=0.0, batch_first=True, norm_first=True, activation="gelu")
        self.enc = nn.TransformerEncoder(layer, num_layers=n_layers)
        self.head = nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, d_out))

    def forward(self, x):
        h = self.proj(x) + self.pos[:, : x.shape[1]]
        return self.head(self.enc(h).mean(1))


class JEPA(nn.Module):
    def __init__(self, d_in, d_out=48, d_model=96):
        super().__init__()
        self.ctx = Enc(d_in, d_model=d_model, n_layers=2, n_heads=4, d_out=d_out)
        self.tgt = Enc(d_in, d_model=d_model, n_layers=2, n_heads=4, d_out=d_out)
        for p in self.tgt.parameters():
            p.requires_grad_(False)
        self.pred = nn.Sequential(
            nn.Linear(d_out, d_model * 2), nn.GELU(),
            nn.Linear(d_model * 2, d_model * 2), nn.GELU(),
            nn.Linear(d_model * 2, d_out))

    @torch.no_grad()
    def ema(self, m=0.996):
        for a, b in zip(self.tgt.parameters(), self.ctx.parameters()):
            a.data.mul_(m).add_(b.data, alpha=1 - m)

    def forward(self, c, t):
        zc = self.pred(self.ctx(c))
        with torch.no_grad():
            zt = self.tgt(t)
        return F.normalize(zc, dim=-1), F.normalize(zt, dim=-1)


def load_nodes(path):
    df = pd.read_csv(path, usecols=["timestamp", "node"] + METRIC_COLS)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    nodes = sorted(df["node"].unique())
    out = {}
    for n in nodes:
        g = df[df["node"] == n].sort_values("timestamp")
        idx = g["timestamp"].to_numpy()
        arr = g[METRIC_COLS].to_numpy(dtype=np.float32)
        arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        med = np.median(arr, axis=0)
        q75, q25 = np.percentile(arr, 75, axis=0), np.percentile(arr, 25, axis=0)
        iqr = np.where((q75 - q25) < 1e-9, 1.0, q75 - q25)
        out[n] = (idx, ((arr - med) / iqr).astype(np.float32))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="/202131510121/lyt/proj/data/mdata")
    ap.add_argument("--out", default="/202131510121/lyt/proj/out/jepa_node")
    ap.add_argument("--regions", default="beida")
    ap.add_argument("--ctx", type=int, default=60)
    ap.add_argument("--tgt", type=int, default=20)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"

    for reg in a.regions.split(","):
        fs = glob.glob(f"{a.data_root}/{reg}/node_metrics_*.csv")
        if not fs:
            print(f"  !! {reg} 无数据"); continue
        print(f"\n=== {reg} (逐节点) ===", flush=True)
        t0 = time.time()
        nodes = load_nodes(fs[0])
        print(f"  节点 {list(nodes)}  用时 {time.time()-t0:.1f}s", flush=True)
        L = a.ctx + a.tgt
        samples = []          # (node_idx, start)
        for j, (n, (idx, x)) in enumerate(nodes.items()):
            for s in range(0, len(x) - L + 1, a.stride):
                samples.append((j, s))
        print(f"  样本数 {len(samples)}", flush=True)

        torch.manual_seed(a.seed); np.random.seed(a.seed)
        model = JEPA(len(METRIC_COLS)).to(dev)
        opt = torch.optim.AdamW(
            list(model.ctx.parameters()) + list(model.pred.parameters()),
            lr=a.lr, weight_decay=0.01)
        arrs = {j: x for j, (n, (idx, x)) in enumerate(nodes.items())}
        n = len(samples)
        for ep in range(1, a.epochs + 1):
            perm = np.random.permutation(n); tot = cnt = 0
            for i in range(0, n, a.batch):
                sel = [samples[k] for k in perm[i:i + a.batch]]
                c = np.stack([arrs[j][s:s + a.ctx] for j, s in sel])
                t = np.stack([arrs[j][s + a.ctx:s + L] for j, s in sel])
                zp, zt = model(torch.from_numpy(c).to(dev), torch.from_numpy(t).to(dev))
                loss = F.smooth_l1_loss(zp, zt)
                opt.zero_grad(set_to_none=True); loss.backward()
                nn.utils.clip_grad_norm_(
                    list(model.ctx.parameters()) + list(model.pred.parameters()), 1.0)
                opt.step(); model.ema()
                tot += float(loss) * len(sel); cnt += len(sel)
            if ep == 1 or ep % 5 == 0 or ep == a.epochs:
                print(f"    epoch {ep:3d}/{a.epochs} loss={tot/max(cnt,1):.5f}", flush=True)

        # 逐节点打分
        model.eval()
        rows = []
        with torch.no_grad():
            for j, (nname, (idx, x)) in enumerate(nodes.items()):
                sc = np.zeros(len(x)); cntp = np.zeros(len(x))
                starts = list(range(0, len(x) - L + 1, max(1, a.stride // 2)))
                for i in range(0, len(starts), a.batch):
                    ssel = starts[i:i + a.batch]
                    c = np.stack([x[s:s + a.ctx] for s in ssel])
                    t = np.stack([x[s + a.ctx:s + L] for s in ssel])
                    zp, zt = model(torch.from_numpy(c).to(dev), torch.from_numpy(t).to(dev))
                    e = (zp - zt).pow(2).sum(-1).sqrt().cpu().numpy()
                    for s, ev in zip(ssel, e):
                        sc[s + a.ctx:s + L] += ev; cntp[s + a.ctx:s + L] += 1
                cntp[cntp == 0] = 1
                rows.append(pd.DataFrame({
                    "timestamp": idx, "node": nname, "jepa_node_score": sc / cntp}))
        out = pd.concat(rows, ignore_index=True)
        p = f"{a.out}/jepa_node_{reg}.csv"
        out.to_csv(p, index=False)
        print(f"  已写出 {p} ({len(out)} 行)  用时 {time.time()-t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
