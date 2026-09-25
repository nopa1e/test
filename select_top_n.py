"""Select the top-N predictions by anomaly probability.

The judge scores with a one-to-one match against a known number of real faults.
Submitting everything is cheap on recall but expensive on precision, and
``alpha_fp = 0.7 + 0.3 * Precision`` directly scales the AD component, so the
number of predictions submitted is itself a scoring decision.

``predictions_high_conf.jsonl`` (and the merged/dedup variants) intentionally
carry only the five fields the competition requires - the confidence lives in the
sibling ``llm_anomaly_scores.jsonl`` and the two are joined on ``prediction_id``.

Usage
-----
    python3 select_top_n.py <predictions.jsonl> --top 292 [--out <path>]
                            [--scores <llm_anomaly_scores.jsonl>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, "/202131510121/lyt/aiops_diagnosis")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def load_scores(path: Path) -> dict[str, float]:
    scores: dict[str, float] = {}
    if not path.is_file():
        return scores
    for rec in read_jsonl(path):
        pid = rec.get("prediction_id")
        if pid is None:
            continue
        try:
            scores[str(pid)] = float(rec.get("anomaly_probability") or 0.0)
        except (TypeError, ValueError):
            scores[str(pid)] = 0.0
    return scores


def main() -> int:
    ap = argparse.ArgumentParser(description="Select top-N predictions by confidence.")
    ap.add_argument("input")
    ap.add_argument("--top", type=int, required=True)
    ap.add_argument("--scores", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    src = Path(args.input).resolve()
    if not src.is_file():
        print(f"file not found: {src}")
        return 1
    scores_path = Path(args.scores) if args.scores else src.parent / "llm_anomaly_scores.jsonl"
    out_path = Path(args.out) if args.out else src.parent / f"{src.stem}.top{args.top}.jsonl"

    records = read_jsonl(src)
    scores = load_scores(scores_path)

    scored = [(scores.get(str(r.get("prediction_id")), 0.0), i, r) for i, r in enumerate(records)]
    missing = sum(1 for s, _, _ in scored if s == 0.0)
    # Stable: equal scores keep file order.
    scored.sort(key=lambda t: (-t[0], t[1]))
    picked = [r for _, _, r in scored[: args.top]]

    print(f"input  : {src}")
    print(f"  records       : {len(records)}")
    print(f"scores : {scores_path}  ({len(scores)} entries)")
    print(f"  无置信度记录的条数: {missing}")
    print(f"选前 {args.top} 条:")
    if scored:
        print(f"  置信度区间: {scored[0][0]:.4f} ~ {scored[min(args.top, len(scored)) - 1][0]:.4f}")
        for t in (0.5, 0.9, 0.95, 0.99):
            print(f"    全体中 >= {t:.2f} 的条数: {sum(1 for s, _, _ in scored if s >= t)}")

    ids = [str(r.get("prediction_id")) for r in picked]
    bad_rank = sum(1 for r in picked
                   if [c.get("rank") for c in r.get("root_cause_top5") or []]
                   != list(range(1, len(r.get("root_cause_top5") or []) + 1)))
    dup = sum(1 for r in picked
              if len({c.get("network_element_id") for c in r.get("root_cause_top5") or []})
              != len(r.get("root_cause_top5") or []))
    print(f"校验: 条数={len(picked)}  id 唯一={len(set(ids)) == len(ids)}  "
          f"rank 连续异常={bad_rank}  网元重复异常={dup}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for r in picked:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"写出: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
