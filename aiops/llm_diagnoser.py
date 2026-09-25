from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

from .config import PipelineConfig
from .dataset import DatasetInfo, official_network_element_ids
from .taxonomy import (
    FAULT_NAME_TO_PAIR,
    category_json_schema_text,
    fault_name,
    infer_category_from_evidence,
    is_valid_category,
)
from .utils import dump_json, get_logger, to_iso

log = get_logger(__name__)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_dependency",
            "description": "Get direct upstream and downstream dependencies of a network element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_element_id": {"type": "string"},
                    "dataset": {"type": "string"},
                },
                "required": ["network_element_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trace_upstream",
            "description": "Walk reverse dependency/trace edges to find parent/upstream elements and their anomaly scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_element_id": {"type": "string"},
                    "depth": {"type": "integer", "default": 3},
                    "dataset": {"type": "string"},
                },
                "required": ["network_element_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trace_downstream",
            "description": "Walk forward dependency edges to find child/downstream elements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_element_id": {"type": "string"},
                    "depth": {"type": "integer", "default": 3},
                    "dataset": {"type": "string"},
                },
                "required": ["network_element_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_node_anomaly",
            "description": "Return the normal-score ranking and top anomalous points of a network element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_element_id": {"type": "string"},
                    "dataset": {"type": "string"},
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["network_element_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_anomaly_points",
            "description": "Return the globally most anomalous points (lowest normal score first).",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20},
                    "dataset": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_metric_evidence",
            "description": "Return aggregated feature-level evidence for a network element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_element_id": {"type": "string"},
                    "dataset": {"type": "string"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["network_element_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_frr_events",
            "description": "Query FRR Syslog events in a time window and optional node filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "network_element_id": {"type": "string"},
                    "dataset": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_temporal_neighbors",
            "description": "Return nearby points and topology-related nodes around a timestamp.",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_element_id": {"type": "string"},
                    "timestamp": {"type": "string"},
                    "window_minutes": {"type": "integer", "default": 15},
                    "dataset": {"type": "string"},
                },
                "required": ["network_element_id", "timestamp"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incident_candidates",
            "description": "List incident candidates generated from temporal episodes.",
            "parameters": {
                "type": "object",
                "properties": {"dataset": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_incident_timeline",
            "description": "Return the full timeline of an incident candidate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "dataset": {"type": "string"},
                },
                "required": ["incident_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_incidents",
            "description": "Compare two incident candidates on temporal/topology/signature evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_a": {"type": "string"},
                    "incident_b": {"type": "string"},
                    "dataset": {"type": "string"},
                },
                "required": ["incident_a", "incident_b"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_propagation_chain",
            "description": "Return the candidate fault propagation chain for an incident.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "dataset": {"type": "string"},
                },
                "required": ["incident_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fault_signature",
            "description": "Return the metric/event signature of an incident.",
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                    "dataset": {"type": "string"},
                },
                "required": ["incident_id"],
            },
        },
    },

]


def _system_prompt() -> str:
    return f"""你是 CCF AIOps 故障诊断系统的故障分类与根因输出模块。

最高优先级：故障类别必须使用封闭枚举，只能从下面的合法组合中选择；
绝对禁止 unknown、other、none、uncertain、undefined、N/A、空字符串或自造类别。

合法类别（major_category / sub_category）:
{category_json_schema_text()}

输出必须是严格合法 JSON，字段只能有:
prediction_id, start_time, end_time, root_cause_top5, fault_category。
root_cause_top5 的 rank 从 1 连续，network_element_id 不得重复，最多 5 个。
start_time 和 end_time 必须是带时区的 ISO-8601 时间。
合规要求：只能使用当前输入证据和训练数据；禁止访问测试集标签、答案文件或人工填写的测试结果。

诊断原则：
1. 不要只看业务最终症状，要优先定位机制。
2. 如果某个点的异常响应大，而它的父服务/上游节点异常分数更高或同样异常，
   根因更可能是上游父节点；必须调用 get_dependency / get_trace_upstream 核实。
3. 如果子节点异常但上游正常，则子节点更可能是根因。
4. 综合 Metrics、FRR Syslog、NetFlow、拓扑和时间相关性判断。
"""


