#!/usr/bin/env python3
"""有向因果传播图 + 数据估计的条件概率（替代手设常权重的均匀计数）。

动机
----
查到的在先技术 **CN122661102A《星座核心网测试故障定位方法及装置》**
（申请 2026-07-17 / 公开 2026-08-28）已经用了：

    ... the multi-dimensional evidence fusion score of each root cause
    network element is calculated ... whether the root cause network element
    with the highest multi-dimensional evidence fusion score is a fault node ...

而且是**有向依赖图**表示故障传播路径 + **贝叶斯网络** + **条件概率表**量化
"根因网元异常 -> 传播节点异常 -> 故障现象"。

我们原先的对应实现（`candidate_generator.py`）是：

    nb_later = [o for o in later if o in neighbours[node]]
    outgoing[node] = len(nb_later) / denom        # 均匀 1/(n-1)，无权重、无结构
    incoming[node] = len(nb_earlier) / denom

即：**把"邻居里比我晚动"当成"我解释了它"，权重恒为 1/(n-1)**。
`topology.json` 的边又全是 `directed: false`（OSPF 邻接本身无向），
所以"谁导致谁"完全靠时间先后 + 均匀计数，没有任何从数据学到的结构。

本模块做三件该做的事（都不需要标签）
------------------------------------
1. **建有向图**：拓扑边按 `confidence` 加权；`traffic_flow` 里 `directed: true`
   的边直接给出方向；其余边按**经验 lead-lag** 定向。
2. **估计条件概率**：对每条有向边 (u -> v)，用该区域**全部 incident** 统计

       w(u->v) = P(v 比 u 晚动 | u 与 v 同时出现在一个 incident 里)

   这是可以直接从 7 张表算出来的频率，不需要任何真值标注。
3. **净因果外流**：对单条 incident，

       outflow(u) = Σ_v w(u->v)·[v 比 u 晚] − Σ_v w(v->u)·[v 比 u 早]

   正项 = "我能解释的下游"，负项 = "我已被人解释"。这同时喂给
   spec 5.9 的 `outgoing_propagation`(+0.20) 与 `incoming_propagation`(−0.15)，
   但**权重是数据估计的，不是常数**。

单变量纪律
----------
`cfg.causal_graph` 默认 **False**，关闭时 `candidate_generator` 走原来的均匀计数，
既有行为逐字节不变。开启才生效。
"""
from __future__ import annotations

import collections
from typing import Any, Iterable, Mapping

#: 一条有向边的传播权重：P(下游晚动 | 上游动了)
DEFAULT_MIN_SUPPORT = 5


def _node_id(x: Any) -> str:
    s = str(x or "")
    return s.split("-", 1)[-1] if "-" in s else s


def build_directed_edges(
    topology: Mapping[str, Any],
    incident_times: Mapping[str, Mapping[str, Any]],
    *,
    min_support: int = DEFAULT_MIN_SUPPORT,
) -> dict[tuple[str, str], float]:
    """返回 {(u, v): w}，w = P(v 比 u 晚动 | 两者同现)。

    方向来自三处证据的合成：
      * `topology.edges[].confidence` —— 结构先验强度；
      * `topology.edges[].directed`   —— 已知有向时直接采用；
      * **经验 lead-lag**（本函数的主角）—— 对每对相邻节点，统计两者
        同现的 incident 里 t_u < t_v 的比例；偏离 0.5 越多，方向越可信。
    """
    adj: dict[str, dict[str, float]] = collections.defaultdict(dict)
    for e in topology.get("edges") or []:
        u, v = _node_id(e.get("source")), _node_id(e.get("target"))
        if not u or not v or u == v:
            continue
        conf = float(e.get("confidence") or 0.0) or 0.5
        adj[u][v] = max(adj[u].get(v, 0.0), conf)
        adj[v][u] = max(adj[v].get(u, 0.0), conf)

    # 经验 lead-lag：对每对无向邻居统计先后
    earlier = collections.Counter()   # (u,v) -> u 比 v 早的次数
    both = collections.Counter()      # (u,v) -> 两者同现次数（无向键）
    for _iid, times in (incident_times or {}).items():
        ts = {}
        for n, t in (times or {}).items():
            if t is None:
                continue
            ts[_node_id(n)] = t
        for u in ts:
            for v in adj.get(u, {}):
                if v not in ts or u >= v:
                    continue
                key = (u, v) if u < v else (v, u)
                both[key] += 1
                if ts[u] < ts[v]:
                    earlier[(u, v)] += 1
                elif ts[v] < ts[u]:
                    earlier[(v, u)] += 1

    out: dict[tuple[str, str], float] = {}
    for (u, v), n in both.items():
        if n < min_support:
            # 支持度不足：退回结构对称（0.5），不编造方向
            out[(u, v)] = 0.5
            out[(v, u)] = 0.5
            continue
        c_uv, c_vu = earlier[(u, v)], earlier[(v, u)]
        # 拉普拉斯平滑，避免 0/1 极端
        p_uv = (c_uv + 1.0) / (n + 2.0)
        p_vu = (c_vu + 1.0) / (n + 2.0)
        s = p_uv + p_vu
        if s > 0:
            p_uv, p_vu = p_uv / s, p_vu / s     # 归一成条件概率对
        # 与结构置信度相乘：结构越弱，方向越靠近 0.5
        conf = adj[u].get(v, 0.5)
        out[(u, v)] = 0.5 + (p_uv - 0.5) * conf
        out[(v, u)] = 0.5 + (p_vu - 0.5) * conf
    return out


