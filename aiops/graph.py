"""Graph construction for the AIOps pipeline.

The entity graph itself is built by :mod:`aiops.topology` from real
control-plane and data-plane evidence.  This module is only responsible for

  * forwarding to that builder (kept as a thin alias so callers do not change), and
  * turning the entity graph into the point-level spatio-temporal graph the GNN
    consumes.

There is no node-name heuristic left here.  Edge confidence from the evidence
layer is propagated into the point graph as an edge weight, so message passing
follows well-supported links more strongly than weak ones.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import torch

from .config import PipelineConfig
from .dataset import DatasetInfo
from .topology import build_entity_graph as build_evidence_graph
from .utils import get_logger

log = get_logger(__name__)


def build_entity_graph(
    ds: DatasetInfo,
    point_df: pd.DataFrame,
    cfg: PipelineConfig,
) -> tuple[nx.DiGraph, dict[str, Any]]:
    """Build the entity graph from evidence.  See :mod:`aiops.topology`."""
    return build_evidence_graph(ds, point_df, cfg)


def build_point_ids(ds: DatasetInfo, point_df: pd.DataFrame) -> list[str]:
    """Stable point id: ``dataset:network_element_id:timestamp_bin``.

    Kept separate from :func:`build_point_graph` so the no-GNN path can still
    label points without building a graph.
    """
    ts = pd.to_datetime(point_df["timestamp_bin"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (ds.name + ":" + point_df["network_element_id"].astype(str) + ":" + ts).tolist()


def build_point_graph(
    ds: DatasetInfo,
    point_df: pd.DataFrame,
    entity_graph: nx.DiGraph,
    cfg: PipelineConfig,
) -> tuple[torch.Tensor, torch.Tensor, list[str], dict[str, Any]]:
    """Build the point-level graph.

    Nodes are ``dataset:network_element_id:timestamp_bin``.  Two kinds of edge
    are added, both derived from data:

      temporal   consecutive observations of the same element
      topology   elements adjacent in the entity graph, observed in the same
                 time bin

    Returns ``(edge_index, edge_weight, point_ids, meta)``.  ``edge_weight``
    carries the entity-graph confidence of the link the edge came from (1.0 for
    pure temporal edges, which are not in question).
    """
    df = point_df.copy()
    df["point_id"] = build_point_ids(ds, point_df)
    point_ids = df["point_id"].astype(str).tolist()
    index = {pid: i for i, pid in enumerate(point_ids)}
    edges: dict[tuple[int, int], float] = {}
    n_temporal = 0
    n_topology = 0

    # --- temporal edges ---------------------------------------------------
    for _, grp in df.sort_values("timestamp_bin").groupby("network_element_id", observed=True):
        pos = [index[point_ids[i]] for i in grp.index.to_numpy()]
        for a, b in zip(pos, pos[1:]):
            edges[(a, b)] = 1.0
            edges[(b, a)] = 1.0
            n_temporal += 1

    # --- topology edges ---------------------------------------------------
    min_conf = float(getattr(cfg, "topology_min_confidence", 0.0) or 0.0)
    kept_pairs: dict[tuple[str, str], float] = {}
    for u, v, d in entity_graph.edges(data=True):
        conf = float(d.get("confidence", 1.0))
        if conf < min_conf:
            continue
        kept_pairs[(str(u), str(v))] = max(conf, kept_pairs.get((str(u), str(v)), 0.0))

    ts_key = pd.to_datetime(df["timestamp_bin"], utc=True)
    df = df.assign(_ts=ts_key)
    buckets: dict[pd.Timestamp, dict[str, int]] = defaultdict(dict)
    for ts, neid, pid in df[["_ts", "network_element_id", "point_id"]].itertuples(index=False, name=None):
        buckets[ts][str(neid)] = index[str(pid)]
    for _ts, mapping in buckets.items():
        for (u, v), conf in kept_pairs.items():
            if u in mapping and v in mapping and mapping[u] != mapping[v]:
                a, b = mapping[u], mapping[v]
                edges[(a, b)] = max(conf, edges.get((a, b), 0.0))
                edges[(b, a)] = max(conf, edges.get((b, a), 0.0))
                n_topology += 1

    if not edges:
        edges = {(i, i): 1.0 for i in range(len(point_ids))}
    if n_topology == 0 and kept_pairs:
        # A silent zero here means the entity graph and the point table disagree
        # about node ids, which would leave the GNN running on temporal edges
        # alone while appearing to work.
        log.warning("point graph %s: entity graph has %d links but none matched a point id "
                    "(sample graph nodes: %s; sample points: %s)",
                    ds.name, len(kept_pairs),
                    sorted({u for u, _ in kept_pairs})[:3],
                    point_ids[:2])

    ordered = sorted(edges)
    edge_index = torch.tensor(ordered, dtype=torch.long).t().contiguous()
    edge_weight = torch.tensor([edges[k] for k in ordered], dtype=torch.float32)

    conf_hist = Counter(round(w, 2) for w in edge_weight.tolist())
    meta = {
        "num_points": len(point_ids),
        "num_edges": int(edge_index.shape[1]) if edge_index.numel() else 0,
        "temporal_edges": n_temporal,
        "topology_edges": n_topology,
        "topology_min_confidence": min_conf,
        "weight_histogram": {str(k): v for k, v in sorted(conf_hist.items())},
        "mode": "evidence_point_graph",
    }
    log.info("point graph %s: %d points, %d edges (%d temporal, %d topology), min_conf=%.2f",
             ds.name, meta["num_points"], meta["num_edges"], n_temporal, n_topology, min_conf)
    return edge_index, edge_weight, point_ids, meta


def build_trace_point_table(
    ds: DatasetInfo,
    cfg: PipelineConfig,
) -> tuple[pd.DataFrame, torch.Tensor, dict[str, Any]] | None:
    """Optional trace mode, kept for datasets that ship span data.

    NOTE: the AIOps challenge dataset used here contains no trace/span file, so
    this path is never taken in the current experiments.  It is retained because
    it is dataset-driven (``ds.has("trace")``) and costs nothing when absent.
    """
    if not ds.has("trace"):
        return None
    path = ds.path("trace")
    if path is None:
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    except Exception as exc:
        log.warning("failed to read trace file %s: %s", path, exc)
        return None
    if df.empty:
        return None

    span_col = next((c for c in ["span_id", "spanId", "id"] if c in df.columns), None)
    parent_col = next((c for c in ["parent_span_id", "parentSpanId", "parent_id", "parent"] if c in df.columns), None)
    if span_col is None or parent_col is None:
        log.warning("trace file %s lacks span/parent columns", path)
        return None
    time_col = next((c for c in ["start_time", "timestamp", "startTime", "time"] if c in df.columns), None)
    if time_col is None:
        return None
    service_col = next((c for c in ["service_name", "service", "node", "component"] if c in df.columns), None)
    if service_col is None:
        service_col = span_col

    from .dataset import network_element_id

    out = df.copy()
    out["_ts"] = pd.to_datetime(out[time_col], errors="coerce", utc=True)
    out = out[out["_ts"].notna()].copy()
    out["timestamp_bin"] = out["_ts"].dt.floor(f"{int(cfg.bin_minutes)}min")
    out["node_type"] = "service"
    out["network_element_id"] = [network_element_id(ds.region_code, s) for s in out[service_col]]
    out["point_id"] = ds.name + ":" + out[span_col].astype(str)
    drop = {span_col, parent_col, time_col, service_col, "timestamp_bin", "network_element_id", "point_id"}
    numeric = [c for c in out.columns if c not in drop and pd.api.types.is_numeric_dtype(out[c])]
    out = out[["point_id", "timestamp_bin", "node_type", "network_element_id"] + numeric].reset_index(drop=True)

    idx = {str(s): i for i, s in enumerate(out[span_col].astype(str).tolist())}
    edges: set[tuple[int, int]] = set()
    for i, (sid, pid) in enumerate(zip(out[span_col].astype(str), out[parent_col].astype(str))):
        if pid in idx:
            j = idx[pid]
            edges.add((j, i))
            edges.add((i, j))
    if not edges:
        edges = {(i, i) for i in range(len(out))}
    edge_index = torch.tensor(sorted(edges), dtype=torch.long).t().contiguous()
    meta = {
        "num_points": int(len(out)),
        "num_edges": int(edge_index.shape[1]),
        "mode": "trace_span_graph",
        "trace_file": str(path),
    }
    return out, edge_index, meta
