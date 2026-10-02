#!/usr/bin/env python3
"""JEPA 式多元时序异常检测（表征预测，而非数值重建）。

为什么做这个
------------
今天（2026-10-02）的实测把杠杆排得很清楚：

    firewall 类别修复   +0.804   (136 条)
    覆盖率注入(两轮)     +0.371   (13224 条)   <- 最大且唯一可再挖的
    尖峰锚定窗口         +0.290
    五大类完整修复       -0.052
    LLM top5 重排        +0.038   <- 排序侧已到天花板

现有检测器是 IsolationForest + VAE，**重建原始数值**。JEPA 的归纳偏置不同：
它预测的是**未来时间窗在表征空间里的位置**，而不是逐点数值。已知优势是
不去拟合无关细节、学到的是语义级结构 —— 因此**可能捞到 VAE 重建得「够好」
但动力学上其实不可预测的故障**。

设计（I-JEPA 的时序适配）
------------------------
    区域状态向量 x_t  (9 节点 x 12 指标 = 108 维/分钟)
        context 窗口  [t-60, t)      --encoder-->  z_ctx
        target  窗口  [t, t+20)      --target-encoder(EMA)--> z_tgt
        predictor(z_ctx)  ~=  z_tgt                       (stop-grad on z_tgt)

    训练：只用预测误差做自监督，不需要任何标签
    打分：推理时每个窗口的预测误差即异常分

评估方式（没有真值，只能做可验证的对比）
--------------------------------------
  1) 与现有 VAE 分（point_scores）逐点对比：相关系数、重合度
  2) 找 **JEPA 高 / VAE 低** 的点 —— 那是「现有检测器可能漏掉的」候选
  3) 这些候选能否提升分数，只能靠提交验证（与注入实验同一套非对称赌注：
     加预测不会让 ΣS_AD 下降，只让 α_fp 略降，下限有界、上限开放）
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import math
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


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------
def load_region_state(path: str, nodes: list[str] | None = None):
    """把 node_metrics 透视成 (T, N*C) 的区域状态矩阵。"""
    df = pd.read_csv(path, usecols=["timestamp", "node"] + METRIC_COLS)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    node_list = sorted(df["node"].unique())
    if nodes:
        node_list = [n for n in node_list if n in nodes]
    df = df[df["node"].isin(node_list)]
    # (T, N, C)
    wide = df.pivot_table(index="timestamp", columns="node",
                          values=METRIC_COLS, aggfunc="mean")
    idx = wide.index
    arr = np.zeros((len(idx), len(node_list), len(METRIC_COLS)), dtype=np.float32)
    for j, n in enumerate(node_list):
        for k, c in enumerate(METRIC_COLS):
            if (c, n) in wide.columns:
                arr[:, j, k] = wide[(c, n)].to_numpy(dtype=np.float32)
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    return idx, node_list, arr


def robust_scale(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """逐通道 robust 标准化。用中位数/IQR，避免注入尖峰支配尺度。"""
    flat = x.reshape(-1, x.shape[-1])
    med = np.median(flat, axis=0)
    q75, q25 = np.percentile(flat, 75, axis=0), np.percentile(flat, 25, axis=0)
    iqr = np.where((q75 - q25) < 1e-9, 1.0, q75 - q25)
    return (x - med) / iqr, med, iqr


# --------------------------------------------------------------------------
# 模型
# --------------------------------------------------------------------------
class BlockEncoder(nn.Module):
    """把一段 (L, D) 序列编码成一个定长表征。"""

    def __init__(self, d_in: int, d_model: int = 128, n_layers: int = 3,
                 n_heads: int = 4, d_out: int = 64, dropout: float = 0.0):
        super().__init__()
        self.proj = nn.Linear(d_in, d_model)
        self.pos = nn.Parameter(torch.zeros(1, 512, d_model))
        nn.init.trunc_normal_(self.pos, std=0.02)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_heads, dim_feedforward=d_model * 4,
            dropout=dropout, batch_first=True, norm_first=True,
            activation="gelu")
        self.enc = nn.TransformerEncoder(layer, num_layers=n_layers)
        self.head = nn.Sequential(nn.LayerNorm(d_model), nn.Linear(d_model, d_out))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.proj(x) + self.pos[:, : x.shape[1]]
        h = self.enc(h)
        return self.head(h.mean(dim=1))          # 时间维平均池化


class Predictor(nn.Module):
    def __init__(self, d_in: int = 64, d_hidden: int = 256, d_out: int = 64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(d_in, d_hidden), nn.GELU(),
            nn.Linear(d_hidden, d_hidden), nn.GELU(),
            nn.Linear(d_hidden, d_out))

    def forward(self, z: torch.Tensor) -> torch.Tensor:
        return self.net(z)


class JEPA(nn.Module):
    def __init__(self, d_in: int, **kw):
        super().__init__()
        self.ctx = BlockEncoder(d_in, **kw)
        self.tgt = copy.deepcopy(self.ctx)
        for p in self.tgt.parameters():
            p.requires_grad_(False)
        self.pred = Predictor(kw.get("d_out", 64),
                              d_hidden=kw.get("d_model", 128) * 2,
                              d_out=kw.get("d_out", 64))

    @torch.no_grad()
    def ema(self, m: float = 0.996) -> None:
        for a, b in zip(self.tgt.parameters(), self.ctx.parameters()):
            a.data.mul_(m).add_(b.data, alpha=1 - m)
        for a, b in zip(self.tgt.buffers(), self.ctx.buffers()):
            a.data.copy_(b.data)

    def forward(self, ctx: torch.Tensor, tgt: torch.Tensor):
        zc = self.ctx(ctx)
        with torch.no_grad():
            zt = self.tgt(tgt)
        zp = self.pred(zc)
        zp = F.normalize(zp, dim=-1)
        zt = F.normalize(zt, dim=-1)
        return zp, zt


# --------------------------------------------------------------------------
# 训练与打分
# --------------------------------------------------------------------------
def make_windows(x: np.ndarray, ctx_len: int, tgt_len: int, stride: int):
    """返回 (ctx, tgt) 索引数组，按时间顺序。"""
    n = len(x)
    L = ctx_len + tgt_len
    starts = np.arange(0, n - L + 1, stride)
    return starts


def train_region(x: np.ndarray, *, ctx_len: int, tgt_len: int, stride: int,
                 epochs: int, batch: int, lr: float, device: str, seed: int,
                 log=print):
    torch.manual_seed(seed)
    np.random.seed(seed)
    starts = make_windows(x, ctx_len, tgt_len, stride)
    log(f"    窗口数 {len(starts)}  (ctx={ctx_len}, tgt={tgt_len}, stride={stride})")
    d_in = x.shape[1]
    model = JEPA(d_in, d_model=128, n_layers=3, n_heads=4, d_out=64).to(device)
    opt = torch.optim.AdamW(
        list(model.ctx.parameters()) + list(model.pred.parameters()),
        lr=lr, weight_decay=0.01)
    n = len(starts)
    for ep in range(1, epochs + 1):
        perm = np.random.permutation(n)
        tot, cnt = 0.0, 0
        for i in range(0, n, batch):
            sel = perm[i:i + batch]
            ctx = np.stack([x[s:s + ctx_len] for s in starts[sel]])
            tgt = np.stack([x[s + ctx_len:s + ctx_len + tgt_len] for s in starts[sel]])
            ctx_t = torch.from_numpy(ctx).to(device)
            tgt_t = torch.from_numpy(tgt).to(device)
            zp, zt = model(ctx_t, tgt_t)
            loss = F.smooth_l1_loss(zp, zt)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            nn.utils.clip_grad_norm_(
                list(model.ctx.parameters()) + list(model.pred.parameters()), 1.0)
            opt.step()
            model.ema()
            tot += float(loss) * len(sel)
            cnt += len(sel)
        if ep == 1 or ep % max(1, epochs // 5) == 0 or ep == epochs:
            log(f"    epoch {ep:3d}/{epochs}  loss={tot/max(cnt,1):.5f}")
    return model, starts


@torch.no_grad()
def score_region(model, x: np.ndarray, starts: np.ndarray, *, ctx_len: int,
                 tgt_len: int, batch: int, device: str) -> np.ndarray:
    """每个窗口的预测误差；把误差回填到 target 窗口覆盖的每个时间点上。"""
    model.eval()
    per_point = np.zeros(len(x), dtype=np.float64)
    counts = np.zeros(len(x), dtype=np.float64)
    for i in range(0, len(starts), batch):
        sel = starts[i:i + batch]
        ctx = np.stack([x[s:s + ctx_len] for s in sel])
        tgt = np.stack([x[s + ctx_len:s + ctx_len + tgt_len] for s in sel])
        zp, zt = model(torch.from_numpy(ctx).to(device),
                       torch.from_numpy(tgt).to(device))
        err = (zp - zt).pow(2).sum(-1).sqrt().cpu().numpy()
        for s, e in zip(sel, err):
            per_point[s + ctx_len: s + ctx_len + tgt_len] += e
            counts[s + ctx_len: s + ctx_len + tgt_len] += 1.0
    counts[counts == 0] = 1.0
    return per_point / counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="/202131510121/lyt/proj/data/mdata")
    ap.add_argument("--out", default="/202131510121/lyt/proj/out/jepa")
    ap.add_argument("--regions", default="beida")
    ap.add_argument("--ctx-len", type=int, default=60)
    ap.add_argument("--tgt-len", type=int, default=20)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[JEPA] device={device}  regions={a.regions}")
    if device == "cuda":
        print(f"       {torch.cuda.get_device_name(0)}")

    for reg in a.regions.split(","):
        files = glob.glob(f"{a.data_root}/{reg}/node_metrics_*.csv")
        if not files:
            print(f"  !! {reg}: 无 node_metrics")
            continue
        print(f"\n=== {reg} ===")
        t0 = time.time()
        idx, nodes, raw = load_region_state(files[0])
        print(f"  状态矩阵 {raw.shape}  节点 {nodes}")
        x, med, iqr = robust_scale(raw)
        x = x.reshape(len(x), -1).astype(np.float32)      # (T, N*C)
        print(f"  展平为 {x.shape}  用时 {time.time()-t0:.1f}s")

        model, starts = train_region(
            x, ctx_len=a.ctx_len, tgt_len=a.tgt_len, stride=a.stride,
            epochs=a.epochs, batch=a.batch, lr=a.lr, device=device, seed=a.seed)
        scores = score_region(model, x, starts, ctx_len=a.ctx_len,
                              tgt_len=a.tgt_len, batch=a.batch, device=device)
        print(f"  打分完成，分位数: "
              f"50%={np.percentile(scores,50):.4f} 90%={np.percentile(scores,90):.4f} "
              f"99%={np.percentile(scores,99):.4f} max={scores.max():.4f}")

        out = pd.DataFrame({"timestamp": idx, "jepa_score": scores})
        p = f"{a.out}/jepa_{reg}.csv"
        out.to_csv(p, index=False)
        print(f"  已写出 {p}")
        np.save(f"{a.out}/nodes_{reg}.npy", np.array(nodes))
        print(f"  总用时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
