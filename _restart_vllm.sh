#!/bin/bash
cd /202531630503/lyt/aiops_diagnosis
LOG=/202531630503/lyt/aiops_diagnosis/f_logs/vllm_restart.log
CUDA_VISIBLE_DEVICES=0 nohup python3 -m vllm.entrypoints.openai.api_server \
  --model /202531630503/lyt/models/DeepSeek-R1-Distill-Qwen-14B \
  --served-model-name deepseek-r1-14b --host 127.0.0.1 --port 8000 \
  --gpu-memory-utilization 0.85 --max-model-len 4096 --max-num-seqs 8 \
  > $LOG 2>&1 &
echo "started $!"
