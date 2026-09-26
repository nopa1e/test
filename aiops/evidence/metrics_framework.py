"""F1 -- the local measurement framework (spec section 4).

Spec section 4 exists because of one uncomfortable measurement: B and C differ
by 3.4% overall while their dominant fault categories differ completely, and
rewriting the prompt flips ``resource`` 58% into ``routing`` 47% **without
moving the score at all** (spec 1.1).  A system whose answers are driven by the
prompt rather than by the data cannot be improved by adding evidence, so F1 has
to come first: without a local, label-free way to ask "does changing the
evidence change the conclusion", every later module is a bet.

    existing   no local metric whatsoever -- the only feedback was the official
               score, which is scarce (5 submissions/day) and explicitly
               forbidden as an objective (spec 1.5-5)
    F adds     a rank-stability distribution under evidence perturbation, plus
               an agreement rate between the ranking and deterministic
               evidence -- both computable without ground truth (F-3)

This file covers 4.1 (stability) and the local half of 4.2 (agreement).  The
prompt-ablation half of 4.2 needs the LLM; it lives in ``prompt_ablation.py``
so this part can run on CPU while the GPUs are busy with someone else's job.

Perturbation is applied at the **evidence** layer, not by re-reading the raw
tables: re-deriving metric evidence from 1.2M interface rows twenty times is
hours of I/O for a statistic whose purpose is to detect ranking noise.  The
dropped fraction and the layer are both recorded in the report so the
approximation is never mistaken for the table-level version spec 4.1 sketches.
"""

from __future__ import annotations

import random
from typing import Any

from ..dataset import DatasetInfo
from ..utils import get_logger
from .candidate_generator import build_candidates

log = get_logger(__name__)

#: The evidence payloads that can be perturbed, i.e. every module whose output
#: is a list of per-node observations.
PERTURBABLE = ("metric_ev", "routing_ev", "log_ev", "quality_ev", "flow_ev")

#: A node that does not appear in a run is ranked below every observed node.
#: Using ``top_k + 1`` rather than ``None`` keeps ``mean_rank``/``rank_std``
#: finite, which is what makes them comparable across runs.
_MISSING_RANK_PENALTY = 1


def _subsample(items: list, fraction: float, rng: random.Random) -> list:
    """Keep ``1 - fraction`` of ``items`` at random, order preserved."""
    if not items or fraction <= 0.0:
        return list(items)
    keep = max(1, int(round(len(items) * (1.0 - fraction))))
    if keep >= len(items):
        return list(items)
    return [items[i] for i in sorted(rng.sample(range(len(items)), keep))]


def _perturb_incident_map(incidents: dict, fraction: float, rng: random.Random) -> dict:
    """Drop a fraction of the observation lists inside an evidence payload."""
    out: dict[str, Any] = {}
    for iid, payload in (incidents or {}).items():
        if not isinstance(payload, dict):
            out[iid] = payload
            continue
        clone = dict(payload)
        if isinstance(clone.get("events"), list):
            clone["events"] = _subsample(clone["events"], fraction, rng)
        if isinstance(clone.get("nodes"), dict):
            nodes = {}
            for node, node_payload in clone["nodes"].items():
                if not isinstance(node_payload, dict):
                    nodes[node] = node_payload
                    continue
                node_clone = dict(node_payload)
                if isinstance(node_clone.get("top_metrics"), list):
                    node_clone["top_metrics"] = _subsample(node_clone["top_metrics"], fraction, rng)
                if isinstance(node_clone.get("metrics"), dict):
                    node_clone["metrics"] = {
                        k: v for k, v in node_clone["metrics"].items()
                        if rng.random() >= fraction
                    }
                if isinstance(node_clone.get("interfaces"), dict):
                    node_clone["interfaces"] = {
                        k: ({kk: vv for kk, vv in v.items() if rng.random() >= fraction}
                            if isinstance(v, dict) else v)
                        for k, v in node_clone["interfaces"].items()
                    }
                nodes[node] = node_clone
            clone["nodes"] = nodes
        if isinstance(clone.get("edges"), list):
            clone["edges"] = _subsample(clone["edges"], fraction, rng)
        out[iid] = clone
    return out


def perturb_bundle(bundle: dict, fraction: float, rng: random.Random) -> dict:
    """Return a copy of *bundle* with a fraction of its evidence dropped.

    Three layers are perturbed independently, mirroring spec 4.1's "delete 5%
    of rows in each family": per-incident observation lists, the per-incident
    node maps, and the dataset-wide point lists (``zero_up_points``, ``events``).
    """
    out: dict[str, Any] = {}
    for key, value in bundle.items():
        if key not in PERTURBABLE or not isinstance(value, dict):
            out[key] = value
            continue
        clone = dict(value)
        clone["incidents"] = _perturb_incident_map(value.get("incidents") or {}, fraction, rng)
        for flat in ("zero_up_points", "events", "interrupt_windows"):
            if isinstance(clone.get(flat), list):
                clone[flat] = _subsample(clone[flat], fraction, rng)
        out[key] = clone
    return out


