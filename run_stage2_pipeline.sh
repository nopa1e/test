#!/bin/bash
# 第二批数据全量流水线（4 核并行编排）
# 依赖链：f6_base -> evidence -> candidates -> predictive -> finalize
# 已完成的区域/阶段会自动跳过或安全覆盖。
set -u
cd /202531630503/lyt/aiops_diagnosis

WS=/202531630503/lyt/workspace/data2
OUT=outputs_experiment_f_stage2
LOG=/202531630503/lyt/f_logs
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"
PAR=4

stage_run() {
  local stage="$1"; shift
  local regions="$*"
  echo "[$(date -u +%H:%M:%S)] === $stage : $regions ==="
  echo "$regions" | tr ' ' '\n' | xargs -P $PAR -I{} bash -c \
    "cd /202531630503/lyt/aiops_diagnosis && python3 run_experiment_f_evidence_agent.py \
       --workspace $WS --regions {} --output-dir $OUT --stage $stage \
       > $LOG/${stage}_{}.log 2>&1"
  echo "[$(date -u +%H:%M:%S)] === $stage 完成 ==="
}

# 等外部已经起好的 f6_base 进程结束
while pgrep -f "[r]un_experiment_f_evidence_agent" > /dev/null; do sleep 30; done
echo "[$(date -u +%H:%M:%S)] 外部 f6_base 进程已全部结束"

# 补齐尚未产出 artifacts 的区域
todo=""
for r in $ALL; do
  [ -d "$OUT/${r}_20260917040000_20260924040000" ] || todo="$todo $r"
done
if [ -n "$todo" ]; then
  stage_run f6_base $todo
fi

stage_run evidence   $ALL
stage_run candidates $ALL
stage_run predictive $ALL

echo "[$(date -u +%H:%M:%S)] === finalize (汇总) ==="
python3 run_experiment_f_evidence_agent.py --workspace $WS --regions all \
  --output-dir $OUT --stage finalize --final-dir $OUT/final_stage2 --no-llm-rerank
echo "[$(date -u +%H:%M:%S)] === finalize 完成 ==="
echo PIPELINE_ALL_DONE
