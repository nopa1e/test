#!/usr/bin/env python3
"""离线量化候选提交对"高分数异常连续段"的召回率（v2，按段计）。

代理真值的构造（对齐 §28 的口径与评测口径）
--------------------------------------------
1. 取 `combined_anomaly_score >= 阈值` 的栅格点；阈值同时试分位阈值与绝对 0.95。
2. 相邻（间隔 <= 1 个栅格）的高分点合并成**连续段**——一个故障对应一段，
   而不是一个点（按点计会重复计数，把长故障算成很多个）。
3. 真值窗取该段的 `[首, 末]`；若段长为 0，则取一个栅格宽 `[t, t+bin]`。
4. 评测按 Dice 匹配时间窗且**不看根因**，故召回只需在时间轴上衡量：
   候选集中存在某窗口 W 使 `Dice(真值窗, W) >= 0.4` 即记该段被覆盖。

注意：覆盖率**不是**分数。§31 已实测"填得越满覆盖越高但分数会掉"
（匹配结构损伤 + α_fp 稀释）。本指标只回答一个具体问题：
**ep1b 相对旧基座，在时间轴上多覆盖了多少真实故障段。**
"""
from __future__ import annotations

import argparse
import collections
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

BIN_MINUTES = 5
DICE_GATE = 0.4


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def bucketize(windows, cell: float = 300.0):
    buckets: dict[int, list[tuple[float, float]]] = collections.defaultdict(list)
    for s, e in windows:
        for h in range(int(s // cell), int(e // cell) + 1):
            buckets[h].append((s, e))
    return buckets


def best_dice(t0: float, t1: float, buckets, cell: float = 300.0) -> float:
    best = 0.0
    span = max(t1 - t0, 1.0)
    for h in range(int(t0 // cell), int(t1 // cell) + 1):
        for (s, e) in buckets.get(h, ()):
            inter = min(t1, e) - max(t0, s)
            if inter <= 0:
                continue
            denom = span + (e - s)
            if denom <= 0:
                continue
            d = 2.0 * inter / denom
            if d > best:
                best = d
                if best >= DICE_GATE:
                    return best
    return best


def segments(times: list[float], gap: float) -> list[tuple[float, float]]:
    """把有序时间戳合并成连续段。"""
    out: list[list[float]] = []
    for t in times:
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [(a, b if b > a else a + BIN_MINUTES * 60) for a, b in out]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="+", default=[
        "outputs_experiment_f_full:20260819040000_20260902040000",
        "outputs_experiment_f_stage2:20260917040000_20260924040000",
    ])
    ap.add_argument("--candidates", nargs="+", default=[
        "submissions/submit_stage12.jsonl",
        "submissions/submit_ep1b_dedup95.jsonl",
        "submissions/submit_ep1b_rean.jsonl",
        "submissions/submit_fill2.jsonl",
    ])
    ap.add_argument("--quantile", type=float, default=0.9)
    a = ap.parse_args()

    # 候选窗口按批次日期分组
    cand: dict[tuple[str, str], dict] = {}
    for c in a.candidates:
        p = Path(c)
        if not p.is_file():
            print(f"[skip] {c}")
            continue
        rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
        for _, span in (s.split(":") for s in a.pairs):
            d0 = span[:8]
            sel = [(parse(r["start_time"]).timestamp(), parse(r["end_time"]).timestamp())
                   for r in rows if parse(r["start_time"]).strftime("%Y%m%d") == d0]
            cand[(c, span)] = bucketize(sel)
        print(f"[load] {Path(c).name}: {len(rows)} 行")

    agg = collections.defaultdict(lambda: [0, 0, 0.0])
    for spec in a.pairs:
        root, span = spec.split(":")
        root_p = Path(root)
        if not root_p.is_dir():
            continue
        for region_dir in sorted(p for p in root_p.iterdir() if p.is_dir() and span in p.name):
            ps = region_dir / "point_scores.csv.gz"
            if not ps.is_file():
                continue
            df = pd.read_csv(ps, usecols=["timestamp_bin", "combined_anomaly_score"])
            score = pd.to_numeric(df["combined_anomaly_score"], errors="coerce").fillna(0.0)
            ts = pd.to_datetime(df["timestamp_bin"], errors="coerce", utc=True)
            q_thr = float(score.quantile(a.quantile))

            for label, thr in (("q90", q_thr), (">=0.95", 0.95)):
                times = sorted({t.timestamp() for t in ts[score >= thr].dropna()})
                if not times:
                    continue
                segs = segments(times, gap=BIN_MINUTES * 60)
                for c in a.candidates:
                    bk = cand.get((c, span))
                    if bk is None:
                        continue
                    hits = 0
                    dsum = 0.0
                    for (s, e) in segs:
                        d = best_dice(s, e, bk)
                        dsum += d
                        if d >= DICE_GATE:
                            hits += 1
                    cell = agg[(span[:8], label, Path(c).name)]
                    cell[0] += hits
                    cell[1] += len(segs)
                    cell[2] += dsum

    print("\n================ 汇总（按连续段计） ================")
    print(f"{'批次':10s} {'代理':8s} {'候选':34s} {'命中/段数':>14s} {'覆盖率':>8s} {'平均Dice':>9s}")
    for (b, label, name), (hit, tot, dsum) in sorted(agg.items()):
        print(f"{b:10s} {label:8s} {name:34s} {hit:7d}/{tot:<6d} "
              f"{hit / max(1, tot) * 100:7.1f}% {dsum / max(1, tot):9.4f}")


if __name__ == "__main__":
    main()
