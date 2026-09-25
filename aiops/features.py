from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

import numpy as np
import pandas as pd

from .config import PipelineConfig
from .dataset import DatasetInfo, canonical_node, network_element_id, read_table, ip_node_map_from_scrape_health
from .utils import get_logger, normalize_name

log = get_logger(__name__)

KEY_COLUMNS = ["timestamp_bin", "region", "node", "node_type", "network_element_id"]
NON_FEATURE_COLUMNS = set(KEY_COLUMNS + [
    "timestamp", "event_time", "minute_utc", "timestamp_utc", "prometheus_sample_time_utc",
    "if_anomaly_score", "vae_anomaly_score", "pseudo_normal_score", "pseudo_label",
    "gnn_normal_score", "final_normal_score", "ensemble_normal_score", "point_rank",
    "point_id", "top_feature_json", "raw_node", "raw_region",
])


def _numeric_columns(df: pd.DataFrame) -> list[str]:
    cols: list[str] = []
    for c in df.columns:
        if c in NON_FEATURE_COLUMNS:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    return cols


def _flatten_columns(df: pd.DataFrame, prefix: str = "") -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [
            f"{prefix}{'_'.join(str(x) for x in col if str(x) != '')}"
            for col in df.columns.values
        ]
    elif prefix:
        df = df.copy()
        df.columns = [f"{prefix}{c}" for c in df.columns]
    return df


def _prepare_time(df: pd.DataFrame, col: str, bin_minutes: int, out_col: str = "timestamp_bin") -> pd.DataFrame:
    if df.empty or col not in df.columns:
        return df
    df = df.copy()
    ts = pd.to_datetime(df[col], errors="coerce", utc=True)
    df[out_col] = ts.dt.floor(f"{int(bin_minutes)}min")
    df = df[df[out_col].notna()].copy()
    return df


def _agg_node_metrics(ds: DatasetInfo, cfg: PipelineConfig) -> pd.DataFrame:
    df = read_table(ds, "node_metrics")
    if df.empty:
        return pd.DataFrame()
    df = _prepare_time(df, "timestamp", cfg.bin_minutes)
    num = [c for c in df.columns if c not in {"timestamp", "timestamp_bin", "region", "node", "node_type"} and pd.api.types.is_numeric_dtype(df[c])]
    keys = ["timestamp_bin", "region", "node", "node_type"]
    if not all(k in df.columns for k in keys) or not num:
        return pd.DataFrame()
    out = df.groupby(keys, observed=True, dropna=False)[num].mean().reset_index()
    out = out.rename(columns={c: f"node_{c}" for c in num})
    return out


def _agg_interface_metrics(ds: DatasetInfo, cfg: PipelineConfig) -> pd.DataFrame:
    df = read_table(ds, "interface_metrics")
    if df.empty:
        return pd.DataFrame()
    df = _prepare_time(df, "timestamp", cfg.bin_minutes)
    num = [c for c in df.columns if c not in {"timestamp", "timestamp_bin", "region", "node", "node_type", "interface_id", "if_role"} and pd.api.types.is_numeric_dtype(df[c])]
    keys = ["timestamp_bin", "region", "node", "node_type"]
    if not all(k in df.columns for k in keys) or not num:
        return pd.DataFrame()
    # Mean captures sustained changes, max captures short spikes/drops.
    agg = df.groupby(keys, observed=True, dropna=False)[num].agg(["mean", "max"])
    agg.columns = [f"iface_{c}_{stat}" for c, stat in agg.columns]
    agg = agg.reset_index()
    return agg


