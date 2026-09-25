from __future__ import annotations

from collections import Counter
from typing import Any


FAULT_MAP: dict[tuple[str, str], str] = {
    ("link", "delay"): "link_delay",
    ("link", "rate_limit"): "link_rate_limit",
    ("link", "loss"): "link_loss",
    ("firewall", "acl_drop"): "firewall_acl_drop",
    ("firewall", "rate_limit"): "firewall_rate_limit",
    ("firewall", "port_block"): "firewall_port_block",
    ("firewall", "cpu_pressure"): "firewall_cpu_pressure",
    ("firewall", "default_route_error"): "firewall_default_route_error",
    ("firewall", "rule_order_error"): "firewall_rule_order_error",
    ("resource", "cpu_pressure"): "resource_cpu_high",
    ("resource", "memory_pressure"): "resource_memory_pressure",
    ("resource", "disk_io_pressure"): "resource_disk_io_pressure",
    ("resource", "disk_space_low"): "resource_disk_space_low",
    ("resource", "process_pressure"): "resource_process_pressure",
    ("resource", "softirq_pressure"): "resource_softirq_udp_pressure",
    ("routing", "blackhole"): "route_blackhole",
    ("routing", "bgp_session_down"): "route_bgp_session_down",
    ("routing", "bgp_route_flap"): "route_bgp_route_flap",
    ("routing", "wrong_static_route"): "route_wrong_static_route",
    ("routing", "ospf6_neighbor_down"): "route_ospf6_neighbor_down",
    ("routing", "ospf6_cost_anomaly"): "route_ospf6_cost_anomaly",
    ("routing", "wrong_default_route"): "route_wrong_default_route",
    ("service", "dns_down"): "service_dns_down",
    ("service", "dns_wrong_record"): "service_dns_wrong_record",
    ("service", "web_5xx"): "service_web_5xx",
    ("service", "web_slow"): "service_web_slow",
    ("service", "auth_timeout"): "service_auth_timeout",
    ("service", "auth_error"): "service_auth_error",
}

FAULT_NAME_TO_PAIR: dict[str, tuple[str, str]] = {v: k for k, v in FAULT_MAP.items()}

FAULT_DESCRIPTIONS: dict[tuple[str, str], str] = {
    ("link", "delay"): "网络链路传输时延增加，导致数据包传输延迟",
    ("link", "rate_limit"): "链路带宽受限，降低网络传输速率",
    ("link", "loss"): "网络链路丢包异常，导致数据传输可靠性下降",
    ("firewall", "acl_drop"): "防火墙访问控制规则错误，丢弃指定流量",
    ("firewall", "rate_limit"): "防火墙流量限制，降低特定业务流量速率",
    ("firewall", "port_block"): "防火墙端口误封，阻断指定端口通信",
    ("firewall", "cpu_pressure"): "防火墙资源压力，导致转发性能下降",
    ("firewall", "default_route_error"): "防火墙默认路由配置错误，引发流量转发异常",
    ("firewall", "rule_order_error"): "防火墙规则匹配顺序错误，导致异常流量处理",
    ("resource", "cpu_pressure"): "节点 CPU 资源占用过高，导致系统负载和业务延迟上升",
    ("resource", "memory_pressure"): "节点内存资源不足，造成可用内存下降和业务响应变慢",
    ("resource", "disk_io_pressure"): "磁盘 I/O 压力过高，导致读写性能下降",
    ("resource", "disk_space_low"): "磁盘空间不足，引发存储异常",
    ("resource", "process_pressure"): "进程资源压力，导致服务处理能力下降",
    ("resource", "softirq_pressure"): "UDP 流量引发软中断压力，影响网络处理性能",
    ("routing", "blackhole"): "路由黑洞，导致目标流量无法正常转发",
    ("routing", "bgp_session_down"): "模拟 BGP 会话中断，导致路由信息不可达",
    ("routing", "bgp_route_flap"): "BGP 路由频繁变化，导致网络不稳定",
    ("routing", "wrong_static_route"): "静态路由配置错误，引发流量转发异常",
    ("routing", "ospf6_neighbor_down"): "OSPFv3 邻居失效，导致路由收敛异常",
    ("routing", "ospf6_cost_anomaly"): "OSPFv3 路径开销异常，导致选路变化",
    ("routing", "wrong_default_route"): "默认路由配置错误，导致流量转发异常",
    ("service", "dns_down"): "DNS 服务不可用，导致域名解析请求失败",
    ("service", "dns_wrong_record"): "DNS 解析记录错误，导致访问目标服务异常",
    ("service", "web_5xx"): "Web 服务返回 5xx 错误，导致业务请求失败",
    ("service", "web_slow"): "Web 服务响应缓慢，导致业务访问延迟增加",
    ("service", "auth_timeout"): "认证服务请求超时，导致用户认证失败",
    ("service", "auth_error"): "认证服务异常错误，导致认证请求失败",
}
VALID_MAJOR = sorted({k[0] for k in FAULT_MAP})
VALID_SUB_BY_MAJOR: dict[str, list[str]] = {}
for major, sub in FAULT_MAP:
    VALID_SUB_BY_MAJOR.setdefault(major, []).append(sub)
