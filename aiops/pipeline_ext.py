"""Optional extension stages for the AIOps pipeline.

All functions here are opt-in through ``PipelineConfig.experiment_mode`` so the
original baseline path remains unchanged.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import episode as episode_mod
from . import incident as incident_mod
from . import propagation as propagation_mod
from . import rca_features as rca_features_mod
from .config import PipelineConfig
from .dataset import DatasetInfo
from .utils import dump_json, get_logger

log = get_logger(__name__)

_EPISODE_MODES = {"episode", "incident", "rca_gnn", "llm_pseudo", "hybrid", "llm_distill", "full"}
_INCIDENT_MODES = {"incident", "rca_gnn", "llm_pseudo", "hybrid", "llm_distill", "full"}
_PROPAGATION_MODES = {"rca_gnn", "llm_pseudo", "hybrid", "llm_distill", "full"}
_TEACHER_MODES = {"llm_pseudo", "hybrid", "llm_distill", "full"}
_RCA_TRAIN_MODES = {"rca_gnn", "llm_pseudo", "hybrid", "llm_distill", "full"}


def _copy_if_exists(src: Path, dst: Path) -> None:
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)


def _teacher_targets(rca_df: pd.DataFrame, labels: list[dict[str, Any]]) -> pd.DataFrame:
    if rca_df.empty or not labels:
        return rca_df
    out = rca_df.copy()
    out["root_cause_target"] = 0.0
    out["label_source"] = "heuristic"
    by_inc = {str(x.get("incident_id")): x for x in labels}
    for inc_id, group in out.groupby("incident_id", observed=True):
        label = by_inc.get(str(inc_id))
        if not label:
            continue
        ranking = label.get("root_cause_ranking") or []
        nodes = [str(x.get("network_element_id")) for x in ranking]
        n = max(1, len(nodes) - 1)
        for item in ranking:
            neid = str(item.get("network_element_id"))
            if neid not in set(group["network_element_id"].astype(str)):
                continue
            rank = int(item.get("rank", 1))
            conf = float(item.get("confidence", label.get("confidence", 0.0)) or 0.0)
            target = max(0.0, min(1.0, conf * (1.0 - (rank - 1) / n)))
            mask = (out["incident_id"].astype(str) == str(inc_id)) & (out["network_element_id"].astype(str) == neid)
            out.loc[mask, "root_cause_target"] = target
            out.loc[mask, "label_source"] = str(label.get("label_source", "llm"))
    return out


def run_extension_stages(
    ds: DatasetInfo,
    cfg: PipelineConfig,
    point_df: pd.DataFrame,
    entity_graph: Any,
    out_dir: str | Path,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Run optional Episode/Incident/Propagation/RCA/Teacher stages."""
    mode = str(cfg.experiment_mode or "baseline").lower()
    out_path = Path(out_dir)
    summary: dict[str, Any] = {"experiment_mode": mode, "artifacts": {}, "counts": {}}
    dump_json(out_path / "experiment_config.json", {"mode": mode, "config": cfg.to_dict()})
    if mode == "baseline":
        dump_json(out_path / "experiment_summary.json", summary)
        return summary

    if mode not in _EPISODE_MODES:
        raise ValueError(f"unsupported experiment_mode: {cfg.experiment_mode}")

    episode_df = episode_mod.build_episodes(point_df, cfg, dataset_name=ds.name)
    episode_mod.save_episodes(episode_df, out_path)
    summary["counts"]["episode_count"] = int(len(episode_df))
    summary["artifacts"]["episode_scores"] = "episode_scores.csv"
    if episode_df.empty or mode not in _INCIDENT_MODES:
        dump_json(out_path / "experiment_summary.json", summary)
        return summary

    incidents, incident_payload = incident_mod.build_incidents(episode_df, point_df, entity_graph, cfg, dataset_name=ds.name)
    incident_mod.save_incidents(incidents, incident_payload, out_path)
    summary["counts"]["incident_count"] = int(len(incidents))
    summary["artifacts"]["incident_candidates"] = "incident_candidates.json"
    summary["artifacts"]["incident_clusters"] = "incident_clusters.json"

    if use_llm and mode in {"full", "llm_distill"} and incidents:
        try:
            from .llm_tasks import task_a_incident_correlation

            decisions = task_a_incident_correlation(
                incidents,
                cfg,
                graph=entity_graph,
                point_df=point_df,
                max_pairs=int(cfg.llm_task_a_max_pairs),
            )
            dump_json(out_path / "incident_correlation.json", {"decisions": decisions})
            summary["artifacts"]["incident_correlation"] = "incident_correlation.json"
            summary["counts"]["incident_correlation_pairs"] = int(len(decisions))
        except Exception as exc:
            log.warning("LLM Task A incident correlation failed for %s: %s", ds.name, exc)
    if not incidents or mode not in _PROPAGATION_MODES:
        dump_json(out_path / "experiment_summary.json", summary)
        return summary

    propagation_by_incident: dict[str, dict[str, Any]] = {}
    for inc in incidents:
        prop = propagation_mod.build_propagation(inc, point_df, entity_graph, cfg)
        propagation_by_incident[str(inc.get("incident_id"))] = prop
    dump_json(out_path / "propagation_graph.json", {"incidents": list(propagation_by_incident.values())})
    summary["artifacts"]["propagation_graph"] = "propagation_graph.json"

    rca_df = rca_features_mod.build_rca_features(
        incidents, episode_df, point_df, propagation_by_incident, entity_graph, cfg, dataset_name=ds.name
    )
    rca_features_mod.save_rca_features(rca_df, out_path)
    summary["counts"]["rca_feature_rows"] = int(len(rca_df))
    summary["artifacts"]["rca_features"] = "rca_features.csv"

    teacher_labels: list[dict[str, Any]] = []
    if mode in _TEACHER_MODES and use_llm and str(cfg.llm_backend).lower() not in {"none", ""}:
        try:
            from .llm_teacher import generate_pseudo_labels

            teacher_labels = generate_pseudo_labels(incidents, point_df, cfg, out_path, dataset_name=ds.name)
            _copy_if_exists(out_path / str(cfg.llm_teacher_cache_dir) / "llm_pseudo_labels.jsonl", out_path / "llm_pseudo_labels.jsonl")
            _copy_if_exists(out_path / str(cfg.llm_teacher_cache_dir) / "teacher_stats.json", out_path / "teacher_stats.json")
            summary["counts"]["teacher_labels"] = int(len(teacher_labels))
            summary["artifacts"]["llm_pseudo_labels"] = "llm_pseudo_labels.jsonl"
            summary["artifacts"]["teacher_stats"] = "teacher_stats.json"
        except Exception as exc:
            log.warning("LLM teacher failed for %s: %s", ds.name, exc)
            teacher_labels = []

    if mode in _RCA_TRAIN_MODES and not rca_df.empty:
        rca_df = _teacher_targets(rca_df, teacher_labels)
        rca_features_mod.save_rca_features(rca_df, out_path)
        try:
            from .rca_gnn import build_incident_edge_index, train_rca_gnn

            edge_index, edge_weight = build_incident_edge_index(rca_df, entity_graph)
            rca_out = train_rca_gnn(rca_df, edge_index, cfg, edge_weight=edge_weight)
            if rca_out.model is not None:
                import torch

                torch.save(rca_out.model.state_dict(), out_path / "rca_gnn_model.pt")
                summary["artifacts"]["rca_gnn_model"] = "rca_gnn_model.pt"
            scores = rca_df[["incident_id", "network_element_id"]].copy()
            scores["root_cause_score"] = rca_out.root_cause_score
            scores.to_csv(out_path / "rca_scores.csv", index=False)
            summary["artifacts"]["rca_scores"] = "rca_scores.csv"
            # Aggregate per network element for the final Top-5 override.
            grouped = scores.groupby("network_element_id", observed=True)["root_cause_score"].max().reset_index()
            grouped = grouped.sort_values("root_cause_score", ascending=False)
            summary["node_scores_override"] = [
                {"network_element_id": str(r), "root_cause_score": float(s)}
                for r, s in zip(grouped["network_element_id"], grouped["root_cause_score"])
            ]
            if use_llm and mode in {"full", "llm_distill"} and incidents:
                try:
                    from .llm_tasks import task_b_rca_verification

                    score_view = scores.copy()
                    inc_order = (
                        score_view.groupby("incident_id", observed=True)["root_cause_score"]
                        .max()
                        .sort_values(ascending=False)
                        .head(int(cfg.llm_task_b_max_incidents))
                        .index.astype(str)
                        .tolist()
                    )
                    verification: list[dict[str, Any]] = []
                    for inc_id in inc_order:
                        incident = next((x for x in incidents if str(x.get("incident_id")) == str(inc_id)), None)
                        if incident is None:
                            continue
                        candidates = (
                            score_view[score_view["incident_id"].astype(str) == str(inc_id)]
                            .sort_values("root_cause_score", ascending=False)["network_element_id"]
                            .astype(str)
                            .tolist()
                        )
                        verification.append(task_b_rca_verification(incident, candidates, cfg))
                    with (out_path / "llm_verification.jsonl").open("w", encoding="utf-8") as f:
                        for item in verification:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    threshold = float(cfg.llm_anomaly_probability_threshold)
                    high_ver = [x for x in verification if x.get("is_anomaly") and float(x.get("anomaly_probability", 0.0)) >= threshold]
                    low_ver = [x for x in verification if x not in high_ver]
                    with (out_path / "llm_verification_high.jsonl").open("w", encoding="utf-8") as f:
                        for item in high_ver:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    with (out_path / "llm_verification_low.jsonl").open("w", encoding="utf-8") as f:
                        for item in low_ver:
                            f.write(json.dumps(item, ensure_ascii=False) + "\n")
                    summary["artifacts"]["llm_verification"] = "llm_verification.jsonl"
                    summary["artifacts"]["llm_verification_high"] = "llm_verification_high.jsonl"
                    summary["artifacts"]["llm_verification_low"] = "llm_verification_low.jsonl"
                    summary["counts"]["llm_verified_incidents"] = int(len(verification))
                    summary["counts"]["llm_high_confidence_incidents"] = int(len(high_ver))
                    summary["counts"]["llm_low_confidence_incidents"] = int(len(low_ver))
                except Exception as exc:
                    log.warning("LLM Task B verification failed for %s: %s", ds.name, exc)
        except Exception as exc:
            log.warning("RCA GNN training failed for %s: %s", ds.name, exc)
    dump_json(out_path / "experiment_summary.json", summary)
    return summary
