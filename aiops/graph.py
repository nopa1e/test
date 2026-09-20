from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import torch

from .config import PipelineConfig
from .dataset import DatasetInfo, canonical_node, network_element_id, ip_node_map_from_scrape_health, read_table
from .utils import get_logger, normalize_name

log = get_logger(__name__)


def _role(node_id: str) -> str:
    n = str(node_id).lower()
    if re.search(r"(^|[-_])fw($|[-_])", n) or n.endswith("-fw"):
        return "firewall"
    if "-service-vm-" in n or n.endswith("service-vm"):
        return "service"
    if "-traffic-vm" in n:
        return "traffic"
    if "-monitor-vm" in n:
        return "monitor"
    if re.search(r"(^|[-_])br[-_]?\d*", n):
        return "br"
    if re.search(r"(^|[-_])cr[-_]?\d*", n):
        return "cr"
    return "unknown"


def _infer_entity_edges(nodes: list[str]) -> list[tuple[str, str, str]]:
    by_role: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        by_role[_role(n)].append(n)
    edges: list[tuple[str, str, str]] = []

    # Service/traffic -> firewall -> backbone -> core.  Direction is
    # upstream -> downstream.  This mirrors the usual traffic path and lets the
    # MCP server walk reverse edges to find an upstream root cause.
    for src in by_role.get("service", []) + by_role.get("traffic", []) + by_role.get("monitor", []):
        for fw in by_role.get("firewall", []):
            edges.append((src, fw, "inferred_service_to_firewall"))
    for fw in by_role.get("firewall", []):
        for br in by_role.get("br", []):
            edges.append((fw, br, "inferred_firewall_to_backbone"))
    for br in by_role.get("br", []):
        for cr in by_role.get("cr", []):
            edges.append((br, cr, "inferred_backbone_to_core"))

    # If the roles are not all present, keep the graph connected with a
    # deterministic chain of node types.
    if not edges and len(nodes) > 1:
        order = {"service": 0, "traffic": 1, "monitor": 2, "firewall": 3, "br": 4, "cr": 5}
        ordered = sorted(nodes, key=lambda n: (order.get(_role(n), 99), n))
        for a, b in zip(ordered, ordered[1:]):
            edges.append((a, b, "fallback_chain"))
    return edges


def build_entity_graph(ds: DatasetInfo, point_df: pd.DataFrame, cfg: PipelineConfig) -> tuple[nx.DiGraph, dict[str, Any]]:
    nodes = sorted(point_df["network_element_id"].dropna().astype(str).unique().tolist())
    G = nx.DiGraph()
    for n in nodes:
        G.add_node(n, role=_role(n))

    explicit_sources: list[str] = []
    # Explicit topology files, if the dataset provides any.
    topo_path = ds.path("topology")
    if topo_path is not None:
        try:
            topo = pd.read_csv(topo_path, encoding="utf-8-sig", low_memory=False)
            src_col = next((c for c in ["source", "src", "from", "source_id", "src_id", "parent"] if c in topo.columns), None)
            dst_col = next((c for c in ["target", "dst", "to", "target_id", "dst_id", "child"] if c in topo.columns), None)
            if src_col and dst_col:
                for _, row in topo.iterrows():
                    s = network_element_id(ds.region_code, row[src_col])
                    t = network_element_id(ds.region_code, row[dst_col])
                    if s in nodes and t in nodes and s != t:
                        G.add_edge(s, t, edge_type="explicit_topology")
                        explicit_sources.append("explicit")
        except Exception as exc:
            log.warning("failed to read explicit topology for %s: %s", ds.name, exc)

    if not explicit_sources:
        for s, t, typ in _infer_entity_edges(nodes):
            if s in nodes and t in nodes and s != t:
                G.add_edge(s, t, edge_type=typ)

    # Netflow-based data-plane edges: only keep edges whose both endpoints can
    # be mapped back to a legal local network element.
    ip_map = ip_node_map_from_scrape_health(ds)
    nf = read_table(
        ds,
        "netflow_5tuple",
        nrows=min(cfg.max_netflow_rows, 120_000),
        usecols=lambda c: c in {"src_addr", "dst_addr", "node_key", "node", "packets", "bytes"},
    )
    if not nf.empty:
        src_col = "src_addr" if "src_addr" in nf.columns else None
        dst_col = "dst_addr" if "dst_addr" in nf.columns else None
        local_col = "node_key" if "node_key" in nf.columns else ("node" if "node" in nf.columns else None)
        if src_col and dst_col and local_col:
            for _, row in nf.head(50_000).iterrows():
                local = network_element_id(ds.region_code, row[local_col])
                if local not in nodes:
                    continue
                for peer_col in (src_col, dst_col):
                    peer_host = str(row[peer_col])
                    peer_host = re.sub(r"^\[(.*)\]:\d+$", r"\1", peer_host)
                    peer_host = re.sub(r"^(.*):\d+$", r"\1", peer_host)
                    peer = ip_map.get(peer_host)
                    if peer:
                        peer_id = network_element_id(ds.region_code, peer)
                        if peer_id in nodes and peer_id != local:
                            G.add_edge(local, peer_id, edge_type="netflow_observed")
    meta = {
        "nodes": nodes,
        "edges": [
            {"source": u, "target": v, "edge_type": d.get("edge_type", "unknown")}
            for u, v, d in G.edges(data=True)
        ],
        "topology_source": "explicit" if explicit_sources else "inferred+netflow",
    }
    log.info("entity graph %s: %d nodes, %d edges", ds.name, G.number_of_nodes(), G.number_of_edges())
    return G, meta


