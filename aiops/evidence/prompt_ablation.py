"""Prompt ablation and evidence agreement (spec 4.2).

The measurement that motivates the whole of F1: rewriting the prompt flips
``resource`` 58% into ``routing`` 47% while the official score does not move at
all (spec 1.1).  Three variants separate the two possible causes:

===========  ==========================================================
``A``        the full prompt
``B``        the topology / dependency block removed, nothing else changed
``C``        identical content, different wording and candidate order
===========  ==========================================================

Two numbers come out (spec 4.2):

1. agreement between the variants' rank-1 / category -- the "玄学程度";
2. agreement between the LLM and *deterministic* evidence -- whether the model
   is actually reading the data.

The spec's diagnostic table is applied verbatim::

    low  / low   dice-rolling  -> add evidence first, prompt tuning is pointless
    high / low   the prompt locks the model onto a prior -> evidence is drowned
    low  / high  the model uses the data and is merely phrasing-sensitive
                 -> *this* is when prompt tuning is worth doing
"""

from __future__ import annotations

from collections import Counter

from ..utils import get_logger
from .llm_rerank import VARIANTS, rerank_all

log = get_logger(__name__)


def deterministic_first_movers(
    iid: str,
    *,
    temporal_ev: dict | None,
    routing_ev: dict | None,
    quality_ev: dict | None,
    top_n: int = 5,
) -> list[str]:
    """Who a *program* would name, from evidence alone (spec 4.2).

    The three deterministic signals named in the spec: the earliest anomalous
    node, the nodes whose routing metrics jumped, and the scrape-outage points.
    They are combined by vote rather than by a learned weight, so the result is
    reproducible and auditable.
    """
    votes: Counter[str] = Counter()

    incident = ((temporal_ev or {}).get("incidents") or {}).get(iid) or {}
    for rank, node in enumerate(list(incident.get("order") or [])[:3], start=1):
        votes[str(node)] += 4 - rank  # earliest mover carries the most weight

    rental = ((routing_ev or {}).get("incidents") or {}).get(iid) or {}
    routing_nodes = Counter(str(e.get("node")) for e in (rental.get("events") or []) if e.get("node"))
    for node, count in routing_nodes.most_common(3):
        votes[node] += min(3, count)

    for point in (quality_ev or {}).get("zero_up_points") or []:
        node = point.get("node")
        if node:
            votes[str(node)] += 1

    return [node for node, _ in votes.most_common(top_n)]


def run_prompt_ablation(
    incidents: list[dict],
    candidates: dict,
    *,
    base_url: str,
    model: str,
    workers: int = 8,
    region: str = "",
    limit: int | None = None,
    variants: tuple[str, ...] = VARIANTS,
) -> dict[str, dict]:
    """Run every variant over the same incidents; return ``{variant: result}``."""
    per_variant: dict[str, dict] = {}
    for variant in variants:
        log.info("prompt ablation: variant %s", variant)
        per_variant[variant] = rerank_all(
            incidents,
            candidates,
            base_url=base_url,
            model=model,
            variant=variant,
            region=region,
            workers=workers,
            limit=limit,
        )
        summary = per_variant[variant].get("summary") or {}
        print(f"[PA] variant {variant}: {summary}", flush=True)
    return per_variant


def summarise_ablation(per_variant: dict[str, dict]) -> dict:
    """Spec 4.2 metric 1: do the variants agree with each other?"""
    variants = [v for v in VARIANTS if v in per_variant]
    by_incident: dict[str, dict[str, str]] = {}
    for variant in variants:
        for iid, entry in (per_variant[variant].get("incidents") or {}).items():
            ranking = entry.get("llm_ranking") or []
            if ranking:
                by_incident.setdefault(iid, {})[variant] = str(ranking[0])

    comparable = {iid: m for iid, m in by_incident.items() if len(m) == len(variants)}
    unanimous = sum(1 for m in comparable.values() if len(set(m.values())) == 1)

    pairwise: dict[str, float] = {}
    for i, left in enumerate(variants):
        for right in variants[i + 1:]:
            same = total = 0
            for m in comparable.values():
                if left in m and right in m:
                    total += 1
                    same += int(m[left] == m[right])
            pairwise[f"{left}-{right}"] = same / total if total else 0.0

    rate = unanimous / len(comparable) if comparable else 0.0
    return {
        "variants": variants,
        "incidents_compared": len(comparable),
        "rank1_unanimous_rate": rate,
        "pairwise_rank1_agreement": pairwise,
        "stability_reading": (
            "low" if rate < 0.5 else ("medium" if rate < 0.8 else "high")
        ),
    }


def summarise_agreement(per_variant: dict[str, dict], deterministic: dict[str, list[str]]) -> dict:
    """Spec 4.2 metric 2: does any variant track the deterministic evidence?"""
    per_variant_out: dict[str, dict] = {}
    for variant, result in per_variant.items():
        hits = comparable = 0
        top3_hits = 0
        for iid, entry in (result.get("incidents") or {}).items():
            expected = deterministic.get(iid) or []
            ranking = entry.get("llm_ranking") or []
            if not expected or not ranking:
                continue
            comparable += 1
            hits += int(str(ranking[0]) == str(expected[0]))
            top3_hits += int(str(ranking[0]) in {str(n) for n in expected[:3]})
        per_variant_out[variant] = {
            "incidents_compared": comparable,
            "rank1_match_rate": hits / comparable if comparable else 0.0,
            "rank1_in_evidence_top3_rate": top3_hits / comparable if comparable else 0.0,
        }

    per_variant_out["reading"] = (
        "high" if any(v["rank1_match_rate"] >= 0.5 for k, v in per_variant_out.items() if k in VARIANTS)
        else "low"
    )
    return per_variant_out


def diagnose(ablation: dict, agreement: dict) -> dict:
    """Apply spec 4.2's decision table to the two measurements."""
    stability = ablation.get("stability_reading", "low")
    reading = agreement.get("reading", "low")

    if stability == "high" and reading == "low":
        verdict = (
            "提示词把模型锁死在先验上（如无脑 cpu_pressure），证据被淹 —— 先补证据，"
            "再谈调提示词"
        )
    elif stability == "low" and reading == "high":
        verdict = "模型在用数据、只是对表述敏感 —— 此时调提示词才有意义"
    elif stability == "low" and reading == "low":
        verdict = "掷骰子 —— 先补证据，调提示词无意义"
    else:
        verdict = "提示词与证据都稳定 —— 检查是否还有可回收的证据缺口"

    return {"stability": stability, "evidence_reading": reading, "verdict": verdict}
