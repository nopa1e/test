"""Incident correlation / incidentization.

Episodes are still local anomaly bursts.  Multiple episodes can belong to one
real incident, therefore this module combines temporal, topological, node,
metric-signature and event-signature evidence into coarse incident candidates.
Ambiguous merge/split decisions are deliberately left to the second-stage LLM.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd

from .config import PipelineConfig
from .utils import dump_json, get_logger, to_iso

log = get_logger(__name__)


def _as_ts(value: Any) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(ts) else pd.Timestamp(ts)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return float(inter / union) if union else 0.0


def _temporal_overlap(a_start: pd.Timestamp, a_end: pd.Timestamp, b_start: pd.Timestamp, b_end: pd.Timestamp) -> float:
    inter_start = max(a_start, b_start)
    inter_end = min(a_end, b_end)
    if inter_end >= inter_start:
        inter = (inter_end - inter_start).total_seconds() / 60.0
    else:
        inter = 0.0
    span = max(
        (a_end - a_start).total_seconds() / 60.0,
        (b_end - b_start).total_seconds() / 60.0,
        1.0,
    )
    return max(0.0, min(1.0, inter / span))


def _topology_similarity(
    graph: nx.Graph | nx.DiGraph | None,
    nodes_a: set[str],
    nodes_b: set[str],
    max_distance: int,
) -> float:
    if graph is None or graph.number_of_nodes() == 0 or not nodes_a or not nodes_b:
        return 0.0
    best = None
    for a in nodes_a:
        if a not in graph:
            continue
        for b in nodes_b:
            if b not in graph:
                continue
            try:
                d = nx.shortest_path_length(graph, a, b)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue
            if best is None or d < best:
                best = d
    if best is None:
        return 0.0
    if best > max_distance:
        return 0.0
    return float(1.0 / (1.0 + best))


def _feature_set_from_points(points: pd.DataFrame, limit: int = 8) -> set[str]:
    names: list[str] = []
    for raw in points.get("top_feature_json", pd.Series(dtype=str)).astype(str).tolist()[:20]:
        try:
            items = json.loads(raw)
        except Exception:
            continue
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
    counts = pd.Series(names).value_counts()
    return set(counts.head(limit).index.tolist()) if not counts.empty else set()


def build_episode_signatures(episode_df: pd.DataFrame, point_df: pd.DataFrame) -> pd.DataFrame:
    """Attach metric and event signature columns to an episode dataframe."""
    if episode_df.empty:
        return episode_df.copy()
    df = episode_df.copy()
    sig: list[list[str]] = []
    evt: list[list[str]] = []
    points = point_df.copy()
    if "timestamp_bin" in points.columns:
        points["_ts"] = pd.to_datetime(points["timestamp_bin"], errors="coerce", utc=True)
    # The naive version re-stringified the whole node column once per episode,
    # costing O(n_episodes * n_points) conversions.  Bucket the frame by node up
    # front instead.  ``sort=False`` keeps the original row order inside every
    # bucket, so each episode still sees a bit-identical sub-frame.
    by_node: dict[str, pd.DataFrame] = {}
    if "network_element_id" in points.columns:
        neid = points["network_element_id"].astype(str)
        for node_key, grp in points.groupby(neid, observed=True, sort=False):
            by_node[str(node_key)] = grp
    empty_points = points.iloc[0:0]
    for _, ep in df.iterrows():
        node = str(ep.get("network_element_id", ""))
        start = _as_ts(ep.get("start_time"))
        end = _as_ts(ep.get("end_time"))
        sub = by_node.get(node, empty_points)
        if "_ts" in sub.columns and start is not None and end is not None:
            sub = sub[(sub["_ts"] >= start) & (sub["_ts"] <= end)]
        names = _feature_set_from_points(sub)
        sig.append(sorted(names))
        # Event signature currently uses the strongest metric prefixes; this is
        # deliberately lightweight and can later be replaced with FRR/NetFlow
        # event encoding without changing the affinity API.
        evt.append(sorted({n.split("_", 1)[0] for n in names if n}))
    df["metric_signature"] = sig
    df["event_signature"] = evt
    return df


def incident_affinity(
    episode_a: dict[str, Any] | pd.Series,
    episode_b: dict[str, Any] | pd.Series,
    topology: nx.Graph | nx.DiGraph | None = None,
    point_features: pd.DataFrame | None = None,
    cfg: PipelineConfig | None = None,
) -> dict[str, Any]:
    """Return a weighted affinity score plus its components."""
    cfg = cfg or PipelineConfig()
    a = dict(episode_a)
    b = dict(episode_b)
    a_start = _as_ts(a.get("start_time"))
    a_end = _as_ts(a.get("end_time"))
    b_start = _as_ts(b.get("start_time"))
    b_end = _as_ts(b.get("end_time"))
    if a_start is None or a_end is None or b_start is None or b_end is None:
        return {"incident_affinity_score": 0.0, "reason": "invalid_time"}
    gap_minutes = 0.0
    if b_start > a_end:
        gap_minutes = (b_start - a_end).total_seconds() / 60.0
    elif a_start > b_end:
        gap_minutes = (a_start - b_end).total_seconds() / 60.0
    if gap_minutes > float(cfg.incident_max_time_gap_minutes):
        temporal = 0.0
    else:
        overlap = _temporal_overlap(a_start, a_end, b_start, b_end)
        gap_score = 1.0 - max(0.0, gap_minutes) / max(1.0, float(cfg.incident_max_time_gap_minutes))
        temporal = max(overlap, gap_score)
    node_a = {str(a.get("network_element_id", ""))}
    node_b = {str(b.get("network_element_id", ""))}
    node_overlap = _jaccard(node_a, node_b)
    topo = _topology_similarity(topology, node_a, node_b, int(cfg.incident_topology_distance))
    sig_a = set(a.get("metric_signature") or [])
    sig_b = set(b.get("metric_signature") or [])
    metric_sim = _jaccard(sig_a, sig_b)
    evt_a = set(a.get("event_signature") or [])
    evt_b = set(b.get("event_signature") or [])
    event_sim = _jaccard(evt_a, evt_b)
    score = (
        float(cfg.incident_temporal_weight) * temporal
        + float(cfg.incident_node_overlap_weight) * node_overlap
        + float(cfg.incident_topology_weight) * topo
        + float(cfg.incident_metric_weight) * metric_sim
        + float(cfg.incident_event_weight) * event_sim
    )
    return {
        "incident_affinity_score": float(max(0.0, min(1.0, score))),
        "temporal_similarity": float(temporal),
        "node_overlap": float(node_overlap),
        "topology_similarity": float(topo),
        "metric_signature_similarity": float(metric_sim),
        "event_signature_similarity": float(event_sim),
        "time_gap_minutes": float(gap_minutes),
    }


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_incidents(
    episode_df: pd.DataFrame,
    point_df: pd.DataFrame,
    entity_graph: nx.Graph | nx.DiGraph | None,
    cfg: PipelineConfig,
    dataset_name: str = "dataset",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate episodes into coarse incident candidates."""
    if episode_df.empty:
        return [], {"dataset": dataset_name, "clusters": [], "affinity_edges": []}
    episodes = build_episode_signatures(episode_df, point_df).reset_index(drop=True)
    n = len(episodes)
    uf = _UnionFind(n)
    affinity_edges: list[dict[str, Any]] = []
    records = episodes.to_dict(orient="records")

    # ---- hoist everything incident_affinity() rebuilt for every pair --------
    # The naive loop called incident_affinity() O(n^2) times and each call
    # re-parsed four timestamps plus re-ran a graph shortest path.  Both are
    # pair-independent, so they are computed once here.  The arithmetic below
    # is a literal transcription of incident_affinity()'s body.
    thr = float(cfg.incident_similarity_threshold)
    merge_thr = float(cfg.incident_merge_threshold)
    max_gap = float(cfg.incident_max_time_gap_minutes)
    w_temporal = float(cfg.incident_temporal_weight)
    w_node = float(cfg.incident_node_overlap_weight)
    w_topo = float(cfg.incident_topology_weight)
    w_metric = float(cfg.incident_metric_weight)
    w_event = float(cfg.incident_event_weight)
    max_distance = int(cfg.incident_topology_distance)
    # Diagnostic-only payload: at 1-minute binning a region can produce millions
    # of affinity edges (~2 GB of JSON) that nothing downstream reads.  0 keeps
    # every edge, which is what the equivalence reference does.
    max_edges = int(getattr(cfg, "incident_max_affinity_edges", 0) or 0)
    edges_total = 0

    def _record_edge(edge: dict[str, Any]) -> None:
        nonlocal edges_total
        edges_total += 1
        if max_edges <= 0 or len(affinity_edges) < max_edges:
            affinity_edges.append(edge)

    starts = [_as_ts(r.get("start_time")) for r in records]
    ends = [_as_ts(r.get("end_time")) for r in records]
    node_of = [str(r.get("network_element_id", "")) for r in records]
    sig_of = [set(r.get("metric_signature") or []) for r in records]
    evt_of = [set(r.get("event_signature") or []) for r in records]
    time_ok = [s is not None and e is not None for s, e in zip(starts, ends)]
    episode_ids = [r["episode_id"] for r in records]

    # There are at most |nodes|^2 distinct node pairs, so this cache stays tiny.
    topo_cache: dict[tuple[str, str], float] = {}

    def _topo_sim(node_a: str, node_b: str) -> float:
        cached = topo_cache.get((node_a, node_b))
        if cached is None:
            cached = _topology_similarity(entity_graph, {node_a}, {node_b}, max_distance)
            topo_cache[(node_a, node_b)] = cached
        return cached

    for i in range(n):
        a_valid = time_ok[i]
        if a_valid:
            a_start, a_end = starts[i], ends[i]
            a_node = node_of[i]
            sig_a, evt_a = sig_of[i], evt_of[i]
        ep_a = episode_ids[i]
        for j in range(i + 1, n):
            if not a_valid or not time_ok[j]:
                # incident_affinity() reports invalid_time; that only survives
                # the threshold test under a degenerate (<= 0) configuration.
                if thr > 0.0:
                    continue
                _record_edge({
                    "episode_a": ep_a,
                    "episode_b": episode_ids[j],
                    "incident_affinity_score": 0.0,
                    "reason": "invalid_time",
                })
                if 0.0 >= merge_thr:
                    uf.union(i, j)
                continue
            b_start, b_end = starts[j], ends[j]
            if b_start > a_end:
                gap_minutes = (b_start - a_end).total_seconds() / 60.0
            elif a_start > b_end:
                gap_minutes = (a_start - b_end).total_seconds() / 60.0
            else:
                gap_minutes = 0.0
            if gap_minutes > max_gap:
                temporal = 0.0
            else:
                overlap = _temporal_overlap(a_start, a_end, b_start, b_end)
                gap_score = 1.0 - max(0.0, gap_minutes) / max(1.0, max_gap)
                temporal = overlap if overlap > gap_score else gap_score
            b_node = node_of[j]
            node_overlap = 1.0 if a_node == b_node else 0.0
            topo = _topo_sim(a_node, b_node)
            sig_b = sig_of[j]
            inter = len(sig_a & sig_b)
            union = len(sig_a) + len(sig_b) - inter
            metric_sim = float(inter / union) if union else 0.0
            evt_b = evt_of[j]
            inter = len(evt_a & evt_b)
            union = len(evt_a) + len(evt_b) - inter
            event_sim = float(inter / union) if union else 0.0
            score = float(max(0.0, min(1.0, (
                w_temporal * temporal
                + w_node * node_overlap
                + w_topo * topo
                + w_metric * metric_sim
                + w_event * event_sim
            ))))
            if score < thr:
                continue
            _record_edge({
                "episode_a": ep_a,
                "episode_b": episode_ids[j],
                "incident_affinity_score": score,
                "temporal_similarity": float(temporal),
                "node_overlap": float(node_overlap),
                "topology_similarity": float(topo),
                "metric_signature_similarity": float(metric_sim),
                "event_signature_similarity": float(event_sim),
                "time_gap_minutes": float(gap_minutes),
            })
            if score >= merge_thr:
                uf.union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(i)

    incidents: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    ordered_groups = sorted(groups.values(), key=lambda idxs: min(starts[i] or pd.Timestamp.min.tz_localize("UTC") for i in idxs))
    points = point_df.copy()
    if "timestamp_bin" in points.columns:
        points["_ts"] = pd.to_datetime(points["timestamp_bin"], errors="coerce", utc=True)
    # Every incident used to rescan the whole point table for its time slice,
    # i.e. O(n_incidents * n_points) comparisons.  Sort once and bisect instead.
    # Sorting is safe because the timeline is order-independent: groupby sorts by
    # its key, the node list is re-sorted, and the original row order is restored
    # before the mean is taken, so results stay bit-identical.
    ts_ns = None
    if "_ts" in points.columns:
        points = points.reset_index(drop=True)
        ts_ns = points["_ts"].astype("int64").to_numpy()
        order = np.argsort(ts_ns, kind="stable")
        points = points.iloc[order]
        ts_ns = ts_ns[order]
    for inc_idx, idxs in enumerate(ordered_groups, start=1):
        eps = [records[i] for i in idxs]
        starts_g = [_as_ts(e["start_time"]) for e in eps]
        ends_g = [_as_ts(e["end_time"]) for e in eps]
        starts_g = [t for t in starts_g if t is not None]
        ends_g = [t for t in ends_g if t is not None]
        if not starts_g or not ends_g:
            continue
        start, end = min(starts_g), max(ends_g)
        nodes = sorted({str(e["network_element_id"]) for e in eps})
        if ts_ns is None:
            sub = points
        else:
            # NaT encodes as the int64 minimum, so it sorts to the front and is
            # excluded by ``lo`` -- exactly as the old ``>= start`` mask did.
            lo = int(np.searchsorted(ts_ns, np.int64(start.value), side="left"))
            hi = int(np.searchsorted(ts_ns, np.int64(end.value), side="right"))
            sub = points.iloc[lo:hi].sort_index(kind="stable")
        if "network_element_id" in sub.columns:
            sub = sub[sub["network_element_id"].astype(str).isin(nodes)]
        timeline = []
        if not sub.empty and "_ts" in sub.columns:
            score_col = "combined_anomaly_score" if "combined_anomaly_score" in sub.columns else "final_normal_score"
            for ts, grp in sub.groupby("_ts", observed=True):
                score_vals = pd.to_numeric(grp[score_col], errors="coerce").fillna(0.0)
                timeline.append({
                    "time": to_iso(ts),
                    "nodes": sorted(grp["network_element_id"].astype(str).unique().tolist()),
                    "anomaly": float(score_vals.mean()),
                })
        incident_id = f"INC_{dataset_name}_{inc_idx:04d}"
        incidents.append({
            "incident_id": incident_id,
            "dataset": dataset_name,
            "time_range": {"start": to_iso(start), "end": to_iso(end)},
            "duration_minutes": int(round((end - start).total_seconds() / 60.0)),
            "nodes": nodes,
            "episodes": eps,
            "timeline": timeline,
            "episode_count": len(eps),
            "node_count": len(nodes),
        })
        clusters.append({"incident_id": incident_id, "episode_ids": [e["episode_id"] for e in eps], "nodes": nodes})
    result = {
        "dataset": dataset_name,
        "incidents": incidents,
        "clusters": clusters,
        "affinity_edges": affinity_edges,
    }
    if max_edges > 0:
        # Only present when a cap is actually in force, so an uncapped payload
        # stays byte-identical to the pre-optimisation reference.
        result["affinity_edges_total"] = edges_total
        result["affinity_edges_truncated"] = edges_total > len(affinity_edges)
    log.info("incidentization: %d episodes -> %d incidents", n, len(incidents))
    return incidents, result


