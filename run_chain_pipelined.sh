#!/bin/bash
# Two-slot experiment chain: CPU-bound stages and vLLM-bound stages overlapped.
#
# Why this exists
# ---------------
# The previous chain ran strictly B -> C -> D -> E, each as [base] then [llm].
# Base stages are CPU-bound and never touch the LLM backend (measured: c_base
# 1h42m, d_base 4h32m), while LLM stages spend most of their wall time waiting on
# vLLM.  Run sequentially, vLLM sits idle for ~14 hours of the run and the CPU
# sits mostly idle during the other ~13.
#
# This script runs two slots in parallel:
#
#   base slot (CPU)   b_base -> c_base -> d_base -> e_base          (sequential)
#   llm  slot (GPU)   ... waits for each base, then runs that
#                     experiment's LLM stage and its post-processing merge
#
# so experiment N's LLM work overlaps experiment N+1's base work.  Expected wall
# time drops from sum(base+llm) ~ 27h to roughly max(sum(base), sum(llm)) + one
# base ~ 17h.
#
# The LLM slot waits on a per-experiment `.base_done` marker, so an experiment's
# LLM stage never starts before its own artifacts exist - the "each experiment
# runs its own first stage" rule still holds.
#
# Environment overrides:
#   EXPERIMENTS=b,c,d,e     experiments to run, in base order
#   SKIP_BASE=            comma list whose base is skipped (marker pre-created);
#                         use when the base artifacts already exist
#   MONITOR_INTERVAL=60   seconds between heartbeats
#   WORKERS=8             concurrency inside base stages AND the merge
#   LLM_WORKERS=8         concurrency for the incident-level LLM stage
#   POST_MERGE=1          run merge_predictions_llm.py after each LLM stage
#   CONTINUE_ON_ERROR=0   1 = keep going after a failed stage
#   RESUME=0              1 = skip stages whose .done marker exists
#   CHAIN_DIR=outputs_experiment_chain_pipelined
#   VAE_EPOCHS / GNN_EPOCHS / RCA_GNN_EPOCHS / SECOND_STAGE_EPOCHS / NETFLOW_ROWS
#   DE_PIPELINE_MODE=rca_gnn
#
# Usage:
#   setsid nohup ./run_chain_pipelined.sh > /dev/null 2>&1 < /dev/null &
#   tail -f outputs_experiment_chain_pipelined/chain.log

set -uo pipefail

cd /202131510121/lyt/aiops_diagnosis

CHAIN_DIR="${CHAIN_DIR:-outputs_experiment_chain_pipelined}"
EXPERIMENTS="${EXPERIMENTS:-b,c,d,e}"
SKIP_BASE="${SKIP_BASE:-}"
MONITOR_INTERVAL="${MONITOR_INTERVAL:-60}"
WORKERS="${WORKERS:-8}"
LLM_WORKERS="${LLM_WORKERS:-8}"
POST_MERGE="${POST_MERGE:-1}"
CONTINUE_ON_ERROR="${CONTINUE_ON_ERROR:-0}"
RESUME="${RESUME:-0}"

VAE_EPOCHS="${VAE_EPOCHS:-3}"
GNN_EPOCHS="${GNN_EPOCHS:-5}"
RCA_GNN_EPOCHS="${RCA_GNN_EPOCHS:-40}"
SECOND_STAGE_EPOCHS="${SECOND_STAGE_EPOCHS:-20}"
NETFLOW_ROWS="${NETFLOW_ROWS:-0}"
DE_PIPELINE_MODE="${DE_PIPELINE_MODE:-rca_gnn}"

PY=/202131510121/conda/envs/micro/bin/python
PY_VLLM=/202131510121/conda/envs/vllm_cu124_sys/bin/python
VLLM_URL=http://127.0.0.1:8000
VLLM_MODEL=deepseek-r1-14b
VLLM_MODEL_PATH=/202131510121/lyt/models/models/deepseek-ai--DeepSeek-R1-Distill-Qwen-14B/snapshots/master
WS=/202131510121/lyt/workspace
N_REGIONS=8

mkdir -p "$CHAIN_DIR"
CHAIN_LOG="$CHAIN_DIR/chain.log"

log() { echo "$(date -Is) [pipeline] $*"; }

