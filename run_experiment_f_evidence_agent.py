"""Experiment F - Evidence-Centric RCA Agent (entry point).

Construction spec: Experiment_F_Prompt.md (v2).

This is deliberately a NEW entry point.  §9 of the spec forbids changing the
global ``PipelineConfig`` defaults or the A~E code path, so F builds its own
config here and calls the shared ``run_all`` unchanged.

Stage layout (built incrementally, one stage per spec §11 step):

    f6_base      F0 base pipeline at 1-minute granularity, episode extents
                 taken from the data instead of being forced to 5 minutes.
                 No GNN, no LLM - pure CPU.  This is step 1: prove it runs and
                 measure the wall-clock cost.

Everything F adds is behind its own config object, so ``--stage f6_base`` is
exactly "C's base stage, re-binned", which is what F6's single-variable
comparison against the already-measured 26.8687 requires.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from aiops.config import PipelineConfig
from aiops.dataset import discover_datasets
from aiops.pipeline import run_all

from aiops.evidence import (
    TableUsage,
    build_log_evidence,
    build_metric_evidence,
    build_quality_evidence,
    build_routing_evidence,
)

#: Tables consumed by the Step-2 evidence modules.  The two flow tables
#: (netflow_5tuple, traffic_flow_metrics) join in spec step 4, so the coverage
#: assertion is scoped to what this stage is supposed to read.
STEP2_TABLES = (
    "node_metrics",
    "interface_metrics",
    "routing_metrics",
    "scrape_health",
    "frr_syslog_events",
)

# ---------------------------------------------------------------------------- F defaults

# Spec 0.4: the data is natively 1-minute (node_metrics 181,352 rows / node /
# 14 days).  The 5 in the shared config is our own quantisation, and it is the
# direct cause of the "median prediction is exactly 5 minutes" defect (D9).
F_BIN_MINUTES = 1
F_EPISODE_MAX_GAP_MINUTES = 1
F_EPISODE_MIN_DURATION_MINUTES = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_cfg(args: argparse.Namespace) -> PipelineConfig:
    """F's own configuration.  Never touches PipelineConfig's defaults."""
    cfg = PipelineConfig(
        workspace=args.workspace,
        output_dir=args.output_dir,
        # Base stage runs with the LLM off; F's LLM stage is a separate stage
        # so it can be scheduled without contending with the A~E chains.
        llm_backend="none",
        bin_minutes=args.bin_minutes,
        episode_max_gap_minutes=args.episode_max_gap_minutes,
        episode_min_duration_minutes=args.episode_min_duration_minutes,
        vae_epochs=args.vae_epochs,
        # F6 must be comparable to C's already-measured base, which had no GNN.
        use_gnn=False,
        experiment_mode="incident",
        max_netflow_rows=args.max_netflow_rows,
        netflow_topology_max_rows=args.netflow_topology_max_rows,
        llm_teacher_workers=args.workers,
        llm_task_a_workers=args.workers,
    )
    return cfg


def _region_filter(regions: str) -> str | None:
    """'all' -> no filter; otherwise the region name is used as a substring."""
    regions = (regions or "").strip()
    if not regions or regions.lower() == "all":
        return None
    return regions


def _peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes.
    return usage / 1024.0 if platform.system() != "Darwin" else usage / (1024.0 * 1024.0)


def stage_f6_base(args: argparse.Namespace) -> int:
    cfg = build_cfg(args)
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)

    print(f"[F6] {_now()} base stage start", flush=True)
    print(f"[F6]   workspace      : {cfg.workspace}", flush=True)
    print(f"[F6]   output_dir     : {cfg.output_dir}", flush=True)
    print(f"[F6]   dataset_filter : {_region_filter(args.regions)!r}", flush=True)
    print(f"[F6]   bin_minutes    : {cfg.bin_minutes}", flush=True)
    print(f"[F6]   episode gap/min: {cfg.episode_max_gap_minutes} / {cfg.episode_min_duration_minutes}",
          flush=True)
    print(f"[F6]   use_gnn        : {cfg.use_gnn}   experiment_mode: {cfg.experiment_mode}", flush=True)

    t0 = time.time()
    try:
        result = run_all(
            cfg.workspace,
            cfg,
            dataset_filter=_region_filter(args.regions),
            use_llm=False,
        )
        rc = 0
        err = None
    except BaseException as exc:  # noqa: BLE001 - we must still write the report
        result = None
        rc = 1
        err = f"{type(exc).__name__}: {exc}"
        print(f"[F6] !! base stage failed: {err}", flush=True)

    elapsed = time.time() - t0
    report = {
        "stage": "f6_base",
        "started_at": args._started_at,
        "finished_at": _now(),
        "elapsed_seconds": round(elapsed, 1),
        "elapsed_human": _hms(elapsed),
        "returncode": rc,
        "error": err,
        "config": {
            "workspace": cfg.workspace,
            "output_dir": cfg.output_dir,
            "regions": args.regions,
            "dataset_filter": _region_filter(args.regions),
            "bin_minutes": cfg.bin_minutes,
            "episode_max_gap_minutes": cfg.episode_max_gap_minutes,
            "episode_min_duration_minutes": cfg.episode_min_duration_minutes,
            "vae_epochs": cfg.vae_epochs,
            "use_gnn": cfg.use_gnn,
            "experiment_mode": cfg.experiment_mode,
            "max_netflow_rows": cfg.max_netflow_rows,
            "netflow_topology_max_rows": cfg.netflow_topology_max_rows,
        },
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "result": result,
    }
    out = Path(cfg.output_dir) / "f6_base_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[F6] {_now()} base stage done rc={rc} in {_hms(elapsed)}", flush=True)
    print(f"[F6] report -> {out}", flush=True)
    return rc


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


# ---------------------------------------------------------------------------- Step 2: evidence modules


def _load_incidents(artifacts_dir: Path, dataset_name: str) -> list[dict]:
    path = artifacts_dir / dataset_name / "incident_candidates.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("incidents") or [])
    return list(data)


def stage_evidence(args: argparse.Namespace) -> int:
    """Step 2: build the section-5 evidence modules over a base run's incidents."""
    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    wanted = _region_filter(args.regions)
    datasets = [
        ds for ds in discover_datasets(args.workspace)
        if wanted is None or wanted in ds.name
    ]
    if not datasets:
        print(f"[FE] no datasets matched regions={args.regions!r} under {args.workspace}", flush=True)
        return 1

    usage = TableUsage()
    summary: dict[str, dict] = {}
    t0 = time.time()

    for ds in datasets:
        incidents = _load_incidents(artifacts, ds.name)
        print(f"[FE] {_now()} {ds.name}: {len(incidents)} incidents", flush=True)
        if not incidents:
            print(f"[FE]   !! no incident_candidates.json under {artifacts / ds.name}; skipping", flush=True)
            continue

        out_dir = out_root / ds.name
        out_dir.mkdir(parents=True, exist_ok=True)

        metric_ev = build_metric_evidence(ds, incidents, usage)
        routing_ev = build_routing_evidence(ds, incidents, usage)
        quality_ev = build_quality_evidence(ds, usage)
        log_ev = build_log_evidence(ds, usage)

        for name, payload in (
            ("metric_evidence.json", metric_ev),
            ("routing_evidence.json", routing_ev),
            ("quality_evidence.json", quality_ev),
            ("log_evidence.json", log_ev),
        ):
            (out_dir / name).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )

        summary[ds.name] = {
            "incidents": len(incidents),
            "metric_evidence_incidents": len(metric_ev.get("incidents", {})),
            "routing_evidence_incidents": len(routing_ev.get("incidents", {})),
            "scrape_zero_up": quality_ev.get("zero_up_count", 0),
            "syslog_events": log_ev.get("event_count", 0),
            "syslog_templates": len(log_ev.get("templates", {})),
            "syslog_programs": list(log_ev.get("programs", {}).keys()),
        }
        print(f"[FE]   {summary[ds.name]}", flush=True)

    usage.save(out_root / "table_usage_report.json", kinds=STEP2_TABLES)
    report = {
        "stage": "evidence",
        "started_at": args._started_at,
        "finished_at": _now(),
        "elapsed_seconds": round(time.time() - t0, 1),
        "artifacts_dir": str(artifacts),
        "table_usage": usage.report(kinds=STEP2_TABLES),
        "datasets": summary,
    }
    (out_root / "evidence_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # Spec 0.5: a table contributing zero rows is a hard failure, not a warning.
    try:
        usage.require_nonzero(STEP2_TABLES)
    except Exception as exc:  # noqa: BLE001
        print(f"[FE] !! {exc}", flush=True)
        return 2

    print(f"[FE] {_now()} evidence done in {_hms(time.time() - t0)}", flush=True)
    print(json.dumps(report["table_usage"], ensure_ascii=False, indent=2, default=str), flush=True)
    return 0


# ---------------------------------------------------------------------------- CLI

#: ``PipelineConfig`` ships a stale Windows default
#: (``C:\Users\Cyber\Downloads\workspace``), so every chain script has to pass
#: ``--workspace`` explicitly.  F defaults to the real data root when it exists
#: so a bare invocation does not silently discover zero datasets.
F_DEFAULT_WORKSPACE = "/202131510121/lyt/workspace"


def _default_workspace() -> str:
    if os.path.isdir(F_DEFAULT_WORKSPACE):
        return F_DEFAULT_WORKSPACE
    return str(PipelineConfig().workspace)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Experiment F - Evidence-Centric RCA Agent.",
    )
    p.add_argument("--workspace", default=_default_workspace())
    p.add_argument("--regions", default="all",
                   help="'all' or a region sub-string, e.g. xian (dev uses one region).")
    p.add_argument("--output-dir", default="outputs_experiment_f_dev")
    p.add_argument("--artifacts-dir", default=None,
                   help="Base-run artifacts root for the evidence stage "
                        "(defaults to --output-dir).")
    p.add_argument("--stage", default="f6_base",
                   choices=["f6_base", "evidence"],
                   help="Which F stage to run.  Built up one step at a time.")
    p.add_argument("--bin-minutes", type=int, default=F_BIN_MINUTES)
    p.add_argument("--episode-max-gap-minutes", type=int, default=F_EPISODE_MAX_GAP_MINUTES)
    p.add_argument("--episode-min-duration-minutes", type=int, default=F_EPISODE_MIN_DURATION_MINUTES)
    p.add_argument("--vae-epochs", type=int, default=40)
    p.add_argument("--max-netflow-rows", type=int, default=250_000)
    p.add_argument("--netflow-topology-max-rows", type=int, default=0)
    # Accepted now so the documented command lines stay valid; the LLM and
    # evidence stages that consume them land in later steps.
    p.add_argument("--llm-base-url", default="http://127.0.0.1:8000")
    p.add_argument("--llm-model", default="deepseek-r1-14b")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--torch-threads", type=int, default=1,
                   help="torch.set_num_threads for the CPU VAE/GNN stages.  Measured on this "
                        "box: 47 ms/batch at 1 thread vs ~1090 ms/batch at the default 32.  "
                        "The model is tiny (hidden=64, batch=512), so the default thread pool "
                        "is pure synchronisation overhead -- 1 thread is ~23x faster.")
    p.add_argument("--coarse-pass-minutes", type=int, default=0,
                   help="Coarse pre-pass granularity (spec 0.4 plan B).  Not implemented yet.")
    p.add_argument("--disable-rag", action="store_true")
    p.add_argument("--disable-predictive", action="store_true")
    p.add_argument("--disable-contradiction", action="store_true")
    args = p.parse_args(argv)
    args._started_at = _now()
    if args.coarse_pass_minutes:
        p.error("--coarse-pass-minutes (spec 0.4 plan B) is not implemented yet; "
                "use --bin-minutes 1 for the faithful run")
    if not args.disable_rag or not args.disable_predictive or not args.disable_contradiction:
        print("note: RAG / predictive / contradiction modules are not implemented yet; "
              "the --disable-* flags are accepted but have no effect.", file=sys.stderr)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if getattr(args, "artifacts_dir", None) is None:
        args.artifacts_dir = args.output_dir
    # Must happen before any stage builds a model.  Kept here (not in
    # aiops/anything) so F cannot change A~E's behaviour.
    if args.torch_threads and args.torch_threads > 0:
        import torch

        torch.set_num_threads(int(args.torch_threads))
        print(f"[F] torch.set_num_threads({args.torch_threads})", flush=True)
    if args.stage == "f6_base":
        return stage_f6_base(args)
    if args.stage == "evidence":
        return stage_evidence(args)
    raise SystemExit(f"unknown stage {args.stage!r}")


if __name__ == "__main__":
    raise SystemExit(main())
