#!/bin/bash
# GPU 探针：显存释放后自动拉起 vLLM
# 修复：启动用的 --gpu-memory-utilization 必须按**当时可用量**动态计算，
# 不能写死 0.70 —— 否则门槛(38G)与需求(0.70=55.4G)不匹配，
# vLLM 会在 47.8G 空闲时被拉起却只拿到 28.26G（恰好只够权重），cache 仍失败。
LOG=/202531630503/lyt/aiops_diagnosis/f_logs/gpu_watch.log
NEED_MIB=43008        # 42 GiB：权重 27.6 + cache 余量，且需 < util 上限
MAX_TRIES=288
i=0
echo "[$(date -u +%F' '%T)] 探针 v2 启动，门槛 ${NEED_MIB} MiB，util 动态计算" >> $LOG
while [ $i -lt $MAX_TRIES ]; do
  free1=$(nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits 2>/dev/null | awk -F, '$1==1{gsub(/ /,"",$2); print $2}')
  ts=$(date -u +%F' '%T)
  if [ -n "$free1" ] && [ "$free1" -ge "$NEED_MIB" ]; then
    RATIO=$(python3 -c "print(round(min(0.75, ($free1-512)/81920), 3))")
    echo "[$ts] GPU1 可用 ${free1} MiB >= 门槛；按可用量算得 util=${RATIO}" >> $LOG
    cd /202531630503/lyt/aiops_diagnosis
    CUDA_VISIBLE_DEVICES=1 nohup python3 -m vllm.entrypoints.openai.api_server \
      --model /202531630503/lyt/models/DeepSeek-R1-Distill-Qwen-14B \
      --served-model-name deepseek-r1-14b --port 8000 \
      --gpu-memory-utilization ${RATIO} \
      --max-model-len 4096 --max-num-seqs 4 \
      >> /202531630503/lyt/aiops_diagnosis/f_logs/vllm_auto.log 2>&1 &
    echo "[$ts] vLLM PID=$! util=${RATIO} 已启动" >> $LOG
    echo GPU_WATCH_LAUNCHED >> $LOG
    exit 0
  fi
  echo "[$ts] 第 $((i+1)) 次: GPU1 可用 ${free1:-?} MiB — 未达门槛" >> $LOG
  i=$((i+1))
  sleep 300
done
echo "[$(date -u +%F' '%T)] 超时退出" >> $LOG
