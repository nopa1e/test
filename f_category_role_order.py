#!/usr/bin/env python3
"""实验 I：类别-角色一致性约束（只重排网元顺序，类别字段一字不改）。

语义依据（赛题 README + 官方样例真值）
--------------------------------------
  resource （cpu/memory/disk/process/softirq 压力）→ 只可能落在业务/流量虚拟机
  routing  （bgp/ospf/route 类协议故障）        → 只可能落在路由器 br/cr
  firewall （acl/port/rule 类）                 → 只可能是 fw
  link     （delay/rate_limit/loss）            → 网元侧，br/cr/vm 均可能

做法与风险隔离
--------------
**只调整 root_cause_top5 的内部顺序，不增删候选、不改 fault_category、不改时间窗。**
因此：top5 集合不变 → 不影响 Dice 匹配；类别不变 → Major/Minor 完全不受影响；
唯一受影响的是 RCA 的排名分（rank1..5 的 1.0/0.8/0.6/0.4/0.2 加权）。

实测影响面（第二批）：类别×top1 角色冲突 358/8649 = 4.1%。
"""
from __future__ import annotations

import argparse
import json
import os
import re

ROLE_RE = re.compile(r"(service-vm-\d+|traffic-vm|monitor-vm|br-\d+|cr-\d+|fw)")
FAMILY = {"service-vm": "vm", "traffic-vm": "vm", "monitor-vm": "vm", "fw": "fw"}
#: 每个大类允许的网元族（按优先级排列，靠前的排更前）
ALLOWED: dict[str, tuple[str, ...]] = {
    "resource": ("vm",),
    "routing": ("br", "cr"),
    "firewall": ("fw",),
    "link": ("br", "cr", "vm"),
}


def family(nid: str) -> str:
    m = ROLE_RE.search(str(nid))
    if not m:
        return "?"
    token = m.group(1)
    if token in FAMILY:
        return FAMILY[token]
    return token.split("-")[0]  # br-1 -> br, cr-2 -> cr


def reorder(top5: list[dict], major: str) -> list[dict]:
    """允许族的候选保持原有相对顺序前置，其余后置；随后重编 rank。"""
    allowed = ALLOWED.get(str(major))
    if not allowed:
        return top5
    ok, bad = [], []
    for e in top5:
        (ok if family(e.get("network_element_id", "")) in allowed else bad).append(e)
    if not bad or not ok:
        return top5
    out = ok + bad
    return [{"rank": i + 1, "network_element_id": e.get("network_element_id")}
            for i, e in enumerate(out)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.input, encoding="utf-8") if l.strip()]
    changed = 0
    detail: dict[str, int] = {}
    out = []
    for r in rows:
        t5 = r.get("root_cause_top5") or []
        major = (r.get("fault_category") or {}).get("major_category")
        new = reorder(t5, major)
        if [e.get("network_element_id") for e in new] != [e.get("network_element_id") for e in t5]:
            changed += 1
            k = f"{major}: rank1 {family(t5[0]['network_element_id'])} -> {family(new[0]['network_element_id'])}"
            detail[k] = detail.get(k, 0) + 1
        r2 = dict(r)
        r2["root_cause_top5"] = new
        out.append(r2)

    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    with open(a.output, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"输入 {len(rows)} 条 -> 输出 {len(out)} 条")
    print(f"顺序被调整: {changed} 条 ({changed/max(1,len(rows))*100:.1f}%)")
    for k, v in sorted(detail.items(), key=lambda kv: -kv[1]):
        print(f"   {k:<40} {v}")


if __name__ == "__main__":
    main()
