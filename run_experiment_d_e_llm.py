"""Experiment D/E LLM stage: use second-stage unsupervised scores before LLM RCA.

D/E are not standalone.  They first train a second unsupervised model to better
model normal behaviour, then the second-stage anomaly scores replace/augment the
incident candidate ranking.  Finally the same incident-level LLM verification
and high/low-confidence routing used by experiments A/B/C is applied.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from aiops.config import PipelineConfig
from aiops.dataset import discover_datasets, official_network_element_ids
from aiops.incident_predictions import generate_incident_predictions, merge_adjacent_incidents, save_routed
from aiops.llm_diagnoser import LLMBackend
from aiops.utils import get_logger

log = get_logger(__name__)


def _second_stage_rca_scores(incidents: list[dict], node_scores: pd.DataFrame) -> pd.DataFrame:
    score_map = {}
    if not node_scores.empty and "network_element_id" in node_scores.columns:
        for _, r in node_scores.iterrows():
            nid = str(r.get("network_element_id", ""))
            if nid:
                score_map[nid] = float(r.get("second_anomaly", 0.0))
    rows = []
    for inc in incidents:
        iid = str(inc.get("incident_id"))
        for nid in inc.get("nodes", []) or []:
            nid = str(nid)
            rows.append({
                "incident_id": iid,
                "network_element_id": nid,
                "root_cause_score": float(score_map.get(nid, 0.0)),
            })
    return pd.DataFrame(rows)


def main() -> int:
    p = argparse.ArgumentParser(description="Experiment D/E LLM stage.")
    p.add_argument("--workspace", required=True)
    p.add_argument("--artifacts-dir", required=True)
    p.add_argument("--second-stage-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--llm-base-url", default="http://127.0.0.1:8000")
    p.add_argument("--llm-model", default="deepseek-r1-14b")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--merge-gap-minutes", type=int, default=5)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--llm-timeout", type=int, default=180)
    p.add_argument("--llm-max-new-tokens", type=int, default=256)
    args = p.parse_args()

    cfg = PipelineConfig(
        workspace=args.workspace,
        output_dir=args.output_dir,
        llm_backend="openai_compatible",
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        llm_tool_calling=False,
        llm_timeout_seconds=args.llm_timeout,
        llm_max_new_tokens=args.llm_max_new_tokens,
        experiment_mode="full",
    )
    backend = LLMBackend(cfg)
    artifacts = Path(args.artifacts_dir)
    second_root = Path(args.second_stage_dir)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    datasets = {d.name: d for d in discover_datasets(args.workspace)}

    all_high, all_low, all_meta, summary = [], [], [], []
    for ds_name, ds in sorted(datasets.items()):
        ds_dir = artifacts / ds_name
        inc_path = ds_dir / "incident_candidates.json"
        if not inc_path.exists():
            log.warning("skip %s: missing incident candidates", ds_name)
            continue
        incidents = json.loads(inc_path.read_text(encoding="utf-8")).get("incidents", [])
        if not incidents:
            continue
        node_path = second_root / f"{ds_name}_second_stage_node_scores.csv"
        node_scores = pd.read_csv(node_path) if node_path.exists() else pd.DataFrame()
        rca_scores = _second_stage_rca_scores(incidents, node_scores)
        merged = merge_adjacent_incidents(incidents, args.merge_gap_minutes)
        fallback_nodes: list[str] = []
        if not node_scores.empty and "network_element_id" in node_scores.columns:
            fallback_nodes = [str(x) for x in node_scores["network_element_id"].tolist() if str(x)]
        for nid in official_network_element_ids(ds.region_code):
            if nid not in fallback_nodes:
                fallback_nodes.append(nid)
        evidence_path = ds_dir / "evidence.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else {}
        log.info("experiment D/E LLM %s: incidents=%d merged=%d", ds_name, len(incidents), len(merged))
        routed = generate_incident_predictions(
            merged,
            ds_name,
            cfg,
            region_code=ds.region_code,
            evidence=evidence,
            rca_scores=rca_scores,
            fallback_nodes=fallback_nodes,
            backend=backend,
            max_workers=args.workers,
            threshold=args.threshold,
        )
        save_routed(out_root / ds_name, routed["high"], routed["low"], routed["meta"])
        all_high.extend(routed["high"])
        all_low.extend(routed["low"])
        all_meta.extend(routed["meta"])
        summary.append({
            "dataset": ds_name,
            "incidents": len(incidents),
            "merged": len(merged),
            "high_conf": len(routed["high"]),
            "low_conf": len(routed["low"]),
        })
        log.info("experiment D/E LLM %s done: high=%d low=%d", ds_name, len(routed["high"]), len(routed["low"]))

    def _write(path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    _write(out_root / "predictions_high_conf.jsonl", all_high)
    _write(out_root / "predictions_low_conf.jsonl", all_low)
    _write(out_root / "llm_anomaly_scores.jsonl", all_meta)
    (out_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "output_dir": str(out_root)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
