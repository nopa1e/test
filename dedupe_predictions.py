"""Deduplicate incident-level predictions that share an identical time window.

Why
---
The competition scores with a one-to-one match: each real fault matches at most
one prediction, and each prediction matches at most one real fault.  The FAQ also
states that two faults never occur at the same time.  Therefore two predictions
with exactly the same (start_time, end_time) inside the same region can never
both match a real fault - at most one of them earns credit, and the rest are
pure false positives that lower Precision, which in turn lowers alpha_fp and
costs AD points.

What this does
--------------
Groups predictions by (region, start_time, end_time).  In each group it keeps
the highest-confidence prediction as the surviving alarm, and folds the other
members' root-cause candidates into its Top5 (without duplicating ids, and
without exceeding five ranks).  That way the surviving alarm carries the best
root-cause evidence of the whole group instead of discarding it.

Confidence is read from a sibling ``llm_anomaly_scores.jsonl`` when present; if
it is absent the first record in file order wins.

Usage
-----
    python3 dedupe_predictions.py <predictions_high_conf.jsonl> \
        [--scores llm_anomaly_scores.jsonl] [--out <path>] [--no-merge-top5]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REGIONS = ("beida", "shenyang", "xian", "chengdu", "wuhan", "shanghai", "nanjing", "guangzhou")


def region_of(record: dict[str, Any]) -> str:
    """Region from the prediction_id, or from any network_element_id."""
    pid = str(record.get("prediction_id", ""))
    for r in REGIONS:
        if r in pid:
            return r
    for c in record.get("root_cause_top5") or []:
        neid = str(c.get("network_element_id", ""))
        for r in REGIONS:
            if neid.startswith(r + "-"):
                return r
    return "unknown"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"  !! line {lineno} is not valid JSON: {exc}", file=sys.stderr)
    return out


def load_scores(path: Path) -> dict[str, float]:
    """prediction_id -> anomaly_probability."""
    scores: dict[str, float] = {}
    if not path.is_file():
        return scores
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = rec.get("prediction_id")
            if pid is None:
                continue
            try:
                scores[str(pid)] = float(rec.get("anomaly_probability") or 0.0)
            except (TypeError, ValueError):
                scores[str(pid)] = 0.0
    return scores


def build_survivor(
    members_sorted: list[dict[str, Any]],
    scores: dict[str, float],
    merge_top5: bool = True,
) -> tuple[dict[str, Any], int]:
    """Collapse one time-window group into a single prediction.

    The members are not clones: measured over experiment B, all 711 duplicated
    windows had differing Top5 lists, and the rank-1 candidate often differed
    too (one window held six predictions whose first choices were br-1, fw, cr-1,
    cr-2 ...).  Simply dropping the lower-confidence ones would throw away those
    alternative first hypotheses.

    So the survivor keeps the highest-confidence member's id, time range and
    category, but its Top5 is rebuilt as:
        1. every member's rank-1 candidate, in member-confidence order, then
        2. the remaining candidates by support count, then best rank, then
           the confidence of the member that proposed them.
    With ranks scored 1.0/0.8/0.6/0.4/0.2, promoting a candidate from absent to
    rank 4 is worth 0.4 of the RCA component for that fault.
    """
    base = json.loads(json.dumps(members_sorted[0]))  # deep copy
    old = [str(c.get("network_element_id")) for c in base.get("root_cause_top5") or []]

    if len(members_sorted) == 1 or not merge_top5:
        top5 = base.get("root_cause_top5") or []
        for i, cand in enumerate(top5, 1):
            cand["rank"] = i
        base["root_cause_top5"] = top5
        return base, 0

    support: Counter = Counter()
    best_rank: dict[str, int] = {}
    first_conf: dict[str, float] = {}
    primaries: list[str] = []

    for member in members_sorted:
        conf = scores.get(str(member.get("prediction_id")), 0.0)
        cands = member.get("root_cause_top5") or []
        for cand in cands:
            nid = str(cand.get("network_element_id", ""))
            if not nid:
                continue
            support[nid] += 1
            try:
                r = int(cand.get("rank") or 99)
            except (TypeError, ValueError):
                r = 99
            best_rank[nid] = min(best_rank.get(nid, 99), r)
            first_conf.setdefault(nid, conf)
        if cands:
            top = str(cands[0].get("network_element_id", ""))
            if top and top not in primaries:
                primaries.append(top)

    rest = sorted(
        (n for n in support if n not in primaries),
        key=lambda n: (-support[n], best_rank.get(n, 99), -first_conf.get(n, 0.0)),
    )
    final = (primaries + rest)[:5]

    base["root_cause_top5"] = [{"rank": i, "network_element_id": n} for i, n in enumerate(final, 1)]
    added = sum(1 for n in final if n not in old)
    return base, added


def main() -> int:
    ap = argparse.ArgumentParser(description="Deduplicate predictions sharing a time window.")
    ap.add_argument("input", help="predictions_high_conf.jsonl")
    ap.add_argument("--scores", default=None, help="llm_anomaly_scores.jsonl (default: sibling file)")
    ap.add_argument("--out", default=None, help="output path (default: <input>.dedup.jsonl)")
    ap.add_argument("--no-merge-top5", action="store_true",
                    help="Do not fold the dropped records' root causes into the survivor.")
    args = ap.parse_args()

    src = Path(args.input).resolve()
    if not src.is_file():
        print(f"file not found: {src}")
        return 1
    scores_path = Path(args.scores) if args.scores else src.parent / "llm_anomaly_scores.jsonl"
    out_path = Path(args.out) if args.out else src.with_suffix("").with_suffix("").parent / (src.stem + ".dedup.jsonl")

    records = read_jsonl(src)
    scores = load_scores(scores_path)
    print(f"input    : {src}")
    print(f"  records: {len(records)}")
    print(f"scores   : {scores_path}  ({len(scores)} entries)")

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        key = (region_of(rec), str(rec.get("start_time", "")), str(rec.get("end_time", "")))
        groups[key].append(rec)

    survivors: list[dict[str, Any]] = []
    merged_slots = 0
    collapsed = 0
    for key, members in groups.items():
        members_sorted = sorted(members, key=lambda r: -scores.get(str(r.get("prediction_id")), 0.0))
        base, added = build_survivor(members_sorted, scores, merge_top5=not args.no_merge_top5)
        if len(members_sorted) > 1:
            collapsed += len(members_sorted) - 1
            merged_slots += added
        survivors.append(base)

    survivors.sort(key=lambda r: (region_of(r), str(r.get("start_time", ""))))

    before = Counter(region_of(r) for r in records)
    after = Counter(region_of(r) for r in survivors)
    print()
    print("%-12s %8s %8s %8s" % ("region", "before", "after", "removed"))
    print("-" * 42)
    for r in sorted(before):
        print("%-12s %8d %8d %8d" % (r, before[r], after.get(r, 0), before[r] - after.get(r, 0)))
    print("-" * 42)
    print("%-12s %8d %8d %8d" % ("TOTAL", len(records), len(survivors), collapsed))
    print()
    print(f"时间窗去重: {len(records)} -> {len(survivors)}  (去掉 {collapsed} 条重复时间窗)")
    print(f"合并进幸存条目的根因候选: {merged_slots} 个")
    reduced = 100.0 * collapsed / max(1, len(records))
    if len(records):
        prec_before = 292 / len(records)
        prec_after = 292 / max(1, len(survivors))
        print(f"Precision 上限: {prec_before:.4f} -> {prec_after:.4f}"
              f"   (alpha_fp {0.7 + 0.3 * prec_before:.4f} -> {0.7 + 0.3 * prec_after:.4f})")
    print(f"削减比例: {reduced:.1f}%")

    # sanity: ids unique, ranks contiguous, no duplicate element ids
    ids = [r["prediction_id"] for r in survivors]
    bad_rank = sum(1 for r in survivors
                   if [c.get("rank") for c in r.get("root_cause_top5") or []] != list(range(1, len(r.get("root_cause_top5") or []) + 1)))
    bad_dup = sum(1 for r in survivors
                  if len({c.get("network_element_id") for c in r.get("root_cause_top5") or []}) != len(r.get("root_cause_top5") or []))
    print()
    print(f"校验: id 唯一={len(set(ids)) == len(ids)}  rank 连续异常={bad_rank}  网元重复异常={bad_dup}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in survivors:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n写出: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
