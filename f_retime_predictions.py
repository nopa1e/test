"""Apply the F6 interval rebuild to an already-scored prediction file.

Why this is a clean single-variable experiment
----------------------------------------------
B's ``predictions_high_conf.merged.jsonl`` has a measured score (27.6056).  The
rebuild touches **only** ``start_time`` / ``end_time``; ``root_cause_top5`` and
``fault_category`` are copied through byte-for-byte.  So any score movement is
attributable to the intervals alone.

How a prediction is mapped back to its fault extent
---------------------------------------------------
The incident-level stages name predictions ``pred_<dataset>_<idx:06d>`` where
``idx`` is the 1-based position in that dataset's ``incident_candidates.json``.
We use that index to fetch the incident's episodes and rebuild

    start = min(episode.first_abnormal_time)      # 1-minute precision, unquantised
    end   = max(episode.recovery_time)            # return to baseline
    clamp 1 <= duration <= 30 minutes (FAQ Q5); longer -> split into <=30-min windows

Records whose interval no longer matches the source incident (e.g. ones the LLM
merge stage rewrote) fall back to a pure duration clamp, so nothing is dropped
and every record is still retimed by the same rule.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

import pandas as pd

MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 30


def _bounds_from_episodes(incident: dict) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    starts, ends = [], []
    for ep in incident.get("episodes") or []:
        s = ep.get("first_abnormal_time") or ep.get("start_time")
        e = ep.get("recovery_time") or ep.get("end_time")
        if s:
            starts.append(pd.Timestamp(s))
        if e:
            ends.append(pd.Timestamp(e))
    if starts and ends:
        return min(starts), max(ends)
    tr = incident.get("time_range") or {}
    if tr.get("start") and tr.get("end"):
        return pd.Timestamp(tr["start"]), pd.Timestamp(tr["end"])
    return None


def _retime(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    if end <= start:
        end = start + timedelta(minutes=MIN_INTERVAL_MINUTES)
    out, cursor = [], start
    while cursor < end:
        stop = min(cursor + timedelta(minutes=MAX_INTERVAL_MINUTES), end)
        if (stop - cursor).total_seconds() / 60.0 < MIN_INTERVAL_MINUTES:
            break
        out.append((cursor, stop))
        cursor = stop
    return out or [(start, start + timedelta(minutes=MIN_INTERVAL_MINUTES))]


def _duration(rec: dict) -> float:
    return (pd.Timestamp(rec["end_time"]) - pd.Timestamp(rec["start_time"])).total_seconds() / 60.0


def _stats(recs: list[dict]) -> dict:
    d = sorted(_duration(r) for r in recs)
    if not d:
        return {}
    q = lambda f: d[int(f * (len(d) - 1))]
    return {
        "n": len(d), "min": round(d[0], 1), "p25": q(.25), "median": q(.5),
        "p75": q(.75), "p90": q(.9), "max": round(d[-1], 1),
        "le5_pct": round(100 * sum(1 for x in d if x <= 5) / len(d), 1),
        "in_5_15_pct": round(100 * sum(1 for x in d if 5 < x <= 15) / len(d), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="F6 interval rebuild over a prediction file")
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--base-dir", required=True,
                    help="artifacts root holding <dataset>/incident_candidates.json")
    ap.add_argument("--output", required=True)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    preds = [json.loads(l) for l in Path(args.predictions).read_text(encoding="utf-8").splitlines() if l.strip()]
    by_ds: dict[str, list[dict]] = defaultdict(list)
    for r in preds:
        pid = str(r.get("prediction_id", ""))
        parts = pid.split("_")
        # pred_<dataset...>_<idx> ; dataset itself contains underscores
        ds = None
        for i in range(len(parts) - 1, 0, -1):
            if parts[i].isdigit() and len(parts[i]) == 6:
                ds = "_".join(parts[1:i])
                break
        by_ds[ds or "UNKNOWN"].append(r)

    out_records: list[dict] = []
    stats: dict[str, dict] = {}
    for ds, recs in sorted(by_ds.items()):
        cand = Path(args.base_dir) / ds / "incident_candidates.json"
        incidents = []
        if cand.is_file():
            incidents = json.loads(cand.read_text(encoding="utf-8")).get("incidents", [])
        by_index = {str(i.get("incident_id")): i for i in incidents}

        mapped = 0
        produced = 0
        for r in recs:
            pid = str(r.get("prediction_id", ""))
            idx = None
            for part in reversed(pid.split("_")):
                if part.isdigit() and len(part) == 6:
                    idx = int(part)
                    break
            incident = incidents[idx - 1] if idx and 1 <= idx <= len(incidents) else None
            if incident is None:
                # fall back: derive from the record's own interval
                s = pd.Timestamp(r["start_time"])
                e = pd.Timestamp(r["end_time"])
            else:
                bounds = _bounds_from_episodes(incident)
                if bounds is None:
                    s, e = pd.Timestamp(r["start_time"]), pd.Timestamp(r["end_time"])
                else:
                    s, e = bounds
                    mapped += 1
            pieces = _retime(s, e)
            for k, (ps, pe) in enumerate(pieces, 1):
                rec = dict(r)
                if len(pieces) > 1:
                    rec["prediction_id"] = "%s_rt%02d" % (pid, k)
                rec["start_time"] = ps.isoformat(timespec="milliseconds")
                rec["end_time"] = pe.isoformat(timespec="milliseconds")
                out_records.append(rec)
                produced += 1
        stats[ds] = {"in": len(recs), "out": produced, "mapped_to_incident": mapped,
                     "before": _stats(recs)}

    # global stats
    before_all = [r for recs in ([] ,) for r in recs]  # placeholder, filled below
    before_stats = _stats(preds)
    after_stats = _stats(out_records)

    outp = Path(args.output)
    outp.parent.mkdir(parents=True, exist_ok=True)
    with outp.open("w", encoding="utf-8") as fh:
        for r in out_records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # id uniqueness (the judge rejects duplicates)
    ids = [r["prediction_id"] for r in out_records]
    dup = len(ids) - len(set(ids))

    report = {
        "source": args.predictions,
        "base_dir": args.base_dir,
        "output": str(outp),
        "n_in": len(preds), "n_out": len(out_records),
        "duplicate_ids": dup,
        "before": before_stats, "after": after_stats,
        "per_dataset": {k: {kk: vv for kk, vv in v.items() if kk != "before"} for k, v in stats.items()},
    }
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
