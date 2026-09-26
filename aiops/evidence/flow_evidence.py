"""Flow evidence: tables 7 (traffic_flow_metrics) and 6 (netflow_5tuple).

Spec section 5.5 -- the *trace replacement*.  The dataset has no ``trace_id``
(spec 0.1), so the directed calling edges come from the active probes instead.

What this adds over the existing pipeline:

    existing   nothing.  ``features.py`` read neither table: netflow was
               collapsed into per-node byte totals for the point table, and
               ``traffic_flow_metrics`` was not read at all.
    F adds     1. **directed, service-level edges** -- one row per
                  (source_region:source_ip -> target_region:target_domain) with
                  ``requests / error_rate / timeout / duration_p95`` deltas
                  against that edge's own pre-incident baseline.
               2. **cross-region propagation.**  xian's probe measures
                  shanghai / wuhan / beida services, so "is xian's own service
                  healthy" is answered by the *other* regions' probes.  Eight
                  regions together form one cross-region propagation net.
               3. **real forwarding relations** plus ``first_seen`` / ``last_seen``
                  at millisecond resolution -- the only sub-minute time source
                  in the whole dataset, and the input F6 needs to refine
                  incident boundaries.  Those two columns are reproduced
                  verbatim (never rebinned to the 1-minute grid).

Measured structure of ``traffic_flow_metrics`` (xian, verified against the raw
CSV header + rows):

    keys      id, timestamp_utc, prometheus_sample_time_utc, series_key,
              flow_type, source_region, source_ip, target_region,
              target_domain, protocol
    metrics   ``{flow_type}_flow_{suffix}`` for flow_type in
              dns / web / auth / elephant.  dns, web and auth carry 31 counters
              each (including 11 latency histogram buckets); elephant carries 23
              -- it has throughput_bps / retransmits_total / loss_rate /
              jitter_seconds instead of histogram buckets.
    shape     one row fills **only its own flow_type's column block**; every
              other block is the literal ``\\N``.  Grouping by flow_type first
              and only then reading that block (what this module does) is what
              keeps the ``\\N`` markers from turning into numbers.

Deliberate separation: ``elephant`` is the bulk-transfer probe and is reported
under ``elephant_edges``, never mixed into the web/dns/auth edge list (spec 5.5
acceptance criterion).  Measured on xian: the three service probes have exactly
one target each (web -> web01.shanghai, dns -> dns.beida, auth -> auth01.wuhan),
while elephant reaches **21 distinct** ``elephant0{1,2,3}.<region>`` targets --
three elephant instances against eight regions, of which 21 of the 24
combinations appear.  Its 744 rows are sparse (median gap 1 minute, 75th
percentile 46 minutes), so a 10-minute incident window usually contains no
elephant sample at all; that is a property of the data, not of this module.

Netflow reading policy (spec 5.5: "must be read with a row cap, never a full
pandas load"):

    windowed=True (default)  stream the file in ``netflow_chunk_rows`` chunks
                             (constant memory, never a full load) and keep only
                             the rows that fall inside an incident or baseline
                             window, stopping at ``max_netflow_rows`` kept rows.
    windowed=False           plain head sample of ``max_netflow_rows`` rows --
                             fast, but it only ever sees the start of the file.

``rows_scanned`` / ``rows_kept`` / ``truncated`` are reported either way, and
the returned dict says which mode ran.  A head sample of 250k rows covers only
the first ~2.4 h of a 14-day, 35.6M-row file, so the default is the windowed
scan; it costs a full sequential read per region (~7 GB), which is why the
switch exists.

Artifact size (measured on xian, head mode, three incidents): a run of 2,195
incidents -- xian's real count -- produces

    edges/incident=120, curve points=180     154 KB/incident   ~323 MiB
    edges/incident=40,  curve points=60       77 KB/incident   ~161 MiB   <- defaults

:mod:`.f_stages` reads this artifact whole, so lower
``max_netflow_edges_per_incident`` if 160 MiB is too much for the later stages.

TODO(verify) -- things only the remaining regions' data can settle:

    # TODO(verify): the 21 elephant targets and the 744 elephant rows are
    # measured on **xian only**.  Confirm the other seven regions also expose
    # elephant0{1,2,3}.<region> (spec 5.5 says "8 regional targets"; xian shows
    # three instances x eight regions, 21 present of 24 possible).
    # TODO(verify): ``minute_utc`` is the flow's *bucket* while ``first_seen``
    # can predate it (xian's first bucket 04:00:00 starts at 03:59:01.541).
    # F6 boundary refinement must decide whether an episode start may sit before
    # the bucket it was observed in; no region other than xian is checked.
    # TODO(verify): the data-plane edge key omits ``region_code`` because a
    # per-region file carries a single region.  If the eight regions are ever
    # pooled into one frame, (node, interface, src, dst, proto) will collide
    # across regions and the key must gain region_code.
    # TODO(verify): the default windowed scan reads the whole ~7 GB file once per
    # region; total wall clock for eight regions is unmeasured (xian's head mode
    # is ~4 s for 250k rows, the windowed early-stop path ~3 s, but a complete
    # sweep was never timed here).
    # TODO(verify): incidents in the first ``baseline_minutes`` of the capture
    # have a short baseline (xian: 61 of the nominal 60+ minutes only because the
    # capture starts at 04:00).  Decide whether such incidents deserve a
    # forward-looking baseline instead of a truncated one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..dataset import DatasetInfo, canonical_node, network_element_id, read_table
from ..utils import get_logger
from .table_usage import TableUsage
from .values import clean_numeric, clean_text, missing_cell_counts

log = get_logger(__name__)

#: The bulk-transfer probe.  Handled separately from the service probes.
ELEPHANT_FLOW_TYPE = "elephant"

#: traffic_flow_metrics key columns (measured header, see module docstring).
_TRAFFIC_KEY_COLS = (
    "timestamp_utc",
    "flow_type",
    "source_region",
    "source_ip",
    "target_region",
    "target_domain",
    "protocol",
)

#: ``{flow_type}_flow_{suffix}`` -> logical name used in the output.
_TRAFFIC_SUFFIXES: dict[str, str] = {
    "requests": "requests_total",
    "errors": "error_total",
    "timeouts": "batches_timeout_total",
    "failed": "batches_failed_total",
    "duration_p95": "latency_p95_seconds",
    "observed_qps": "observed_qps",
    "active": "active",
}

#: Only the elephant probe exports these.
_ELEPHANT_SUFFIXES: dict[str, str] = {
    "throughput_bps": "throughput_bps",
    "retransmits": "retransmits_total",
    "loss_rate": "loss_rate",
    "jitter": "jitter_seconds",
}

#: Columns this module needs from netflow_5tuple.
_NETFLOW_WANTED = frozenset({
    "minute_utc",
    "region_code",
    "node_key",
    "node",
    "interface_id",
    "protocol",
    "src_addr",
    "dst_addr",
    "packets",
    "bytes",
    "flow_record_count",
    "first_seen",
    "last_seen",
})

#: The edge key of the data plane.  Direction is part of the key: (src, dst)
#: and (dst, src) are two different edges.
_NETFLOW_EDGE_KEYS = ("node", "interface_id", "src_addr", "dst_addr", "protocol")


# --------------------------------------------------------------------------- helpers
def _note(warnings: list[str], message: str) -> None:
    """Record a diagnostic once (warnings stay readable)."""
    if message not in warnings:
        warnings.append(message)


def _iso(value) -> str | None:
    """Millisecond-precision UTC ISO string, or None."""
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return str(value)
    if pd.isna(stamp):
        return None
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.isoformat(timespec="milliseconds")


def _as_utc(value) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp):
        return None
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(clean_text(series), errors="coerce", utc=True)


def _minute_of(stamp) -> int:
    naive = pd.Timestamp(stamp)
    naive = naive.tz_convert("UTC").tz_localize(None) if naive.tzinfo else naive
    return int(naive.value // 60_000_000_000)


def _stamp_minutes(stamps: pd.Series) -> np.ndarray:
    """tz-aware timestamps -> int64 minutes since the epoch; NaT -> -1."""
    if len(stamps) == 0:
        return np.zeros(0, dtype="int64")
    naive = stamps.dt.tz_convert("UTC").dt.tz_localize(None)
    values = naive.to_numpy(dtype="datetime64[m]")
    return np.where(np.isnat(values), -1, values.astype("int64"))


def _merge_intervals(lo: np.ndarray, hi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Sort and coalesce [lo, hi] minute intervals so window tests stay O(log n).

    xian produces 2,195 incidents in one run; testing every chunk against every
    raw interval would be 4,390 comparisons per chunk.
    """
    if lo.size == 0:
        return lo, hi
    order = np.argsort(lo, kind="stable")
    lo, hi = lo[order], hi[order]
    merged_lo = [int(lo[0])]
    merged_hi = [int(hi[0])]
    for a, b in zip(lo[1:], hi[1:]):
        if int(a) <= merged_hi[-1]:
            merged_hi[-1] = max(merged_hi[-1], int(b))
        else:
            merged_lo.append(int(a))
            merged_hi.append(int(b))
    return np.asarray(merged_lo, dtype="int64"), np.asarray(merged_hi, dtype="int64")


