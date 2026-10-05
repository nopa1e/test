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

from ..dataset import canonical_node, discover_datasets, official_network_element_ids
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
        # 官方网元清单共 10 个，而拓扑可达集只有 9 个——``monitor-vm`` 在全部
        # 7 张观测表里出现 0 次（2026-10-05 实测），因此在 adjacency 里孤立，
        # BFS 永远到不了它。但官方 README 明确把 ``monitor-vm`` 列为合法根因，
        # 缺了它就会出现"真值是它、而我们连候选都没有"的**不可恢复盲区**。
        # 故用官方清单补齐到 10 个；补在末尾——它零观测、排序里自然垫底，
        # 不会挤占任何有证据支撑的候选。
        ds_name = str(incident.get("dataset") or "")
        region = ds_name.split("_")[0] if ds_name else ""
        if region:
            for nid in official_network_element_ids(region):
                key = canonical_node(nid)
                if key and key not in seen:
                    seen.append(key)

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
        # 跨区域一致性（空间维）：仅当显式开启且预计算文件存在时启用，
        # 否则 cross_region=None，候选与既有行为逐字节一致。
        cross_region = None
        if getattr(args, "cross_region", False):
            cross_region = _load_json(out_dir / "cross_region_evidence.json")
            if cross_region:
                print(f"[FC] {ds.name}: cross-region ON "
                      f"({len((cross_region.get('roles') or {}))} roles)", flush=True)
            else:
                print(f"[FC] {ds.name}: cross-region 请求但缺 cross_region_evidence.json，"
                      f"回退为关闭", flush=True)
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
            cross_region=cross_region,
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

        # Persist variant A's per-incident ranking.  A is the production prompt
        # (B strips the dependency block, C rewords and reorders), and it is
        # what ``--stage finalize`` consumes.  Without this write the ablation
        # produced statistics only -- 9.5 h of GPU time that could never reach a
        # submission file, because finalize silently fell back to RootScore.
        variant_a = per_variant.get("A") or {}
        if variant_a.get("incidents"):
            (out_dir / "llm_rerank.json").write_text(
                json.dumps(variant_a, ensure_ascii=False), encoding="utf-8"
            )
            print(
                f"[PA]   wrote llm_rerank.json for {len(variant_a['incidents'])} incidents",
                flush=True,
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
            min_predictability=args.min_predictability,
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

#: node_metrics columns grouped by the resource sub-category they imply.  Used
#: only when no routing evidence names the fault: metric evidence covers every
#: incident (554/554 on xian), whereas routing evidence covers a minority, so
#: this is what actually widens category coverage.
#: resource 子类的最弱 z 门槛。低于它视为无可判资源异常，交回上层回退。
_RESOURCE_Z_MIN = 3.0


def _resource_subtype(payload: dict | None) -> tuple[str, float] | None:
    """在该节点的**全部**指标里，选出异常最强的那个 resource 子族。

    为什么要改（实测依据，2026-10-02）
    ---------------------------------
    原实现只扫描 ``top_metrics[:3]``，而 ``load1`` / ``disk_io_util`` /
    ``cpu_usage`` / ``disk_write_rate`` 几乎永远霸占前三，于是
    ``_METRIC_FAMILY`` 里排在后面的子族从不触发：

        指标                        进前三次数   全量出现次数
        cpu_usage                      23543        40324
        disk_io_util                   29252        40482
        load1                          29634        38624
        --- 以下三个子族因此恒为 0 条预测 ---
        memory_available_ratio             1         1763
        process_count                      9         4391
        filesystem_used_ratio              1           38

    结果 resource 只输出 cpu_pressure / disk_io_pressure 两类，
    官方 6 个子类里有 3 个从未出现。改为在全部指标上按 ``peak_z``
    （标准化后的偏离强度，跨指标可比）挑最强子族。
    """
    if not payload:
        return None
    metrics = payload.get("metrics") or {}
    best: tuple[str, float] | None = None
    for sub, cols in _METRIC_FAMILY:
        z = 0.0
        for col in cols:
            stat = metrics.get(col) or {}
            try:
                z = max(z, float(stat.get("peak_z") or 0.0))
            except (TypeError, ValueError):
                continue
        if best is None or z > best[1]:
            best = (sub, z)
    if best and best[1] >= _RESOURCE_Z_MIN:
        return best
    # 向后兼容回退：全部子族都很弱时，沿用旧的「top_metrics 前三个里能对上就取」
    # 行为。缺了这一步，原本能命中 resource 的样本会掉给 base 类别
    # （实测会让 122 条 resource 变成 base 的 link/rate_limit，属回归）。
    for entry in (payload.get("top_metrics") or [])[:3]:
        name = str(entry.get("metric") or "")
        for sub, columns in _METRIC_FAMILY:
            if name in columns:
                return (sub, 0.0)
    return None


_METRIC_FAMILY = (
    ("cpu_pressure", ("cpu_usage", "load1", "load5")),
    ("memory_pressure", ("memory_available_ratio", "swap_used_ratio")),
    ("disk_space_low", ("filesystem_used_ratio", "inode_used_ratio")),
    ("disk_io_pressure", ("disk_io_util", "disk_read_rate", "disk_write_rate")),
    ("process_pressure", ("process_count",)),
)

#: 真协议信号 —— 只有这些 metric 能作为 routing 类别证据。
#: ``bgp_peer_uptime_seconds`` 被刻意排除：它是单调计数器，窗口内时间流逝即必然
#: 冲出基线区间，属钟表效应，不是故障（见 13.11 的实测记录）。
_REAL_ROUTING_METRICS = {
    "bgp_peer_up",
    "bgp_peer_prefix_received",
    "bgp_peer_prefix_sent",
    "bgp_command_success",
    "bgp_peer_count",
    "ipv6_route_nexthop_info",
    "ipv6_route_change_total",
    "ipv6_route_exists",
    "ipv6_route_count",
    "ipv6_default_route_info",
    "ipv6_default_route_changed_total",
    "ospf6_neighbor_state_code",
    "ospf6_interface_cost",
    "ospf6_interface_enabled",
}

#: flow_type x 异常形态 -> judge 的 service 子类（README 的封闭词表）。
_FLOW_ERROR_SUBS = {"web": "web_5xx", "dns": "dns_down", "auth": "auth_error"}
_FLOW_SLOW_SUBS = {"web": "web_slow", "dns": "dns_wrong_record", "auth": "auth_timeout"}

#: flow 异常判定阈值。
_FLOW_ERROR_RATE_MIN = 0.01
_FLOW_TIMEOUT_PER_MIN_MIN = 0.1
_FLOW_SLOW_RATIO = 2.0


def _flow_category(flow_ev: dict | None, iid: str) -> dict | None:
    """service 子类判定，证据来自 traffic_flow_metrics 的跨区业务流。

    web / dns / auth 三种 flow 的 error_rate、timeout 与 p95 延迟，直接对应
    judge 词表里 service 大类的六个子类。取强度最高的那条边。
    """
    inc = ((flow_ev or {}).get("incidents") or {}).get(iid) or {}
    edges = (inc.get("traffic_flow") or {}).get("edges") or []
    best: tuple[float, str] | None = None
    for e in edges:
        ft = str(e.get("flow_type") or "")
        er = e.get("error_rate_incident") or 0.0
        to = e.get("timeout_per_min_incident") or 0.0
        dp = e.get("duration_p95_incident")
        db = e.get("duration_p95_baseline")
        sub = None
        strength = 0.0
        if er > _FLOW_ERROR_RATE_MIN or to > _FLOW_TIMEOUT_PER_MIN_MIN:
            sub = _FLOW_ERROR_SUBS.get(ft)
            strength = float(er) * 10.0 + float(to)
        elif db and dp and float(dp) > _FLOW_SLOW_RATIO * float(db):
            sub = _FLOW_SLOW_SUBS.get(ft)
            strength = float(dp) / float(db)
        if sub and (best is None or strength > best[0]):
            best = (strength, sub)
    if best:
        return {
            "major_category": "service",
            "sub_category": best[1],
            "source": "flow_evidence",
        }
    return None

#: Routing metric hints that map onto a judge sub-category (spec 5.2's table).
#: All of them live under the judge's ``routing`` major category (README).
_ROUTING_SUBS = {
    "bgp_session_down",
    "bgp_route_flap",
    "ospf6_neighbor_down",
    "ospf6_cost_anomaly",
    "blackhole",
    "wrong_default_route",
    "wrong_static_route",
}


#: firewall_cpu_pressure 的判定阈值（见下方注释的实测依据）
_FW_CPU_PEAK_MIN = 10.0
_FW_CPU_Z_MIN = 8.0


def _firewall_pressure(fw_payload: dict | None) -> str | None:
    """fw 节点自身出现资源压力时，返回 firewall 的子类。

    赛题把 ``firewall_cpu_pressure`` 定义为「防火墙资源压力，导致转发性能下降」。
    它模仿的对象恰恰是 service 类故障：防火墙打满 -> 转发变慢 -> 认证/Web 请求
    超时 -> ``_flow_category`` 看到的就是 auth_timeout / web_slow。**唯一能把两者
    分开的观测量，是防火墙网元自己的 CPU**。

    实测依据（2026-10-01，8 区域 node_metrics 全量）：
      fw cpu 中位 0.43~0.77，99 分位 1.8~5.1，而注入尖峰达 50~72；
      尖峰孤立出现（每段 1~5 分钟），与「常态高负载」形态不同。
      因此取 peak>=10 且 z>=8，只有真正的注入尖峰会命中。
    """
    if not fw_payload:
        return None
    cpu = ((fw_payload.get("metrics") or {}).get("cpu_usage")) or {}
    peak, z = cpu.get("incident_peak"), cpu.get("peak_z")
    if peak is None or z is None:
        return None
    try:
        peak = float(peak)
        z = float(z)
    except (TypeError, ValueError):
        return None
    if peak >= _FW_CPU_PEAK_MIN and z >= _FW_CPU_Z_MIN:
        return "cpu_pressure"
    return None


def _firewall_mechanism(fw_payload, routing_events) -> dict | None:
    """fw 节点上的非 CPU 机制子类（acl_drop / rate_limit / default_route_error）。

    官方 firewall 共 6 子类，此前只实现了 cpu_pressure。这里按证据强度补齐：
      acl_drop          <- fw 接口出现丢包/错误（ACL 规则丢弃流量）
      rate_limit        <- fw 接口收发速率相对基线显著下降
      default_route_error <- fw 上出现 ipv6_default_route_* 的变更事件
      port_block / rule_order_error <- 现有 evidence 中无可判据（见文档）
    """
    if fw_payload:
        itfs = fw_payload.get("interfaces") or {}
        drop_z = 0.0
        rate_drop = 0.0
        for _iface, m in itfs.items():
            if not isinstance(m, dict):
                continue
            for k in ("rx_drop_rate", "tx_drop_rate", "rx_error_rate", "tx_error_rate"):
                stat = m.get(k) or {}
                try:
                    drop_z = max(drop_z, float(stat.get("peak_z") or 0.0))
                except (TypeError, ValueError):
                    continue
            for k in ("rx_bytes_rate", "tx_bytes_rate", "rx_packets_rate", "tx_packets_rate"):
                stat = m.get(k) or {}
                try:
                    rate_drop = max(rate_drop, float(stat.get("drop_ratio") or 0.0))
                except (TypeError, ValueError):
                    continue
        if drop_z >= _LINK_DROP_Z_MIN:
            return {"major_category": "firewall", "sub_category": "acl_drop",
                    "source": "interface_evidence:dropz=%.1f" % drop_z}
        # firewall/rate_limit 同 link/rate_limit，因 rate 判据无判别力而撤除。
    for e in (routing_events or []):
        mn = str(e.get("metric_name") or "")
        if mn.startswith("ipv6_default_route"):
            return {"major_category": "firewall", "sub_category": "default_route_error",
                    "source": "routing_evidence:default_route"}
    return None


#: link 子类判据（接口层）。实测依据（2026-10-02，8 区域全量 metric_evidence）：
#:   drops/errors 在 99.9% 分位都是 0，即绝大多数接口零丢包，一旦非零即为强信号；
#:   而 rate 的 drop_ratio 中位 0、90 分位 0.76，过于常见，故 rate_limit 取更高门槛。
_LINK_DROP_Z_MIN = 8.0
_LINK_RATE_DROP_MIN = 0.95


def _link_category(payload):
    """从节点的接口度量判定 link 子类。

    官方 link 三子类：delay / rate_limit / loss。
      loss       <- rx_drop_rate/tx_drop_rate/rx_error_rate/tx_error_rate/carrier_changes
      rate_limit <- 收发字节/包速率相对基线显著下降
      delay      <- 数据里没有任何时延或抖动字段（已核对 interface_metrics 全部 9 列），
                    故该子类不可判，不输出。
    """
    if not payload:
        return None
    itfs = payload.get("interfaces") or {}
    if not itfs:
        return None
    drop_z = 0.0
    rate_drop = 0.0
    for _iface, m in itfs.items():
        if not isinstance(m, dict):
            continue
        for k in ("rx_drop_rate", "tx_drop_rate", "rx_error_rate", "tx_error_rate",
                  "carrier_changes"):
            stat = m.get(k) or {}
            try:
                drop_z = max(drop_z, float(stat.get("peak_z") or 0.0))
            except (TypeError, ValueError):
                continue
        for k in ("rx_bytes_rate", "tx_bytes_rate", "rx_packets_rate", "tx_packets_rate"):
            stat = m.get(k) or {}
            try:
                rate_drop = max(rate_drop, float(stat.get("drop_ratio") or 0.0))
            except (TypeError, ValueError):
                continue
    if drop_z >= _LINK_DROP_Z_MIN:
        return {"major_category": "link", "sub_category": "loss",
                "source": "interface_evidence:dropz=%.1f" % drop_z}
    # rate_limit 分支已撤除：实测 rate 的 drop_ratio 中位 0、90 分位 0.76，
    # 即 10% 的接口在正常波动下就会掉 76% 以上，无法与故障区分。
    # 实测开启它会令 1204 条 resource 被改判成 link/rate_limit，是净损失。
    return None


def _infer_category(
    routing_ev: dict,
    metric_ev: dict,
    iid: str,
    node: str,
    fallback: dict,
    flow_ev: dict | None = None,
) -> dict:
    """Category for one prediction, from the evidence supporting its top candidate.

    Evidence is consulted strongest-first, and every branch only fires when the
    evidence can actually name a category in the judge's closed vocabulary:

    1. **routing** -- metric names map almost one-to-one onto routing
       sub-categories (spec 5.2), so this is the most specific signal available;
    2. **metric** -- node_metrics column names imply a resource sub-category.
       Weaker than routing (a hot CPU may be a symptom), but metric evidence
       covers *every* incident, so it is what widens coverage;
    3. otherwise keep the base run's category -- inventing an unsupported
       sub-category would only add noise, and a wrong one scores zero anyway.
    """
    incident = ((routing_ev or {}).get("incidents") or {}).get(iid) or {}
    events = [e for e in (incident.get("events") or []) if e.get("node") == node]

    metrics = (((metric_ev or {}).get("incidents") or {}).get(iid) or {}).get("nodes") or {}

    # 0. firewall —— 必须排在 routing 之前。
    #    防火墙资源压力会让 BGP/OSPF 邻居超时，routing_evidence 里同样会有事件，
    #    但那是后果而非注入点；fw 自身 CPU 的注入尖峰（z >> 8）才是根因证据。
    fw_payload = metrics.get("fw")
    fw_sub = _firewall_pressure(fw_payload)
    if fw_sub:
        return {
            "major_category": "firewall",
            "sub_category": fw_sub,
            "source": "metric_evidence:fw",
        }
    fw_mech = _firewall_mechanism(fw_payload, events)
    if fw_mech:
        return fw_mech

    # 1. routing —— 只承认真协议信号。``bgp_peer_uptime_seconds`` 的钟表信号不算，
    #    否则它恒真，会把后面所有分支都吃掉（这正是 F 只输出两个大类的原因）。
    for event in events:
        if str(event.get("metric_name")) not in _REAL_ROUTING_METRICS:
            continue
        sub = event.get("hints_sub_category")
        if sub in _ROUTING_SUBS:
            return {
                "major_category": "routing",
                "sub_category": sub,
                "source": "routing_evidence:real",
            }

    # 2. service —— traffic_flow_metrics 给出的 web/dns/auth 业务异常。
    svc = _flow_category(flow_ev, iid)
    if svc:
        return svc

    # 3. routing 钟表信号 —— 保留原有行为。它零信息但在统计上偏向真实高频根因
    #    （13.12 实测：削弱它掉 1.048 分），故只在 service 无证据时才回退到它。
    for event in events:
        sub = event.get("hints_sub_category")
        if sub in _ROUTING_SUBS:
            return {"major_category": "routing", "sub_category": sub, "source": "routing_evidence"}

    # 3.5 link —— 接口层证据。置于钟表信号之后：钟表是零信息统计先验，
    #     接口丢包是真实观测但覆盖极窄（全量仅 1 条 z>=8），故只接管
    #     钟表接不住的样本，不去抢已有归属。
    link_res = _link_category(metrics.get(node))
    if link_res:
        return link_res

    payload = metrics.get(node) or {}
    picked = _resource_subtype(payload)
    if picked:
        return {
            "major_category": "resource",
            "sub_category": picked[0],
            "source": f"metric_evidence:z={picked[1]:.1f}",
        }

    return {**fallback, "source": "base"}

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
        routing_ev = _load_json(out_root / ds.name / "routing_evidence.json")
        metric_ev = _load_json(out_root / ds.name / "metric_evidence.json")
        flow_ev = _load_json(out_root / ds.name / "flow_evidence.json")
        # ``--no-llm-rerank`` forces the programmatic RootScore order, which is
        # the production path: the LLM re-rank cost a submission and did not pay
        # for itself, so it must never be picked up by accident when present.
        rerank = (
            {}
            if getattr(args, "no_llm_rerank", False)
            else _load_json(out_root / ds.name / "llm_rerank.json")
        )
        region = getattr(ds, "region_code", None) or ds.name.split("_", 1)[0]

        records: list[dict] = []
        used_llm = 0
        used_evidence_category = 0
        category_sources: dict[str, int] = {}
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
            inferred = _infer_category(
                routing_ev, metric_ev, iid, seen[0] if seen else "", category, flow_ev
            )
            src = inferred.get("source", "base")
            category_sources[src] = category_sources.get(src, 0) + 1
            if src != "base":
                used_evidence_category += 1
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
                        "major_category": inferred["major_category"],
                        "sub_category": inferred["sub_category"],
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
            "records_with_evidence_category": used_evidence_category,
            "category_source_counts": dict(category_sources),
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

def stage_g_discriminator(args: argparse.Namespace) -> int:
    """Experiment G: second-layer "is this a real fault" discriminator (方案 §14).

    Fits on evidence-structure features the pipeline already produced -- no new
    data reads, no LLM -- and audits the unsupervised score against cross-modal
    agreement.  The audit is what decides whether weighting RootScore with it
    could help; the module never deletes a prediction (spec 1.3: cutting rows is
    worth 0~1 point in practice).
    """
    from .discriminator import run_discriminator

    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    datasets = _matched(args)
    if not datasets:
        print(f"[G] no datasets matched regions={args.regions!r}", flush=True)
        return 1

    summary: dict[str, dict] = {}
    started = time.time()
    for ds in datasets:
        incidents = _load_incidents(artifacts, ds.name)
        if not incidents:
            print(f"[G] {ds.name}: no incidents, skipping", flush=True)
            continue
        out_dir = out_root / ds.name
        candidates = _load_json(out_dir / "candidates.json")
        if not candidates:
            print(f"[G] {ds.name}: needs --stage candidates first", flush=True)
            continue
        bundle = _evidence_bundle(out_dir)
        bundle["temporal_ev"] = _load_json(out_dir / "temporal_evidence.json")
        bundle["predictive_ev"] = _load_json(out_dir / "predictive_evidence.json")
        bundle["contradiction_ev"] = _load_json(out_dir / "contradiction_evidence.json")

        result = run_discriminator(ds, incidents, candidates, seed=args.seed, **bundle)
        (out_dir / "discriminator.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        summary_data = result.get("summary") or {}
        hard = result.get("hard_samples") or []
        knowledge = result.get("knowledge") or []
        summary[ds.name] = {
            **summary_data,
            "hard_samples_returned": len(hard),
            "knowledge_entries": len(knowledge),
        }
        print(
            f"[G] {ds.name.split('_')[0]}: n={summary_data.get('n')} "
            f"hard={summary_data.get('n_hard')} ({summary_data.get('hard_ratio')}) "
            f"knowledge={len(knowledge)} "
            f"r(anomaly,root)={summary_data.get('corr_anomaly_with_root_score')}",
            flush=True,
        )

    (out_root / "discriminator_report.json").write_text(
        json.dumps(
            {"stage": "g_discriminator", "finished_at": _now(), "datasets": summary},
            ensure_ascii=False, indent=2, default=str,
        ),
        encoding="utf-8",
    )
    print(f"[G] {_now()} done in {_hms(time.time() - started)}", flush=True)
    return 0