def rank_incidents(
    ds: DatasetInfo,
    incidents: list[dict],
    bundle: dict,
    *,
    adjacency=None,
    top_k: int = 10,
    filter_k: int = 5,
) -> dict[str, dict]:
    """One ranking pass: incident id -> ``{"top10", "top5", "root_scores"}``."""
    result = build_candidates(
        ds,
        incidents,
        adjacency=adjacency,
        top_k=top_k,
        filter_k=filter_k,
        **{key: bundle.get(key) for key in
           ("metric_ev", "routing_ev", "log_ev", "quality_ev", "temporal_ev",
            "flow_ev", "predictive_ev", "contradiction_ev")},
    )
    per_incident: dict[str, dict] = {}
    for iid, payload in (result.get("incidents") or {}).items():
        per_incident[iid] = {
            "top10": list(payload.get("top10") or []),
            "top5": list(payload.get("top5") or []),
            "root_scores": {
                c["node"]: c["root_score"] for c in (payload.get("candidates") or [])
            },
        }
    return per_incident


def run_stability(
    ds: DatasetInfo,
    incidents: list[dict],
    bundle: dict,
    *,
    n_repeats: int = 20,
    drop_fraction: float = 0.05,
    seed: int = 20260925,
    top_k: int = 10,
    adjacency=None,
) -> dict:
    """Spec 4.1: repeat the ranking under evidence perturbation.

    Reports, per incident and per node, the frequency of landing in top-1 /
    top-3 / top-5, plus mean rank and rank standard deviation across repeats.
    High frequencies with low spread mean the ranking is evidence-driven; a
    flat distribution means it is noise, and no amount of prompt tuning will
    help until the evidence is fixed.
    """
    rng = random.Random(seed)
    baseline = rank_incidents(ds, incidents, bundle, adjacency=adjacency, top_k=top_k)
    runs: list[dict[str, dict]] = []
    for _ in range(max(1, n_repeats)):
        perturbed = perturb_bundle(bundle, drop_fraction, rng)
        runs.append(rank_incidents(ds, incidents, perturbed, adjacency=adjacency, top_k=top_k))

    per_incident: dict[str, dict] = {}
    for iid, base in baseline.items():
        ranks: dict[str, list[int]] = {}
        pool = set(base["top10"])
        for run in runs:
            entry = run.get(iid) or {}
            order = list(entry.get("top10") or [])
            pool.update(order)
            for node in order:
                ranks.setdefault(node, []).append(order.index(node) + 1)
        nodes: dict[str, dict] = {}
        for node in sorted(pool):
            observed = ranks.get(node, [])
            filled = observed + [_MISSING_RANK_PENALTY + top_k] * (len(runs) - len(observed))
            if not filled:
                continue
            mean_rank = sum(filled) / len(filled)
            variance = sum((r - mean_rank) ** 2 for r in filled) / len(filled)
            nodes[node] = {
                "top1_freq": sum(1 for r in filled if r == 1) / len(filled),
                "top3_freq": sum(1 for r in filled if r <= 3) / len(filled),
                "top5_freq": sum(1 for r in filled if r <= 5) / len(filled),
                "mean_rank": mean_rank,
                "rank_std": variance ** 0.5,
                "n_runs_observed": len(observed),
            }
        changed = sum(
            1 for run in runs if (run.get(iid) or {}).get("top10", [None])[:1] != base["top10"][:1]
        )
        per_incident[iid] = {
            "baseline_top10": base["top10"],
            "nodes": nodes,
            "top1_changed_runs": changed,
            "top1_change_rate": changed / len(runs) if runs else 0.0,
        }

    top1_rates = [entry["top1_change_rate"] for entry in per_incident.values()]
    summary = {
        "n_repeats": len(runs),
        "drop_fraction": drop_fraction,
        "perturbation_layer": "evidence payloads (not raw tables)",
        "incidents": len(per_incident),
        "mean_top1_change_rate": (sum(top1_rates) / len(top1_rates)) if top1_rates else 0.0,
        "incidents_with_stable_top1": sum(1 for r in top1_rates if r == 0.0),
        "verdict": _verdict(top1_rates),
    }
    return {"stage": "f1_stability", "summary": summary, "per_incident": per_incident}


def _verdict(rates: list[float]) -> str:
    """Spec 4.2's diagnostic table, applied to the stability statistic."""
    if not rates:
        return "no incidents"
    mean = sum(rates) / len(rates)
    if mean <= 0.10:
        return "stable: the ranking is evidence-driven, adding evidence can pay off"
    if mean <= 0.40:
        return "mixed: part of the ranking is noise; inspect the weakest incidents first"
    return "unstable: the ranking is dominated by noise -- fix evidence before prompt tuning"
