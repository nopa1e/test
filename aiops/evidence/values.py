"""Missing-value normalisation shared by the evidence modules.

The dataset spells "no value" three ways, but only **one** of them survives
``pd.read_csv`` as a string:

    ``NULL``  ``interface_metrics.if_role`` (926,656 rows in xian, 927,202 in
              beida), ``scrape_health.scrape_error`` (261,900 of 262,080), and
              16 cells spread over eight xian ``node_metrics`` columns.
              pandas' default ``na_values`` already contains ``"NULL"``, so
              these arrive as ``NaN`` and need no cleaning at all.
    ``\\N``    ``netflow_5tuple.if_role`` (every sampled row) and every flow
              column of ``traffic_flow_metrics`` (dns/web/auth/elephant).
              **pandas does not recognise ``\\N``**, so it arrives as a real
              two-character string and would become a real category.
    blank     a genuinely empty field (the frr syslog has none).

So the numeric paths were never wrong -- ``pd.to_numeric(..., errors="coerce")``
turns both spellings into ``NaN`` anyway (measured).  What the markers do break
is **categorical or key** use: ``if_role == "\\N"`` would read as one more real
role, and ``canonical_node("NULL")`` returns the plausible-looking fake node
``"null"``.

This module is the single place those are normalised, and it reports what it
found so ``table_usage_report.json`` can show the cleaning happened.  Verified
cell-for-cell equivalent to the previous inline parsing on xian and beida.
"""

from __future__ import annotations

import pandas as pd

#: Every spelling of "no value" observed in the eight regions.  ``"nan"`` and
#: ``"None"`` are listed because ``astype(str)`` renders an already-missing cell
#: that way -- without them, cleaning would *create* a category out of a value
#: that was missing all along.
MISSING_MARKERS: frozenset[str] = frozenset(
    {r"\N", "NULL", "null", "None", "none", "N/A", "nan", "NaN", ""}
)


def clean_text(series: pd.Series) -> pd.Series:
    """Strip whitespace and turn every missing marker into ``NaN``."""
    if series is None:
        return pd.Series(dtype=object)
    text = series.astype(str).str.strip()
    return text.mask(text.isin(MISSING_MARKERS))


def clean_numeric(series: pd.Series) -> pd.Series:
    """Numeric parse where the missing markers (and blanks) become ``NaN``."""
    if series is None:
        return pd.Series(dtype=float)
    if pd.api.types.is_numeric_dtype(series):
        return series
    return pd.to_numeric(clean_text(series), errors="coerce")


def missing_cell_counts(df: pd.DataFrame, columns=None) -> dict[str, int]:
    """Count cells that are missing or carry a marker, per column.

    An already-``NaN`` cell counts too (``astype(str)`` renders it ``"nan"``),
    which is what makes ``interface_metrics.if_role`` report its full 926,656
    rather than only the literal spellings.
    """
    out: dict[str, int] = {}
    for col in list(df.columns if columns is None else columns):
        if col not in df.columns:
            continue
        series = df[col]
        if pd.api.types.is_numeric_dtype(series):
            continue
        text = series.astype(str).str.strip()
        hits = int(text.isin(MISSING_MARKERS).sum())
        if hits:
            out[col] = hits
    return out
