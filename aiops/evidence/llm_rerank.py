"""LLM re-ranking of program-generated candidates (spec 5.9).

Spec 5.9 splits the decision cleanly: **programs propose, the LLM disposes**.

    Candidate Generator (program) -> Top10 -> Evidence Filter -> Top5 -> LLM

The LLM is handed a closed candidate list and may only reorder it -- it must
never invent a node (spec section 8 lists "让 LLM 自由创造候选根因" among the
things F explicitly does not do).  This module therefore:

* renders the evidence the programs already computed into a compact prompt
  (supporting / contradicting items plus the ``RootScore`` breakdown);
* calls an OpenAI-compatible ``/v1/chat/completions`` endpoint with plain
  ``urllib`` so no extra client dependency is needed;
* parses the answer defensively and **intersects it with the allowed set** --
  a hallucinated node is dropped rather than trusted, and the drop is counted
  in the report so the failure is visible instead of silent.

It also implements the three prompt variants spec 4.2 asks for, because the
ablation is the only way to tell "the model is reading the evidence" from "the
model is reciting a prior".
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..utils import get_logger

log = get_logger(__name__)

#: Prompt variants for the spec 4.2 ablation.
#:   A  the full prompt
#:   B  the topology/dependency block removed, nothing else touched
#:   C  identical content, different wording and candidate order
VARIANTS = ("A", "B", "C")

_SYSTEM = (
    "你是网络故障根因分析专家。候选根因由程序依据证据筛选产生，"
    "你只能在给定候选之间排序，绝对不得引入候选之外的网元。"
)

_JSON_INSTRUCTION = (
    "只输出一个 JSON 对象，不要任何解释文字或 Markdown 代码块：\n"
    '{"ranking": ["候选节点名", "..."], "fault_sub_category": ["每个排名对应的故障子类或 null"]}'
)


def _format_candidate(index: int, candidate: dict, *, with_evidence: bool = True) -> str:
    node = candidate.get("node")
    scores = candidate.get("sub_scores") or {}
    head = (
        f"{index}. 节点 {node}  root_score={float(candidate.get('root_score') or 0.0):.3f} "
        f"(本地异常={scores.get('local_anomaly', 0):.2f} 时序优先={scores.get('temporal_priority', 0):.2f} "
        f"外传={scores.get('outgoing_propagation', 0):.2f} 跨模态支持={scores.get('cross_modal_support', 0):.2f})"
        + (f" 首次异常={candidate['first_anomaly_time']}" if candidate.get("first_anomaly_time") else "")
    )
    if not with_evidence:
        return head
    lines = [head]
    for item in (candidate.get("supporting") or [])[:4]:
        lines.append(f"     支持: {item.get('text', item)}")
    for item in candidate.get("contradicting") or []:
        lines.append(f"     反驳: {item}")
    return "\n".join(lines)


def build_prompt(incident: dict, candidates: list[dict], *, variant: str = "A", region: str = "") -> str:
    """Render one incident's candidate list into a prompt (spec 4.2 variants)."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; expected one of {VARIANTS}")

    tr = incident.get("time_range") or {}
    ordered = list(candidates)
    if variant == "C":
        # Same content, different order: if the answer tracks the order rather
        # than the evidence, the ranking is positional noise.
        ordered = list(reversed(ordered))

    with_evidence = True
    blocks = [_format_candidate(i, c, with_evidence=with_evidence) for i, c in enumerate(ordered, 1)]

    if variant == "B":
        header = (
            "下面是某次网络故障的候选根因清单（不含拓扑依赖信息）。"
            "请依据各候选自身的证据强弱排序。"
        )
    elif variant == "C":
        header = (
            "请判断下列网元中，哪一个最可能是本次故障的根因，并给出由强到弱的完整顺序。"
            "候选顺序不代表任何含义。"
        )
    else:
        header = (
            "下面是某次网络故障的候选根因清单。候选由程序按证据打分并排序；"
            "时间上更早异常、且其变化无法被其他候选解释的网元，更可能是根因。"
        )

    parts = [
        _SYSTEM,
        "",
        header,
        "",
        f"【故障信息】区域={region or incident.get('dataset', '')} "
        f"时间窗={tr.get('start')} ~ {tr.get('end')} 受影响网元数={len(incident.get('nodes') or [])}",
        "",
        "【候选根因】",
        *blocks,
        "",
        _JSON_INSTRUCTION,
    ]
    return "\n".join(parts)


