"""Coverage self-check for the seven dataset tables (spec section 0.5).

Every F evidence module records what it actually read here.  At the end of a
run :meth:`TableUsage.require_nonzero` fails loudly if any table contributed
zero rows -- the spec forbids silently skipping a table, because "this table had
nothing to say" is exactly the kind of assumption that hid the earlier defects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The seven tables of the real dataset, in spec order.
TABLE_KINDS: tuple[str, ...] = (
    "node_metrics",
    "interface_metrics",
    "routing_metrics",
    "scrape_health",
    "frr_syslog_events",
    "netflow_5tuple",
    "traffic_flow_metrics",
)


class TableUsageError(RuntimeError):
    """Raised when a table contributed no rows."""


class TableUsage:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, Any]] = {}

    def record(self, kind: str, rows_read: int, **extra: Any) -> None:
        """Accumulate ``rows_read`` (and any extra facts) for one table."""
        slot = self._rows.setdefault(kind, {"rows_read": 0})
        slot["rows_read"] = int(slot.get("rows_read", 0)) + int(rows_read)
        for key, value in extra.items():
            if value is None:
                continue
            if key in ("fields_used", "metric_names_list", "programs", "flow_types"):
                merged = list(dict.fromkeys(list(slot.get(key) or []) + list(value)))
                slot[key] = merged
            elif isinstance(value, (int, float)) and isinstance(slot.get(key), (int, float)):
                slot[key] = slot[key] + value
            else:
                slot[key] = value

    def get(self, kind: str) -> dict[str, Any]:
        return dict(self._rows.get(kind, {"rows_read": 0}))

    def require_nonzero(self, kinds: tuple[str, ...] = TABLE_KINDS) -> None:
        """Spec 0.5: any table with ``rows_read == 0`` aborts the run."""
        empty = [k for k in kinds if int(self._rows.get(k, {}).get("rows_read", 0)) == 0]
        if empty:
            missing = ", ".join(f"{k}(rows_read=0)" for k in empty)
            raise TableUsageError(
                "spec 0.5 violation: these tables contributed no rows: " + missing
            )

    def report(self, *, kinds: tuple[str, ...] = TABLE_KINDS) -> dict[str, Any]:
        return {k: self.get(k) for k in kinds}

    def save(self, path: str | Path, *, kinds: tuple[str, ...] = TABLE_KINDS) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(self.report(kinds=kinds), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out