def compare_incidents(
    incident_a: dict[str, Any],
    incident_b: dict[str, Any],
    topology: nx.Graph | nx.DiGraph | None = None,
    point_features: pd.DataFrame | None = None,
    cfg: PipelineConfig | None = None,
) -> dict[str, Any]:
    """Structured comparison used by the MCP/LLM incident-correlation task."""
    cfg = cfg or PipelineConfig()
    a_start = _as_ts((incident_a.get("time_range") or {}).get("start"))
    a_end = _as_ts((incident_a.get("time_range") or {}).get("end"))
    b_start = _as_ts((incident_b.get("time_range") or {}).get("start"))
    b_end = _as_ts((incident_b.get("time_range") or {}).get("end"))
    temporal = 0.0
    if all(x is not None for x in (a_start, a_end, b_start, b_end)):
        temporal = _temporal_overlap(a_start, a_end, b_start, b_end)
    nodes_a = {str(x) for x in incident_a.get("nodes", [])}
    nodes_b = {str(x) for x in incident_b.get("nodes", [])}
    return {
        "time_overlap": float(temporal),
        "node_overlap": float(_jaccard(nodes_a, nodes_b)),
        "topology_similarity": float(_topology_similarity(topology, nodes_a, nodes_b, int(cfg.incident_topology_distance))),
        "metric_similarity": float(_jaccard(
            {str(x) for x in incident_a.get("metric_signature", [])},
            {str(x) for x in incident_b.get("metric_signature", [])},
        )),
        "event_similarity": float(_jaccard(
            {str(x) for x in incident_a.get("event_signature", [])},
            {str(x) for x in incident_b.get("event_signature", [])},
        )),
    }


def save_incidents(incidents: list[dict[str, Any]], cluster_payload: dict[str, Any], out_dir: str | Path) -> None:
    from pathlib import Path

    root = Path(out_dir)
    dump_json(root / "incident_candidates.json", {"incidents": incidents})
    dump_json(root / "incident_clusters.json", cluster_payload)
