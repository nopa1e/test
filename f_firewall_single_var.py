#!/usr/bin/env python3
"""在 submit_nfsvc.jsonl（21.47 分基线）上做 firewall 单变量实验。

冻结时间窗与 root_cause_top5，只重算 fault_category。
"""
import json, glob, collections, sys
from aiops.evidence.f_stages import _infer_category

NF = "submissions/submit_nfsvc.jsonl"
rows = [json.loads(l) for l in open(NF, encoding="utf-8")]
print(f"nfsvc 基线: {len(rows)} 条", flush=True)

BATCH_EV = {
  "20260819040000_20260902040000": dict(metric="outputs_experiment_f_full_fw",
                                        aux="outputs_experiment_f_full",
                                        flow="outputs_experiment_f_full_nf"),
  "20260917040000_20260924040000": dict(metric="outputs_experiment_f_stage2_fw",
                                        aux="outputs_experiment_f_stage2",
                                        flow="outputs_experiment_f_stage2"),
}

_cache = {}
def ev(root, region, name):
    k = (root, region, name)
    if k not in _cache:
        p = glob.glob(f"{root}/{region}_*/{name}")
        if p:
            print(f"    [load] {p[0]}", flush=True)
            _cache[k] = json.load(open(p[0]))
        else:
            print(f"    [MISS] {root}/{region}_*/{name}", flush=True)
            _cache[k] = {}
    return _cache[k]

out, changed, n_fw = [], 0, 0
trans = collections.Counter()
missing = collections.Counter()

# 预热：每个区域一次性载入所需 evidence
regions = sorted({r["prediction_id"].split("_")[2] for r in rows})
print(f"区域: {regions}", flush=True)
for span, cfg in BATCH_EV.items():
    for reg in regions:
        if not glob.glob(f"{cfg['metric']}/{reg}_*/metric_evidence.json"):
            continue
        print(f"  预热 {reg} [{span[:8]}]", flush=True)
        ev(cfg["metric"], reg, "metric_evidence.json")
        ev(cfg["aux"], reg, "routing_evidence.json")
        ev(cfg["aux"], reg, "prediction.json")
        ev(cfg["flow"], reg, "flow_evidence.json")
print("预热完成，开始逐条重算...", flush=True)

for i, r in enumerate(rows):
    if i % 2000 == 0:
        print(f"  进度 {i}/{len(rows)}", flush=True)
    pid = r["prediction_id"]
    iid = pid[2:] if pid.startswith("f_") else pid
    region = pid.split("_")[2]
    span = next((s for s in BATCH_EV if s in pid), None)
    if span is None:
        missing["unknown-span"] += 1; out.append(r); continue
    cfg = BATCH_EV[span]
    if not glob.glob(f"{cfg['metric']}/{region}_*/metric_evidence.json"):
        missing[f"no-metric:{span[:8]}"] += 1; out.append(r); continue

    res = _infer_category(ev(cfg["aux"], region, "routing_evidence.json"),
                          ev(cfg["metric"], region, "metric_evidence.json"),
                          iid, str(r["root_cause_top5"][0]["network_element_id"]).split("-", 1)[1],
                          ev(cfg["aux"], region, "prediction.json").get("fault_category") or {},
                          ev(cfg["flow"], region, "flow_evidence.json"))
    om, nm = r["fault_category"]["major_category"], res["major_category"]
    if om != nm or r["fault_category"]["sub_category"] != res["sub_category"]:
        changed += 1; trans[f"{om} -> {nm}"] += 1
    if nm == "firewall": n_fw += 1
    r2 = dict(r)
    r2["fault_category"] = {"major_category": nm, "sub_category": res["sub_category"]}
    out.append(r2)

print(f"\n未处理: {dict(missing)}")
print("="*76)
print("★ firewall 单变量：在 nfsvc(21.470821) 上冻结时间窗与 top5")
print("="*76)
print(f"  类别改变: {changed} / {len(rows)} = {100*changed/len(rows):.2f}%")
print(f"  firewall : {n_fw} 条")
for k, v in trans.most_common():
    print(f"      {k:<34s} {v}")

bad_t = bad_s = 0
for a, b in zip(rows, out):
    if a["start_time"] != b["start_time"] or a["end_time"] != b["end_time"]: bad_t += 1
    if [(x["rank"], x["network_element_id"]) for x in a["root_cause_top5"]] != \
       [(x["rank"], x["network_element_id"]) for x in b["root_cause_top5"]]: bad_s += 1
print(f"\n  自检（必须 0）: 时间窗={bad_t}  top5={bad_s}")

with open("submissions/submit_fw.jsonl", "w", encoding="utf-8") as fh:
    for r in out:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
print(f"\n  已写出 submissions/submit_fw.jsonl ({len(out)} 行)")