def _hit_mask(values: np.ndarray, merged_lo: np.ndarray, merged_hi: np.ndarray) -> np.ndarray:
    """Boolean mask: which minute values fall inside any merged interval."""
    out = np.zeros(values.shape, dtype=bool)
    if merged_lo.size == 0 or values.size == 0:
        return out
    idx = np.searchsorted(merged_lo, values, side="right") - 1
    ok = (idx >= 0) & (values >= 0)
    out[ok] = values[ok] <= merged_hi[idx[ok]]
    return out


def _finite_median(frame: pd.DataFrame, column: str | None) -> float | None:
    if column is None or column not in frame.columns:
        return None
    series = frame[column]
    if isinstance(series, pd.DataFrame):  # duplicated label, keep the last
        series = series.iloc[:, -1]
    # ``errors="coerce"`` keeps an unexpected missing marker from aborting the run.
    arr = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    arr = arr[np.isfinite(arr)]
    return float(np.median(arr)) if arr.size else None


def _delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return float(after - before)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) <= 1e-9:
        return None
    return float(numerator / denominator)


def _counter_like(column: str) -> bool:
    """Prometheus counters are monotonic; gauges are read as they are."""
    return column.endswith(("_total", "_sum", "_count"))


def _thin(points: list, limit: int) -> tuple[list, bool]:
    if limit <= 0 or len(points) <= limit:
        return points, False
    idx = np.unique(np.linspace(0, len(points) - 1, limit).round().astype(int))
    return [points[int(i)] for i in idx], True


