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
    for _, ep in df.iterrows():
        node = str(ep.get("network_element_id", ""))
        start = _as_ts(ep.get("start_time"))
        end = _as_ts(ep.get("end_time"))
        sub = points[points["network_element_id"].astype(str) == node] if "network_element_id" in points.columns else points.iloc[0:0]
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
    for i in range(n):
        for j in range(i + 1, n):
            aff = incident_affinity(records[i], records[j], entity_graph, point_df, cfg)
            if aff["incident_affinity_score"] < float(cfg.incident_similarity_threshold):
                continue
            edge = {"episode_a": records[i]["episode_id"], "episode_b": records[j]["episode_id"], **aff}
            affinity_edges.append(edge)
            if aff["incident_affinity_score"] >= float(cfg.incident_merge_threshold):
                uf.union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(i)

    incidents: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    ordered_groups = sorted(groups.values(), key=lambda idxs: min(_as_ts(records[i]["start_time"]) or pd.Timestamp.min.tz_localize("UTC") for i in idxs))
    points = point_df.copy()
    if "timestamp_bin" in points.columns:
        points["_ts"] = pd.to_datetime(points["timestamp_bin"], errors="coerce", utc=True)
    for inc_idx, idxs in enumerate(ordered_groups, start=1):
        eps = [records[i] for i in idxs]
        starts = [_as_ts(e["start_time"]) for e in eps]
        ends = [_as_ts(e["end_time"]) for e in eps]
        starts = [t for t in starts if t is not None]
        ends = [t for t in ends if t is not None]
        if not starts or not ends:
            continue
        start, end = min(starts), max(ends)
        nodes = sorted({str(e["network_element_id"]) for e in eps})
        sub = points
        if "_ts" in sub.columns:
            sub = sub[(sub["_ts"] >= start) & (sub["_ts"] <= end)]
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
