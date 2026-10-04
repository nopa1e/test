#!/usr/bin/env python3
"""Stratified, auditable probe for information in incident-level evidence.

Require evidence from the current experiment via --artifacts. The preparation
mode assembles prompts without contacting an LLM, so schema and sample selection
can be reviewed before any GPU time is spent.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SYSTEM = (
    "你是一名资深网络故障诊断专家，熟悉 BGP/OSPF、防火墙、链路和主机资源故障。"
    "只使用输入中的观测，不臆测缺失事实；用简短、可核对的证据说明判断，不输出隐含推理过程。"
)

PROMPT = """一次网络事故的证据如下。
区域：{region}
事故时间窗：{start} ~ {end}

设备指标证据：
{evidence}

真实拓扑边：
{topology}

问题：哪台设备最可能是无法由其他设备异常解释的根因？如果证据无法区分根因与受害者，请明确说明不确定。

要求：
1. 只引用上面确实出现的设备、指标和值；不得编造拓扑关系。
2. `peak_z` 可能在基线波动极小时不稳定；单个极大值不能单独证明根因，请指出是否有其他独立指标或拓扑/时间证据佐证。
3. 用简短理由指出关键支持证据和最强反证。

只输出 JSON：
{{"root_cause":"<设备名>","reason":"<简短证据说明>","excluded":["<设备及理由>"],"confidence":<0到1>}}
"""


def call_llm(base_url: str, model: str, prompt: str, timeout: int, max_tokens: int) -> str:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    url = base_url.rstrip("/") + "/v1/chat/completions"
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def parse_json(text: str) -> dict | None:
    text = re.sub(r"```(?:json)?", "", text, flags=re.IGNORECASE)
    found = None
    for match in re.finditer(r"\{", text):
        depth = 0
        quoted = False
        escaped = False
        for i in range(match.start(), len(text)):
            ch = text[i]
            if quoted:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    quoted = False
                continue
            if ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        item = json.loads(text[match.start():i + 1])
                        if isinstance(item, dict) and "root_cause" in item:
                            found = item
                    except json.JSONDecodeError:
                        pass
                    break
    return found


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def incident_window(incident: dict) -> tuple[str, str]:
    windows = []
    for payload in (incident.get("nodes") or {}).values():
        window = payload.get("incident_window") or []
        if len(window) == 2 and window[0] and window[1]:
            windows.append((str(window[0]), str(window[1])))
    if not windows:
        return "UNKNOWN", "UNKNOWN"
    return min(x[0] for x in windows), max(x[1] for x in windows)


def build_evidence(nodes: dict) -> tuple[str, float]:
    """Format all node metrics and return a log-scaled top-node separation."""
    lines = []
    node_scores = []
    for node, payload in sorted(nodes.items()):
        metrics = payload.get("metrics") or {}
        if not metrics:
            lines.append(f"- {node}: no node metric evidence")
            continue
        ranked = []
        for name, metric in metrics.items():
            metric = metric if isinstance(metric, dict) else {}
            z = _number(metric.get("peak_z"))
            severity = _number(metric.get("severity"))
            ranked.append((severity if severity is not None else (z or 0.0), name, metric))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        z_values = [_number(m.get("peak_z")) for _, _, m in ranked]
        valid_z = [z for z in z_values if z is not None]
        if valid_z:
            node_scores.append((math.log1p(max(0.0, max(valid_z))), node))
        fields = []
        for _, name, metric in ranked:
            bits = []
            for key, label in (("peak_z", "peak_z"), ("relative_change", "rel"),
                               ("incident_peak", "peak"), ("baseline_median", "base_med"),
                               ("baseline_p95", "base_p95"), ("drop_ratio", "drop")):
                value = _number(metric.get(key))
                if value is not None:
                    bits.append(f"{label}={value:.4g}")
            peak_time = metric.get("peak_time") or metric.get("first_anomaly_time")
            if peak_time:
                bits.append(f"time={peak_time}")
            fields.append(f"{name}[{', '.join(bits)}]")
        lines.append(f"- {node}: " + "; ".join(fields))

        # Interface detail is useful for link/firewall faults; keep the strongest
        # three metrics per interface to bound prompt size while retaining names.
        for interface, imetrics in sorted((payload.get("interfaces") or {}).items()):
            ranked_i = []
            for name, metric in (imetrics or {}).items():
                metric = metric if isinstance(metric, dict) else {}
                severity = _number(metric.get("severity")) or 0.0
                ranked_i.append((severity, name, metric))
            ranked_i.sort(key=lambda x: (-x[0], x[1]))
            bits = []
            for _, name, metric in ranked_i[:3]:
                z = _number(metric.get("peak_z"))
                rel = _number(metric.get("relative_change"))
                detail = [f"peak_z={z:.4g}" if z is not None else ""]
                if rel is not None:
                    detail.append(f"rel={rel:.4g}")
                bits.append(f"{name}[{', '.join(x for x in detail if x)}]")
            if bits:
                lines.append(f"  interface {node}/{interface}: " + "; ".join(bits))

    node_scores.sort(reverse=True)
    gap = node_scores[0][0] - node_scores[1][0] if len(node_scores) > 1 else 0.0
    return "\n".join(lines), gap


def topology_text(topology: dict, region: str, incident_nodes: set[str]) -> tuple[str, int]:
    full_nodes = set(topology.get("nodes") or [])
    wanted = set()
    for node in incident_nodes:
        candidate = node if node in full_nodes else f"{region}-{node}"
        if candidate in full_nodes:
            wanted.add(candidate)
    edges = []
    for edge in topology.get("edges") or []:
        src, dst = edge.get("source"), edge.get("target")
        if src not in wanted or dst not in wanted:
            continue
        arrow = "->" if edge.get("directed", False) else "<->"
        label = edge.get("edge_type") or edge.get("source_kind") or "edge"
        edges.append(f"{src} {arrow} {dst} [{label}]")
    if not edges:
        return "No topology edge connects the incident nodes in this topology file.", 0
    return "\n".join(sorted(set(edges))), len(set(edges))


def _start_key(item: dict) -> str:
    return item.get("start") or ""


def select_stratified(candidates: list[dict], limit: int, min_gap: float) -> list[dict]:
    if len(candidates) <= limit:
        return sorted(candidates, key=lambda x: _start_key(x["window"]))
    clean = sorted((x for x in candidates if x["gap"] >= min_gap),
                   key=lambda x: (-x["gap"], _start_key(x["window"])))
    clean_count = min((limit + 1) // 2, len(clean))
    selected = list(clean[:clean_count])
    selected_ids = {x["iid"] for x in selected}
    remainder = sorted((x for x in candidates if x["iid"] not in selected_ids),
                       key=lambda x: _start_key(x["window"]))
    needed = limit - len(selected)
    if needed and remainder:
        positions = [round(i * (len(remainder) - 1) / max(needed - 1, 1)) for i in range(needed)]
        for pos in positions:
            candidate = remainder[pos]
            if candidate["iid"] not in selected_ids:
                selected.append(candidate)
                selected_ids.add(candidate["iid"])
    if len(selected) < limit:
        for candidate in remainder:
            if candidate["iid"] not in selected_ids:
                selected.append(candidate)
                selected_ids.add(candidate["iid"])
                if len(selected) == limit:
                    break
    return sorted(selected, key=lambda x: _start_key(x["window"]))


def discover_dataset_dirs(artifacts: Path, regions: set[str] | None) -> list[Path]:
    paths = sorted(artifacts.glob("*/metric_evidence.json"))
    result = []
    for path in paths:
        region = path.parent.name.split("_", 1)[0]
        if regions is None or region in regions:
            result.append(path.parent)
    return result


def prepare(args: argparse.Namespace) -> list[dict]:
    artifact_root = Path(args.artifacts)
    topology_root = Path(args.topology_dir) if args.topology_dir else artifact_root
    regions = None if args.regions.strip().lower() == "all" else {
        x.strip() for x in args.regions.split(",") if x.strip()
    }
    dataset_dirs = discover_dataset_dirs(artifact_root, regions)
    if not dataset_dirs:
        raise SystemExit(f"No metric_evidence.json found under {artifact_root}")

    picked = []
    for dataset_dir in dataset_dirs:
        dataset = dataset_dir.name
        region = dataset.split("_", 1)[0]
        metric_path = dataset_dir / "metric_evidence.json"
        topology_path = topology_root / dataset / "topology.json"
        if not topology_path.is_file():
            raise SystemExit(f"Missing topology file for {dataset}: {topology_path}")
        metric_doc = json.loads(metric_path.read_text(encoding="utf-8"))
        topology = json.loads(topology_path.read_text(encoding="utf-8"))
        candidates = []
        for iid, incident in (metric_doc.get("incidents") or {}).items():
            nodes = incident.get("nodes") or {}
            if len(nodes) < 2:
                continue
            evidence, gap = build_evidence(nodes)
            start, end = incident_window(incident)
            topo, edge_count = topology_text(topology, region, set(nodes))
            prompt = PROMPT.format(region=region, start=start, end=end,
                                   evidence=evidence, topology=topo)
            candidates.append({
                "region": region, "dataset": dataset, "iid": iid,
                "window": {"start": start, "end": end}, "gap": gap,
                "topology_edges": edge_count, "prompt": prompt,
            })
        chosen = select_stratified(candidates, args.limit, args.min_gap)
        print(f"{dataset}: evidence={len(candidates)} selected={len(chosen)} "
              f"with_time={sum(x['window']['start'] != 'UNKNOWN' for x in chosen)} "
              f"with_topology={sum(x['topology_edges'] > 0 for x in chosen)}")
        picked.extend(chosen)
    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions", default="all", help="all or comma-separated region names")
    parser.add_argument("--limit", type=int, default=10, help="sample count per dataset/batch")
    parser.add_argument("--min-gap", type=float, default=1.0,
                        help="log-scaled node-score gap for the clean half of each sample")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--artifacts", required=True,
                        help="metric_evidence root from this experiment")
    parser.add_argument("--topology-dir", default=None,
                        help="root containing matching <dataset>/topology.json; defaults to --artifacts")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="deepseek-r1-14b")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--prepare-only", action="store_true",
                        help="write selected prompts without making any LLM requests")
    parser.add_argument("--out", default="f_llm_probe_out.json")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")

    picked = prepare(args)
    print(f"Prepared {len(picked)} prompts from {len({x['dataset'] for x in picked})} dataset batches")
    if args.prepare_only:
        results = [{**{k: x[k] for k in ("region", "dataset", "iid", "window", "gap", "topology_edges")},
                     "prompt_sha256": hashlib.sha256(x["prompt"].encode()).hexdigest(),
                     "prompt": x["prompt"], "prepared_only": True} for x in picked]
        Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"No LLM request sent; prepared prompts -> {args.out}")
        return

    def work(item: dict) -> dict:
        t0 = time.time()
        try:
            raw = call_llm(args.base_url, args.model, item["prompt"], args.timeout, args.max_tokens)
            parsed = parse_json(raw)
            return {
                **{k: item[k] for k in ("region", "dataset", "iid", "window", "gap", "topology_edges")},
                "prompt_sha256": hashlib.sha256(item["prompt"].encode()).hexdigest(),
                "prompt": item["prompt"], "parsed": parsed, "raw_len": len(raw),
                "raw_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "raw_excerpt": raw[:800], "elapsed_seconds": round(time.time() - t0, 2),
            }
        except Exception as exc:
            return {
                **{k: item[k] for k in ("region", "dataset", "iid", "window", "gap", "topology_edges")},
                "prompt_sha256": hashlib.sha256(item["prompt"].encode()).hexdigest(),
                "prompt": item["prompt"], "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "elapsed_seconds": round(time.time() - t0, 2),
            }

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        results = list(executor.map(work, picked))
    parsed = [r for r in results if r.get("parsed")]
    reasons = [str(r["parsed"].get("reason") or "") for r in parsed]
    roots = [str(r["parsed"].get("root_cause") or "") for r in parsed]
    print(f"Completed {len(parsed)}/{len(results)} parsed in {time.time() - started:.1f}s; "
          f"distinct roots={len(set(roots))}, distinct reasons={len(set(reasons))}")
    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Results -> {args.out}")


if __name__ == "__main__":
    main()
