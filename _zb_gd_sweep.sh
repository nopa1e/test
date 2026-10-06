#!/bin/bash
# 把新基座的填充量压回最佳提交的密度（~55784 行）。
#
# §47.4 实测：同行数下，55784 -> 23.2556，84072 -> 22.9895，斜率约 -9.4e-6/行。
# 新基座在 gap-dice 0.9 时填到 97297 行，超出峰值 41513 行 ≈ 白亏 0.39 分。
# 约束（§49.2）：**不能动 step-factor**——那会改槽宽（W/sf），既丢召回又压 S_AD。
# 所以只用 --gap-dice 调"什么样的缺口才算缺口"，槽宽保持 5.0 / 1.5 分钟不变。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/zb_gd_sweep.log
{
  echo "[$(date -u +%H:%M:%S)] 基座 submissions/zb_base.jsonl = $(wc -l < submissions/zb_base.jsonl) 行"
  for gd in 0.30 0.45 0.60; do
    out="submissions/zb_fill_gd${gd}.jsonl"
    echo "[$(date -u +%H:%M:%S)] --- gap-dice $gd ---"
    python3 f_fill_timeline.py --base submissions/zb_base.jsonl --out "$out" \
      --gap-dice "$gd" --step-factor 2.0 2>&1 | tail -5
    echo "  -> $(wc -l < $out) 行"
  done
  echo "[$(date -u +%H:%M:%S)] ZB_GD_SWEEP_DONE"
} > $L 2>&1
