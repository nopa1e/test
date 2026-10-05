#!/bin/bash
# 用三项修复（z 尺度 / resource 阈值 / 10 元候选）重跑第一批全链路。
# 配置钉死为当前最佳基座 outputs_experiment_f_full 的同一套，
# 以便把"修复本身的效应"从其他变量里隔离出来：
#   bin=5  gap=5  pts=2  dur=5   （与 f_full/experiment_config.json 一致）
set -u
cd /202531630503/lyt/aiops_diagnosis
WS=/202531630503/lyt/workspace/data
OUT=outputs_experiment_f_zfull
LOG=/202531630503/lyt/aiops_diagnosis/f_logs
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"
F6="--bin-minutes 5 --episode-max-gap-minutes 5 \
    --episode-min-abnormal-points 2 --episode-min-duration-minutes 5 \
    --incident-max-affinity-edges 50000"
mkdir -p "$OUT" "$LOG"
echo "[$(date -u +%H:%M:%S)] ===== f6_base（含三项修复，配置同基线）====="
echo "$ALL" | tr ' ' '\n' | xargs -P 2 -I{} bash -c "
  cd /202531630503/lyt/aiops_diagnosis
  python3 run_experiment_f_evidence_agent.py --workspace $WS --regions {} \
    --output-dir $OUT --stage f6_base --torch-threads 1 $F6 \
    > $LOG/zfull_f6_{}.log 2>&1
  echo \"  [\$(date -u +%H:%M:%S)] f6 {} rc=\$?\""
run_stage() {
  local stage=$1; shift
  echo "[$(date -u +%H:%M:%S)] ===== $stage ====="
  echo "$ALL" | tr ' ' '\n' | xargs -P 2 -I{} bash -c "
    cd /202531630503/lyt/aiops_diagnosis
    python3 run_experiment_f_evidence_agent.py --workspace $WS --regions {} \
      --output-dir $OUT --stage $stage --torch-threads 1 $* \
      > $LOG/zfull_${stage}_{}.log 2>&1"
  echo "[$(date -u +%H:%M:%S)] $stage 完成"
}
run_stage evidence
run_stage flow --max-netflow-rows 14000000
run_stage predictive --max-lag-minutes 5
run_stage candidates
echo "[$(date -u +%H:%M:%S)] ===== finalize ====="
python3 run_experiment_f_evidence_agent.py --workspace "$WS" --regions all \
  --output-dir "$OUT" --stage finalize --final-dir "$OUT/final" --no-llm-rerank \
  > "$LOG/zfull_finalize.log" 2>&1
echo "[$(date -u +%H:%M:%S)] finalize rc=$?"
wc -l "$OUT/final/predictions_f_all.jsonl" 2>/dev/null
echo ZFULL_DONE
