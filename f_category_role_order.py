#!/usr/bin/env python3
"""实验 I：类别-角色一致性约束（只重排网元顺序，fault_category 一字不改）。

背景
----
赛题语义约束（README + 官方样例真值）：
  resource （cpu/memory/disk/process/softirq 压力）-> 只可能落在虚拟机
  service  （web_5xx / dns_down / auth_error）      -> 业务服务只跑在 service-vm
  routing  （bgp/ospf/route 协议故障）              -> 只可能落在路由器 br/cr
  firewall （acl/port/rule）                        -> 只可能是 fw
  link     （delay/rate_limit/loss）                -> 网元侧均可

v1 的缺口（本版修复）
--------------------
v1 的 ALLOWED 里**没有 service 大类**，reorder() 遇到 service 直接原样返回。
而 service 占我们预测的 33.6%（2620/7786），其 rank1 有 56.7% 是 br、6.8% 是 cr
——冲突率 63.5%，是全部语义矛盾的主体。v1 实测影响面仅 4.1%，正因它没覆盖它。

两种重排模式
------------
partition（v1 原行为）：允许族全部前置、其余全部后移。
    -> 会把原本排在 rank2 的合规节点提上来，同时把其他节点压到末尾；
       若被压下去的其实是真根因，它会从 0.8 掉到 0.2，净亏 0.6。
       用一个错误换另一个错误，风险大。

swap（本版默认）：rank1 已合规则**完全不动**；不合规才把**第一个**合规候选
    与 rank1 对调，其余位置一律不动。
    -> 每条最多动 2 个位置，代价可控，是"最小干预"。

隔离性（本实验最重要的性质）
--------------------------
只调整 root_cause_top5 的内部顺序：不增删候选、不改 fault_category、不改时间窗。
  - top5 集合不变 -> Dice 匹配不变 -> **AD 分完全不受影响**
  - 类别字段不变   -> Major/Minor 不变
  - 唯一受影响的是 **RCA 排名分**（rank1..5 的 1.0/0.8/0.6/0.4/0.2 加权）
这是一个"纯 RCA 单变量"实验。
"""
from __future__ import annotations

import argparse
import json
import os
import re

ROLE_RE = re.compile(r"(service-vm-\d+|traffic-vm|monitor-vm|br-\d+|cr-\d+|fw)")
VM_FAMILY = ("service-vm", "traffic-vm", "monitor-vm")

ALLOWED_V2: dict[str, tuple[str, ...]] = {
    "resource": VM_FAMILY,
    "service": ("service-vm",),
    "routing": ("br", "cr"),
    "firewall": ("fw",),
    "link": ("br", "cr") + VM_FAMILY,
}

#: v1 规则（无 service 大类），保留用于对照
ALLOWED_V1: dict[str, tuple[str, ...]] = {
    "resource": VM_FAMILY,
    "routing": ("br", "cr"),
    "firewall": ("fw",),
    "link": ("br", "cr") + VM_FAMILY,
}

RULESETS = {"v1": ALLOWED_V1, "v2": ALLOWED_V2}


def role_of(nid: str) -> str:
    m = ROLE_RE.search(str(nid))
    if not m:
        return "?"
    token = m.group(1)
    for r in VM_FAMILY:
        if token.startswith(r):
            return r
    if token.startswith("br-"):
        return "br"
    if token.startswith("cr-"):
        return "cr"
    if token == "fw":
        return "fw"
    return "?"


def reorder(top5: list[dict], major: str, rules: dict, mode: str = "swap") -> list[dict]:
    allowed = rules.get(str(major))
    if not allowed or not top5:
        return top5
    roles = [role_of(e.get("network_element_id", "")) for e in top5]

    if mode == "partition":
        ok, bad = [], []
        for e, r in zip(top5, roles):
            (ok if r in allowed else bad).append(e)
        if not bad or not ok:
            return top5
        out = ok + bad
    else:  # swap：最小干预
        if roles[0] in allowed:
            return top5
        idx = next((i for i, r in enumerate(roles) if r in allowed), None)
        if idx is None:
            return top5
        out = list(top5)
        out[0], out[idx] = out[idx], out[0]

    return [{"rank": i + 1, "network_element_id": e.get("network_element_id")}
            for i, e in enumerate(out)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--rules", default="v2", choices=sorted(RULESETS))
    ap.add_argument("--mode", default="swap", choices=["swap", "partition"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rules = RULESETS[a.rules]
    rows = [json.loads(l) for l in open(a.input, encoding="utf-8") if l.strip()]
    changed = 0            # 任何位置变动
    rank1_changed = 0      # rank1 节点 ID 变了
    detail: dict[str, int] = {}
    out = []
    for r in rows:
        t5 = r.get("root_cause_top5") or []
        if not t5:
            out.append(r)
            continue
        major = (r.get("fault_category") or {}).get("major_category")
        new = reorder(t5, major, rules, a.mode)
        old_ids = [e.get("network_element_id") for e in t5]
        new_ids = [e.get("network_element_id") for e in new]
        if new_ids != old_ids:
            changed += 1
            if old_ids[0] != new_ids[0]:
                rank1_changed += 1
                k = f"{major}: rank1 {role_of(old_ids[0])} -> {role_of(new_ids[0])}"
                detail[k] = detail.get(k, 0) + 1
        r2 = dict(r)
        r2["root_cause_top5"] = new
        out.append(r2)

    n = max(1, len(rows))
    print(f"[rules={a.rules} mode={a.mode}] 输入 {len(rows)} 条")
    print(f"  任意位置变动 : {changed:5d}  ({changed/n*100:.1f}%)")
    print(f"  rank1 改变   : {rank1_changed:5d}  ({rank1_changed/n*100:.1f}%)   <-- 实质差异")
    for k, v in sorted(detail.items(), key=lambda kv: -kv[1]):
        print(f"     {k:<46} {v}")

    if a.dry_run:
        print("  (dry-run，未写文件)")
        return
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    with open(a.output, "w", encoding="utf-8") as fh:
        for r in out:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  已写出 -> {a.output}")


if __name__ == "__main__":
    main()
