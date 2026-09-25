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
    p.add_argument(
        "--use-gnn",
        dest="use_gnn",
        action="store_true",
        default=True,
        help="Run the graph GNN (experiments A/B/D/E).",
    )
    p.add_argument(
        "--no-gnn",
        dest="use_gnn",
        action="store_false",
        help="Disable the GNN entirely (experiment C).  Unlike --gnn-epochs 0 this "
             "does not leave a randomly initialised GCN driving the point score.",
    )
    p.add_argument("--rca-gnn-epochs", type=int, default=120)
    p.add_argument(
        "--second-stage-mode",
        default="",
        choices=["", "weighted", "threshold"],
        help="Second unsupervised model before the GNN: 'weighted' = experiment D, "
             "'threshold' = experiment E, '' = off (A/B/C).",
    )
    p.add_argument("--second-stage-alpha", type=float, default=1.0, help="D: weight = max(0, 1 - alpha*first_anomaly)")
    p.add_argument("--second-stage-threshold", type=float, default=0.7, help="E: exclude first_anomaly > threshold")
    p.add_argument("--second-stage-epochs", type=int, default=20)
    p.add_argument("--llm-teacher-num-cases", type=int, default=100)
    p.add_argument("--llm-workers", type=int, default=8,
                   help="Concurrency for the LLM teacher and Task A stages.")
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
        use_gnn=args.use_gnn,
        second_stage_mode=args.second_stage_mode,
        second_stage_alpha=args.second_stage_alpha,
        second_stage_threshold=args.second_stage_threshold,
        second_stage_epochs=args.second_stage_epochs,
        rca_gnn_epochs=args.rca_gnn_epochs,
        llm_teacher_num_cases=args.llm_teacher_num_cases,
        llm_teacher_workers=args.llm_workers,
        llm_task_a_workers=args.llm_workers,
        max_netflow_rows=args.max_netflow_rows,
        experiment_mode=args.experiment,
    )
    result = run_all(cfg.workspace, cfg, dataset_filter=args.dataset, use_llm=cfg.llm_backend != "none")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
