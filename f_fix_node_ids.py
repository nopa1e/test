#!/usr/bin/env python3
"""把提交里的畸形网元 ID 修回官方格式 `<region>-<node_id>`。

背景（2026-10-05 实测）
------------------------
当前最佳提交 `submissions/submit_fill2.jsonl`（23.2556）里有 83 条畸形引用：

  * 76 条 `shenyang-shenyang-fw` —— 区域前缀被加了两次（region-fw 归一化补丁
    在 shenyang 这一支被应用了两遍）；
  *  7 条 `traffic-vm` —— 完全没有区域前缀。

官方 README 明确：Top5「模块自身无效时，按照评分规则将对应模块记 0 分，
不会一律拒绝整份答案」。也就是说这 83 条记录的 RCA 模块直接归零——
而它们的第一名候选本来就是正确答案，只是前缀写坏了。这是一个**纯免费**的修复。

做法
----
区域名从预测自己的 `prediction_id`（第 3 段）取，这是权威来源；再兼容从
top5 里已合法的网元反推。对每个网元：反复剥掉重复的区域前缀，直到剩下的是
官方 10 个 node_id 之一，然后重写成 `<region>-<node>`。无法解析的原样保留并计数。

用法
----
    python3 f_fix_node_ids.py --base submissions/x.jsonl --out submissions/x_fixed.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json

REGIONS = ("beida", "shenyang", "xian", "chengdu", "wuhan", "shanghai", "nanjing", "guangzhou")
NODES = ("br-1", "br-2", "cr-1", "cr-2", "fw", "traffic-vm",
         "service-vm-1", "service-vm-2", "service-vm-3", "monitor-vm")


def split_region(pid: str) -> str | None:
    parts = pid.split("_")
    for p in parts:
        if p in REGIONS:
            return p
    return None


def fix_one(nid: str, region: str | None) -> tuple[str, str]:
    """返回 (修好的 id, 动作标签)。"""
    if not isinstance(nid, str) or not nid:
        return nid, "unparsable"
    # 已经在官方格式里 -> 不动
    for r in REGIONS:
        if nid.startswith(r + "-"):
            rest = nid[len(r) + 1:]
            if rest in NODES:
                return nid, "ok"
    # 反复剥掉区域前缀
    rest = nid
    stripped = False
    changed = True
    while changed:
        changed = False
        for r in REGIONS:
            if rest.startswith(r + "-"):
                rest = rest[len(r) + 1:]
                stripped = True
                changed = True
                break
    if stripped and rest in NODES:
        if region is None:
            return nid, "unparsable"
        return f"{region}-{rest}", "stripped"
    # 完全没有区域前缀，本身就是一个合法 node_id
    if rest in NODES:
        if region is None:
            return nid, "unparsable"
        return f"{region}-{rest}", "added"
    return nid, "unparsable"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rows = [json.loads(l) for l in open(a.base, encoding="utf-8") if l.strip()]
    acts: collections.Counter = collections.Counter()
    touched_records = 0
    unparsable: collections.Counter = collections.Counter()

    for r in rows:
        region = split_region(r.get("prediction_id", ""))
        if region is None:
            # 退而从 top5 里合法的网元反推
            for c in r.get("root_cause_top5") or []:
                nid = c.get("network_element_id", "")
                for rr in REGIONS:
                    if nid.startswith(rr + "-") and nid[len(rr) + 1:] in NODES:
                        region = rr
                        break
                if region:
                    break
        rec_touched = False
        for c in r.get("root_cause_top5") or []:
            new, act = fix_one(c.get("network_element_id"), region)
            acts[act] += 1
            if act == "unparsable":
                unparsable[c.get("network_element_id")] += 1
            if new != c.get("network_element_id"):
                c["network_element_id"] = new
                rec_touched = True
        if rec_touched:
            touched_records += 1

    print(f"输入 {len(rows)} 条")
    print(f"  动作统计: {dict(acts)}")
    print(f"  被动过的记录数: {touched_records}")
    if unparsable:
        print(f"  !! 仍无法解析: {dict(unparsable.most_common(10))}")
    with open(a.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  已写出 {a.out}")


if __name__ == "__main__":
    main()
