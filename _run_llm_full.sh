#!/bin/bash
# LLM 根因判定：两批都跑，但**只跑提交里实际存在的 incident**（过滤掉被 dedup 掉的），
# 省约 41% 算力。每区域开跑前做真实生成健康检查。
cd /202531630503/lyt/aiops_diagnosis
LOG=f_logs/llm_full.log
mkdir -p llm_rootcause_out

health() {
  timeout 90 curl -s http://127.0.0.1:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"deepseek-r1-14b","messages":[{"role":"user","content":"hi"}],"max_tokens":16,"temperature":0}' 2>/dev/null \
    | grep -q '"choices"'
}

ensure_vllm() {
  health && return 0
  echo "[$(date +%H:%M:%S)] !! vLLM 不健康，重启中"
  pkill -9 -f "vllm.entrypoints"; sleep 12
  CUDA_VISIBLE_DEVICES=1 nohup python3 -m vllm.entrypoints.openai.api_server \
    --model /202531630503/lyt/models/DeepSeek-R1-Distill-Qwen-14B \
    --served-model-name deepseek-r1-14b --port 8000 \
    --gpu-memory-utilization 0.60 --max-model-len 4096 --max-num-seqs 8 \
    >> f_logs/vllm_auto2.log 2>&1 &
  for i in $(seq 1 40); do sleep 15; health && break; done
  health && { echo "[$(date +%H:%M:%S)] vLLM 已恢复"; return 0; } || return 1
}

run_batch() {   # $1=out-dir  $2=iids-file  $3=tag
  for r in beida chengdu guangzhou nanjing shanghai shenyang wuhan xian; do
    OUT="llm_rootcause_out/${r}_${3}.json"
    [ -f "$OUT" ] && { echo "[$(date +%H:%M:%S)] $r [$3] 已有产物，跳过"; continue; }
    ensure_vllm || { echo "[$(date +%H:%M:%S)] !! vLLM 无法恢复，退出"; exit 1; }
    echo "[$(date +%H:%M:%S)] === $r [$3] ==="
    timeout 7200 python3 llm_rootcause.py \
      --out-dir "$1" --region "$r" --limit 100000 --workers 8 \
      --iids-file "$2" --save "$OUT" 2>&1 \
      | grep -E "iid 过滤|区域|成功解析|== RootScore|∈ RootScore|平均耗时"
  done
}

echo "[$(date +%H:%M:%S)] ===== 第一批 ====="
run_batch outputs_experiment_f_full   llm_rootcause_out/iids_b1.txt b1full
echo "[$(date +%H:%M:%S)] ===== 第二批 ====="
run_batch outputs_experiment_f_stage2 llm_rootcause_out/iids_b2.txt b2full
echo "LLM_FULL_DONE"
