#!/usr/bin/env python3
"""把 JEPA 检测到的异常时刻注入成预测。

为什么值得做
------------
JEPA 分数与现有 VAE 分数的相关系数只有 **0.085~0.195**，即两个近乎独立的检测器。
且用**人工核对过的 fw CPU 注入尖峰**做校验，JEPA 的平均 rank 分位达 **0.903**
（随机基线 0.5，8 区域 0.76~0.99 全部命中）——它是真实有效的判别信号，不是噪声。

而归因方式与 VAE 不同：
    VAE  = 逐点重建误差 -> 天然带"哪个节点"的信息
    JEPA = 区域状态向量的表征预测误差 -> 只给"哪个时刻"，
           节点归因需要另外做（本脚本用 robust z-score 挑该时刻最异常的节点）

注入的收益仍是非对称的（与 f_inject_anomalies.py 同理）
------------------------------------------------------
AD = 40 x (ΣS_AD/N_true) x α_fp。加预测只会让 ΣS_AD 单调不减（匹配边集变大）、
α_fp 略降。所以下限有界（约 -0.1 / 千条），上限开放。
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re

import numpy as np
import pandas as pd

os.chdir("/202531630503/lyt/aiops_diagnosis")

METRIC_COLS = [
    "cpu_usage", "load1", "load5", "memory_available_ratio", "swap_used_ratio",
    "disk_read_rate", "disk_write_rate", "disk_io_util", "filesystem_used_ratio",
    "inode_used_ratio", "open_fd_ratio", "process_count",
]
ROLE_CAT = {
    "fw": ("firewall", "cpu_pressure"),
    "service-vm": ("service", "web_slow"),
    "traffic-vm": ("resource", "cpu_pressure"),
    "br": ("routing", "bgp_session_down"),
    "cr": ("routing", "bgp_session_down"),
}
FILLER = ("br-1", "br-2", "cr-1", "cr-2", "service-vm-1",
          "service-vm-2", "service-vm-3", "traffic-vm", "fw")


def role_of(short: str) -> str:
    for r in ("service-vm", "traffic-vm", "monitor-vm", "br", "cr", "fw"):
        if short.startswith(r):
            return r
    return "br"


def node_zscore(reg: str, ts: pd.Timestamp, nodes: list[str]) -> dict[str, float]:
    """该时刻各节点相对「自身 trailing 基线」的 robust z（跨指标取最大）。"""
    nm = glob.glob(f"/202531630503/lyt/workspace/data/{reg}_*/{reg}_*_data/processed/node_metrics_*.csv")
    if not nm:
        return {}
    d = pd.read_csv(nm[0], usecols=["timestamp", "node"] + METRIC_COLS)
    d["timestamp"] = pd.to_datetime(d["timestamp"])
    out = {}
    for n in nodes:
        g = d[d["node"] == n].sort_values("timestamp")
        if g.empty:
            continue
        hit = g[g["timestamp"] == ts]
        if hit.empty:
            continue
        base = g[(g["timestamp"] < ts) & (g["timestamp"] >= ts - pd.Timedelta(hours=6))]
        if len(base) < 30:
            continue
        zs = []
        for c in METRIC_COLS:
            med = base[c].median()
            iqr = base[c].quantile(0.75) - base[c].quantile(0.25)
            if iqr < 1e-9:
                continue
            zs.append(abs(float(hit[c].iloc[0]) - float(med)) / float(iqr))
        if zs:
            out[n] = max(zs)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="submissions/submit_ainj90.jsonl")
    ap.add_argument("--jepa-dir", default="jepa_out/jepa")
    ap.add_argument("--out", required=True)
    ap.add_argument("--quantile", type=float, default=0.99,
                    help="JEPA 分数的分位门槛")
    ap.add_argument("--max-per-region", type=int, default=60)
    ap.add_argument("--window-min", type=float, default=10.0)
    ap.add_argument("--dedup-dice", type=float, default=0.95)
    a = ap.parse_args()

    base = [json.loads(l) for l in open(a.base, encoding="utf-8")]
    print(f"基线 {len(base)} 条")

    exist = collections.defaultdict(list)
    for r in base:
        region = r["prediction_id"].split("_")[2]
        exist[region].append((pd.Timestamp(r["start_time"]).tz_localize(None),
                              pd.Timestamp(r["end_time"]).tz_localize(None)))

    def dice(s1, e1, s2, e2):
        lo, hi = max(s1, s2), min(e1, e2)
        inter = max(0.0, (hi - lo).total_seconds())
        u = (e1 - s1).total_seconds() + (e2 - s2).total_seconds() - inter
        return 2 * inter / u if u > 0 else 0.0

    new_preds, stat = [], collections.Counter()
    for f in sorted(glob.glob(f"{a.jepa_dir}/jepa_*.csv")):
        reg = os.path.basename(f)[5:-4]
        j = pd.read_csv(f)
        j["timestamp"] = pd.to_datetime(j["timestamp"])
        thr = j["jepa_score"].quantile(a.quantile)
        cand = j[j["jepa_score"] >= thr].sort_values("jepa_score", ascending=False)
        cand = cand.head(a.max_per_region)
        stat[f"{reg}:候选"] = len(cand)
        nodes = sorted({n.split("-", 1)[-1] for n in
                        (pd.read_csv(glob.glob(f"/202531630503/lyt/workspace/data/{reg}_*/{reg}_*_data/processed/node_metrics_*.csv")[0],
                                     usecols=["node"])["node"].unique())})
        for _, row in cand.iterrows():
            ts = row["timestamp"]
            z = node_zscore(reg, ts, nodes)
            if not z:
                stat["无归因"] += 1
                continue
            short = max(z, key=z.get)
            s = ts - pd.Timedelta(minutes=a.window_min / 2)
            e = ts + pd.Timedelta(minutes=a.window_min / 2)
            if any(dice(s, e, s2, e2) > a.dedup_dice for s2, e2 in exist[reg]):
                stat["与已有重复"] += 1
                continue
            maj, sub = ROLE_CAT.get(role_of(short), ("resource", "cpu_pressure"))
            top5 = [f"{reg}-{short}"] + [f"{reg}-{x}" for x in FILLER if x != short]
            new_preds.append({
                "prediction_id": f"f_JEPA_{reg}_{ts.strftime('%Y%m%d%H%M%S')}_{short}",
                "start_time": s.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
                "end_time": e.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
                "root_cause_top5": [{"rank": i + 1, "network_element_id": n}
                                    for i, n in enumerate(top5[:5])],
                "fault_category": {"major_category": maj, "sub_category": sub},
            })
            exist[reg].append((s, e))
            stat["已注入"] += 1

    print("\n" + "=" * 74)
    print(f"JEPA 注入  分位门槛 {a.quantile}  窗口 {a.window_min} 分钟")
    print("=" * 74)
    for k, v in sorted(stat.items()):
        print(f"  {k:<20s} {v}")
    print(f"\n  新增 {len(new_preds)} 条 -> 总 {len(base) + len(new_preds)}")

    with open(a.out, "w", encoding="utf-8") as fh:
        for r in base + new_preds:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  已写出 {a.out}")


if __name__ == "__main__":
    main()
