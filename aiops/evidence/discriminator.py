"""Experiment G -- second-layer discriminator (see DSH连接工作文档.md §14).

Layer 1 (the existing pipeline) learns what a *statistical anomaly* looks like on
raw data.  G asks a different question on a harder input: given a prediction the
pipeline already produced, does its **evidence structure** look like a genuine
fault or like a misfire?

    existing   one RootScore per incident; no notion of "is this real"
    G adds     a per-incident authenticity score from second-layer features
               (cross-modal agreement, contradiction load, timing sharpness,
               interval shape), plus the hook that weights RootScore with it

Deliberately unsupervised.  Ground truth for "is this a real fault" does not
exist, so an IsolationForest + VAE is fitted on the *shape* of well-supported
predictions rather than being told which ones are correct.  Cross-modal
agreement is used only to **audit** the result, never to train it -- otherwise
the discriminator would simply re-learn F's own bias and the audit would be
circular.

Spec 1.3 is explicit that cutting submission count is worth at most ~5 points
and in practice 0~1 (top-292 lost 72%).  So this module never deletes a
prediction: it produces a **weight**, to be blended into RootScore by the
candidate stage when the audit justifies it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..dataset import DatasetInfo, canonical_node
from ..utils import get_logger

log = get_logger(__name__)

#: Second-layer features.  Every one of them is derived from evidence the
#: pipeline has already computed -- G adds no new data reads.
FEATURE_COLUMNS = (
    "n_modalities",          # how many evidence families back the top candidate
    "root_score",            # the candidate stage's own confidence
    "n_supporting",          # supporting evidence items
    "n_contradicting",       # contradicting items (excluding the "none found" marker)
    "contradiction_ratio",   # contradicting / (supporting + contradicting)
    "top1_margin",           # root_score of rank1 minus rank2
    "temporal_gap_minutes",  # first-mover vs runner-up gap
    "duration_minutes",      # prediction interval length
    "episode_count",         # episodes aggregated into this incident
    "n_candidates",          # size of the candidate set
    "predictive_excess",     # excess change not explained by upstream
    "category_from_evidence",  # 1 when the category came from evidence, not base
)

#: Audit bands for cross-modal agreement.  Used only to check whether the
#: unsupervised score separates well-supported from thinly-supported incidents.
AUDIT_STRONG = 3
AUDIT_WEAK = 1

#: The literal marker contradiction.py writes when it finds no counter-evidence.
NO_COUNTER_MARKER = "未发现反驳证据"


def _evidence_marker() -> str:
    return NO_COUNTER_MARKER


def build_feature_table(
    incidents: list[dict],
    candidates: dict,
    *,
    metric_ev: dict | None = None,
    routing_ev: dict | None = None,
    log_ev: dict | None = None,
    quality_ev: dict | None = None,
    flow_ev: dict | None = None,
    temporal_ev: dict | None = None,
    predictive_ev: dict | None = None,
    contradiction_ev: dict | None = None,
) -> pd.DataFrame:
    """One row per incident: the evidence-structure features of its top candidate."""
    cand_map = (candidates or {}).get("incidents") or {}
    temporal_map = (temporal_ev or {}).get("incidents") or {}
    contra_map = (contradiction_ev or {}).get("incidents") or {}
    pred_map = (predictive_ev or {}).get("incidents") or {}
    marker = _evidence_marker()

    rows: list[dict] = []
    for incident in incidents:
        iid = str(incident.get("incident_id"))
        payload = cand_map.get(iid) or {}
        cands = payload.get("candidates") or []
        if not cands:
            continue
        top = cands[0]
        node = canonical_node(top.get("node"))

        supporting = [s for s in (top.get("supporting") or []) if s]
        contradicting = [
            c for c in (top.get("contradicting") or [])
            if c and c != marker
        ]
        n_sup, n_con = len(supporting), len(contradicting)

        scores = [float(c.get("root_score") or 0.0) for c in cands]
        margin = scores[0] - (scores[1] if len(scores) > 1 else 0.0)

        timing = ((temporal_map.get(iid) or {}).get("nodes") or {}).get(node) or {}
        gap = timing.get("gap_to_second_seconds")

        tr = incident.get("time_range") or {}
        duration = float(incident.get("duration_minutes") or 0.0)

        pairs = ((pred_map.get(iid) or {}).get("pairs") or [])
        excess = 0.0
        for pair in pairs:
            if canonical_node(pair.get("target")) == node:
                excess = max(excess, float(pair.get("excess_change") or 0.0))

        categories = (contra_map.get(iid) or {}).get("nodes") or {}
        entry = categories.get(node) or {}

        rows.append(
            {
                "incident_id": iid,
                "node": node,
                "n_modalities": float(len(top.get("modalities") or [])),
                "root_score": scores[0],
                "n_supporting": float(n_sup),
                "n_contradicting": float(n_con),
                "contradiction_ratio": float(n_con) / max(1, n_sup + n_con),
                "top1_margin": float(margin),
                "temporal_gap_minutes": float(gap) / 60.0 if gap is not None else np.nan,
                "duration_minutes": duration,
                "episode_count": float(incident.get("episode_count") or 0),
                "n_candidates": float(len(cands)),
                "predictive_excess": float(excess),
                "category_from_evidence": float(
                    1.0 if "source" not in entry else (0.0 if entry.get("source") == "base" else 1.0)
                ),
                "audit_modalities": float(len(top.get("modalities") or [])),
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    # Median-fill rather than zero-fill: a missing temporal gap means "unknown",
    # and zero would read as "two nodes moved at the same instant", which is the
    # opposite of what an absent measurement implies.
    for column in FEATURE_COLUMNS:
        if column in frame.columns:
            frame[column] = frame[column].fillna(frame[column].median())
    return frame


def _fit_if_vae(matrix: np.ndarray, *, seed: int = 42, epochs: int = 40, latent: int = 4):
    """IsolationForest + a small VAE, reusing the same shape as the base stage.

    Returns a combined anomaly score in [0, 1] where higher means "less like a
    well-formed evidence structure".
    """
    from sklearn.ensemble import IsolationForest
    import torch

    torch.manual_seed(seed)
    n, d = matrix.shape

    forest = IsolationForest(n_estimators=200, contamination="auto", random_state=seed)
    forest.fit(matrix)
    if_score = -forest.score_samples(matrix)  # higher = more anomalous
    if_score = (if_score - if_score.min()) / max(1e-9, np.ptp(if_score))

    x = torch.tensor(matrix, dtype=torch.float32)
    hidden = max(8, d * 2)
    encoder = torch.nn.Sequential(
        torch.nn.Linear(d, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, latent),
    )
    decoder = torch.nn.Sequential(
        torch.nn.Linear(latent, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, d),
    )
    params = list(encoder.parameters()) + list(decoder.parameters())
    optimiser = torch.optim.Adam(params, lr=1e-3)
    batch = min(512, max(32, n // 4))
    for _ in range(epochs):
        perm = torch.randperm(n)
        for start in range(0, n, batch):
            idx = perm[start:start + batch]
            xb = x[idx]
            z = encoder(xb)
            recon = decoder(z)
            loss = torch.nn.functional.mse_loss(recon, xb)
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
    with torch.no_grad():
        recon = decoder(encoder(x))
        vae_err = ((recon - x) ** 2).mean(dim=1).numpy()
    vae_err = (vae_err - vae_err.min()) / max(1e-9, np.ptp(vae_err))

    combined = 0.5 * if_score + 0.5 * vae_err
    return combined, if_score, vae_err


def run_discriminator(
    ds: DatasetInfo,
    incidents: list[dict],
    candidates: dict,
    *,
    metric_ev: dict | None = None,
    routing_ev: dict | None = None,
    log_ev: dict | None = None,
    quality_ev: dict | None = None,
    flow_ev: dict | None = None,
    temporal_ev: dict | None = None,
    predictive_ev: dict | None = None,
    contradiction_ev: dict | None = None,
    seed: int = 42,
) -> dict:
    """Fit the second-layer discriminator and audit it against cross-modal agreement."""
    frame = build_feature_table(
        incidents,
        candidates,
        metric_ev=metric_ev,
        routing_ev=routing_ev,
        log_ev=log_ev,
        quality_ev=quality_ev,
        flow_ev=flow_ev,
        temporal_ev=temporal_ev,
        predictive_ev=predictive_ev,
        contradiction_ev=contradiction_ev,
    )
    if frame.empty or len(frame) < 20:
        return {"dataset": ds.name, "incidents": {}, "warnings": ["too few incidents to fit"]}

    matrix = frame[list(FEATURE_COLUMNS)].to_numpy(dtype=float)
    combined, if_score, vae_err = _fit_if_vae(matrix, seed=seed)
    # "Authenticity" = the opposite of anomalous-within-predictions, and it is
    # what a positive RootScore weight needs.
    authenticity = 1.0 - (combined - combined.min()) / max(1e-9, np.ptp(combined))

    frame = frame.assign(
        anomaly_score=combined,
        if_score=if_score,
        vae_score=vae_err,
        authenticity=authenticity,
    )

    # ---- audit: does the unsupervised score separate support bands? ---------
    strong = frame[frame["audit_modalities"] >= AUDIT_STRONG]
    weak = frame[frame["audit_modalities"] <= AUDIT_WEAK]
    audit = {
        "n": int(len(frame)),
        "n_strong": int(len(strong)),
        "n_weak": int(len(weak)),
        "mean_authenticity_strong": float(strong["authenticity"].mean()) if len(strong) else None,
        "mean_authenticity_weak": float(weak["authenticity"].mean()) if len(weak) else None,
        "separation": (
            float(strong["authenticity"].mean() - weak["authenticity"].mean())
            if len(strong) and len(weak) else None
        ),
        "pearson_with_modalities": float(
            np.corrcoef(frame["audit_modalities"], frame["authenticity"])[0, 1]
        ) if frame["authenticity"].std() > 0 else None,
        "pearson_with_root_score": float(
            np.corrcoef(frame["root_score"], frame["authenticity"])[0, 1]
        ) if frame["authenticity"].std() > 0 else None,
    }
    if audit["separation"] is not None:
        audit["verdict"] = (
            "discriminator tracks evidence support"
            if audit["separation"] > 0.05
            else "no usable separation -- weighting would add noise, not signal"
        )
    else:
        audit["verdict"] = "insufficient bands to audit"

    per_incident = {
        row["incident_id"]: {
            "node": row["node"],
            "authenticity": float(row["authenticity"]),
            "anomaly_score": float(row["anomaly_score"]),
            "n_modalities": int(row["n_modalities"]),
            "root_score": float(row["root_score"]),
        }
        for _, row in frame.iterrows()
    }
    log.info(
        "discriminator[%s]: n=%d separation=%s verdict=%s",
        ds.name, len(frame), audit.get("separation"), audit.get("verdict"),
    )
    return {
        "dataset": ds.name,
        "features": list(FEATURE_COLUMNS),
        "audit": audit,
        "incidents": per_incident,
    }
