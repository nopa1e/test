#!/bin/bash
cd /202531630503/lyt/aiops_diagnosis
OUT=outputs_experiment_f_full_ep1
mkdir -p $OUT f_logs
echo "[$(date +%H:%M:%S)] === f6_base ep1 开始（min_abnormal_points=1, min_duration=0）==="
echo beida chengdu guangzhou nanjing shanghai shenyang wuhan xian | tr ' ' '\n' | xargs -P2 -I{} bash -c "
  python3 run_experiment_f_evidence_agent.py \
    --workspace /202531630503/lyt/workspace \
    --regions {} --output-dir $OUT \
    --stage f6_base --torch-threads 1 \
    --episode-min-abnormal-points 1 --episode-min-duration-minutes 0 \
    > f_logs/ep1_f6_{}.log 2>&1
  echo \"  [\$(date +%H:%M:%S)] {} done rc=\$?\"
"
echo "[$(date +%H:%M:%S)] f6_base ep1 全部结束"
echo "EP1_F6_DONE"
