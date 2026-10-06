#!/usr/bin/env python3
"""把 LLM 的类别判定**按时序最近**传播到全部行，做满强度的类别受控实验。

问题：LLM 只覆盖 incident（f_INC_*），而提交里 93% 是 f_FILL / f_REAN 变体行。
如果只替换 f_INC 行，那么这个"只换类别"的实验只有 7% 的强度，测不出东西。

做法：`f_fill_timeline.py` 本身就是"克隆同区域时间最近的那条已有预测的
root_cause_top5 与 fault_category"，所以这里用**同样的规则**把 LLM 类别
传播到每一行——按时间窗中心找同区域最近的那条 f_INC 记录。

这样每一行都拿到 LLM 的类别，实验强度 100%，而且不改变任何行数 / 窗口 / 顺序。
"""
from __future__ import annotations

import bisect
import json
import os
from datetime import datetime

SRC = "submissions/submit_fill2_rean.jsonl"
OUT = "submissions/submit_fill2_rean_catllm_full.jsonl"


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def center(r: dict) -> datetime:
    return ts(r["start_time"]) + (ts(r["end_time"]) - ts(r["start_time"])) / 2


def region_of(r: dict) -> str:
    return r["root_cause_top5"][0]["network_element_id"].split("-", 1)[0]


rows = [json.loads(l) for l in open(SRC, encoding="utf-8") if l.strip()]
llm = json.load(open("llm_cat_clean.json", encoding="utf-8"))
print(f"基线 {len(rows)} 行；LLM 类别表 {len(llm)} 条")

# 每个区域的锚点：f_INC 行 + 它的 LLM 类别
anchors: dict[str, list[tuple[datetime, dict]]] = {}
no_llm = 0
for r in rows:
    pid = r["prediction_id"]
    if not pid.startswith("f_INC_"):
        continue
    key = pid[2:]
    v = llm.get(key)
    if not v:
        no_llm += 1
        continue
    anchors.setdefault(region_of(r), []).append((center(r), v))
for k in anchors:
    anchors[k].sort(key=lambda t: t[0])
print(f"锚点区域数 {len(anchors)}；无 LLM 值的 f_INC {no_llm}")
print("  每区锚点数:", {k: len(v) for k, v in sorted(anchors.items())})

# 每行按时序最近取类别
changed = same = 0
missing = 0
dist: dict[str, int] = {}
out = []
for r in rows:
    reg = region_of(r)
    lst = anchors.get(reg)
    if not lst:
        missing += 1
        out.append(r)
        continue
    c = center(r)
    times = [t for t, _ in lst]
    i = bisect.bisect_left(times, c)
    cand = [j for j in (i - 1, i) if 0 <= j < len(times)]
    j = min(cand, key=lambda k: abs((times[k] - c).total_seconds()))
    v = llm_choice = lst[j][1]
    old = (r.get("fault_category") or {}).get("major_category")
    new = {"major_category": v["major_category"],
           "sub_category": v["sub_category"] or "cpu_pressure"}
    if old != new["major_category"]:
        changed += 1
    else:
        same += 1
    dist[new["major_category"]] = dist.get(new["major_category"], 0) + 1
    r["fault_category"] = new
    out.append(r)

print(f"  改了 {changed} 行，未改 {same} 行，无锚点 {missing} 行")
print(f"  替换后大类分布 {dist}")
with open(OUT, "w", encoding="utf-8") as fh:
    for r in out:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"已写出 {OUT}  ({len(out)} 行)")
