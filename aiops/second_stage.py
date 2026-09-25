"""Second-stage unsupervised model for experiments D and E.

D and E run TWO unsupervised models back to back before the graph GNN:

    IF + VAE     first stage: the ordinary pipeline front end, which yields
                 ``combined_anomaly_score`` per point
    second VAE   second stage: trained only on what the first stage believes is
                 normal, so it models the normal state more sharply
                   D  weight = max(0, 1 - alpha * first_anomaly)   soft down-weight
                   E  weight = 1 if first_anomaly <= threshold     hard exclusion
    GNN          graph model over the point graph
    LLM          incident-level verification

The second-stage score feeds the GNN (as an extra node feature and as the
regression target) rather than replacing the GNN's output.  That is why this
lives inside the pipeline and not as a post-processing step over artifacts.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F

from .anomaly import VAE
from .config import PipelineConfig
from .utils import get_logger, rank_to_unit

log = get_logger(__name__)


@dataclass
class SecondStageOutput:
    anomaly_score: np.ndarray
    normal_score: np.ndarray
    weights: np.ndarray
    mode: str
    effective_samples: int
    stats: dict[str, Any]


def second_stage_weights(first_anomaly: np.ndarray, cfg: PipelineConfig) -> tuple[np.ndarray, str]:
    """Turn first-stage anomaly scores into second-stage training weights."""
    first = np.clip(np.asarray(first_anomaly, dtype=float), 0.0, 1.0)
    mode = str(getattr(cfg, "second_stage_mode", "") or "").strip().lower()
    if mode == "weighted":
        alpha = max(0.0, float(getattr(cfg, "second_stage_alpha", 1.0)))
        # Anomalous points get SMALLER weights so the model learns normal shape.
        return np.maximum(0.0, 1.0 - alpha * first), mode
    if mode == "threshold":
        thr = float(getattr(cfg, "second_stage_threshold", 0.7))
        # Anomalous points are excluded outright.
        return (first <= thr).astype(float), mode
    return np.ones_like(first), "none"


def _train_weighted_vae(
    X: np.ndarray,
    weights: np.ndarray,
    cfg: PipelineConfig,
) -> np.ndarray:
    """Train the second VAE with a weighted reconstruction loss; return raw errors."""
    epochs = max(1, int(getattr(cfg, "second_stage_epochs", 20)))
    hidden = int(getattr(cfg, "second_stage_hidden", 32))
    latent = int(getattr(cfg, "second_stage_latent", 4))
    lr = float(getattr(cfg, "second_stage_lr", 1e-3))
    beta = float(getattr(cfg, "second_stage_beta", 5e-4))
    batch_size = int(getattr(cfg, "second_stage_batch_size", 512))
    seed = int(getattr(cfg, "seed", 42))

    # CPU on purpose: the GPU is busy serving the LLM backend.
    device = torch.device("cpu")
    torch.manual_seed(seed)
    generator = torch.Generator(device=device).manual_seed(seed)

    model = VAE(X.shape[1], hidden_dim=hidden, latent_dim=latent).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    tensor = torch.tensor(X, dtype=torch.float32, device=device)
    w = torch.tensor(weights, dtype=torch.float32, device=device)
    n = len(X)

    model.train()
    for epoch in range(1, epochs + 1):
        perm = torch.randperm(n, device=device, generator=generator)
        total = 0.0
        for start in range(0, n, batch_size):
            idx = perm[start:start + batch_size]
            batch = tensor[idx]
            batch_w = w[idx]
            opt.zero_grad(set_to_none=True)
            recon, mu, logvar = model(batch)
            recon_loss = F.mse_loss(recon, batch, reduction="none").mean(dim=1)
            loss = (recon_loss * batch_w).sum() / torch.clamp(batch_w.sum(), min=1e-6)
            kl = -0.5 * torch.mean(1.0 + logvar - mu.pow(2) - logvar.exp())
            loss = loss + beta * kl
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            total += float(loss.item())
        if epoch == 1 or epoch % max(1, epochs // 4) == 0 or epoch == epochs:
            log.info("second-stage VAE epoch %d/%d loss=%.6f", epoch, epochs, total / max(1, (n // batch_size) + 1))

    model.eval()
    with torch.no_grad():
        recon, _, _ = model(tensor)
        err = F.mse_loss(recon, tensor, reduction="none").mean(dim=1).cpu().numpy()
    return err


def run_second_stage(
    X_scaled: np.ndarray,
    first_anomaly: np.ndarray,
    cfg: PipelineConfig,
) -> SecondStageOutput:
    """Train the second unsupervised model and return its anomaly score."""
    mode = str(getattr(cfg, "second_stage_mode", "") or "").strip().lower()
    if mode not in {"weighted", "threshold"}:
        raise ValueError(f"unsupported second_stage_mode: {mode!r}")

    weights, mode = second_stage_weights(first_anomaly, cfg)
    effective = int((weights > 0).sum())
    if weights.sum() < 2:
        log.warning("second stage (%s): effective weight too small; falling back to all points", mode)
        weights = np.ones_like(weights)
        effective = len(weights)

    raw = _train_weighted_vae(np.nan_to_num(X_scaled, nan=0.0, posinf=0.0, neginf=0.0), weights, cfg)
    anomaly = rank_to_unit(raw, higher_is_more_anomalous=True)
    anomaly = np.clip(np.nan_to_num(anomaly, nan=0.0, posinf=1.0, neginf=0.0), 0.0, 1.0)
    stats = {
        "mode": mode,
        "effective_samples": effective,
        "total_samples": int(len(weights)),
        "excluded_samples": int(len(weights) - effective),
        "mean_weight": float(np.mean(weights)),
    }
    log.info("second stage (%s): %d/%d samples weighted, mean_w=%.3f",
             mode, effective, len(weights), stats["mean_weight"])
    return SecondStageOutput(
        anomaly_score=anomaly,
        normal_score=1.0 - anomaly,
        weights=weights,
        mode=mode,
        effective_samples=effective,
        stats=stats,
    )
