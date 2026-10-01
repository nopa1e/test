#!/usr/bin/env python3
"""自监督故障记忆库 v2：跨区域建库（BERT 语义 + 连续数值 混合距离）。

自监督强化规则（不依赖真值标签，与 VAE 靠重构误差学正常分布同源）：
    证据指纹 --检索 top-k--> 命中已有模板(max_sim >= tau)
                              -> 模板 hits+1，权重 w = 1 + log1p(hits) 上升
                              -> 样本继承该模板类别，置信度 = 加权投票占比
                          未命中 -> 新建模板，类别沿用规则判定（低权重起步）
"""
import json, glob, sys, time, collections, os
import numpy as np
sys.path.insert(0, ".")
from aiops.memory import build_fingerprint, MemoryStore, naturalize
from aiops.memory.hybrid import numeric_vector, NumericScaler

BATCHES = [
    ("20260819040000_20260902040000", "outputs_experiment_f_full_fw",
     "outputs_experiment_f_full", "outputs_experiment_f_full_nf"),
    ("20260917040000_20260924040000", "outputs_experiment_f_stage2_fw",
     "outputs_experiment_f_stage2", "outputs_experiment_f_stage2"),
]
REGIONS = ["beida","chengdu","guangzhou","nanjing","shanghai","shenyang","wuhan","xian"]
TAU = 0.90
TOP_K = 6
W_SEM = 0.5

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
        mev = load(g(mroot, "metric_evidence.json"))
        cev = load(g(aroot, "candidates.json"))
        fev = load(g(froot, "flow_evidence.json"))
        rev = load(g(aroot, "routing_evidence.json"))
        base = (load(g(aroot, "prediction.json")).get("fault_category") or {})
        for inc in incs:
            fp = build_fingerprint(inc, mev, cev, fev, rev, base)
            records.append(fp)

print(f"[MEM] {len(records)} 条指纹", flush=True)
records.sort(key=lambda r: (r.get("start") or "", r["incident_id"]))

texts = [naturalize(r) for r in records]
X = np.array([numeric_vector(r) for r in records], dtype=np.float64)
store = MemoryStore("/202531630503/lyt/models/all-MiniLM-L6-v2")
t0 = time.time()
V = store.encode(texts)
print(f"[MEM] 编码 {V.shape} 用时 {time.time()-t0:.1f}s", flush=True)
sc = NumericScaler().fit(X)
Z = sc.transform(X)

# ---- 顺序建库（先到先当模板），自监督强化 ----
items: list[dict] = []          # 模板
n_max = len(records)
BERT_M = np.zeros((n_max, V.shape[1]), dtype=np.float32)   # 预分配，避免 O(n^2) vstack
NUM_M  = np.zeros((n_max, Z.shape[1]), dtype=np.float32)
hits_arr = np.zeros(n_max, dtype=np.int64)
n_tpl = 0
res = []
for i, (fp, t, v, z) in enumerate(zip(records, texts, V, Z)):
    if n_tpl:
        B = BERT_M[:n_tpl]; N = NUM_M[:n_tpl]
        sem = B @ v
        num = np.exp(-np.linalg.norm(N - z[None, :], axis=1))
        sim = W_SEM * sem + (1 - W_SEM) * num
        # 自监督权重：命中多的模板更可信（放大其相似度）
        hits = hits_arr[:n_tpl]
        w = 1.0 + np.log1p(hits.astype(float))
        simw = sim * (0.7 + 0.3 * (w / max(w.max(), 1.0)))
        order = np.argsort(-simw)[:TOP_K]
        best = int(order[0]); best_sim = float(sim[best])
    else:
        order, best, best_sim = [], None, 0.0

    if best is not None and best_sim >= TAU:
        hits_arr[best] += 1
        votes: dict[str, float] = {}
        for j in order:
            if float(sim[j]) < TAU * 0.9: continue
            votes[items[j]["label"]] = votes.get(items[j]["label"], 0.0) + float(sim[j])
        tot = sum(votes.values()) or 1.0
        label = max(votes, key=votes.get)
        out = dict(label=label, sublabel=items[best]["sublabel"],
                   confidence=votes[label] / tot, matched=True,
                   template_id=items[best]["tid"], nearest_sim=best_sim,
                   votes={k: round(x / tot, 3) for k, x in votes.items()})
    else:
        tid = f"T{n_tpl:05d}"
        items.append(dict(tid=tid, label=fp.get("label") or "unknown",
                          sublabel=fp.get("sublabel") or "unknown",
                          first_incident=fp["incident_id"], region=fp["region"]))
        BERT_M[n_tpl] = v; NUM_M[n_tpl] = z; n_tpl += 1
        out = dict(label=fp.get("label") or "unknown",
                   sublabel=fp.get("sublabel") or "unknown",
                   confidence=0.0, matched=False, template_id=tid,
                   nearest_sim=best_sim or None, votes={})
    res.append({**fp, **out, "tpl_label": items[int(out["template_id"][1:])]["label"]})
    if (i+1) % 4000 == 0: print(f"  ...{i+1}/{len(records)}", flush=True)

# ---- 统计 ----
matched = sum(1 for r in res if r["matched"])
print("\n" + "="*76); print("① 覆盖率"); print("="*76)
print(f"  匹配已有模板 : {matched:6d} ({100*matched/len(res):.1f}%)")
print(f"  新建模板     : {len(res)-matched:6d} ({100*(len(res)-matched)/len(res):.1f}%)")
print(f"  模板总数     : {len(items)}")

print("\n" + "="*76); print("② 置信度分布（'砍预测'的前提）"); print("="*76)
conf = np.array([r["confidence"] for r in res if r["matched"]])
if conf.size:
    for lo, hi in ((0,0.4),(0.4,0.6),(0.6,0.8),(0.8,1.01)):
        n = int(((conf>=lo)&(conf<hi)).sum())
        print(f"  [{lo:.1f},{hi:.1f}) : {n:6d} ({100*n/conf.size:5.1f}%)")
    print(f"  中位 {np.median(conf):.3f} | 均值 {conf.mean():.3f}")

print("\n" + "="*76); print("③ 跨区域泛化（region 当通配符剥掉的验证）"); print("="*76)
tpl_reg = collections.defaultdict(set); tpl_n = collections.Counter()
for r in res:
    tpl_reg[r["template_id"]].add(r["region"]); tpl_n[r["template_id"]] += 1
cross = sum(1 for t, s in tpl_reg.items() if len(s) > 1)
print(f"  被 >=2 区域共用的模板: {cross} / {len(tpl_reg)} ({100*cross/max(len(tpl_reg),1):.1f}%)")
for tid, n in tpl_n.most_common(8):
    print(f"    {tid}: {n:4d} 次命中, 跨 {len(tpl_reg[tid])} 区域 {sorted(tpl_reg[tid])[:4]}")

print("\n" + "="*76); print("④ 记忆库 vs 规则判定（一致率）"); print("="*76)
both = [r for r in res if r["matched"]]
agree = sum(1 for r in both if r["label"] == r["tpl_label"])
print(f"  已匹配 {len(both)} 条，与模板标签一致 {agree} ({100*agree/max(len(both),1):.1f}%)")
print(f"  记忆库输出类别分布: {dict(collections.Counter(r['label'] for r in res).most_common())}")

os.makedirs("outputs_memory", exist_ok=True)
with open("outputs_memory/incidents_v2.jsonl", "w", encoding="utf-8") as fh:
    for r in res:
        fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
print(f"\n[MEM] 已保存 outputs_memory/incidents_v2.jsonl")
