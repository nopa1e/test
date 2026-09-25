"""Explicit LLM tasks for the second stage.

Task A: incident correlation (merge / split / keep_separate / uncertain).
Task B: candidate evidence verification and final RCA/classification.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import networkx as nx

from .config import PipelineConfig
from .incident import compare_incidents
from .taxonomy import is_valid_category
from .utils import get_logger

log = get_logger(__name__)


def _backend(cfg: PipelineConfig):
    from .llm_diagnoser import LLMBackend

    return LLMBackend(cfg)


def _parse(text: str | None):
    from .llm_diagnoser import _parse_json_object

    return _parse_json_object(text)


def task_a_incident_correlation(
    incidents: list[dict[str, Any]],
    cfg: PipelineConfig,
    graph: nx.Graph | nx.DiGraph | None = None,
    point_df: Any | None = None,
    backend: Any | None = None,
    max_pairs: int = 50,
) -> list[dict[str, Any]]:
    """Ask the LLM to classify near-in-time incident-candidate pairs."""
    if not incidents:
        return []
    backend = backend or _backend(cfg)
    decisions: list[dict[str, Any]] = []
    pairs = []
    for i in range(len(incidents)):
        for j in range(i + 1, len(incidents)):
            relations = compare_incidents(incidents[i], incidents[j], graph, point_df, cfg)
            if relations.get("time_overlap", 0.0) <= 0 and relations.get("node_overlap", 0.0) <= 0:
                continue
            pairs.append((incidents[i], incidents[j], relations))
            if len(pairs) >= max_pairs:
                break
        if len(pairs) >= max_pairs:
            break
    def _decide(item: tuple[int, tuple]) -> tuple[int, dict[str, Any]]:
        idx, (a, b, relations) = item
        prompt = {
            "incident_a": {
                "incident_id": a.get("incident_id"),
                "time_range": a.get("time_range"),
                "nodes": a.get("nodes", []),
                "timeline": (a.get("timeline") or [])[:20],
            },
            "incident_b": {
                "incident_id": b.get("incident_id"),
                "time_range": b.get("time_range"),
                "nodes": b.get("nodes", []),
                "timeline": (b.get("timeline") or [])[:20],
            },
            "relations": relations,
        }
        try:
            resp = backend.chat(
                [
                    {"role": "system", "content": "你是 AIOps incident correlation 模块，只输出严格 JSON。"},
                    {
                        "role": "user",
                        "content": (
                            "判断两个 incident candidate 的关系，输出："
                            '{"decision":"merge|split|keep_separate|uncertain","confidence":0.0,"evidence":["..."]}\n'
                            + json.dumps(prompt, ensure_ascii=False)
                        ),
                    },
                ],
                tools=[],
            )
            msg = resp.get("message", resp) if isinstance(resp, dict) else {}
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            raw = _parse(content)
        except Exception as exc:
            log.warning("task A failed: %s", exc)
            raw = None
        if not isinstance(raw, dict):
            raw = {"decision": "uncertain", "confidence": 0.0, "evidence": ["llm_unparseable"]}
        decision = str(raw.get("decision", "uncertain"))
        if decision not in {"merge", "split", "keep_separate", "uncertain"}:
            decision = "uncertain"
        return idx, {
            "incident_a": a.get("incident_id"),
            "incident_b": b.get("incident_id"),
            "decision": decision,
            "confidence": float(raw.get("confidence", 0.0) or 0.0),
            "evidence": [str(x) for x in (raw.get("evidence") or [])],
            "relations": relations,
        }

    # Pairs are independent, and the backend serves one request per call, so run
    # them concurrently instead of leaving vLLM idle between calls.
    workers = max(1, int(getattr(cfg, "llm_task_a_workers", 8)))
    log.info("task A: %d pairs, %d parallel workers", len(pairs), workers)
    ordered: dict[int, dict[str, Any]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_decide, (i, p)) for i, p in enumerate(pairs)]
        for fut in as_completed(futures):
            done += 1
            try:
                idx, decision = fut.result()
            except Exception as exc:
                log.warning("task A worker failed: %s", exc)
                continue
            ordered[idx] = decision
            if done % 10 == 0 or done == len(pairs):
                log.info("task A progress %d/%d pairs", done, len(pairs))
    decisions = [ordered[i] for i in sorted(ordered)]
    return decisions


def task_b_rca_verification(
    incident: dict[str, Any],
    candidate_nodes: list[str],
    cfg: PipelineConfig,
    backend: Any | None = None,
) -> dict[str, Any]:
    """Ask the LLM to verify GNN Top-K candidates and output final RCA JSON."""
    backend = backend or _backend(cfg)
    payload = {
        "incident_id": incident.get("incident_id"),
        "time_range": incident.get("time_range"),
        "candidate_nodes": candidate_nodes,
        "timeline": (incident.get("timeline") or [])[:50],
        "episodes": (incident.get("episodes") or [])[:50],
        "instruction": (
            "必须优先检查 first abnormal、upstream、downstream、metric evidence、"
            "FRR/trace/flow evidence 和 propagation consistency。"
        ),
    }
    prompt = (
        "你是 AIOps RCA 验证模块。对候选节点逐一验证并输出严格 JSON："
        '{"root_cause_top5":[{"rank":1,"network_element_id":"..."}],'
        '"fault_category":{"major_category":"...","sub_category":"..."},'
        '"reasoning_evidence":["..."]}\n'
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        resp = backend.chat(
            [
                {"role": "system", "content": "你是封闭集故障分类与根因验证模块，只输出严格 JSON。"},
                {"role": "user", "content": prompt},
            ],
            tools=[],
        )
        msg = resp.get("message", resp) if isinstance(resp, dict) else {}
        content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
        raw = _parse(content)
    except Exception as exc:
        log.warning("task B failed: %s", exc)
        raw = None
    if not isinstance(raw, dict):
        raw = {}
    cat = raw.get("fault_category")
    if isinstance(cat, dict):
        major, sub = str(cat.get("major_category", "")), str(cat.get("sub_category", ""))
        if not is_valid_category(major, sub):
            cat = None
    else:
        cat = None
    roots = []
    for item in raw.get("root_cause_top5") or []:
        if not isinstance(item, dict):
            continue
        neid = str(item.get("network_element_id", ""))
        if neid and neid in set(candidate_nodes) and all(r["network_element_id"] != neid for r in roots):
            roots.append({"rank": len(roots) + 1, "network_element_id": neid})
    return {
        "incident_id": incident.get("incident_id"),
        "root_cause_top5": roots,
        "fault_category": cat,
        "reasoning_evidence": [str(x) for x in (raw.get("reasoning_evidence") or [])],
        "anomaly_probability": float(raw.get("anomaly_probability", 0.0) or 0.0),
        "is_anomaly": bool(raw.get("is_anomaly", False)),
        "source": "llm_verification",
    }
