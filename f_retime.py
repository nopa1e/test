"""F6 interval rebuild -- spec sections 6.3 / 6.4.

The 1-minute base stage emits raw incidents whose extent is just the span of the
flagged minutes, so the median prediction is ~2 minutes.  That is NOT the F6
product: spec 0.4/6 require the output interval to be

    start = the node's earliest abnormal point (1-minute precision, unquantised)
    end   = the moment it returns to baseline (the episode's recovery_time)
    clamp = 1 <= duration <= 30 minutes (FAQ Q5: a fault lasts <= 1800 s)
            longer -> split into consecutive <=30-minute windows

This is pure post-processing over an existing base run: no model, no LLM, no GPU.
It answers the one question that decides whether F6 is worth a submission --
does the rebuilt interval distribution actually land on the 8~13 minute range the
official sample truths show?
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pandas as pd

MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 30


def _episode_bounds(incident: dict) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    starts: list[pd.Timestamp] = []
    ends: list[pd.Timestamp] = []
    for ep in incident.get("episodes") or []:
        s = ep.get("first_abnormal_time") or ep.get("start_time")
        e = ep.get("recovery_time") or ep.get("end_time")
        if s:
            starts.append(pd.Timestamp(s))
        if e:
            ends.append(pd.Timestamp(e))
    if not starts or not ends:
        tr = incident.get("time_range") or {}
        if tr.get("start") and tr.get("end"):
            return pd.Timestamp(tr["start"]), pd.Timestamp(tr["end"])
        return None
    return min(starts), max(ends)


def rebuild(incident: dict) -> list[dict]:
    bounds = _episode_bounds(incident)
    if bounds is None:
        return []
    start, end = bounds
    if end <= start:
        end = start + timedelta(minutes=MIN_INTERVAL_MINUTES)
    pieces: list[dict] = []
    cursor = start
    index = 0
    while cursor < end:
        stop = min(cursor + timedelta(minutes=MAX_INTERVAL_MINUTES), end)
        if (stop - cursor).total_seconds() / 60.0 < MIN_INTERVAL_MINUTES:
            break
        index += 1
        pieces.append({
            "prediction_id": "%s_rt%02d" % (incident.get("incident_id"), index),
            "incident_id": incident.get("incident_id"),
            "start_time": cursor.isoformat(timespec="milliseconds"),
            "end_time": stop.isoformat(timespec="milliseconds"),
            "duration_minutes": round((stop - cursor).total_seconds() / 60.0, 2),
            "nodes": incident.get("nodes") or [],
        })
        cursor = stop
    return pieces


def _stats(durations: list[float]) -> dict:
    d = sorted(durations)
    if not d:
        return {}
    def q(f: float) -> float:
        return d[int(f * (len(d) - 1))]
    return {
        "n": len(d),
        "min": d[0], "p25": q(.25), "median": q(.5), "p75": q(.75),
        "p90": q(.9), "max": d[-1],
        "le5_pct": round(100 * sum(1 for x in d if x <= 5) / len(d), 1),
        "in_5_15_pct": round(100 * sum(1 for x in d if 5 < x <= 15) / len(d), 1),
        "over30_pct": round(100 * sum(1 for x in d if x > 30) / len(d), 1),
        "mode": Counter(round(x) for x in d).most_common(6),
    }


def process(incident_path: Path, out_dir: Path, label: str) -> dict:
    incidents = json.loads(incident_path.read_text(encoding="utf-8"))["incidents"]
    raw = [float(i.get("duration_minutes") or 0) for i in incidents]
    rebuilt: list[dict] = []
    for inc in incidents:
        rebuilt.extend(rebuild(inc))

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "retimed_intervals.json").write_text(
        json.dumps({"dataset": incident_path.parent.name, "intervals": rebuilt},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "label": label,
        "source": str(incident_path),
        "incidents_before": len(incidents),
        "intervals_after": len(rebuilt),
        "raw_durations": _stats(raw),
        "retimed_durations": _stats([r["duration_minutes"] for r in rebuilt]),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="F6 interval rebuild (spec 6.3/6.4)")
    p.add_argument("--runs", nargs="+", required=True,
                   help="label=path/to/incident_candidates.json pairs")
    p.add_argument("--output-dir", default="outputs_experiment_f_retime")
    args = p.parse_args()

    out_dir = Path(args.output_dir)
    results = []
    for item in args.runs:
        label, _, path = item.partition("=")
        ip = Path(path)
        if not ip.is_file():
            print(f"  !! missing {ip}")
            continue
        results.append(process(ip, out_dir / label, label))

    (out_dir / "retime_report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for r in results:
        print(f"\n=== {r['label']} ===")
        print(f"  incident {r['incidents_before']} -> 区间 {r['intervals_after']}")
        for key in ("raw_durations", "retimed_durations"):
            s = r[key]
            if not s:
                continue
            print(f"  {key:18s} n={s['n']:5d} min={s['min']:.0f} median={s['median']:.1f} "
                  f"p75={s['p75']:.0f} p90={s['p90']:.0f} max={s['max']:.0f} "
                  f"<=5min={s['le5_pct']}% 5-15min={s['in_5_15_pct']}% >30min={s['over30_pct']}%")
            print(f"  {'':18s} 众数={s['mode']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
