"""跨区域一致性证据（CRCS, Cross-Region Consistency Score）。

动机（实测支撑，见文档 §17）
--------------------------
8 个区域拓扑同构（br-1/br-2/cr-1/cr-2/fw/traffic-vm/service-vm-1..3），实测：
    跨区域·同角色 画像距离中位 0.423
    跨区域·异角色 画像距离中位 1.954        （比值 0.216）
    区域内·任意两节点 画像距离中位 1.985
即「xian-br-1 与 beida-br-1 的相似度」比「xian-br-1 与 xian-service-vm-1」
还高 4.7 倍 —— 同角色节点在不同区域高度可互换，可互为空间 baseline。

进一步按异常强度分层实测跨区域并发度：
    q0.00 : 并发 3.54 区域, 单区域独占  9.9%
    q0.90 : 并发 1.64 区域, 单区域独占 53.6%
    q0.99 : 并发 1.17 区域, 单区域独占 84.0%
按角色：br-1 独占 41.1%（共模最重）、service-vm-1 独占 88.0%（最干净）。
=> 时间维 baseline 对 service-vm 已足够，**对 br/cr 这类共模重的角色才是缺口**。

定位
----
**不改变 RootScore 的任何权重**（spec 规定七项权重为常数、禁止调权），
本模块只产出**分项内部**可用的空间维偏离量，供 local_anomaly 这类
已有分项在自身计算中融合。默认不启用（flag 关闭），保持既有行为不变。
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

#: 同一时刻参与比较的最少区域数；不足则该点不给 CRCS（避免用 1~2 个样本定标）
MIN_PEERS = 4
#: CRCS 的分母下限，防止 MAD=0 时爆掉
MAD_FLOOR = 1e-9
#: CRCS 截断上限。MAD 趋零时 |v-med|/MAD 会爆到 1e9 量级（实测
#: disk_io_util 出现过 9.6e9），截断既保序也避免污染下游归一化。
CRCS_CAP = 50.0


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


def _mad(xs: list[float], med: float) -> float:
    return _median([abs(x - med) for x in xs])


#: 角色名模式。必须显式枚举，不能用 split("-", 1)[1] —— 那样会把短名
#: ``br-1`` 切成 ``1``、``cr-1`` 也切成 ``1``，导致 br-1 与 cr-1 被误当成
#: 同一角色（实测踩过：两者合并后 n 恰好是 2 倍）。
_ROLE_RE = re.compile(
    r"(service-vm-\d+|traffic-vm|monitor-vm|br-\d+|cr-\d+|fw)\s*$"
)


def role_key(network_element_id: str) -> str:
    """归一成角色键：``xian-br-1`` 与 ``br-1`` 都得到 ``br-1``。

    角色是「跨区域可互换」的粒度，因此 service-vm-1 / traffic-vm / fw
    必须区分开，绝不能被归并。
    """
    s = str(network_element_id).strip()
    m = _ROLE_RE.search(s)
    return m.group(1) if m else s


def build_crcs(
    series: dict[tuple[str, str], dict[str, float]],
) -> dict[tuple[str, str], dict[str, float]]:
    """计算每个 (region, role) 在每个时刻的跨区域一致性偏离。

    Args:
        series: ``{(region, role): {timestamp: value}}``。调用方需保证各区域的
            时间轴对齐（同一批数据内 8 个区域时间范围完全一致，可严格对齐）。

    Returns:
        ``{(region, role): {timestamp: crcs}}``，crcs 为稳健 z 分数
        ``|v - median(peers)| / MAD(peers)``；同刻 peer 少于 ``MIN_PEERS``
        的时刻不下发（避免用极少样本定标）。
    """
    # 先按 (role, timestamp) 收集所有区域的值，构成 peer 池
    pools: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (region, role), pts in series.items():
        for ts, v in pts.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            pools[(role, ts)].append(float(v))

    out: dict[tuple[str, str], dict[str, float]] = {}
    for (region, role), pts in series.items():
        acc: dict[str, float] = {}
        for ts, v in pts.items():
            if v is None or (isinstance(v, float) and math.isnan(v)):
                continue
            peers = pools.get((role, ts)) or []
            if len(peers) < MIN_PEERS:
                continue
            med = _median(peers)
            mad = _mad(peers, med)
            denom = mad if mad > MAD_FLOOR else MAD_FLOOR
            z = abs(float(v) - med) / denom
            # 稳健化：MAD 极小时比值会爆到 1e9 量级，截断到 CRCS_CAP
            acc[ts] = z if z < CRCS_CAP else CRCS_CAP
        out[(region, role)] = acc
    return out


def summarize(crcs: dict[tuple[str, str], dict[str, float]]) -> dict[str, object]:
    """给报告用的概要。"""
    per_role: dict[str, list[float]] = defaultdict(list)
    for (_, role), pts in crcs.items():
        per_role[role].extend(pts.values())
    rows = []
    for role, vals in sorted(per_role.items()):
        if not vals:
            continue
        s = sorted(vals)
        rows.append({
            "role": role,
            "n": len(vals),
            "median": s[len(s) // 2],
            "p90": s[int(len(s) * 0.9)],
            "max": s[-1],
        })
    return {"roles": rows}
