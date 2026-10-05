"""Metric evidence: tables 1 (node_metrics) and 2 (interface_metrics).

Spec section 5.1.  What this adds over the existing pipeline:

    existing   one scalar ``combined_anomaly_score`` per point
    F adds     which metric moved, when, and by how much relative to that
               series' own baseline -- and, for interface_metrics, the
               ``interface_id`` the fault actually sits on (the old system
               never went below node level).

Baseline windows never include the incident itself (spec acceptance criterion).
Change point detection is a hand-rolled CUSUM so no new dependency is needed.

Missing values arrive as the literal ``NULL`` (16 cells in xian's node_metrics,
``interface_metrics.if_role`` on 76.7 % of rows) and are normalised through
:mod:`.values`, so the key columns cannot grow a fake ``"null"`` node or a
fourth ``if_role`` category.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..dataset import DatasetInfo, canonical_node, read_table
from ..utils import get_logger
from .table_usage import TableUsage
from .values import clean_numeric, clean_text, missing_cell_counts

log = get_logger(__name__)

#: node_metrics columns that are actually measurements (the rest are keys).
_NODE_KEY_COLS = {"timestamp", "region", "node", "node_type"}
_INTERFACE_KEY_COLS = {"timestamp", "region", "node", "node_type", "interface_id", "if_role"}

#: Deliberately kept small; a change point is only reported when the CUSUM
#: statistic actually crosses zero, otherwise it is None.
_CUSUM_DRIFT = 0.5


def _cusum_change_point(values: np.ndarray, baseline: np.ndarray, stamps) -> tuple[int | None, object]:
    """Return (index, timestamp) of the strongest upward shift, or (None, None)."""
    if values.size == 0:
        return None, None
    # ``stamps`` arrives as a pandas Series carrying the frame's original index,
    # so positional access must go through a numpy view -- ``stamps[idx]`` would
    # be a *label* lookup and blows up with KeyError.
    stamp_arr = stamps.to_numpy() if hasattr(stamps, "to_numpy") else np.asarray(stamps)
    mu = float(np.nanmean(baseline)) if baseline.size else 0.0
    sd = float(np.nanstd(baseline)) if baseline.size else 0.0
    if not np.isfinite(sd) or sd <= 1e-12:
        sd = 1.0
    z = (values - mu) / sd
    stat = np.cumsum(z - _CUSUM_DRIFT)
    idx = int(np.argmax(stat))
    if stat[idx] <= 0:
        return None, None
    return idx, stamp_arr[idx]


#: z 值的稳健尺度下界（基线中位量级的 1%）与截断上限。
_Z_CLIP = 200.0


def _robust_scale(b: np.ndarray, a: np.ndarray, b_med: float) -> float:
    """基线尺度的稳健估计，用于替代 ``np.std``。

    为什么必须换掉 ``np.std``
    ------------------------
    对**基线过于安静**的指标，std 会退化到接近 0，而 ``z = (peak - b_med) / sigma``
    随即爆炸。实测（2026-10-04，8 区域 metric_evidence，按 incident 取最大 z）：

        指标                  中位     p99        最大
        disk_read_rate        0.00     0.1    27,482,521
        disk_write_rate       0.49    11.4       318,760
        inode_used_ratio      0.00    10.5       119,652
        cpu_usage             0.54    26.9           593

    ``disk_read_rate`` 的 p99 只有 0.1、最大值却是 2748 万——跨 9 个数量级。
    原防护 ``np.std(b) if np.std(b) > 1e-12 else 1.0`` **阈值太低**：
    std 只要大于 1e-12 就放行，1e-5 的 std 照样算出上千万的 z。

    影响面：``_resource_subtype``（扫全部指标按 z 挑最强子族 -> 必然挑中
    disk_read_rate -> 判成 disk_io_pressure）、``local_anomaly``（权重 0.20）、
    以及喂给 LLM 的全部证据。

    做法（两处稳健化）
    ------------------
    1. **尺度用 MAD**：中位绝对偏差 x1.4826（正态下等于 sigma），
       对极端值不敏感——不会因基线里有几个尖峰把尺度撑大，也不会因基线安静塌到 0。
    2. **下界四个候选取最大**：只用基线中位数会失效——``disk_read_rate`` 的基线
       **恒为 0**，MAD 也是 0，于是下界退化成一个无意义的极小值、z 仍是天文数字。
       故同时看基线 p95 与**事故期自身的量级**，让"基线全零但事故期非零"这种情形
       拿到一个有物理意义的尺度。
    3. **截断 200 作保险**：只挡真正的退化情形，不参与正常判别
       （实测正常指票的 z 在个位到几十）。
    """
    cands = [
        abs(float(b_med)) * 0.01,
        abs(float(np.percentile(b, 95))) * 0.01 if b.size else 0.0,
        abs(float(np.median(a))) * 0.01 if a.size else 0.0,
        abs(float(np.percentile(a, 95))) * 0.01 if a.size else 0.0,
    ]
    floor = max(max(cands), 1e-12)
    if b.size < 2:
        return floor
    mad = float(np.median(np.abs(b - b_med))) * 1.4826
    p5, p95 = np.percentile(b, [5, 95])
    span_sigma = float(p95 - p5) / 3.29
    return max(mad, span_sigma, floor)


def _series_stats(baseline: pd.Series, incident: pd.Series, base_stamps, inc_stamps) -> dict | None:
    b = baseline.to_numpy(dtype=float)
    a = incident.to_numpy(dtype=float)
    b = b[np.isfinite(b)]
    a = a[np.isfinite(a)]
    if b.size == 0 or a.size == 0:
        return None
    base_stamps = base_stamps.to_numpy() if hasattr(base_stamps, "to_numpy") else np.asarray(base_stamps)
    inc_stamps = inc_stamps.to_numpy() if hasattr(inc_stamps, "to_numpy") else np.asarray(inc_stamps)

    b_med = float(np.median(b))
    b_p95 = float(np.percentile(b, 95))
    peak = float(np.max(a))
    trough = float(np.min(a))
    # Relative change uses the baseline median; guard against a zero baseline
    # (many interface counters are legitimately 0 for most of the day).
    # 分母退化保护：原实现在 |b_med| 与 |b_p95| 都趋近 0 时退化成 1.0，
    # 于是 relative_change 变成指标的**原始量级**——速率类指标可达千万
    # （探针实测出现过 rel=+27482521.60），而 local_anomaly 正是取 max(|severity|)。
    # 改为用事故期自身的量级兜底：基线全零时，"事故期有值"本身就是相对变化 ~1。
    denom = abs(b_med) if abs(b_med) > 1e-9 else (
        abs(b_p95) if abs(b_p95) > 1e-9 else max(
            abs(float(np.median(a))), abs(float(np.percentile(a, 95))), 1.0))
    relative_change = (peak - b_med) / denom
    drop_ratio = (b_med - trough) / denom

    idx, changed_at = _cusum_change_point(a, b, inc_stamps)
    if idx is None:
        # Fall back to the single largest deviation from the baseline median.
        dev = np.abs(a - b_med)
        idx = int(np.argmax(dev))

    return {
        "baseline_median": b_med,
        "baseline_p95": b_p95,
        "incident_peak": peak,
        "incident_min": trough,
        "relative_change": float(relative_change),
        "drop_ratio": float(drop_ratio),
        # 见 _robust_scale 的说明：std 在基线安静时会退化成 0，
        # 使 z 爆炸到千万量级。改用 MAD 并截断作保险。
        "peak_z": float(np.clip((peak - b_med) / _robust_scale(b, a, b_med),
                                -_Z_CLIP, _Z_CLIP)),
        "first_anomaly_time": _iso(inc_stamps[idx]) if idx is not None and idx < len(inc_stamps) else None,
        "peak_time": _iso(inc_stamps[int(np.argmax(a))]),
        "change_point": _iso(changed_at) if changed_at is not None else None,
        "severity": float(abs(relative_change)),
    }


def _iso(value) -> str | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).isoformat()
    except Exception:  # pragma: no cover - defensive
        return str(value)


def _windows(incident: dict, node: str, baseline_minutes: int, gap_minutes: int):
    """Return (incident_start, incident_end, baseline_start, baseline_end)."""
    tr = incident.get("time_range") or {}
    start = pd.Timestamp(tr.get("start"))
    end = pd.Timestamp(tr.get("end"))
    for ep in incident.get("episodes") or []:
        if canonical_node(ep.get("network_element_id")) and str(ep.get("network_element_id", "")).endswith(node):
            start = pd.Timestamp(ep.get("start_time") or start)
            end = pd.Timestamp(ep.get("end_time") or end)
            break
    base_end = start - pd.Timedelta(minutes=gap_minutes)
    base_start = base_end - pd.Timedelta(minutes=baseline_minutes)
    return start, end, base_start, base_end


def _clean_node_column(series: pd.Series) -> pd.Series:
    """Canonicalise node names without inventing a ``"null"`` node."""
    return clean_text(series).map(lambda n: canonical_node(n) if pd.notna(n) else "unknown")


def _prepare_node_metrics(ds: DatasetInfo, usage: TableUsage) -> pd.DataFrame:
    df = read_table(ds, "node_metrics")
    if df.empty:
        return df
    metric_cols = [c for c in df.columns if c not in _NODE_KEY_COLS]
    missing = missing_cell_counts(df, metric_cols)
    df["timestamp"] = pd.to_datetime(clean_text(df["timestamp"]), errors="coerce", utc=True)
    df["node"] = _clean_node_column(df["node"])
    for c in metric_cols:
        df[c] = clean_numeric(df[c])
    usage.record(
        "node_metrics",
        len(df),
        nodes=int(df["node"].nunique()),
        fields_used=metric_cols,
        time_range=[_iso(df["timestamp"].min()), _iso(df["timestamp"].max())],
        missing_cells=missing or None,
    )
    return df


def _prepare_interface_metrics(ds: DatasetInfo, usage: TableUsage) -> pd.DataFrame:
    df = read_table(ds, "interface_metrics")
    if df.empty:
        return df
    metric_cols = [c for c in df.columns if c not in _INTERFACE_KEY_COLS]
    missing = missing_cell_counts(df, metric_cols + ["if_role"])
    df["timestamp"] = pd.to_datetime(clean_text(df["timestamp"]), errors="coerce", utc=True)
    df["node"] = _clean_node_column(df["node"])
    df["interface_id"] = clean_text(df["interface_id"]).fillna("")
    if "if_role" in df.columns:
        df["if_role"] = clean_text(df["if_role"])
    for c in metric_cols:
        df[c] = clean_numeric(df[c])
    usage.record(
        "interface_metrics",
        len(df),
        interfaces=int(df["interface_id"].nunique()),
        fields_used=metric_cols,
        missing_cells=missing or None,
    )
    return df


def build_metric_evidence(
    ds: DatasetInfo,
    incidents: list[dict],
    usage: TableUsage,
    *,
    baseline_minutes: int = 60,
    gap_minutes: int = 5,
    max_nodes_per_incident: int = 10,
) -> dict:
    """Build node- and interface-level metric evidence for every incident."""
    node_df = _prepare_node_metrics(ds, usage)
    iface_df = _prepare_interface_metrics(ds, usage)
    if node_df.empty:
        return {"dataset": ds.name, "incidents": {}}

    node_cols = [c for c in node_df.columns if c not in _NODE_KEY_COLS]
    iface_cols = (
        [c for c in iface_df.columns if c not in _INTERFACE_KEY_COLS] if not iface_df.empty else []
    )
    node_groups = {n: g for n, g in node_df.groupby("node", observed=True)}
    iface_groups = (
        {k: g for k, g in iface_df.groupby(["node", "interface_id"], observed=True)}
        if not iface_df.empty
        else {}
    )

    out: dict[str, dict] = {}
    for incident in incidents:
        iid = str(incident.get("incident_id"))
        nodes = [canonical_node(n) for n in (incident.get("nodes") or [])][:max_nodes_per_incident]
        per_node: dict[str, dict] = {}
        for node in nodes:
            grp = node_groups.get(node)
            if grp is None or grp.empty:
                continue
            start, end, b_start, b_end = _windows(incident, node, baseline_minutes, gap_minutes)
            inc_win = grp[(grp["timestamp"] >= start) & (grp["timestamp"] <= end)]
            base_win = grp[(grp["timestamp"] >= b_start) & (grp["timestamp"] <= b_end)]
            if inc_win.empty or base_win.empty:
                continue

            metrics: dict[str, dict] = {}
            for col in node_cols:
                stats = _series_stats(base_win[col], inc_win[col], base_win["timestamp"], inc_win["timestamp"])
                if stats is not None:
                    metrics[col] = stats

            interfaces: dict[str, dict] = {}
            for (inode, iid_name), igrp in iface_groups.items():
                if inode != node:
                    continue
                i_inc = igrp[(igrp["timestamp"] >= start) & (igrp["timestamp"] <= end)]
                i_base = igrp[(igrp["timestamp"] >= b_start) & (igrp["timestamp"] <= b_end)]
                if i_inc.empty or i_base.empty:
                    continue
                im: dict[str, dict] = {}
                for col in iface_cols:
                    stats = _series_stats(i_base[col], i_inc[col], i_base["timestamp"], i_inc["timestamp"])
                    if stats is not None:
                        im[col] = stats
                if im:
                    interfaces[str(iid_name)] = im

            # Rank the node's own metrics so the strongest movers are readable.
            ranked = sorted(metrics.items(), key=lambda kv: -kv[1]["severity"])
            per_node[node] = {
                "incident_window": [_iso(start), _iso(end)],
                "baseline_window": [_iso(b_start), _iso(b_end)],
                "metrics": metrics,
                "top_metrics": [{"metric": k, **v} for k, v in ranked[:5]],
                "interfaces": interfaces,
            }
        if per_node:
            out[iid] = {"nodes": per_node}

    return {"dataset": ds.name, "incidents": out}
