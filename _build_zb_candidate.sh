#!/bin/bash
# 用**新基座**（zB1+zB2，含 z 尺度修复 / resource 阈值 / 10 元候选 / local_anomaly 绝对量级）
# 建一个可提交的候选。这是新基座第一次被测。
#
# 链条与最佳提交（submit_fill2_rean -> 23.3344）保持同形：
#   合并两批 -> f_fill_timeline(gap-dice 0.9, step-factor 2.0)
# 先把填充量对上最佳提交的密度（§47.4 实测峰值在 ~5.6 万行）。
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/build_zb.log
{
  echo "[$(date -u +%H:%M:%S)] 1) 合并 zB1 + zB2"
  python3 - <<'PY'
import json
rows = []
for p in ("outputs_experiment_f_zB1/final/predictions_f_all.jsonl",
          "outputs_experiment_f_zB2/final/predictions_f_all.jsonl"):
    n = 0
    for l in open(p, encoding="utf-8"):
        l = l.strip()
        if l:
            rows.append(json.loads(l)); n += 1
    print(f"   {p}: {n}")
ids = [r["prediction_id"] for r in rows]
print(f"   合计 {len(rows)}；唯一 id {len(set(ids))}；重复 {len(ids)-len(set(ids))}")
with open("submissions/zb_base.jsonl", "w", encoding="utf-8") as fh:
    for r in rows:
        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
print("   -> submissions/zb_base.jsonl")
PY

  echo "[$(date -u +%H:%M:%S)] 2) 时间轴缺口填充（与最佳提交同参）"
  python3 f_fill_timeline.py \
    --base submissions/zb_base.jsonl \
    --out submissions/zb_fill.jsonl \
    --gap-dice 0.9 --step-factor 2.0 2>&1 | tail -8
  echo "  rc=$?  行数=$(wc -l < submissions/zb_fill.jsonl 2>/dev/null)"
  echo "[$(date -u +%H:%M:%S)] BUILD_ZB_DONE"
} > $L 2>&1
