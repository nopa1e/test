#!/bin/bash
# 用 --max-vacuum-minutes 把新基座的填充量压回历史拐点。
#
# 依据（f_fill_timeline.py 的 --max-vacuum-minutes 注释 + §31 + §47.4）：
#   * 最佳提交 23.2556 的 38996 条填充，来自"真空区被跳过"这个曾被当 bug 的行为；
#   * 修掉后填充涨到 83147 条，远超 §31 拟合的拐点（约 34100 条）；
#   * 我在 §47.4 用 3 个实测点独立拟合出的峰值是 34093 条 —— 与 §31 吻合。
# 所以目标填充量 ≈ 34000~42000。gap-dice 对此几乎无效（实测只减 10%），
# 真正有效的旋钮是这个。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/zb_vac_sweep.log
{
  echo "[$(date -u +%H:%M:%S)] 基座 $(wc -l < submissions/zb_base.jsonl) 行；目标总行数 ~55784（填充 ~42300）"
  for mv in 20 40 80; do
    out="submissions/zb_vac${mv}.jsonl"
    echo "[$(date -u +%H:%M:%S)] --- max-vacuum-minutes $mv（step-factor 固定 2.0）---"
    python3 f_fill_timeline.py --base submissions/zb_base.jsonl --out "$out" \
      --gap-dice 0.9 --step-factor 2.0 --max-vacuum-minutes "$mv" 2>&1 | tail -5
    echo "  -> $(wc -l < $out) 行"
  done
  echo "[$(date -u +%H:%M:%S)] ZB_VAC_SWEEP_DONE"
} > $L 2>&1