def _windows_for_incident(
    incident: dict,
    baseline_minutes: int,
    gap_minutes: int,
    warnings: list[str],
) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp] | None:
    """(incident_start, incident_end, baseline_start, baseline_end).

    Spec acceptance criterion: the baseline must never be taken from inside the
    incident window.  The baseline ends ``gap_minutes`` before the incident
    starts and reaches back from there, so the two windows cannot overlap.
    """
    tr = incident.get("time_range") or {}
    start = _as_utc(tr.get("start"))
    end = _as_utc(tr.get("end"))
    if start is None or end is None:
        _note(
            warnings,
            f"incident {incident.get('incident_id')}: time_range missing or unparsable -> skipped",
        )
        return None
    if end < start:
        start, end = end, start
    base_end = start - pd.Timedelta(minutes=gap_minutes)
    base_start = base_end - pd.Timedelta(minutes=baseline_minutes)
    return start, end, base_start, base_end


# --------------------------------------------------------------------------- table 7
def _prepare_traffic_flow(
    ds: DatasetInfo, usage: TableUsage, warnings: list[str]
) -> pd.DataFrame:
    """Read + normalise traffic_flow_metrics, and derive its counter rates."""
    df = read_table(ds, "traffic_flow_metrics")
    if df.empty:
        _note(warnings, "traffic_flow_metrics: no rows readable -> probe edges unavailable")
        return df

    missing_cols = [c for c in _TRAFFIC_KEY_COLS if c not in df.columns]
    if missing_cols:
        _note(warnings, f"traffic_flow_metrics: missing columns {missing_cols}")
    if "timestamp_utc" not in df.columns or "flow_type" not in df.columns:
        _note(warnings, "traffic_flow_metrics: timestamp_utc/flow_type absent -> table unusable")
        return pd.DataFrame()

    df["timestamp"] = _to_utc(df["timestamp_utc"])
    df = df[df["timestamp"].notna()].copy()
    if df.empty:
        _note(warnings, "traffic_flow_metrics: no parsable timestamps")
        return df

    df["flow_type"] = clean_text(df["flow_type"]).str.lower()
    for col in ("source_region", "source_ip", "target_region", "target_domain", "protocol"):
        df[col] = clean_text(df[col]) if col in df.columns else pd.NA
    # target_region is occasionally blank; the domain carries the same fact
    # ("dns.beida.aiops.local" -> beida).
    if df["target_region"].isna().any():
        guess = (
            df["target_domain"].astype("string").str.split(".").str[1]
            if df["target_domain"].notna().any()
            else pd.Series(pd.NA, index=df.index, dtype="string")
        )
        df["target_region"] = df["target_region"].fillna(guess)

    flow_types = sorted(t for t in df["flow_type"].dropna().unique().tolist())
    metric_columns = [
        c for c in df.columns if any(c.startswith(f"{ft}_flow_") for ft in flow_types)
    ]
    # Counts the \N flood: every row fills only its own flow_type's block, so the
    # other blocks are markers -- measured on xian, 4,485,813 marker cells spread
    # over 116 columns of 52,674 rows.
    missing_cells = missing_cell_counts(df, list(_TRAFFIC_KEY_COLS) + metric_columns)
    df["ts_min"] = _stamp_minutes(df["timestamp"])
    # Direction is part of the edge key: (a -> b) and (b -> a) are two edges.
    df["edge_key"] = (
        df["flow_type"].astype("string")
        + "|" + df["source_ip"].astype("string")
        + "|" + df["target_domain"].astype("string")
        + "|" + df["protocol"].astype("string")
    )
    used_fields: list[str] = []
    metric_blocks: dict[str, pd.Series] = {}
    for ft in flow_types:
        prefix = f"{ft}_flow_"
        cols = [c for c in df.columns if c.startswith(prefix)]
        if not cols:
            _note(
                warnings,
                f"traffic_flow_metrics: flow_type {ft!r} has no {prefix}* columns",
            )
            continue
        used_fields.extend(cols)
        for col in cols:
            # Every other flow_type's block is the literal \N -> NaN here.
            metric_blocks[col] = clean_numeric(df[col])

    rate_blocks: dict[str, pd.Series] = {}
    # Rates must be per edge and in time order: counters grow per minute and a
    # single missing minute would otherwise inflate every delta.
    for ft in flow_types:
        cols = [c for c in metric_blocks if c.startswith(f"{ft}_flow_")]
        if not cols:
            continue
        rows = df.index[df["flow_type"] == ft]
        if len(rows) == 0:
            continue
        block = pd.DataFrame(
            {col: metric_blocks[col].loc[rows] for col in cols},
            index=rows,
        )
        block["edge_key"] = df.loc[rows, "edge_key"]
        block["ts_min"] = df.loc[rows, "ts_min"]
        block = block.sort_values("ts_min")
        grouped = block.groupby("edge_key", observed=True, sort=False)
        dt = grouped["ts_min"].diff().astype("float64")
        for col in cols:
            if not _counter_like(col):
                continue
            # A negative delta is a counter reset, not traffic; drop the sample.
            rate = (grouped[col].diff() / dt.where(dt > 0)).where(lambda s: s >= 0)
            rate_blocks[col + "_rate"] = rate.reindex(df.index)

    extra = {**metric_blocks, **rate_blocks}
    if extra:
        # Replace, do not append: concatenating a cleaned column next to the raw
        # one of the same name would leave duplicate labels, and ``frame[name]``
        # would then hand back a two-column DataFrame whose first column still
        # holds the literal \N markers.
        df = df.drop(columns=list(extra), errors="ignore")
        df = pd.concat([df, pd.DataFrame(extra, index=df.index)], axis=1)
    df = df[df["edge_key"].notna() & df["source_ip"].notna()].copy()
    log.info(
        "%s: traffic_flow_metrics rows=%d flow_types=%s",
        ds.name, len(df), flow_types,
    )
    usage.record(
        "traffic_flow_metrics",
        len(df),
        flow_types=flow_types,
        edges=int(df["edge_key"].nunique()) if not df.empty else None,
        source_ips=sorted(df["source_ip"].dropna().astype(str).unique().tolist()),
        target_domains=int(df["target_domain"].nunique()),
        fields_used=used_fields,
        time_range=[_iso(df["timestamp"].min()), _iso(df["timestamp"].max())],
        missing_cells=missing_cells or None,
    )
    return df


