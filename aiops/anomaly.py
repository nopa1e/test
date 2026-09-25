from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.ensemble import IsolationForest
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler

from .config import PipelineConfig
from .utils import get_logger, rank_to_unit

log = get_logger(__name__)


@dataclass
class FirstStageOutput:
    feature_names: list[str]
    X_imputed: np.ndarray
    X_scaled: np.ndarray
    if_anomaly_score: np.ndarray
    vae_anomaly_score: np.ndarray
    combined_anomaly_score: np.ndarray
    pseudo_label: np.ndarray
    pseudo_normal_score: np.ndarray
    imputer: SimpleImputer
    scaler: StandardScaler
    vae_model: "VAE | None"
    if_model: IsolationForest | None


class VAE(nn.Module):
    def __init__(self, input_dim: int, hidden_dim: int = 64, latent_dim: int = 8):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
        )
        self.mu = nn.Linear(hidden_dim, latent_dim)
        self.logvar = nn.Linear(hidden_dim, latent_dim)
        self.decoder = nn.Sequential(
            nn.Linear(latent_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, input_dim),
        )

    def encode(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        h = self.encoder(x)
        return self.mu(h), self.logvar(h)

    @staticmethod
    def reparameterize(mu: torch.Tensor, logvar: torch.Tensor) -> torch.Tensor:
        std = torch.exp(0.5 * logvar)
        eps = torch.randn_like(std)
        return mu + eps * std

    def decode(self, z: torch.Tensor) -> torch.Tensor:
        return self.decoder(z)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        mu, logvar = self.encode(x)
        z = self.reparameterize(mu, logvar)
        return self.decode(z), mu, logvar


def _impute_and_scale(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, SimpleImputer, StandardScaler]:
    imputer = SimpleImputer(strategy="median")
    X_imputed = imputer.fit_transform(X)
    X_imputed = np.nan_to_num(X_imputed, nan=0.0, posinf=0.0, neginf=0.0)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_imputed)
    # Constant columns produce zero scale; keep them finite.
    if hasattr(scaler, "scale_"):
        scaler.scale_ = np.where(np.abs(scaler.scale_) < 1e-12, 1.0, scaler.scale_)
        X_scaled = (X_imputed - scaler.mean_) / scaler.scale_
    X_scaled = np.nan_to_num(X_scaled, nan=0.0, posinf=0.0, neginf=0.0)
    return X_imputed, X_scaled, imputer, scaler


def _train_vae(X_scaled: np.ndarray, cfg: PipelineConfig) -> tuple[VAE, np.ndarray]:
    n, d = X_scaled.shape
    device = torch.device("cpu")
    model = VAE(d, hidden_dim=cfg.vae_hidden_dim, latent_dim=cfg.vae_latent_dim).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=cfg.vae_lr)
    tensor = torch.tensor(X_scaled, dtype=torch.float32, device=device)
    dataset = torch.utils.data.TensorDataset(tensor)
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=min(cfg.vae_batch_size, max(32, n)),
        shuffle=True,
        drop_last=False,
    )
    model.train()
    for epoch in range(1, int(cfg.vae_epochs) + 1):
        total = 0.0
        seen = 0
        for (batch,) in loader:
            optimizer.zero_grad(set_to_none=True)
            recon, mu, logvar = model(batch)
            recon_loss = F.mse_loss(recon, batch, reduction="none").mean(dim=1).mean()
            kl = -0.5 * torch.mean(1.0 + logvar - mu.pow(2) - logvar.exp())
            loss = recon_loss + float(cfg.vae_beta) * kl
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            optimizer.step()
            total += float(loss.item()) * batch.shape[0]
            seen += batch.shape[0]
        if epoch == 1 or epoch % max(1, int(cfg.vae_epochs // 4)) == 0 or epoch == cfg.vae_epochs:
            log.info("VAE epoch %d/%d loss=%.6f", epoch, cfg.vae_epochs, total / max(1, seen))

    model.eval()
    errors = np.empty(n, dtype=np.float64)
    with torch.no_grad():
        for start in range(0, n, 4096):
            batch = tensor[start:start + 4096]
            recon, _, _ = model(batch)
            err = F.mse_loss(recon, batch, reduction="none").mean(dim=1)
            errors[start:start + len(err)] = err.detach().cpu().numpy()
    return model, errors


def run_first_stage(
    X: np.ndarray,
    feature_names: list[str],
    cfg: PipelineConfig,
) -> FirstStageOutput:
    if X.ndim != 2 or X.shape[0] == 0:
        raise ValueError("X must be a non-empty 2-D array")

    X_imputed, X_scaled, imputer, scaler = _impute_and_scale(X)
    n = X_scaled.shape[0]

    # Isolation Forest: assume the majority is normal; the model is trained
    # directly on all data without labels.
    log.info("fitting IsolationForest on %s points x %s features", n, X_scaled.shape[1])
    if_model = IsolationForest(
        n_estimators=int(cfg.if_estimators),
        contamination=cfg.if_contamination,
        random_state=int(cfg.seed),
        n_jobs=-1,
    )
    if_model.fit(X_scaled)
    if_raw = -if_model.score_samples(X_scaled)
    if_score = rank_to_unit(if_raw, higher_is_more_anomalous=True)

    # VAE is trained as a pure normal-data autoencoder.  High reconstruction
    # error at inference means "less normal".
    log.info("training VAE on %s points x %s features", n, X_scaled.shape[1])
    vae_model, vae_raw = _train_vae(X_scaled, cfg)
    vae_score = rank_to_unit(vae_raw, higher_is_more_anomalous=True)

    combined = np.nanmean(np.vstack([if_score, vae_score]), axis=0)
    combined = np.nan_to_num(combined, nan=0.5, posinf=1.0, neginf=0.0)

    # Pseudo labels: the quietest normal majority gets y=1, the extreme tail
    # gets a weak y=0.  The middle is masked and ignored by the GNN loss.
    low = float(np.nanquantile(combined, cfg.pseudo_normal_quantile))
    high = float(np.nanquantile(combined, cfg.pseudo_anomaly_quantile))
    pseudo = np.full(n, np.nan, dtype=np.float64)
    pseudo[combined <= low] = 1.0
    pseudo[combined >= high] = 0.0

    # The pseudo normal confidence is used as an input feature *and* as a
    # regression target for graph smoothing.
    pseudo_normal_score = 1.0 - combined
    pseudo_normal_score = np.clip(pseudo_normal_score, 0.0, 1.0)

    out = FirstStageOutput(
        feature_names=list(feature_names),
        X_imputed=X_imputed,
        X_scaled=X_scaled,
        if_anomaly_score=if_score,
        vae_anomaly_score=vae_score,
        combined_anomaly_score=combined,
        pseudo_label=pseudo,
        pseudo_normal_score=pseudo_normal_score,
        imputer=imputer,
        scaler=scaler,
        vae_model=vae_model,
        if_model=if_model,
    )
    log.info(
        "first stage done: if_mean=%.4f vae_mean=%.4f pseudo_normal=%d pseudo_anomaly=%d",
        float(np.nanmean(if_score)),
        float(np.nanmean(vae_score)),
        int(np.nansum(pseudo == 1.0)),
        int(np.nansum(pseudo == 0.0)),
    )
    return out
