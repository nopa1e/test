#!/bin/bash
# 在**最佳提交的原始链条上**插入一步 f_reanchor，其余参数逐字不变——单变量对照。
#
# 最佳提交的链条（见工作文档 §46.3）：
#   submit_ainj90_llm2.jsonl --f_fill_timeline(gap-dice 0.9, step-factor 2.0)--> submit_combo == submit_fill2 (23.2556)
#
# 本脚本只多插一步：
#   submit_ainj90_llm2.jsonl --f_reanchor--> submit_rean_llm2.jsonl --f_fill_timeline(同参数)--> submit_rean_fill.jsonl
#
# 动机（f_reanchor.py 自带实测）：我们的预测窗口与其**自身节点**的 >=0.9 异常段
# 相比 Dice 中位仅 0.500、41.4% 低于 0.4 门槛；重锚到异常起点后 >=0.4 比例
# 从 58.6% 升到 75.1%。而 f_REAN 从未出现在任何已提交文件里。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/rean_candidate.log
{
  echo "[$(date -u +%H:%M:%S)] 0) 基线核对"
  wc -l submissions/submit_ainj90_llm2.jsonl submissions/submit_combo.jsonl
  echo "[$(date -u +%H:%M:%S)] 1) f_reanchor：为每条预测补一条「重锚到异常起点」的窗口变体"
  python3 f_reanchor.py \
    --base submissions/submit_ainj90_llm2.jsonl \
    --out submissions/submit_rean_llm2.jsonl
  echo "  rc=$?"
  echo "[$(date -u +%H:%M:%S)] 2) f_fill_timeline：与最佳提交完全相同的参数"
  python3 f_fill_timeline.py \
    --base submissions/submit_rean_llm2.jsonl \
    --out submissions/submit_rean_fill.jsonl \
    --gap-dice 0.9 --step-factor 2.0
  echo "  rc=$?"
  echo "[$(date -u +%H:%M:%S)] 3) 结果"
  wc -l submissions/submit_rean_llm2.jsonl submissions/submit_rean_fill.jsonl
  echo "[$(date -u +%H:%M:%S)] REAN_DONE"
} > $L 2>&1
