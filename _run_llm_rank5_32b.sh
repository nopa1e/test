#!/bin/bash
# 32B 五判据相对排名 · 跑在**旧基座**（outputs_experiment_f_full / f_stage2）上。
# 为什么用旧基座：它的 incident id 与最佳提交 submit_fill2_rean.jsonl 一一对应，
# 所以可以**直接替换 top5 顺序**，不需要任何时间移植——彻底消掉 §59.4 的移植混淆。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/llm_rank5_32b.log
mkdir -p llm_rank5_32b_out
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"
{
echo "[$(date -u +%H:%M:%S)] ===== 32B 五判据 · 旧基座 ====="
for spec in "outputs_experiment_f_full:b1" "outputs_experiment_f_stage2:b2"; do
  outdir=${spec%%:*}; tag=${spec##*:}
  echo "[$(date -u +%H:%M:%S)] ---------- 批次 $tag ($outdir) ----------"
  for r in $ALL; do
    OUT="llm_rank5_32b_out/${r}_${tag}.json"
    if [ -s "$OUT" ]; then echo "[$(date -u +%H:%M:%S)] $r [$tag] 已有产物，跳过"; continue; fi
    echo "[$(date -u +%H:%M:%S)] === $r [$tag] ==="
    timeout 43200 python3 llm_rank5.py --out-dir "$outdir" --region "$r" \
      --limit 100000 --workers 16 --max-nodes 10 --save "$OUT" 2>&1 \
      | grep -E "成功解析|吞吐|区域|top1 分布|五判据|Error|Traceback"
    echo "  rc=$?"
  done
done
echo "[$(date -u +%H:%M:%S)] LLM_RANK5_32B_DONE"
} > $L 2>&1