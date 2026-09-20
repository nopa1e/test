"""Temporal propagation analysis over the entity graph."""
from __future__ import annotations

from typing import Any

import networkx as nx
import numpy as np
import pandas as pd

from .config import PipelineConfig
from .utils import dump_json, get_logger, to_iso

log = get_logger(__name__)


def _score_col(point_df: pd.DataFrame, cfg: PipelineConfig) -> str:
    for col in (cfg.episode_score_column, "combined_anomaly_score", "final_normal_score"):
        if col in point_df.columns:
            return col
    raise ValueError("point dataframe has no score column")


def _normalise_rank(values: dict[str, float], higher_is_better: bool = True) -> dict[str, float]:
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1], reverse=higher_is_better)
    n = len(items)
    if n == 1:
        return {items[0][0]: 1.0}
    return {k: float(1.0 - rank / (n - 1)) for rank, (k, _) in enumerate(items)}


def build_propagation(
    incident: dict[str, Any],
    point_df: pd.DataFrame,
    entity_graph: nx.Graph | nx.DiGraph | None,
    cfg: PipelineConfig,
) -> dict[str, Any]:
    """Return node-level temporal/upstream/downstream propagation scores."""
    nodes = [str(x) for x in incident.get("nodes", [])]
    if not nodes or point_df.empty:
        return {"incident_id": incident.get("incident_id"), "nodes": [], "edges": [], "chain": []}
    score_col = _score_col(point_df, cfg)
    df = point_df.copy()
    if "timestamp_bin" in df.columns:
        df["_ts"] = pd.to_datetime(df["timestamp_bin"], errors="coerce", utc=True)
    else:
        return {"incident_id": incident.get("incident_id"), "nodes": [], "edges": [], "chain": []}
    try:
        threshold = float(df[score_col].quantile(float(cfg.episode_anomaly_quantile)))
    except Exception:
        threshold = 0.5

    node_stats: list[dict[str, Any]] = []
    first_times: dict[str, float] = {}
    for node in nodes:
        sub = df[df["network_element_id"].astype(str) == node].sort_values("_ts")
        if sub.empty:
            node_stats.append({
                "network_element_id": node,
                "first_abnormal_time": None,
                "peak_time": None,
                "peak_anomaly_score": 0.0,
                "mean_anomaly_score": 0.0,
                "temporal_precedence_score": 0.0,
                "upstream_score": 0.0,
                "downstream_impact_score": 0.0,
                "propagation_consistency_score": 0.0,
            })
            continue
        abnormal = sub[pd.to_numeric(sub[score_col], errors="coerce").fillna(0.0) >= threshold]
        if abnormal.empty:
            abnormal = sub.tail(1)
        first = pd.Timestamp(abnormal.iloc[0]["_ts"])
        peak_idx = int(pd.to_numeric(sub[score_col], errors="coerce").fillna(0.0).to_numpy().argmax())
        peak_row = sub.iloc[peak_idx]
        mean_score = float(pd.to_numeric(abnormal[score_col], errors="coerce").fillna(0.0).mean())
        first_times[node] = first.timestamp()
        node_stats.append({
            "network_element_id": node,
            "first_abnormal_time": to_iso(first),
            "peak_time": to_iso(peak_row["_ts"]),
            "peak_anomaly_score": float(pd.to_numeric(pd.Series([peak_row[score_col]]), errors="coerce").fillna(0.0).iloc[0]),
            "mean_anomaly_score": mean_score,
        })

    precedence = _normalise_rank(first_times, higher_is_better=False)
    graph = entity_graph
    for stat in node_stats:
        node = stat["network_element_id"]
        descendants = 0
        ancestors = 0
        if graph is not None and node in graph:
            try:
                descendants = len(nx.descendants(graph, node))
                ancestors = len(nx.ancestors(graph, node))
            except Exception:
                descendants = ancestors = 0
        denom = max(1, len(nodes) - 1)
        stat["temporal_precedence_score"] = float(precedence.get(node, 0.0))
        stat["upstream_score"] = float(min(1.0, descendants / denom))
        stat["downstream_impact_score"] = float(min(1.0, ancestors / denom))
        stat["propagation_consistency_score"] = 0.0

    edges: list[dict[str, Any]] = []
    if graph is not None:
        for u, v in graph.edges():
            u, v = str(u), str(v)
            if u not in first_times or v not in first_times:
                continue
            lag = (first_times[v] - first_times[u]) / 60.0
            if abs(lag) > float(cfg.propagation_max_lag_minutes):
                continue
            consistent = lag >= 0
            edge = {
                "source": u,
                "target": v,
                "edge_type": "topology",
                "time_lag_minutes": float(lag),
                "consistent": bool(consistent and abs(lag) > 0),
            }
            edges.append(edge)
            if consistent:
                for stat in node_stats:
                    if stat["network_element_id"] in {u, v}:
                        stat["propagation_consistency_score"] = max(
                            stat.get("propagation_consistency_score", 0.0), 1.0
                        )
    # Build a readable candidate chain ordered by first abnormal time.
    chain = []
    for stat in sorted(node_stats, key=lambda s: s.get("first_abnormal_time") or "9999"):
        if stat.get("first_abnormal_time"):
            chain.append({
                "network_element_id": stat["network_element_id"],
                "first_abnormal_time": stat["first_abnormal_time"],
                "peak_anomaly_score": stat["peak_anomaly_score"],
            })
    return {
        "incident_id": incident.get("incident_id"),
        "dataset": incident.get("dataset"),
        "threshold": float(threshold),
        "nodes": node_stats,
        "edges": edges,
        "chain": chain,
    }


def save_propagation(prop: dict[str, Any], out_dir: str | Path) -> None:
    from pathlib import Path

    dump_json(Path(out_dir) / "propagation_graph.json", prop)
