#!/usr/bin/env python3
"""Exhaustive adversarial equivalence test for the optimized incidentization.

Real-data comparison only reveals pairs that clear ``incident_similarity_threshold``.
Setting the threshold to 0.0 forces *every* pair into ``affinity_edges``, so a
divergence anywhere in the pairwise arithmetic is caught, not just above the cut.

Covers: overlapping / touching / 1-minute / exactly-30-minute / >30-minute gaps,
same vs cross node, nodes absent from the graph, a missing graph, invalid
timestamps, empty and identical signatures, and a degenerate merge threshold.
"""
from __future__ import annotations

import sys
from typing import Any

import networkx as nx
import pandas as pd

from aiops import _incident_ref as ref
from aiops import incident as new
from aiops.config import PipelineConfig
from f_incident_equiv import deep_equal

BASE = pd.Timestamp("2026-09-01T00:00:00Z")
FEATURES = [
    [{"name": "interface_metrics_in_octets"}],
    [{"name": "node_metrics_cpu_usage_ratio"}],
    [{"name": "interface_metrics_in_octets", "v": 1}, {"name": "routing_metrics_bgp_peer_state"}],
    [{"name": "frr_syslog_events_severity"}],
    [],
    [{"name": "node_metrics_memory_available_ratio"}, {"name": "scrape_health_up"}],
]


def build_point_df() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    nodes = ["n1", "n2", "n3", "n4", "n5", "n6", "n7"]
    for step in range(144):                      # 12 h on a 5-minute grid
        for k, node in enumerate(nodes):
            rows.append({
                "point_id": f"P{step}_{k}",
                "timestamp_bin": (BASE + pd.Timedelta(minutes=5 * step)).isoformat(),
                "network_element_id": node,
                "combined_anomaly_score": 0.5,
                "top_feature_json": __import__("json").dumps(FEATURES[(step + k) % len(FEATURES)]),
            })
    return pd.DataFrame(rows)


def build_episode_df() -> pd.DataFrame:
    """Hand-built episodes that straddle every branch of the gap arithmetic."""
    specs = [
        # (node, start_offset_min, duration_min) -- gaps chosen around the 30' cut
        ("n1", 0, 5), ("n1", 0, 5), ("n1", 5, 5), ("n1", 9, 5),
        ("n1", 30, 5), ("n1", 35, 5), ("n1", 36, 5), ("n1", 65, 5),
        ("n1", 200, 30), ("n2", 2, 10), ("n3", 0, 5), ("n4", 0, 5),
        ("n6", 0, 5), ("n7", 0, 5), ("n5", 0, 5),
        ("", 0, 5),                       # empty node id
        ("ghost", 0, 5),                  # node absent from the graph
    ]
    rows = []
    for idx, (node, off, dur) in enumerate(specs, start=1):
        rows.append({
            "episode_id": f"EP{idx:04d}",
            "dataset": "synthetic",
            "network_element_id": node,
            "start_time": (BASE + pd.Timedelta(minutes=off)).isoformat(),
            "end_time": (BASE + pd.Timedelta(minutes=off + dur)).isoformat(),
            "duration_minutes": dur,
            "num_points": max(1, dur // 5),
            "abnormal_points": max(1, dur // 5),
            "growth_rate": float(idx) / 10.0,
        })
    # invalid timestamps must be tolerated by both implementations
    rows.append({"episode_id": "EP9001", "dataset": "synthetic",
                 "network_element_id": "n1", "start_time": None,
                 "end_time": None, "duration_minutes": 0})
    rows.append({"episode_id": "EP9002", "dataset": "synthetic",
                 "network_element_id": "n2", "start_time": "not-a-time",
                 "end_time": "not-a-time", "duration_minutes": 0})
    return pd.DataFrame(rows)


def make_graph() -> nx.Graph:
    g = nx.Graph()
    g.add_edge("n1", "n2")
    g.add_edge("n2", "n3")
    g.add_edge("n3", "n4")     # n1..n4 is a chain: distance > 2 beyond n4
    g.add_edge("n5", "n6")
    return g


def cases() -> list[tuple[str, PipelineConfig, nx.Graph | None]]:
    out: list[tuple[str, PipelineConfig, nx.Graph | None]] = []
    for label, thr in (("default-threshold", 0.45), ("bisect-threshold", 0.0)):
        for graph_label, graph in (("graph", make_graph()), ("no-graph", None)):
            cfg = PipelineConfig()
            cfg.incident_similarity_threshold = thr
            out.append((f"{label}/{graph_label}", cfg, graph))
    cfg = PipelineConfig()
    cfg.incident_similarity_threshold = 0.0
    cfg.incident_merge_threshold = 0.0          # forces every pair to union
    out.append(("merge-0/bisect/graph", cfg, make_graph()))
    cfg = PipelineConfig()
    cfg.incident_similarity_threshold = 0.0
    cfg.incident_max_time_gap_minutes = 0       # collapses the temporal term
    out.append(("zero-gap-window/bisect/graph", cfg, make_graph()))
    return out


def main() -> int:
    point_df = build_point_df()
    episode_df = build_episode_df()
    failures = 0

    # empty-frame short circuit
    cfg0 = PipelineConfig()
    a_inc, a_pay = ref.build_incidents(episode_df.iloc[0:0], point_df, make_graph(), cfg0, "synthetic")
    b_inc, b_pay = new.build_incidents(episode_df.iloc[0:0], point_df, make_graph(), cfg0, "synthetic")
    diffs = deep_equal([a_inc, a_pay], [b_inc, b_pay], "empty")
    print(f"empty-input            : {'OK' if not diffs else diffs[:4]}")
    failures += bool(diffs)

    for label, cfg, graph in cases():
        r_inc, r_pay = ref.build_incidents(episode_df, point_df, graph, cfg, dataset_name="synthetic")
        n_inc, n_pay = new.build_incidents(episode_df, point_df, graph, cfg, dataset_name="synthetic")
        diffs = deep_equal(r_inc, n_inc, "incidents")
        diffs += deep_equal(r_pay.get("affinity_edges"), n_pay.get("affinity_edges"), "edges")
        diffs += deep_equal(r_pay.get("clusters"), n_pay.get("clusters"), "clusters")
        edges = len(r_pay.get("affinity_edges") or [])
        status = "OK" if not diffs else f"FAIL {diffs[:4]}"
        print(f"{label:22s} : {status}  ({len(r_inc)} incidents, {edges} edges)")
        failures += bool(diffs)

    print("=" * 70)
    print("STRESS TEST PASSED" if not failures else f"STRESS TEST FAILED ({failures} cases)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
