#!/usr/bin/env python3
"""Profile the episode -> incident path to locate the real bottleneck.

Rebuilds authentic pipeline inputs offline from saved artifacts
(point_scores.csv.gz / topology.json / experiment_config.json) so no Stage-1
re-run is needed, then times each phase separately.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import networkx as nx
import pandas as pd

from aiops import episode as episode_mod
from aiops import incident as incident_mod
from aiops.config import PipelineConfig


def load_region(region_dir: Path) -> tuple[pd.DataFrame, nx.Graph, PipelineConfig, str]:
    """Reconstruct (point_df, entity_graph, cfg, dataset_name) from artifacts."""
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


def discover(root: Path, limit: int) -> list[Path]:
    found = sorted(p for p in root.glob("*/*/point_scores.csv.gz"))
    if not found:
        found = sorted(p for p in root.glob("*/point_scores.csv.gz"))
    return [p.parent for p in found][:limit]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--regions", default="all")
    ap.add_argument("--limit", type=int, default=2)
    ap.add_argument("--sample-pairs", type=int, default=20000)
    ap.add_argument("--min-points", type=int, default=None,
                    help="override cfg.episode_min_abnormal_points")
    ap.add_argument("--min-duration", type=int, default=None)
    args = ap.parse_args()

    root = Path(args.root)
    dirs = discover(root, 99)
    if args.regions != "all":
        dirs = [d for d in dirs if args.regions in str(d)]
    dirs = dirs[: args.limit]
    if not dirs:
        print("no regions with point_scores.csv.gz found")
        return

    for region_dir in dirs:
        point_df, graph, cfg, ds_name = load_region(region_dir)
        if args.min_points is not None:
            cfg.episode_min_abnormal_points = args.min_points
        if args.min_duration is not None:
            cfg.episode_min_duration_minutes = args.min_duration

        print("=" * 78)
        print(f"region   : {region_dir}")
        print(f"points   : {len(point_df)}  nodes={point_df['network_element_id'].nunique()}")
        print(f"graph    : {graph.number_of_nodes()} nodes / {graph.number_of_edges()} edges")
        print(f"min_pts  : {cfg.episode_min_abnormal_points}  "
              f"min_dur: {cfg.episode_min_duration_minutes}")

        t0 = time.perf_counter()
        episode_df = episode_mod.build_episodes(point_df, cfg, dataset_name=ds_name)
        t_ep = time.perf_counter() - t0
        n = len(episode_df)
        print(f"[1] build_episodes        : {t_ep:8.2f} s  -> {n} episodes")

        t0 = time.perf_counter()
        sig_df = incident_mod.build_episode_signatures(episode_df, point_df)
        t_sig = time.perf_counter() - t0
        print(f"[2] build_episode_sigs    : {t_sig:8.2f} s  "
              f"({t_sig / max(1, n) * 1000:.2f} ms/episode)")

        records = sig_df.reset_index(drop=True).to_dict(orient="records")
        k = min(args.sample_pairs, max(1, n * (n - 1) // 2))
        t0 = time.perf_counter()
        for idx in range(k):
            i = idx % max(1, n - 1)
            j = min(n - 1, i + 1 + (idx // max(1, n - 1)))
            if i == j:
                continue
            incident_mod.incident_affinity(records[i], records[j], graph, point_df, cfg)
        t_pair = time.perf_counter() - t0
        per_pair = t_pair / max(1, k)
        total_pairs = n * (n - 1) // 2
        print(f"[3] incident_affinity     : {per_pair * 1e6:8.1f} us/pair  "
              f"({k} sampled)")
        print(f"    => pair loop estimate : {per_pair * total_pairs:8.1f} s "
              f"for {total_pairs:,} pairs")

        t0 = time.perf_counter()
        incidents, payload = incident_mod.build_incidents(
            episode_df, point_df, graph, cfg, dataset_name=ds_name)
        t_inc = time.perf_counter() - t0
        print(f"[4] build_incidents TOTAL : {t_inc:8.2f} s  -> {len(incidents)} incidents, "
              f"{len(payload.get('affinity_edges') or [])} edges")
        print(f"    episode={t_ep:.1f}s  sig={t_sig:.1f}s  pairloop~={per_pair * total_pairs:.1f}s  "
              f"rest={max(0.0, t_inc - t_sig - per_pair * total_pairs):.1f}s")


if __name__ == "__main__":
    main()
