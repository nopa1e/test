"""把一条 incident 的多模态证据压成「指纹」并渲染成可编码的文本。

指纹是结构化 dict（便于分析），render_text() 把它线性化成 BERT 能吃的句子。
通配符设计：region / node 名这类**实例值**在模板中应被视作槽位，
因此 render_text 用 region 无关的描述（role 而非 node id）来提升跨区域泛化。
"""
from __future__ import annotations

import re
from typing import Any

ROLE_RE = re.compile(r"(service-vm-\d+|traffic-vm|monitor-vm|br-\d+|cr-\d+|fw)")


def role_of(node: Any) -> str:
    m = ROLE_RE.search(str(node))
    return m.group(1) if m else "?"


def _num(x: Any, nd: int = 2) -> str:
    try:
        return f"{float(x):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _bucket(v: Any, edges: tuple[float, ...]) -> str:
    """把连续量离散成档位，让相似量级落到同一模板。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "none"
    labels = ["none", "low", "mid", "high", "extreme"]
    for i, e in enumerate(edges):
        if x < e:
            return labels[i]
    return labels[len(edges)]


def build_fingerprint(incident: dict, metric_ev: dict, candidates: dict,
                      flow_ev: dict | None = None, routing_ev: dict | None = None,
                      category: dict | None = None) -> dict:
    iid = str(incident.get("incident_id"))
    nodes = [role_of(n) for n in (incident.get("nodes") or [])]

    # --- 各节点指标的异常档位 ---
    per_node: dict[str, dict] = {}
    m_nodes = (((metric_ev or {}).get("incidents") or {}).get(iid) or {}).get("nodes") or {}
    for role, payload in m_nodes.items():
        cpu = ((payload.get("metrics") or {}).get("cpu_usage")) or {}
        mem = ((payload.get("metrics") or {}).get("memory_available_ratio")) or {}
        disk = ((payload.get("metrics") or {}).get("disk_io_util")) or {}
        per_node[role_of(role)] = {
            "cpu_peak": cpu.get("incident_peak"),
            "cpu_z": cpu.get("peak_z"),
            "cpu_bucket": _bucket(cpu.get("incident_peak"), (5, 15, 35)),
            "cpu_zbucket": _bucket(cpu.get("peak_z"), (3, 8, 20)),
            "mem_bucket": _bucket(mem.get("relative_change"), (0.2, 0.5, 1.0)),
            "disk_bucket": _bucket(disk.get("incident_peak"), (10, 40, 80)),
        }

    # --- 候选排序 ---
    pay = (candidates.get("incidents") or {}).get(iid) or {}
    top5 = [role_of(x) for x in (pay.get("top5") or [])]
    top10 = [role_of(x) for x in (pay.get("top10") or [])]

    # --- flow 证据（应用层）---
    flow = {}
    fe = (((flow_ev or {}).get("incidents") or {}).get(iid) or {})
    for k in ("web_5xx", "web_slow", "dns_down", "dns_wrong_record",
              "auth_error", "auth_timeout"):
        v = fe.get(k)
        flow[k] = "none" if not v else (v if isinstance(v, str) else "yes")

    # --- routing 证据（协议层）---
    routing = {}
    re_ = (((routing_ev or {}).get("incidents") or {}).get(iid) or {})
    evs = re_.get("events") or []
    routing["n_events"] = len(evs)
    routing["subs"] = sorted({str(e.get("hints_sub_category")) for e in evs if e.get("hints_sub_category")})[:3]
    routing["real"] = any(str(e.get("metric_name")) in {
        "bgp_peer_state", "bgp_peer_uptime_seconds", "ospf6_neighbor_state",
    } for e in evs)

    ts = incident.get("time_range") or {}
    return {
        "incident_id": iid,
        "region": (iid.split("_")[1] if "_" in iid else "?"),
        "duration_min": incident.get("duration_minutes"),
        "episode_count": incident.get("episode_count"),
        "seeds": sorted(nodes),
        "per_node": per_node,
        "top5": top5,
        "top10": top10,
        "flow": flow,
        "routing": routing,
        "label": (category or {}).get("major_category"),
        "sublabel": (category or {}).get("sub_category"),
        "start": ts.get("start"),
    }


def render_text(fp: dict) -> str:
    """把指纹线性化为 BERT 输入文本。

    刻意使用 **role** 而不是 ``beida-fw`` 这种实例名 —— region 就是你说的
    「通配符槽位」，模板要跨区域可复用，编码时就必须剥掉实例信息。
    """
    parts = [
        f"anomaly incident duration {fp.get('duration_min')} min "
        f"episodes {fp.get('episode_count')} seed {' '.join(fp.get('seeds') or [])}",
    ]
    # 只写"有异常迹象"的节点：全 none 的节点不携带信息，写进去只会
    # 稀释向量、拖慢编码（实测文本长度约 -60%）。
    for role, m in sorted((fp.get("per_node") or {}).items()):
        bits = []
        for key, label in (("cpu_bucket", "cpu"), ("cpu_zbucket", "cpu_z"),
                           ("mem_bucket", "mem"), ("disk_bucket", "disk")):
            v = m.get(key)
            if v and v != "none":
                bits.append(f"{label} {v}")
        if bits:
            parts.append(f"{role} " + " ".join(bits))
        elif role in (fp.get("seeds") or []):
            parts.append(f"{role} no_metric_anomaly")
    if fp.get("top5"):
        parts.append("ranked candidates " + " ".join(fp["top5"]))
    fl = fp.get("flow") or {}
    act = [k for k, v in fl.items() if v not in ("none", None)]
    parts.append("application flow " + (" ".join(act) if act else "none"))
    rt = fp.get("routing") or {}
    parts.append(
        f"routing events {rt.get('n_events', 0)} "
        f"real_protocol_signal {'yes' if rt.get('real') else 'no'} "
        + (" ".join(rt.get("subs") or []) if rt.get("subs") else "")
    )
    return " ; ".join(parts)
