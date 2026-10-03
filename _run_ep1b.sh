#!/bin/bash
# 单变量实验 ep1b：只放开"单栅格 episode"，其余全部显式钉死为基线值。
#
# ⚠️ 教训（2026-10-03 首次启动时踩到）：runner 的默认常量是
#   F_BIN_MINUTES=1 / F_EPISODE_MAX_GAP_MINUTES=1 / F_EPISODE_MIN_DURATION_MINUTES=1，
# 而 batch1 基线当年是**显式传了 --bin-minutes 5 --episode-max-gap-minutes 5
# --episode-min-duration-minutes 5** 才跑的（见各区域 experiment_config.json）。
# 因此只覆盖 min_points 会连带继承 bin=1/gap=1，变成一次三变量实验，
# 产出 10718 episodes / 181417 点（基线 614 / 36274）——对照被污染，已作废重跑。
#
# 本实验的唯一逻辑变量："允许单栅格 episode"：
#   --episode-min-abnormal-points   2 -> 1   （len(abn_window)=1 的孤立点此前被丢弃）
#   --episode-min-duration-minutes  5 -> 0   （单点 duration=0，此前被第二个过滤器丢弃）
# 二者必须同时改，只改其一完全无效（episode.py 注释亦写明
# "Single-bin episodes are still allowed only when min_duration=0"）。
#
# 用 data/ 子工作区定向 batch1，避免误触 data2/ 的第二批。
set -u
cd /202531630503/lyt/aiops_diagnosis
WS=/202531630503/lyt/workspace/data
OUT=outputs_experiment_f_ep1
LOG=/202531630503/lyt/aiops_diagnosis/f_logs
mkdir -p "$OUT" "$LOG"
echo "[$(date -u +%H:%M:%S)] === ep1b f6_base 开始（batch1, 单变量=允许单栅格 episode）==="
echo beida chengdu guangzhou nanjing shanghai shenyang wuhan xian | tr ' ' '\n' | xargs -P 2 -I{} bash -c "
  cd /202531630503/lyt/aiops_diagnosis
  python3 run_experiment_f_evidence_agent.py \
    --workspace $WS --regions {} --output-dir $OUT \
    --stage f6_base --torch-threads 1 \
    --bin-minutes 5 --episode-max-gap-minutes 5 \
    --episode-min-abnormal-points 1 --episode-min-duration-minutes 0 \
    --incident-max-affinity-edges 200000 \
    > $LOG/ep1b_f6_{}.log 2>&1
  echo \"  [\$(date -u +%H:%M:%S)] {} rc=\$?\"
"
echo "[$(date -u +%H:%M:%S)] === ep1b f6_base 全部结束 ==="
echo EP1B_F6_DONE
