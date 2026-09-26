"""Contradiction evidence: the case *against* each candidate (spec 5.8).

The old pipeline produced exactly one number per node and no notion of evidence
at all, so it could never say "this node looks anomalous, but something upstream
moved first and explains it".  Spec 5.8 makes that case explicit::

    {"candidate": "xian-br-1",
     "supporting":   ["ospf6_neighbor_state_code moved first", ...],
     "contradicting": ["upstream shanghai-cr-1 moved earlier", ...],
     "net_evidence": 0.42}

Two rules from the spec are enforced in code rather than left to the writer:

* an empty ``contradicting`` list is written as the literal string
  ``"未发现反驳证据"`` -- an empty array reads as "not computed", which is a
  different claim;
* ``net_evidence`` is a bounded difference of counts, so the LLM stage can use
  it as a signed signal without needing to know how many items were found.
"""

from __future__ import annotations

import pandas as pd

from ..dataset import DatasetInfo, canonical_node
from ..utils import get_logger

log = get_logger(__name__)

#: The spec's literal marker for "no counter-evidence found".
NO_COUNTER_EVIDENCE = "未发现反驳证据"

#: A metric excursion below this relative change is not worth calling support;
#: it keeps ordinary jitter from counting as evidence in either direction.
_MIN_SEVERITY = 0.25


def _first_times(temporal_ev: dict | None, iid: str) -> dict[str, str]:
    incident = ((temporal_ev or {}).get("incidents") or {}).get(iid) or {}
    return {
        node: info.get("first_anomaly_time")
        for node, info in (incident.get("nodes") or {}).items()
        if info.get("first_anomaly_time")
    }


def _parse(value):
    if not value:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp


def build_contradiction_evidence(
    ds: DatasetInfo,
    incidents: list[dict],
    *,
    metric_ev: dict | None = None,
    routing_ev: dict | None = None,
    log_ev: dict | None = None,
    temporal_ev: dict | None = None,
    predictive_ev: dict | None = None,
    max_candidates: int = 5,
    net_evidence_scale: float = 3.0,
) -> dict:
    """Per-candidate supporting / contradicting split with a signed net score."""
    out: dict[str, dict] = {}

    for incident in incidents:
        iid = str(incident.get("incident_id"))
        times = _first_times(temporal_ev, iid)
        if not times:
            continue
        ordered = sorted(times.items(), key=lambda kv: _parse(kv[1]) or pd.Timestamp.max.tz_localize("UTC"))
        candidates = [node for node, _ in ordered][:max_candidates]

        metric_nodes = ((metric_ev or {}).get("incidents") or {}).get(iid, {}).get("nodes") or {}
        routing_events = ((routing_ev or {}).get("incidents") or {}).get(iid, {}).get("events") or []
        log_events = (log_ev or {}).get("events") or []
        predictive_pairs = ((predictive_ev or {}).get("incidents") or {}).get(iid, {}).get("pairs") or []

        per_candidate: dict[str, dict] = {}
        for rank, node in enumerate(candidates, start=1):
            supporting: list[str] = []
            contradicting: list[str] = []

            payload = metric_nodes.get(node) or {}
            strongest = None
            for entry in payload.get("top_metrics") or []:
                if abs(float(entry.get("severity") or 0.0)) >= _MIN_SEVERITY:
                    strongest = entry
                    break
            if strongest is not None:
                supporting.append(
                    f"metric {strongest.get('metric')} moved x{float(strongest.get('relative_change') or 0.0):.2f}"
                )
            else:
                contradicting.append("no metric exceeded the jitter threshold")

            node_routing = [e for e in routing_events if canonical_node(e.get("node")) == node]
            if node_routing:
                names = sorted({str(e.get("metric_name")) for e in node_routing})[:3]
                supporting.append(f"routing metrics changed: {', '.join(names)}")
            node_logs = [e for e in log_events if canonical_node(e.get("node")) == node]
            if node_logs:
                supporting.append(f"{len(node_logs)} syslog event(s) from this node")

            # What happened *before* this candidate moved.
            for other, stamp in ordered:
                if other == node:
                    continue
                if (_parse(stamp) or pd.Timestamp.max.tz_localize("UTC")) < (
                    _parse(times[node]) or pd.Timestamp.max.tz_localize("UTC")
                ):
                    contradicting.append(f"{other} moved earlier ({stamp})")

            # Predictive: a node whose excursion is fully explained by an
            # upstream neighbour is a victim, not a cause (spec 5.7).
            for pair in predictive_pairs:
                if pair.get("target") != node:
                    continue
                if float(pair.get("excess_change") or 0.0) <= 0.0:
                    contradicting.append(
                        f"explained by {pair.get('source')} "
                        f"(R2={float(pair.get('normal_predictability') or 0.0):.2f}, "
                        f"excess<=0)"
                    )
                    break

            net = (len(supporting) - len(contradicting)) / net_evidence_scale
            per_candidate[node] = {
                "candidate": node,
                "temporal_rank": rank,
                "supporting": supporting or ["no positive evidence found"],
                "contradicting": contradicting or [NO_COUNTER_EVIDENCE],
                "net_evidence": max(-1.0, min(1.0, net)),
            }

        if per_candidate:
            out[iid] = {"nodes": per_candidate, "n_candidates": len(per_candidate)}

    log.info("contradiction evidence: %d incidents", len(out))
    return {
        "dataset": ds.name,
        "no_counter_evidence_marker": NO_COUNTER_EVIDENCE,
        "incidents": out,
    }