def _probe_edge_payload(
    grp: pd.DataFrame,
    flow_type: str,
    windows: tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp],
    elephant: bool,
) -> dict | None:
    """One directed edge with its baseline/incident deltas."""
    start, end, b_start, b_end = windows
    base = grp[(grp["timestamp"] >= b_start) & (grp["timestamp"] <= b_end)]
    inc = grp[(grp["timestamp"] >= start) & (grp["timestamp"] <= end)]
    if inc.empty:
        return None

    def column(logical: str) -> str | None:
        name = f"{flow_type}_flow_{_TRAFFIC_SUFFIXES[logical]}"
        return name if name in grp.columns else None

    def rate_column(logical: str) -> str | None:
        name = column(logical)
        return (name + "_rate") if name and (name + "_rate") in grp.columns else None

    req_base = _finite_median(base, rate_column("requests"))
    req_inc = _finite_median(inc, rate_column("requests"))
    err_base = _finite_median(base, rate_column("errors"))
    err_inc = _finite_median(inc, rate_column("errors"))
    to_base = _finite_median(base, rate_column("timeouts"))
    to_inc = _finite_median(inc, rate_column("timeouts"))
    p95_base = _finite_median(base, column("duration_p95"))
    p95_inc = _finite_median(inc, column("duration_p95"))

    err_rate_base = _ratio(err_base, req_base)
    err_rate_inc = _ratio(err_inc, req_inc)

    first_error = None
    err_col = rate_column("errors")
    if err_col is not None and not inc.empty:
        hit = inc[(inc[err_col] > 0) & inc[err_col].notna()]
        if not hit.empty:
            first_error = _iso(hit.sort_values("timestamp")["timestamp"].iloc[0])

    row = grp.iloc[0]
    payload = {
        # --- spec 5.5 field set, per directed edge
        "source_region": _text_or_none(row.get("source_region")),
        "source_ip": _text_or_none(row.get("source_ip")),
        "target_region": _text_or_none(row.get("target_region")),
        "target_domain": _text_or_none(row.get("target_domain")),
        "flow_type": flow_type,
        "protocol": _text_or_none(row.get("protocol")),
        "requests_delta": _delta(req_inc, req_base),
        "error_rate_delta": _delta(err_rate_inc, err_rate_base),
        "timeout_delta": _delta(to_inc, to_base),
        "duration_p95_delta": _delta(p95_inc, p95_base),
        "first_error_time": first_error,
        # --- direction, spelled out so no consumer can treat the edge as undirected
        "direction": (
            f"{_text_or_none(row.get('source_region'))}:{_text_or_none(row.get('source_ip'))}"
            f" -> {_text_or_none(row.get('target_region'))}:{_text_or_none(row.get('target_domain'))}"
        ),
        "directed": True,
        # --- the numbers the deltas were computed from
        "requests_per_min_baseline": req_base,
        "requests_per_min_incident": req_inc,
        "error_rate_baseline": err_rate_base,
        "error_rate_incident": err_rate_inc,
        "timeout_per_min_baseline": to_base,
        "timeout_per_min_incident": to_inc,
        "duration_p95_baseline": p95_base,
        "duration_p95_incident": p95_inc,
        "samples_baseline": int(len(base)),
        "samples_incident": int(len(inc)),
        "incident_window": [_iso(start), _iso(end)],
        "baseline_window": [_iso(b_start), _iso(b_end)],
    }
    if elephant:
        for logical in _ELEPHANT_SUFFIXES:
            name = f"{flow_type}_flow_{_ELEPHANT_SUFFIXES[logical]}"
            if name not in grp.columns:
                continue
            base_value = _finite_median(base, name)
            inc_value = _finite_median(inc, name)
            payload[f"{logical}_baseline"] = base_value
            payload[f"{logical}_incident"] = inc_value
            payload[f"{logical}_delta"] = _delta(inc_value, base_value)
    return payload


