#!/usr/bin/env python3
"""把 ep1b 的第一批预测与未动的第二批合并，并与旧基座做结构对比。

对比口径（只比结构，不猜分数）：
  * 行数、时间窗数量与宽度分布
  * 新旧基座的重叠率：旧基座的每一条，是否被新基座某条以 Dice>=0.4 覆盖
    （Dice 用 |A|+|B| 作分母，与评测口径一致——注意不是并集口径）
  * major_category 分布
  * 根因 top1 的节点分布
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

B1 = "outputs_experiment_f_ep1/final/predictions_f_all.jsonl"
B2 = "outputs_experiment_f_stage2/final_stage2/predictions_f_all.jsonl"
OLD = "submissions/submit_stage12.jsonl"


def load(p: str | Path) -> list[dict]:
    return [json.loads(line) for line in open(p, encoding="utf-8") if line.strip()]


def parse(ts: str):
    from datetime import datetime
    return datetime.fromisoformat(ts)


def dice(a: dict, b: dict) -> float:
    """|A n B| * 2 / (|A| + |B|)  -- the contest's denominator, not the union."""
    s1, e1 = parse(a["start_time"]), parse(a["end_time"])
    s2, e2 = parse(b["start_time"]), parse(b["end_time"])
    inter = (min(e1, e2) - max(s1, s2)).total_seconds()
    if inter <= 0:
        return 0.0
    len1 = (e1 - s1).total_seconds()
    len2 = (e2 - s2).total_seconds()
    if len1 + len2 <= 0:
        return 0.0
    return 2.0 * inter / (len1 + len2)


def width_minutes(row: dict) -> float:
    return (parse(row["end_time"]) - parse(row["start_time"])).total_seconds() / 60.0


def batch_of(row: dict) -> str:
    return row["prediction_id"].split("_")[3][:8]


def describe(name: str, rows: list[dict]) -> None:
    per_batch = collections.Counter(batch_of(r) for r in rows)
    widths = collections.defaultdict(list)
    for r in rows:
        widths[batch_of(r)].append(width_minutes(r))
    majors = collections.Counter((r.get("fault_category") or {}).get("major_category") for r in rows)
    print(f"\n=== {name}: {len(rows)} 行 ===")
    for b in sorted(per_batch):
        w = sorted(widths[b])
        med = w[len(w) // 2] if w else 0
        print(f"  batch {b}: {per_batch[b]:6d} 条, 窗宽 中位 {med:5.1f} 分  "
              f"最小 {min(w):5.1f}  最大 {max(w):7.1f}")
    print("  major_category:", dict(majors.most_common()))


def coverage(old_rows: list[dict], new_rows: list[dict], label: str) -> None:
    """How much of the old prediction set survives in the new one."""
    by_batch = collections.defaultdict(list)
    for r in new_rows:
        by_batch[batch_of(r)].append(r)
    hit = miss = 0
    miss_examples = []
    for r in old_rows:
        cands = by_batch.get(batch_of(r), [])
        best = 0.0
        for c in cands:
            d = dice(r, c)
            if d > best:
                best = d
                if best >= 0.4:
                    break
        if best >= 0.4:
            hit += 1
        else:
            miss += 1
            if len(miss_examples) < 5:
                miss_examples.append((r["prediction_id"], round(best, 3)))
    total = hit + miss
    print(f"\n=== 旧集在新集中的存活率（{label}）===")
    print(f"  Dice>=0.4 命中 {hit}/{total} = {hit / max(1, total) * 100:.1f}%   丢失 {miss}")
    if miss_examples:
        print("  丢失样例 (id, 最佳 Dice):")
        for pid, d in miss_examples:
            print(f"    {pid}  {d}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="submissions/submit_ep1b_base.jsonl")
    ap.add_argument("--b1", default=B1)
    ap.add_argument("--b2", default=B2)
    ap.add_argument("--old", default=OLD)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    b1 = load(a.b1)
    b2 = load(a.b2)
    merged = b1 + b2
    ids = [r["prediction_id"] for r in merged]
    dup = len(ids) - len(set(ids))
    print(f"batch1(ep1b) {len(b1)} + batch2(未动) {len(b2)} = {len(merged)} 行, id 重复 {dup}")

    describe("ep1b 合并基座", merged)
    old = load(a.old)
    describe("旧基座 submit_stage12", old)
    coverage(old, merged, "旧基座 vs ep1b 合并基座")

    if a.write:
        if dup:
            raise SystemExit("refusing to write: duplicate prediction_id")
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for r in merged:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\n已写出 {a.out} ({len(merged)} 行)")


if __name__ == "__main__":
    main()
