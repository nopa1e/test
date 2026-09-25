"""Log evidence: table 5 (frr_syslog_events).

Spec section 5.4 and defect D3.  Today the syslog is squashed into 23 keyword
counts that feed the unsupervised anomaly model, and the *ranking* stage never
sees it.  This module keeps the events as events: template, node, exact time,
severity, program and a semantic class.

It is only ~3,500 rows region-wide, so it cannot be a backbone -- it is
high-confidence punctuation ("this node's ospf6d complained at 10:26:57 and its
ospf6_neighbor_state_code changed at 10:27").  Every event in the region goes
into the evidence; nothing is sampled.

Two measured facts about the table (corrects doc 13.6 item 1):

* the CSV is **not** broken.  beida's 310 rows contain 39 quoted messages such
  as ``"SPF processing: # Areas: 1, SPF runtime: 0 sec 105 usec, Reason: R+, R-"``
  -- the quotes are all paired (zero rows with an odd count), a strict
  ``csv.reader`` yields 310/310 rows of exactly 15 fields, and ``pandas`` reads
  back all 310 ``id`` values.  Nothing is silently swallowed.
* the collector *does* store some events twice: beida has 7 repeated pairs and
  xian 11, sharing millisecond ``event_time_raw`` + ``hostname`` + ``message``
  and differing only in ``id`` / ``inserted_at``.  They are collapsed here, and
  the number dropped is recorded in ``table_usage_report.json``.
"""

from __future__ import annotations

import re

import pandas as pd

from ..dataset import DatasetInfo, network_element_id, node_from_hostname, read_table
from ..utils import get_logger
from .table_usage import TableUsage
from .values import clean_numeric, clean_text, missing_cell_counts

log = get_logger(__name__)

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F]{0,4}\b")
_HEX = re.compile(r"\b[0-9a-fA-F]{6,}\b")
_NUM = re.compile(r"\b\d+\b")
_QUOTED = re.compile(r'"[^"]*"')

#: Ordered: the first class whose keyword appears wins.
_SEMANTIC_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("flap", ("flap", "flapping")),
    ("unavailable", ("unavailable", "no route", "unreachable")),
    ("timeout", ("timeout", "timed out")),
    ("refused", ("refused",)),
    ("down", ("down", "went down", "shutdown")),
    ("up", ("up", "established", "comes up")),
    ("change", ("change", "changed", "update", "added", "removed", "withdraw")),
    ("error", ("error", "err", "could not", "failed", "failure")),
    ("warning", ("warn",)),
)


def _template(message: str) -> str:
    """Normalise a syslog line to a template (a very small Drain)."""
    text = str(message)
    text = _QUOTED.sub("<str>", text)
    text = _IPV4.sub("<ip>", text)
    text = _IPV6.sub("<ipv6>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUM.sub("<n>", text)
    return re.sub(r"\s+", " ", text).strip()


def _semantic_class(message: str, severity_code) -> str:
    low = str(message).lower()
    for name, keywords in _SEMANTIC_RULES:
        if any(k in low for k in keywords):
            return name
    try:
        if severity_code is not None and float(severity_code) <= 3:
            return "error"
    except (TypeError, ValueError):
        pass
    return "info"


def _text_of(value) -> str:
    """Render a cell as text, mapping a missing value to the empty string."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):  # pragma: no cover - defensive
        pass
    return str(value)


def _drop_repeated_events(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Collapse events the collector stored twice; returns (frame, n_dropped).

    Keyed on the millisecond event timestamp, the host and the message, which
    the repeated rows share exactly.  Falls back to the second-resolution
    ``event_time`` when the raw column is absent.
    """
    keys = [c for c in ("event_time_raw", "hostname", "message") if c in df.columns]
    if len(keys) < 3:
        keys = [c for c in ("event_time", "hostname", "message") if c in df.columns]
    if len(keys) < 3:
        return df, 0
    before = len(df)
    kept = df.drop_duplicates(subset=keys, keep="first")
    return kept, before - len(kept)


def build_log_evidence(ds: DatasetInfo, usage: TableUsage) -> dict:
    df = read_table(ds, "frr_syslog_events")
    if df.empty:
        return {"dataset": ds.name, "events": [], "templates": {}, "programs": {}}

    rows_read = len(df)
    missing = missing_cell_counts(df)
    df, dropped_repeated = _drop_repeated_events(df)

    time_col = "event_time" if "event_time" in df.columns else "received_at"
    # NOTE: itertuples() renames columns that are not valid Python identifiers,
    # so these working columns must NOT start with an underscore ("_t" becomes
    # the positional "_1" and attribute access then raises AttributeError).
    df["event_dt"] = pd.to_datetime(clean_text(df[time_col]), errors="coerce", utc=True)
    for col in ("hostname", "program", "severity", "message"):
        if col in df.columns:
            df[col] = clean_text(df[col])
    if "severity_code" in df.columns:
        df["severity_code"] = clean_numeric(df["severity_code"])
    df["canon_node"] = df.get("hostname", pd.Series(index=df.index, dtype=object)).map(
        lambda h: node_from_hostname(h, fallback=None) if pd.notna(h) else None
    )

    events: list[dict] = []
    for r in df.itertuples(index=False):
        node = r.canon_node
        message = _text_of(getattr(r, "message", ""))
        events.append({
            "time": pd.Timestamp(r.event_dt).isoformat() if pd.notna(r.event_dt) else None,
            "node": node,
            "network_element_id": network_element_id(ds.region_code, node) if node else None,
            "program": _text_of(getattr(r, "program", "")),
            "severity": _text_of(getattr(r, "severity", "")),
            "severity_code": (
                int(getattr(r, "severity_code")) if pd.notna(getattr(r, "severity_code", None)) else None
            ),
            "template": _template(message),
            "semantic_class": _semantic_class(message, getattr(r, "severity_code", None)),
            "message": message[:400],
        })
    events.sort(key=lambda e: (e["time"] or ""))

    templates: dict[str, int] = {}
    programs: dict[str, int] = {}
    classes: dict[str, int] = {}
    for e in events:
        templates[e["template"]] = templates.get(e["template"], 0) + 1
        programs[e["program"]] = programs.get(e["program"], 0) + 1
        classes[e["semantic_class"]] = classes.get(e["semantic_class"], 0) + 1

    usage.record(
        "frr_syslog_events",
        rows_read,
        programs=sorted(programs),
        templates=len(templates),
        semantic_classes=sorted(classes),
        events_kept=len(events),
        dropped_repeated_events=dropped_repeated,
        missing_cells=missing or None,
    )

    return {
        "dataset": ds.name,
        "event_count": len(events),
        "dropped_repeated_events": dropped_repeated,
        "events": events,
        "templates": dict(sorted(templates.items(), key=lambda kv: -kv[1])),
        "programs": dict(sorted(programs.items(), key=lambda kv: -kv[1])),
        "semantic_classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])),
    }
