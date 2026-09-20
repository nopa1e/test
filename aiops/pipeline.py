from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch

from . import anomaly, features, graph, pipeline_ext
from .config import PipelineConfig
from .dataset import DatasetInfo, discover_datasets, official_network_element_ids
from .gnn import train_gnn
from .taxonomy import infer_category_from_evidence, is_valid_category
from .utils import dump_json, ensure_dir, get_logger, read_json, to_iso

log = get_logger(__name__)


def _safe_json_features(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, ensure_ascii=False, default=str)


def _top_point_evidence(
    point_df: pd.DataFrame,
    X_imputed: np.ndarray,
    feature_names: list[str],
    top_n: int = 40,
    top_k_features: int = 6,
) -> list[dict[str, Any]]:
    order = np.argsort(point_df["final_normal_score"].to_numpy(dtype=float), kind="stable")
    out: list[dict[str, Any]] = []
    for rank, idx in enumerate(order[:top_n], start=1):
        row = point_df.iloc[int(idx)]
        model_row = int(row.get("_model_row", idx))
        feats = features.top_feature_deviations(X_imputed, feature_names, model_row, top_k=top_k_features)
        out.append(
            {
                "rank": rank,
                "point_id": str(row.get("point_id", "")),
                "timestamp": to_iso(row["timestamp_bin"]),
                "network_element_id": str(row["network_element_id"]),
                "node_type": str(row.get("node_type", "unknown")),
                "normal_score": float(row["final_normal_score"]),
                "if_anomaly_score": float(row.get("if_anomaly_score", 0.0)),
                "vae_anomaly_score": float(row.get("vae_anomaly_score", 0.0)),
                "top_features": feats,
            }
        )
    return out


def _rank_nodes(point_df: pd.DataFrame, top_k: int = 5) -> list[dict[str, Any]]:
    df = point_df.copy()
    df["anomaly_score"] = 1.0 - df["final_normal_score"].astype(float)
    rows: list[dict[str, Any]] = []
    for neid, grp in df.groupby("network_element_id", observed=True):
        vals = grp["anomaly_score"].to_numpy(dtype=float)
        if vals.size == 0:
            continue
        # Sustained and peak anomaly both matter for root-cause ranking.
        score = 0.65 * float(np.nanmean(vals)) + 0.35 * float(np.nanpercentile(vals, 95))
        rows.append(
            {
                "network_element_id": str(neid),
                "anomaly_score": score,
                "mean_anomaly": float(np.nanmean(vals)),
                "p95_anomaly": float(np.nanpercentile(vals, 95)),
                "top_point_rank": int(grp["point_rank"].min()) if "point_rank" in grp else None,
                "node_type": str(grp["node_type"].iloc[0]) if "node_type" in grp else "unknown",
            }
        )
    rows.sort(key=lambda r: (-r["anomaly_score"], r["network_element_id"]))
    return rows[:top_k]