def _text_or_none(value) -> str | None:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    if pd.isna(value):
        return None
    return str(value)


def _traffic_flow_for_incident(
    df: pd.DataFrame,
    windows: tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp],
    *,
    max_edges: int,
) -> dict:
    """Service-probe edges for one incident, elephant kept apart."""
    start, end, b_start, b_end = windows
    reachable = df[
        ((df["timestamp"] >= b_start) & (df["timestamp"] <= b_end))
        | ((df["timestamp"] >= start) & (df["timestamp"] <= end))
    ]
    edges: list[dict] = []
    elephant_edges: list[dict] = []
    if not reachable.empty:
        for _key, grp in reachable.groupby("edge_key", observed=True, sort=False):
            flow_type = str(grp["flow_type"].iloc[0])
            is_elephant = flow_type == ELEPHANT_FLOW_TYPE
            payload = _probe_edge_payload(grp, flow_type, windows, is_elephant)
            if payload is None:
                continue
            (elephant_edges if is_elephant else edges).append(payload)

    def _rank(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda e: -(e.get("requests_per_min_incident") or 0.0))

    edges = _rank(edges)[:max_edges]
    elephant_edges = _rank(elephant_edges)[:max_edges]
    return {
        "edges": edges,
        "elephant_edges": elephant_edges,
        "n_edges": len(edges),
        "n_elephant_edges": len(elephant_edges),
        "flow_types": sorted({e["flow_type"] for e in edges + elephant_edges}),
    }


