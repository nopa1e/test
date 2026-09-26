"""Predictive evidence: which changes can actually be *explained* (spec 5.7).

For a candidate edge ``A -> B`` the useful question is not "did B move" but
"did B move **more than A already explains**".  A victim's excursion is
predictable from the upstream element that caused it; a root cause's is not.

    existing   each node's own anomaly magnitude -- causality implied by
               nothing at all (defect D1)
    F adds     lagged cross-correlation plus a linear residual, giving
               ``normal_predictability`` / ``expected_change`` /
               ``actual_change`` / ``excess_change`` per directed pair

Spec 5.7 puts an explicit ceiling on the method: lagged cross-correlation with
a linear residual, **not** PCMCI / LiNGAM / RCD.  With ~80 network elements and
a static topology, causal-discovery packages would be learning a graph that is
already known, from far too little data.

Acceptance rules implemented here:
* fit on the **normal** window and predict the incident window -- never fit on
  the incident itself;
* drop edges whose normal-window predictability is below ``min_predictability``
  instead of forcing a number out of noise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..dataset import DatasetInfo, canonical_node, read_table
from ..utils import get_logger
from .table_usage import TableUsage
from .values import clean_numeric, clean_text

log = get_logger(__name__)

_NODE_KEY_COLS = {"timestamp", "region", "node", "node_type"}


def _node_score_frame(ds: DatasetInfo, usage: TableUsage) -> pd.DataFrame:
    """Per-node, per-minute anomaly magnitude derived from ``node_metrics``.

    The magnitude is the largest absolute z-score across that node's metric
    columns relative to the node's own full-series median/std.  A single
    scalar per (node, minute) is what the lagged regression needs; using the
    per-column detail would multiply the pair count for no extra resolution.
    """
    df = read_table(ds, "node_metrics")
    if df.empty:
        return pd.DataFrame()

    metric_cols = [c for c in df.columns if c not in _NODE_KEY_COLS]
    df["timestamp"] = pd.to_datetime(clean_text(df["timestamp"]), errors="coerce", utc=True)
    df["node"] = clean_text(df["node"]).map(lambda n: canonical_node(n) if pd.notna(n) else "unknown")
    for col in metric_cols:
        df[col] = clean_numeric(df[col])

    scores = []
    for node, group in df.groupby("node", observed=True):
        group = group.dropna(subset=["timestamp"]).sort_values("timestamp")
        if group.empty:
            continue
        z_max = np.zeros(len(group))
        for col in metric_cols:
            values = group[col].to_numpy(dtype=float)
            finite = values[np.isfinite(values)]
            if finite.size < 3:
                continue
            mu = float(np.median(finite))
            sd = float(np.std(finite))
            if sd <= 1e-12:
                continue
            z = np.abs((values - mu) / sd)
            z[~np.isfinite(z)] = 0.0
            z_max = np.maximum(z_max, z)
        scores.append(pd.DataFrame({"node": node, "timestamp": group["timestamp"].to_numpy(), "score": z_max}))

    usage.record(
        "node_metrics",
        len(df),
        nodes=int(df["node"].nunique()),
        fields_used=metric_cols,
        time_range=[str(df["timestamp"].min()), str(df["timestamp"].max())],
    )
    if not scores:
        return pd.DataFrame()
    out = pd.concat(scores, ignore_index=True)
    return out


def _best_lag(a: np.ndarray, b: np.ndarray, max_lag: int):
    """Return (lag, r2) for the lag maximising the correlation of A(t-lag) -> B(t).

    A positive lag means A moves *before* B, which is the direction a
    propagation story requires.
    """
    best = (0, 0.0)
    n = min(len(a), len(b))
    for lag in range(0, max_lag + 1):
        if n - lag < 8:
            break
        x = a[: n - lag]
        y = b[lag:n]
        mask = np.isfinite(x) & np.isfinite(y)
        if mask.sum() < 8:
            continue
        x, y = x[mask], y[mask]
        if np.std(x) <= 1e-12 or np.std(y) <= 1e-12:
            continue
        r = float(np.corrcoef(x, y)[0, 1])
        if not np.isfinite(r):
            continue
        if abs(r) > abs(best[1]):
            best = (lag, r)
    lag, r = best
    return lag, r * r


def _fit_predict(
    a_normal: np.ndarray,
    b_normal: np.ndarray,
    a_incident: np.ndarray,
    b_incident: np.ndarray,
    lag: int,
):
    """Least-squares fit ``B ~ alpha + beta * A(-lag)`` on normal, predict incident.

    Only the *normal* window is used to estimate the coefficients; the incident
    window is predicted, never fitted (spec 5.7 acceptance).
    """
    if lag:
        x, y = a_normal[: len(a_normal) - lag], b_normal[lag:]
    else:
        x, y = a_normal, b_normal
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 8:
        return None
    x, y = x[mask], y[mask]
    if np.std(x) <= 1e-12:
        return None
    beta, alpha = np.polyfit(x, y, 1)

    if lag:
        xa = a_incident[: max(0, len(a_incident) - lag)]
        yb = b_incident[lag:] if len(b_incident) > lag else np.array([])
    else:
        xa, yb = a_incident, b_incident
    n = min(xa.size, yb.size)
    if n == 0:
        return None
    return alpha + beta * xa[:n], yb[:n]


def build_predictive_evidence(
    ds: DatasetInfo,
    incidents: list[dict],
    usage: TableUsage,
    *,
    adjacency: dict[str, set[str]] | None = None,
    max_lag_minutes: int = 5,
    min_predictability: float = 0.3,
    baseline_minutes: int = 120,
    max_pairs_per_incident: int = 60,
) -> dict:
    """Directed, predictability-filtered pair evidence for every incident."""
    frame = _node_score_frame(ds, usage)
    if frame.empty:
        return {"dataset": ds.name, "incidents": {}, "warnings": ["node_metrics empty"]}

    series: dict[str, pd.Series] = {
        node: group.set_index("timestamp")["score"].sort_index()
        for node, group in frame.groupby("node", observed=True)
    }

    out: dict[str, dict] = {}
    dropped_low_predictability = 0

    for incident in incidents:
        iid = str(incident.get("incident_id"))
        tr = incident.get("time_range") or {}
        start, end = pd.Timestamp(tr.get("start")), pd.Timestamp(tr.get("end"))
        if pd.isna(start) or pd.isna(end):
            continue
        b_start = start - pd.Timedelta(minutes=baseline_minutes)

        nodes = [canonical_node(n) for n in (incident.get("nodes") or [])]
        nodes = [n for n in nodes if n in series][:12]
        if len(nodes) < 2:
            continue

        pairs: list[tuple[str, str]] = []
        for a in nodes:
            for b in nodes:
                if a == b:
                    continue
                if adjacency is not None and b not in adjacency.get(a, set()):
                    continue
                pairs.append((a, b))
        if not pairs:  # no topology available: fall back to every ordered pair
            pairs = [(a, b) for a in nodes for b in nodes if a != b]
        pairs = pairs[:max_pairs_per_incident]

        findings: list[dict] = []
        for a, b in pairs:
            sa, sb = series[a], series[b]
            normal_idx = sa.loc[(sa.index >= b_start) & (sa.index < start)].index
            if len(normal_idx) < 10:
                continue
            a_norm = sa.reindex(normal_idx).to_numpy(dtype=float)
            b_norm = sb.reindex(normal_idx).to_numpy(dtype=float)
            inc_idx = sa.loc[(sa.index >= start) & (sa.index <= end)].index
            if len(inc_idx) < 3:
                continue
            a_inc = sa.reindex(inc_idx).to_numpy(dtype=float)
            b_inc = sb.reindex(inc_idx).to_numpy(dtype=float)

            lag, predictability = _best_lag(a_norm, b_norm, max_lag_minutes)
            if predictability < min_predictability:
                dropped_low_predictability += 1
                continue
            fitted = _fit_predict(a_norm, b_norm, a_inc, b_inc, lag)
            if fitted is None:
                continue
            expected, actual = fitted
            mask = np.isfinite(expected) & np.isfinite(actual)
            if mask.sum() < 3:
                continue
            expected, actual = expected[mask], actual[mask]
            findings.append(
                {
                    "source": a,
                    "target": b,
                    "lag_minutes": int(lag),
                    "normal_predictability": float(predictability),
                    "expected_change": float(np.nanmean(expected)),
                    "actual_change": float(np.nanmean(actual)),
                    "excess_change": float(np.nanmax(actual) - np.nanmax(expected)),
                }
            )

        if findings:
            # Strongest explanatory power first: those are the edges that make
            # the *source* look like a cause rather than a victim.
            findings.sort(key=lambda f: -f["excess_change"])
            out[iid] = {
                "pairs": findings[:30],
                "n_pairs_tested": len(pairs),
                "n_pairs_kept": len(findings),
                "min_predictability": min_predictability,
            }

    if dropped_low_predictability:
        log.info(
            "predictive evidence: dropped %d pair(s) below predictability %.2f",
            dropped_low_predictability,
            min_predictability,
        )
    return {
        "dataset": ds.name,
        "min_predictability": min_predictability,
        "max_lag_minutes": max_lag_minutes,
        "incidents": out,
    }
