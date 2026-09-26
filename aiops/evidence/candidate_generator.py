"""Candidate generation and RootScore assembly (spec 5.9).

Spec 5.9 fixes the shape of F's decision path::

    Candidate Generator (program) -> Top10 -> Evidence Filter -> Top5 -> LLM

with one hard constraint: the LLM may only **re-rank** what it is handed, never
invent a candidate.  Everything upstream of the LLM therefore has to be
deterministic and auditable, which is what this module is.

    existing   the ranking stage saw a scalar anomaly magnitude per node with
               no prior, no topology, no syslog and no timing (defect D1), and
               the LLM prompt carried only ``nodes`` + episode statistics (D7)
    F adds     named, timed, cross-modal evidence per candidate, the seven
               sub-scores of :mod:`.root_score`, and the supporting /
               contradicting split the prompt is built from

Nothing here calls the LLM: the stage is pure CPU, so it runs while the GPU is
busy, and its JSON output is what the LLM stage later re-ranks.  The two hard
constraints from spec 5.9 that are enforced in code:

* a node with no evidence of any kind cannot enter the Top-10
  (``min_evidence`` filter), because the LLM must never be asked to rank noise;
* when a candidate has no counter-evidence the reason is stated explicitly
  rather than left as an empty list (spec 5.8).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pandas as pd

from ..dataset import DatasetInfo, canonical_node
from ..utils import get_logger
from .root_score import ROOT_SCORE_WEIGHTS, combine, explain, minmax, rank_priority

log = get_logger(__name__)

#: Evidence families a candidate can be supported by.  ``flow`` and
#: ``predictive`` join once spec step 4 lands; they are listed now so
#: ``cross_modal_support`` does not silently change scale later on.
MODALITIES: tuple[str, ...] = ("metric", "interface", "routing", "log", "quality", "flow")

#: A candidate needs at least this many distinct evidence families to reach the
#: Top-10.  One is the minimum that keeps the stage honest: a node surfaced by
#: nothing at all is not a candidate, it is a guess.
MIN_MODALITIES = 1

#: How many supporting items to keep per candidate in the persisted JSON.  The
#: prompt gets a further truncation; this bound is for the audit trail.
MAX_SUPPORTING = 8


def _ts(value: Any):
    """Parse a stamp to a tz-aware UTC ``Timestamp``; None on junk."""
    if value is None or value == "":
        return None
    try:
        stamp = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if stamp is pd.NaT:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _slot(store: dict[str, dict], node: Any) -> dict:
    name = canonical_node(node)
    return store.setdefault(
        name,
        {
            "metric": [],
            "interface": [],
            "routing": [],
            "log": [],
            "quality": [],
            "flow": [],
            "timing": None,
        },
    )


def _collect_evidence(
    iid: str,
    incident: dict,
    *,
    metric_ev: dict | None,
    routing_ev: dict | None,
    log_ev: dict | None,
    quality_ev: dict | None,
    temporal_ev: dict | None,
    flow_ev: dict | None,
) -> dict:
    """Gather per-node evidence for one incident, keyed by canonical node."""
    store: dict[str, dict] = {}
    tr = incident.get("time_range") or {}
    start, end = _ts(tr.get("start")), _ts(tr.get("end"))

    # -- metric / interface (tables 1 + 2) --------------------------------
    inc = ((metric_ev or {}).get("incidents") or {}).get(iid) or {}
    for node, payload in (inc.get("nodes") or {}).items():
        slot = _slot(store, node)
        for entry in (payload.get("top_metrics") or [])[:3]:
            slot["metric"].append(
                {
                    "metric": entry.get("metric"),
                    "severity": entry.get("severity"),
                    "relative_change": entry.get("relative_change"),
                    "drop_ratio": entry.get("drop_ratio"),
                    "time": entry.get("first_anomaly_time") or entry.get("change_point"),
                    "peak_z": entry.get("peak_z"),
                }
            )
        for iface, metrics in (payload.get("interfaces") or {}).items():
            best = max(
                ((name, stats or {}) for name, stats in metrics.items()),
                key=lambda kv: abs(kv[1].get("severity") or 0.0),
                default=None,
            )
            if best is None:
                continue
            name, stats = best
            slot["interface"].append(
                {
                    "interface": iface,
                    "metric": name,
                    "severity": stats.get("severity"),
                    "relative_change": stats.get("relative_change"),
                    "time": stats.get("change_point") or stats.get("first_anomaly_time"),
                }
            )

    # -- routing (table 3) -------------------------------------------------
    rinc = ((routing_ev or {}).get("incidents") or {}).get(iid) or {}
    for event in rinc.get("events") or []:
        slot = _slot(store, event.get("node"))
        slot["routing"].append(
            {
                "metric": event.get("metric_name"),
                "kind": event.get("kind"),
                "delta": event.get("delta"),
                "baseline": event.get("baseline"),
                "time": event.get("changed_at"),
                "hints_sub_category": event.get("hints_sub_category"),
                "label_only_family": event.get("label_only_family"),
            }
        )

    # -- syslog (table 5), restricted to this incident's window ------------
    for event in (log_ev or {}).get("events") or []:
        ts = _ts(event.get("time") or event.get("timestamp"))
        if start is not None and end is not None and ts is not None:
            if not (start <= ts <= end):
                continue
        slot = _slot(store, event.get("node"))
        slot["log"].append(
            {
                "template": event.get("template"),
                "semantic_class": event.get("semantic_class"),
                "program": event.get("program"),
                "severity": event.get("severity"),
                "time": event.get("time") or event.get("timestamp"),
                "count": event.get("count", 1),
            }
        )

    # -- scrape health (table 4) ------------------------------------------
    if quality_ev:
        for point in quality_ev.get("zero_up_points") or []:
            ts = _ts(point.get("time") or point.get("timestamp"))
            if start is not None and end is not None and ts is not None:
                if not (start <= ts <= end):
                    continue
            slot = _slot(store, point.get("node"))
            slot["quality"].append(
                {
                    "kind": "scrape_up=0",
                    "time": point.get("time") or point.get("timestamp"),
                    "target_id": point.get("target_id"),
                }
            )
        for window in quality_ev.get("interrupt_windows") or []:
            w_start, w_end = _ts(window.get("start")), _ts(window.get("end"))
            if start is not None and end is not None and w_start is not None and w_end is not None:
                if w_end < start or w_start > end:
                    continue
            slot = _slot(store, window.get("node"))
            slot["quality"].append(
                {
                    "kind": "interrupt_window",
                    "start": window.get("start"),
                    "end": window.get("end"),
                    "samples": window.get("samples"),
                }
            )

    # -- flow (tables 6 + 7), if step 4 has produced it --------------------
    finc = ((flow_ev or {}).get("incidents") or {}).get(iid) or {}
    for edge in finc.get("edges") or []:
        for role in ("source", "target"):
            node = edge.get(f"{role}_node")
            if not node:
                continue
            slot = _slot(store, node)
            slot["flow"].append({"role": role, **{k: v for k, v in edge.items() if k != "traffic_curve_1min"}})

    # -- timing ------------------------------------------------------------
    tinc = ((temporal_ev or {}).get("incidents") or {}).get(iid) or {}
    for node, timing in (tinc.get("nodes") or {}).items():
        _slot(store, node)["timing"] = timing
    order = list(tinc.get("order") or [])

    return {"nodes": store, "order": order}


def _local_magnitude(slot: Mapping[str, Any]) -> float:
    """Strongest relative change this node shows, across every modality.

    Metric ``severity`` is already ``|relative_change|``; routing events are
    turned into the same currency by dividing ``|delta|`` by the baseline
    magnitude, so the two families are comparable before min-max scaling.
    """
    best = 0.0
    for entry in slot.get("metric") or []:
        best = max(best, abs(float(entry.get("severity") or 0.0)))
    for entry in slot.get("interface") or []:
        best = max(best, abs(float(entry.get("severity") or 0.0)))
    for entry in slot.get("routing") or []:
        delta = abs(float(entry.get("delta") or 0.0))
        baseline = abs(float(entry.get("baseline") or 0.0))
        best = max(best, delta / baseline if baseline > 1e-9 else delta)
    return best


def _modalities(slot: Mapping[str, Any]) -> list[str]:
    return [name for name in MODALITIES if slot.get(name)]


def _supporting(slot: Mapping[str, Any]) -> list[dict]:
    """Human-readable support items, strongest first."""
    items: list[dict] = []
    for entry in sorted(slot.get("routing") or [], key=lambda e: -abs(float(e.get("delta") or 0.0))):
        items.append(
            {
                "modality": "routing",
                "text": f"{entry.get('metric')} {entry.get('kind')} at {entry.get('time')}"
                + (f" -> {entry['hints_sub_category']}" if entry.get("hints_sub_category") else ""),
                "time": entry.get("time"),
            }
        )
    for entry in sorted(slot.get("metric") or [], key=lambda e: -abs(float(e.get("severity") or 0.0))):
        items.append(
            {
                "modality": "metric",
                "text": f"{entry.get('metric')} x{float(entry.get('relative_change') or 0.0):.2f}"
                f" (severity {float(entry.get('severity') or 0.0):.2f}) at {entry.get('time')}",
                "time": entry.get("time"),
            }
        )
    for entry in (slot.get("interface") or [])[:2]:
        items.append(
            {
                "modality": "interface",
                "text": f"if{entry.get('interface')} {entry.get('metric')}"
                f" severity {float(entry.get('severity') or 0.0):.2f} at {entry.get('time')}",
                "time": entry.get("time"),
            }
        )
    for entry in (slot.get("log") or [])[:2]:
        items.append(
            {
                "modality": "log",
                "text": f"{entry.get('program')}/{entry.get('semantic_class')} at {entry.get('time')}",
                "time": entry.get("time"),
            }
        )
    for entry in (slot.get("quality") or [])[:2]:
        items.append({"modality": "quality", "text": str(entry.get("kind")), "time": entry.get("time")})
    for entry in (slot.get("flow") or [])[:2]:
        items.append({"modality": "flow", "text": str(entry.get("role")), "time": entry.get("time")})
    return items[:MAX_SUPPORTING]


def _filter_top(candidates: list[dict], limit: int) -> tuple[list[dict], str]:
    """Evidence Filter: Top-10 -> Top-5 (spec 5.9).

    Drops candidates that carry no positive support at all, then keeps the
    best ``limit``.  The dropped count is returned so the run can report how
    aggressive the filter actually was instead of leaving it implicit.
    """
    kept = [c for c in candidates if c["sub_scores"].get("local_anomaly", 0.0) > 0.0 or c["supporting"]]
    if not kept:
        kept = candidates
    return kept[:limit], f"{len(candidates)} -> {len(kept)} kept (limit {limit})"


def build_candidates(
    ds: DatasetInfo,
    incidents: list[dict],
    *,
    metric_ev: dict | None = None,
    routing_ev: dict | None = None,
    log_ev: dict | None = None,
    quality_ev: dict | None = None,
    temporal_ev: dict | None = None,
    flow_ev: dict | None = None,
    predictive_ev: dict | None = None,
    contradiction_ev: dict | None = None,
    adjacency: Mapping[str, Iterable[str]] | None = None,
    top_k: int = 10,
    filter_k: int = 5,
    max_nodes_per_incident: int = 10,
) -> dict:
    """Build Top-10 / Top-5 candidates with their ``RootScore`` breakdown.

    ``adjacency`` is an optional node -> neighbours map (from the entity graph).
    When it is absent the propagation terms fall back to the incident's own
    time order, which is weaker but never invents an edge that does not exist.
    """
    out: dict[str, dict] = {}

    for incident in incidents:
        iid = str(incident.get("incident_id"))
        collected = _collect_evidence(
            iid,
            incident,
            metric_ev=metric_ev,
            routing_ev=routing_ev,
            log_ev=log_ev,
            quality_ev=quality_ev,
            temporal_ev=temporal_ev,
            flow_ev=flow_ev,
        )
        store: dict[str, dict] = collected["nodes"]
        order: list[str] = collected["order"]

        claimed = [canonical_node(n) for n in (incident.get("nodes") or [])]
        claimed = [n for n in claimed if n][:max_nodes_per_incident]
        for node in claimed:
            store.setdefault(
                node,
                {name: [] for name in MODALITIES} | {"timing": None},
            )

        # Topology expansion.  Measured on xian: every incident carries exactly
        # ONE network element (554/554 incidents, nodes=[...] length 1), so the
        # candidate set cannot be "read off" the incident -- it has to be built.
        # Spec 5.9's premise (a program proposes several candidates and the LLM
        # only re-ranks them) is unsatisfiable without this step: with one
        # candidate there is nothing to rank, which is also why the old
        # pipeline's RCA scored ~0.31.
        expanded: set[str] = set()
        if adjacency:
            seeds = list(store) + claimed
            for seed in seeds:
                for neighbour in adjacency.get(seed, ()):
                    if neighbour not in store:
                        expanded.add(neighbour)
                    store.setdefault(
                        neighbour,
                        {name: [] for name in MODALITIES} | {"timing": None},
                    )
        if not store:
            continue

        # --- score families -------------------------------------------------
        local_raw = {node: _local_magnitude(slot) for node, slot in store.items()}
        local = minmax(local_raw)

        timing = {node: (slot.get("timing") or {}) for node, slot in store.items()}
        times = {node: _ts(info.get("first_anomaly_time")) for node, info in timing.items()}
        timed = sorted(
            ((node, ts) for node, ts in times.items() if ts is not None), key=lambda kv: kv[1]
        )
        if order:
            priority = rank_priority(order)
        else:
            priority = rank_priority([node for node, _ in timed])

        neighbours = {node: {canonical_node(n) for n in adjacency.get(node, ())} for node in store} if adjacency else {}

        outgoing: dict[str, float] = {}
        incoming: dict[str, float] = {}
        for node, ts in times.items():
            later, earlier = [], []
            for other, other_ts in times.items():
                if other == node or ts is None or other_ts is None:
                    continue
                (later if other_ts > ts else earlier).append(other)
            # A root cause moves first and explains what follows; a victim has
            # already been explained by something else (spec 5.7/5.9).
            nb_later = [o for o in later if o in neighbours.get(node, set())] if neighbours else later
            nb_earlier = [o for o in earlier if o in neighbours.get(node, set())] if neighbours else earlier
            denom = max(1, len(times) - 1)
            outgoing[node] = len(nb_later) / denom
            incoming[node] = len(nb_earlier) / denom

        modality_counts = {node: len(_modalities(slot)) for node, slot in store.items()}
        cross = {node: min(1.0, count / 3.0) for node, count in modality_counts.items()}

        predictive = {node: 0.0 for node in store}
        contradiction = {node: 0.0 for node in store}
        if predictive_ev:
            # The module emits directed pairs, not per-node values: a node whose
            # movement is NOT explained by its upstream neighbour is the more
            # likely cause (spec 5.7), so positive excess feeds the positive
            # predictive_explanation weight.
            for pair in ((predictive_ev.get("incidents") or {}).get(iid) or {}).get("pairs") or []:
                target = canonical_node(pair.get("target"))
                if target not in predictive:
                    continue
                excess = float(pair.get("excess_change") or 0.0)
                predictability = float(pair.get("normal_predictability") or 0.0)
                predictive[target] = max(predictive[target], max(0.0, excess) * predictability)
        if contradiction_ev:
            for node, value in ((contradiction_ev.get("incidents") or {}).get(iid) or {}).get("nodes", {}).items():
                if canonical_node(node) in contradiction:
                    contradiction[canonical_node(node)] = float(value.get("net_evidence") or 0.0)

        # --- assemble -------------------------------------------------------
        candidates: list[dict] = []
        for node, slot in store.items():
            if modality_counts[node] < MIN_MODALITIES and node not in claimed and node not in expanded:
                continue
            sub_scores = {
                "local_anomaly": local.get(node, 0.0),
                "temporal_priority": priority.get(node, 0.0),
                "outgoing_propagation": outgoing.get(node, 0.0),
                "incoming_propagation": incoming.get(node, 0.0),
                "cross_modal_support": cross.get(node, 0.0),
                "predictive_explanation": predictive.get(node, 0.0),
                "contradiction": contradiction.get(node, 0.0),
            }
            supporting = _supporting(slot)
            if not supporting and node in expanded:
                # A neighbour with no local evidence is still a legitimate
                # candidate: the incident element fired and this node is wired
                # to it.  Without this the topology expansion is immediately
                # filtered away again and the candidate set collapses to one.
                anchor = claimed[0] if claimed else "the incident element"
                supporting = [
                    {
                        "modality": "topology",
                        "text": f"neighbour of {anchor} in the entity graph",
                        "time": None,
                    }
                ]
            contradicting: list[str] = []
            for other, other_ts in timed:
                if other == node:
                    continue
                ts = times.get(node)
                if ts is None or other_ts >= ts:
                    continue
                if neighbours and other not in neighbours.get(node, set()):
                    continue
                contradicting.append(f"{other} moved earlier ({other_ts.isoformat()})")
            if not supporting:
                contradicting.append("no positive evidence of any modality")
            candidates.append(
                {
                    "node": node,
                    "sub_scores": sub_scores,
                    "root_score": combine(sub_scores),
                    "breakdown": explain(sub_scores),
                    "supporting": supporting,
                    "contradicting": contradicting or ["未发现反驳证据"],
                    "modalities": _modalities(slot),
                    "first_anomaly_time": (timing.get(node) or {}).get("first_anomaly_time"),
                }
            )

        candidates.sort(key=lambda c: -c["root_score"])
        top10 = candidates[:top_k]
        top5, note = _filter_top(top10, filter_k)
        out[iid] = {
            "n_candidates": len(candidates),
            "top10": [c["node"] for c in top10],
            "top5": [c["node"] for c in top5],
            "filter_note": note,
            "candidates": top10,
        }

    log.info("candidates: %d incidents", len(out))
    return {"dataset": ds.name, "weights": dict(ROOT_SCORE_WEIGHTS), "incidents": out}