def _evidence_text(evidence: dict[str, Any], node_scores: list[dict[str, Any]]) -> str:
    top_points = evidence.get("top_points", [])[:25]
    lines = [
        f"数据集: {evidence.get('dataset')}",
        f"区域: {evidence.get('region_code')}",
        f"时间范围: {evidence.get('time_start')} ~ {evidence.get('time_end')}",
        "节点异常排名(前10):",
    ]
    for n in node_scores[:10]:
        lines.append(
            f"  - {n.get('network_element_id')} anomaly={n.get('anomaly_score'):.4f} "
            f"type={n.get('node_type')}"
        )
    lines.append("最异常的点(前25, normal score 越低越异常):")
    for p in top_points:
        feats = ", ".join(
            f"{f.get('name')}={f.get('value')}(z={f.get('robust_z')})"
            for f in (p.get("top_features") or [])[:4]
        )
        lines.append(
            f"  - rank={p.get('rank')} {p.get('network_element_id')} @ {p.get('timestamp')} "
            f"normal={p.get('normal_score'):.4f}; {feats}"
        )
    lines.append("拓扑边(上游->下游):")
    for e in (evidence.get("topology", {}) or {}).get("edges", [])[:60]:
        lines.append(f"  - {e.get('source')} -> {e.get('target')} ({e.get('edge_type')})")
    return "\n".join(lines)