# --------------------------------------------------------------------------- table 6
def _clean_netflow_chunk(chunk: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    """Normalise one raw chunk into the columns this module aggregates on."""
    for col in ("minute_utc", "src_addr", "dst_addr"):
        if col not in chunk.columns:
            _note(warnings, f"netflow_5tuple: required column {col!r} missing")
            return pd.DataFrame()
    out = pd.DataFrame(index=chunk.index)
    out["timestamp"] = _to_utc(chunk["minute_utc"])
    out["ts_min"] = _stamp_minutes(out["timestamp"])
    node_source = chunk["node_key"] if "node_key" in chunk.columns else chunk.get("node")
    if node_source is None:
        _note(warnings, "netflow_5tuple: neither node_key nor node present")
        return pd.DataFrame()
    out["node"] = clean_text(node_source).map(
        lambda n: canonical_node(n) if pd.notna(n) else "unknown"
    )
    out["interface_id"] = (
        clean_text(chunk["interface_id"]).fillna("") if "interface_id" in chunk.columns
        else ""
    )
    out["protocol"] = (
        clean_text(chunk["protocol"]).fillna("") if "protocol" in chunk.columns else ""
    )
    out["src_addr"] = clean_text(chunk["src_addr"]).fillna("")
    out["dst_addr"] = clean_text(chunk["dst_addr"]).fillna("")
    for col in ("packets", "bytes", "flow_record_count"):
        out[col] = clean_numeric(chunk[col]).fillna(0.0) if col in chunk.columns else 0.0
    for col in ("first_seen", "last_seen"):
        # Millisecond precision is kept as-is: this is the only sub-minute time
        # source in the dataset and F6 uses it to refine episode boundaries.
        out[col] = _to_utc(chunk[col]) if col in chunk.columns else pd.NaT
    return out[out["ts_min"] >= 0].copy()


def _stream_netflow(
    ds: DatasetInfo,
    *,
    max_rows: int,
    chunk_rows: int,
    windowed: bool,
    merged_lo: np.ndarray,
    merged_hi: np.ndarray,
    warnings: list[str],
) -> tuple[pd.DataFrame, dict]:
    """Bounded, chunked read of netflow_5tuple -- never a full pandas load."""
    mode = "windowed" if windowed else "head"
    meta = {
        "mode": mode,
        "rows_scanned": 0,
        "rows_kept": 0,
        "truncated": False,
        "chunk_rows": int(chunk_rows),
        "row_limit": int(max_rows),
        "columns_missing": [],
    }
    if ds.path("netflow_5tuple") is None:
        _note(warnings, "netflow_5tuple: file not present in this dataset -> contributes 0 rows")
        meta["columns_missing"] = list(_NETFLOW_WANTED)
        return pd.DataFrame(), meta
    if max_rows <= 0:
        _note(warnings, "netflow_5tuple: max_netflow_rows <= 0 -> nothing read")
        return pd.DataFrame(), meta

    reader = read_table(
        ds,
        "netflow_5tuple",
        chunksize=max(1, int(chunk_rows)),
        usecols=lambda c: c in _NETFLOW_WANTED,
    )
    chunks = [reader] if isinstance(reader, pd.DataFrame) else reader

    kept: list[pd.DataFrame] = []
    scanned = 0
    kept_rows = 0
    truncated = False
    stopped = False
    for chunk in chunks:
        if stopped or chunk is None or len(chunk) == 0:
            continue
        if not windowed and scanned >= max_rows:
            truncated = True
            break
        if not windowed and scanned + len(chunk) > max_rows:
            # Head sample: read exactly the cap, the rest of the file is unread.
            chunk = chunk.iloc[: max_rows - scanned]
            truncated = True
        clean = _clean_netflow_chunk(chunk, warnings)
        scanned += len(chunk)
        if clean.empty:
            if truncated:
                break
            continue
        if windowed and merged_lo.size:
            clean = clean[_hit_mask(clean["ts_min"].to_numpy(), merged_lo, merged_hi)]
        if not clean.empty:
            room = max_rows - kept_rows
            if len(clean) > room:
                clean = clean.iloc[:room]
                truncated = True
            kept.append(clean)
            kept_rows += len(clean)
            if truncated:
                stopped = True
                break
        if not windowed and truncated:
            break

    meta["rows_scanned"] = int(scanned)
    meta["rows_kept"] = int(kept_rows)
    meta["truncated"] = bool(truncated)
    if not kept:
        _note(
            warnings,
            f"netflow_5tuple: {mode} read kept no rows (scanned={scanned}); "
            "incident windows may fall outside the read range",
        )
        return pd.DataFrame(), meta
    df = pd.concat(kept, ignore_index=True).sort_values("ts_min", kind="stable")
    meta["time_range"] = [_iso(df["timestamp"].min()), _iso(df["timestamp"].max())]
    meta["global_first_seen"] = _iso(df["first_seen"].min())
    meta["global_last_seen"] = _iso(df["last_seen"].max())
    log.info(
        "%s: netflow %s scanned=%d kept=%d truncated=%s",
        ds.name, mode, meta["rows_scanned"], meta["rows_kept"], meta["truncated"],
    )
    return df, meta


def _netflow_window_edges(
    nf: pd.DataFrame,
    minutes: np.ndarray,
    windows: tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, pd.Timestamp],
    region_code: str,
    *,
    max_edges: int,
    max_curve_points: int,
) -> dict:
    """Directed data-plane edges observed inside one incident's windows."""
    start, end, b_start, b_end = windows
    lo = _minute_of(start)
    hi = _minute_of(end)
    b_lo = _minute_of(b_start)
    b_hi = _minute_of(b_end)

    def slice_between(low: int, high: int) -> pd.DataFrame:
        if minutes.size == 0:
            return nf.iloc[0:0]
        a = int(np.searchsorted(minutes, low, side="left"))
        b = int(np.searchsorted(minutes, high, side="right"))
        return nf.iloc[a:b]

    inc = slice_between(lo, hi)
    if inc.empty:
        return {"edges": [], "n_edges": 0, "bytes_total": 0.0}
    base = slice_between(b_lo, b_hi)

    base_rate: dict[tuple, float] = {}
    if not base.empty:
        base_minutes = max(1, b_hi - b_lo)
        base_grouped = base.groupby(list(_NETFLOW_EDGE_KEYS), observed=True, sort=False)["bytes"].sum()
        base_rate = {k: float(v) / base_minutes for k, v in base_grouped.items()}

    inc_minutes = max(1, hi - lo)
    per_edge = inc.groupby(list(_NETFLOW_EDGE_KEYS), observed=True, sort=False).agg(
        bytes_total=("bytes", "sum"),
        packets_total=("packets", "sum"),
        first_seen=("first_seen", "min"),
        last_seen=("last_seen", "max"),
    )
    per_edge = per_edge.sort_values("bytes_total", ascending=False).head(max(1, max_edges))
    if per_edge.empty:
        return {"edges": [], "n_edges": 0, "bytes_total": 0.0}

    wanted = per_edge.reset_index()[list(_NETFLOW_EDGE_KEYS)]
    curve_rows = inc.merge(wanted, on=list(_NETFLOW_EDGE_KEYS), how="inner")
    curves: dict[tuple, list] = {}
    if not curve_rows.empty:
        per_minute = (
            curve_rows.groupby(list(_NETFLOW_EDGE_KEYS) + ["ts_min"], observed=True, sort=False)
            .agg(packets=("packets", "sum"), bytes=("bytes", "sum"))
            .reset_index()
        )
        for key, grp in per_minute.groupby(list(_NETFLOW_EDGE_KEYS), observed=True, sort=False):
            key_tuple = tuple(key) if isinstance(key, tuple) else (key,)
            curves[key_tuple] = [
                [
                    pd.Timestamp(int(minute), unit="m", tz="UTC").isoformat(timespec="minutes"),
                    int(packets),
                    int(nbytes),
                ]
                for minute, packets, nbytes in grp[["ts_min", "packets", "bytes"]].itertuples(index=False)
            ]

    edges: list[dict] = []
    for key, row in per_edge.iterrows():
        node, interface_id, src_addr, dst_addr, protocol = key
        points, thinned = _thin(curves.get(key, []), max_curve_points)
        inc_rate = float(row["bytes_total"]) / inc_minutes
        base_value = base_rate.get(key)
        change_rate = (
            float((inc_rate - base_value) / base_value)
            if base_value is not None and abs(base_value) > 1e-9
            else None
        )
        edges.append({
            # --- spec 5.5 field set, per directed edge
            "src_addr": str(src_addr),
            "dst_addr": str(dst_addr),
            "protocol": str(protocol),
            "node": str(node),
            "interface_id": str(interface_id),
            "traffic_curve_1min": points,
            "first_seen": _iso(row["first_seen"]),
            "last_seen": _iso(row["last_seen"]),
            "change_rate": change_rate,
            # --- direction + context
            "network_element_id": network_element_id(region_code, node),
            "direction": f"{src_addr} -> {dst_addr}",
            "directed": True,
            "bytes_total": float(row["bytes_total"]),
            "packets_total": float(row["packets_total"]),
            "bytes_per_min_baseline": base_value,
            "bytes_per_min_incident": inc_rate,
            "curve_points": len(points),
            "curve_thinned": thinned,
            "incident_window": [_iso(start), _iso(end)],
            "baseline_window": [_iso(b_start), _iso(b_end)],
        })
    return {
        "edges": edges,
        "n_edges": len(edges),
        "bytes_total": float(sum(e["bytes_total"] for e in edges)),
    }


