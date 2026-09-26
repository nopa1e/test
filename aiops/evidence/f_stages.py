"""F's later stages (spec 5.9 and section 4), kept out of the entry point.

``run_experiment_f_evidence_agent.py`` also hosts the F6 base stage, which
pulls in the whole base pipeline.  Putting the candidate and F1 stages in their
own module keeps that file small, avoids a circular import (the base pipeline
never needs these), and lets the stages be imported in a REPL or a test without
starting the VAE.

Both stages are pure CPU: they run while the GPUs are busy with someone else's
job, and their outputs are what the LLM stage later re-ranks.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ..dataset import canonical_node, discover_datasets
from ..utils import get_logger
from .candidate_generator import build_candidates
from .flow_evidence import build_flow_evidence
from .metrics_framework import run_stability
from .prompt_ablation import (
    deterministic_first_movers,
    diagnose,
    run_prompt_ablation,
    summarise_ablation,
    summarise_agreement,
)
from .temporal_evidence import build_temporal_evidence

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hms(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h{minutes:02d}m{secs:02d}s"


def _load_json(path: Path) -> dict:
    """Read a JSON artifact; a missing or corrupt file means 'not built yet'."""
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("unreadable JSON artifact: %s", path)
        return {}


def _load_incidents(artifacts_dir: Path, dataset_name: str) -> list[dict]:
    path = artifacts_dir / dataset_name / "incident_candidates.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("incidents") or [])
    return list(data)


def _matched(args: argparse.Namespace):
    wanted = (args.regions or "").strip()
    wanted = None if not wanted or wanted.lower() == "all" else wanted
    return [
        ds for ds in discover_datasets(args.workspace)
        if wanted is None or wanted in ds.name
    ]


def _evidence_bundle(out_dir: Path) -> dict:
    """The Step-2a payloads, plus flow evidence once step 4 has produced it."""
    return {
        "metric_ev": _load_json(out_dir / "metric_evidence.json"),
        "routing_ev": _load_json(out_dir / "routing_evidence.json"),
        "log_ev": _load_json(out_dir / "log_evidence.json"),
        "quality_ev": _load_json(out_dir / "quality_evidence.json"),
        "flow_ev": _load_json(out_dir / "flow_evidence.json"),
    }


def _load_adjacency(out_dir: Path) -> dict[str, set[str]]:
    """Undirected adjacency from the base run's ``topology.json``.

    Used to expand a one-element incident into a real candidate set (see the
    note in ``candidate_generator``).  Edges are treated as undirected because
    a fault propagates both ways along a link; direction is handled later by
    the predictive evidence, which is what decides cause vs victim.
    """
    topology = _load_json(out_dir / "topology.json")
    adjacency: dict[str, set[str]] = {}
    for edge in topology.get("edges") or []:
        source, target = edge.get("source"), edge.get("target")
        if not source or not target or source == target:
            continue
        # topology.json stores network_element_id ("xian-br-1") while the
        # evidence modules work on canonical node names ("br-1"); without this
        # normalisation the adjacency lookup silently returns nothing and the
        # candidate set stays at one element.
        left, right = canonical_node(source), canonical_node(target)
        if not left or not right or left == right:
            continue
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    return adjacency

def load_adjacency(dataset_artifacts_dir: Path) -> dict[str, set[str]]:
    """Public wrapper around the topology adjacency loader."""
    return _load_adjacency(dataset_artifacts_dir)


def expand_incidents_with_topology(
    incidents: list[dict],
    adjacency: dict[str, set[str]],
    *,
    max_nodes: int = 10,
) -> list[dict]:
    """Grow each incident's node set with topology neighbours (F's own step).

    Measured on xian: **all 554 incidents carry exactly one network element**.
    Every evidence module iterates ``incident["nodes"]``, so incident-scoped
    evidence is single-node evidence -- and spec 5.9's ranking has nothing to
    rank.  ``max_nodes`` matches ``build_metric_evidence``'s own default
    (spec 5.1), i.e. the capacity was always there; the upstream clustering
    simply never filled it.

    The seed elements are preserved under ``seed_nodes`` so the expansion is
    auditable and reversible.
    """
    if not adjacency:
        return incidents

    expanded: list[dict] = []
    for incident in incidents:
        seeds = [canonical_node(n) for n in (incident.get("nodes") or []) if n]
        seeds = list(dict.fromkeys(n for n in seeds if n))
        seen = list(seeds)
        frontier = list(seeds)
        while frontier and len(seen) < max_nodes:
            nxt: list[str] = []
            for node in frontier:
                for neighbour in sorted(adjacency.get(node, ())):
                    if neighbour in seen:
                        continue
                    seen.append(neighbour)
                    nxt.append(neighbour)
                    if len(seen) >= max_nodes:
                        break
                if len(seen) >= max_nodes:
                    break
            frontier = nxt
        clone = dict(incident)
        clone["seed_nodes"] = seeds
        clone["nodes"] = seen
        clone["node_count"] = len(seen)
        expanded.append(clone)
    return expanded

def stage_candidates(args: argparse.Namespace) -> int:
    """Spec 5.9: candidates + RootScore, produced entirely by programs.

    The LLM is only ever allowed to re-rank this list, so everything the
    ranking depends on is written to disk here: per-candidate sub-scores, the
    weighted total, and the supporting / contradicting split.
    """
    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    datasets = _matched(args)
    if not datasets:
        print(f"[FC] no datasets matched regions={args.regions!r} under {args.workspace}", flush=True)
        return 1

    summary: dict[str, dict] = {}
    started = time.time()
    for ds in datasets:
        incidents = _load_incidents(artifacts, ds.name)
        if not incidents:
            print(f"[FC] {ds.name}: no incident_candidates.json, skipping", flush=True)
            continue
        out_dir = out_root / ds.name
        out_dir.mkdir(parents=True, exist_ok=True)
        bundle = _evidence_bundle(out_dir)
        # Consumed by the candidate generator to fill the two RootScore terms
        # that are otherwise structurally zero (spec 5.9).
        bundle["predictive_ev"] = _load_json(out_dir / "predictive_evidence.json")
        bundle["contradiction_ev"] = _load_json(out_dir / "contradiction_evidence.json")
        if not bundle["metric_ev"]:
            print(f"[FC] {ds.name}: no evidence yet -- run --stage evidence first", flush=True)
            continue

        temporal_ev = build_temporal_evidence(
            ds,
            incidents,
            metric_ev=bundle["metric_ev"],
            routing_ev=bundle["routing_ev"],
            log_ev=bundle["log_ev"],
            quality_ev=bundle["quality_ev"],
        )
        (out_dir / "temporal_evidence.json").write_text(
            json.dumps(temporal_ev, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

        adjacency = _load_adjacency(out_dir)
        candidates = build_candidates(
            ds, incidents, temporal_ev=temporal_ev, top_k=args.top_k,
            adjacency=adjacency, **bundle
        )
        (out_dir / "candidates.json").write_text(
            json.dumps(candidates, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

        per_incident = candidates.get("incidents") or {}
        scored = [v for v in per_incident.values() if v.get("candidates")]
        summary[ds.name] = {
            "incidents": len(incidents),
            "with_candidates": len(per_incident),
            "with_timing": len(temporal_ev.get("incidents") or {}),
            "mean_candidates": round(
                sum(v.get("n_candidates", 0) for v in per_incident.values())
                / max(1, len(per_incident)),
                2,
            ),
            "mean_top1_score": round(
                sum((v.get("candidates") or [{}])[0].get("root_score", 0.0) for v in scored)
                / max(1, len(scored)),
                4,
            ),
        }
        print(f"[FC] {ds.name}: {summary[ds.name]}", flush=True)

    report = {
        "stage": "candidates",
        "started_at": getattr(args, "_started_at", _now()),
        "finished_at": _now(),
        "elapsed_seconds": round(time.time() - started, 1),
        "elapsed_human": _hms(time.time() - started),
        "datasets": summary,
    }
    (out_root / "candidates_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"[FC] {_now()} done in {_hms(time.time() - started)}", flush=True)
    return 0


def stage_f1_stability(args: argparse.Namespace) -> int:
    """Spec 4.1: perturb the evidence, repeat, and report ranking stability.

    The point of measuring this before adding more evidence: if the ranking is
    dominated by noise, no amount of extra evidence (or prompt tuning) can pay
    off, and the spec's own diagnostic table says so.
    """
    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    metrics_dir = Path(args.metrics_dir) if args.metrics_dir else out_root / "f1_metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    datasets = _matched(args)
    if not datasets:
        print(f"[F1] no datasets matched regions={args.regions!r}", flush=True)
        return 1

    per_dataset: dict[str, dict] = {}
    started = time.time()
    for ds in datasets:
        incidents = _load_incidents(artifacts, ds.name)
        if not incidents:
            print(f"[F1] {ds.name}: no incidents, skipping", flush=True)
            continue
        out_dir = out_root / ds.name
        bundle = _evidence_bundle(out_dir)
        bundle["temporal_ev"] = _load_json(out_dir / "temporal_evidence.json")
        # Same evidence set the candidate stage consumes.  Without these two the
        # stability test would perturb a *different* ranking than the one the
        # pipeline actually produces (contradiction carries weight -0.15), and
        # its verdict would not transfer to the real candidates.
        bundle["predictive_ev"] = _load_json(out_dir / "predictive_evidence.json")
        bundle["contradiction_ev"] = _load_json(out_dir / "contradiction_evidence.json")
        if not bundle["metric_ev"]:
            print(f"[F1] {ds.name}: no evidence yet, skipping", flush=True)
            continue

        report = run_stability(
            ds,
            incidents,
            bundle,
            n_repeats=args.n_repeats,
            drop_fraction=args.drop_fraction,
            top_k=args.top_k,
        )
        per_dataset[ds.name] = report
        print(f"[F1] {ds.name}: {report['summary']}", flush=True)

    payload = {
        "stage": "f1_stability",
        "started_at": getattr(args, "_started_at", _now()),
        "finished_at": _now(),
        "elapsed_seconds": round(time.time() - started, 1),
        "settings": {
            "n_repeats": args.n_repeats,
            "drop_fraction": args.drop_fraction,
            "top_k": args.top_k,
        },
        "datasets": per_dataset,
    }
    out = metrics_dir / "stability.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[F1] {_now()} done in {_hms(time.time() - started)} -> {out}", flush=True)
    return 0


def stage_prompt_ablation(args: argparse.Namespace) -> int:
    """Spec 4.2: three prompt variants, then the two agreement numbers.

    Requires the LLM, so it runs once the vLLM server is up.  Writes
    ``prompt_ablation.json`` and ``evidence_agreement.json`` next to the
    stability report (the layout spec 4.3 asks for).
    """
    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    metrics_dir = Path(args.metrics_dir) if args.metrics_dir else out_root / "f1_metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    datasets = _matched(args)
    if not datasets:
        print(f"[PA] no datasets matched regions={args.regions!r}", flush=True)
        return 1

    summary: dict[str, dict] = {}
    started = time.time()
    for ds in datasets:
        incidents = _load_incidents(artifacts, ds.name)
        out_dir = out_root / ds.name
        candidates = _load_json(out_dir / "candidates.json")
        if not incidents or not candidates:
            print(f"[PA] {ds.name}: needs --stage candidates output first", flush=True)
            continue

        temporal_ev = _load_json(out_dir / "temporal_evidence.json")
        routing_ev = _load_json(out_dir / "routing_evidence.json")
        quality_ev = _load_json(out_dir / "quality_evidence.json")
        region = ds.name.split("_", 1)[0]

        per_variant = run_prompt_ablation(
            incidents,
            candidates,
            base_url=args.llm_base_url,
            model=args.llm_model,
            workers=args.workers,
            region=region,
            limit=args.ablation_limit,
        )
        deterministic = {
            str(incident.get("incident_id")): deterministic_first_movers(
                str(incident.get("incident_id")),
                temporal_ev=temporal_ev,
                routing_ev=routing_ev,
                quality_ev=quality_ev,
            )
            for incident in incidents
        }
        ablation = summarise_ablation(per_variant)
        agreement = summarise_agreement(per_variant, deterministic)
        verdict = diagnose(ablation, agreement)
        summary[ds.name] = {
            "ablation": ablation,
            "agreement": agreement,
            "verdict": verdict,
            "variant_summaries": {
                name: (result.get("summary") or {}) for name, result in per_variant.items()
            },
        }
        print(
            f"[PA] {ds.name}: rank1_unanimous={ablation.get('rank1_unanimous_rate')} "
            f"-> {verdict['verdict']}",
            flush=True,
        )

    (metrics_dir / "prompt_ablation.json").write_text(
        json.dumps(
            {"stage": "prompt_ablation", "started_at": getattr(args, "_started_at", _now()),
             "finished_at": _now(), "datasets": summary},
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    (metrics_dir / "evidence_agreement.json").write_text(
        json.dumps(
            {
                "stage": "evidence_agreement",
                "datasets": {name: entry["agreement"] for name, entry in summary.items()},
                "verdicts": {name: entry["verdict"] for name, entry in summary.items()},
            },
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"[PA] {_now()} done in {_hms(time.time() - started)} -> {metrics_dir}", flush=True)
    return 0

def stage_flow(args: argparse.Namespace) -> int:
    """Spec 5.5 (step 4): probe and netflow evidence.

    Kept out of the Step-2a evidence stage on purpose.  These are the two
    heaviest tables in the dataset (netflow alone is 2.7 GB per region), the
    scan is streaming and row-limited, and the payload for one full region
    reaches roughly 160 MiB -- so it is written **compact** (no ``indent``).
    """
    from .table_usage import TableUsage

    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    datasets = _matched(args)
    if not datasets:
        print(f"[FF] no datasets matched regions={args.regions!r}", flush=True)
        return 1

    usage = TableUsage()
    summary: dict[str, dict] = {}
    started = time.time()
    for ds in datasets:
        incidents = _load_incidents(artifacts, ds.name)
        if not incidents:
            print(f"[FF] {ds.name}: no incident_candidates.json, skipping", flush=True)
            continue
        out_dir = out_root / ds.name
        out_dir.mkdir(parents=True, exist_ok=True)

        incidents = expand_incidents_with_topology(incidents, _load_adjacency(out_dir))
        t0 = time.time()
        flow_ev = build_flow_evidence(ds, incidents, usage, max_netflow_rows=args.max_netflow_rows)
        out = out_dir / "flow_evidence.json"
        out.write_text(json.dumps(flow_ev, ensure_ascii=False), encoding="utf-8")

        netflow = flow_ev.get("netflow_5tuple") or {}
        traffic = flow_ev.get("traffic_flow_metrics") or {}
        summary[ds.name] = {
            "incidents": len(incidents),
            "incidents_with_edges": flow_ev.get("incident_count"),
            "traffic_edges": traffic.get("edges"),
            "traffic_flow_types": traffic.get("flow_types"),
            "netflow_mode": netflow.get("mode"),
            "netflow_rows_scanned": netflow.get("rows_scanned"),
            "netflow_rows_kept": netflow.get("rows_kept"),
            "netflow_edges": netflow.get("edges"),
            "truncated": netflow.get("truncated"),
            "json_mb": round(out.stat().st_size / 1e6, 1),
            "seconds": round(time.time() - t0, 1),
            "warnings": flow_ev.get("warnings"),
        }
        print(f"[FF] {ds.name}: {summary[ds.name]}", flush=True)

    kinds = ("netflow_5tuple", "traffic_flow_metrics")
    usage.save(out_root / "table_usage_flow.json", kinds=kinds)
    (out_root / "flow_report.json").write_text(
        json.dumps(
            {
                "stage": "flow",
                "started_at": getattr(args, "_started_at", _now()),
                "finished_at": _now(),
                "elapsed_human": _hms(time.time() - started),
                "datasets": summary,
                "table_usage": usage.report(kinds=kinds),
            },
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )

    # Spec 0.5: a table that contributed zero rows is a hard failure.
    try:
        usage.require_nonzero(kinds)
    except Exception as exc:  # noqa: BLE001 - surfaced as a non-zero exit
        print(f"[FF] !! {exc}", flush=True)
        return 2

    print(f"[FF] {_now()} done in {_hms(time.time() - started)}", flush=True)
    return 0

def stage_predictive(args: argparse.Namespace) -> int:
    """Spec 5.7 + 5.8: predictive (cause vs victim) and contradiction evidence.

    Both need the entity graph, so they run after the base stage; and both are
    consumed by the candidate generator, so they run **before** ``--stage
    candidates``.  Neither touches the LLM.  Until they run, two of the seven
    RootScore terms (``predictive_explanation`` +0.15, ``contradiction`` -0.15)
    are structurally zero.
    """
    from .contradiction import build_contradiction_evidence
    from .predictive_evidence import build_predictive_evidence
    from .table_usage import TableUsage

    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    datasets = _matched(args)
    if not datasets:
        print(f"[FP] no datasets matched regions={args.regions!r}", flush=True)
        return 1

    summary: dict[str, dict] = {}
    started = time.time()
    for ds in datasets:
        incidents = _load_incidents(artifacts, ds.name)
        if not incidents:
            print(f"[FP] {ds.name}: no incidents, skipping", flush=True)
            continue
        out_dir = out_root / ds.name
        out_dir.mkdir(parents=True, exist_ok=True)
        adjacency = _load_adjacency(out_dir)
        # Same expansion the evidence stage applies.  Incidents arrive with a
        # single network element, and a one-node pair space has nothing to
        # relate -- which is exactly why the predictive module emitted zero
        # incidents on the first attempt.
        incidents = expand_incidents_with_topology(incidents, adjacency)
        bundle = _evidence_bundle(out_dir)
        temporal_ev = _load_json(out_dir / "temporal_evidence.json")
        if not temporal_ev:
            # Ordering trap: the pipeline runs predictive *before* candidates,
            # and only the candidate stage writes temporal_evidence.json.  On a
            # fresh run the contradiction module therefore saw an empty
            # timeline and emitted zero incidents (observed: guangzhou 0/561,
            # nanjing 0/464, versus 554/554 when candidates ran first).
            # Derive it here from the same evidence payloads instead.
            temporal_ev = build_temporal_evidence(
                ds,
                incidents,
                metric_ev=bundle["metric_ev"],
                routing_ev=bundle["routing_ev"],
                log_ev=bundle["log_ev"],
                quality_ev=bundle["quality_ev"],
            )
            (out_dir / "temporal_evidence.json").write_text(
                json.dumps(temporal_ev, ensure_ascii=False), encoding="utf-8"
            )
            print(
                f"[FP]   derived temporal evidence on the fly: "
                f"{len(temporal_ev.get('incidents') or {})} incidents",
                flush=True,
            )

        usage = TableUsage()
        t0 = time.time()
        predictive = build_predictive_evidence(
            ds,
            incidents,
            usage,
            adjacency=adjacency or None,
            max_lag_minutes=args.max_lag_minutes,
        )
        (out_dir / "predictive_evidence.json").write_text(
            json.dumps(predictive, ensure_ascii=False), encoding="utf-8"
        )
        t_pred = time.time() - t0

        t0 = time.time()
        contradiction = build_contradiction_evidence(
            ds,
            incidents,
            metric_ev=bundle["metric_ev"],
            routing_ev=bundle["routing_ev"],
            log_ev=bundle["log_ev"],
            temporal_ev=temporal_ev,
            predictive_ev=predictive,
        )
        (out_dir / "contradiction_evidence.json").write_text(
            json.dumps(contradiction, ensure_ascii=False), encoding="utf-8"
        )

        marker = contradiction.get("no_counter_evidence_marker")
        pairs = sum(len(v.get("pairs") or []) for v in (predictive.get("incidents") or {}).values())
        with_counter = sum(
            1
            for entry in (contradiction.get("incidents") or {}).values()
            for node in (entry.get("nodes") or {}).values()
            if node.get("contradicting") and node["contradicting"][0] != marker
        )
        summary[ds.name] = {
            "incidents": len(incidents),
            "predictive_incidents": len(predictive.get("incidents") or {}),
            "predictive_pairs": pairs,
            "predictive_seconds": round(t_pred, 1),
            "contradiction_incidents": len(contradiction.get("incidents") or {}),
            "candidates_with_counter_evidence": with_counter,
            "contradiction_seconds": round(time.time() - t0, 1),
        }
        print(f"[FP] {ds.name}: {summary[ds.name]}", flush=True)

    (out_root / "predictive_report.json").write_text(
        json.dumps(
            {"stage": "predictive", "finished_at": _now(), "datasets": summary},
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"[FP] {_now()} done in {_hms(time.time() - started)}", flush=True)
    return 0

def stage_finalize(args: argparse.Namespace) -> int:
    """Write submission-format JSONL in the judge's own schema.

    Ordering is F's contribution; the fault category is carried over from the
    base run's ``prediction.json`` so this stage deliberately changes exactly
    one variable (spec 1.5: single-variable comparisons only).  Network
    elements are emitted as ``"<region>-<node_id>"`` and ``root_cause_top5``
    ranks start at 1 with no duplicates, per the judge README.

    If an LLM re-rank has been produced (``llm_rerank.json``) it is used;
    otherwise the programmatic RootScore order is written, which needs no LLM
    and is therefore available immediately.
    """
    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    final_dir = Path(args.final_dir) if args.final_dir else out_root / "final"
    final_dir.mkdir(parents=True, exist_ok=True)

    datasets = _matched(args)
    if not datasets:
        print(f"[FIN] no datasets matched regions={args.regions!r}", flush=True)
        return 1

    merged: list[dict] = []
    summary: dict[str, dict] = {}
    for ds in datasets:
        incidents = _load_incidents(artifacts, ds.name)
        if not incidents:
            print(f"[FIN] {ds.name}: no incidents, skipping", flush=True)
            continue
        candidates = _load_json(out_root / ds.name / "candidates.json")
        base_pred = _load_json(artifacts / ds.name / "prediction.json")
        category = base_pred.get("fault_category") or {}
        rerank = _load_json(out_root / ds.name / "llm_rerank.json")
        region = getattr(ds, "region_code", None) or ds.name.split("_", 1)[0]

        records: list[dict] = []
        used_llm = 0
        for incident in incidents:
            iid = str(incident.get("incident_id"))
            payload = (candidates.get("incidents") or {}).get(iid) or {}
            from_llm = ((rerank.get("incidents") or {}).get(iid) or {}).get("llm_ranking") or []
            order = from_llm or payload.get("top5") or payload.get("top10") or []
            if not order:
                continue
            used_llm += 1 if from_llm else 0
            tr = incident.get("time_range") or {}
            # Deduplicate while keeping order: the judge forbids repeats.
            seen: list[str] = []
            for node in order:
                if node not in seen:
                    seen.append(node)
            records.append(
                {
                    "prediction_id": f"f_{iid}",
                    "start_time": tr.get("start"),
                    "end_time": tr.get("end"),
                    "root_cause_top5": [
                        {"rank": rank, "network_element_id": f"{region}-{node}"}
                        for rank, node in enumerate(seen[:5], start=1)
                    ],
                    "fault_category": {
                        "major_category": category.get("major_category", ""),
                        "sub_category": category.get("sub_category", ""),
                    },
                }
            )

        out = final_dir / f"predictions_{region}.jsonl"
        out.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
        )
        merged.extend(records)
        summary[ds.name] = {
            "records": len(records),
            "file": str(out),
            "records_from_llm_rerank": used_llm,
            "category": category,
        }
        print(f"[FIN] {ds.name}: {len(records)} records (llm={used_llm}) -> {out.name}", flush=True)

    merged_path = final_dir / "predictions_f_all.jsonl"
    merged_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in merged), encoding="utf-8"
    )
    (final_dir / "finalize_report.json").write_text(
        json.dumps(
            {
                "stage": "finalize",
                "finished_at": _now(),
                "total_records": len(merged),
                "merged_file": str(merged_path),
                "datasets": summary,
            },
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"[FIN] total {len(merged)} records -> {merged_path}", flush=True)
    return 0