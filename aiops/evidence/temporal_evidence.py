"""Temporal evidence: who moved first (spec section 5.6).

``propagation.py`` has always computed a temporal-precedence score, but that
score only ever reached the RCA-GNN (defect D6): the ranking stage and the LLM
prompt never saw it.  This module surfaces it.

    existing   per-node anomaly magnitudes, arrival order unspecified
    F adds     ``first_anomaly_time`` per node at the data's own precision,
               the resulting order, and the gap to the runner-up

Spec 5.6's acceptance criterion is that the stamp is **never re-quantised**, so
the timestamps come straight from the evidence modules' own change points
(``metric_evidence`` CUSUM indices, ``routing_evidence`` ``changed_at``, syslog
event times, scrape-outage points) rather than from any re-binned frame.

The module is a *consumer*: it takes the already-built evidence payloads so a
run does not pay for re-reading the tables.  Nothing here needs the LLM.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..dataset import DatasetInfo, canonical_node
from ..utils import get_logger

log = get_logger(__name__)

#: Tie-break order when two sources report the very same stamp.  A routing
#: metric crossing its baseline is mechanism-level evidence (spec 5.2 calls it
#: the most valuable signal in this dataset); syslog is a discrete event;
#: scrape health is a collection-side signal; a smoothed metric excursion is
#: the softest of the four.
_SOURCE_PRIORITY = {"routing": 0, "log": 1, "quality": 2, "metric": 3}


def _to_ts(value: Any):
    """Parse a stamp into a tz-aware UTC ``Timestamp``; return None on junk."""
    if value is None or value == "":
        return None
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if ts is pd.NaT:
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _record(store: dict[str, list[tuple]], node: Any, value: Any, source: str) -> None:
    ts = _to_ts(value)
    if ts is None:
        return
    name = canonical_node(node)
    if not name:
        return
    store.setdefault(name, []).append((ts, source))


def _metric_observations(metric_ev: dict | None, iid: str) -> dict[str, list[tuple]]:
    store: dict[str, list[tuple]] = {}
    incident = ((metric_ev or {}).get("incidents") or {}).get(iid) or {}
    for node, payload in (incident.get("nodes") or {}).items():
        for entry in payload.get("top_metrics") or []:
            _record(
                store,
                node,
                entry.get("first_anomaly_time") or entry.get("change_point"),
                f"metric:{entry.get('metric')}",
            )
        # Interface-level movers are the only sub-node localisation the old
        # pipeline never had, so they count as first-mover evidence too.
        for iface, metrics in (payload.get("interfaces") or {}).items():
            for metric, stats in (metrics or {}).items():
                _record(
                    store,
                    node,
                    (stats or {}).get("change_point") or (stats or {}).get("first_anomaly_time"),
                    f"interface:{iface}:{metric}",
                )
    return store


def _routing_observations(routing_ev: dict | None, iid: str) -> dict[str, list[tuple]]:
    store: dict[str, list[tuple]] = {}
    incident = ((routing_ev or {}).get("incidents") or {}).get(iid) or {}
    for event in incident.get("events") or []:
        _record(
            store,
            event.get("node"),
            event.get("changed_at"),
            f"routing:{event.get('metric_name')}",
        )
    return store


def _log_observations(log_ev: dict | None, wanted: set[str]) -> dict[str, list[tuple]]:
    store: dict[str, list[tuple]] = {}
    for event in (log_ev or {}).get("events") or []:
        node = canonical_node(event.get("node"))
        if wanted and node not in wanted:
            continue
        _record(
            store,
            node,
            event.get("time") or event.get("timestamp"),
            f"log:{event.get('semantic_class') or event.get('program') or 'event'}",
        )
    return store


def _quality_observations(quality_ev: dict | None, start, end) -> dict[str, list[tuple]]:
    """Only the outage points that fall inside *this* incident's window."""
    store: dict[str, list[tuple]] = {}
    if not quality_ev or start is None or end is None:
        return store
    for point in quality_ev.get("zero_up_points") or []:
        ts = _to_ts(point.get("time") or point.get("timestamp"))
        if ts is None or not (start <= ts <= end):
            continue
        _record(store, point.get("node"), ts, "quality:scrape_up=0")
    return store


def _key(item: tuple) -> tuple:
    ts, source = item
    return ts, _SOURCE_PRIORITY.get(str(source).split(":", 1)[0], 9)


def build_temporal_evidence(
    ds: DatasetInfo,
    incidents: list[dict],
    metric_ev: dict | None = None,
    routing_ev: dict | None = None,
    log_ev: dict | None = None,
    quality_ev: dict | None = None,
    *,
    max_nodes_per_incident: int = 10,
) -> dict:
    """First-mover order for every incident (spec 5.6).

    Returns ``{"dataset", "incidents": {iid: {"order", "nodes", ...}}}`` where
    ``nodes[node]`` carries ``first_anomaly_time`` / ``source`` / ``rank`` /
    ``gap_to_second_seconds``.  Incidents with no timed evidence at all are
    omitted rather than emitted empty, so callers can tell "no timing" from
    "timing says nothing moved".
    """
    out: dict[str, dict] = {}
    for incident in incidents:
        iid = str(incident.get("incident_id"))
        tr = incident.get("time_range") or {}
        start, end = _to_ts(tr.get("start")), _to_ts(tr.get("end"))

        nodes = [canonical_node(n) for n in (incident.get("nodes") or [])]
        nodes = [n for n in nodes if n][:max_nodes_per_incident]

        store: dict[str, list[tuple]] = {}
        for partial in (
            _metric_observations(metric_ev, iid),
            _routing_observations(routing_ev, iid),
            _log_observations(log_ev, set(nodes)),
            _quality_observations(quality_ev, start, end),
        ):
            for node, items in partial.items():
                store.setdefault(node, []).extend(items)

        # Keep only nodes this incident actually claims, plus anything the
        # evidence itself surfaced (an upstream neighbour can matter more than
        # a listed node -- that is exactly the F hypothesis).
        if not store:
            continue

        firsts = {node: min(items, key=_key) for node, items in store.items()}
        ordered = sorted(firsts.items(), key=lambda kv: kv[1][0])

        runner_up = ordered[1][1][0] if len(ordered) > 1 else None
        per_node: dict[str, dict] = {}
        for rank, (node, (ts, source)) in enumerate(ordered, start=1):
            per_node[node] = {
                "first_anomaly_time": ts.isoformat(),
                "source": source,
                "n_observations": len(store[node]),
                "sources": sorted({src for _, src in store[node]}),
                "rank": rank,
                "gap_to_second_seconds": (
                    (runner_up - ts).total_seconds() if rank == 1 and runner_up is not None else None
                ),
            }

        out[iid] = {
            "order": [node for node, _ in ordered],
            "nodes": per_node,
            "n_nodes_with_timing": len(per_node),
            "window": [tr.get("start"), tr.get("end")],
        }

    log.info("temporal evidence: %d/%d incidents carry timing", len(out), len(incidents))
    return {"dataset": ds.name, "incidents": out}
