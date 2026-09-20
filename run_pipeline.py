from __future__ import annotations

import argparse
import json

from aiops.config import PipelineConfig
from aiops.pipeline import run_all


def main() -> None:
    p = argparse.ArgumentParser(description="Train the metric/VAE/GNN anomaly pipeline over every processed dataset.")
    p.add_argument("--workspace", default=str(PipelineConfig().workspace))
    p.add_argument("--output-dir", default="outputs")
    p.add_argument("--dataset", default=None, help="substring filter, e.g. shenyang")
    p.add_argument(
        "--experiment",
        default="baseline",
        choices=["baseline", "episode", "incident", "rca_gnn", "llm_pseudo", "hybrid", "llm_distill", "full"],
        help="pipeline experiment mode; baseline keeps the original behaviour",
    )
    p.add_argument("--llm-backend", default="none", choices=["none", "ollama", "openai_compatible", "transformers"])
    p.add_argument("--llm-model", default="qwen2.5:7b")
    p.add_argument("--llm-base-url", default="http://localhost:11434")
    p.add_argument("--llm-max-new-tokens", type=int, default=1024)
    p.add_argument("--bin-minutes", type=int, default=5)
    p.add_argument("--vae-epochs", type=int, default=40)
    p.add_argument("--gnn-epochs", type=int, default=120)
    p.add_argument("--rca-gnn-epochs", type=int, default=120)
    p.add_argument("--llm-teacher-num-cases", type=int, default=100)
    p.add_argument("--max-netflow-rows", type=int, default=250_000)
    args = p.parse_args()

    cfg = PipelineConfig(
        workspace=args.workspace,
        output_dir=args.output_dir,
        llm_backend=args.llm_backend,
        llm_model=args.llm_model,
        llm_base_url=args.llm_base_url,
        llm_max_new_tokens=args.llm_max_new_tokens,
        bin_minutes=args.bin_minutes,
        vae_epochs=args.vae_epochs,
        gnn_epochs=args.gnn_epochs,
        rca_gnn_epochs=args.rca_gnn_epochs,
        llm_teacher_num_cases=args.llm_teacher_num_cases,
        max_netflow_rows=args.max_netflow_rows,
        experiment_mode=args.experiment,
    )
    result = run_all(cfg.workspace, cfg, dataset_filter=args.dataset, use_llm=cfg.llm_backend != "none")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
