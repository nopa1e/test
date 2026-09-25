"""Feature engineering for the RCA-oriented GNN."""
from __future__ import annotations

from typing import Any

import networkx as nx
import numpy as np
import pandas as pd

from .config import PipelineConfig
from .utils import get_logger

log = get_logger(__name__)


def _type_one_hot(node_type: str, kinds: list[str]) -> dict[str, float]:
    """One-hot the node kind using the dataset's own node_type values.

    The previous version matched substrings of the element id ("br-", "fw", ...);
    the kinds are now whatever the dataset actually reports, discovered at run
    time, so a new element kind needs no code change.
    """
    t = str(node_type or "").strip().lower()
    return {f"type_{k}": (1.0 if t == k else 0.0) for k in kinds}


def build_rca_features(
    incidents: list[dict[str, Any]],
    episode_df: pd.DataFrame,
    point_df: pd.DataFrame,
    propagation_by_incident: dict[str, dict[str, Any]],
    entity_graph: nx.Graph | nx.DiGraph | None,
    cfg: PipelineConfig,
    dataset_name: str = "dataset",
) -> pd.DataFrame:
    """Return one row per (incident, candidate node)."""
    if not incidents or point_df.empty:
        return pd.DataFrame()
    score_col = cfg.episode_score_column if cfg.episode_score_column in point_df.columns else "combined_anomaly_score"
    if score_col not in point_df.columns:
        score_col = "final_normal_score"
    points = point_df.copy()
    if "timestamp_bin" in points.columns:
        points["_ts"] = pd.to_datetime(points["timestamp_bin"], errors="coerce", utc=True)

    # Node kind per element, straight from the dataset's node_type column, and
    # the set of kinds actually present, so the one-hot columns are data-driven.
    type_map: dict[str, str] = {}
    if {"network_element_id", "node_type"} <= set(point_df.columns):
        for neid, ntype in point_df[["network_element_id", "node_type"]].drop_duplicates().itertuples(index=False, name=None):
            type_map[str(neid)] = str(ntype).strip().lower()
    node_kinds = sorted(k for k in set(type_map.values()) if k and k != "nan")

    centrality: dict[str, dict[str, float]] = {}
    if entity_graph is not None and entity_graph.number_of_nodes() > 0:
        try:
            pr = nx.pagerank(entity_graph.to_undirected()) if entity_graph.is_directed() else nx.pagerank(entity_graph)
        except Exception:
            pr = {n: 0.0 for n in entity_graph.nodes()}
        for node in entity_graph.nodes():
            centrality[str(node)] = {
                "degree_centrality": float(entity_graph.degree(node)) if entity_graph.is_directed() else float(entity_graph.degree(node)),
                "pagerank": float(pr.get(node, 0.0)),
                "in_degree": float(entity_graph.in_degree(node)) if entity_graph.is_directed() else float(entity_graph.degree(node)),
                "out_degree": float(entity_graph.out_degree(node)) if entity_graph.is_directed() else float(entity_graph.degree(node)),
            }

    rows: list[dict[str, Any]] = []
    for incident in incidents:
        inc_id = str(incident.get("incident_id", ""))
        nodes = [str(x) for x in incident.get("nodes", [])]
        time_start = pd.to_datetime((incident.get("time_range") or {}).get("start"), errors="coerce", utc=True)
        time_end = pd.to_datetime((incident.get("time_range") or {}).get("end"), errors="coerce", utc=True)
        prop = propagation_by_incident.get(inc_id, {})
        prop_nodes = {str(x.get("network_element_id")): x for x in prop.get("nodes", [])}
        for node in nodes:
            sub = points[points["network_element_id"].astype(str) == node] if "network_element_id" in points.columns else points.iloc[0:0]
            if "_ts" in sub.columns and pd.notna(time_start) and pd.notna(time_end):
                sub = sub[(sub["_ts"] >= time_start) & (sub["_ts"] <= time_end)]
            vals = pd.to_numeric(sub.get(score_col, pd.Series(dtype=float)), errors="coerce").dropna()
            eps = episode_df[episode_df["network_element_id"].astype(str) == node] if not episode_df.empty and "network_element_id" in episode_df.columns else episode_df.iloc[0:0]
            if not eps.empty:
                eps = eps[(pd.to_datetime(eps["start_time"], errors="coerce", utc=True) <= time_end) & (pd.to_datetime(eps["end_time"], errors="coerce", utc=True) >= time_start)]
            p = prop_nodes.get(node, {})
            c = centrality.get(node, {})
            row = {
                "incident_id": inc_id,
                "dataset": dataset_name,
                "network_element_id": node,
                "point_mean": float(vals.mean()) if len(vals) else 0.0,
                "point_max": float(vals.max()) if len(vals) else 0.0,
                "point_min": float(vals.min()) if len(vals) else 0.0,
                "point_p95": float(vals.quantile(0.95)) if len(vals) else 0.0,
                "point_std": float(vals.std(ddof=0)) if len(vals) > 1 else 0.0,
                "point_count": int(len(vals)),
                "episode_count": int(len(eps)),
                "episode_max_peak": float(pd.to_numeric(eps.get("peak_anomaly_score", pd.Series(dtype=float)), errors="coerce").max()) if not eps.empty else 0.0,
                "episode_mean_score": float(pd.to_numeric(eps.get("mean_anomaly_score", pd.Series(dtype=float)), errors="coerce").mean()) if not eps.empty else 0.0,
                "episode_max_duration": float(pd.to_numeric(eps.get("duration_minutes", pd.Series(dtype=float)), errors="coerce").max()) if not eps.empty else 0.0,
                "episode_mean_persistence": float(pd.to_numeric(eps.get("persistence", pd.Series(dtype=float)), errors="coerce").mean()) if not eps.empty else 0.0,
                "episode_max_growth": float(pd.to_numeric(eps.get("growth_rate", pd.Series(dtype=float)), errors="coerce").max()) if not eps.empty else 0.0,
                "temporal_precedence_score": float(p.get("temporal_precedence_score", 0.0)),
                "upstream_score": float(p.get("upstream_score", 0.0)),
                "downstream_impact_score": float(p.get("downstream_impact_score", 0.0)),
                "propagation_consistency_score": float(p.get("propagation_consistency_score", 0.0)),
                "degree_centrality": float(c.get("degree_centrality", 0.0)),
                "pagerank": float(c.get("pagerank", 0.0)),
                "in_degree": float(c.get("in_degree", 0.0)),
                "out_degree": float(c.get("out_degree", 0.0)),
            }
            row.update(_type_one_hot(type_map.get(node, ""), node_kinds))
            rows.append(row)
    out = pd.DataFrame(rows)
    log.info("rca features: %d rows for %d incidents", len(out), len(incidents))
    return out


def save_rca_features(df: pd.DataFrame, out_dir: str | Path) -> None:
    from pathlib import Path

    df.to_csv(Path(out_dir) / "rca_features.csv", index=False)
