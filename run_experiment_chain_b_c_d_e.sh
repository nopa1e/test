#!/bin/bash
set -eu

cd /202131510121/lyt/aiops_diagnosis

CHAIN_DIR=outputs_experiment_chain
mkdir -p "$CHAIN_DIR"
LOG="$CHAIN_DIR/chain.log"
PY=/202131510121/conda/envs/micro/bin/python
VLLM_URL=http://127.0.0.1:8000
VLLM_MODEL=deepseek-r1-14b

{
  echo "===== CHAIN START $(date -Is) ====="
  echo "waiting for experiment B PID 56089 ..."
  while ps -p 56089 >/dev/null 2>&1; do
    sleep 60
  done
  echo "===== EXPERIMENT B PROCESS GONE $(date -Is) ====="
  test -d outputs_experiment_b_full/_ablation/full
  echo "B artifacts found"

  echo "===== START EXPERIMENT C $(date -Is) ====="
  mkdir -p outputs_experiment_c_dependency_llm
  "$PY" run_experiment_c_dependency_llm.py \
    --workspace /202131510121/lyt/workspace \
    --artifacts-dir outputs_experiment_b_full/_ablation/full \
    --output-dir outputs_experiment_c_dependency_llm \
    --llm-base-url "$VLLM_URL" \
    --llm-model "$VLLM_MODEL" \
    --threshold 0.5 \
    --workers 8 \
    > outputs_experiment_c_dependency_llm/run.log 2>&1
  echo "===== EXPERIMENT C DONE $(date -Is) ====="

  echo "===== START EXPERIMENT D $(date -Is) ====="
  mkdir -p outputs_experiment_d_weighted
  "$PY" run_experiment_d_e_second_stage.py \
    --artifacts-dir outputs_experiment_b_full/_ablation/full \
    --output-dir outputs_experiment_d_weighted \
    --mode weighted \
    --alpha 1.0 \
    --epochs 20 \
    > outputs_experiment_d_weighted/run.log 2>&1
  echo "===== EXPERIMENT D DONE $(date -Is) ====="

  echo "===== START EXPERIMENT E $(date -Is) ====="
  mkdir -p outputs_experiment_e_threshold
  "$PY" run_experiment_d_e_second_stage.py \
    --artifacts-dir outputs_experiment_b_full/_ablation/full \
    --output-dir outputs_experiment_e_threshold \
    --mode threshold \
    --threshold 0.7 \
    --epochs 20 \
    > outputs_experiment_e_threshold/run.log 2>&1
  echo "===== EXPERIMENT E DONE $(date -Is) ====="
  echo "===== CHAIN COMPLETE $(date -Is) ====="
} >> "$LOG" 2>&1
