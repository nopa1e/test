#!/bin/bash
# ep1b 单变量实验的后续链路：evidence -> flow -> predictive -> candidates
# -> f1_stability -> finalize。
#
# 与 run_f_pipeline.sh 的唯一区别是工作区被钉死在 data/（batch1 的 8 个数据集）。
# run_f_pipeline.sh 用的是工作区根目录，那里现在有 16 个数据集（data/ + data2/），
# 会让本实验意外重跑第二批数据、破坏单变量对照。
#
# 每阶段先做产物体检，已完成的区域跳过，因此中途被 kill 后可直接重跑本脚本。
set -u
cd /202531630503/lyt/aiops_diagnosis || exit 1

WS=/202531630503/lyt/workspace/data
OUT=outputs_experiment_f_ep1
LOG=/202531630503/lyt/aiops_diagnosis/f_logs
SPAN=20260819040000_20260902040000
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"
NETFLOW_ROWS=${NETFLOW_ROWS:-14000000}
mkdir -p "$LOG"

d_of() { echo "$OUT/${1}_$SPAN"; }

is_done() {
  local r="$1" stage="$2" d; d=$(d_of "$r")
  [ -d "$d" ] || return 1
  case "$stage" in
    f6_base)    [ -s "$d/incident_candidates.json" ] && [ -s "$d/episode_scores.csv" ] ;;
    evidence)   [ -s "$d/routing_evidence.json" ] && [ -s "$d/metric_evidence.json" ] ;;
    flow)       [ -s "$d/flow_evidence.json" ] ;;
    predictive) [ -s "$d/predictive_evidence.json" ] ;;
    candidates) [ -s "$d/candidates.json" ] ;;
    *) return 1 ;;
  esac
}

pending() {
  local stage="$1" out="" r
  for r in $ALL; do is_done "$r" "$stage" || out="$out $r"; done
  echo "$out"
}

stage_run() {
  local stage="$1"; shift
  local todo; todo=$(pending "$stage")
  if [ -z "$todo" ]; then
    echo "[$(date -u +%H:%M:%S)] $stage : 无待处理区域"
    return 0
  fi
  echo "[$(date -u +%H:%M:%S)] === $stage :$todo ==="
  echo "$todo" | tr ' ' '\n' | xargs -P 2 -I{} bash -c \
    "cd /202531630503/lyt/aiops_diagnosis && python3 run_experiment_f_evidence_agent.py \
       --workspace $WS --regions {} --output-dir $OUT --stage $stage \
       --torch-threads 1 $* > $LOG/ep1b_${stage}_{}.log 2>&1"
  echo "[$(date -u +%H:%M:%S)] === $stage 完成 ==="
}

echo "========== 体检 =========="
for st in f6_base evidence flow predictive candidates; do
  p=$(pending "$st")
  echo "  $st : 待处理 $(echo $p | wc -w) 个 ->$p"
done

stage_run f6_base
stage_run evidence
stage_run flow --max-netflow-rows "$NETFLOW_ROWS"
stage_run predictive --max-lag-minutes 5
stage_run candidates

echo "[$(date -u +%H:%M:%S)] ===== f1_stability ====="
python3 run_experiment_f_evidence_agent.py --workspace "$WS" --regions all \
  --output-dir "$OUT" --stage f1_stability --torch-threads 1 \
  --n-repeats 20 --drop-fraction 0.05 > "$LOG/ep1b_f1_stability.log" 2>&1
echo "[$(date -u +%H:%M:%S)] f1_stability rc=$?"

echo "[$(date -u +%H:%M:%S)] ===== finalize ====="
python3 run_experiment_f_evidence_agent.py --workspace "$WS" --regions all \
  --output-dir "$OUT" --stage finalize --final-dir "$OUT/final" --no-llm-rerank \
  > "$LOG/ep1b_finalize.log" 2>&1
echo "[$(date -u +%H:%M:%S)] finalize rc=$?"
ls -la "$OUT/final/" 2>/dev/null

echo EP1B_PIPELINE_DONE
