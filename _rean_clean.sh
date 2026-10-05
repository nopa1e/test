#!/bin/bash
# 干净版重锚测试：**填充一个字都不动**，只往最终提交上叠加重锚变体。
#
# 为什么上一次（§47.6）不算数：
#   上次为了控行数把 --step-factor 从 2.0 降到 0.60，导致第一批填充槽宽
#   从 W/sf = 10/2.0 = 5.0 分钟 变成 10/0.60 = 16.7 分钟。槽宽变大本身就会
#   既丢召回又压 S_AD。所以那 −0.81 里混了槽宽，归因不成立（§49.1）。
#
# 这次的对照是单变量：
#   A) submit_fill2.jsonl                          55784 行  -> 23.2556（已实测）
#   B) submit_fill2.jsonl + f_REAN 变体           ~69871 行  -> 本次测
#   唯一的差别就是那 ~14087 条"重锚到异常起点"的窗口变体。
#
# 行数惩罚的预估：按 §47.4 实测斜率 9.41e-6/行，+14087 行 ≈ −0.13 分。
# 所以只要重锚本身的对齐收益 > 0.13，这次就会看到净正。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/rean_clean.log
{
  echo "[$(date -u +%H:%M:%S)] 基线 $(wc -l < submissions/submit_fill2.jsonl) 行（填充完全不动）"
  python3 f_reanchor.py \
    --base submissions/submit_fill2.jsonl \
    --out submissions/submit_fill2_rean.jsonl
  echo "  rc=$?"
  wc -l submissions/submit_fill2_rean.jsonl
  echo "[$(date -u +%H:%M:%S)] REAN_CLEAN_DONE"
} > $L 2>&1
