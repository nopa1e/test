#!/usr/bin/env python3
"""离线量化候选提交对"高分数异常时刻"的召回率。

没有官方真值，但有一个可信的代理：分数超过分位阈值的栅格点极可能就是真实
故障注入点（§28 的实测依据：阈值 0.8209 之上的 2667 个连续段里 1982 个长度为 1）。

评测按 Dice 匹配时间窗，**不看根因**，所以召回只需在时间轴上衡量：
对每个异常时刻 t，其真值窗近似为一个栅格宽 [t, t+bin]，若候选集中存在某窗口
W 使 Dice([t,t+bin], W) >= 0.4，则记 t 被覆盖。

输出各候选集在 8 个区域上的覆盖率，用来判断 ep1b 相对旧基座是否真的提高了召回。
"""
from __future__ import annotations

import argparse
import collections
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def load_windows(path: str | Path, batch: str) -> list[tuple[float, float]]:
    out = []
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        r = json.loads(line)
        pid = r["prediction_id"]
        # reanchor 变体的 id 里嵌的是区域名而不是批次时间戳，用窗口本身判批次
        s = parse(r["start_time"])
        if s.strftime("%Y%m%d") not in ("20260819", "20260917"):
            continue
        if not pid.split("_")[3].startswith(batch[:8]) and batch not in pid:
            # 兜底：按窗口起始日期归属
            pass
        out.append((s.timestamp(), parse(r["end_time"]).timestamp()))
    return out


def bucketize(windows: list[tuple[float, float]], cell: float = 300.0):
    buckets: dict[int, list[tuple[float, float]]] = collections.defaultdict(list)
    for s, e in windows:
        for h in range(int(s // cell), int(e // cell) + 1):
            buckets[h].append((s, e))
    return buckets


def covered(t: float, width: float, buckets, cell: float = 300.0) -> bool:
    """Is there a window W with Dice([t,t+width], W) >= 0.4 ?"""
    target = (t, t + width)
    best = 0.0
    for h in range(int(target[0] // cell), int(target[1] // cell) + 1):
        for (s, e) in buckets.get(h, ()):  # noqa: E501
            inter = min(target[1], e) - max(target[0], s)
            if inter <= 0:
                continue
            denom = (target[1] - target[0]) + (e - s)
            if denom <= 0:
                continue
            d = 2.0 * inter / denom
            if d > best:
                best = d
                if best >= 0.4:
                    return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+",
                    default=["outputs_experiment_f_full", "outputs_experiment_f_stage2"])
    ap.add_argument("--spans", nargs="+",
                    default=["20260819040000_20260902040000", "20260917040000_20260924040000"])
    ap.add_argument("--candidates", nargs="+", default=[
        "submissions/submit_stage12.jsonl",
        "submissions/submit_ep1b_dedup95.jsonl",
        "submissions/submit_ep1b_rean.jsonl",
        "submissions/submit_fill2.jsonl",
    ])
    ap.add_argument("--quantile", type=float, default=0.9)
    ap.add_argument("--wide-threshold", type=float, default=0.95,
                    help="另按此绝对分数统计一次（更严的代理真值）")
    a = ap.parse_args()

    cand_windows = {}
    for c in a.candidates:
        p = Path(c)
        if not p.is_file():
            print(f"[skip] {c} 不存在")
            continue
        rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
        for span in a.spans:
            # 归属只按窗口起始日期判定：reanchor 变体的 prediction_id 里嵌的是
            # 区域名而不是批次时间戳，按 id 解析会漏掉它们。
            d0 = span[:8]
            sel = [r for r in rows if parse(r["start_time"]).strftime("%Y%m%d") == d0]
            cand_windows[(c, span)] = bucketize(
                [(parse(r["start_time"]).timestamp(), parse(r["end_time"]).timestamp()) for r in sel])
            print(f"[load] {Path(c).name} @ {d0}: {len(sel)} 行")

    print(f"\n{'region':34s} {'异常时刻':>8s} {'候选':>26s} {'覆盖率':>8s}")
    summary = collections.defaultdict(lambda: [0, 0])
    for root, span in zip(a.roots, a.spans):
        root_p = Path(root)
        if not root_p.is_dir():
            continue
        for region_dir in sorted(p for p in root_p.iterdir() if p.is_dir() and span in p.name):
            ps = region_dir / "point_scores.csv.gz"
            if not ps.is_file():
                continue
            df = pd.read_csv(ps, usecols=["timestamp_bin", "combined_anomaly_score"])
            score = pd.to_numeric(df["combined_anomaly_score"], errors="coerce").fillna(0.0)
            thr = float(score.quantile(a.quantile))
            ts = pd.to_datetime(df["timestamp_bin"], errors="coerce", utc=True)
            for label, mask in (("q90", score >= thr), (">=0.95", score >= a.wide_threshold)):
                times = sorted({t.timestamp() for t in ts[mask].dropna()})
                if not times:
                    continue
                for c in a.candidates:
                    key = (c, span)
                    if key not in cand_windows:
                        continue
                    bk = cand_windows[key]
                    hit = sum(1 for t in times if covered(t, 300.0, bk))
                    summary[(span, label, c)][0] += hit
                    summary[(span, label, c)][1] += len(times)
                    print(f"{region_dir.name[:34]:34s} {len(times):8d} "
                          f"{Path(c).name:>26s} {hit / len(times) * 100:7.1f}%")
                break  # 只打印 q90 那张明细表，避免刷屏

    print("\n================ 汇总 ================")
    for (span, label, c), (hit, tot) in sorted(summary.items()):
        print(f"  batch {span[:8]}  真值代理 {label:6s}  {Path(c).name:34s} "
              f"{hit:6d}/{tot:6d} = {hit / max(1, tot) * 100:5.1f}%")


if __name__ == "__main__":
    main()
