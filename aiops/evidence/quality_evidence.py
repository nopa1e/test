"""Quality evidence: table 4 (scrape_health).

Spec section 5.3.  Never used anywhere in the existing pipeline.  Two things it
buys:

1. a **discriminator** -- when another table has missing data, was the node
   actually down or did collection fail?
2. a **rare strong signal** -- ``scrape_up == 0`` is ~0.07% of samples and lines
   up with fault injection windows; ``scrape_samples`` dropping means the node
   exposed fewer series, i.e. an interface or process disappeared.

Three measured facts about this table (correct doc 13.6 item 3):

* ``scrape_error`` is the literal ``NULL`` on 262,037 of 262,080 rows -- all of
  them successes, i.e. the column is only populated when a scrape actually
  failed.  It stays unused here, but is normalised so nobody later reads
  ``"NULL"`` as an error string.
* sample drops must be measured **per (node, target_id)**, never per node.  A
  router runs two exporters side by side: ``node_exporter`` exposes ~1040 series
  while ``routing_exporter`` on the same box exposes ~50, so a node-level median
  sits between the two and flags *every* routing sample as a drop.  Measured on
  xian, 40,320 of the 40,500 reported points were exactly that artefact -- and
  the 2,000-point output cap then pushed the 180 real points out entirely.  With
  the key fixed the same threshold (0.8 x the series' own median) reports 180
  points on xian and 30 on beida, every one a real ``scrape_up == 0`` minute.
  The threshold was never the problem: healthy series vary by ~2 % (service-vm-1
  spans 1556..1589 around a median of 1579).
* interrupt windows must be built from **consecutive** zero-up minutes.  The
  first version took each target's first and last zero and called everything in
  between an outage, which merged xian's 45 isolated one-minute blips -- spread
  over six days -- into a single "2026-08-22 21:43 -> 2026-08-28 19:15 outage".
  Runs are now split wherever consecutive zero minutes are further apart than
  ``interrupt_gap_minutes``.

Bucket count: xian has 20160 distinct minutes on all 13 series; beida has 20159
on *all 13 simultaneously*, i.e. the export simply ends one minute short -- a
boundary, not a collection gap.  No window is derived from bucket count at all.
"""

from __future__ import annotations

import pandas as pd

from ..dataset import DatasetInfo, canonical_node, read_table
from ..utils import get_logger
from .table_usage import TableUsage
from .values import clean_numeric, clean_text, missing_cell_counts

log = get_logger(__name__)

#: A sample counts as a drop when it falls below this fraction of its own
#: series median.  Measured: healthy series move by ~2 %, so this only fires on
#: real interruptions.
DEFAULT_DROP_RATIO = 0.8

#: Zero-up minutes further apart than this belong to separate outages.
DEFAULT_INTERRUPT_GAP_MINUTES = 5


def _interrupt_runs(node: str, target: str, stamps: list, gap: pd.Timedelta) -> list[dict]:
    """Split one target's zero-up minutes into genuinely consecutive runs."""
    windows: list[dict] = []
    run_start = 0
    for i in range(1, len(stamps) + 1):
        if i == len(stamps) or (stamps[i] - stamps[i - 1]) > gap:
            windows.append({
                "node": node,
                "target_id": target,
                "start": pd.Timestamp(stamps[run_start]).isoformat(),
                "end": pd.Timestamp(stamps[i - 1]).isoformat(),
                "samples": i - run_start,
            })
            run_start = i
    return windows


def build_quality_evidence(
    ds: DatasetInfo,
    usage: TableUsage,
    *,
    drop_ratio: float = DEFAULT_DROP_RATIO,
    interrupt_gap_minutes: float = DEFAULT_INTERRUPT_GAP_MINUTES,
) -> dict:
    df = read_table(ds, "scrape_health")
    if df.empty:
        return {"dataset": ds.name, "interrupt_windows": [], "sample_drop_points": [], "zero_up_points": []}

    missing = missing_cell_counts(df)
    df["timestamp"] = pd.to_datetime(clean_text(df["timestamp"]), errors="coerce", utc=True)
    df["node"] = clean_text(df["node"]).map(lambda n: canonical_node(n) if pd.notna(n) else "unknown")
    df["scrape_up"] = clean_numeric(df.get("scrape_up"))
    df["scrape_samples"] = clean_numeric(df.get("scrape_samples"))
    df["scrape_duration_seconds"] = clean_numeric(df.get("scrape_duration_seconds"))
    if "scrape_error" in df.columns:
        df["scrape_error"] = clean_text(df["scrape_error"])

    exporters = sorted(df["exporter_type"].dropna().astype(str).unique().tolist()) if "exporter_type" in df else []
    usage.record(
        "scrape_health",
        len(df),
        exporters=exporters,
        targets=int(df["target_id"].nunique()) if "target_id" in df else None,
        fields_used=["scrape_up", "scrape_samples", "scrape_duration_seconds"],
        missing_cells=missing or None,
    )

    zero = df[df["scrape_up"] == 0]
    zero_points = [
        {
            "node": str(r.node),
            "target_id": str(r.target_id),
            "exporter_type": str(r.exporter_type),
            "time": pd.Timestamp(r.timestamp).isoformat(),
        }
        for r in zero.itertuples(index=False)
    ]

    # Sample-count drops: a target exposing markedly fewer series than its own
    # median.  Grouped per (node, target_id) -- see the module docstring for why
    # a node-level baseline is wrong.
    drops: list[dict] = []
    for (node, target), grp in df.groupby(["node", "target_id"], observed=True):
        med = grp["scrape_samples"].median()
        if not pd.notna(med) or med <= 0:
            continue
        low = grp[grp["scrape_samples"] < drop_ratio * med]
        for r in low.itertuples(index=False):
            drops.append({
                "node": str(node),
                "target_id": str(target),
                "time": pd.Timestamp(r.timestamp).isoformat(),
                "scrape_samples": float(r.scrape_samples) if pd.notna(r.scrape_samples) else None,
                "target_median": float(med),
                "scrape_up": int(r.scrape_up) if pd.notna(r.scrape_up) else None,
            })

    # Interrupt windows: consecutive zero-up minutes per target.
    gap = pd.Timedelta(minutes=interrupt_gap_minutes)
    windows: list[dict] = []
    for (node, target), grp in zero.groupby(["node", "target_id"], observed=True):
        grp = grp.sort_values("timestamp")
        if grp.empty:
            continue
        windows.extend(_interrupt_runs(str(node), str(target), list(grp["timestamp"]), gap))

    drops_sorted = sorted(drops, key=lambda d: d["time"])
    windows_sorted = sorted(windows, key=lambda w: (w["start"], w["target_id"]))
    return {
        "dataset": ds.name,
        "exporter_types": exporters,
        "zero_up_count": int(len(zero_points)),
        "zero_up_points": zero_points,
        "interrupt_windows": windows_sorted,
        "interrupt_window_count": len(windows_sorted),
        "interrupt_gap_minutes": interrupt_gap_minutes,
        "sample_drop_points": drops_sorted[:2000],
        "sample_drop_count": len(drops),
        "sample_drop_ratio_threshold": drop_ratio,
        "sample_drop_baseline": "per (node, target_id) median",
        "sample_drop_during_outage": sum(1 for d in drops if d["scrape_up"] == 0),
    }
