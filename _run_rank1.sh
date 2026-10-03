#!/bin/bash
# rank1 全量：把时间分辨率从 5 分钟还原到原始数据的 1 分钟，
# 目的是解开 temporal_priority(0.25) 与 outgoing_propagation(0.20) 的并列塌缩
# （实测 bin=5 时 76%/64.5% 的 incident 里 9 台设备 first_anomaly_time 完全相同）。
#
# 配置经单区域探针验证（xian）：
#   episodes 7482 / incidents 1778 —— incident 数比 ep1b(2116) 还少 16%，
#   故全链路成本与 ep1b 相当（约 2 小时），不是 6 小时。
#   --episode-max-gap-minutes 5 是关键：只买时间精度，不放大事件数。
set -u
cd /202531630503/lyt/aiops_diagnosis
WS=/202531630503/lyt/workspace/data
OUT=outputs_experiment_f_rank1
LOG=/202531630503/lyt/aiops_diagnosis/f_logs
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"
F6="--bin-minutes 1 --episode-max-gap-minutes 5 \
    --episode-min-abnormal-points 1 --episode-min-duration-minutes 0 \
    --incident-max-affinity-edges 50000"
mkdir -p "$OUT" "$LOG"

echo "[$(date -u +%H:%M:%S)] ===== f6_base (bin=1) ====="
echo "$ALL" | tr ' ' '\n' | xargs -P 2 -I{} bash -c "
  cd /202531630503/lyt/aiops_diagnosis
  python3 run_experiment_f_evidence_agent.py --workspace $WS --regions {} \
    --output-dir $OUT --stage f6_base --torch-threads 1 $F6 \
    > $LOG/rank1_f6_{}.log 2>&1
  echo \"  [\$(date -u +%H:%M:%S)] f6 {} rc=\$?\""

run_stage() {
  local stage=$1; shift
  echo "[$(date -u +%H:%M:%S)] ===== $stage ====="
  echo "$ALL" | tr ' ' '\n' | xargs -P 2 -I{} bash -c "
    cd /202531630503/lyt/aiops_diagnosis
    python3 run_experiment_f_evidence_agent.py --workspace $WS --regions {} \
      --output-dir $OUT --stage $stage --torch-threads 1 $* \
      > $LOG/rank1_${stage}_{}.log 2>&1"
  echo "[$(date -u +%H:%M:%S)] $stage 完成"
}
run_stage evidence
run_stage flow --max-netflow-rows 14000000
run_stage predictive --max-lag-minutes 5
run_stage candidates

echo "[$(date -u +%H:%M:%S)] ===== finalize ====="
python3 run_experiment_f_evidence_agent.py --workspace "$WS" --regions all \
  --output-dir "$OUT" --stage finalize --final-dir "$OUT/final" --no-llm-rerank \
  > "$LOG/rank1_finalize.log" 2>&1
echo "[$(date -u +%H:%M:%S)] finalize rc=$?"
ls -la "$OUT/final/" 2>/dev/null | head -4
echo RANK1_DONE
