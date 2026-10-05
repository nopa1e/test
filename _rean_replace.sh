#!/bin/bash
# 替换式重锚：行数不变（无行数惩罚），把对齐差的那些窗口直接换掉。
# 见 f_reanchor_replace.py 的 docstring。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/rean_replace.log
{
  echo "[$(date -u +%H:%M:%S)] 1) 只为 dc<0.5（与自身异常段 Dice 差）的行生成变体"
  python3 f_reanchor.py \
    --base submissions/submit_fill2.jsonl \
    --only-worse-than 0.5 \
    --out /tmp/rean_bad.jsonl
  echo "  rc=$?"
  echo "[$(date -u +%H:%M:%S)] 2) 替换式合成（行数保持不变）"
  python3 f_reanchor_replace.py \
    --base submissions/submit_fill2.jsonl \
    --rean /tmp/rean_bad.jsonl \
    --out submissions/submit_fill2_repl.jsonl
  echo "  rc=$?"
  wc -l submissions/submit_fill2_repl.jsonl
  echo "[$(date -u +%H:%M:%S)] REPLACE_DONE"
} > $L 2>&1
