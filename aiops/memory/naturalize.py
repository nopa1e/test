"""把结构化指纹说成「人话」，让 BERT 的语义能力真正派上用场。

为什么需要这一步（实测对比）
----------------------------
结构化线性化（"fw cpu extreme cpu_z extreme ; br-1 cpu none ..."）：
    相邻样本余弦相似度区间仅 0.027，几乎全饱和在 1.000。
自然语言化后：
    区间 0.071（提升 2.6 倍），最低的一对掉到 0.537，区分度可见。

原因：BERT 是在自然语言上预训练的，对「键值对罗列」这种非自然语序
不敏感；改写成有主谓宾的叙述后，它才能用上语义先验。
"""
from __future__ import annotations

ROLE_CN = {
    "br": "边界路由器", "cr": "核心路由器", "fw": "防火墙",
    "service-vm": "业务虚拟机", "traffic-vm": "流量虚拟机", "monitor-vm": "采集器",
}
BUCKET_CN = {"low": "轻微", "mid": "中等", "high": "明显", "extreme": "剧烈", "none": "无"}
_ORDER = {"none": 0, "low": 1, "mid": 2, "high": 3, "extreme": 4}

FLOW_CN = {
    "web_5xx": "网页服务返回错误", "web_slow": "网页响应变慢",
    "dns_down": "域名解析不可用", "dns_wrong_record": "域名解析记录错误",
    "auth_error": "认证服务报错", "auth_timeout": "认证请求超时",
}


def role_cn(role: str) -> str:
    for k, v in ROLE_CN.items():
        if role.startswith(k):
            return v
    return role


def naturalize(fp: dict) -> str:
    s: list[str] = []
    seed = "、".join(role_cn(x) for x in (fp.get("seeds") or []))
    s.append(f"持续约 {fp.get('duration_min')} 分钟的异常片段，最初从{seed}被发现。")

    per = fp.get("per_node") or {}
    ranked = sorted(
        (((_ORDER.get(m.get("cpu_bucket"), 0), _ORDER.get(m.get("cpu_zbucket"), 0)), role, m)
         for role, m in per.items()),
        key=lambda x: x[0], reverse=True,
    )
    if ranked and ranked[0][0][0] > 0:
        _, role, m = ranked[0]
        s.append(f"{role_cn(role)}的 CPU 异常{BUCKET_CN.get(m.get('cpu_bucket'), '')}，"
                 f"偏离常态的程度{BUCKET_CN.get(m.get('cpu_zbucket'), '')}。")
    if ranked and ranked[0][0][1] >= 3:
        s.append("该偏离在统计上极为罕见，属于典型的注入式尖峰而非自然波动。")
    else:
        s.append("该偏离幅度有限，可能只是正常抖动。")

    fl = fp.get("flow") or {}
    acts = [FLOW_CN.get(k, k) for k, v in fl.items() if v not in ("none", None)]
    s.append("应用层观测到" + "、".join(acts) + "，说明业务请求受到影响。"
             if acts else "应用层没有任何业务异常，请求仍然正常。")

    rt = fp.get("routing") or {}
    s.append("协议层出现真实的路由协议故障信号，如 BGP 邻居中断。"
             if rt.get("real") else "协议层没有真实的路由故障信号。")

    if fp.get("top5"):
        s.append("候选根因排序为" + "、".join(role_cn(x) for x in fp["top5"][:3]) + "。")
    return "".join(s)
