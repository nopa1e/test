#!/usr/bin/env python3
"""离线评分代理：把官方 AD 公式套在代理真值上，用来给候选提交排序。

官方公式（已确认口径）
----------------------
    Score_AD = ( Σ_{i∈TP} S_AD(i) / N_true ) × α_fp × 40
    α_fp     = 0.7 + 0.3 · tp / len          （0.7 为硬地板）
    S_AD(i)  = 0.7 + 0.3 · max(0, 1 − (Δs + Δe) / 360 秒)

其中 len = 该批次提交的预测条数，Δs/Δe 为匹配到的预测窗口与真值窗口的起止偏移。
匹配门槛：Dice ≥ 0.4，且为全局最大权 1 对 1（本脚本用贪心近似，见下）。

代理真值：同 f_recall_offline2.py（高分数连续段，段窗取 [首,末]，段长为 0 时取一栅格）。

**为什么需要它**：覆盖率只回答"覆盖了多少"，漏掉了 α_fp 这个密度权衡——
多加预测会同时抬高 tp（分子、且抬高 α_fp）和 len（压低 α_fp）。
这正是密度阶梯上要权衡的东西，必须显式算出来。

**它不能替代评测**：代理真值里绝大多数段很可能是噪声（覆盖率只有个位数百分比
正说明了这点），所以绝对数值没有意义，**只用于候选之间的相对排序**。
匹配用贪心而非匈牙利，也会带来轻微偏差。
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
GRID_SECONDS = 300.0


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def bucketize(windows, cell: float = GRID_SECONDS):
    buckets: dict[int, list[int]] = collections.defaultdict(list)
    for idx, (s, e) in enumerate(windows):
        for h in range(int(s // cell), int(e // cell) + 1):
            buckets[h].append(idx)
    return buckets


def best_match(t0: float, t1: float, wins, buckets, cell: float = GRID_SECONDS):
    """Return (dice, ds, de) of the best-overlapping window, or None."""
    best_d = 0.0
    best = None
    span = max(t1 - t0, 1.0)
    for h in range(int(t0 // cell), int(t1 // cell) + 1):
        for idx in buckets.get(h, ()):
            s, e = wins[idx]
            inter = min(t1, e) - max(t0, s)
            if inter <= 0:
                continue
            denom = span + (e - s)
            if denom <= 0:
                continue
            d = 2.0 * inter / denom
            if d > best_d:
                best_d = d
                best = (d, abs(s - t0), abs(e - t1))
    return best


def segments(times: list[float], gap: float) -> list[tuple[float, float]]:
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
    ap.add_argument("--candidates", nargs="+", required=True)
    ap.add_argument("--quantile", type=float, default=0.9)
    ap.add_argument("--score-threshold", type=float, default=None,
                    help="改用绝对分数阈值（默认用分位阈值）")
    a = ap.parse_args()

    # 载入候选，按 span 切分
    cand_by_span: dict[str, dict[str, list]] = collections.defaultdict(dict)
    for c in a.candidates:
        rows = [json.loads(x) for x in open(c, encoding="utf-8") if x.strip()]
        for spec in a.pairs:
            span = spec.split(":")[1]
            lo_s, hi_s = span.split("_")
            lo = pd.Timestamp(f"{lo_s[:4]}-{lo_s[4:6]}-{lo_s[6:8]}")
            hi = pd.Timestamp(f"{hi_s[:4]}-{hi_s[4:6]}-{hi_s[6:8]}") + pd.Timedelta(days=1)
            sel = []
            for r in rows:
                ts = pd.Timestamp(r["start_time"]).tz_localize(None)
                if lo <= ts < hi:
                    sel.append((ts.timestamp(),
                                pd.Timestamp(r["end_time"]).tz_localize(None).timestamp()))
            cand_by_span[span][c] = sel
        print(f"[load] {Path(c).name}: {len(rows)} 行")

    result = collections.defaultdict(
        lambda: collections.defaultdict(lambda: collections.defaultdict(float)))
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
            thr = a.score_threshold if a.score_threshold is not None else float(score.quantile(a.quantile))
            times = sorted({t.timestamp() for t in ts[score >= thr].dropna()})
            segs = segments(times, gap=BIN_MINUTES * 60)

            for c in a.candidates:
                wins = cand_by_span[span][c]
                bk = bucketize(wins)
                tp = 0
                sad = 0.0
                for (s, e) in segs:
                    m = best_match(s, e, wins, bk)
                    if m and m[0] >= DICE_GATE:
                        tp += 1
                        ds, de = m[1], m[2]
                        sad += 0.7 + 0.3 * max(0.0, 1.0 - (ds + de) / 360.0)
                cell = result[span][c]
                cell["tp"] += tp
                cell["n_seg"] += len(segs)
                cell["sad"] += sad
                # len 是该批次提交的总条数，只能赋一次值。
                # 早先写成 += len(wins) 会把整批的条数按区域重复累加 8 次，
                # 使 α_fp 被严重低估。
                cell["n_pred"] = len(wins)

    print("\n============ 离线 AD 代理 ============")
    print(f"{'批次':10s} {'候选':34s} {'tp':>6s} {'N段':>7s} {'len':>7s} "
          f"{'α_fp':>6s} {'ΣS_AD':>9s} {'AD代理':>8s}")
    for span in sorted(result):
        for c in a.candidates:
            d = result[span].get(c)
            if not d:
                continue
            tp, nseg, npr, sad = d["tp"], d["n_seg"], d["n_pred"], d["sad"]
            # α_fp 里的 tp 是**被匹配上的预测条数**，不会超过 len
            alpha = 0.7 + 0.3 * min(tp, npr) / max(1, npr)
            ad = (sad / max(1, nseg)) * alpha * 40.0
            print(f"{span[:8]:10s} {Path(c).name:34s} {tp:6.0f} {nseg:7.0f} {npr:7.0f} "
                  f"{alpha:6.4f} {sad:9.1f} {ad:8.4f}")
        print()


if __name__ == "__main__":
    main()
