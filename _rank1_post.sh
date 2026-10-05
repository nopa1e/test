#!/bin/bash
set -u
cd /202531630503/lyt/aiops_diagnosis
LOG=f_logs/rank1_post.log
{
echo "[$(date -u +%H:%M:%S)] 1) 切出 rank1 的 batch1 单独一份"
python3 - <<'PY'
import json
rows=[json.loads(l) for l in open("outputs_experiment_f_rank1/final/predictions_f_all.jsonl",encoding="utf-8")]
b1=[r for r in rows if "20260819"<=r["start_time"][:10].replace("-","")<="20260902"]
with open("submissions/submit_rank1_b1.jsonl","w",encoding="utf-8") as f:
    for r in b1: f.write(json.dumps(r,ensure_ascii=False)+"\n")
print(f"  rank1 batch1 = {len(b1)} 行")
PY

echo "[$(date -u +%H:%M:%S)] 2) 缺口填充（槽位宽 = 窗口宽/2；rank1 batch1 窗宽中位 2 分，故 W=4）"
python3 f_fill_timeline.py --base submissions/submit_rank1_b1.jsonl \
  --window-b1 4 --out submissions/submit_rank1_b1_fill.jsonl 2>&1 | grep -E "填充|已覆盖|新增|警告"

echo "[$(date -u +%H:%M:%S)] 3) 与 fill2 的 batch2 合并"
python3 - <<'PY'
import json
b1=[json.loads(l) for l in open("submissions/submit_rank1_b1_fill.jsonl",encoding="utf-8")]
best=[json.loads(l) for l in open("submissions/submit_fill2.jsonl",encoding="utf-8")]
b2=[r for r in best if not ("20260819"<=r["start_time"][:10].replace("-","")<="20260902")]
out=b1+b2
ids=[r["prediction_id"] for r in out]
with open("submissions/submit_rank1_fill_merged.jsonl","w",encoding="utf-8") as f:
    for r in out: f.write(json.dumps(r,ensure_ascii=False)+"\n")
print(f"  b1={len(b1)} + b2={len(b2)} = {len(out)} 行, id重复 {len(ids)-len(set(ids))}")
from statistics import median
from datetime import datetime
w=[(datetime.fromisoformat(r["end_time"])-datetime.fromisoformat(r["start_time"])).total_seconds()/60 for r in b1]
print(f"  batch1 窗宽中位 {median(w):.0f} 分")
PY
echo "[$(date -u +%H:%M:%S)] RANK1_POST_DONE"
} > $LOG 2>&1
