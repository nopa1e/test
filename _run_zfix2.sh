#!/bin/bash
# 用当前 HEAD 代码重跑**两批**的全链路，配置各自对齐当前最佳基座：
#   第一批 outputs_experiment_f_full   : ws=data  span=20260819040000_20260902040000  bin=5 gap=5 dur=5 pts=2
#   第二批 outputs_experiment_f_stage2 : ws=data2 span=20260917040000_20260924040000  bin=1 gap=1 dur=1 pts=2
#
# 与 _run_zfixfull.sh 的区别（那个脚本有两处错，见工作文档 §45.10）：
#   1) 它只跑了第一批，而最佳提交同时覆盖两批（batch1 14992 + batch2 40792 条）；
#   2) 它以为基座是 bin=5，其实只有第一批是 bin=5，第二批是 bin=1。
#
# 本脚本包含 HEAD 的三项修复：peak_z 尺度（MAD+截断）、resource 子族阈值 3.0->30.0、
# 候选集补齐官方 10 网元。incident_max_affinity_edges=50000 用来压住 O(n^2) 亲和图。
set -u
cd /202531630503/lyt/aiops_diagnosis
LOG=/202531630503/lyt/aiops_diagnosis/f_logs
PAR=2
NETFLOW=14000000
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"

B1_WS=/202531630503/lyt/workspace/data
B1_OUT=outputs_experiment_f_zB1
B1_CFG="--bin-minutes 5 --episode-max-gap-minutes 5 --episode-min-abnormal-points 2 --episode-min-duration-minutes 5"

B2_WS=/202531630503/lyt/workspace/data2
B2_OUT=outputs_experiment_f_zB2
B2_CFG="--bin-minutes 1 --episode-max-gap-minutes 1 --episode-min-abnormal-points 2 --episode-min-duration-minutes 1"

mkdir -p "$LOG" "$B1_OUT" "$B2_OUT"

run_one_batch() {
  local tag=$1 ws=$2 out=$3 cfg=$4
  echo "[$(date -u +%H:%M:%S)] ========== $tag f6_base =========="
  echo "$ALL" | tr ' ' '\n' | xargs -P $PAR -I{} bash -c "
    cd /202531630503/lyt/aiops_diagnosis
    python3 run_experiment_f_evidence_agent.py --workspace $ws --regions {} \
      --output-dir $out --stage f6_base --torch-threads 1 \
      --incident-max-affinity-edges 50000 $cfg \
      > $LOG/${tag}_f6_{}.log 2>&1
    echo \"  [\$(date -u +%H:%M:%S)] $tag f6 {} rc=\$?\""
  echo "[$(date -u +%H:%M:%S)] ========== $tag 后续阶段 =========="
  for spec in "evidence::" "flow::--max-netflow-rows $NETFLOW" "predictive::--max-lag-minutes 5" "candidates::"; do
    stage=${spec%%::*}; extra=${spec#*::}
    echo "[$(date -u +%H:%M:%S)] --- $tag $stage ---"
    echo "$ALL" | tr ' ' '\n' | xargs -P $PAR -I{} bash -c "
      cd /202531630503/lyt/aiops_diagnosis
      python3 run_experiment_f_evidence_agent.py --workspace $ws --regions {} \
        --output-dir $out --stage $stage --torch-threads 1 $extra \
        > $LOG/${tag}_${stage}_{}.log 2>&1
      echo \"  [\$(date -u +%H:%M:%S)] $tag $stage {} rc=\$?\""
  done
  echo "[$(date -u +%H:%M:%S)] ========== $tag finalize =========="
  python3 run_experiment_f_evidence_agent.py --workspace "$ws" --regions all \
    --output-dir "$out" --stage finalize --final-dir "$out/final" --no-llm-rerank \
    > "$LOG/${tag}_finalize.log" 2>&1
  echo "[$(date -u +%H:%M:%S)] $tag finalize rc=$?  lines=$(wc -l < $out/final/predictions_f_all.jsonl 2>/dev/null)"
}

run_one_batch zb1 "$B1_WS" "$B1_OUT" "$B1_CFG"
run_one_batch zb2 "$B2_WS" "$B2_OUT" "$B2_CFG"

echo "[$(date -u +%H:%M:%S)] ZBOTH_DONE"