def _flat_edges(
    entry: dict,
    region_code: str,
    *,
    max_netflow_edges: int,
) -> list[dict]:
    """The flat ``edges`` list :mod:`.candidate_generator` reads.

    That module pulls ``flow_ev["incidents"][iid]["edges"]`` and hangs each edge
    on ``edge["source_node"]`` / ``edge["target_node"]``, while spec 5.5 names the
    probe fields ``source_region/source_ip/target_region/target_domain``.  Both
    are kept: the spec structures stay under ``traffic_flow`` / ``netflow`` and
    this list repeats them in the consumer's shape.

    Node attribution is deliberately narrow:

    * probe edges are emitted by this region's traffic-vm (measured: source_ip is
      always ``fd00:3:40::10``), so ``source_node`` is set only when the edge's
      ``source_region`` really is this region.  The remote target is another
      region's service and is **not** one of this region's ten candidates, so
      ``target_node`` stays ``None`` rather than inventing a node id.
    * netflow edges carry the local network element that observed the flow
      (``network_element_id``), which is the only defensible owner.
    * ``traffic_curve_1min`` is dropped here (the consumer discards it anyway) so
      the flat list stays small.
    """
    flat: list[dict] = []
    traffic_part = entry.get("traffic_flow") or {}
    for edge in (traffic_part.get("edges") or []) + (traffic_part.get("elephant_edges") or []):
        same_region = edge.get("source_region") == region_code
        flat.append({
            **edge,
            "modality": "probe",
            "source_node": network_element_id(region_code, "traffic-vm") if same_region else None,
            "target_node": None,
        })
    seen_netflow = 0
    for edge in (entry.get("netflow") or {}).get("edges") or []:
        if seen_netflow >= max_netflow_edges:
            break
        seen_netflow += 1
        flat.append({
            **{k: v for k, v in edge.items() if k != "traffic_curve_1min"},
            "modality": "netflow",
            "source_node": edge.get("network_element_id"),
            "target_node": None,
        })
    return flat


