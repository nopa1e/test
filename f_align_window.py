#!/usr/bin/env python3
"""窗口起点对齐：把「明显早于强异常点」的预测窗口后移，其余一律不动。

动机
----
实测（第二批）预测起点相对窗口内高异常点：中位 −1.0 分钟（提前），
但尾部很重——提前 >2 分钟占 17.1%、>10 分钟占 9.5%。
对第二批 2 分钟的预测：
    真值 10:00–10:02，预测 09:59–10:01 → 重叠 1 分钟 → Dice 0.50（勉强过线）
    若起点后移到 10:00            → 完全覆盖   → Dice 1.00
即起点是决定 Dice 的**上游**变量（决定谁进入计分），而时长只是把窗口拉宽。

本脚本只做「高风险修复」而非整体平移：
    · 仅当 预测起点 早于 窗口内首个强异常点 超过 --min-lead 分钟时才后移；
    · 后移时保持原时长不变（end = new_start + 原时长）；
    · 其余条目逐字节不动。
因此它同时具备「上游杠杆」与「低方差」两个性质。

用法：
  python3 f_align_window.py --input <preds.jsonl> --workspace <ws> --output <out.jsonl>
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import timedelta

import pandas as pd

SPAN_RE = re.compile(r"(\d{14}_\d{14})")


def load_high_points(points_dir: str, region: str, quantile: float = 0.99) -> list:
    """从产物目录读取该区域的 point_scores，返回高异常时刻升序列表。

    注意：point_scores.csv.gz 在**产物目录**（outputs_experiment_*）里，
    不在原始数据 workspace 里 —— 第一次实现就是栽在这个路径上。
    """
    import glob as _glob
    pat = os.path.join(points_dir, f"{region}_*", "point_scores.csv.gz")
    files = _glob.glob(pat)
    if not files:
        return []
    df = pd.read_csv(files[0], usecols=["timestamp_bin", "combined_anomaly_score"])
    thr = df["combined_anomaly_score"].quantile(quantile)
    sub = df[df["combined_anomaly_score"] >= thr]
    t = pd.to_datetime(sub["timestamp_bin"], utc=True).dt.tz_localize(None)
    return sorted(set(t.tolist()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--points-dir", dest="points_dir", required=True,
                    help="含 point_scores.csv.gz 的产物目录（outputs_experiment_*）")
    ap.add_argument("--output", required=True)
    ap.add_argument("--min-lead", type=float, default=2.0,
                    help="仅当提前量超过该分钟数时才后移")
    ap.add_argument("--quantile", type=float, default=0.99,
                    help="强异常点分位（生成点表时用；此处仅记录）")
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.input, encoding="utf-8") if l.strip()]
    regions = sorted({r["prediction_id"].split("_")[2] for r in rows})
    print(f"输入 {len(rows)} 条，区域 {len(regions)} 个")

    hp = {}
    for r in regions:
        hp[r] = load_high_points(a.points_dir, r, a.quantile)
        print(f"  {r}: 强异常点 {len(hp[r])}")

    moved = skipped = nohit = 0
    leads = []
    out = []
    for r in rows:
        reg = r["prediction_id"].split("_")[2]
        s = pd.Timestamp(r["start_time"]).tz_convert("UTC").tz_localize(None)
        e = pd.Timestamp(r["end_time"]).tz_convert("UTC").tz_localize(None)
        dur = e - s
        pool = hp.get(reg) or []
        inside = [t for t in pool if s <= t <= e]
        r2 = dict(r)
        if not inside:
            nohit += 1
            out.append(r2)
            continue
        first = min(inside)
        lead = (s - first).total_seconds() / 60.0     # 负=提前
        leads.append(lead)
        if lead < -a.min_lead:                        # 提前过多 → 后移
            ns = first
            r2["start_time"] = ns.isoformat() + "+00:00"
            r2["end_time"] = (ns + dur).isoformat() + "+00:00"
            moved += 1
        else:
            skipped += 1
        out.append(r2)

    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    with open(a.output, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n窗口内无强异常点: {nohit}")
    print(f"后移修正: {moved}    保持不动: {skipped}")
    print(f"输出 {a.output}")
    if leads:
        import numpy as np
        arr = np.array(leads)
        print(f"提前量分布(分钟): 中位 {np.median(arr):+.1f}  p10 {np.percentile(arr,10):+.1f}  "
              f">2分钟 {(arr<-2).mean()*100:.1f}%")


if __name__ == "__main__":
    main()
