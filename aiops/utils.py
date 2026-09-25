from __future__ import annotations

import json
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd


LOGGER_NAME = "aiops"


def get_logger(name: str | None = None) -> logging.Logger:
    logger = logging.getLogger(name or LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def json_default(obj: Any) -> Any:
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def dump_json(path: str | Path, data: Any, indent: int = 2) -> None:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, default=json_default)


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_timestamp_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", utc=True)


def to_iso(dt: Any) -> str:
    ts = pd.Timestamp(dt)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat(timespec="milliseconds")


def normalize_name(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_]+", "_", str(name).strip().lower()).strip("_")


def rank_to_unit(x: np.ndarray, higher_is_more_anomalous: bool = True) -> np.ndarray:
    """Return a robust [0, 1] score using average ranks.

    ``higher_is_more_anomalous=True`` means the largest raw value gets ~1.0.
    """
    from scipy.stats import rankdata

    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    finite = np.isfinite(x)
    out = np.full_like(x, np.nan, dtype=float)
    if not finite.any():
        return out
    ranks = rankdata(x[finite], method="average")
    denom = max(1, finite.sum() - 1)
    vals = (ranks - 1.0) / denom
    if not higher_is_more_anomalous:
        vals = 1.0 - vals
    out[finite] = vals
    return out


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return default


def first_existing(paths: Iterable[str | Path]) -> Path | None:
    for p in paths:
        p = Path(p)
        if p.exists():
            return p
    return None


def snake_case_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [normalize_name(c) for c in df.columns]
    return df
