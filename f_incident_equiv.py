#!/usr/bin/env python3
"""Prove the optimized incidentization is bit-identical to the frozen reference.

Authentic pipeline inputs are rebuilt offline from saved artifacts
(point_scores.csv.gz / topology.json / experiment_config.json), so no Stage-1
re-run is needed.  The frozen reference in ``aiops/_incident_ref.py`` and the
optimized ``aiops/incident.py`` are then run on the *same* objects and their
incidents, clusters and affinity edges are deep-compared.

Exit code is non-zero on any divergence.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import networkx as nx
import pandas as pd

from aiops import _incident_ref as ref
from aiops import episode as episode_mod
from aiops import incident as new
from aiops.config import PipelineConfig


def load_region(region_dir: Path) -> tuple[pd.DataFrame, nx.Graph, PipelineConfig, str]:
    point_df = pd.read_csv(region_dir / "point_scores.csv.gz")
    topo = json.loads((region_dir / "topology.json").read_text(encoding="utf-8"))
    g = nx.Graph()
    for node in topo.get("nodes") or []:
        g.add_node(str(node))
    for edge in topo.get("edges") or []:
        g.add_edge(str(edge["source"]), str(edge["target"]))
    cfg = PipelineConfig()
    cfg_path = region_dir / "experiment_config.json"
    if cfg_path.is_file():
        saved = json.loads(cfg_path.read_text(encoding="utf-8")).get("config", {})
        for key, value in saved.items():
            if hasattr(cfg, key):
                try:
                    setattr(cfg, key, value)
                except Exception:
                    pass
    return point_df, g, cfg, str(topo.get("dataset") or region_dir.name)


def deep_equal(a: Any, b: Any, path: str = "$") -> list[str]:
    """Recursive equality that treats NaN as equal to NaN."""
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return []
        if a != b:
            return [f"{path}: {a!r} != {b!r}"]
        return []
    if type(a) is not type(b):
        # numpy scalars / Timestamps are compared by value below
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return [] if a == b else [f"{path}: {a!r} != {b!r}"]
        if not (hasattr(a, "isoformat") and hasattr(b, "isoformat")):
            return [f"{path}: type {type(a).__name__} != {type(b).__name__}"]
    if isinstance(a, dict):
        if set(a) != set(b):
            return [f"{path}: keys {sorted(set(a) ^ set(b))}"]
        out: list[str] = []
        for k in a:
            out += deep_equal(a[k], b[k], f"{path}.{k}")
            if len(out) > 20:
                return out
        return out
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return [f"{path}: len {len(a)} != {len(b)}"]
        out = []
        for idx, (x, y) in enumerate(zip(a, b)):
            out += deep_equal(x, y, f"{path}[{idx}]")
            if len(out) > 20:
                return out
        return out
    if a != b:
        return [f"{path}: {a!r} != {b!r}"]
    return []


def discover(root: Path) -> list[Path]:
    found = sorted(root.glob("*/*/point_scores.csv.gz"))
    if not found:
        found = sorted(root.glob("*/point_scores.csv.gz"))
    return [p.parent for p in found]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", default=None,
                    help="outputs root; repeatable (default: the two known roots)")
    ap.add_argument("--regions", default="all")
    ap.add_argument("--limit", type=int, default=0, help="0 = all regions")
    ap.add_argument("--min-points", type=int, default=None)
    ap.add_argument("--min-duration", type=int, default=None)
    ap.add_argument("--skip-ref", action="store_true",
                    help="only run the optimized path (sanity/benchmark mode)")
    args = ap.parse_args()

    roots = [Path(r) for r in (args.root or [
        "outputs_experiment_f_stage2", "outputs_experiment_f_full",
    ])]
    dirs: list[Path] = []
    for root in roots:
        if root.is_dir():
            dirs += discover(root)
    if args.regions != "all":
        dirs = [d for d in dirs if args.regions in str(d)]
    if args.limit:
        dirs = dirs[: args.limit]
    if not dirs:
        print("no regions found")
        return 2

    failures = 0
    for region_dir in dirs:
        point_df, graph, cfg, ds_name = load_region(region_dir)
        if args.min_points is not None:
            cfg.episode_min_abnormal_points = args.min_points
        if args.min_duration is not None:
            cfg.episode_min_duration_minutes = args.min_duration

        episode_df = episode_mod.build_episodes(point_df, cfg, dataset_name=ds_name)
        n = len(episode_df)
        print("=" * 78)
        print(f"{ds_name}  points={len(point_df)}  episodes={n}  "
              f"min_pts={cfg.episode_min_abnormal_points}")

        t0 = time.perf_counter()
        if args.skip_ref:
            ref_inc, ref_payload = None, None
            t_ref = float("nan")
        else:
            ref_inc, ref_payload = ref.build_incidents(
                episode_df, point_df, graph, cfg, dataset_name=ds_name)
            t_ref = time.perf_counter() - t0

        t0 = time.perf_counter()
        new_inc, new_payload = new.build_incidents(
            episode_df, point_df, graph, cfg, dataset_name=ds_name)
        t_new = time.perf_counter() - t0

        print(f"  ref : {t_ref:9.2f} s -> {len(ref_inc or [])} incidents, "
              f"{len((ref_payload or {}).get('affinity_edges') or [])} edges")
        print(f"  new : {t_new:9.2f} s -> {len(new_inc)} incidents, "
              f"{len(new_payload.get('affinity_edges') or [])} edges")
        if not args.skip_ref and t_ref == t_ref and t_new > 0:
            print(f"  speedup: x{t_ref / t_new:.1f}")

        if args.skip_ref:
            continue

        diffs = deep_equal(ref_inc, new_inc, "incidents")
        diffs += deep_equal(ref_payload.get("affinity_edges"),
                            new_payload.get("affinity_edges"), "affinity_edges")
        diffs += deep_equal(ref_payload.get("clusters"),
                            new_payload.get("clusters"), "clusters")
        if diffs:
            failures += 1
            print(f"  [FAIL] {len(diffs)} divergence(s):")
            for line in diffs[:12]:
                print(f"         {line}")
        else:
            print(f"  [ OK ] bit-identical: {len(ref_inc)} incidents, "
                  f"{len(ref_payload.get('affinity_edges') or [])} edges")

    print("=" * 78)
    print("EQUIVALENCE PASSED" if failures == 0 else f"EQUIVALENCE FAILED ({failures} regions)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
