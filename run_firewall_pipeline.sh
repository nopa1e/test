#!/bin/bash
# firewall 实验：并行跑 8 区域的 evidence -> flow -> candidates
cd /202531630503/lyt/aiops_diagnosis
OUT=outputs_experiment_f_full_fw
ART=outputs_experiment_f_full
REGIONS="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"

run_par() {
  local stage=$1; shift
  echo "[$(date +%F' '%T)] ===== $stage （8 区域并行 P4）====="
  echo "$REGIONS" | tr ' ' '\n' | xargs -P4 -I{} bash -c "
    python3 run_experiment_f_evidence_agent.py \
      --workspace /202531630503/lyt/workspace \
      --regions {} \
      --output-dir $OUT \
      --artifacts-dir $ART \
      --stage $stage --torch-threads 1 \
      $* > f_logs/fw_${stage}_{}.log 2>&1
  "
  echo "[$(date +%F' '%T)] $stage 完成"
}

run_par evidence
run_par candidates
echo "[$(date +%F' '%T)] FW_PIPELINE_DONE"
