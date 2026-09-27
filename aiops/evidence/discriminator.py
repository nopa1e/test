"""Experiment G -- dual-encoder hard-sample miner (see DSH连接工作文档.md §14).

The point of G is **not** to score predictions.  It is to find the events that
*neither* a normal-form nor an anomaly-form autoencoder can account for, and to
hand those out as a short, auditable worklist.

    layer 1 (existing)   the pipeline's evidence and RootScore
    layer 2 (G)          two autoencoders fitted on the same second-layer
                         features -- one on the normal-form half, one on the
                         anomaly-form half -- plus the events both of them
                         reconstruct badly ("excluded twice")

Those twice-excluded events are the valuable output:

* they are the small set worth **reading**, to find systematic defects in the
  pipeline that no aggregate metric would reveal;
* whatever they have in common is **knowledge** -- written out as explicit
  entries that can be injected back into an agent's context.

Direction note (fixed 2026-09-27): the anomaly score is *supposed* to be high
for evidence-rich predictions -- that is what "fault-shaped" means here.  An
earlier version labelled the score ``authenticity`` and read the resulting
negative correlation with RootScore as a failure; it was only a naming error.

Reference: DSH连接工作文档.md §14.  Spec §8 forbids any GNN/GAT-style model
swap, so this stays a plain autoencoder pair.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..dataset import DatasetInfo, canonical_node
from ..utils import get_logger

log = get_logger(__name__)

#: Second-layer features.  Every one is derived from evidence the pipeline has
#: already computed -- G adds no new data reads.
FEATURE_COLUMNS = (
    "n_modalities",
    "root_score",
    "n_supporting",
    "n_contradicting",
    "contradiction_ratio",
    "top1_margin",
    "temporal_gap_minutes",
    "duration_minutes",
    "episode_count",
    "n_candidates",
    "predictive_excess",
    "category_from_evidence",
)

#: A sample is "excluded twice" when BOTH autoencoders reconstruct it at least
#: this many sigmas worse than their own typical row.
HARD_SIGMA = 1.0

#: Cap on the worklist so it stays readable by a human.
MAX_HARD_SAMPLES = 60

NO_COUNTER_MARKER = "未发现反驳证据"


# --------------------------------------------------------------------------- features


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
    """One row per incident: evidence-structure features of its top candidate."""
    cand_map = (candidates or {}).get("incidents") or {}
    temporal_map = (temporal_ev or {}).get("incidents") or {}
    contra_map = (contradiction_ev or {}).get("incidents") or {}
    pred_map = (predictive_ev or {}).get("incidents") or {}

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
            if c and c != NO_COUNTER_MARKER
        ]
        n_sup, n_con = len(supporting), len(contradicting)

        scores = [float(c.get("root_score") or 0.0) for c in cands]
        margin = scores[0] - (scores[1] if len(scores) > 1 else 0.0)

        timing = ((temporal_map.get(iid) or {}).get("nodes") or {}).get(node) or {}
        gap = timing.get("gap_to_second_seconds")

        excess = 0.0
        for pair in ((pred_map.get(iid) or {}).get("pairs") or []):
            if canonical_node(pair.get("target")) == node:
                excess = max(excess, float(pair.get("excess_change") or 0.0))

        entry = ((contra_map.get(iid) or {}).get("nodes") or {}).get(node) or {}

        first_evidence = ""
        if supporting:
            head = supporting[0]
            first_evidence = head.get("text", "") if isinstance(head, dict) else str(head)

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
                "duration_minutes": float(incident.get("duration_minutes") or 0.0),
                "episode_count": float(incident.get("episode_count") or 0),
                "n_candidates": float(len(cands)),
                "predictive_excess": float(excess),
                "category_from_evidence": float(0.0 if entry.get("source") == "base" else 1.0),
                # worklist context, deliberately not features
                "top_modalities": ",".join(top.get("modalities") or []),
                "top_evidence": first_evidence,
            }
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    for column in FEATURE_COLUMNS:
        frame[column] = frame[column].fillna(frame[column].median())
    return frame


# --------------------------------------------------------------------------- autoencoders


def _zscore(values: np.ndarray) -> np.ndarray:
    mu, sd = float(np.mean(values)), float(np.std(values))
    return (values - mu) / (sd if sd > 1e-12 else 1.0)


def _train_ae_on(
    matrix: np.ndarray,
    idx: np.ndarray,
    *,
    latent: int,
    epochs: int,
    seed: int,
) -> np.ndarray:
    """Train an autoencoder on ``matrix[idx]``, then reconstruct **all** rows.

    Scoring every row (not just the training half) is what makes the two
    encoders comparable: each row gets a normal-form error and an anomaly-form
    error from models that never saw it in their own training split.
    """
    import torch

    torch.manual_seed(seed)
    train = matrix[idx]
    n, d = train.shape
    hidden = max(8, d * 2)
    model = torch.nn.Sequential(
        torch.nn.Linear(d, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, latent), torch.nn.ReLU(),
        torch.nn.Linear(latent, hidden), torch.nn.ReLU(),
        torch.nn.Linear(hidden, d),
    )
    xt = torch.tensor(train, dtype=torch.float32)
    optimiser = torch.optim.Adam(model.parameters(), lr=1e-3)
    batch = min(256, max(16, n // 4))
    for _ in range(epochs):
        perm = torch.randperm(n)
        for start in range(0, n, batch):
            xb = xt[perm[start:start + batch]]
            loss = torch.nn.functional.mse_loss(model(xb), xb)
            optimiser.zero_grad()
            loss.backward()
            optimiser.step()
    with torch.no_grad():
        xall = torch.tensor(matrix, dtype=torch.float32)
        return ((model(xall) - xall) ** 2).mean(dim=1).numpy()


def train_dual_autoencoders(
    matrix: np.ndarray,
    *,
    latent: int = 4,
    epochs: int = 60,
    seed: int = 42,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fit normal-form and anomaly-form AEs; flag the rows neither one owns.

    The split uses IsolationForest as a *weak, unsupervised* hint, so the two
    encoders learn "the shape of a typical prediction" and "the shape of an
    unusual one" without labels.

    ``margin = err_anomaly_z - err_normal_z``: positive reads as normal-form,
    negative as anomaly-form, and near zero means **neither encoder owns it** --
    the hard samples.

    ``anomaly_score`` is deliberately high for evidence-rich predictions (that is
    what "fault-shaped" means here) and is meant for a **positive** weight.
    """
    from sklearn.ensemble import IsolationForest

    forest = IsolationForest(n_estimators=200, contamination="auto", random_state=seed)
    forest.fit(matrix)
    hint = -forest.score_samples(matrix)
    cut = float(np.median(hint))
    normal_idx = np.where(hint <= cut)[0]
    anomaly_idx = np.where(hint > cut)[0]
    if normal_idx.size < 10 or anomaly_idx.size < 10:
        raise ValueError("not enough samples on one side of the unsupervised split")

    err_normal = _zscore(
        _train_ae_on(matrix, normal_idx, latent=latent, epochs=epochs, seed=seed)
    )
    err_anomaly = _zscore(
        _train_ae_on(matrix, anomaly_idx, latent=latent, epochs=epochs, seed=seed + 1)
    )

    margin = err_anomaly - err_normal
    hard = (err_normal > HARD_SIGMA) & (err_anomaly > HARD_SIGMA)

    # High normal-form error == the sample does not look like a typical
    # prediction, i.e. it looks fault-shaped.  Clipped so a couple of outliers
    # cannot saturate the scale.
    anomaly_score = 0.5 + np.clip(err_normal, -3.0, 3.0) / 6.0
    return err_normal, err_anomaly, margin, hard, anomaly_score