def _agg_routing_metrics(ds: DatasetInfo, cfg: PipelineConfig) -> pd.DataFrame:
    df = read_table(ds, "routing_metrics")
    if df.empty:
        return pd.DataFrame()
    df = _prepare_time(df, "timestamp", cfg.bin_minutes)
    if "value" not in df.columns:
        return pd.DataFrame()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    keys = ["timestamp_bin", "region", "node", "node_type"]
    if not all(k in df.columns for k in keys):
        return pd.DataFrame()
    if "metric_name" not in df.columns:
        return pd.DataFrame()
    # Cumulative / monotonic routing counters must be differenced; otherwise
    # their raw increasing values dominate the anomaly score (e.g. BGP uptime).
    metric_s = df["metric_name"].astype(str)
    cum_mask = metric_s.str.contains("uptime|_total", case=False, regex=True, na=False)
    if cum_mask.any():
        group_cols = [c for c in ["region", "node", "node_type", "metric_name", "label"] if c in df.columns]
        if group_cols:
            df = df.sort_values(["timestamp_bin"] + group_cols, kind="stable").copy()
            df.loc[cum_mask, "value"] = (
                df.loc[cum_mask].groupby(group_cols, observed=True, dropna=False)["value"].diff()
            )
            df = df[~(cum_mask & df["value"].isna())].copy()
    # Limit to the most useful routing metric names to keep the feature matrix small.
    metric_counts = df["metric_name"].astype(str).value_counts()
    keep_metrics = list(metric_counts.head(max(10, cfg.max_routing_labels)).index)
    df = df[df["metric_name"].astype(str).isin(keep_metrics)].copy()
    if df.empty:
        return pd.DataFrame()
    stats = df.groupby(keys + ["metric_name"], observed=True, dropna=False)["value"].agg(
        ["mean", "min", "max", "std", "count"]
    ).reset_index()
    pivot = stats.pivot_table(
        index=keys,
        columns="metric_name",
        values=["mean", "min", "max", "std", "count"],
        aggfunc="first",
        observed=True,
    )
    pivot.columns = [f"routing_{metric}_{stat}" for stat, metric in pivot.columns]
    pivot = pivot.reset_index()
    return pivot