# --------------------------------------------------------------------------- entry point
def build_flow_evidence(
    ds: DatasetInfo,
    incidents: list[dict],
    usage: TableUsage,
    *,
    max_netflow_rows: int = 250_000,
    baseline_minutes: int = 60,
    gap_minutes: int = 5,
    netflow_windowed: bool = True,
    netflow_chunk_rows: int = 200_000,
    max_netflow_edges_per_incident: int = 40,
    max_traffic_edges_per_incident: int = 200,
    max_curve_points: int = 60,
    max_flat_netflow_edges: int = 40,
) -> dict:
    """Build probe-edge (table 7) and data-plane edge (table 6) evidence.

    Returns ``{"dataset", "region", "incidents": {incident_id: {...}},
    "traffic_flow_metrics", "netflow_5tuple", "warnings"}`` -- the same shape as
    :func:`~aiops.evidence.metric_evidence.build_metric_evidence`, plus the
    per-table statistics the coverage report needs.

    Each ``incidents[iid]`` carries:

        incident_window / baseline_window   ISO pair, baseline strictly before the
                                            incident and never overlapping it
        traffic_flow                        ``{edges, elephant_edges, n_edges,
                                            n_elephant_edges, flow_types}`` --
                                            spec 5.5 field names, elephant apart
        netflow                             ``{edges, n_edges, truncated,
                                            covered_by_sample}``
        edges                               flat list in the shape
                                            :mod:`.candidate_generator` consumes;
                                            see :func:`_flat_edges`

    Both tables are recorded into ``usage`` with a non-zero ``rows_read``; a
    dataset that genuinely has no rows for one of them is left at 0 on purpose
    so :meth:`TableUsage.require_nonzero` fails the run (spec 0.5) instead of
    this module swallowing the problem.
    """
    warnings: list[str] = []
    out: dict[str, dict] = {}

    traffic = _prepare_traffic_flow(ds, usage, warnings)

    windows_by_incident: dict[str, tuple] = {}
    for incident in incidents:
        iid = str(incident.get("incident_id"))
        window = _windows_for_incident(incident, baseline_minutes, gap_minutes, warnings)
        if window is not None:
            windows_by_incident[iid] = window

    # Every incident's windows, coalesced once, feed the streaming filter.
    if windows_by_incident:
        lo = np.asarray([_minute_of(w[0]) for w in windows_by_incident.values()], dtype="int64")
        hi = np.asarray([_minute_of(w[1]) for w in windows_by_incident.values()], dtype="int64")
        b_lo = np.asarray([_minute_of(w[2]) for w in windows_by_incident.values()], dtype="int64")
        b_hi = np.asarray([_minute_of(w[3]) for w in windows_by_incident.values()], dtype="int64")
        merged_lo, merged_hi = _merge_intervals(
            np.concatenate([lo, b_lo]), np.concatenate([hi, b_hi])
        )
    else:
        merged_lo = np.zeros(0, dtype="int64")
        merged_hi = np.zeros(0, dtype="int64")
        _note(warnings, "no incident carried a usable time_range -> netflow windows are empty")

    netflow, netflow_meta = _stream_netflow(
        ds,
        max_rows=int(max_netflow_rows),
        chunk_rows=int(netflow_chunk_rows),
        windowed=bool(netflow_windowed),
        merged_lo=merged_lo,
        merged_hi=merged_hi,
        warnings=warnings,
    )
    netflow_minutes = netflow["ts_min"].to_numpy() if not netflow.empty else np.zeros(0, dtype="int64")
    if not netflow.empty:
        netflow_meta["edges"] = int(
            netflow.groupby(list(_NETFLOW_EDGE_KEYS), observed=True, sort=False).ngroups
        )
    usage.record(
        "netflow_5tuple",
        # rows_read counts the rows that actually became evidence, so it can
        # never exceed --netflow-max-rows; the raw scan volume is reported
        # separately instead of being passed off as the read count.
        netflow_meta["rows_kept"],
        rows_scanned=netflow_meta["rows_scanned"],
        edges=netflow_meta.get("edges"),
        truncated=netflow_meta["truncated"],
        mode=netflow_meta["mode"],
        row_limit=netflow_meta["row_limit"],
        first_seen=netflow_meta.get("global_first_seen"),
        last_seen=netflow_meta.get("global_last_seen"),
        fields_used=sorted(_NETFLOW_WANTED),
    )
    if netflow.empty:
        _note(warnings, "netflow_5tuple contributed no evidence rows for any incident")

    nf_first_minute = int(netflow_minutes.min()) if netflow_minutes.size else None
    nf_last_minute = int(netflow_minutes.max()) if netflow_minutes.size else None
    uncovered: list[str] = []
    for iid, window in windows_by_incident.items():
        start, end, b_start, b_end = window
        entry: dict = {
            "incident_window": [_iso(start), _iso(end)],
            "baseline_window": [_iso(b_start), _iso(b_end)],
        }
        if not traffic.empty:
            entry["traffic_flow"] = _traffic_flow_for_incident(
                traffic, window, max_edges=max_traffic_edges_per_incident
            )
        if not netflow.empty:
            payload = _netflow_window_edges(
                netflow,
                netflow_minutes,
                window,
                ds.region_code,
                max_edges=max_netflow_edges_per_incident,
                max_curve_points=max_curve_points,
            )
            payload["truncated"] = netflow_meta["truncated"]
            # A capped read can stop before a later incident.  Saying so per
            # incident keeps an empty edge list from reading as "no traffic".
            payload["covered_by_sample"] = not (
                _minute_of(end) < nf_first_minute
                or _minute_of(start) > nf_last_minute
                or _minute_of(b_end) < nf_first_minute
                or _minute_of(b_start) > nf_last_minute
            )
            entry["netflow"] = payload
            if not payload["covered_by_sample"]:
                uncovered.append(iid)
        if len(entry) > 2:
            flat = _flat_edges(entry, ds.region_code, max_netflow_edges=max_flat_netflow_edges)
            if flat:
                entry["edges"] = flat
                entry["n_edges"] = len(flat)
            out[iid] = entry

    if uncovered:
        _note(
            warnings,
            f"netflow_5tuple sample covers {netflow_meta.get('time_range')}; "
            f"{len(uncovered)}/{len(windows_by_incident)} incident windows fall outside it "
            f"-> raise --netflow-max-rows (currently {netflow_meta['row_limit']}) "
            "or keep netflow_windowed=True",
        )
        netflow_meta["incidents_outside_sample"] = len(uncovered)

    traffic_meta = usage.get("traffic_flow_metrics")
    return {
        "dataset": ds.name,
        "region": ds.region_code,
        "incidents": out,
        "incident_count": len(out),
        "traffic_flow_metrics": {
            "rows_read": traffic_meta.get("rows_read", 0),
            "flow_types": traffic_meta.get("flow_types", []),
            "edges": traffic_meta.get("edges", 0),
            "source_ips": traffic_meta.get("source_ips", []),
            "target_domains": traffic_meta.get("target_domains", 0),
            "time_range": traffic_meta.get("time_range"),
        },
        "netflow_5tuple": netflow_meta,
        "warnings": warnings,
    }