def chat(
    base_url: str,
    model: str,
    prompt: str,
    *,
    timeout: float = 180.0,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> str:
    """POST to an OpenAI-compatible endpoint and return the message text."""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }
    if max_tokens:
        # Unbounded by default.  DeepSeek-R1's chat template injects " thinking"
        # unconditionally, so ANY ceiling risks cutting the reasoning chain
        # before the JSON answer appears -- measured: an 800-token cap broke
        # parsing on 158/640 incidents (24.7%).  A limit is applied only when
        # the caller explicitly asks for one.
        payload["max_tokens"] = max_tokens
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"]


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_ranking(text: str, allowed: list[str]) -> dict:
    """Extract the ranking, keeping only nodes that were actually offered.

    Returns ``{"ranking", "sub_categories", "unknown", "parsed"}``.  ``unknown``
    lists hallucinated nodes; a non-empty list means the model tried to escape
    the candidate set, which spec 5.9 forbids, so it is reported rather than
    silently repaired.
    """
    match = _JSON_BLOCK.search(text or "")
    if not match:
        return {"ranking": [], "sub_categories": [], "unknown": [], "parsed": False}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"ranking": [], "sub_categories": [], "unknown": [], "parsed": False}

    raw = data.get("ranking") or data.get("root_cause_top5") or []
    if isinstance(raw, str):
        raw = [raw]
    allowed_set = set(allowed)
    ranking, unknown = [], []
    for node in raw:
        name = str(node).strip()
        if name in allowed_set and name not in ranking:
            ranking.append(name)
        elif name:
            unknown.append(name)
    # Any offered candidate the model forgot is appended in its original
    # (highest-RootScore-first) order: the LLM re-ranks, it does not truncate.
    for node in allowed:
        if node not in ranking:
            ranking.append(node)
    subs = data.get("fault_sub_category") or []
    if not isinstance(subs, list):
        subs = [subs]
    return {"ranking": ranking, "sub_categories": subs, "unknown": unknown, "parsed": True}


def rerank_incident(
    incident: dict,
    candidates_payload: dict,
    *,
    base_url: str,
    model: str,
    variant: str = "A",
    region: str = "",
    timeout: float = 180.0,
) -> dict:
    """One incident through the LLM.  Never raises: failures are recorded."""
    candidates = list(candidates_payload.get("candidates") or [])
    allowed = [c.get("node") for c in candidates if c.get("node")]
    if not allowed:
        return {"llm_ranking": [], "error": "no candidates", "variant": variant}

    prompt = build_prompt(incident, candidates, variant=variant, region=region)
    try:
        text = chat(base_url, model, prompt, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as exc:
        return {
            "llm_ranking": allowed,
            "error": f"{type(exc).__name__}: {exc}",
            "variant": variant,
            "prompt_chars": len(prompt),
        }

    parsed = parse_ranking(text, allowed)
    return {
        "variant": variant,
        "llm_ranking": parsed["ranking"],
        "sub_categories": parsed["sub_categories"],
        "hallucinated": parsed["unknown"],
        "parsed": parsed["parsed"],
        "prompt_chars": len(prompt),
        "raw_chars": len(text or ""),
    }


def rerank_all(
    incidents: list[dict],
    candidates: dict,
    *,
    base_url: str,
    model: str,
    variant: str = "A",
    region: str = "",
    workers: int = 8,
    limit: int | None = None,
) -> dict:
    """Re-rank every incident that has candidates, in a small thread pool."""
    per_incident = candidates.get("incidents") or {}
    jobs = [
        (incident, per_incident.get(str(incident.get("incident_id"))))
        for incident in incidents
        if per_incident.get(str(incident.get("incident_id")))
    ]
    if limit:
        jobs = jobs[:limit]

    results: dict[str, dict] = {}

    def _run(job):
        incident, payload = job
        iid = str(incident.get("incident_id"))
        return iid, rerank_incident(
            incident,
            payload,
            base_url=base_url,
            model=model,
            variant=variant,
            region=region,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for iid, result in pool.map(_run, jobs):
            results[iid] = result

    errors = sum(1 for r in results.values() if r.get("error"))
    hallucinated = sum(1 for r in results.values() if r.get("hallucinated"))
    return {
        "variant": variant,
        "model": model,
        "base_url": base_url,
        "incidents": results,
        "summary": {
            "incidents": len(results),
            "errors": errors,
            "incidents_with_hallucinated_nodes": hallucinated,
            "parse_failures": sum(1 for r in results.values() if r.get("parsed") is False),
        },
    }