class LLMBackend:
    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        backend = (self.cfg.llm_backend or "none").lower()
        if backend == "ollama":
            return self._chat_ollama(messages, tools)
        if backend in {"openai", "openai_compatible", "openai-compatible"}:
            return self._chat_openai_compatible(messages, tools)
        if backend in {"transformers", "hf", "local"}:
            return self._chat_transformers(messages, tools)
        raise ValueError(f"unsupported LLM backend: {self.cfg.llm_backend}")

    def _chat_ollama(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        import ollama

        resp = ollama.chat(
            model=self.cfg.llm_model,
            messages=messages,
            tools=tools,
            stream=False,
            options={"temperature": 0.0},
        )
        msg = getattr(resp, "message", None)
        if msg is None and isinstance(resp, dict):
            msg = resp.get("message", resp)
        if hasattr(msg, "model_dump"):
            msg = msg.model_dump()
        if not isinstance(msg, dict):
            msg = dict(msg or {})
        return {"message": msg}

    def _chat_transformers(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """Local HuggingFace Transformers backend for the 14B model."""
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        model_path = str(self.cfg.llm_model)
        if getattr(self, "_hf_tokenizer", None) is None:
            local = Path(model_path).exists()
            self._hf_tokenizer = AutoTokenizer.from_pretrained(
                model_path, local_files_only=local, trust_remote_code=False
            )
            kwargs = {
                "trust_remote_code": False,
                "local_files_only": local,
                "low_cpu_mem_usage": True,
            }
            if torch.cuda.is_available():
                kwargs.update({"dtype": torch.bfloat16, "device_map": {"": 0}})
            try:
                self._hf_model = AutoModelForCausalLM.from_pretrained(model_path, **kwargs)
            except TypeError:
                if "dtype" in kwargs:
                    kwargs.pop("dtype")
                    kwargs["torch_dtype"] = torch.bfloat16
                self._hf_model = AutoModelForCausalLM.from_pretrained(model_path, **kwargs)
            self._hf_model.eval()

        rendered = self._hf_tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        rendered += "</think>\n"
        inputs = self._hf_tokenizer(
            rendered, return_tensors="pt", truncation=True, max_length=8192
        )
        device = next(self._hf_model.parameters()).device
        inputs = {k: v.to(device) for k, v in inputs.items()}
        with torch.inference_mode():
            output = self._hf_model.generate(
                **inputs,
                max_new_tokens=int(self.cfg.llm_max_new_tokens),
                do_sample=False,
                pad_token_id=self._hf_tokenizer.eos_token_id,
            )
        generated = output[0, inputs["input_ids"].shape[-1]:]
        text = self._hf_tokenizer.decode(generated, skip_special_tokens=True)
        return {"message": {"content": text}}

    def _chat_openai_compatible(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        base = self.cfg.llm_base_url.rstrip("/")
        url = f"{base}/v1/chat/completions"
        use_tools = bool(getattr(self.cfg, "llm_tool_calling", True)) and bool(tools)
        payload = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "temperature": 0.0,
        }
        if use_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.cfg.llm_api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout_seconds) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
        return {"message": body["choices"][0]["message"]}


def _parse_json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    text = str(text).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    # Find the first balanced object if the model added chatter.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    candidate = text[start:end + 1] if end >= 0 else text[start:]
    try:
        obj = json.loads(candidate)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _normalize_tool_calls(msg: dict[str, Any]) -> list[dict[str, Any]]:
    calls = msg.get("tool_calls") or []
    out: list[dict[str, Any]] = []
    for c in calls:
        if hasattr(c, "model_dump"):
            c = c.model_dump()
        if not isinstance(c, dict):
            continue
        fn = c.get("function", {}) or {}
        if hasattr(fn, "model_dump"):
            fn = fn.model_dump()
        name = fn.get("name") if isinstance(fn, dict) else None
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        out.append(
            {
                "id": c.get("id") or f"call_{len(out) + 1}",
                "name": name,
                "arguments": args or {},
            }
        )
    return out


def _allowed_ids(ds: DatasetInfo, evidence: dict[str, Any], node_scores: list[dict[str, Any]]) -> list[str]:
    official = set(official_network_element_ids(ds.region_code))
    ids: list[str] = []
    for n in node_scores:
        ids.append(str(n.get("network_element_id", "")))
    for n in evidence.get("nodes", []) or []:
        ids.append(str(n))
    seen = set()
    out = []
    for x in ids:
        if x and x in official and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _finalize_prediction(
    ds: DatasetInfo,
    evidence: dict[str, Any],
    node_scores: list[dict[str, Any]],
    raw: dict[str, Any] | None,
    prediction_id: str | None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    raw_cat = raw.get("fault_category")
    major = ""
    sub = ""
    if isinstance(raw_cat, dict):
        major = str(raw_cat.get("major_category", "") or "")
        sub = str(raw_cat.get("sub_category", "") or "")
    elif isinstance(raw_cat, str):
        text = raw_cat.strip()
        pair = FAULT_NAME_TO_PAIR.get(text)
        if pair is None:
            for sep in ("/", ".", ":", "|"):
                if sep in text:
                    a, b = text.split(sep, 1)
                    if is_valid_category(a.strip(), b.strip()):
                        pair = (a.strip(), b.strip())
                        break
        if pair is not None:
            major, sub = pair
    if not is_valid_category(major, sub):
        major, sub = infer_category_from_evidence(evidence, node_scores)

    allowed = _allowed_ids(ds, evidence, node_scores)
    allowed_set = set(allowed)
    final_roots: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_roots = raw.get("root_cause_top5", []) if isinstance(raw.get("root_cause_top5"), list) else []
    for item in raw_roots:
        if not isinstance(item, dict):
            continue
        neid = str(item.get("network_element_id", ""))
        if neid and neid in allowed_set and neid not in seen:
            seen.add(neid)
            final_roots.append({"rank": len(final_roots) + 1, "network_element_id": neid})
    for n in node_scores:
        neid = str(n.get("network_element_id", ""))
        if neid and neid in allowed_set and neid not in seen:
            seen.add(neid)
            final_roots.append({"rank": len(final_roots) + 1, "network_element_id": neid})
        if len(final_roots) == 5:
            break
    if not final_roots:
        for neid in allowed[:5]:
            final_roots.append({"rank": len(final_roots) + 1, "network_element_id": neid})

    start = raw.get("start_time")
    end = raw.get("end_time")
    ok_time = False
    try:
        s = pd.to_datetime(start, utc=True)
        e = pd.to_datetime(end, utc=True)
        if pd.notna(s) and pd.notna(e) and s < e:
            start, end = to_iso(s), to_iso(e)
            ok_time = True
    except Exception:
        ok_time = False
    if not ok_time:
        raw_times = [p.get("timestamp") for p in evidence.get("top_points", []) if p.get("timestamp")]
        parsed = [pd.to_datetime(t, utc=True) for t in raw_times[:5]]
        parsed = [t for t in parsed if pd.notna(t)]
        if parsed:
            top_ts = parsed[0]
            if len(parsed) > 1 and (max(parsed) - min(parsed)) <= pd.Timedelta(hours=2):
                s, e = min(parsed), max(parsed)
            else:
                s, e = top_ts - pd.Timedelta(minutes=5), top_ts + pd.Timedelta(minutes=5)
        else:
            s = pd.to_datetime(evidence.get("time_start"), utc=True)
            e = pd.to_datetime(evidence.get("time_end"), utc=True)
            if pd.isna(s):
                s = pd.Timestamp.utcnow()
            if pd.isna(e) or e <= s:
                e = s + pd.Timedelta(minutes=5)
        start, end = to_iso(s), to_iso(e)

    return {
        "prediction_id": str(raw.get("prediction_id") or prediction_id or f"pred_{ds.region_code}_00000001"),
        "start_time": start,
        "end_time": end,
        "root_cause_top5": final_roots[:5],
        "fault_category": {"major_category": major, "sub_category": sub},
    }


def diagnose_dataset(
    ds: DatasetInfo,
    cfg: PipelineConfig,
    evidence: dict[str, Any],
    node_scores: list[dict[str, Any]],
    out_dir: str | Path | None = None,
    prediction_id: str | None = None,
) -> dict[str, Any]:
    from .mcp_server import DiagnosisToolbox

    toolbox = DiagnosisToolbox(cfg.output_dir, workspace=cfg.workspace)
    backend = LLMBackend(cfg)
    user_text = _evidence_text(evidence, node_scores)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system_prompt()},
        {
            "role": "user",
            "content": (
                "请根据以下检测证据完成根因诊断。"
                "当发现异常点时，必须调用 get_dependency 和 get_trace_upstream 检查其父服务/上游是否才是根因。"
                "调用工具后，最终只输出合法 JSON。\n\n" + user_text
            ),
        },
    ]

    final_raw: dict[str, Any] | None = None
    for round_no in range(1, int(cfg.max_llm_tool_rounds) + 1):
        resp = backend.chat(messages, TOOL_SCHEMAS)
        msg = resp.get("message", resp) if isinstance(resp, dict) else {}
        if hasattr(msg, "model_dump"):
            msg = msg.model_dump()
        if not isinstance(msg, dict):
            msg = {}
        content = msg.get("content") or ""
        calls = _normalize_tool_calls(msg)
        assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
        if calls:
            assistant_message["tool_calls"] = [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)},
                }
                for c in calls
            ]
        messages.append(assistant_message)

        if calls:
            for c in calls:
                try:
                    result = toolbox.call_tool(c["name"], c["arguments"])
                except Exception as exc:
                    result = {"error": str(exc), "tool": c["name"]}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "name": c["name"],
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
            log.info("LLM tool round %d: %s", round_no, [c["name"] for c in calls])
            continue

        raw = _parse_json_object(content)
        if raw is not None:
            final_raw = raw
            break
        messages.append(
            {
                "role": "user",
                "content": "你没有输出可解析的 JSON。请重新只输出规定的 JSON，不要解释。",
            }
        )

    prediction = _finalize_prediction(ds, evidence, node_scores, final_raw, prediction_id)
    if out_dir is not None:
        dump_json(Path(out_dir) / "prediction_llm.json", prediction)
    return prediction