wanted() { case ",$EXPERIMENTS," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
skip_base() { case ",$SKIP_BASE," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

vllm_up() {
    "$PY" - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8000/v1/models', timeout=3).read()
PY
}

ensure_vllm() {
    if vllm_up; then log "vLLM already serving"; return 0; fi
    log "vLLM not responding; starting it"
    # Append, not truncate: overwriting the log destroyed the evidence for the
    # experiment-C failure on 2026-09-20.
    nohup "$PY_VLLM" -m vllm.entrypoints.openai.api_server \
        --model "$VLLM_MODEL_PATH" --served-model-name "$VLLM_MODEL" \
        --host 0.0.0.0 --port 8000 \
        --dtype bfloat16 --max-model-len 8192 \
        --gpu-memory-utilization 0.45 \
        --enable-prefix-caching --enforce-eager --disable-log-requests \
        --enable-auto-tool-choice --tool-call-parser hermes \
        >> "$CHAIN_DIR/vllm-$(date +%Y%m%d).log" 2>&1 &
    for _ in $(seq 1 120); do
        sleep 5
        if vllm_up; then log "vLLM is up"; return 0; fi
    done
    log "ERROR: vLLM failed to start"
    return 1
}

progress() {
    local dir="$1" pat="$2"
    if [ ! -d "$dir" ]; then echo -n "progress=no-output-yet"; return; fi
    local n; n=$(find "$dir" -maxdepth 3 -name "$pat" 2>/dev/null | wc -l)
    echo -n "progress=${n}/${N_REGIONS} ${pat}"
}

# run_and_monitor <name> <logfile> <progress_dir> <pattern> <cmd...>
run_and_monitor() {
    local name="$1" logfile="$2" pdir="$3" ppat="$4"
    shift 4
    local mon="$CHAIN_DIR/${name}.monitor.log"
    local marker="$CHAIN_DIR/${name}.done"

    if [ "$RESUME" = "1" ] && [ -f "$marker" ]; then log "SKIP $name (marker exists)"; return 0; fi

    log "START $name"
    mkdir -p "$(dirname "$logfile")"
    { echo "===== $name START $(date -Is) ====="; echo "cmd: $*"; } >> "$mon"

    "$@" > "$logfile" 2>&1 &
    local pid=$!
    local start; start=$(date +%s)

    while kill -0 "$pid" 2>/dev/null; do
        sleep "$MONITOR_INTERVAL"
        kill -0 "$pid" 2>/dev/null || break
        local elapsed stat prog health last
        elapsed=$(( $(date +%s) - start ))
        stat=$(ps -o pcpu=,rss=,stat=,etime= -p "$pid" 2>/dev/null | tr -s ' ' | sed 's/^ //')
        prog=$(progress "$pdir" "$ppat")
        if vllm_up; then health="vllm=up"; else health="vllm=DOWN"; fi
        last=$(tail -n 1 "$logfile" 2>/dev/null | tr -d '\r' | cut -c1-150)
        {
            echo "$(date -Is) elapsed=${elapsed}s pid=${pid} cpu/rss/stat=${stat:-gone} ${prog} ${health}"
            echo "        last: ${last}"
        } >> "$mon"
    done

    wait "$pid"
    local rc=$? dur=$(( $(date +%s) - start ))
    log "$([ $rc -eq 0 ] && echo DONE || echo FAIL) $name rc=$rc in ${dur}s"
    if [ "$rc" -eq 0 ]; then touch "$marker"; else log "see $logfile"; fi
    return "$rc"
}

after() {
    local rc=$1 name=$2
    [ "$rc" -eq 0 ] && return 0
    if [ "$CONTINUE_ON_ERROR" = "1" ]; then log "CONTINUE_ON_ERROR=1 after $name"; return 0; fi
    log "stopping: $name failed"
    return 1
}

# --- stage definitions -------------------------------------------------

stage_base() {
    case "$1" in
        b) "$PY" run_ablation.py --workspace "$WS" --output-dir outputs_experiment_b_full \
               --experiments full --vae-epochs "$VAE_EPOCHS" --gnn-epochs "$GNN_EPOCHS" \
               --rca-gnn-epochs "$RCA_GNN_EPOCHS" --max-netflow-rows "$NETFLOW_ROWS" \
               --llm-workers "$WORKERS" \
               --llm-backend openai_compatible --llm-base-url "$VLLM_URL" --llm-model "$VLLM_MODEL" ;;
        c) "$PY" run_pipeline.py --workspace "$WS" --output-dir outputs_experiment_c_base \
               --experiment incident --no-gnn --vae-epochs "$VAE_EPOCHS" --gnn-epochs "$GNN_EPOCHS" \
               --llm-workers "$WORKERS" --max-netflow-rows "$NETFLOW_ROWS" --llm-backend none ;;
        d) "$PY" run_pipeline.py --workspace "$WS" --output-dir outputs_experiment_d_base \
               --experiment "$DE_PIPELINE_MODE" --use-gnn \
               --second-stage-mode weighted --second-stage-alpha 1.0 \
               --second-stage-epochs "$SECOND_STAGE_EPOCHS" --vae-epochs "$VAE_EPOCHS" \
               --gnn-epochs "$GNN_EPOCHS" --rca-gnn-epochs "$RCA_GNN_EPOCHS" \
               --llm-workers "$WORKERS" --max-netflow-rows "$NETFLOW_ROWS" --llm-backend none ;;
        e) "$PY" run_pipeline.py --workspace "$WS" --output-dir outputs_experiment_e_base \
               --experiment "$DE_PIPELINE_MODE" --use-gnn \
               --second-stage-mode threshold --second-stage-threshold 0.7 \
               --second-stage-epochs "$SECOND_STAGE_EPOCHS" --vae-epochs "$VAE_EPOCHS" \
               --gnn-epochs "$GNN_EPOCHS" --rca-gnn-epochs "$RCA_GNN_EPOCHS" \
               --llm-workers "$WORKERS" --max-netflow-rows "$NETFLOW_ROWS" --llm-backend none ;;
    esac
}

