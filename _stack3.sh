#!/bin/bash
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/stack3.log
{
echo "[$(date -u +%H:%M:%S)] 1) 注入门槛降到 0.85（含全部 >=0.90）"
python3 f_inject_anomalies.py --base submissions/submit_llm_rc.jsonl \
  --out submissions/submit_inj85.jsonl --gate 0.85 --dedup-dice 0.95
echo "[$(date -u +%H:%M:%S)] 2) 为全部预测补重锚变体"
python3 f_reanchor.py --base submissions/submit_inj85.jsonl \
  --out submissions/submit_inj85_rean.jsonl
echo "[$(date -u +%H:%M:%S)] STACK3_DONE"
} > $L 2>&1
