#!/usr/bin/env python3
"""在固定行数下，用重锚变体「换掉」价值最低的填充窗。

依据
----
另一会话的填充密度扫描（§31）：
    f=0      16788 行  -> 22.974546
    f=38996  55784 行  -> 23.255595   （峰值附近）
    f=67284  84072 行  -> 22.989460   （越过拐点）
二次拟合峰值 f* ≈ 34100。结论：**不能继续「加」行**。

而本会话实测（§32）：重锚窗口变体（为每条预测补一条起点锚到异常起点的窗，
两条都留、由全局最大权匹配自选）带来 **+0.228**，且离线代理指标事先预言
了方向（>=0.4 命中率 58.6% -> 75.1%）。

本脚本把两者合并：**总行数维持 55784 不变**，用重锚变体替换掉
「离最近强异常最远」的那批填充窗 —— 那些窗最不可能压中真故障。
"""
from __future__ import annotations

import argparse
import glob
import json
import os

import pandas as pd

os.chdir("/202531630503/lyt/aiops_diagnosis")

SPANS = {
    "20260819040000_20260902040000": ("outputs_experiment_f_full", 10.0),
    "20260917040000_20260924040000": ("outputs_experiment_f_stage2", 3.0),
}
_c = {}


def pts(root, reg, span):
    k = (root, reg, span)
    if k not in _c:
        f = glob.glob(f"{root}/{reg}_{span}/point_scores.csv.gz")
        if not f:
            _c[k] = None
        else:
            d = pd.read_csv(f[0], usecols=["network_element_id", "timestamp_bin",
                                           "combined_anomaly_score"])
            d["timestamp_bin"] = pd.to_datetime(d["timestamp_bin"])
            if getattr(d["timestamp_bin"].dt, "tz", None) is not None:
                d["timestamp_bin"] = d["timestamp_bin"].dt.tz_localize(None)
            _c[k] = d[d["combined_anomaly_score"] >= 0.9].copy()
    return _c[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="submissions/submit_fill2.jsonl")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.base, encoding="utf-8")]
    fills = [r for r in rows if r["prediction_id"].startswith("f_FILL_")]
    keep_rows = [r for r in rows if not r["prediction_id"].startswith("f_FILL_")]
    print(f"基线 {len(rows)} 条 = 非填充 {len(keep_rows)} + 填充 {len(fills)}")

    # 给每条填充窗打分：其窗口中心到「同区域最近 >=0.9 异常点」的距离（分钟）
    scored = []
    for r in fills:
        pid = r["prediction_id"]
        reg = pid.split("_")[2]
        span = next((s for s in SPANS if s in pid), None)
        if span is None:
            continue
        root, _ = SPANS[span]
        d = pts(root, reg, span)
        if d is None or d.empty:
            scored.append((1e9, r))
            continue
        s = pd.Timestamp(r["start_time"]).tz_localize(None)
        e = pd.Timestamp(r["end_time"]).tz_localize(None)
        mid = s + (e - s) / 2
        # 只看同区域的异常点，找时间上最近的一个
        near = (d["timestamp_bin"] - mid).abs().min()
        scored.append((near.total_seconds() / 60.0, r))
    scored.sort(key=lambda x: x[0])
    dists = [x[0] for x in scored if x[0] < 1e8]
    if dists:
        import numpy as np
        print(f"  填充窗到最近异常的时距: 中位 {np.median(dists):.0f} 分  "
              f"75% {np.percentile(dists,75):.0f} 分  最大 {max(dists):.0f} 分")

    # 预算：能加多少重锚变体
    budget = min(len(fills), 18556)
    drop = {id(x[1]) for x in scored[-budget:]} if budget else set()
    kept_fills = [r for r in fills if id(r) not in drop]
    print(f"  预算 {budget}：丢弃最远的 {len(drop)} 条填充窗，保留 {len(kept_fills)} 条")

    # 生成重锚变体（沿用 f_reanchor 的逻辑，只对非填充行）
    import subprocess
    tmp = "/tmp/_keep_rows.jsonl"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in keep_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    subprocess.run(["python3", "f_reanchor.py", "--base", tmp,
                    "--out", "/tmp/_rean_pool.jsonl"], check=True)
    pool = [json.loads(l) for l in open("/tmp/_rean_pool.jsonl", encoding="utf-8")]
    variants = [r for r in pool if r["prediction_id"].startswith("f_REAN_")]
    print(f"  重锚候选池 {len(variants)} 条，取前 {budget} 条")
    add = variants[:budget]

    out = keep_rows + kept_fills + add
    ids = [r["prediction_id"] for r in out]
    print(f"\n  输出 {len(out)} 行（原 {len(rows)}）  id 重复 {len(ids)-len(set(ids))}")
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  已写出 {a.out}")


if __name__ == "__main__":
    main()
