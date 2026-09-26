"""RootScore: the seven sub-scores behind a candidate ranking (spec 5.9).

The candidate list is produced by *programs*; the LLM only re-ranks what it is
given (spec 5.9: "LLM 不得自由创造候选").  Because of that inversion, spec 5.9
requires every sub-score to be persisted alongside the final score so a ranking
can be audited later without re-running anything.

Weights are a **constant, not a tuning knob**.  Spec 1.5-5 and section 8 forbid
optimising anything against the official black-box score, so the only permitted
justification is: theoretical plausibility -> local label-free consistency ->
public benchmark.  ``ROOT_SCORE_WEIGHTS`` is copied verbatim from spec 5.9.

``incoming_propagation`` and ``contradiction`` carry **negative** weights: a
node that everything else already explains, or that has counter-evidence
against it, is a worse root-cause candidate, not a better one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

#: Verbatim from spec 5.9.  Do not tune against the official evaluator.
ROOT_SCORE_WEIGHTS: dict[str, float] = {
    "local_anomaly": 0.20,
    "temporal_priority": 0.25,
    "outgoing_propagation": 0.20,
    "cross_modal_support": 0.20,
    "predictive_explanation": 0.15,
    "incoming_propagation": -0.15,
    "contradiction": -0.15,
}

SUB_SCORES: tuple[str, ...] = tuple(ROOT_SCORE_WEIGHTS)

#: Sub-scores that make a node *less* likely to be the root cause.
NEGATIVE_SUB_SCORES: tuple[str, ...] = tuple(
    name for name, weight in ROOT_SCORE_WEIGHTS.items() if weight < 0
)


def minmax(values: Mapping[str, float]) -> dict[str, float]:
    """Scale a score family to [0, 1].

    A family with no spread maps to all-zeros rather than all-ones: if every
    candidate looks identical on this axis it carries no information, and
    awarding 1.0 to everyone would silently inflate every ``root_score``.
    """
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi - lo <= 1e-12:
        return {key: 0.0 for key in values}
    return {key: (value - lo) / (hi - lo) for key, value in values.items()}


def clamp01(value: float) -> float:
    """Clamp to [0, 1]; evidence counts are ratios but arithmetic drift is real."""
    if value != value:  # NaN
        return 0.0
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else float(value))


def rank_priority(order: Sequence[str]) -> dict[str, float]:
    """Turn a first-mover order into a [0, 1] priority.

    The earliest mover gets 1.0 and the last gets 0.0; a single candidate gets
    1.0 (it is trivially first).  Linear rather than exponential so one noisy
    stamp cannot dominate the 0.25-weight term.
    """
    n = len(order)
    if n == 0:
        return {}
    if n == 1:
        return {order[0]: 1.0}
    return {node: 1.0 - (idx / (n - 1)) for idx, node in enumerate(order)}


def combine(sub_scores: Mapping[str, float]) -> float:
    """Weighted sum of the seven sub-scores.

    Missing sub-scores count as 0.0 -- spec 5.9 wants unimplemented evidence
    (predictive / contradiction in the current build) to be *visible* as a
    zero contribution rather than silently renormalising the weights, which
    would quietly change every other term's effective weight.
    """
    total = 0.0
    for name, weight in ROOT_SCORE_WEIGHTS.items():
        value = sub_scores.get(name)
        if value is None:
            continue
        total += weight * float(value)
    return float(total)


def explain(sub_scores: Mapping[str, float]) -> list[dict]:
    """Per-term contribution, largest absolute first, for the persisted JSON."""
    rows = []
    for name, weight in ROOT_SCORE_WEIGHTS.items():
        value = float(sub_scores.get(name) or 0.0)
        rows.append({"term": name, "value": value, "weight": weight, "contribution": weight * value})
    rows.sort(key=lambda row: -abs(row["contribution"]))
    return rows
