#!/bin/bash
cd /202531630503/lyt/aiops_diagnosis
mkdir -p llm_rootcause_out
for r in beida chengdu guangzhou nanjing shanghai shenyang wuhan xian; do
  echo "[$(date +%H:%M:%S)] === $r ==="
  timeout 2400 python3 llm_rootcause.py \
    --out-dir outputs_experiment_f_full \
    --region "$r" --limit 60 --workers 6 \
    --save "llm_rootcause_out/${r}_b1.json" 2>&1 | grep -E "成功解析|== RootScore|∈ RootScore|平均耗时|区域" 
done
echo "LLM_RC_DONE"
