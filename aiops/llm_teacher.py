"""LLM teacher for incident-level RCA pseudo-labels.

Pseudo-labels are written to a disk cache.  Re-running an experiment reuses the
cache instead of calling the LLM again.  The teacher never reads test labels;
callers should pass only training incidents (or incidents whose ``split`` is not
``test``).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import PipelineConfig
from .taxonomy import FAULT_NAME_TO_PAIR, category_json_schema_text, is_valid_category
from .utils import dump_json, get_logger, read_json

log = get_logger(__name__)


def _incident_summary(incident: dict[str, Any]) -> dict[str, Any]:
    return {
        "incident_id": incident.get("incident_id"),
        "time_range": incident.get("time_range"),
        "duration_minutes": incident.get("duration_minutes"),
        "nodes": incident.get("nodes", []),
        "episode_count": incident.get("episode_count"),
        "timeline": (incident.get("timeline") or [])[:50],
        "episodes": [
            {
                "episode_id": e.get("episode_id"),
                "network_element_id": e.get("network_element_id"),
                "start_time": e.get("start_time"),
                "end_time": e.get("end_time"),
                "peak_anomaly_score": e.get("peak_anomaly_score"),
                "mean_anomaly_score": e.get("mean_anomaly_score"),
                "persistence": e.get("persistence"),
                "growth_rate": e.get("growth_rate"),
            }
            for e in (incident.get("episodes") or [])[:30]
        ],
    }


def select_teacher_cases(
    incidents: list[dict[str, Any]],
    cfg: PipelineConfig,
    split_manifest: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Select diverse training incidents for LLM pseudo-labelling.

    ``split_manifest`` maps incident_id -> split.  Incidents marked ``test`` are
    never selected.  The selection is deterministic and intentionally
    representative rather than random: high-anomaly incidents come first, then
    different node roles and duration buckets are round-robined.
    """
    eligible: list[dict[str, Any]] = []
    for inc in incidents:
        inc_id = str(inc.get("incident_id", ""))
        if split_manifest and split_manifest.get(inc_id) == "test":
            continue
        if str(inc.get("split", "")).lower() == "test":
            continue
        eligible.append(inc)
    if not eligible:
        return []
    limit = max(1, int(cfg.llm_teacher_num_cases))
    if len(eligible) <= limit:
        return eligible

    def anomaly(inc: dict[str, Any]) -> float:
        vals = [float(e.get("peak_anomaly_score", 0.0) or 0.0) for e in (inc.get("episodes") or [])]
        return float(np.mean(vals)) if vals else 0.0

    def bucket(inc: dict[str, Any]) -> str:
        node = str((inc.get("nodes") or ["unknown"])[0])
        role = next((r for r in ("service", "traffic", "firewall", "br", "cr", "monitor") if r in node), "other")
        duration = float(inc.get("duration_minutes", 0) or 0)
        return f"{role}:{int(duration // 30)}"

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    # First pass: one high-anomaly incident per bucket.
    grouped: dict[str, list[dict[str, Any]]] = {}
    for inc in sorted(eligible, key=lambda x: (-anomaly(x), str(x.get("incident_id")))):
        grouped.setdefault(bucket(inc), []).append(inc)
    while len(selected) < limit:
        progressed = False
        for b in sorted(grouped):
            if len(selected) >= limit:
                break
            while grouped[b]:
                inc = grouped[b].pop(0)
                inc_id = str(inc.get("incident_id"))
                if inc_id not in seen:
                    seen.add(inc_id)
                    selected.append(inc)
                    progressed = True
                    break
        if not progressed:
            break
    return selected


def _teacher_prompt(incident: dict[str, Any]) -> str:
    return f"""你是 AIOps 故障诊断的 LLM Teacher。请只根据下面给出的 incident 证据做根因排序和故障分类，不要猜测未给出的测试答案。
输出必须是严格 JSON，结构如下：
{{
  "incident_id": "...",
  "root_cause_ranking": [
    {{"network_element_id": "...", "rank": 1, "confidence": 0.0}},
    {{"network_element_id": "...", "rank": 2, "confidence": 0.0}}
  ],
  "major_category": "...",
  "minor_category": "...",
  "confidence": 0.0,
  "evidence": ["...", "..."]
}}

合法故障类别：
{category_json_schema_text()}

要求：
- root_cause_ranking 只能使用 incident.nodes 中出现的 network_element_id；
- rank 从 1 连续，不能重复；
- 不要输出 unknown/other/none 等占位类别；
- confidence 是 0~1 的整体置信度；
- evidence 是简短证据列表。

Incident 证据：
{json.dumps(_incident_summary(incident), ensure_ascii=False)}
"""


def _cache_paths(out_dir: str | Path, cfg: PipelineConfig) -> dict[str, Path]:
    root = Path(out_dir) / str(cfg.llm_teacher_cache_dir)
    return {
        "root": root,
        "selected": root / "selected_cases.json",
        "labels": root / "llm_pseudo_labels.jsonl",
        "stats": root / "teacher_stats.json",
    }


