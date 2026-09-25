"""Temporal episode construction.

The first-stage anomaly detector produces 5-minute points.  A point is not an
incident and, more importantly, adjacent 5-minute points are not automatically
one episode.  This module groups abnormal points from the same network element
using a configurable maximum abnormal gap, minimum duration and minimum number
of abnormal points.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import PipelineConfig
from .utils import get_logger, to_iso

log = get_logger(__name__)


def _score_series(point_df: pd.DataFrame, cfg: PipelineConfig) -> tuple[pd.Series, str]:
    col = str(cfg.episode_score_column)
    if col in point_df.columns:
        score = pd.to_numeric(point_df[col], errors="coerce")
        return score.fillna(0.0), col
    if "final_normal_score" in point_df.columns:
        score = 1.0 - pd.to_numeric(point_df["final_normal_score"], errors="coerce")
        return score.fillna(0.0), "1-final_normal_score"
    if "combined_anomaly_score" in point_df.columns:
        score = pd.to_numeric(point_df["combined_anomaly_score"], errors="coerce")
        return score.fillna(0.0), "combined_anomaly_score"
    raise ValueError("point dataframe has no anomaly score column")


def _threshold(score: pd.Series, cfg: PipelineConfig) -> float:
    if cfg.episode_anomaly_threshold is not None:
        return float(cfg.episode_anomaly_threshold)
    q = float(cfg.episode_anomaly_quantile)
    q = min(max(q, 0.0), 1.0)
    if score.notna().sum() == 0:
        return 0.0
    return float(score.quantile(q))


def _group_abnormal_times(times: list[pd.Timestamp], max_gap_minutes: int) -> list[list[pd.Timestamp]]:
    groups: list[list[pd.Timestamp]] = []
    if not times:
        return groups
    current = [times[0]]
    max_gap = pd.Timedelta(minutes=int(max_gap_minutes))
    for ts in times[1:]:
        if ts - current[-1] <= max_gap:
            current.append(ts)
        else:
            groups.append(current)
            current = [ts]
    groups.append(current)
    return groups


def build_episodes(point_df: pd.DataFrame, cfg: PipelineConfig, dataset_name: str | None = None) -> pd.DataFrame:
    """Convert point-level anomaly scores into temporal episodes."""
    if point_df.empty:
        return pd.DataFrame(columns=[
            "episode_id", "network_element_id", "start_time", "end_time",
            "duration_minutes", "num_points", "abnormal_points",
            "peak_anomaly_score", "mean_anomaly_score", "persistence",
            "growth_rate", "first_abnormal_time", "peak_time", "recovery_time",
            "score_column",
        ])
    required = {"network_element_id", "timestamp_bin"}
    missing = required - set(point_df.columns)
    if missing:
        raise ValueError(f"point dataframe missing columns: {sorted(missing)}")

    df = point_df.copy()
    df["_score"], score_col = _score_series(df, cfg)
    df["_ts"] = pd.to_datetime(df["timestamp_bin"], errors="coerce", utc=True)
    df = df[df["_ts"].notna()].copy()
    if df.empty:
        return pd.DataFrame()
    threshold = _threshold(df["_score"], cfg)
    min_points = max(1, int(cfg.episode_min_abnormal_points))
    min_duration = max(0, int(cfg.episode_min_duration_minutes))
    max_gap = max(0, int(cfg.episode_max_gap_minutes))
    base = dataset_name or "dataset"
    rows: list[dict[str, Any]] = []

    for neid, grp in df.sort_values("_ts").groupby("network_element_id", observed=True):
        grp = grp.sort_values("_ts").reset_index(drop=True)
        abnormal = grp[grp["_score"] >= threshold].copy()
        if abnormal.empty:
            continue
        abnormal_times = [pd.Timestamp(t) for t in abnormal["_ts"].tolist()]
        for group_idx, times in enumerate(_group_abnormal_times(abnormal_times, max_gap), start=1):
            start = min(times)
            end = max(times)
            duration = max(0, int(round((end - start).total_seconds() / 60.0)))
            mask = (grp["_ts"] >= start) & (grp["_ts"] <= end)
            window = grp[mask]
            # A short normal gap is tolerated inside an episode by construction:
            # the group boundary only depends on the gap between abnormal points.
            abn_window = window[window["_score"] >= threshold]
            if len(abn_window) < min_points:
                continue
            if duration < min_duration:
                # Single-bin episodes are still allowed only when min_duration=0.
                continue
            values = abn_window["_score"].to_numpy(dtype=float)
            peak_idx = int(np.nanargmax(values)) if len(values) else 0
            peak_time = abn_window.iloc[peak_idx]["_ts"]
            first_time = abn_window.iloc[0]["_ts"]
            last_abnormal = abn_window.iloc[-1]["_ts"]
            after = grp[grp["_ts"] > last_abnormal]
            recovery = after.iloc[0]["_ts"] if not after.empty else end + pd.Timedelta(minutes=int(cfg.bin_minutes))
            total_window_points = max(1, len(window))
            persistence = float(len(abn_window) / total_window_points)
            first_val = float(abn_window.iloc[0]["_score"])
            last_val = float(abn_window.iloc[-1]["_score"])
            denom_minutes = max(1.0, float(duration))
            growth_rate = float((last_val - first_val) / denom_minutes)
            episode_id = f"EP_{base}_{neid}_{group_idx:04d}"
            rows.append({
                "episode_id": episode_id,
                "dataset": base,
                "network_element_id": str(neid),
                "start_time": to_iso(start),
                "end_time": to_iso(end),
                "duration_minutes": duration,
                "num_points": int(total_window_points),
                "abnormal_points": int(len(abn_window)),
                "peak_anomaly_score": float(np.nanmax(values)) if len(values) else 0.0,
                "mean_anomaly_score": float(np.nanmean(values)) if len(values) else 0.0,
                "persistence": persistence,
                "growth_rate": growth_rate,
                "first_abnormal_time": to_iso(first_time),
                "peak_time": to_iso(peak_time),
                "recovery_time": to_iso(recovery),
                "score_column": score_col,
                "threshold": float(threshold),
            })

    out = pd.DataFrame(rows)
    if out.empty:
        log.info("episode build: no episode survived thresholds")
        return out
    out = out.sort_values(["start_time", "network_element_id"]).reset_index(drop=True)
    log.info("episode build: %d episodes from %d points (threshold=%.4f)", len(out), len(df), threshold)
    return out


def save_episodes(episode_df: pd.DataFrame, out_dir: str | Path) -> Path:
    from pathlib import Path

    path = Path(out_dir) / "episode_scores.csv"
    episode_df.to_csv(path, index=False)
    return path
