"""Run reproducible AIOps pipeline ablations.

Usage example:

    python run_ablation.py --workspace /data/workspace --output-dir outputs_ablation --dataset shenyang
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from aiops.config import PipelineConfig
from aiops.pipeline import run_all
from aiops.utils import dump_json


EXPERIMENTS: list[tuple[str, str]] = [
    ("baseline", "baseline"),
    ("ifvae_gnn", "baseline"),
    ("episode", "episode"),
    ("incident", "incident"),
    ("rca_gnn", "rca_gnn"),
    ("llm_pseudo", "llm_pseudo"),
    ("hybrid", "hybrid"),
    ("distill", "llm_distill"),
    ("full", "full"),
]


def _count_duplicate_predictions(predictions: list[dict]) -> int:
    seen: set[str] = set()
    dup = 0
    for p in predictions:
        key = json.dumps(
            {
                "start_time": p.get("start_time"),
                "end_time": p.get("end_time"),
                "roots": [r.get("network_element_id") for r in p.get("root_cause_top5", [])],
            },
            sort_keys=True,
        )
        if key in seen:
            dup += 1
        seen.add(key)
    return dup


def _proxy_summary(summary: dict) -> dict:
    predictions = [r["prediction"] for r in summary.get("results", []) if isinstance(r, dict) and "prediction" in r]
    incident_count = 0
    episode_count = 0
    for r in summary.get("results", []):
        out_dir = Path(r.get("output_dir", "")) if isinstance(r, dict) else None
        if out_dir and out_dir.exists():
            ep = out_dir / "episode_scores.csv"
            inc = out_dir / "incident_candidates.json"
            if ep.exists():
                try:
                    import pandas as pd

                    episode_count += int(len(pd.read_csv(ep)))
                except Exception:
                    pass
            if inc.exists():
                try:
                    incident_count += int(len(json.loads(inc.read_text(encoding="utf-8")).get("incidents", [])))
                except Exception:
                    pass
    return {
        "predictions": len(predictions),
        "duplicate_prediction_count": int(_count_duplicate_predictions(predictions)),
        "incident_count": int(incident_count),
        "episode_count": int(episode_count),
        "merge_count": "N/A",
        "split_count": "N/A",
        "Top1_RCA": "N/A",
        "Top3_RCA": "N/A",
        "MRR": "N/A",
        "Major_accuracy": "N/A",
        "Minor_accuracy": "N/A",
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Run V0-V8 AIOps ablations.")
    p.add_argument("--workspace", required=True)
    p.add_argument("--output-dir", default="outputs_ablation")
    p.add_argument("--dataset", default=None)
    p.add_argument("--experiments", default=",".join(name for name, _ in EXPERIMENTS))
    p.add_argument("--vae-epochs", type=int, default=3)
    p.add_argument("--gnn-epochs", type=int, default=5)
    p.add_argument("--rca-gnn-epochs", type=int, default=40)
    p.add_argument("--llm-backend", default="none", choices=["none", "ollama", "openai_compatible", "transformers"])
    p.add_argument("--llm-model", default="qwen2.5:7b")
    p.add_argument("--llm-base-url", default="http://localhost:11434")
    p.add_argument("--llm-teacher-num-cases", type=int, default=100)
    p.add_argument("--llm-workers", type=int, default=8,
                   help="Concurrency for the LLM teacher and Task A stages.")
    p.add_argument("--no-llm-tools", action="store_true", help="Do not send MCP tools to an OpenAI-compatible backend.")
    p.add_argument("--no-gnn", dest="use_gnn", action="store_false", default=True,
                   help="Disable the graph GNN entirely (unlike --gnn-epochs 0, which "
                        "leaves a randomly initialised GCN driving the point score).")
    p.add_argument("--max-netflow-rows", type=int, default=50_000)
    args = p.parse_args()

    base_out = Path(args.output_dir)
    ablation_dir = base_out / "_ablation"
    records: list[dict] = []
    summaries: dict[str, dict] = {}
    name_to_mode = dict(EXPERIMENTS)
    requested = [x.strip() for x in args.experiments.split(",") if x.strip()]
    for name in requested:
        mode = name_to_mode.get(name)
        if mode is None:
            print(f"skip unknown experiment: {name}")
            continue
        out_dir = ablation_dir / name
        cfg = PipelineConfig(
            workspace=args.workspace,
            output_dir=out_dir,
            vae_epochs=args.vae_epochs,
            gnn_epochs=args.gnn_epochs,
            use_gnn=args.use_gnn,
            rca_gnn_epochs=args.rca_gnn_epochs,
            llm_backend=args.llm_backend,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
            llm_teacher_num_cases=args.llm_teacher_num_cases,
            llm_teacher_workers=args.llm_workers,
            llm_task_a_workers=args.llm_workers,
            max_netflow_rows=args.max_netflow_rows,
            experiment_mode=mode,
            llm_tool_calling=not args.no_llm_tools,
        )
        print(f"=== experiment {name} (mode={mode}) ===", flush=True)
        try:
            summary = run_all(cfg.workspace, cfg, dataset_filter=args.dataset, use_llm=args.llm_backend != "none")
            summaries[name] = summary
            proxy = _proxy_summary(summary)
        except Exception as exc:
            summaries[name] = {"error": f"{type(exc).__name__}: {exc}"}
            proxy = {"predictions": 0, "duplicate_prediction_count": "N/A", "incident_count": "N/A", "episode_count": "N/A"}
        records.append({
            "experiment": name,
            "mode": mode,
            "AD": "N/A",
            "RCA": "N/A",
            "Major": "N/A",
            "Minor": "N/A",
            "Total": "N/A",
            **proxy,
        })

    base_out.mkdir(parents=True, exist_ok=True)
    csv_path = base_out / "ablation_results.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        fieldnames = [
            "experiment", "mode", "AD", "RCA", "Major", "Minor", "Total",
            "predictions", "duplicate_prediction_count", "incident_count",
            "episode_count", "merge_count", "split_count", "Top1_RCA",
            "Top3_RCA", "MRR", "Major_accuracy", "Minor_accuracy",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in records:
            writer.writerow({k: row.get(k, "N/A") for k in fieldnames})
    dump_json(base_out / "_all" / "experiment_summary.json", {"experiments": records, "summaries": summaries})
    print(json.dumps({"ablation_results": str(csv_path), "experiments": records}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
