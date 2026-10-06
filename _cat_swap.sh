#!/usr/bin/env bash
# 受控实验：只把 fault_category 换成 LLM 的判定，其它一个字节都不动。
#
# 为什么这是一个干净的测量
#   评分里 AD 的匹配只依赖时间窗（Dice），RCA 依赖匹配上的那条记录的 top5；
#   而 fault_category 既不影响匹配、也不影响 top5。
#   所以「只换类别」的提交，分数变化 = 纯粹的 Major + Minor 变化。
#
# 对照：
#   submit_fill2_rean.jsonl                     108613 行  -> 23.3344
#   submit_fill2_rean_catllm.jsonl              108613 行  -> 本次测
set -euo pipefail
cd /202531630503/lyt/aiops_diagnosis
python3 - <<'PY'
import json

llm = json.load(open("llm_cat_clean.json", encoding="utf-8"))
rows = [json.loads(l) for l in open("submissions/submit_fill2_rean.jsonl", encoding="utf-8") if l.strip()]
print(f"基线 {len(rows)} 行；LLM 类别表 {len(llm)} 条")

hit = miss = null = 0
dist = {}
for r in rows:
    pid = r["prediction_id"]
    key = pid[2:] if pid.startswith("f_") else pid
    v = llm.get(key)
    if v is None:
        null += 1
        continue
    if key not in llm:
        miss += 1
    else:
        hit += 1
    r["fault_category"] = {"major_category": v["major_category"],
                           "sub_category": v["sub_category"] or "cpu_pressure"}
    dist[v["major_category"]] = dist.get(v["major_category"], 0) + 1

print(f"  替换成功 {hit}；LLM 表里没有 {miss}；LLM 无有效值 {null}")
print(f"  替换后大类分布 {dist}")
with open("submissions/submit_fill2_rean_catllm.jsonl", "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
print("已写出 submissions/submit_fill2_rean_catllm.jsonl")
PY
wc -l submissions/submit_fill2_rean_catllm.jsonl
