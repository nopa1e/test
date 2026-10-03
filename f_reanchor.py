#!/usr/bin/env python3
"""为每条预测补一条「重锚到异常起点」的窗口变体（不替换原窗口）。

动机
----
实测（2026-10-03）：当前预测窗口与其自身节点的 >=0.9 异常段相比，
Dice 中位仅 0.500、**41.4% 低于 0.4 门槛** —— 连自己检出的异常都对不上。
而异常段时长中位 3 分钟、预测窗口中位 10 分钟，说明问题在**对齐**而非宽度
（§18 已实测第一阶段 10 分钟优于 5 分钟，故不动宽度）。

离线评估（n=7782）：
    方案              Dice中位   Dice均值   >=0.4 比例
    当前单窗            0.500     0.497      58.6%
    +错位变体(±W/4)     0.500     0.504      59.5%   <- 无效
    重锚到异常起点       0.600     0.540      75.1%   <- +16.5pp

为什么不直接替换
----------------
§19 记录过全局窗口对齐的失败（-1.6878）。所以这里**保留原窗口、另加一条变体**，
由全局最大权匹配自行挑选对齐更好的那条。由于 ΣS_AD 对匹配边集单调不减，
该做法在 Dice 指标上**严格不劣于现状**，代价只有 alpha_fp 的约 1%
（len 翻倍，但 alpha_fp 有 0.7 地板）。
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
            _c[k] = d
    return _c[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="submissions/submit_ainj90.jsonl")
    ap.add_argument("--out", required=True)
    ap.add_argument("--only-worse-than", type=float, default=1.0,
                    help="只为「与自身异常段 Dice 低于它」的预测补变体（1.0 = 全部）")
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.base, encoding="utf-8")]
    print(f"基线 {len(rows)} 条")

    new = []
    for i, r in enumerate(rows):
        pid = r["prediction_id"]
        reg = pid.split("_")[2]
        span = next((s for s in SPANS if s in pid), None)
        if span is None:
            continue
        root, W = SPANS[span]
        d = pts(root, reg, span)
        if d is None:
            continue
        s = pd.Timestamp(r["start_time"]).tz_localize(None)
        e = pd.Timestamp(r["end_time"]).tz_localize(None)
        nodes = [x["network_element_id"].split("-", 1)[1] for x in r["root_cause_top5"]]
        # 在 top5 节点里找窗口附近最强的那段 >=0.9 异常
        best = None
        for nd in nodes:
            g = d[(d["network_element_id"] == f"{reg}-{nd}") &
                  (d["timestamp_bin"] >= s - pd.Timedelta(minutes=45)) &
                  (d["timestamp_bin"] <= e + pd.Timedelta(minutes=45))]
            if g.empty:
                continue
            hi = g[g["combined_anomaly_score"] >= 0.9].sort_values("timestamp_bin")
            if hi.empty:
                continue
            ct = [hi.iloc[0]["timestamp_bin"]]
            segs = []
            for t in list(hi["timestamp_bin"])[1:]:
                if (t - ct[-1]).total_seconds() <= 300:
                    ct.append(t)
                else:
                    segs.append((ct[0], ct[-1])); ct = [t]
            segs.append((ct[0], ct[-1]))
            for a2, b2 in segs:
                lo, hi2 = max(s, a2), min(e, b2)
                inter = max(0.0, (hi2 - lo).total_seconds())
                tot = (e - s).total_seconds() + (b2 - a2).total_seconds()
                dc = 2 * inter / tot if tot > 0 else 0.0
                if best is None or dc > best[0]:
                    best = (dc, a2)
        if best is None:
            continue
        dc, onset = best
        if dc >= a.only_worse_than:
            continue
        ns = onset
        ne = ns + pd.Timedelta(minutes=W)
        if ns == s and ne == e:
            continue
        new.append({
            "prediction_id": f"f_REAN_{reg}_{pid.split('_')[2]}_{i:06d}",
            "start_time": ns.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
            "end_time": ne.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"),
            "root_cause_top5": [dict(x) for x in r["root_cause_top5"]],
            "fault_category": dict(r["fault_category"]),
        })
        if (i + 1) % 5000 == 0:
            print(f"  ...{i+1}/{len(rows)}", flush=True)

    ids = {r["prediction_id"] for r in rows}
    dup = sum(1 for p in new if p["prediction_id"] in ids)
    print(f"\n  新增变体 {len(new)} 条  (id 冲突 {dup})")
    print(f"  总行数 {len(rows) + len(new)}")
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in rows + new:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  已写出 {a.out}")


if __name__ == "__main__":
    main()
