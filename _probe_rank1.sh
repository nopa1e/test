#!/bin/bash
# 探针：单区域 f6_base，只为量两个数（episode/incident 规模），不为提交。
#
# 配置思路：原始数据是 1 分钟分辨率，"9 台设备同时报警"很大程度是我们自己
# bin_minutes=5 压出来的。改成 bin=1 让 first_anomaly_time 有 1 分钟分辨率，
# 从而解开 temporal_priority(0.25) 与 outgoing_propagation(0.20) 的并列塌缩。
#
# 但 bin=1 + 默认 episode_max_gap=1 会让相邻 2 分钟的异常各自成段，episode 数
# 暴涨（首次 ep1b 实验实测：beida 2591 -> 10718）。故把 max_gap 放宽到 5 分钟，
# 让 1 分钟分辨率下本来就连成一片的异常仍归为一段——只买到时间分辨率，
# 不改变事件的粒度。
set -u
cd /202531630503/lyt/aiops_diagnosis
timeout 1800 python3 run_experiment_f_evidence_agent.py \
  --workspace /202531630503/lyt/workspace/data \
  --regions xian --output-dir outputs_experiment_f_rank1_probe \
  --stage f6_base --torch-threads 1 \
  --bin-minutes 1 --episode-max-gap-minutes 5 \
  --episode-min-abnormal-points 1 --episode-min-duration-minutes 0 \
  --incident-max-affinity-edges 50000
echo PROBE_DONE
