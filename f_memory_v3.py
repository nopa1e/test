#!/usr/bin/env python3
"""记忆库 v3：修正 v2 暴露的两个缺陷。

v2 的问题
---------
  覆盖率仅 26.2%（9714/13166 条要新建模板），跨区域共用模板仅 1.0%。

诊断：语义侧已经在 naturalize 里把 region 剥成 role 了，理论上是跨区域
可复用的；但**数值特征占 50% 权重且用全局绝对值** —— fw 的 CPU 峰值在
beida 是 49.7、在 guangzhou 是 69.3，同属"极端注入尖峰"，却因为绝对
数值差 20 而被判定不相似。数值侧把跨区域泛化彻底拖垮了。

v3 修正
-------
  1) 数值特征改为 **区域内 z 分数**：先按 region 分组算中位数/IQR，
     再做跨区域的相对比较。这样"该区域内的极端异常"可以跨区域对齐。
  2) 数值权重从 0.5 降到 0.35：语义为主，数值做细分。
  3) 阈值 tau 从 0.90 降到 0.85（v2 实测相似度中位 0.889，卡在阈值上）。
"""
import json, glob, sys, time, collections, os
import numpy as np
sys.path.insert(0, ".")
from aiops.memory import build_fingerprint, MemoryStore, naturalize
from aiops.memory.hybrid import numeric_vector

BATCHES = [
    ("20260819040000_20260902040000", "outputs_experiment_f_full_fw",
     "outputs_experiment_f_full", "outputs_experiment_f_full_nf"),
    ("20260917040000_20260924040000", "outputs_experiment_f_stage2_fw",
     "outputs_experiment_f_stage2", "outputs_experiment_f_stage2"),
]
REGIONS = ["beida","chengdu","guangzhou","nanjing","shanghai","shenyang","wuhan","xian"]
TAU, TOP_K, W_SEM = 0.85, 6, 0.65

def load(p):
    try: return json.load(open(p))
    except Exception: return {}

records = []
for span, mroot, aroot, froot in BATCHES:
    for reg in REGIONS:
        cp = glob.glob(f"{aroot}/{reg}_{span}/incident_candidates.json")
        if not cp: continue
        raw = load(cp[0])
        incs = raw if isinstance(raw, list) else (raw.get("incidents") or raw)
        if isinstance(incs, dict): incs = list(incs.values())
        g = lambda r, n: (glob.glob(f"{r}/{reg}_{span}/{n}") or [None])[0]
        mev = load(g(mroot, "metric_evidence.json")); cev = load(g(aroot, "candidates.json"))
        fev = load(g(froot, "flow_evidence.json"));  rev = load(g(aroot, "routing_evidence.json"))
        base = (load(g(aroot, "prediction.json")).get("fault_category") or {})
        for inc in incs:
            records.append(build_fingerprint(inc, mev, cev, fev, rev, base))

print(f"[V3] {len(records)} 条", flush=True)
records.sort(key=lambda r: (r.get("start") or "", r["incident_id"]))
texts = [naturalize(r) for r in records]
X = np.array([numeric_vector(r) for r in records], dtype=np.float64)
regs = np.array([r["region"] for r in records])

# ---- 关键修正：区域内 robust 归一化 ----
Z = np.zeros_like(X)
for reg in np.unique(regs):
    m = regs == reg
    med = np.median(X[m], axis=0)
    q75, q25 = np.percentile(X[m], 75, axis=0), np.percentile(X[m], 25, axis=0)
    iqr = np.where((q75-q25) < 1e-9, 1.0, q75-q25)
    Zi = (X[m]-med)/iqr
    n = np.linalg.norm(Zi, axis=1, keepdims=True)
    Z[m] = Zi/np.clip(n, 1e-9, None)
print(f"[V3] 区域内归一化完成（{len(np.unique(regs))} 个区域）", flush=True)

store = MemoryStore("/202531630503/lyt/models/all-MiniLM-L6-v2")
t0=time.time(); V = store.encode(texts); print(f"[V3] 编码 {time.time()-t0:.1f}s", flush=True)

