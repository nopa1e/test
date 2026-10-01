"""从 netflow 重建 service 类别的证据（第二批没有 traffic_flow_metrics 时的替代）。

背景
----
第二批起主办方不再提供 `traffic_flow_metrics`（各类流的详细指标），
但 **netflow_5tuple 仍在提供**。第一批的 service 判定依赖 flow 表，
这导致第二批一条 service 预测都产生不出来 —— 而这是**数据源缺失**，
不是**故障类型缺失**。

本模块把 netflow 聚合成与 flow 表等价的"服务维度时序"：

    端口 1053      -> dns  服务     异常/归零 → dns_down / dns_wrong_record
    端口 80, 8080  -> web  服务     流量骤降 → web_5xx;  会话跨度拉长 → web_slow
    dst_addr 前缀  -> 目标区域（fd00:1..8），用于识别跨区域访问

输出格式与 flow 证据对齐，供 `_infer_category` 统一消费，
使两批数据走同一套类别判定逻辑。
"""
from __future__ import annotations

import os
from collections import defaultdict

import pandas as pd

#: 端口 -> service 子类族
PORT_SERVICE = {
    1053: "dns",
    80: "web",
    8080: "web",
}
#: 判定阈值：相对基线的跌幅
DROP_RATIO = 0.5
#: 会话跨度相对基线的放大倍数（判断"慢"）
SLOW_RATIO = 2.0


def _series(df: pd.DataFrame, key: str) -> dict[str, float]:
    g = df.groupby("minute_utc")[key].sum()
    return {str(k): float(v) for k, v in g.items()}


def build_service_evidence(
    netflow_path: str,
    incident_window: tuple[str, str],
    baseline_window: tuple[str, str],
    max_rows: int | None = None,
) -> dict:
    """聚合 netflow 并产出与 flow 证据对齐的 service 维度时序。

    Returns:
        {"ports": {port: {"service": str, "incident": {...}, "baseline": {...},
                          "drop_ratio": float, "slow_ratio": float}}}
    """
    if not os.path.exists(netflow_path):
        return {"ports": {}, "warn": "netflow 文件不存在"}
    cols = ["minute_utc", "dst_port", "packets", "bytes", "first_seen", "last_seen"]
    df = pd.read_csv(netflow_path, usecols=cols, nrows=max_rows)
    df = df[df["dst_port"].isin(PORT_SERVICE.keys())]
    if df.empty:
        return {"ports": {}, "warn": "无服务端口流量"}

    fs = pd.to_datetime(df["first_seen"], errors="coerce")
    ls = pd.to_datetime(df["last_seen"], errors="coerce")
    df = df.assign(span=(ls - fs).dt.total_seconds())

    def clip(w):
        return df[(df["minute_utc"] >= w[0]) & (df["minute_utc"] <= w[1])]

    inc, base = clip(incident_window), clip(baseline_window)
    out: dict = {"ports": {}}
    for port, svc in PORT_SERVICE.items():
        i, b = inc[inc["dst_port"] == port], base[base["dst_port"] == port]
        if i.empty:
            continue
        i_pk = float(i["packets"].sum())
        b_pk = float(b["packets"].sum())
        denom = max(b_pk, 1.0)
        i_span = float(i["span"].mean()) if i["span"].notna().any() else 0.0
        b_span = float(b["span"].mean()) if b["span"].notna().any() else 0.0
        out["ports"][int(port)] = {
            "service": svc,
            "incident_packets": i_pk,
            "baseline_packets": b_pk,
            "drop_ratio": (i_pk / denom) if b_pk > 0 else None,
            "incident_span_s": i_span,
            "baseline_span_s": b_span,
            "slow_ratio": (i_span / b_span) if b_span > 0 else None,
        }
    return out


def service_subcategory(port: int, ev: dict) -> str | None:
    """按端口与异常形态给出 service 子类。"""
    svc = PORT_SERVICE.get(int(port))
    if not svc:
        return None
    drop = ev.get("drop_ratio")
    slow = ev.get("slow_ratio")
    if svc == "dns":
        if drop is not None and drop < DROP_RATIO:
            return "dns_down"
        return None
    if svc == "web":
        if slow is not None and slow >= SLOW_RATIO:
            return "web_slow"
        if drop is not None and drop < DROP_RATIO:
            return "web_5xx"
        return None
    return None