for key in VALID_SUB_BY_MAJOR:
    VALID_SUB_BY_MAJOR[key] = sorted(VALID_SUB_BY_MAJOR[key])


def is_valid_category(major: str, sub: str) -> bool:
    return (str(major), str(sub)) in FAULT_MAP


def fault_name(major: str, sub: str) -> str:
    return FAULT_MAP.get((major, sub), f"{major}_{sub}")


def category_json_schema_text() -> str:
    lines: list[str] = []
    for major, sub in sorted(FAULT_MAP):
        name = FAULT_MAP[(major, sub)]
        desc = FAULT_DESCRIPTIONS.get((major, sub), "")
        lines.append(f"{name}: major_category={major}, sub_category={sub} - {desc}")
    return "\n".join(lines)


def infer_category_from_evidence(
    evidence: dict[str, Any],
    node_scores: list[dict[str, Any]] | None = None,
    node_types: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Deterministic fallback classifier.

    It uses mechanism-level feature names and node kinds instead of final
    business symptoms.  Kind is read from the dataset's ``node_type`` field
    (directly, or via an ``node_types`` id -> type map supplied by the caller),
    never inferred from how an element is named.  Directional checks (e.g.
    BGP peer_up < 1) avoid the common mistake of treating every BGP metric as a
    session-down event.  It always returns a legal pair.
    """
    feature_rows: list[dict[str, Any]] = []
    for p in evidence.get("top_points", []):
        for f in p.get("top_features", []) or []:
            if isinstance(f, dict):
                row = dict(f)
                row["name"] = str(row.get("name", ""))
            else:
                row = {"name": str(f), "value": 0.0}
            feature_rows.append(row)

    def has(*needles: str) -> bool:
        joined = " ".join(str(r.get("name", "")) for r in feature_rows).lower()
        return any(n.lower() in joined for n in needles)

    def values(*needles: str) -> list[float]:
        out: list[float] = []
        for r in feature_rows:
            name = str(r.get("name", "")).lower()
            if any(n.lower() in name for n in needles):
                try:
                    out.append(float(r.get("value", 0.0)))
                except Exception:
                    pass
        return out

    # Node kind comes from the dataset's own node_type field, never from the
    # shape of the element id.  Callers may pass an explicit id -> type map;
    # otherwise the per-candidate node_type attached by the ranker is used.
    def _type_of(entry: dict[str, Any]) -> str:
        if node_types:
            neid = str(entry.get("network_element_id", ""))
            if neid in node_types:
                return str(node_types[neid]).strip().lower()
            short = neid.split("-", 1)[-1]
            if short in node_types:
                return str(node_types[short]).strip().lower()
        return str(entry.get("node_type", "")).strip().lower()

    top_types = [_type_of(n) for n in (node_scores or [])]
    fw_top = "firewall" in top_types[:3]
    service_top = "service" in top_types[:3]
    router_top = any(t in {"br", "cr"} for t in top_types[:3])

    # --- routing: explicit protocol state/metric direction ---
    bgp_up = values("bgp_peer_up")
    if (bgp_up and min(bgp_up) < 1.0) or has("bgp_session_down", "bgp_peer_down"):
        return "routing", "bgp_session_down"
    uptime = values("bgp_peer_uptime")
    if uptime and min(uptime) < 300:
        return "routing", "bgp_session_down"
    route_changes = values("ipv6_route_change_total")
    if (route_changes and max(route_changes) > 0) or has("route_flap"):
        return "routing", "bgp_route_flap"
    ospf_state = values("ospf6_neighbor_state_code")
    if ospf_state and min(ospf_state) < 6:
        return "routing", "ospf6_neighbor_down"
    if has("ospf6_neighbor_state"):
        return "routing", "ospf6_neighbor_down"
    if has("ospf6_interface_cost"):
        return "routing", "ospf6_cost_anomaly"
    default_changed = values("ipv6_default_route_changed")
    default_info = values("ipv6_default_route_info")
    if (default_changed and max(default_changed) > 0) or (default_info and min(default_info) < 1.0):
        if fw_top:
            return "firewall", "default_route_error"
        return "routing", "wrong_default_route"
    route_exists = values("ipv6_route_exists")
    if route_exists and min(route_exists) < 1.0:
        return "routing", "blackhole"
    if has("ipv6_route_nexthop_info"):
        return "routing", "wrong_static_route"

    # --- firewall mechanisms ---
    if fw_top:
        if has("acl", "deny", "rx_drop_rate", "tx_drop_rate"):
            return "firewall", "acl_drop"
        if has("port_block"):
            return "firewall", "port_block"
        if has("rate_limit", "bytes_rate", "packets_rate"):
            return "firewall", "rate_limit"
        if has("rule_order", "rule_order_error"):
            return "firewall", "rule_order_error"
        if has("cpu_usage", "load1", "load5"):
            return "firewall", "cpu_pressure"

    # --- resource mechanisms ---
    if has("memory_available_ratio", "swap_used_ratio"):
        return "resource", "memory_pressure"
    if has("softirq"):
        return "resource", "softirq_pressure"
    if has("process_count", "open_fd_ratio"):
        return "resource", "process_pressure"
    if has("disk_io_util", "disk_read_rate", "disk_write_rate"):
        return "resource", "disk_io_pressure"
    if has("filesystem_used_ratio", "inode_used_ratio"):
        return "resource", "disk_space_low"
    if has("cpu_usage", "load1", "load5"):
        return "resource", "cpu_pressure"

    # --- link mechanisms: use directional values where possible ---
    drops = values("rx_drop_rate", "tx_drop_rate", "rx_error_rate", "tx_error_rate", "carrier_changes")
    if (drops and max(drops) > 0) or has("carrier_changes"):
        return "link", "loss"
    latencies = values("latency", "jitter")
    if latencies or has("web_flow_latency"):
        return "link", "delay"
    if has("rx_bytes_rate", "tx_bytes_rate", "rx_packets_rate", "tx_packets_rate", "observed_qps"):
        return "link", "rate_limit"

    # --- service mechanisms ---
    if has("web_flow_error", "web_5xx", "5xx"):
        return "service", "web_5xx"
    if has("web_flow_latency", "web_slow"):
        return "service", "web_slow"
    if has("dns_flow_error", "dns_flow_timeout", "dns_flow_failed"):
        return "service", "dns_down"
    if has("dns_flow"):
        return "service", "dns_wrong_record"
    if has("auth_flow_timeout", "auth_timeout"):
        return "service", "auth_timeout"
    if has("auth_flow_error", "auth_flow_failed"):
        return "service", "auth_error"

    # --- fallbacks by node kind ---
    if fw_top:
        return "firewall", "cpu_pressure"
    if service_top:
        return "service", "web_slow"
    if router_top:
        return "link", "loss"
    return "link", "delay"
