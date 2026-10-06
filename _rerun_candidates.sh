#!/bin/bash
# 用修好的 local_anomaly（绝对量级饱和映射）重跑 candidates 阶段。
#
# 为什么要重跑：local_anomaly 是 RootScore 的 7 项之一（权重 0.20），
# 它的尺度变了 -> root_score 变了 -> top5 顺序变了。candidates.json 是
# top5 的唯一来源，必须重算，否则 §43.5 ③ 的修复对提交毫无作用。
#
# 只跑 candidates（不改 evidence），所以是单变量：
#   唯一变化 = local_anomaly 的度量方式。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/rerun_cand.log
PAR=2
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"

{
echo "[$(date -u +%H:%M:%S)] 备份旧 candidates.json（便于对照/回退）"
for spec in "outputs_experiment_f_zB1:zB1" "outputs_experiment_f_zB2:zB2"; do
  out=${spec%%:*}; tag=${spec##*:}
  for r in $ALL; do
    d=$(ls -d ${out}/${r}_*/ 2>/dev/null | head -1)
    [ -n "$d" ] && [ -s "${d}candidates.json" ] && cp "${d}candidates.json" "${d}candidates.json.bak-preminmax"
  done
  echo "  $tag 备份完成"
done

for spec in "outputs_experiment_f_zB1:/202531630503/lyt/workspace/data:zB1" \
            "outputs_experiment_f_zB2:/202531630503/lyt/workspace/data2:zB2"; do
  out=$(echo "$spec" | cut -d: -f1)
  ws=$(echo "$spec" | cut -d: -f2)
  tag=$(echo "$spec" | cut -d: -f3)
  echo "[$(date -u +%H:%M:%S)] ========== $tag candidates 重跑（ws=$ws）=========="
  echo "$ALL" | tr ' ' '\n' | xargs -P $PAR -I{} bash -c "
    cd /202531630503/lyt/aiops_diagnosis
    python3 run_experiment_f_evidence_agent.py --workspace $ws --regions {} \
      --output-dir $out --stage candidates --torch-threads 1 \
      > f_logs/rerun_cand_${tag}_{}.log 2>&1
    echo \"  [\$(date -u +%H:%M:%S)] $tag candidates {} rc=\$?\""
done
echo "[$(date -u +%H:%M:%S)] CAND_RERUN_DONE"
} > $L 2>&1
