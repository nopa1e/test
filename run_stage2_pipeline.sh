#!/bin/bash
# 第二批数据全量流水线（4 核并行编排）v2
#
# v2 修复（2026-09-29）：完成判定从「目录是否存在」改为「关键产物是否存在」。
#   v1 的 bug：只要 <region>_<span>/ 目录在就当作已完成跳过，导致中途被 kill 的
#   区域（有 artifacts 但缺 incident_candidates.json）被误判为完成，
#   后续 evidence 阶段读不到 incident_candidates.json 直接失败、且不会重跑。
#
# 用法：
#   bash run_stage2_pipeline.sh            # 检测 + 补齐 + 跑完全链
#   bash run_stage2_pipeline.sh --check    # 只体检，不跑
set -u
cd /202531630503/lyt/aiops_diagnosis

WS=/202531630503/lyt/workspace/data2
OUT=outputs_experiment_f_stage2
LOG=/202531630503/lyt/f_logs
SPAN=20260917040000_20260924040000
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"
PAR=4
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

d_of() { echo "$OUT/${1}_$SPAN"; }

# f6_base 的关键产物：缺任一即视为未完成
KEY_F6="incident_candidates.json incident_clusters.json prediction.json"

is_done() {
  local r="$1" stage="$2" d; d=$(d_of "$r")
  [ -d "$d" ] || return 1
  case "$stage" in
    f6_base)
      local f
      for f in $KEY_F6; do [ -s "$d/$f" ] || return 1; done
      return 0 ;;
    evidence)   [ -s "$d/routing_evidence.json" ] && [ -s "$d/metric_evidence.json" ] ;;
    candidates) [ -s "$d/candidates.json" ] ;;
    predictive) [ -s "$d/predictive_evidence.json" ] ;;
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
  local regions="$*"
  [ -z "$regions" ] && { echo "[$(date -u +%H:%M:%S)] $stage : 无待处理区域"; return 0; }
  echo "[$(date -u +%H:%M:%S)] === $stage :$regions ==="
  echo "$regions" | tr ' ' '\n' | xargs -P $PAR -I{} bash -c \
    "cd /202531630503/lyt/aiops_diagnosis && python3 run_experiment_f_evidence_agent.py \
       --workspace $WS --regions {} --output-dir $OUT --stage $stage \
       > $LOG/${stage}_{}.log 2>&1"
  echo "[$(date -u +%H:%M:%S)] === $stage 完成 ==="
}

echo "========== 体检 =========="
for st in f6_base evidence candidates predictive; do
  p=$(pending "$st")
  n=$(echo $p | wc -w)
  echo "  $st : 待处理 $n 个 ->$p"
done
[ $CHECK_ONLY -eq 1 ] && exit 0

stage_run f6_base    "$(pending f6_base)"
stage_run evidence   "$(pending evidence)"
stage_run candidates "$(pending candidates)"
stage_run predictive "$(pending predictive)"

echo "[$(date -u +%H:%M:%S)] === finalize (汇总) ==="
python3 run_experiment_f_evidence_agent.py --workspace $WS --regions all \
  --output-dir $OUT --stage finalize --final-dir $OUT/final_stage2 --no-llm-rerank
echo "[$(date -u +%H:%M:%S)] === finalize 完成 ==="
echo PIPELINE_ALL_DONE