# --------------------------------------------------------------------------- knowledge


def extract_knowledge(frame: pd.DataFrame, hard: np.ndarray) -> list[dict]:
    """Entries describing what the twice-excluded events have in common.

    Each entry compares the hard set against the whole set on one feature, in
    plain numbers -- readable by a human and injectable into an agent's context
    verbatim.
    """
    entries: list[dict] = []
    if int(hard.sum()) < 3:
        return entries
    whole = frame[list(FEATURE_COLUMNS)]
    subset = frame.loc[hard, list(FEATURE_COLUMNS)]
    for column in FEATURE_COLUMNS:
        base = float(whole[column].mean())
        here = float(subset[column].mean())
        sd = float(whole[column].std())
        if sd <= 1e-9:
            continue
        shift = (here - base) / sd
        if abs(shift) < 0.5:
            continue
        entries.append(
            {
                "feature": column,
                "hard_mean": round(here, 4),
                "overall_mean": round(base, 4),
                "shift_sigma": round(shift, 3),
                "reading": (
                    f"困难样本的 {column} 平均为 {here:.3f}，全体为 {base:.3f}"
                    f"（相差 {shift:+.2f}σ）"
                ),
            }
        )
    entries.sort(key=lambda e: -abs(e["shift_sigma"]))
    return entries


