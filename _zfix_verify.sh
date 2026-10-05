#!/bin/bash
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/zfix.log
OUT=outputs_experiment_f_zfix
{
echo "[$(date -u +%H:%M:%S)] 1) f6_base（与 ep1b 同配置，便于对比 z 分布）"
python3 run_experiment_f_evidence_agent.py \
  --workspace /202531630503/lyt/workspace/data --regions xian \
  --output-dir $OUT --stage f6_base --torch-threads 1 \
  --bin-minutes 5 --episode-max-gap-minutes 5 \
  --episode-min-abnormal-points 1 --episode-min-duration-minutes 0 \
  --incident-max-affinity-edges 50000 2>&1 | tail -3

echo "[$(date -u +%H:%M:%S)] 2) evidence（这一步会重算 peak_z）"
python3 run_experiment_f_evidence_agent.py \
  --workspace /202531630503/lyt/workspace/data --regions xian \
  --output-dir $OUT --stage evidence --torch-threads 1 2>&1 | tail -3

echo "[$(date -u +%H:%M:%S)] 3) 验收：新旧 z 分布对比"
python3 - <<'PY'
import json, collections
def dist(p, tag):
    d=json.load(open(p)); inc=d["incidents"]
    per=collections.defaultdict(list)
    for v in inc.values():
        for node,pay in (v.get("nodes") or {}).items():
            for name,m in ((pay or {}).get("metrics") or {}).items():
                if isinstance(m,dict):
                    try: per[name].append(abs(float(m.get("peak_z") or 0)))
                    except Exception: pass
    print(f"  === {tag} ===")
    rows=[]
    for name in per:
        a=sorted(per[name]); n=len(a)
        rows.append((a[-1], name, a[n//2], a[int(n*0.99)]))
    for mx,name,med,p99 in sorted(rows, reverse=True):
        print(f"    {name:26s} 中位{med:9.2f} p99{p99:10.2f} 最大{mx:12.2f}")
    allv=sorted(x for v in per.values() for x in v)
    print(f"    >>> 全体: 最大 {allv[-1]:.1f}   (修复前 disk_read_rate 最大 27482521.6)")
PY

echo "[$(date -u +%H:%M:%S)] 4) 验收②：_resource_subtype 选中的子族分布"
python3 - <<'PY'
import json, collections, sys
sys.path.insert(0,'/202531630503/lyt/aiops_diagnosis')
from aiops.evidence.f_stages import _resource_subtype
d=json.load(open("outputs_experiment_f_zfix/xian_20260819040000_20260902040000/metric_evidence.json"))
c=collections.Counter(); n=0
for v in d["incidents"].values():
    n+=1
    for node,pay in (v.get("nodes") or {}).items():
        r=_resource_subtype(pay)
        if r: c[r[0]]+=1
print(f"  {n} 个 incident，_resource_subtype 命中 {sum(c.values())} 次")
for k,v in c.most_common(): print(f"    {k:22s} {v}")
PY
echo "[$(date -u +%H:%M:%S)] ZFIX_DONE"
} > $L 2>&1
