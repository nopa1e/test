#!/bin/bash
# 全量 LLM 类别判定：两批 8 区，共 7945 条 incident。
# 实测吞吐 0.34~0.37 incident/s（vLLM 跨容器，workers=12）-> 约 6~7 小时。
#
# 产物: llm_cat_out/{region}_{b1,b2}.json
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/llm_cat_full.log
mkdir -p llm_cat_out

B1=outputs_experiment_f_full
B2=outputs_experiment_f_stage2
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"

health() {
  python3 - <<'PY'
import json,urllib.request,sys
try:
    with urllib.request.urlopen("http://172.23.191.167:8000/v1/models", timeout=15) as r:
        print("OK" if b"deepseek" in r.read() else "BAD")
except Exception as e:
    print("FAIL", e)
PY
}

{
echo "[$(date -u +%H:%M:%S)] 健康检查: $(health)"
for spec in "$B1:b1" "$B2:b2"; do
  outdir=${spec%%:*}; tag=${spec##*:}
  echo "[$(date -u +%H:%M:%S)] ========== 批次 $tag ($outdir) =========="
  for r in $ALL; do
    OUT="llm_cat_out/${r}_${tag}.json"
    if [ -s "$OUT" ]; then echo "[$(date -u +%H:%M:%S)] $r [$tag] 已有产物，跳过"; continue; fi
    echo "[$(date -u +%H:%M:%S)] === $r [$tag] ==="
    timeout 14400 python3 llm_category.py \
      --out-dir "$outdir" --region "$r" --limit 100000 --workers 12 --save "$OUT" 2>&1 \
      | grep -E "健康|成功解析|吞吐|区域|大类分布|子类分布|Error|Traceback"
    echo "  rc=$?"
  done
done
echo "[$(date -u +%H:%M:%S)] LLM_CAT_FULL_DONE"
} > $L 2>&1
