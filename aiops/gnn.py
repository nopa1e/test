from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler

from .config import PipelineConfig
from .utils import get_logger

log = get_logger(__name__)


@dataclass
class GNNOutput:
    normal_score: np.ndarray
    anomaly_score: np.ndarray
    X_imputed: np.ndarray
    X_scaled: np.ndarray
    feature_names: list[str]
    model: "GCN | None"
    history: list[dict[str, float]]


class GCN(nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int = 64, dropout: float = 0.2):
        super().__init__()
        self.lin1 = nn.Linear(in_dim, hidden_dim)
        self.lin2 = nn.Linear(hidden_dim, hidden_dim)
        self.out = nn.Linear(hidden_dim, 1)
        self.dropout = float(dropout)

    def forward(self, x: torch.Tensor, adj: torch.Tensor) -> torch.Tensor:
        x = F.relu(torch.sparse.mm(adj, self.lin1(x)))
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = F.relu(torch.sparse.mm(adj, self.lin2(x)))
        return self.out(x).squeeze(-1)


def normalize_adjacency(num_nodes: int, edge_index: torch.Tensor, device: torch.device) -> torch.Tensor:
    if edge_index.numel() == 0:
        edge_index = torch.arange(num_nodes, dtype=torch.long).unsqueeze(0).repeat(2, 1)
    edge_index = edge_index.to(device)
    loops = torch.arange(num_nodes, dtype=torch.long, device=device).unsqueeze(0).repeat(2, 1)
    edge_index = torch.cat([edge_index, loops], dim=1)
    row, col = edge_index
    deg = torch.zeros(num_nodes, dtype=torch.float32, device=device)
    deg.index_add_(0, row, torch.ones_like(row, dtype=torch.float32))
    deg_inv_sqrt = deg.pow(-0.5)
    deg_inv_sqrt[torch.isinf(deg_inv_sqrt)] = 0.0
    values = deg_inv_sqrt[row] * deg_inv_sqrt[col]
    adj = torch.sparse_coo_tensor(edge_index, values, (num_nodes, num_nodes), device=device)
    return adj.coalesce()


def _prepare_x(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, SimpleImputer, StandardScaler]:
    imputer = SimpleImputer(strategy="median")
    X_imputed = imputer.fit_transform(X)
    X_imputed = np.nan_to_num(X_imputed, nan=0.0, posinf=0.0, neginf=0.0)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_imputed)
    if hasattr(scaler, "scale_"):
        scaler.scale_ = np.where(np.abs(scaler.scale_) < 1e-12, 1.0, scaler.scale_)
        X_scaled = (X_imputed - scaler.mean_) / scaler.scale_
    X_scaled = np.nan_to_num(X_scaled, nan=0.0, posinf=0.0, neginf=0.0)
    return X_imputed, X_scaled, imputer, scaler


def _fallback_smooth(
    pseudo_normal_score: np.ndarray,
    edge_index: torch.Tensor,
) -> np.ndarray:
    """Simple graph smoothing fallback when training is not possible."""
    n = len(pseudo_normal_score)
    if edge_index.numel() == 0:
        return np.clip(pseudo_normal_score, 0.0, 1.0)
    row, col = edge_index.cpu().numpy()
    base = np.asarray(pseudo_normal_score, dtype=float)
    neigh_sum = np.zeros(n, dtype=float)
    counts = np.zeros(n, dtype=float)
    for r, c in zip(row, col):
        neigh_sum[r] += base[c]
        counts[r] += 1.0
    neigh = np.divide(neigh_sum, np.maximum(counts, 1.0))
    return np.clip(0.5 * base + 0.5 * neigh, 0.0, 1.0)


