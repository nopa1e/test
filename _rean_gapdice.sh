#!/bin/bash
# 把 §50 里被行数惩罚吃掉的 ~0.50 分拿回来。
#
# 目标：拿住重锚的对齐收益，同时把总行数压回 §47.4 测出的峰值 ~55784。
#
# 关键约束（§49.2）：**不能用 step-factor 压行数**——那会放大槽宽
# （W/step_factor），既丢召回又压 S_AD，正是 §47.6 踩的坑。
# 正确做法是**保持 step-factor=2.0（槽宽 5.0 / 1.5 分钟不变）**，
# 用 --gap-dice 调"什么样的缺口才算缺口"：
#   gap-dice 越低 -> 越多时段被判定为"已覆盖" -> 填充行越少，且槽宽不变。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/rean_gapdice.log
{
  echo "[$(date -u +%H:%M:%S)] 基座 submit_rean_llm2.jsonl = $(wc -l < submissions/submit_rean_llm2.jsonl) 行"
  for gd in 0.50 0.65 0.80; do
    out="submissions/submit_reangd${gd}.jsonl"
    echo "[$(date -u +%H:%M:%S)] --- gap-dice $gd (step-factor 固定 2.0) ---"
    python3 f_fill_timeline.py \
      --base submissions/submit_rean_llm2.jsonl \
      --out "$out" \
      --gap-dice "$gd" --step-factor 2.0 2>&1 | tail -6
    echo "  -> $(wc -l < $out) 行"
  done
  echo "[$(date -u +%H:%M:%S)] GAPDICE_SWEEP_DONE"
} > $L 2>&1
