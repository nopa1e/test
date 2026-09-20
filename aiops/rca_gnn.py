"""RCA-oriented GNN student.

This module is intentionally separate from :mod:`aiops.gnn`, so the original
normality GNN remains available as the V1 baseline.  It can train a lightweight
multi-task GCN with a root-cause head and optional major/minor heads.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F

from .config import PipelineConfig
from .gnn import normalize_adjacency
from .utils import get_logger

log = get_logger(__name__)


@dataclass
class RCAOutput:
    root_cause_score: np.ndarray
    X_imputed: np.ndarray
    X_scaled: np.ndarray
    feature_names: list[str]
    model: "MultiTaskGCN | None"
    history: list[dict[str, float]]
    major_logits: np.ndarray | None = None
    minor_logits: np.ndarray | None = None


class MultiTaskGCN(nn.Module):
    def __init__(self, in_dim: int, hidden_dim: int, n_major: int = 0, n_minor: int = 0, dropout: float = 0.2):
        super().__init__()
        self.lin1 = nn.Linear(in_dim, hidden_dim)
        self.lin2 = nn.Linear(hidden_dim, hidden_dim)
        self.dropout = float(dropout)
        self.rca_head = nn.Linear(hidden_dim, 1)
        self.major_head = nn.Linear(hidden_dim, n_major) if n_major > 0 else None
        self.minor_head = nn.Linear(hidden_dim, n_minor) if n_minor > 0 else None

    def encode(self, x: torch.Tensor, adj: torch.Tensor) -> torch.Tensor:
        x = F.relu(torch.sparse.mm(adj, self.lin1(x)))
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = F.relu(torch.sparse.mm(adj, self.lin2(x)))
        return x

    def forward(self, x: torch.Tensor, adj: torch.Tensor) -> dict[str, torch.Tensor]:
        h = self.encode(x, adj)
        out = {"rca": self.rca_head(h).squeeze(-1)}
        if self.major_head is not None:
            out["major"] = self.major_head(h)
        if self.minor_head is not None:
            out["minor"] = self.minor_head(h)
        return out


def _prepare_features(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    ignore = {"incident_id", "dataset", "network_element_id", "split", "label_source", "root_cause_target"}
    cols: list[str] = []
    for c in df.columns:
        if c in ignore:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    if not cols:
        raise ValueError("no numeric RCA feature columns")
    X = df[cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float64)
    return X, cols


def _impute_scale(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler

    imputer = SimpleImputer(strategy="median")
    X_imputed = imputer.fit_transform(X)
    X_imputed = np.nan_to_num(X_imputed, nan=0.0, posinf=0.0, neginf=0.0)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_imputed)
    if hasattr(scaler, "scale_"):
        scaler.scale_ = np.where(np.abs(scaler.scale_) < 1e-12, 1.0, scaler.scale_)
        X_scaled = (X_imputed - scaler.mean_) / scaler.scale_
    X_scaled = np.nan_to_num(X_scaled, nan=0.0, posinf=0.0, neginf=0.0)
    return X_imputed, X_scaled


def _pairwise_ranking_loss(scores: torch.Tensor, targets: torch.Tensor, incident_ids: list[str]) -> torch.Tensor:
    """A small listwise pairwise ranking loss within each incident."""
    losses: list[torch.Tensor] = []
    groups: dict[str, list[int]] = {}
    for i, inc in enumerate(incident_ids):
        groups.setdefault(str(inc), []).append(i)
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        idx = torch.tensor(idxs, dtype=torch.long, device=scores.device)
        s = scores[idx]
        t = targets[idx]
        diff = s.unsqueeze(0) - s.unsqueeze(1)
        target_diff = (t.unsqueeze(0) > t.unsqueeze(1)).float()
        # only ordered pairs with a meaningful target difference
        mask = (target_diff > 0) | (t.unsqueeze(0) < t.unsqueeze(1))
        if mask.any():
            losses.append(F.softplus(-diff[mask]).mean())
    if not losses:
        return torch.tensor(0.0, device=scores.device)
    return torch.stack(losses).mean()


def build_incident_edge_index(rca_features: pd.DataFrame, entity_graph: nx.Graph | nx.DiGraph | None) -> torch.Tensor:
    """Build a global edge index with edges only inside the same incident."""
    if rca_features.empty or entity_graph is None:
        return torch.empty((2, 0), dtype=torch.long)
    id_to_idx = {str(n): i for i, n in enumerate(rca_features["network_element_id"].astype(str).tolist())}
    inc_col = rca_features.get("incident_id", pd.Series([""] * len(rca_features))).astype(str).tolist()
    edges: set[tuple[int, int]] = set()
    for i, (node, inc) in enumerate(zip(rca_features["network_element_id"].astype(str), inc_col)):
        if node not in entity_graph:
            continue
        for peer in entity_graph.neighbors(node):
            j = id_to_idx.get(str(peer))
            if j is not None and inc_col[j] == inc:
                edges.add((i, j))
                edges.add((j, i))
    if not edges:
        return torch.empty((2, 0), dtype=torch.long)
    return torch.tensor(sorted(edges), dtype=torch.long).t().contiguous()


def train_rca_gnn(
    rca_features: pd.DataFrame,
    edge_index: torch.Tensor,
    cfg: PipelineConfig,
    labels: dict[str, np.ndarray] | None = None,
) -> RCAOutput:
    """Train the RCA GNN student on incident-node features."""
    if rca_features.empty:
        raise ValueError("rca_features is empty")
    X, feature_names = _prepare_features(rca_features)
    X_imputed, X_scaled = _impute_scale(X)
    n = len(rca_features)
    device = torch.device("cpu")
    x_t = torch.tensor(X_scaled, dtype=torch.float32, device=device)
    labels = labels or {}

    root_target = labels.get("root_cause_target")
    if root_target is None and "root_cause_target" in rca_features.columns:
        root_target = pd.to_numeric(rca_features["root_cause_target"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    if root_target is None:
        # Fall back to a deterministic heuristic target: peak anomaly and
        # temporal precedence.  It is used only to make the module runnable
        # without a teacher; ablation logs record this source.
        peak = pd.to_numeric(rca_features.get("point_max", pd.Series(np.zeros(n))), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        prec = pd.to_numeric(rca_features.get("temporal_precedence_score", pd.Series(np.zeros(n))), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        raw = 0.65 * peak + 0.35 * prec
        root_target = (raw - raw.min()) / max(1e-12, (raw.max() - raw.min()))
    root_target = np.asarray(root_target, dtype=float)
    root_target = np.clip(root_target, 0.0, 1.0)

    major_target = labels.get("major")
    minor_target = labels.get("minor")
    n_major = int(np.nanmax(major_target)) + 1 if major_target is not None and len(major_target) else 0
    n_minor = int(np.nanmax(minor_target)) + 1 if minor_target is not None and len(minor_target) else 0

    if n < 4 or edge_index.numel() == 0:
        out = RCAOutput(
            root_cause_score=root_target,
            X_imputed=X_imputed,
            X_scaled=X_scaled,
            feature_names=feature_names,
            model=None,
            history=[],
        )
        return out

    adj = normalize_adjacency(n, edge_index, device)
    model = MultiTaskGCN(
        X_scaled.shape[1],
        hidden_dim=int(cfg.rca_gnn_hidden_dim),
        n_major=n_major,
        n_minor=n_minor,
        dropout=float(cfg.rca_gnn_dropout),
    ).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=float(cfg.rca_gnn_lr), weight_decay=1e-4)
    root_t = torch.tensor(root_target, dtype=torch.float32, device=device)
    inc_ids = rca_features.get("incident_id", pd.Series([""] * n)).astype(str).tolist()
    major_t = torch.tensor(major_target, dtype=torch.long, device=device) if major_target is not None else None
    minor_t = torch.tensor(minor_target, dtype=torch.long, device=device) if minor_target is not None else None

    history: list[dict[str, float]] = []
    model.train()
    for epoch in range(1, int(cfg.rca_gnn_epochs) + 1):
        opt.zero_grad(set_to_none=True)
        out = model(x_t, adj)
        rca_logits = out["rca"]
        loss_rca = F.binary_cross_entropy_with_logits(rca_logits, root_t)
        loss_rank = _pairwise_ranking_loss(torch.sigmoid(rca_logits), root_t, inc_ids)
        loss = float(cfg.lambda_rca) * (loss_rca + 0.5 * loss_rank)
        if major_t is not None and "major" in out and int(cfg.lambda_major) > 0:
            loss = loss + float(cfg.lambda_major) * F.cross_entropy(out["major"], major_t)
        if minor_t is not None and "minor" in out and int(cfg.lambda_minor) > 0:
            loss = loss + float(cfg.lambda_minor) * F.cross_entropy(out["minor"], minor_t)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        opt.step()
        if epoch == 1 or epoch % max(1, int(cfg.rca_gnn_epochs // 5)) == 0 or epoch == cfg.rca_gnn_epochs:
            history.append({"epoch": float(epoch), "loss": float(loss.item())})
            log.info("RCA GNN epoch %d/%d loss=%.6f", epoch, cfg.rca_gnn_epochs, loss.item())

    model.eval()
    with torch.no_grad():
        out = model(x_t, adj)
        root = torch.sigmoid(out["rca"]).cpu().numpy().astype(np.float64)
        major_logits = out.get("major").cpu().numpy() if "major" in out else None
        minor_logits = out.get("minor").cpu().numpy() if "minor" in out else None
    return RCAOutput(
        root_cause_score=np.clip(root, 0.0, 1.0),
        X_imputed=X_imputed,
        X_scaled=X_scaled,
        feature_names=feature_names,
        model=model,
        history=history,
        major_logits=major_logits,
        minor_logits=minor_logits,
    )