n_max=len(records)
BERT_M=np.zeros((n_max,V.shape[1]),dtype=np.float32); NUM_M=np.zeros((n_max,Z.shape[1]),dtype=np.float32)
hits_arr=np.zeros(n_max,dtype=np.int64); n_tpl=0
items=[]; res=[]
for i,(fp,v,z) in enumerate(zip(records,V,Z)):
    if n_tpl:
        sem = BERT_M[:n_tpl] @ v
        num = np.exp(-np.linalg.norm(NUM_M[:n_tpl]-z[None,:],axis=1))
        sim = W_SEM*sem + (1-W_SEM)*num
        h = hits_arr[:n_tpl]; w = 1.0+np.log1p(h.astype(float))
        simw = sim*(0.7+0.3*(w/max(w.max(),1.0)))
        order = np.argsort(-simw)[:TOP_K]; best=int(order[0]); best_sim=float(sim[best])
    else:
        order,best,best_sim=[],None,0.0
    if best is not None and best_sim>=TAU:
        hits_arr[best]+=1
        votes={}
        for j in order:
            if float(sim[j]) < TAU*0.9: continue
            votes[items[j]["label"]] = votes.get(items[j]["label"],0.0)+float(sim[j])
        tot=sum(votes.values()) or 1.0; label=max(votes,key=votes.get)
        out=dict(label=label,sublabel=items[best]["sublabel"],confidence=votes[label]/tot,
                 matched=True,template_id=items[best]["tid"],nearest_sim=best_sim,
                 votes={k:round(x/tot,3) for k,x in votes.items()})
    else:
        tid=f"T{n_tpl:05d}"
        items.append(dict(tid=tid,label=fp.get("label") or "unknown",
                          sublabel=fp.get("sublabel") or "unknown",
                          first_incident=fp["incident_id"],region=fp["region"]))
        BERT_M[n_tpl]=v; NUM_M[n_tpl]=z; n_tpl+=1
        out=dict(label=fp.get("label") or "unknown",sublabel=fp.get("sublabel") or "unknown",
                 confidence=0.0,matched=False,template_id=tid,
                 nearest_sim=best_sim or None,votes={})
    res.append({**fp,**out,"tpl_label":items[int(out["template_id"][1:])]["label"]})
    if (i+1)%4000==0: print(f"  ...{i+1}/{len(records)}",flush=True)

matched=sum(1 for r in res if r["matched"])
print("\n"+"="*76); print("v3 结果"); print("="*76)
print(f"  匹配已有模板 : {matched:6d} ({100*matched/len(res):.1f}%)   [v2: 26.2%]")
print(f"  模板总数     : {len(items)}   [v2: 9714]")
tpl_reg=collections.defaultdict(set); tpl_n=collections.Counter()
for r in res:
    tpl_reg[r["template_id"]].add(r["region"]); tpl_n[r["template_id"]]+=1
cross=sum(1 for t,s in tpl_reg.items() if len(s)>1)
print(f"  跨区域共用模板: {cross}/{len(tpl_reg)} ({100*cross/max(len(tpl_reg),1):.1f}%)   [v2: 1.0%]")
conf=np.array([r["confidence"] for r in res if r["matched"]])
if conf.size:
    print(f"  置信度中位 {np.median(conf):.3f} | 均值 {conf.mean():.3f}   [v2: 1.000 / 0.838]")
both=[r for r in res if r["matched"]]
agree=sum(1 for r in both if r["label"]==r["tpl_label"])
print(f"  与模板标签一致: {agree}/{len(both)} ({100*agree/max(len(both),1):.1f}%)   [v2: 91.5%]")
print(f"\n  跨区域最多的模板:")
for tid,n in tpl_n.most_common(6):
    if len(tpl_reg[tid])>1:
        print(f"    {tid}: {n:4d} 次, 跨 {len(tpl_reg[tid])} 区域 {sorted(tpl_reg[tid])}")

os.makedirs("outputs_memory",exist_ok=True)
with open("outputs_memory/incidents_v3.jsonl","w",encoding="utf-8") as fh:
    for r in res: fh.write(json.dumps(r,ensure_ascii=False,default=str)+"\n")
print(f"\n[MEM] outputs_memory/incidents_v3.jsonl")