def build_point_graph(
    ds: DatasetInfo,
    point_df: pd.DataFrame,
    entity_graph: nx.DiGraph,
    cfg: PipelineConfig,
) -> tuple[torch.Tensor, list[str], dict[str, Any]]:
    """Build a point-level graph.

    Nodes are ``dataset:network_element_id:timestamp_bin``.  Temporal edges
    connect consecutive observations of the same element, and topology edges
    connect elements that are adjacent in the entity graph at the same time.
    """
    df = point_df.copy()
    df["point_id"] = (
        ds.name
        + ":"
        + df["network_element_id"].astype(str)
        + ":"
        + pd.to_datetime(df["timestamp_bin"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    point_ids = df["point_id"].astype(str).tolist()
    index = {pid: i for i, pid in enumerate(point_ids)}
    edges: set[tuple[int, int]] = set()

    # Temporal edges.
    for _, grp in df.sort_values("timestamp_bin").groupby("network_element_id", observed=True):
        idxs = grp.index.to_numpy()
        # positional indices in point_ids
        pos = [index[point_ids[i]] for i in idxs]
        for a, b in zip(pos, pos[1:]):
            edges.add((a, b))
            edges.add((b, a))

    # Same-timestamp topology edges.
    ts_key = pd.to_datetime(df["timestamp_bin"], utc=True)
    df = df.assign(_ts=ts_key)
    buckets: dict[pd.Timestamp, dict[str, int]] = defaultdict(dict)
    for ts, neid, pid in df[["_ts", "network_element_id", "point_id"]].itertuples(index=False, name=None):
        buckets[ts][str(neid)] = index[str(pid)]
    for ts, mapping in buckets.items():
        for u, v in entity_graph.edges():
            if u in mapping and v in mapping and mapping[u] != mapping[v]:
                a, b = mapping[u], mapping[v]
                edges.add((a, b))
                edges.add((b, a))

    if not edges:
        edges = {(i, i) for i in range(len(point_ids))}

    edge_index = torch.tensor(sorted(edges), dtype=torch.long).t().contiguous()
    meta = {
        "num_points": len(point_ids),
        "num_edges": int(edge_index.shape[1]) if edge_index.numel() else 0,
        "mode": "entity_point_graph",
    }
    log.info("point graph %s: %d points, %d directed edges", ds.name, meta["num_points"], meta["num_edges"])
    return edge_index, point_ids, meta


def build_trace_point_table(ds: DatasetInfo, cfg: PipelineConfig) -> tuple[pd.DataFrame, torch.Tensor, dict[str, Any]] | None:
    """Optional trace mode.

    If a trace/span CSV is present, each span becomes a point and parent-child
    edges become graph edges.  The expected, but not strictly required, columns
    are: ``span_id``, ``parent_span_id``, ``service_name``/``node``,
    ``start_time``/``timestamp``, plus numeric span features.
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

    out = df.copy()
    out["_ts"] = pd.to_datetime(out[time_col], errors="coerce", utc=True)
    out = out[out["_ts"].notna()].copy()
    out["timestamp_bin"] = out["_ts"].dt.floor(f"{int(cfg.bin_minutes)}min")
    out["node"] = out[service_col].map(canonical_node)
    out["node_type"] = "service"
    out["network_element_id"] = [network_element_id(ds.region_code, n) for n in out["node"]]
    out["point_id"] = ds.name + ":" + out[span_col].astype(str)
    # Keep numeric span features only.
    drop = {span_col, parent_col, time_col, service_col, "timestamp_bin", "node", "node_type", "network_element_id", "point_id"}
    numeric = [c for c in out.columns if c not in drop and pd.api.types.is_numeric_dtype(out[c])]
    out = out[["point_id", "timestamp_bin", "node", "node_type", "network_element_id"] + numeric].reset_index(drop=True)

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
