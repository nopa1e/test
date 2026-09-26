"""Experiment F evidence modules (spec section 5).

Each module reads one or more of the seven dataset tables and turns it into
*named, timed evidence* -- replacing the "field exists" tests (defects D4/D5)
and the keyword-count collapse of the syslog (D3).

Every module records into a shared :class:`TableUsage` so the run can prove that
no table was silently skipped (spec section 0.5).

Missing-value spellings (``\\N``, ``NULL``, blanks) are normalised in exactly one
place, :mod:`.values`; :func:`missing_cell_counts` feeds the per-table
``missing_cells`` entry of the coverage report.
"""

from __future__ import annotations

from .table_usage import TABLE_KINDS, TableUsage, TableUsageError
from .values import MISSING_MARKERS, clean_numeric, clean_text, missing_cell_counts
from .metric_evidence import build_metric_evidence
from .routing_evidence import ROUTING_METRIC_HINTS, build_routing_evidence
from .quality_evidence import build_quality_evidence
from .log_evidence import build_log_evidence
from .flow_evidence import ELEPHANT_FLOW_TYPE, build_flow_evidence

__all__ = [
    "TABLE_KINDS",
    "TableUsage",
    "TableUsageError",
    "MISSING_MARKERS",
    "clean_numeric",
    "clean_text",
    "missing_cell_counts",
    "build_metric_evidence",
    "build_routing_evidence",
    "build_quality_evidence",
    "build_log_evidence",
    "build_flow_evidence",
    "ELEPHANT_FLOW_TYPE",
    "ROUTING_METRIC_HINTS",
]
