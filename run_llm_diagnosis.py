from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from aiops.config import PipelineConfig
from aiops.dataset import discover_datasets
from aiops.llm_diagnoser import diagnose_dataset
from aiops.utils import read_json


def main() -> None:
    p = argparse.ArgumentParser(description="Re-run the second-stage LLM diagnosis for one already-processed dataset.")
    p.add_argument("--workspace", default=str(PipelineConfig().workspace))
    p.add_argument("--output-dir", default="outputs")
    p.add_argument("--dataset", required=True)
    p.add_argument(
        "--experiment",
        default="baseline",
        choices=["baseline", "episode", "incident", "rca_gnn", "llm_pseudo", "hybrid", "llm_distill", "full"],
    )
    p.add_argument("--llm-backend", default="ollama", choices=["ollama", "openai_compatible", "transformers"])
    p.add_argument("--llm-model", default="qwen2.5:7b")
    p.add_argument("--llm-base-url", default="http://localhost:11434")
    p.add_argument("--llm-max-new-tokens", type=int, default=1024)
    p.add_argument("--no-llm-tools", action="store_true", help="Do not send MCP tools to an OpenAI-compatible backend.")
    args = p.parse_args()

    cfg = PipelineConfig(
        workspace=args.workspace,
        output_dir=args.output_dir,
        llm_backend=args.llm_backend,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
        llm_max_new_tokens=args.llm_max_new_tokens,
        llm_tool_calling=not args.no_llm_tools,
        experiment_mode=args.experiment,
    )
    datasets = {d.name: d for d in discover_datasets(cfg.workspace)}
    ds = datasets.get(args.dataset)
    if ds is None:
        matches = [d for n, d in datasets.items() if args.dataset.lower() in n.lower()]
        if not matches:
            raise SystemExit(f"dataset not found: {args.dataset}")
        ds = matches[0]
    ds_dir = Path(cfg.output_dir) / ds.name
    evidence = read_json(ds_dir / "evidence.json")
    node_scores = pd.read_csv(ds_dir / "node_scores.csv").to_dict(orient="records")
    prediction = diagnose_dataset(ds, cfg, evidence, node_scores, out_dir=ds_dir, prediction_id="pred_000001")
    print(json.dumps(prediction, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
