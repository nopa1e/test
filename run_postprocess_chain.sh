#!/bin/bash
# Run the post-processing merge for B, then re-run C's LLM stage and merge it.
#
# Order requested by the user: "先跑B的后处理第二个脚本，然后跑C的".
#
# C needs its LLM stage RE-RUN first: on 2026-09-20 the platform reclaimed GPU
# resources at 16:44 UTC, vLLM answered every request with HTTP 500, and C
# produced 4089 rule fallbacks with an empty predictions_high_conf.jsonl.  The
# stage script now retries transient errors and exits non-zero when the LLM
# success rate is too low, so a repeat of that failure stops the chain instead of
# producing junk.
#
# Both stages are vLLM-bound, so they run strictly one after another - running
# them concurrently is what the earlier failure looked like.
set -uo pipefail

cd /202131510121/lyt/aiops_diagnosis

PY=/202131510121/conda/envs/micro/bin/python
LOG_DIR=outputs_experiment_chain_monitored
LOG="$LOG_DIR/postprocess_chain.log"
mkdir -p "$LOG_DIR"

log() { echo "$(date -Is) [postproc] $*" | tee -a "$LOG"; }

B_DIR=outputs_experiment_b_incident_llm
C_DIR=outputs_experiment_c_full_llm
C_BASE=outputs_experiment_c_base
VLLM_URL=http://127.0.0.1:8000
VLLM_MODEL=deepseek-r1-14b

log "===== POSTPROCESS CHAIN START ====="

# ---------------------------------------------------------------- 1) B merge
log "--- [1/3] B 后处理合并 ---"
"$PY" -u merge_predictions_llm.py "$B_DIR/predictions_high_conf.jsonl" \
    --gap-minutes 20 --max-span-minutes 60 --workers 8 \
    --max-tokens 0 --llm-timeout 1800 \
    --out "$B_DIR/predictions_high_conf.merged.jsonl" \
    > "$B_DIR/merge_llm.log" 2>&1
b_rc=$?
log "--- [1/3] B 合并 rc=$b_rc ---"
tail -6 "$B_DIR/merge_llm.log" | sed 's/^/    /' | tee -a "$LOG"

# ---------------------------------------------------------------- 2) C llm
log "--- [2/3] C LLM 阶段重跑（上次被平台回收打断，产物作废）---"
mkdir -p "$C_DIR"
"$PY" -u run_experiment_c_dependency_llm.py \
    --workspace /202131510121/lyt/workspace \
    --artifacts-dir "$C_BASE" \
    --output-dir "$C_DIR" \
    --llm-base-url "$VLLM_URL" --llm-model "$VLLM_MODEL" \
    --threshold 0.5 --workers 8 --min-llm-ratio 0.5 \
    > "$C_DIR/run.log" 2>&1
c_rc=$?
log "--- [2/3] C LLM rc=$c_rc ---"
if [ "$c_rc" -ne 0 ]; then
    log "C 的 LLM 阶段判定为 INVALID 或失败，跳过 C 的后处理合并"
    grep -E "INVALID|LLM answered" "$C_DIR/run.log" | tail -3 | sed 's/^/    /' | tee -a "$LOG"
    log "===== POSTPROCESS CHAIN ENDED WITH FAILURE ====="
    exit 2
fi

# ---------------------------------------------------------------- 3) C merge
log "--- [3/3] C 后处理合并 ---"
"$PY" -u merge_predictions_llm.py "$C_DIR/predictions_high_conf.jsonl" \
    --gap-minutes 20 --max-span-minutes 60 --workers 8 \
    --max-tokens 0 --llm-timeout 1800 \
    --out "$C_DIR/predictions_high_conf.merged.jsonl" \
    > "$C_DIR/merge_llm.log" 2>&1
c_merge_rc=$?
log "--- [3/3] C 合并 rc=$c_merge_rc ---"
tail -6 "$C_DIR/merge_llm.log" | sed 's/^/    /' | tee -a "$LOG"

log "===== POSTPROCESS CHAIN COMPLETE (b=$b_rc c_llm=$c_rc c_merge=$c_merge_rc) ====="
