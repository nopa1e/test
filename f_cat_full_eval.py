#!/usr/bin/env python3
"""评估 _infer_category 完整修复后的影响面（不写文件，只统计）。"""
import json, glob, collections, sys, os
os.chdir("/202531630503/lyt/aiops_diagnosis")
sys.path.insert(0, "/202531630503/lyt/aiops_diagnosis")
from aiops.evidence.f_stages import _infer_category

BATCH = {
 "20260819040000_20260902040000": dict(metric="outputs_experiment_f_full_fw",
                                       aux="outputs_experiment_f_full",
                                       flow="outputs_experiment_f_full_nf"),
 "20260917040000_20260924040000": dict(metric="outputs_experiment_f_stage2_fw",
                                       aux="outputs_experiment_f_stage2",
                                       flow="outputs_experiment_f_stage2"),
}
_c = {}
def ev(root, reg, name):
    k = (root, reg, name)
    if k not in _c:
        p = glob.glob(f"{root}/{reg}_*/{name}")
        _c[k] = json.load(open(p[0])) if p else {}
    return _c[k]

rows = [json.loads(l) for l in open("submissions/submit_fw_win.jsonl", encoding="utf-8")]
regions = sorted({r["prediction_id"].split("_")[2] for r in rows})
for span, cfg in BATCH.items():
    for reg in regions:
        if not glob.glob(f"{cfg['metric']}/{reg}_{span}/metric_evidence.json"): continue
        ev(cfg["metric"], reg, "metric_evidence.json")
        ev(cfg["aux"], reg, "routing_evidence.json")
        ev(cfg["aux"], reg, "prediction.json")
        ev(cfg["flow"], reg, "flow_evidence.json")
print(f"预热完成，开始逐条重算 ({len(rows)} 条)...", flush=True)

trans = collections.Counter(); changed = 0
subtrans = collections.Counter()
out = []
for i, r in enumerate(rows):
    pid = r["prediction_id"]; iid = pid[2:]
    reg = pid.split("_")[2]
    span = next((s for s in BATCH if s in pid), None)
    cfg = BATCH[span]
    top1 = str(r["root_cause_top5"][0]["network_element_id"]).split("-", 1)[1]
    res = _infer_category(ev(cfg["aux"], reg, "routing_evidence.json"),
                          ev(cfg["metric"], reg, "metric_evidence.json"),
                          iid, top1,
                          ev(cfg["aux"], reg, "prediction.json").get("fault_category") or {},
                          ev(cfg["flow"], reg, "flow_evidence.json"))
    om = r["fault_category"]["major_category"]; os_ = r["fault_category"]["sub_category"]
    nm = res["major_category"]; ns = res["sub_category"]
    if (om, os_) != (nm, ns):
        changed += 1
        trans[f"{om}/{os_} -> {nm}/{ns}"] += 1
        if om == nm: subtrans[f"{om}: {os_} -> {ns}"] += 1
    out.append({**r, "fault_category": {"major_category": nm, "sub_category": ns}})

n = len(rows)
print(f"\n{'='*78}\n影响面\n{'='*78}")
print(f"  类别改变 {changed} / {n} = {100*changed/n:.2f}%")
print(f"  其中仅子类改变（大类不变）: {sum(subtrans.values())}")
print(f"\n  大类分布对比:")
old_c = collections.Counter(r["fault_category"]["major_category"] for r in rows)
new_c = collections.Counter(r["fault_category"]["major_category"] for r in out)
for k in sorted(set(old_c)|set(new_c)):
    d = new_c.get(k,0)-old_c.get(k,0)
    print(f"    {k:<10s} {old_c.get(k,0):6d} -> {new_c.get(k,0):6d}  ({d:+d})")

print(f"\n  迁移明细 TOP15:")
for k, v in trans.most_common(15):
    print(f"    {k:<58s} {v}")

import sys as _sys
OUT = _sys.argv[1] if len(_sys.argv) > 1 else "submissions/submit_catfull.jsonl"
with open(OUT,"w",encoding="utf-8") as fh:
    for r in out: fh.write(json.dumps(r, ensure_ascii=False)+"\n")
print(f"\n  已写出 {OUT} ({len(out)} 行)")