def train_gnn(
    X: np.ndarray,
    feature_names: list[str],
    edge_index: torch.Tensor,
    pseudo_label: np.ndarray,
    pseudo_normal_score: np.ndarray,
    cfg: PipelineConfig,
) -> GNNOutput:
    n = X.shape[0]
    if n == 0:
        raise ValueError("no graph nodes")

    X_imputed, X_scaled, _, _ = _prepare_x(X)
    device = torch.device("cpu")
    # Keep the tensor in float32 for PyTorch; feature matrices are small after
    # 5-minute binning.
    x_t = torch.tensor(X_scaled, dtype=torch.float32, device=device)
    scores_t = torch.tensor(np.clip(pseudo_normal_score, 0.0, 1.0), dtype=torch.float32, device=device)

    if n < 8 or edge_index.numel() == 0:
        normal = _fallback_smooth(pseudo_normal_score, edge_index)
        return GNNOutput(
            normal_score=normal,
            anomaly_score=1.0 - normal,
            X_imputed=X_imputed,
            X_scaled=X_scaled,
            feature_names=feature_names,
            model=None,
            history=[],
        )

    adj = normalize_adjacency(n, edge_index, device)
    model = GCN(X_scaled.shape[1], hidden_dim=cfg.gnn_hidden_dim, dropout=cfg.gnn_dropout).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=cfg.gnn_lr, weight_decay=1e-4)

    label_mask_np = ~np.isnan(pseudo_label)
    labels_np = np.nan_to_num(pseudo_label, nan=0.0)
    if label_mask_np.sum() == 0:
        label_mask_np = np.ones(n, dtype=bool)
        labels_np = (pseudo_normal_score >= np.nanmedian(pseudo_normal_score)).astype(float)
    label_mask = torch.tensor(label_mask_np, dtype=torch.bool, device=device)
    labels = torch.tensor(labels_np, dtype=torch.float32, device=device)

    pos_count = float((labels[label_mask] == 1).sum().item())
    neg_count = float((labels[label_mask] == 0).sum().item())
    pos_weight = torch.tensor([max(1.0, neg_count / max(1.0, pos_count))], dtype=torch.float32, device=device)
    bce = nn.BCEWithLogitsLoss(pos_weight=pos_weight, reduction="mean")

    history: list[dict[str, float]] = []
    model.train()
    for epoch in range(1, int(cfg.gnn_epochs) + 1):
        opt.zero_grad(set_to_none=True)
        logits = model(x_t, adj)
        probs = torch.sigmoid(logits)
        loss_cls = bce(logits[label_mask], labels[label_mask])
        loss_reg = F.mse_loss(probs, scores_t)
        # Graph smoothness on adjacent points.
        if edge_index.numel() > 0:
            row, col = edge_index.to(device)
            smooth = ((probs[row] - probs[col]) ** 2).mean()
        else:
            smooth = torch.tensor(0.0, device=device)
        loss = loss_cls + float(cfg.gnn_reg_weight) * loss_reg + 0.05 * smooth
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        opt.step()
        if epoch == 1 or epoch % max(1, int(cfg.gnn_epochs // 4)) == 0 or epoch == cfg.gnn_epochs:
            history.append(
                {
                    "epoch": float(epoch),
                    "loss": float(loss.item()),
                    "loss_cls": float(loss_cls.item()),
                    "loss_reg": float(loss_reg.item()),
                    "smooth": float(smooth.item()),
                }
            )
            log.info(
                "GNN epoch %d/%d loss=%.6f cls=%.6f reg=%.6f smooth=%.6f",
                epoch,
                cfg.gnn_epochs,
                loss.item(),
                loss_cls.item(),
                loss_reg.item(),
                smooth.item(),
            )

    model.eval()
    with torch.no_grad():
        normal = torch.sigmoid(model(x_t, adj)).detach().cpu().numpy().astype(np.float64)
    normal = np.clip(normal, 0.0, 1.0)
    # Very small safety blend with the unsupervised pseudo score to avoid
    # over-confident GNN extrapolation.
    normal = 0.85 * normal + 0.15 * np.clip(pseudo_normal_score, 0.0, 1.0)
    return GNNOutput(
        normal_score=normal,
        anomaly_score=1.0 - normal,
        X_imputed=X_imputed,
        X_scaled=X_scaled,
        feature_names=feature_names,
        model=model,
        history=history,
    )
