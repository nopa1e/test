#!/bin/bash
# 在 reanchor 基座上扫 --step-factor，找出行数与「最佳提交 55784」最接近的一组。
#
# 为什么必须匹配行数（2026-10-05 实测）：
#   同一基座（7786 INC + 9002 AINJ）下
#     fill 0     条 -> 22.9745  (submit_ainj90)
#     fill 38996 条 -> 23.2556  (submit_fill2)   <-- 峰值
#     fill 67284 条 -> 22.9895  (submit_combo2)
#   多填 28288 行净亏 0.266 分。所以做 reanchor 时必须把总行数压回 5.6 万附近，
#   否则行数惩罚会吃掉重锚带来的对齐收益。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/rean_sweep.log
{
  echo "[$(date -u +%H:%M:%S)] reanchor 基座 $(wc -l < submissions/submit_rean_llm2.jsonl) 行"
  for sf in 0.40 0.50 0.60; do
    out=submissions/submit_rean_sf${sf}.jsonl
    echo "[$(date -u +%H:%M:%S)] --- step-factor $sf ---"
    python3 f_fill_timeline.py \
      --base submissions/submit_rean_llm2.jsonl \
      --out "$out" \
      --gap-dice 0.9 --step-factor "$sf"
    echo "  rc=$?  -> $(wc -l < $out) 行"
  done
  echo "[$(date -u +%H:%M:%S)] SWEEP_DONE"
} > $L 2>&1
