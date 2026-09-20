"""Experiment A: post-hoc incident-level LLM verification with high/low routing.

This script does not retrain VAE/GNN.  It consumes existing full-mode artifacts
(incident_candidates.json, evidence.json, rca_scores.csv) and calls a vLLM
OpenAI-compatible endpoint once per incident.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from aiops.config import PipelineConfig
from aiops.dataset import discover_datasets
from aiops.incident_predictions import generate_incident_predictions, merge_adjacent_incidents, save_routed
from aiops.llm_diagnoser import LLMBackend
from aiops.utils import get_logger

log = get_logger(__name__)


def main() -> int:
    p = argparse.ArgumentParser(description="Incident-level experiment A with vLLM verification.")
    p.add_argument("--workspace", required=True)
    p.add_argument("--artifacts-dir", required=True, help="Existing full-mode artifact root.")
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
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    datasets = {d.name: d for d in discover_datasets(args.workspace)}
    all_high: list[dict] = []
    all_low: list[dict] = []
    all_meta: list[dict] = []
    summary = []
    for ds_name, ds in sorted(datasets.items()):
        ds_dir = artifacts / ds_name
        inc_path = ds_dir / "incident_candidates.json"
        if not inc_path.exists():
            log.warning("skip %s: missing %s", ds_name, inc_path)
            continue
        incidents = json.loads(inc_path.read_text(encoding="utf-8")).get("incidents", [])
        if not incidents:
            continue
        merged = merge_adjacent_incidents(incidents, args.merge_gap_minutes)
        evidence_path = ds_dir / "evidence.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else {}
        rca = None
        rca_path = ds_dir / "rca_scores.csv"
        if rca_path.exists():
            import pandas as pd

            rca = pd.read_csv(rca_path)
        log.info("experiment A %s: incidents=%d merged=%d", ds_name, len(incidents), len(merged))
        fallback_nodes: list[str] = []
        for item in evidence.get("node_scores", []) or []:
            nid = str(item.get("network_element_id", ""))
            if nid and nid not in fallback_nodes:
                fallback_nodes.append(nid)
        try:
            from aiops.dataset import official_network_element_ids
            for nid in official_network_element_ids(ds.region_code):
                if nid not in fallback_nodes:
                    fallback_nodes.append(nid)
        except Exception:
            pass
        routed = generate_incident_predictions(
            merged,
            ds_name,
            cfg,
            region_code=ds.region_code,
            evidence=evidence,
            rca_scores=rca,
            fallback_nodes=fallback_nodes,
            backend=backend,
            max_workers=args.workers,
            threshold=args.threshold,
        )
        ds_out = out_root / ds_name
        save_routed(ds_out, routed["high"], routed["low"], routed["meta"])
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
        log.info(
            "experiment A %s done: high=%d low=%d",
            ds_name,
            len(routed["high"]),
            len(routed["low"]),
        )

    # Global combined files.
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