def _save_artifacts(
    ds: DatasetInfo,
    cfg: PipelineConfig,
    point_df: pd.DataFrame,
    meta: dict[str, Any],
    entity_graph: graph.nx.DiGraph if hasattr(graph, "nx") else Any,
    graph_meta: dict[str, Any],
    point_graph_meta: dict[str, Any],
    first_out: anomaly.FirstStageOutput,
    gnn_out: Any,
    node_scores: list[dict[str, Any]],
    top_points: list[dict[str, Any]],
) -> Path:
    out_dir = ensure_dir(Path(cfg.output_dir) / ds.name)
    keep_cols = [
        "point_id", "timestamp_bin", "region", "node", "node_type", "network_element_id",
        "final_normal_score", "gnn_normal_score", "ensemble_normal_score",
        "if_anomaly_score", "vae_anomaly_score", "combined_anomaly_score",
        "pseudo_normal_score", "pseudo_label", "point_rank",
    ]
    keep = [c for c in keep_cols if c in point_df.columns]
    score_df = point_df[keep].copy()
    score_df["top_feature_json"] = ""
    if top_points:
        order = {p["point_id"]: p for p in top_points}
        score_df["top_feature_json"] = score_df["point_id"].map(
            lambda pid: _safe_json_features(order.get(str(pid), {}).get("top_features", []))
        )
    score_df.to_csv(out_dir / "point_scores.csv.gz", index=False, compression="gzip")

    pd.DataFrame(node_scores).to_csv(out_dir / "node_scores.csv", index=False)

    topology = {
        "dataset": ds.name,
        "region_code": ds.region_code,
        "nodes": graph_meta.get("nodes", []),
        "edges": graph_meta.get("edges", []),
        "topology_source": graph_meta.get("topology_source", "unknown"),
    }
    dump_json(out_dir / "topology.json", topology)
    meta = dict(meta)
    meta.update(
        {
            "point_graph": point_graph_meta,
            "node_scores": node_scores,
            "first_stage_feature_count": len(first_out.feature_names),
            "gnn_history": gnn_out.history if gnn_out is not None else [],
        }
    )
    dump_json(out_dir / "dataset_meta.json", meta)
    evidence = {
        "dataset": ds.name,
        "region_code": ds.region_code,
        "time_start": meta.get("time_start"),
        "time_end": meta.get("time_end"),
        "nodes": meta.get("nodes", []),
        "node_scores": node_scores,
        "top_points": top_points,
        "topology": topology,
    }
    dump_json(out_dir / "evidence.json", evidence)

    # Persist model state when torch models exist.
    try:
        if first_out.vae_model is not None:
            torch.save(first_out.vae_model.state_dict(), out_dir / "vae_model.pt")
        if gnn_out is not None and getattr(gnn_out, "model", None) is not None:
            torch.save(gnn_out.model.state_dict(), out_dir / "gnn_model.pt")
    except Exception as exc:
        log.warning("failed to save model state for %s: %s", ds.name, exc)
    log.info("saved artifacts for %s -> %s", ds.name, out_dir)
    return out_dir


def _build_prediction(
    ds: DatasetInfo,
    major: str,
    sub: str,
    node_scores: list[dict[str, Any]],
    evidence: dict[str, Any],
    prediction_id: str | None = None,
) -> dict[str, Any]:
    if not is_valid_category(major, sub):
        major, sub = infer_category_from_evidence(evidence, node_scores)
    raw_times = [p.get("timestamp") for p in evidence.get("top_points", []) if p.get("timestamp")]
    parsed_times = [pd.to_datetime(t, utc=True) for t in raw_times[:5]]
    parsed_times = [t for t in parsed_times if pd.notna(t)]
    if parsed_times:
        top_ts = parsed_times[0]
        if len(parsed_times) > 1 and (max(parsed_times) - min(parsed_times)) <= pd.Timedelta(hours=2):
            start_ts, end_ts = min(parsed_times), max(parsed_times)
        else:
            start_ts, end_ts = top_ts - pd.Timedelta(minutes=5), top_ts + pd.Timedelta(minutes=5)
    else:
        start_ts = pd.to_datetime(evidence.get("time_start", "2026-01-01T00:00:00.000+00:00"), utc=True)
        end_ts = pd.to_datetime(evidence.get("time_end", "2026-01-01T00:10:00.000+00:00"), utc=True)
        if pd.isna(start_ts):
            start_ts = pd.Timestamp.utcnow()
        if pd.isna(end_ts) or end_ts <= start_ts:
            end_ts = start_ts + pd.Timedelta(minutes=5)
    start, end = to_iso(start_ts), to_iso(end_ts)
    # Final safety: at most 5 unique legal official elements.
    allowed = set(official_network_element_ids(ds.region_code))
    root_cause_top5: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in node_scores:
        neid = str(row.get("network_element_id", ""))
        if not neid or neid in seen or neid not in allowed:
            continue
        seen.add(neid)
        root_cause_top5.append({"rank": len(root_cause_top5) + 1, "network_element_id": neid})
        if len(root_cause_top5) == 5:
            break
    if prediction_id is None:
        region = ds.region_code or "region"
        prediction_id = f"pred_{region}_{abs(hash(ds.name)) % 100000000:08d}"
    return {
        "prediction_id": prediction_id,
        "start_time": to_iso(start),
        "end_time": to_iso(end),
        "root_cause_top5": root_cause_top5,
        "fault_category": {"major_category": major, "sub_category": sub},
    }


