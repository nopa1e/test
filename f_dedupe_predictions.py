#!/usr/bin/env python3
"""预测去重：同一真值上的重复预测只保留代表，降低 len(pred) 以抬升 alpha_fp。

背景
----
匈牙利匹配是**一对一**的：一个真值只能配一条预测，其周围其余预测全部计入 FP。
实测 final_r13 的 4517 条预测只铺在 425 个时间并集段上，平均每段 10.63 条，
平均每条预测与 9.43 条其他预测在时间上重叠 —— 即 8.5 条预测才换 1 条命中，
alpha_fp = 0.7 + 0.3 * tp / len(pred) 因此被压在 0.74。

两种策略
--------
1. ``--mode dice``   段内贪心：新预测与已保留代表的 Dice >= 阈值则丢弃。
   保守，保留部分交叠者，适合担心丢 tp 时使用。
2. ``--mode disjoint`` 段内取最大互不重叠子集（按结束时间贪心）。
   任意两条 Dice = 0，理论下界，最激进但保证不互相抢名额。

代表选取：段内优先保留「与同段其他预测平均 Dice 最高」的那条（最具代表性），
而非简单保留第一条。只改条数，start/end/root_cause_top5/fault_category 原样保留。
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _sec(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds()


def dice(p, q) -> float:
    """区间 Dice 系数：2|交| / (|p| + |q|)。"""
    s = max(p[0], q[0])
    e = min(p[1], q[1])
    overlap = _sec(s, e)
    if overlap <= 0:
        return 0.0
    return 2.0 * overlap / (_sec(p[0], p[1]) + _sec(q[0], q[1]))


def _centrality(item, group) -> float:
    """与同组其他预测的平均 Dice，越大越具代表性。"""
    if len(group) <= 1:
        return 0.0
    tot = sum(dice(item, other) for other in group if other is not item)
    return tot / (len(group) - 1)


def dedupe_dice(group, threshold: float):
    """贪心：按代表性从高到低，保留与已选者 Dice 全部 < 阈值 的条目。"""
    ordered = sorted(group, key=lambda x: _centrality(x, group), reverse=True)
    kept = []
    for it in ordered:
        if all(dice(it, k) < threshold for k in kept):
            kept.append(it)
    return kept


def dedupe_disjoint(group):
    """区间调度：最大互不重叠子集（按结束时间贪心）。"""
    ordered = sorted(group, key=lambda x: x[1])
    kept, last_end = [], None
    for it in ordered:
        if last_end is None or it[0] >= last_end:
            kept.append(it)
            last_end = it[1]
    return kept


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--mode", choices=["dice", "disjoint"], default="dice")
    ap.add_argument("--threshold", type=float, default=0.8, help="仅 dice 模式使用")
    a = ap.parse_args()

    rows = [json.loads(line) for line in open(a.input, encoding="utf-8") if line.strip()]
    items = []
    for r in rows:
        items.append((parse(r["start_time"]), parse(r["end_time"]), r))
    items.sort(key=lambda x: (x[0], x[1]))

    # 时间并集切段
    groups, cur = [], [items[0]]
    for it in items[1:]:
        if it[0] < max(x[1] for x in cur):
            cur.append(it)
        else:
            groups.append(cur)
            cur = [it]
    groups.append(cur)

    kept = []
    for g in groups:
        kept.extend(dedupe_dice(g, a.threshold) if a.mode == "dice" else dedupe_disjoint(g))

    kept.sort(key=lambda x: (x[0], x[1]))
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    with open(a.output, "w", encoding="utf-8") as fh:
        for _, _, r in kept:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"模式={a.mode} 阈值={a.threshold if a.mode == 'dice' else '-'}")
    print(f"  输入 {len(rows)} 条 -> 输出 {len(kept)} 条   (压缩 {len(rows)/len(kept):.2f}x)")
    print(f"  时段 {len(groups)} 段，段均保留 {len(kept)/len(groups):.2f} 条")
    print(f"  输出 {a.output}")


if __name__ == "__main__":
    main()