base_dir() {
    case "$1" in
        b) echo outputs_experiment_b_full/_ablation/full ;;
        *) echo "outputs_experiment_$1_base" ;;
    esac
}

stage_llm() {
    local exp="$1" artifacts="$2" outdir="$3"
    case "$exp" in
        c) "$PY" run_experiment_c_dependency_llm.py --workspace "$WS" \
               --artifacts-dir "$artifacts" --output-dir "$outdir" \
               --llm-base-url "$VLLM_URL" --llm-model "$VLLM_MODEL" \
               --threshold 0.5 --workers "$LLM_WORKERS" ;;
        *) "$PY" run_experiment_a_incident_llm.py --workspace "$WS" \
               --artifacts-dir "$artifacts" --output-dir "$outdir" \
               --llm-base-url "$VLLM_URL" --llm-model "$VLLM_MODEL" \
               --threshold 0.5 --merge-gap-minutes 5 \
               --workers "$LLM_WORKERS" --llm-max-new-tokens 256 ;;
    esac
}

stage_post_merge() {
    local outdir="$1"
    "$PY" merge_predictions_llm.py "$outdir/predictions_high_conf.jsonl" \
        --gap-minutes 20 --max-span-minutes 60 --workers "$LLM_WORKERS" \
        --max-tokens 1024 --llm-timeout 240 \
        --out "$outdir/predictions_high_conf.merged.jsonl"
}

# ---------------------------------------------------------------- main
{
    log "===== PIPELINED CHAIN START $(date -Is) ====="
    log "experiments=$EXPERIMENTS skip_base='$SKIP_BASE' post_merge=$POST_MERGE"
    log "workers=$WORKERS llm_workers=$LLM_WORKERS chain_dir=$CHAIN_DIR"

    # ---- base slot: CPU-bound, sequential ----
    (
        for exp in $(echo "$EXPERIMENTS" | tr ',' ' '); do
            if skip_base "$exp"; then
                log "base slot: skip $exp (SKIP_BASE)"
                touch "$CHAIN_DIR/base_${exp}.done"
                continue
            fi
            run_and_monitor "${exp}_base" "$CHAIN_DIR/${exp}_base.log" \
                "$(base_dir "$exp")" "incident_candidates.json" \
                stage_base "$exp"
            after $? "${exp}_base" || exit 1
            touch "$CHAIN_DIR/base_${exp}.done"
        done
        log "base slot finished"
    ) &
    BASE_PID=$!

    # ---- llm slot: vLLM-bound, waits per experiment on its base marker ----
    (
        for exp in $(echo "$EXPERIMENTS" | tr ',' ' '); do
            log "llm slot: waiting for base_${exp}.done"
            while [ ! -f "$CHAIN_DIR/base_${exp}.done" ]; do sleep 20; done
            ensure_vllm || exit 1

            out_dir="outputs_experiment_${exp}_llm"
            [ "$exp" = b ] && out_dir="outputs_experiment_b_incident_llm"
            [ "$exp" = c ] && out_dir="outputs_experiment_c_full_llm"

            run_and_monitor "${exp}_llm" "$out_dir/run.log" "$out_dir" \
                "predictions_high_conf.jsonl" \
                stage_llm "$exp" "$(base_dir "$exp")" "$out_dir"
            after $? "${exp}_llm" || exit 1

            if [ "$POST_MERGE" = "1" ]; then
                run_and_monitor "${exp}_merge" "$CHAIN_DIR/${exp}_merge.log" \
                    "$out_dir" "predictions_high_conf.merged.jsonl" \
                    stage_post_merge "$out_dir"
                after $? "${exp}_merge" || exit 1
            fi
        done
        log "llm slot finished"
    ) &
    LLM_PID=$!

    wait "$BASE_PID"; BASE_RC=$?
    wait "$LLM_PID";  LLM_RC=$?

    log "slots finished: base_rc=$BASE_RC llm_rc=$LLM_RC"
    if [ "$BASE_RC" -eq 0 ] && [ "$LLM_RC" -eq 0 ]; then
        log "===== PIPELINED CHAIN COMPLETE $(date -Is) ====="
    else
        log "===== PIPELINED CHAIN ENDED WITH FAILURES $(date -Is) ====="
    fi
} >> "$CHAIN_LOG" 2>&1
