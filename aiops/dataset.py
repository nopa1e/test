from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from .utils import get_logger

log = get_logger(__name__)

FILE_KIND_KEYWORDS = [
    ("node_metrics", ("node_metrics",)),
    ("interface_metrics", ("interface_metrics",)),
    ("routing_metrics", ("routing_metrics",)),
    ("scrape_health", ("scrape_health",)),
    ("frr_syslog_events", ("frr_syslog_events", "frr_syslog")),
    ("traffic_flow_metrics", ("traffic_flow_metrics",)),
    ("netflow_5tuple", ("netflow_5tuple", "netflow")),
    ("trace", ("trace", "span")),
    ("topology", ("topolog", "topo", "depend", "edge")),
]

CHINESE_REGION_TO_CODE = {
    "沈阳": "shenyang",
    "北京": "beida",
    "北大": "beida",
    "成都": "chengdu",
    "广州": "guangzhou",
    "南京": "nanjing",
    "上海": "shanghai",
    "武汉": "wuhan",
    "西安": "xian",
}


OFFICIAL_NODE_ROLES: tuple[str, ...] = (
    "br-1",
    "br-2",
    "cr-1",
    "cr-2",
    "fw",
    "traffic-vm",
    "service-vm-1",
    "service-vm-2",
    "service-vm-3",
    "monitor-vm",
)


def official_network_element_ids(region_code: str) -> list[str]:
    """Return the official network_element_id enumeration for one region."""
    return [f"{region_code}-{role}" for role in OFFICIAL_NODE_ROLES]


@dataclass
class DatasetInfo:
    name: str
    region_code: str
    processed_dir: Path
    files: dict[str, Path] = field(default_factory=dict)

    def path(self, kind: str) -> Path | None:
        return self.files.get(kind)

    def has(self, kind: str) -> bool:
        return kind in self.files


def infer_region_code(name: str, processed_dir: Path | None = None) -> str:
    m = re.match(r"([a-zA-Z]+)[_-]", str(name))
    if m:
        return m.group(1).lower()
    for keyword, code in CHINESE_REGION_TO_CODE.items():
        if keyword in str(name) or (processed_dir and keyword in str(processed_dir)):
            return code
    return re.sub(r"[^a-zA-Z0-9]+", "_", str(name)).strip("_").lower() or "unknownregion"


def discover_datasets(workspace: str | Path) -> list[DatasetInfo]:
    root = Path(workspace)
    processed_dirs = sorted({p for p in root.rglob("processed") if p.is_dir()})
    datasets: list[DatasetInfo] = []
    for processed in processed_dirs:
        csvs = sorted(processed.glob("*.csv"))
        if not csvs:
            continue
        dataset_dir = processed.parent.parent if processed.parent.parent != processed else processed.parent
        name = dataset_dir.name
        files: dict[str, Path] = {}
        for p in csvs:
            low = p.name.lower()
            for kind, keywords in FILE_KIND_KEYWORDS:
                if any(k in low for k in keywords):
                    files.setdefault(kind, p)
                    break
        datasets.append(
            DatasetInfo(
                name=name,
                region_code=infer_region_code(name, processed),
                processed_dir=processed,
                files=files,
            )
        )
    return datasets


def read_table(
    ds: DatasetInfo,
    kind: str,
    nrows: int | None = None,
    usecols: list[str] | None = None,
    **kwargs: Any,
) -> pd.DataFrame:
    path = ds.path(kind)
    if path is None:
        return pd.DataFrame()
    try:
        return pd.read_csv(
            path,
            encoding="utf-8-sig",
            low_memory=False,
            nrows=nrows,
            usecols=usecols,
            **kwargs,
        )
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("failed to read %s (%s): %s", path, kind, exc)
        # Retry without usecols if the requested subset is bad.
        if usecols is not None:
            try:
                return pd.read_csv(path, encoding="utf-8-sig", low_memory=False, nrows=nrows, **kwargs)
            except Exception as exc2:
                log.warning("retry failed for %s: %s", path, exc2)
        return pd.DataFrame()


def canonical_node(node: Any) -> str:
    s = str(node).strip().lower()
    if not s:
        return "unknown"
    # Direct forms.
    for prefix in ("service-vm", "traffic-vm", "monitor-vm", "br", "cr"):
        if s.startswith(prefix):
            m = re.match(rf"{re.escape(prefix)}[-_]?(\d*)", s)
            if m:
                num = m.group(1)
                return f"{prefix}-{num}" if num else prefix
    if s.startswith("fw") or "firewall" in s:
        return "fw"
    # Embedded forms such as BR-2-ccf-aiops-shenyang / service-vm-1-...
    m = re.search(r"(service-vm|traffic-vm|monitor-vm|br|cr)[-_]?(\d*)", s)
    if m:
        prefix, num = m.group(1), m.group(2)
        return f"{prefix}-{num}" if num else prefix
    return s


def network_element_id(region_code: str, node: Any) -> str:
    return f"{region_code}-{canonical_node(node)}"


def node_from_hostname(hostname: Any, fallback: str | None = None) -> str | None:
    if hostname is None or (isinstance(hostname, float) and pd.isna(hostname)):
        return fallback
    node = canonical_node(str(hostname))
    if node != str(hostname).strip().lower() and node not in {"unknown", ""}:
        return node
    if fallback:
        return fallback
    return None


def ip_node_map_from_scrape_health(ds: DatasetInfo) -> dict[str, str]:
    """Extract IP -> node from scrape target IDs when available."""
    sh = read_table(ds, "scrape_health")
    if sh.empty or "target_id" not in sh.columns or "node" not in sh.columns:
        return {}
    out: dict[str, str] = {}
    for _, row in sh[["target_id", "node"]].drop_duplicates().iterrows():
        target = str(row["target_id"])
        node = canonical_node(row["node"])
        # target forms: [fd00:2:20::1]:9100 or 10.0.0.1:9100
        host = re.sub(r"^\[(.*)\]:\d+$", r"\1", target)
        host = re.sub(r"^(.*):\d+$", r"\1", host)
        if host:
            out[host] = node
    return out
