#!/bin/bash
# §43.3 五判据相对排名 · 全量 · 跑在**新证据**（zB1 / zB2，含 z 尺度修复）上。
#
# 修正了之前那次 LLM 全量的三处偏差：
#   1) 证据：outputs_experiment_f_full(Sep 25) -> outputs_experiment_f_zB1(Oct 5)
#             outputs_experiment_f_stage2(Sep 29) -> outputs_experiment_f_zB2(Oct 5)
#   2) 问法：直接问类别/根因 -> §43.3 的**五判据相对排名** + 加权合成 R_d
#   3) 覆盖面：旧基座 13166 -> 新基座 13462
set -u
cd /202531630503/lyt/aiops_diagnosis
L=f_logs/llm_rank5_full.log
mkdir -p llm_rank5_out

B1=outputs_experiment_f_zB1
B2=outputs_experiment_f_zB2
ALL="beida chengdu guangzhou nanjing shanghai shenyang wuhan xian"

health() {
  python3 - <<'PY'
import urllib.request
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
    OUT="llm_rank5_out/${r}_${tag}.json"
    if [ -s "$OUT" ]; then echo "[$(date -u +%H:%M:%S)] $r [$tag] 已有产物，跳过"; continue; fi
    echo "[$(date -u +%H:%M:%S)] === $r [$tag] ==="
    timeout 21600 python3 llm_rank5.py \
      --out-dir "$outdir" --region "$r" --limit 100000 --workers 12 \
      --max-nodes 10 --save "$OUT" 2>&1 \
      | grep -E "成功解析|吞吐|区域|top1 分布|五判据|Error|Traceback"
    echo "  rc=$?"
  done
done
echo "[$(date -u +%H:%M:%S)] LLM_RANK5_FULL_DONE"
} > $L 2>&1