def _agg_scrape_health(ds: DatasetInfo, cfg: PipelineConfig) -> pd.DataFrame:
    df = read_table(ds, "scrape_health")
    if df.empty:
        return pd.DataFrame()
    df = _prepare_time(df, "timestamp", cfg.bin_minutes)
    keys = ["timestamp_bin", "region", "node", "node_type"]
    if not all(k in df.columns for k in keys):
        return pd.DataFrame()
    num = [c for c in ["scrape_up", "scrape_duration_seconds", "scrape_samples"] if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
    if not num:
        return pd.DataFrame()
    out = df.groupby(keys, observed=True, dropna=False)[num].mean().reset_index()
    out = out.rename(columns={c: f"scrape_{c}" for c in num})
    return out


def _keyword_columns() -> list[str]:
    return [
        "bgp", "ospf", "route", "link", "interface", "acl", "deny", "drop",
        "cpu", "memory", "disk", "dns", "web", "auth", "timeout", "error",
        "fail", "down", "flap", "blackhole", "port", "rule", "default_route",
    ]


def _agg_frr(ds: DatasetInfo, cfg: PipelineConfig) -> pd.DataFrame:
    df = read_table(ds, "frr_syslog_events")
    if df.empty:
        return pd.DataFrame()
    time_col = "event_time" if "event_time" in df.columns else ("received_at" if "received_at" in df.columns else None)
    if time_col is None:
        return pd.DataFrame()
    df = _prepare_time(df, time_col, cfg.bin_minutes)
    if "hostname" not in df.columns and "source_ip" not in df.columns:
        return pd.DataFrame()
    # Prefer hostname, fall back to source IP.
    if "hostname" in df.columns:
        node_candidates = df["hostname"]
    else:
        node_candidates = df["source_ip"]
    df["node"] = node_candidates.map(canonical_node)
    if "region" not in df.columns:
        df["region"] = ds.region_code
    if "node_type" not in df.columns:
        df["node_type"] = "unknown"
    df["severity_code"] = pd.to_numeric(df.get("severity_code"), errors="coerce")
    msg = df.get("message", pd.Series("", index=df.index)).astype(str).str.lower()
    for kw in _keyword_columns():
        df[f"frr_kw_{kw}"] = msg.str.contains(kw, regex=False).astype(float)
    df["frr_event_count"] = 1.0
    agg_cols = ["frr_event_count", "severity_code"] + [f"frr_kw_{kw}" for kw in _keyword_columns()]
    out = df.groupby(["timestamp_bin", "region", "node", "node_type"], observed=True, dropna=False)[agg_cols].sum().reset_index()
    out["frr_mean_severity"] = out.pop("severity_code")
    return out


def _traffic_numeric_columns(df: pd.DataFrame) -> list[str]:
    cols: list[str] = []
    for c in df.columns:
        low = str(c).lower()
        if not pd.api.types.is_numeric_dtype(df[c]):
            continue
        if "_flow_" in low or low.startswith(("dns_", "web_", "auth_", "elephant_")):
            cols.append(c)
    return cols


def _agg_traffic_flow(ds: DatasetInfo, cfg: PipelineConfig) -> pd.DataFrame:
    df = read_table(ds, "traffic_flow_metrics")
    if df.empty:
        return pd.DataFrame()
    if "timestamp_utc" not in df.columns or "flow_type" not in df.columns:
        return pd.DataFrame()
    df = _prepare_time(df, "timestamp_utc", cfg.bin_minutes)
    num = _traffic_numeric_columns(df)
    if not num:
        return pd.DataFrame()
    df = df[["timestamp_bin", "flow_type"] + num].copy()
    # Cumulative counters must be converted to per-interval deltas before
    # aggregation.
    counter_cols = [c for c in num if c.lower().endswith(("_total", "_sum", "_count"))]
    for c in counter_cols:
        vals = pd.to_numeric(df[c], errors="coerce")
        df[c] = vals.groupby(df["flow_type"], observed=True).diff().clip(lower=0)
    keep = [c for c in num if df[c].notna().any()]
    if not keep:
        return pd.DataFrame()
    stats = df.groupby(["timestamp_bin", "flow_type"], observed=True, dropna=False)[keep].agg(["mean", "max"]).reset_index()
    stats.columns = [f"flow_{'_'.join(str(x) for x in col if str(x) != '')}" for col in stats.columns.values]
    id_cols = ["timestamp_bin"]
    value_cols = [c for c in stats.columns if c not in id_cols + ["flow_type"]]
    stats["flow_type"] = stats["flow_type"].astype(str).map(normalize_name)
    wide = stats.pivot_table(index=id_cols, columns="flow_type", values=value_cols, aggfunc="first", observed=True)
    wide.columns = [f"traffic_{b}_{a}" for a, b in wide.columns]
    wide = wide.reset_index()
    return wide


def _agg_netflow(ds: DatasetInfo, cfg: PipelineConfig) -> pd.DataFrame:
    path = ds.path("netflow_5tuple")
    if path is None:
        return pd.DataFrame()
    wanted = ["minute_utc", "region_code", "node_key", "node", "packets", "bytes", "flow_record_count"]
    try:
        df = read_table(ds, "netflow_5tuple", nrows=cfg.max_netflow_rows, usecols=lambda c: c in wanted)
    except Exception:
        df = read_table(ds, "netflow_5tuple", nrows=cfg.max_netflow_rows)
    if df.empty:
        return pd.DataFrame()
    if "minute_utc" not in df.columns:
        return pd.DataFrame()
    df = _prepare_time(df, "minute_utc", cfg.bin_minutes)
    if "node_key" in df.columns:
        df["node"] = df["node_key"].map(canonical_node)
    elif "node" in df.columns:
        df["node"] = df["node"].map(canonical_node)
    else:
        return pd.DataFrame()
    if "region_code" not in df.columns:
        df["region_code"] = ds.region_code
    df["region"] = df["region_code"]
    if "node_type" not in df.columns:
        df["node_type"] = "unknown"
    num = [c for c in ["packets", "bytes", "flow_record_count"] if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
    if not num:
        return pd.DataFrame()
    out = df.groupby(["timestamp_bin", "region", "node", "node_type"], observed=True, dropna=False)[num].sum().reset_index()
    out = out.rename(columns={c: f"netflow_{c}" for c in num})
    return out


def _time_features(df: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(df["timestamp_bin"], utc=True)
    out = df.copy()
    out["time_hour_sin"] = np.sin(2 * np.pi * ts.dt.hour / 24.0)
    out["time_hour_cos"] = np.cos(2 * np.pi * ts.dt.hour / 24.0)
    out["time_dow_sin"] = np.sin(2 * np.pi * ts.dt.dayofweek / 7.0)
    out["time_dow_cos"] = np.cos(2 * np.pi * ts.dt.dayofweek / 7.0)
    return out


def build_point_table(ds: DatasetInfo, cfg: PipelineConfig) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build a 5-minute point table for one dataset.

    Each row is one network element at one time bin.  Features are constructed
    from node, interface, routing, scrape, FRR, traffic-flow and netflow data.
    """
    log.info("building point table for %s", ds.name)
    node = _agg_node_metrics(ds, cfg)
    if node.empty:
        raise ValueError(f"no node_metrics usable in {ds.name}")

    point = node.copy()
    point["region"] = point["region"].astype(str)
    point["node"] = point["node"].map(canonical_node)
    point["node_type"] = point["node_type"].fillna("unknown").astype(str)

    for agg_fn in (_agg_interface_metrics, _agg_routing_metrics, _agg_scrape_health, _agg_frr, _agg_netflow):
        try:
            part = agg_fn(ds, cfg)
            if part.empty:
                continue
            if "node" in part.columns:
                part = part.copy()
                part["node"] = part["node"].map(canonical_node)
            if "region" in part.columns:
                part = part.copy()
                part["region"] = part["region"].astype(str)
            keys = ["timestamp_bin", "region", "node", "node_type"]
            if all(k in part.columns for k in keys):
                point = point.merge(part, on=keys, how="left")
            elif "timestamp_bin" in part.columns:
                point = point.merge(part, on="timestamp_bin", how="left")
        except Exception as exc:
            log.warning("failed to aggregate %s for %s: %s", agg_fn.__name__, ds.name, exc)

    point["network_element_id"] = [
        network_element_id(ds.region_code, n) for n in point["node"].astype(str)
    ]
    point = _time_features(point)
    point = point.sort_values(["network_element_id", "timestamp_bin"]).reset_index(drop=True)

    meta = {
        "dataset": ds.name,
        "region_code": ds.region_code,
        "processed_dir": str(ds.processed_dir),
        "rows": int(len(point)),
        "nodes": sorted(point["network_element_id"].dropna().unique().tolist()),
        "time_start": str(point["timestamp_bin"].min()),
        "time_end": str(point["timestamp_bin"].max()),
        "file_kinds": sorted(ds.files.keys()),
    }
    log.info(
        "point table %s rows=%d nodes=%d features=%d",
        ds.name,
        len(point),
        len(meta["nodes"]),
        len(point.columns),
    )
    return point, meta


def build_feature_matrix(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    cols = _numeric_columns(df)
    keep: list[str] = []
    for c in cols:
        s = pd.to_numeric(df[c], errors="coerce")
        if s.notna().any():
            keep.append(c)
    if not keep:
        raise ValueError("no numeric feature columns found")
    X = df[keep].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=np.float64)
    return X, keep


def top_feature_deviations(
    X_imputed: np.ndarray,
    feature_names: list[str],
    row_index: int,
    top_k: int = 6,
) -> list[dict[str, Any]]:
    if X_imputed.size == 0 or row_index >= X_imputed.shape[0]:
        return []
    med = np.nanmedian(X_imputed, axis=0)
    q1 = np.nanpercentile(X_imputed, 25, axis=0)
    q3 = np.nanpercentile(X_imputed, 75, axis=0)
    iqr = np.where(np.abs(q3 - q1) < 1e-12, 1.0, q3 - q1)
    row = X_imputed[row_index]
    z = np.abs(row - med) / iqr
    order = np.argsort(-np.nan_to_num(z, nan=-1.0))
    out = []
    for i in order:
        if len(out) >= top_k:
            break
        name = str(feature_names[int(i)])
        # ``_count`` columns mostly indicate scrape coverage, not a fault
        # mechanism; keep them out of the human/LLM evidence list.
        if name.endswith("_count"):
            continue
        if not np.isfinite(row[i]):
            continue
        out.append(
            {
                "name": name,
                "value": float(row[i]),
                "robust_z": float(z[i]) if np.isfinite(z[i]) else None,
            }
        )
    return out
