"""Experiment D/E: a second unsupervised anomaly model over first-stage outputs.

D: weighted reconstruction loss using the first-stage anomaly score; anomalous
samples receive smaller weights so the model learns the normal state.
E: hard threshold; first-stage anomalies above the threshold are excluded.

This is an artifact-level implementation: it consumes ``point_scores.csv.gz``
from an existing full pipeline run and writes a second-stage anomaly score
table.  It does not read ground-truth labels.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from sklearn.preprocessing import StandardScaler

from aiops.anomaly import VAE
from aiops.utils import ensure_dir, get_logger, rank_to_unit

log = get_logger(__name__)

META_COLS = {
    "point_id", "timestamp_bin", "region", "node", "node_type", "network_element_id",
    "top_feature_json", "point_rank",
    # D/E must not depend on GNN-derived scores.
    "gnn_normal_score", "final_normal_score", "ensemble_normal_score",
}


def _features(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    cols: list[str] = []
    for c in df.columns:
        if c in META_COLS:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    if not cols:
        raise ValueError("no numeric columns for second-stage model")
    X = df[cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float64)
    return X, cols


def _first_anomaly(df: pd.DataFrame) -> np.ndarray:
    for c in ("combined_anomaly_score", "if_anomaly_score", "vae_anomaly_score"):
        if c in df.columns:
            v = pd.to_numeric(df[c], errors="coerce").fillna(0.0).to_numpy(dtype=float)
            if v.max() > v.min():
                return v
    if "final_normal_score" in df.columns:
        return 1.0 - pd.to_numeric(df["final_normal_score"], errors="coerce").fillna(1.0).to_numpy(dtype=float)
    return np.zeros(len(df), dtype=float)


def _train_weighted_vae(
    X: np.ndarray,
    weights: np.ndarray,
    cfg,
    epochs: int,
    hidden: int,
    latent: int,
    lr: float,
    beta: float,
    batch_size: int,
) -> tuple[VAE, np.ndarray]:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = VAE(X.shape[1], hidden_dim=hidden, latent_dim=latent).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    tensor = torch.tensor(X, dtype=torch.float32, device=device)
    w = torch.tensor(weights, dtype=torch.float32, device=device)
    n = len(X)
    for _ in range(max(1, int(epochs))):
        perm = torch.randperm(n, device=device)
        for start in range(0, n, batch_size):
            idx = perm[start:start + batch_size]
            batch = tensor[idx]
            batch_w = w[idx]
            opt.zero_grad(set_to_none=True)
            recon, mu, logvar = model(batch)
            recon_loss = F.mse_loss(recon, batch, reduction="none").mean(dim=1)
            loss = (recon_loss * batch_w).sum() / torch.clamp(batch_w.sum(), min=1e-6)
            kl = -0.5 * torch.mean(1.0 + logvar - mu.pow(2) - logvar.exp())
            loss = loss + float(beta) * kl
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
    model.eval()
    with torch.no_grad():
        recon, _, _ = model(tensor)
        err = F.mse_loss(recon, tensor, reduction="none").mean(dim=1).cpu().numpy()
    return model, err


def run_one(ds_name: str, point_path: Path, out_dir: Path, args) -> dict:
    df = pd.read_csv(point_path, compression="gzip")
    if df.empty:
        raise ValueError(f"{point_path} is empty")
    first = _first_anomaly(df)
    X, feature_names = _features(df)
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0))
    X_scaled = np.nan_to_num(X_scaled, nan=0.0, posinf=0.0, neginf=0.0)
    if args.mode == "weighted":
        # Experiment D: learn the normal state.
        # Anomalous samples must receive *smaller* weights, not larger ones.
        alpha = max(0.0, float(args.alpha))
        weights = np.maximum(0.0, 1.0 - alpha * np.clip(first, 0.0, 1.0))
    else:
        keep = first <= float(args.threshold)
        weights = keep.astype(float)
    if weights.sum() < 2:
        log.warning("%s: effective sample weight too small; falling back to all points", ds_name)
        weights = np.ones_like(first)
    model, raw = _train_weighted_vae(
        X_scaled,
        weights,
        None,
        epochs=args.epochs,
        hidden=args.hidden,
        latent=args.latent,
        lr=args.lr,
        beta=args.beta,
        batch_size=args.batch_size,
    )
    second = rank_to_unit(raw, higher_is_more_anomalous=True)
    out = df[["point_id", "timestamp_bin", "network_element_id"]].copy() if "point_id" in df.columns else df[["timestamp_bin", "network_element_id"]].copy()
    out["first_anomaly"] = first
    out["second_anomaly"] = second
    out["second_raw_error"] = raw
    out["mode"] = args.mode
    ensure_dir(out_dir)
    out.to_csv(out_dir / f"{ds_name}_second_stage_scores.csv", index=False)
    if "point_rank" in df.columns:
        out["point_rank"] = df["point_rank"].to_numpy()
    node = out.groupby("network_element_id", observed=True).agg(
        first_anomaly=("first_anomaly", "mean"),
        second_anomaly=("second_anomaly", "mean"),
        second_peak=("second_anomaly", "max"),
        points=("second_anomaly", "count"),
    ).reset_index()
    node = node.sort_values("second_anomaly", ascending=False)
    node.to_csv(out_dir / f"{ds_name}_second_stage_node_scores.csv", index=False)
    return {
        "dataset": ds_name,
        "mode": args.mode,
        "points": int(len(out)),
        "features": len(feature_names),
        "second_mean": float(np.nanmean(second)),
        "second_max": float(np.nanmax(second)),
        "output": str(out_dir / f"{ds_name}_second_stage_scores.csv"),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Experiment D/E second-stage unsupervised anomaly model.")
    p.add_argument("--artifacts-dir", required=True, help="Root containing <dataset>/point_scores.csv.gz")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--mode", choices=("weighted", "threshold"), required=True)
    p.add_argument("--threshold", type=float, default=0.7)
    p.add_argument("--alpha", type=float, default=1.0)
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--hidden", type=int, default=32)
    p.add_argument("--latent", type=int, default=4)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--beta", type=float, default=5e-4)
    p.add_argument("--batch-size", type=int, default=512)
    args = p.parse_args()

    root = Path(args.artifacts_dir)
    out_root = ensure_dir(args.output_dir)
    summaries = []
    for ds_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        point_path = ds_dir / "point_scores.csv.gz"
        if not point_path.exists():
            log.warning("skip %s: missing %s", ds_dir.name, point_path)
            continue
        try:
            summaries.append(run_one(ds_dir.name, point_path, out_root, args))
        except Exception as exc:
            log.exception("second-stage failed for %s: %s", ds_dir.name, exc)
    (out_root / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summaries": summaries, "output_dir": str(out_root)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