def load_cached_labels(out_dir: str | Path, cfg: PipelineConfig) -> list[dict[str, Any]]:
    paths = _cache_paths(out_dir, cfg)
    if not paths["labels"].exists():
        return []
    out: list[dict[str, Any]] = []
    with paths["labels"].open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _label_one(
    incident: dict[str, Any],
    backend: Any,
    cfg: PipelineConfig,
    dataset_name: str,
) -> dict[str, Any] | None:
    """Label a single incident.  Returns None when the answer is unusable.

    Split out of the driver loop so many incidents can be labelled concurrently:
    the LLM backend serves one request at a time per call, and vLLM is otherwise
    idle between calls, so a serial loop wastes most of the GPU.
    """
    from .llm_diagnoser import _parse_json_object

    prompt = _teacher_prompt(incident)
    try:
        resp = backend.chat(
            [
                {"role": "system", "content": "你是封闭集故障分类和根因排序的 LLM Teacher。"},
                {"role": "user", "content": prompt},
            ],
            tools=[],
        )
        msg = resp.get("message", resp) if isinstance(resp, dict) else {}
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        raw = _parse_json_object(content)
    except Exception as exc:
        log.warning("teacher LLM call failed for %s: %s", incident.get("incident_id"), exc)
        raw = None
    if not isinstance(raw, dict):
        return None

    major = str(raw.get("major_category", "") or "")
    minor = str(raw.get("minor_category", "") or "")
    if not is_valid_category(major, minor):
        pair = FAULT_NAME_TO_PAIR.get(str(raw.get("fault_category", "")))
        if pair:
            major, minor = pair
        else:
            return None

    nodes = {str(x) for x in incident.get("nodes", [])}
    ranking: list[dict[str, Any]] = []
    for item in raw.get("root_cause_ranking") or []:
        if not isinstance(item, dict):
            continue
        neid = str(item.get("network_element_id", ""))
        if neid not in nodes or any(r["network_element_id"] == neid for r in ranking):
            continue
        try:
            conf = float(item.get("confidence", 0.0))
        except Exception:
            conf = 0.0
        ranking.append({
            "network_element_id": neid,
            "rank": len(ranking) + 1,
            "confidence": max(0.0, min(1.0, conf)),
        })
    if not ranking:
        return None

    try:
        confidence = float(raw.get("confidence", 0.0))
    except Exception:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    if confidence >= float(cfg.llm_pseudo_high_confidence):
        tier = "hard"
    elif confidence >= float(cfg.llm_pseudo_low_confidence):
        tier = "soft"
    else:
        return None

    evidence = [str(x) for x in (raw.get("evidence") or []) if str(x).strip()]
    return {
        "incident_id": incident.get("incident_id"),
        "dataset": dataset_name,
        "root_cause_ranking": ranking,
        "major_category": major,
        "minor_category": minor,
        "confidence": confidence,
        "evidence": evidence,
        "evidence_count": len(evidence),
        "evidence_strength": float(min(1.0, len(evidence) / 5.0)),
        "label_source": "llm",
        "label_tier": tier,
    }


def generate_pseudo_labels(
    incidents: list[dict[str, Any]],
    point_df: pd.DataFrame,
    cfg: PipelineConfig,
    out_dir: str | Path,
    dataset_name: str = "dataset",
    split_manifest: dict[str, str] | None = None,
    backend: Any | None = None,
) -> list[dict[str, Any]]:
    """Generate and cache incident-level RCA pseudo-labels."""
    paths = _cache_paths(out_dir, cfg)
    cached = load_cached_labels(out_dir, cfg)
    if cached:
        log.info("LLM teacher cache hit: %d labels", len(cached))
        return cached
    selected = select_teacher_cases(incidents, cfg, split_manifest=split_manifest)
    if not selected:
        return []
    if backend is None:
        from .llm_diagnoser import LLMBackend, _parse_json_object

        backend = LLMBackend(cfg)
    else:
        from .llm_diagnoser import _parse_json_object

    if str(cfg.llm_backend).lower() in {"none", ""}:
        log.warning("LLM teacher requested but llm_backend=none; writing empty cache")
        dump_json(paths["selected"], {"selected": [_incident_summary(i) for i in selected]})
        dump_json(paths["stats"], {"teacher_case_count": 0, "written": 0, "reason": "llm_backend_none"})
        paths["root"].mkdir(parents=True, exist_ok=True)
        paths["labels"].write_text("", encoding="utf-8")
        return []

    workers = max(1, int(getattr(cfg, "llm_teacher_workers", 8)))
    log.info("LLM teacher: %d cases to label, %d parallel workers", len(selected), workers)

    labels_by_index: dict[int, dict[str, Any]] = {}
    discarded = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_label_one, incident, backend, cfg, dataset_name): idx
            for idx, incident in enumerate(selected)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            completed += 1
            try:
                label = fut.result()
            except Exception as exc:
                log.warning("teacher worker failed: %s", exc)
                label = None
            if label is None:
                discarded += 1
            else:
                labels_by_index[idx] = label
            # A progress line per batch, so an external monitor can see this
            # stage advancing instead of sitting silent for half an hour.
            if completed % 10 == 0 or completed == len(selected):
                log.info("LLM teacher progress %d/%d labels=%d discarded=%d",
                         completed, len(selected), len(labels_by_index), discarded)

    # Restore the selection order so the cached labels are deterministic.
    labels: list[dict[str, Any]] = [labels_by_index[i] for i in sorted(labels_by_index)]

    paths["root"].mkdir(parents=True, exist_ok=True)
    with paths["labels"].open("w", encoding="utf-8") as f:
        for item in labels:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    dump_json(paths["selected"], {"selected": [_incident_summary(i) for i in selected]})
    dump_json(paths["stats"], {
        "teacher_case_count": len(selected),
        "written": len(labels),
        "discarded": discarded,
        "high_confidence": sum(1 for x in labels if x["label_tier"] == "hard"),
        "soft_confidence": sum(1 for x in labels if x["label_tier"] == "soft"),
    })
    log.info("LLM teacher wrote %d labels (%d discarded)", len(labels), discarded)
    return labels
