"""Routing evidence: table 3 (routing_metrics) -- v2, per dataset_schema.md.

v1 assumed every series carried its signal in `value`.  The schema scan shows
that is wrong for **8 of the 15 metric families**, which are numerically
constant for the whole 14 days:

    value-bearing (7)   bgp_peer_up, bgp_peer_uptime_seconds,
                        bgp_peer_prefix_received, ospf6_neighbor_state_code,
                        ospf6_interface_enabled, ipv6_route_count,
                        ipv6_route_change_total
    label-only    (8)   bgp_peer_prefix_sent, bgp_peer_count, bgp_command_success,
                        ospf6_interface_cost, ipv6_route_exists,
                        ipv6_route_nexthop_info, ipv6_default_route_info,
                        ipv6_default_route_changed_total

For the label-only families the information lives entirely in the Prometheus
label string, so a value threshold can never fire -- this is exactly why D5
(`has(...)` = "does the field exist") never worked.  Consequences recorded here:

  * ``ipv6_route_exists`` is always 1 and **never 0** -> `blackhole` cannot be
    detected as ``value < 1``; it must be the *disappearance of a prefix series*.
  * ``ospf6_interface_cost`` is always 1 -> `ospf6_cost_anomaly` is **not
    detectable from this table at all** (the real cost is not exported).
  * ``ipv6_route_nexthop_info`` is always 1 -> `wrong_static_route` shows up as a
    *label replacement for the same prefix*.
  * ``ipv6_default_route_info`` is always 1 -> same treatment.

So this module emits three independent kinds of event:

    value_change       a series' numbers moved out of its baseline range
    series_disappeared a (metric_name, label) key present in the baseline is
                       absent in the incident window -> route withdrawn / blackhole
    series_appeared    a key appears in the window -> route installed / next-hop
                       replaced

Facts only.  ``hints_sub_category`` is prompt context, never a filled-in answer
(the competition forbids replacing the model with pure scripted reasoning).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..dataset import DatasetInfo, canonical_node, read_table
from ..utils import get_logger
from .table_usage import TableUsage

log = get_logger(__name__)

ROUTING_METRIC_HINTS: dict[str, str | None] = {
    "bgp_peer_up": "bgp_session_down",
    "bgp_peer_uptime_seconds": "bgp_session_down",
    "bgp_peer_prefix_received": "bgp_route_flap",
    "bgp_peer_prefix_sent": "bgp_route_flap",
    "ipv6_route_change_total": "bgp_route_flap",
    "ospf6_neighbor_state_code": "ospf6_neighbor_down",
    "ospf6_interface_cost": "ospf6_cost_anomaly",
    "ipv6_route_exists": "blackhole",
    "ipv6_default_route_info": "wrong_default_route",
    "ipv6_default_route_changed_total": "wrong_default_route",
    "ipv6_route_nexthop_info": "wrong_static_route",
    "bgp_command_success": None,
    "ospf6_interface_enabled": None,
    "bgp_peer_count": None,
    "ipv6_route_count": None,
}

#: Families whose numbers are constant across the dataset (schema section 3).
LABEL_ONLY_METRICS: frozenset[str] = frozenset({
    "bgp_peer_prefix_sent", "bgp_peer_count", "bgp_command_success",
    "ospf6_interface_cost", "ipv6_route_exists", "ipv6_route_nexthop_info",
    "ipv6_default_route_info", "ipv6_default_route_changed_total",
})


def _clean_value(series: pd.Series) -> pd.Series:
    """Numeric parse that also treats the literal 'NULL' / '\\N' markers as missing."""
    text = series.astype(str).str.strip()
    text = text.mask(text.str.upper().isin({"NULL", r"\N", ""}), np.nan)
    return pd.to_numeric(text, errors="coerce")


def load_routing_metrics(ds: DatasetInfo, usage: TableUsage, nodes: set[str] | None = None) -> pd.DataFrame:
    df = read_table(ds, "routing_metrics")
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    df["node"] = df["node"].map(canonical_node)
    df["value"] = _clean_value(df["value"])
    df["label"] = df["label"].fillna("").astype(str)
    if nodes:
        df = df[df["node"].isin(nodes)]
    names = sorted(df["metric_name"].astype(str).unique().tolist())
    usage.record(
        "routing_metrics",
        len(df),
        metric_names=len(names),
        metric_names_list=names,
        missing_from_spec=sorted(set(ROUTING_METRIC_HINTS) - set(names)),
        label_only_present=sorted(LABEL_ONLY_METRICS & set(names)),
        nodes=sorted(df["node"].unique().tolist()),
    )
    return df


def _label_key(label: str) -> str:
    """Normalise a Prometheus label string so comparisons are order-insensitive."""
    parts = [p.strip() for p in str(label).split(",") if p.strip()]
    return "|".join(sorted(parts))


def build_routing_evidence(
    ds: DatasetInfo,
    incidents: list[dict],
    usage: TableUsage,
    *,
    baseline_minutes: int = 60,
    gap_minutes: int = 5,
    max_events_per_incident: int = 80,
) -> dict:
    incident_nodes = {canonical_node(n) for inc in incidents for n in (inc.get("nodes") or [])}
    df = load_routing_metrics(ds, usage, nodes=incident_nodes or None)
    if df.empty:
        return {"dataset": ds.name, "metric_hints": ROUTING_METRIC_HINTS, "incidents": {}}

    df["skey"] = df["metric_name"].astype(str) + "||" + df["label"].map(_label_key)

    wide_by_node: dict[str, pd.DataFrame] = {}
    keysets_by_node: dict[str, pd.Series] = {}
    names_by_node: dict[str, dict[str, str]] = {}
    for node, grp in df.groupby("node", observed=True):
        wide_by_node[str(node)] = grp.pivot_table(
            index="timestamp", columns="skey", values="value", aggfunc="mean"
        ).sort_index()
        keysets_by_node[str(node)] = (
            grp.groupby("timestamp", observed=True)["skey"].apply(frozenset).sort_index()
        )
        names_by_node[str(node)] = (
            grp.drop_duplicates("skey").set_index("skey")["metric_name"].to_dict()
        )

    out: dict[str, dict] = {}
    for incident in incidents:
        iid = str(incident.get("incident_id"))
        tr = incident.get("time_range") or {}
        start = pd.Timestamp(tr.get("start"))
        end = pd.Timestamp(tr.get("end"))
        b_end = start - pd.Timedelta(minutes=gap_minutes)
        b_start = b_end - pd.Timedelta(minutes=baseline_minutes)

        events: list[dict] = []
        for node in {canonical_node(n) for n in (incident.get("nodes") or [])}:
            wide = wide_by_node.get(node)
            if wide is None or wide.empty:
                continue
            inc_win = wide[(wide.index >= start) & (wide.index <= end)]
            base_win = wide[(wide.index >= b_start) & (wide.index <= b_end)]
            if inc_win.empty or base_win.empty:
                continue

            # ---- 1) value changes
            for col in wide.columns:
                metric_name = names_by_node[node].get(col, str(col).split("||")[0])
                b = base_win[col].to_numpy(dtype=float)
                a = inc_win[col].to_numpy(dtype=float)
                b = b[np.isfinite(b)]
                a = a[np.isfinite(a)]
                if b.size == 0 or a.size == 0:
                    continue
                b_med = float(np.median(b))
                b_min, b_max = float(np.min(b)), float(np.max(b))
                a_max, a_min = float(np.max(a)), float(np.min(a))
                if abs(a_max - b_med) <= 1e-9 and abs(a_min - b_med) <= 1e-9:
                    continue
                outside = inc_win[col][(inc_win[col] > b_max) | (inc_win[col] < b_min)]
                delta = (a_max - b_med) if abs(a_max - b_med) >= abs(a_min - b_med) else (a_min - b_med)
                events.append({
                    "kind": "value_change",
                    "node": node,
                    "metric_name": metric_name,
                    "label": col.split("||", 1)[1] if "||" in col else "",
                    "baseline": b_med,
                    "baseline_range": [b_min, b_max],
                    "incident_min": a_min,
                    "incident_max": a_max,
                    "delta": float(delta),
                    "changed_at": outside.index[0].isoformat() if not outside.empty else None,
                    "hints_sub_category": ROUTING_METRIC_HINTS.get(metric_name),
                })

            # ---- 2) series disappearance / appearance (the label-only signal)
            ks = keysets_by_node.get(node)
            if ks is not None and not ks.empty:
                base_keys = ks[(ks.index >= b_start) & (ks.index <= b_end)]
                inc_keys = ks[(ks.index >= start) & (ks.index <= end)]
                if not base_keys.empty and not inc_keys.empty:
                    bset = frozenset().union(*base_keys.tolist())
                    iset = frozenset().union(*inc_keys.tolist())
                    for kind, delta_keys in (("series_disappeared", bset - iset),
                                             ("series_appeared", iset - bset)):
                        for sk in sorted(delta_keys):
                            metric_name, _, lbl = sk.partition("||")
                            events.append({
                                "kind": kind,
                                "node": node,
                                "metric_name": metric_name,
                                "label": lbl,
                                "hints_sub_category": ROUTING_METRIC_HINTS.get(metric_name),
                                "label_only_family": metric_name in LABEL_ONLY_METRICS,
                            })

        events.sort(key=lambda e: (e["kind"], -abs(e.get("delta") or 0.0)))
        if events:
            out[iid] = {
                "events": events[:max_events_per_incident],
                "total_events": len(events),
                "n_value_change": sum(1 for e in events if e["kind"] == "value_change"),
                "n_series_disappeared": sum(1 for e in events if e["kind"] == "series_disappeared"),
                "n_series_appeared": sum(1 for e in events if e["kind"] == "series_appeared"),
            }

    return {
        "dataset": ds.name,
        "metric_hints": ROUTING_METRIC_HINTS,
        "label_only_metrics": sorted(LABEL_ONLY_METRICS),
        "incidents": out,
    }