def run_one_dataset(
    ds: DatasetInfo,
    cfg: PipelineConfig,
    use_llm: bool = False,
    prediction_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    log.info("=" * 70)
    log.info("running dataset %s (%s)", ds.name, ds.region_code)
    point_df, meta = features.build_point_table(ds, cfg)

    X, feature_names = features.build_feature_matrix(point_df)
    first_out = anomaly.run_first_stage(X, feature_names, cfg)

    point_df = point_df.copy()
    point_df["if_anomaly_score"] = first_out.if_anomaly_score
    point_df["vae_anomaly_score"] = first_out.vae_anomaly_score
    point_df["combined_anomaly_score"] = first_out.combined_anomaly_score
    point_df["pseudo_normal_score"] = first_out.pseudo_normal_score
    point_df["pseudo_label"] = first_out.pseudo_label

    # Graph-based features include the two unsupervised anomaly scores and the
    # pseudo-normal confidence.  This is the "point feature" idea requested.
    gnn_feature_names = list(first_out.feature_names) + [
        "if_anomaly_score",
        "vae_anomaly_score",
        "pseudo_normal_score",
    ]
    X_gnn = np.column_stack(
        [
            first_out.X_imputed,
            first_out.if_anomaly_score,
            first_out.vae_anomaly_score,
            first_out.pseudo_normal_score,
        ]
    )

    entity_graph, graph_meta = graph.build_entity_graph(ds, point_df, cfg)
    edge_index, point_ids, point_graph_meta = graph.build_point_graph(ds, point_df, entity_graph, cfg)
    point_df["point_id"] = point_ids
    point_df["_model_row"] = np.arange(len(point_df), dtype=int)

    gnn_out = train_gnn(
        X_gnn,
        gnn_feature_names,
        edge_index,
        first_out.pseudo_label,
        first_out.pseudo_normal_score,
        cfg,
    )
    point_df["gnn_normal_score"] = gnn_out.normal_score
    point_df["ensemble_normal_score"] = (
        0.55 * gnn_out.normal_score
        + 0.25 * (1.0 - first_out.if_anomaly_score)
        + 0.20 * (1.0 - first_out.vae_anomaly_score)
    )
    # The primary score is the GNN normal score.  Lower score = more abnormal.
    point_df["final_normal_score"] = point_df["gnn_normal_score"].astype(float)
    point_df = point_df.sort_values("final_normal_score", ascending=True).reset_index(drop=True)
    point_df["point_rank"] = np.arange(1, len(point_df) + 1, dtype=int)

    node_scores = _rank_nodes(point_df, top_k=10)
    top_points = _top_point_evidence(
        point_df,
        gnn_out.X_imputed,
        gnn_out.feature_names,
        top_n=40,
        top_k_features=6,
    )
    evidence = {
        "dataset": ds.name,
        "region_code": ds.region_code,
        "time_start": meta.get("time_start"),
        "time_end": meta.get("time_end"),
        "nodes": meta.get("nodes", []),
        "node_scores": node_scores,
        "top_points": top_points,
        "topology": {
            "nodes": graph_meta.get("nodes", []),
            "edges": graph_meta.get("edges", []),
            "topology_source": graph_meta.get("topology_source", "unknown"),
        },
        "first_stage": {
            "feature_count": len(first_out.feature_names),
            "if_mean_anomaly": float(np.nanmean(first_out.if_anomaly_score)),
            "vae_mean_anomaly": float(np.nanmean(first_out.vae_anomaly_score)),
        },
    }

    out_dir = _save_artifacts(
        ds,
        cfg,
        point_df,
        meta,
        entity_graph,
        graph_meta,
        point_graph_meta,
        first_out,
        gnn_out,
        node_scores,
        top_points,
    )

    experiment = pipeline_ext.run_extension_stages(
        ds,
        cfg,
        point_df,
        entity_graph,
        out_dir,
        use_llm=use_llm,
    )
    prediction_node_scores = list(node_scores)
    override = experiment.get("node_scores_override") if isinstance(experiment, dict) else None
    if override:
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for item in override:
            neid = str(item.get("network_element_id", ""))
            if not neid or neid in seen:
                continue
            seen.add(neid)
            score = float(item.get("root_cause_score", 0.0))
            merged.append({
                "network_element_id": neid,
                "anomaly_score": score,
                "root_cause_score": score,
                "source": "rca_gnn",
            })
        for item in node_scores:
            neid = str(item.get("network_element_id", ""))
            if neid and neid not in seen:
                seen.add(neid)
                merged.append(item)
        if merged:
            prediction_node_scores = merged

    major, sub = infer_category_from_evidence(evidence, prediction_node_scores)
    prediction: dict[str, Any]
    if use_llm and cfg.llm_backend != "none":
        try:
            from .llm_diagnoser import diagnose_dataset

            prediction = diagnose_dataset(ds, cfg, evidence, prediction_node_scores, out_dir, prediction_id=prediction_id)
        except Exception as exc:
            log.warning("LLM diagnosis failed for %s; using rule fallback: %s", ds.name, exc)
            prediction = _build_prediction(ds, major, sub, prediction_node_scores, evidence, prediction_id=prediction_id)
    else:
        prediction = _build_prediction(ds, major, sub, prediction_node_scores, evidence, prediction_id=prediction_id)

    if not is_valid_category(
        prediction.get("fault_category", {}).get("major_category", ""),
        prediction.get("fault_category", {}).get("sub_category", ""),
    ):
        prediction = _build_prediction(ds, major, sub, prediction_node_scores, evidence, prediction_id=prediction_id)
    dump_json(out_dir / "prediction.json", prediction)
    result = {
        "dataset": ds.name,
        "region_code": ds.region_code,
        "output_dir": str(out_dir),
        "prediction": prediction,
        "top_nodes": prediction_node_scores[:5],
        "experiment": experiment,
    }
    log.info("dataset %s prediction: %s", ds.name, json.dumps(prediction, ensure_ascii=False))
    return result, evidence


def run_all(
    workspace: str | Path,
    cfg: PipelineConfig,
    dataset_filter: str | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    datasets = discover_datasets(workspace)
    if dataset_filter:
        datasets = [d for d in datasets if dataset_filter.lower() in d.name.lower()]
    log.info("discovered %d datasets", len(datasets))
    if not datasets:
        raise SystemExit("no datasets with processed/*.csv were found")

    results: list[dict[str, Any]] = []
    for i, ds in enumerate(datasets, start=1):
        pred_id = f"pred_{i:06d}"
        try:
            result, _ = run_one_dataset(ds, cfg, use_llm=use_llm, prediction_id=pred_id)
            results.append(result)
        except Exception as exc:
            log.exception("dataset %s failed: %s", ds.name, exc)
            results.append({"dataset": ds.name, "error": str(exc)})

    out_root = ensure_dir(Path(cfg.output_dir) / "_all")
    dump_json(out_root / "run_summary.json", {"config": cfg.to_dict(), "results": results})
    dump_json(
        out_root / "experiment_summary.json",
        {
            "experiment_mode": cfg.experiment_mode,
            "datasets": len(results),
            "experiments": [r.get("experiment") for r in results if isinstance(r, dict) and r.get("experiment")],
        },
    )
    with (out_root / "predictions.jsonl").open("w", encoding="utf-8") as f:
        for r in results:
            if "prediction" in r:
                f.write(json.dumps(r["prediction"], ensure_ascii=False) + "\n")
    log.info("all done. summary: %s", out_root / "run_summary.json")
    return {"results": results, "summary_path": str(out_root / "run_summary.json")}
