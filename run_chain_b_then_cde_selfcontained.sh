#!/bin/bash
set -eu

cd /202131510121/lyt/aiops_diagnosis

CHAIN_DIR=outputs_experiment_chain_v2
mkdir -p "$CHAIN_DIR"
LOG="$CHAIN_DIR/chain.log"
PY=/202131510121/conda/envs/micro/bin/python
PY_VLLM=/202131510121/conda/envs/vllm_cu124_sys/bin/python
VLLM_URL=http://127.0.0.1:8000
VLLM_MODEL=deepseek-r1-14b
VLLM_MODEL_PATH=/202131510121/lyt/models/models/deepseek-ai--DeepSeek-R1-Distill-Qwen-14B/snapshots/master

ensure_vllm() {
  if "$PY" - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8000/v1/models', timeout=3).read()
PY
  then
    echo "vLLM already running"
    return 0
  fi
  echo "starting vLLM ..."
  nohup "$PY_VLLM" -m vllm.entrypoints.openai.api_server \
    --model "$VLLM_MODEL_PATH" \
    --served-model-name "$VLLM_MODEL" \
    --host 0.0.0.0 \
    --port 8000 \
    --dtype bfloat16 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.45 \
    --enable-prefix-caching \
    --enforce-eager \
    --disable-log-requests \
    > "$CHAIN_DIR/vllm.log" 2>&1 &
  for _ in $(seq 1 60); do
    sleep 5
    if "$PY" - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8000/v1/models', timeout=3).read()
PY
    then
      echo "vLLM is up"
      return 0
    fi
  done
  echo "vLLM failed to start; see $CHAIN_DIR/vllm.log"
  return 1
}

{
  echo "===== CHAIN v2 START $(date -Is) ====="
  ensure_vllm

  echo "===== B incident-level LLM (B own artifacts) START $(date -Is) ====="
  "$PY" run_experiment_a_incident_llm.py \
    --workspace /202131510121/lyt/workspace \
    --artifacts-dir outputs_experiment_b_full/_ablation/full \
    --output-dir outputs_experiment_b_incident_llm_v2 \
    --llm-base-url "$VLLM_URL" \
    --llm-model "$VLLM_MODEL" \
    --threshold 0.5 \
    --merge-gap-minutes 5 \
    --workers 8 \
    --llm-max-new-tokens 256
  echo "===== B incident-level LLM DONE $(date -Is) ====="

  echo "===== C base (C own IF/VAE + incidents, no GNN usage) START $(date -Is) ====="
  "$PY" run_pipeline.py \
    --workspace /202131510121/lyt/workspace \
    --output-dir outputs_experiment_c_base \
    --experiment incident \
    --vae-epochs 3 \
    --gnn-epochs 0 \
    --llm-backend none
  echo "===== C base DONE $(date -Is) ====="

  echo "===== C dependency LLM START $(date -Is) ====="
  "$PY" run_experiment_c_dependency_llm.py \
    --workspace /202131510121/lyt/workspace \
    --artifacts-dir outputs_experiment_c_base \
    --output-dir outputs_experiment_c_full_llm \
    --llm-base-url "$VLLM_URL" \
    --llm-model "$VLLM_MODEL" \
    --threshold 0.5 \
    --workers 8
  echo "===== C dependency LLM DONE $(date -Is) ====="

  echo "===== D base (D own IF/VAE + incidents) START $(date -Is) ====="
  "$PY" run_pipeline.py \
    --workspace /202131510121/lyt/workspace \
    --output-dir outputs_experiment_d_base \
    --experiment incident \
    --vae-epochs 3 \
    --gnn-epochs 0 \
    --llm-backend none
  echo "===== D base DONE $(date -Is) ====="

  echo "===== D second-stage weighted START $(date -Is) ====="
  "$PY" run_experiment_d_e_second_stage.py \
    --artifacts-dir outputs_experiment_d_base \
    --output-dir outputs_experiment_d_weighted \
    --mode weighted \
    --alpha 1.0 \
    --epochs 20
  echo "===== D second-stage weighted DONE $(date -Is) ====="

  echo "===== D LLM START $(date -Is) ====="
  "$PY" run_experiment_d_e_llm.py \
    --workspace /202131510121/lyt/workspace \
    --artifacts-dir outputs_experiment_d_base \
    --second-stage-dir outputs_experiment_d_weighted \
    --output-dir outputs_experiment_d_llm \
    --llm-base-url "$VLLM_URL" \
    --llm-model "$VLLM_MODEL" \
    --threshold 0.5 \
    --workers 8 \
    --llm-max-new-tokens 256
  echo "===== D LLM DONE $(date -Is) ====="

  echo "===== E base (E own IF/VAE + incidents) START $(date -Is) ====="
  "$PY" run_pipeline.py \
    --workspace /202131510121/lyt/workspace \
    --output-dir outputs_experiment_e_base \
    --experiment incident \
    --vae-epochs 3 \
    --gnn-epochs 0 \
    --llm-backend none
  echo "===== E base DONE $(date -Is) ====="

  echo "===== E second-stage threshold START $(date -Is) ====="
  "$PY" run_experiment_d_e_second_stage.py \
    --artifacts-dir outputs_experiment_e_base \
    --output-dir outputs_experiment_e_threshold \
    --mode threshold \
    --threshold 0.7 \
    --epochs 20
  echo "===== E second-stage threshold DONE $(date -Is) ====="

  echo "===== E LLM START $(date -Is) ====="
  "$PY" run_experiment_d_e_llm.py \
    --workspace /202131510121/lyt/workspace \
    --artifacts-dir outputs_experiment_e_base \
    --second-stage-dir outputs_experiment_e_threshold \
    --output-dir outputs_experiment_e_llm \
    --llm-base-url "$VLLM_URL" \
    --llm-model "$VLLM_MODEL" \
    --threshold 0.5 \
    --workers 8 \
    --llm-max-new-tokens 256
  echo "===== E LLM DONE $(date -Is) ====="

  echo "===== CHAIN v2 COMPLETE $(date -Is) ====="
} >> "$LOG" 2>&1
