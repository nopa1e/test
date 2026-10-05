#!/bin/bash
# rank2：真正干净的单变量实验 —— 只改 bin_minutes，其余全部钉回 batch1 基线
#
# 对照 rank1 的教训：rank1 同时改了三项（bin 5->1, pts 2->1, dur 5->0），
# 而短窗（2 分钟）其实是 dur=0 造成的、不是 bin=1；短窗又把填充量顶到 8 万条。
#
# batch1 基线（outputs_experiment_f_full/experiment_config.json）:
#   bin=5  gap=5  pts=2  dur=5
# 本次:
#   bin=1  gap=5  pts=2  dur=5      <- 唯一变量
set -u
cd /202531630503/lyt/aiops_diagnosis
WS=/202531630503/lyt/workspace/data
OUT=outputs_experiment_f_rank2
LOG=/202531630503/lyt/aiops_diagnosis/f_logs
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"
F6="--bin-minutes 1 --episode-max-gap-minutes 5 \
    --episode-min-abnormal-points 2 --episode-min-duration-minutes 5 \
    --incident-max-affinity-edges 50000"
mkdir -p "$OUT" "$LOG"

echo "[$(date -u +%H:%M:%S)] ===== rank2 f6_base (bin=1, 其余同基线) ====="
echo "$ALL" | tr ' ' '\n' | xargs -P 2 -I{} bash -c "
  cd /202531630503/lyt/aiops_diagnosis
  python3 run_experiment_f_evidence_agent.py --workspace $WS --regions {} \
    --output-dir $OUT --stage f6_base --torch-threads 1 $F6 \
    > $LOG/rank2_f6_{}.log 2>&1
  echo \"  [\$(date -u +%H:%M:%S)] f6 {} rc=\$?\""

run_stage() {
  local stage=$1; shift
  echo "[$(date -u +%H:%M:%S)] ===== $stage ====="
  echo "$ALL" | tr ' ' '\n' | xargs -P 2 -I{} bash -c "
    cd /202531630503/lyt/aiops_diagnosis
    python3 run_experiment_f_evidence_agent.py --workspace $WS --regions {} \
      --output-dir $OUT --stage $stage --torch-threads 1 $* \
      > $LOG/rank2_${stage}_{}.log 2>&1"
  echo "[$(date -u +%H:%M:%S)] $stage 完成"
}
run_stage evidence
run_stage flow --max-netflow-rows 14000000
run_stage predictive --max-lag-minutes 5
run_stage candidates
echo "[$(date -u +%H:%M:%S)] ===== finalize ====="
python3 run_experiment_f_evidence_agent.py --workspace "$WS" --regions all \
  --output-dir "$OUT" --stage finalize --final-dir "$OUT/final" --no-llm-rerank \
  > "$LOG/rank2_finalize.log" 2>&1
echo "[$(date -u +%H:%M:%S)] finalize rc=$?"
wc -l "$OUT/final/predictions_f_all.jsonl" 2>/dev/null
echo RANK2_DONE
