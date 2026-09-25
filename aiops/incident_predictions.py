"""Incident-level prediction generation and high/low-confidence routing.

This module is shared by experiment A (post-hoc LLM verification of existing
incident artifacts) and experiment B (LLM teacher / Task A+B full pipeline).
It never reads ground-truth labels.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .config import PipelineConfig
from .dataset import official_network_element_ids
from .taxonomy import category_json_schema_text, infer_category_from_evidence, is_valid_category
from .utils import dump_json, get_logger, to_iso

log = get_logger(__name__)


def _parse_json(text: str | None) -> dict[str, Any] | None:
    from .llm_diagnoser import _parse_json_object

    return _parse_json_object(text)


def _ts(value: Any) -> pd.Timestamp | None:
    t = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(t) else pd.Timestamp(t)


def merge_adjacent_incidents(
    incidents: list[dict[str, Any]],
    max_gap_minutes: int = 5,
) -> list[dict[str, Any]]:
    """Merge strongly adjacent incidents with overlapping nodes.

    This is intentionally a conservative first-stage rule.  Ambiguous cases are
    left to the LLM incident-correlation stage.
    """
    if not incidents:
        return []
    ordered = sorted(incidents, key=lambda x: str(x.get("time_range", {}).get("start", "")))
    parent = list(range(len(ordered)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i], ordered[j]
            a_start, a_end = _ts(a.get("time_range", {}).get("start")), _ts(a.get("time_range", {}).get("end"))
            b_start, b_end = _ts(b.get("time_range", {}).get("start")), _ts(b.get("time_range", {}).get("end"))
            if any(x is None for x in (a_start, a_end, b_start, b_end)):
                continue
            gap = 0.0
            if b_start > a_end:
                gap = (b_start - a_end).total_seconds() / 60.0
            elif a_start > b_end:
                gap = (a_start - b_end).total_seconds() / 60.0
            if gap > max_gap_minutes:
                continue
            nodes_a = {str(x) for x in a.get("nodes", [])}
            nodes_b = {str(x) for x in b.get("nodes", [])}
            if nodes_a & nodes_b:
                union(i, j)

    groups: dict[int, list[dict[str, Any]]] = {}
    for i, inc in enumerate(ordered):
        groups.setdefault(find(i), []).append(inc)
    merged: list[dict[str, Any]] = []
    for idx, group in enumerate(groups.values(), 1):
        starts = [_ts(x.get("time_range", {}).get("start")) for x in group]
        ends = [_ts(x.get("time_range", {}).get("end")) for x in group]
        starts = [x for x in starts if x is not None]
        ends = [x for x in ends if x is not None]
        if not starts or not ends:
            continue
        nodes = sorted({str(n) for x in group for n in x.get("nodes", [])})
        merged.append({
            "incident_id": group[0].get("incident_id"),
            "merged_ids": [x.get("incident_id") for x in group],
            "time_range": {"start": to_iso(min(starts)), "end": to_iso(max(ends))},
            "nodes": nodes,
            "episodes": [ep for x in group for ep in x.get("episodes", [])],
            "timeline": [t for x in group for t in (x.get("timeline") or [])],
            "source_incidents": group,
        })
    return merged


def _candidate_order(incident: dict[str, Any], rca_scores: pd.DataFrame | None, region_code: str) -> list[str]:
    nodes = [str(x) for x in incident.get("nodes", [])]
    allowed = set(official_network_element_ids(region_code))
    nodes = [n for n in nodes if n in allowed]
    if rca_scores is not None and not rca_scores.empty and "incident_id" in rca_scores.columns:
        ids = set(incident.get("merged_ids") or [incident.get("incident_id")])
        sub = rca_scores[rca_scores["incident_id"].astype(str).isin({str(x) for x in ids})]
        if not sub.empty:
            sub = sub.sort_values("root_cause_score", ascending=False)
            ranked = [str(n) for n in sub["network_element_id"].tolist() if str(n) in allowed]
            for n in ranked:
                if n not in nodes:
                    nodes.append(n)
    return nodes


def _fallback_category(evidence: dict[str, Any], node_scores: list[dict[str, Any]]) -> tuple[str, str]:
    try:
        major, sub = infer_category_from_evidence(evidence, node_scores)
        if is_valid_category(major, sub):
            return major, sub
    except Exception:
        pass
    return "link", "delay"


def _normalize_result(
    raw: dict[str, Any] | None,
    incident: dict[str, Any],
    ordered_nodes: list[str],
    evidence: dict[str, Any],
    cfg: PipelineConfig,
    fallback_nodes: list[str] | None = None,
) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    prob = raw.get("anomaly_probability")
    try:
        prob = float(prob)
    except (TypeError, ValueError):
        prob = 0.0
    prob = max(0.0, min(1.0, prob))
    cat = raw.get("fault_category")
    major, sub = "", ""
    if isinstance(cat, dict):
        major, sub = str(cat.get("major_category", "") or ""), str(cat.get("sub_category", "") or "")
    if not is_valid_category(major, sub):
        major, sub = _fallback_category(evidence, [{"network_element_id": n} for n in ordered_nodes])
    top = raw.get("root_cause_top5")
    roots: list[str] = []
    if isinstance(top, list):
        for item in top:
            if isinstance(item, dict):
                nid = str(item.get("network_element_id", ""))
            else:
                nid = str(item)
            if nid in ordered_nodes and nid not in roots:
                roots.append(nid)
    for nid in ordered_nodes:
        if len(roots) >= 5:
            break
        if nid not in roots:
            roots.append(nid)
    for nid in (fallback_nodes or []):
        if len(roots) >= 5:
            break
        if nid not in roots:
            roots.append(nid)
    top5 = [{"rank": i, "network_element_id": nid} for i, nid in enumerate(roots[:5], 1)]
    return {
        "is_anomaly": bool(raw.get("is_anomaly", prob >= 0.5)),
        "anomaly_probability": prob,
        "root_cause_top5": top5,
        "fault_category": {"major_category": major, "sub_category": sub},
        "reasoning_evidence": [str(x) for x in (raw.get("reasoning_evidence") or [])],
    }


def build_incident_prompt(incident: dict[str, Any], ordered_nodes: list[str]) -> list[dict[str, str]]:
    payload = {
        "incident_id": incident.get("merged_ids") or incident.get("incident_id"),
        "time_range": incident.get("time_range"),
        "nodes": ordered_nodes,
        "timeline": (incident.get("timeline") or [])[:25],
        "episodes": [
            {
                "network_element_id": e.get("network_element_id"),
                "start_time": e.get("start_time"),
                "end_time": e.get("end_time"),
                "peak_anomaly_score": e.get("peak_anomaly_score"),
                "mean_anomaly_score": e.get("mean_anomaly_score"),
                "persistence": e.get("persistence"),
                "growth_rate": e.get("growth_rate"),
            }
            for e in (incident.get("episodes") or [])[:25]
        ],
    }
    system = (
        "你是 AIOps 故障事件验证与根因分析模块。输出严格 JSON。"
        "必须输出 anomaly_probability（0~1），表示该事件是真实异常的概率。"
        "如果多个相邻 episode 属于同一持续故障，必须视为同一事件，不要重复输出。"
        "root_cause_top5 只能使用给定的 nodes；故障类别必须从下面的封闭枚举中选择。\n"
        + category_json_schema_text()
    )
    user = (
        "请判断该事件是否为真实异常，并给出根因 Top5 和故障类别。"
        '返回格式：{"is_anomaly": true, "anomaly_probability": 0.0, '
        '"root_cause_top5": [{"rank":1,"network_element_id":"..."}], '
        '"fault_category": {"major_category":"...","sub_category":"..."}, '
        '"reasoning_evidence": ["..."]}\n'
        "事件证据：\n" + json.dumps(payload, ensure_ascii=False)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def generate_incident_predictions(
    incidents: list[dict[str, Any]],
    dataset: str,
    cfg: PipelineConfig,
    region_code: str | None = None,
    evidence: dict[str, Any] | None = None,
    rca_scores: pd.DataFrame | None = None,
    fallback_nodes: list[str] | None = None,
    backend: Any | None = None,
    max_workers: int = 8,
    threshold: float = 0.5,
) -> dict[str, Any]:
    """Run LLM verification for every incident and route high/low confidence.

    If ``backend`` is None the function uses deterministic RCA/category output.
    """
    evidence = evidence or {}
    region_code = region_code or str(dataset).split("_", 1)[0]
    high: list[dict[str, Any]] = []
    low: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []

    def work(item: tuple[int, dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        idx, incident = item
        ordered = _candidate_order(incident, rca_scores, region_code)
        raw = None
        if backend is not None and ordered:
            try:
                resp = backend.chat(build_incident_prompt(incident, ordered), tools=[])
                msg = resp.get("message", resp) if isinstance(resp, dict) else {}
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                raw = _parse_json(content)
            except Exception as exc:
                log.warning("incident %s LLM failed: %s", incident.get("incident_id"), exc)
        normalized = _normalize_result(raw, incident, ordered, evidence, cfg, fallback_nodes)
        pid = f"pred_{dataset}_{idx:06d}"
        start = incident.get("time_range", {}).get("start")
        end = incident.get("time_range", {}).get("end")
        pred = {
            "prediction_id": pid,
            "start_time": start,
            "end_time": end,
            "root_cause_top5": normalized["root_cause_top5"],
            "fault_category": normalized["fault_category"],
        }
        meta_item = {
            "prediction_id": pid,
            "incident_id": incident.get("incident_id"),
            "merged_ids": incident.get("merged_ids"),
            "anomaly_probability": normalized["anomaly_probability"],
            "is_anomaly": normalized["is_anomaly"],
            "reasoning_evidence": normalized["reasoning_evidence"],
            "source": "llm" if raw is not None else "fallback",
        }
        if normalized["anomaly_probability"] >= float(threshold) and normalized["is_anomaly"]:
            high.append(pred)
        else:
            low.append(pred)
        meta.append(meta_item)
        return pred, meta_item, incident

    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
        futures = [pool.submit(work, (i, inc)) for i, inc in enumerate(incidents, 1)]
        for fut in as_completed(futures):
            fut.result()
    return {"high": high, "low": low, "meta": meta}


def save_routed(
    output_dir: str | Path,
    high: list[dict[str, Any]],
    low: list[dict[str, Any]],
    meta: list[dict[str, Any]],
) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    high_path = out / "predictions_high_conf.jsonl"
    low_path = out / "predictions_low_conf.jsonl"
    meta_path = out / "llm_anomaly_scores.jsonl"
    with high_path.open("w", encoding="utf-8") as f:
        for rec in high:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with low_path.open("w", encoding="utf-8") as f:
        for rec in low:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with meta_path.open("w", encoding="utf-8") as f:
        for rec in meta:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"high": str(high_path), "low": str(low_path), "meta": str(meta_path)}