# --------------------------------------------------------------------------- entry point


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
    epochs: int = 60,
) -> dict:
    """Fit the dual encoders; emit the hard-sample worklist and the knowledge."""
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
    if frame.empty or len(frame) < 40:
        return {"dataset": ds.name, "incidents": {}, "warnings": ["too few incidents to fit"]}

    matrix = frame[list(FEATURE_COLUMNS)].to_numpy(dtype=float)
    err_normal, err_anomaly, margin, hard, anomaly_score = train_dual_autoencoders(
        matrix, seed=seed, epochs=epochs
    )
    frame = frame.assign(
        err_normal=err_normal,
        err_anomaly=err_anomaly,
        margin=margin,
        anomaly_score=anomaly_score,
        is_hard=hard,
    )

    hard_rows = frame[frame["is_hard"]].sort_values("err_normal", ascending=False)
    worklist = [
        {
            "incident_id": row["incident_id"],
            "node": row["node"],
            "err_normal_sigma": round(float(row["err_normal"]), 3),
            "err_anomaly_sigma": round(float(row["err_anomaly"]), 3),
            "margin": round(float(row["margin"]), 3),
            "root_score": round(float(row["root_score"]), 4),
            "n_modalities": int(row["n_modalities"]),
            "contradiction_ratio": round(float(row["contradiction_ratio"]), 3),
            "duration_minutes": round(float(row["duration_minutes"]), 1),
            "modalities": row["top_modalities"],
            "first_evidence": str(row["top_evidence"])[:160],
        }
        for _, row in hard_rows.head(MAX_HARD_SAMPLES).iterrows()
    ]

    knowledge = extract_knowledge(frame, hard)
    summary = {
        "n": int(len(frame)),
        "n_hard": int(hard.sum()),
        "hard_ratio": round(float(hard.mean()), 4),
        "margin_mean": round(float(margin.mean()), 4),
        "margin_std": round(float(margin.std()), 4),
        "corr_anomaly_with_root_score": round(
            float(np.corrcoef(frame["anomaly_score"], frame["root_score"])[0, 1]), 4
        ),
        "n_knowledge_entries": len(knowledge),
    }
    log.info("dual-encoder[%s]: %s", ds.name, summary)

    per_incident = {
        row["incident_id"]: {
            "node": row["node"],
            "anomaly_score": round(float(row["anomaly_score"]), 4),
            "margin": round(float(row["margin"]), 4),
            "is_hard": bool(row["is_hard"]),
        }
        for _, row in frame.iterrows()
    }
    return {
        "dataset": ds.name,
        "features": list(FEATURE_COLUMNS),
        "summary": summary,
        "hard_samples": worklist,
        "knowledge": knowledge,
        "incidents": per_incident,
    }
