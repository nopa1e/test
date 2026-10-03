#!/usr/bin/env python3
"""Focused equivalence test for the timeline fast path and the edge cap.

The pairwise arithmetic is covered by f_incident_stress_test.py.  This script
targets the second half of build_incidents, which changed from a full table scan
per incident to a single sort plus bisect:

  * NaT rows in ``timestamp_bin`` (must be excluded exactly as the old mask did)
  * many duplicate timestamps shared across nodes
  * ``combined_anomaly_score`` absent -> ``final_normal_score`` fallback
  * ``timestamp_bin`` absent entirely -> the whole table is the slice
  * ``incident_max_affinity_edges`` truncating the diagnostic payload
"""
from __future__ import annotations

import json
import random
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
    [{"name": "routing_metrics_bgp_peer_state"}],
    [],
]


def rng(seed: int) -> random.Random:
    return random.Random(seed)


def point_df(n_points: int, nat: bool, score_col: str | None, ts_col: bool, seed: int) -> pd.DataFrame:
    r = rng(seed)
    rows = []
    for i in range(n_points):
        ts = BASE + pd.Timedelta(minutes=r.randint(0, 600))
        row: dict[str, Any] = {
            "point_id": f"P{i}",
            "network_element_id": f"n{r.randint(1, 5)}",
            "top_feature_json": json.dumps(r.choice(FEATURES)),
        }
        if ts_col:
            row["timestamp_bin"] = None if (nat and i % 37 == 0) else ts.isoformat()
        if score_col:
            row[score_col] = r.random()
        rows.append(row)
    return pd.DataFrame(rows)


def episode_df(n_eps: int, seed: int) -> pd.DataFrame:
    r = rng(seed)
    rows = []
    for i in range(n_eps):
        off = r.randint(0, 590)
        dur = r.choice([1, 5, 5, 10, 30])
        rows.append({
            "episode_id": f"EP{i:05d}",
            "dataset": "synthetic",
            "network_element_id": f"n{r.randint(1, 5)}",
            "start_time": (BASE + pd.Timedelta(minutes=off)).isoformat(),
            "end_time": (BASE + pd.Timedelta(minutes=off + dur)).isoformat(),
            "duration_minutes": dur,
            "growth_rate": r.random(),
        })
    return pd.DataFrame(rows)


def graph() -> nx.Graph:
    g = nx.Graph()
    for a, b in (("n1", "n2"), ("n2", "n3"), ("n3", "n4"), ("n4", "n5")):
        g.add_edge(a, b)
    return g


def run(label: str, pts: pd.DataFrame, eps: pd.DataFrame, cfg: PipelineConfig,
        cap_check: bool = False) -> int:
    r_inc, r_pay = ref.build_incidents(eps, pts, graph(), cfg, "synthetic")
    n_inc, n_pay = new.build_incidents(eps, pts, graph(), cfg, "synthetic")
    diffs = deep_equal(r_inc, n_inc, "incidents")
    diffs += deep_equal(r_pay.get("clusters"), n_pay.get("clusters"), "clusters")
    if cap_check:
        r_edges = r_pay.get("affinity_edges") or []
        n_edges = n_pay.get("affinity_edges") or []
        diffs += deep_equal(r_edges[: len(n_edges)], n_edges, "edges[:cap]")
        if n_pay.get("affinity_edges_truncated") is not True or \
                n_pay.get("affinity_edges_total") != len(r_edges):
            diffs.append(f"cap metadata wrong: total={n_pay.get('affinity_edges_total')} "
                         f"expected {len(r_edges)}")
    else:
        diffs += deep_equal(r_pay.get("affinity_edges"), n_pay.get("affinity_edges"), "edges")
    tl = sum(len(i.get("timeline") or []) for i in r_inc)
    status = "OK" if not diffs else f"FAIL {diffs[:3]}"
    print(f"{label:38s}: {status}  ({len(r_inc)} incidents, {tl} timeline rows)")
    return bool(diffs)


def main() -> int:
    failures = 0
    eps = episode_df(300, seed=7)
    cfg = PipelineConfig()
    cfg.incident_similarity_threshold = 0.0

    cases = [
        ("NaT + dup ts + combined", point_df(4000, True, "combined_anomaly_score", True, 1), cfg),
        ("no NaT, final_normal_score", point_df(4000, False, "final_normal_score", True, 2), cfg),
        ("NaT + final_normal_score", point_df(4000, True, "final_normal_score", True, 3), cfg),
        ("no timestamp_bin column", point_df(2000, False, "combined_anomaly_score", False, 4), cfg),
        ("tiny grid, many dup ts", point_df(600, False, "combined_anomaly_score", True, 6), cfg),
    ]
    for label, pts, c in cases:
        failures += run(label, pts, eps, c)

    # A frame carrying neither score column makes the *original* implementation
    # raise KeyError (score_col falls back to "final_normal_score" and no such
    # column exists).  The optimised path must preserve that behaviour exactly
    # rather than silently inventing a timeline.
    raised: dict[str, str | None] = {}
    for name, mod in (("ref", ref), ("new", new)):
        try:
            mod.build_incidents(eps, point_df(500, False, None, True, 5), graph(), cfg, "synthetic")
            raised[name] = None
        except Exception as exc:                     # noqa: BLE001 - behaviour probe
            raised[name] = type(exc).__name__
    if raised["ref"] == raised["new"]:
        print(f"{'neither score column (both raise)':38s}: OK  ({raised['ref']})")
    else:
        failures += 1
        print(f"{'neither score column (both raise)':38s}: FAIL ref={raised['ref']} new={raised['new']}")

    cap_cfg = PipelineConfig()
    cap_cfg.incident_similarity_threshold = 0.0
    cap_cfg.incident_max_affinity_edges = 100
    failures += run("edge cap = 100 (truncation)", point_df(4000, True, "combined_anomaly_score", True, 1),
                    eps, cap_cfg, cap_check=True)

    # the uncapped payload must stay identical to the reference
    uncapped = PipelineConfig()
    uncapped.incident_similarity_threshold = 0.0
    _, pay = new.build_incidents(eps, point_df(500, False, "combined_anomaly_score", True, 8),
                                 graph(), uncapped, "synthetic")
    if "affinity_edges_truncated" in pay or "affinity_edges_total" in pay:
        failures += 1
        print("uncapped payload grew extra keys: FAIL")
    else:
        print(f"{'uncapped payload keys unchanged':38s}: OK")

    print("=" * 70)
    print("TIMELINE TEST PASSED" if not failures else f"TIMELINE TEST FAILED ({failures} cases)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