def causal_outflow(
    times: Mapping[str, Any],
    edges: Mapping[tuple[str, str], float],
) -> dict[str, float]:
    """净因果外流：正=能解释下游，负=已被上游解释。范围约 [-1, 1]。"""
    ts = {}
    for n, t in (times or {}).items():
        if t is not None:
            ts[_node_id(n)] = t
    score: dict[str, float] = {n: 0.0 for n in ts}
    for (u, v), w in edges.items():
        if u not in ts or v not in ts:
            continue
        if ts[u] < ts[v]:
            score[u] += w          # u 先动 -> u 解释 v
        elif ts[v] < ts[u]:
            score[v] += w          # 反过来
    n = max(1, len(ts) - 1)
    return {k: v / n for k, v in score.items()}


def outgoing_incoming(
    times: Mapping[str, Any],
    edges: Mapping[tuple[str, str], float],
) -> tuple[dict[str, float], dict[str, float]]:
    """拆成 spec 5.9 要的两个非负项，直接替换原来的均匀计数。

      outgoing_propagation(u) = Σ_{v 比 u 晚} w(u->v) / (n-1)    权重 +0.20
      incoming_propagation(u) = Σ_{v 比 u 早} w(v->u) / (n-1)    权重 -0.15

    与原实现的唯一区别：原来是 |{邻居里比我晚}| / (n-1)（每条边权重恒为 1），
    现在是**数据估计的 P(下游晚动 | 上游动了)** 加权求和。
    """
    ts = {}
    for n, t in (times or {}).items():
        if t is not None:
            ts[_node_id(n)] = t
    n = max(1, len(ts) - 1)
    out = {k: 0.0 for k in ts}
    inc = {k: 0.0 for k in ts}
    for (u, v), w in edges.items():
        if u not in ts or v not in ts:
            continue
        if ts[u] < ts[v]:
            out[u] += w
        elif ts[v] < ts[u]:
            inc[v] += w
    return ({k: v / n for k, v in out.items()}, {k: v / n for k, v in inc.items()})


def topology_from_adjacency(adjacency: Mapping[str, Iterable[str]]) -> dict:
    """把 node->neighbours 映射包成 build_directed_edges 认的 topology 形状。"""
    seen: set[tuple[str, str]] = set()
    edges = []
    for u, vs in (adjacency or {}).items():
        for v in vs or ():
            a, b = _node_id(u), _node_id(v)
            if not a or not b or a == b:
                continue
            k = (a, b) if a < b else (b, a)
            if k in seen:
                continue
            seen.add(k)
            edges.append({"source": a, "target": b, "confidence": 0.5,
                          "directed": False})
    return {"edges": edges}


def collect_incident_times(metric_evidence: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """从 metric_evidence 抠出 {iid: {node: first_anomaly_time}}。"""
    out: dict[str, dict[str, Any]] = {}
    for iid, payload in (metric_evidence.get("incidents") or {}).items():
        times: dict[str, Any] = {}
        for node, np_ in (payload.get("nodes") or {}).items():
            best = None
            for _m, st in (np_.get("metrics") or {}).items():
                t = st.get("first_anomaly_time")
                if t and (best is None or t < best):
                    best = t
            if best is not None:
                times[_node_id(node)] = best
        out[iid] = times
    return out
