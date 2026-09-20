"""Experiment C: dependency-only LLM RCA (no GNN).

This ablation removes GNN / RCA-GNN scores entirely.  The LLM receives only:
  - incident time range / timeline / episodes
  - candidate nodes
  - topology dependency relations (upstream / downstream / edge type)
  - propagation timeline if available

It is intended to answer whether the RCA improvement comes from the GNN or
from giving dependency relations directly to the LLM.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from aiops.config import PipelineConfig
from aiops.dataset import discover_datasets, official_network_element_ids
from aiops.incident_predictions import _normalize_result, _parse_json, merge_adjacent_incidents, save_routed
from aiops.llm_diagnoser import LLMBackend
from aiops.taxonomy import category_json_schema_text
from aiops.utils import get_logger

log = get_logger(__name__)


def _dependency_context(nodes: list[str], edges: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    node_set = set(nodes)
    for node in nodes:
        up = []
        down = []
        for e in edges:
            src, dst = str(e.get("source", "")), str(e.get("target", ""))
            typ = str(e.get("edge_type", "topology"))
            if dst == node and src:
                up.append(f"{src}({typ})")
            if src == node and dst:
                down.append(f"{dst}({typ})")
        lines.append(
            f"- {node}: upstream=[{', '.join(sorted(set(up))) or 'none'}], "
            f"downstream=[{', '.join(sorted(set(down))) or 'none'}]"
        )
    return "\n".join(lines)


def _build_prompt(incident: dict[str, Any], nodes: list[str], edges: list[dict[str, Any]]) -> list[dict[str, str]]:
    payload = {
        "incident_id": incident.get("merged_ids") or incident.get("incident_id"),
        "time_range": incident.get("time_range"),
        "nodes": nodes,
        "dependency_graph": _dependency_context(nodes, edges),
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
        "你是 AIOps 依赖关系根因分析模块。当前实验没有 GNN 和 RCA-GNN 分数，"
        "只能依据节点依赖关系、时间先后、异常传播和 incident 证据进行判断。"
        "输出严格 JSON，必须包含 anomaly_probability、is_anomaly、root_cause_top5、"
        "fault_category、reasoning_evidence。"
        "root_cause_top5 只能使用给定 nodes；故障类别必须从下面的封闭枚举中选择。"
        "同一持续故障必须合并，不要重复输出。\n"
        + category_json_schema_text()
    )
    user = (
        "请仅根据依赖关系和证据做根因排序与分类。特别注意：\n"
        "1. 先异常且位于上游的节点更可能是根因；\n"
        "2. 下游节点异常可能是传播结果；\n"
        "3. 节点依赖关系是主要依据，不得使用任何 GNN 分数。\n"
        "返回格式：{\"is_anomaly\":true,\"anomaly_probability\":0.0,"
        "\"root_cause_top5\":[{\"rank\":1,\"network_element_id\":\"...\"}],"
        "\"fault_category\":{\"major_category\":\"...\",\"sub_category\":\"...\"},"
        "\"reasoning_evidence\":[\"...\"]}\n"
        "事件证据：\n" + json.dumps(payload, ensure_ascii=False)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def main() -> int:
    p = argparse.ArgumentParser(description="Experiment C: dependency-only LLM RCA.")
    p.add_argument("--workspace", required=True)
    p.add_argument("--artifacts-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--llm-base-url", default="http://127.0.0.1:8000")
    p.add_argument("--llm-model", default="deepseek-r1-14b")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--merge-gap-minutes", type=int, default=5)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--llm-timeout", type=int, default=180)
    p.add_argument("--llm-max-new-tokens", type=int, default=256)
    args = p.parse_args()

    cfg = PipelineConfig(
        workspace=args.workspace,
        output_dir=args.output_dir,
        llm_backend="openai_compatible",
        llm_base_url=args.llm_base_url,
        llm_model=args.llm_model,
        llm_tool_calling=False,
        llm_timeout_seconds=args.llm_timeout,
        llm_max_new_tokens=args.llm_max_new_tokens,
        experiment_mode="full",
    )
    backend = LLMBackend(cfg)
    artifacts = Path(args.artifacts_dir)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    datasets = {d.name: d for d in discover_datasets(args.workspace)}
    all_high: list[dict[str, Any]] = []
    all_low: list[dict[str, Any]] = []
    all_meta: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []

    for ds_name, ds in sorted(datasets.items()):
        ds_dir = artifacts / ds_name
        inc_path = ds_dir / "incident_candidates.json"
        topo_path = ds_dir / "topology.json"
        if not inc_path.exists():
            log.warning("skip %s: missing %s", ds_name, inc_path)
            continue
        incidents = json.loads(inc_path.read_text(encoding="utf-8")).get("incidents", [])
        topology = json.loads(topo_path.read_text(encoding="utf-8")) if topo_path.exists() else {"edges": []}
        edges = topology.get("edges", []) or []
        merged = merge_adjacent_incidents(incidents, args.merge_gap_minutes)
        fallback_nodes = [n for n in official_network_element_ids(ds.region_code)]
        high: list[dict[str, Any]] = []
        low: list[dict[str, Any]] = []
        meta: list[dict[str, Any]] = []

        def work(idx_inc: tuple[int, dict[str, Any]]) -> None:
            idx, incident = idx_inc
            nodes = [str(n) for n in incident.get("nodes", []) if str(n) in set(fallback_nodes)]
            raw = None
            if nodes:
                try:
                    resp = backend.chat(_build_prompt(incident, nodes, edges), tools=[])
                    msg = resp.get("message", resp) if isinstance(resp, dict) else {}
                    content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                    raw = _parse_json(content)
                except Exception as exc:
                    log.warning("experiment C incident %s failed: %s", incident.get("incident_id"), exc)
            normalized = _normalize_result(raw, incident, nodes, {}, cfg, fallback_nodes)
            pid = f"pred_C_{ds.name}_{idx:06d}"
            pred = {
                "prediction_id": pid,
                "start_time": incident.get("time_range", {}).get("start"),
                "end_time": incident.get("time_range", {}).get("end"),
                "root_cause_top5": normalized["root_cause_top5"],
                "fault_category": normalized["fault_category"],
            }
            meta_item = {
                "prediction_id": pid,
                "incident_id": incident.get("incident_id"),
                "anomaly_probability": normalized["anomaly_probability"],
                "is_anomaly": normalized["is_anomaly"],
                "reasoning_evidence": normalized["reasoning_evidence"],
                "source": "dependency_llm" if raw is not None else "fallback",
            }
            if normalized["is_anomaly"] and normalized["anomaly_probability"] >= args.threshold:
                high.append(pred)
            else:
                low.append(pred)
            meta.append(meta_item)

        with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as pool:
            futures = [pool.submit(work, (i, inc)) for i, inc in enumerate(merged, 1)]
            for fut in as_completed(futures):
                fut.result()
        save_routed(out_root / ds_name, high, low, meta)
        all_high.extend(high)
        all_low.extend(low)
        all_meta.extend(meta)
        summary.append({
            "dataset": ds_name,
            "incidents": len(incidents),
            "merged": len(merged),
            "high_conf": len(high),
            "low_conf": len(low),
            "mode": "dependency_only",
        })
        log.info("experiment C %s done: high=%d low=%d", ds_name, len(high), len(low))

    def _write(path: Path, rows: list[dict[str, Any]]) -> None:
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    _write(out_root / "predictions_high_conf.jsonl", all_high)
    _write(out_root / "predictions_low_conf.jsonl", all_low)
    _write(out_root / "llm_anomaly_scores.jsonl", all_meta)
    (out_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "output_dir": str(out_root)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
