#!/bin/bash
# Monitored B -> C -> D -> E experiment chain.
#
# Requirements this implements (see /202131510121/lyt/DSH连接工作文档.md):
#
#   最高优先级规则: every experiment runs its OWN first stage.  No experiment
#   reads another experiment's artifacts.
#
#   A (already completed, off by default)
#       IF/VAE + Episode/Incident -> GNN + RCA-GNN -> incident-level LLM
#
#   B   IF/VAE + Episode/Incident -> LLM Teacher + RCA-GNN Student
#                                  + Task A / Task B          -> incident-level LLM
#
#   C   IF/VAE + Episode/Incident -> NO GNN / RCA-GNN
#       -> unsupervised results + topology dependencies handed to the LLM
#
#   D   TWO unsupervised stages back to back, then the GNN, then the LLM:
#         IF + VAE
#           -> second VAE, loss weight = max(0, 1 - alpha * first_anomaly)
#              (anomalous points down-weighted so the model learns normal shape)
#           -> GNN over the point graph
#           -> incident-level LLM verification
#
#   E   as D, but the second VAE hard-excludes first-stage anomalies:
#         weight = 1 if first_anomaly <= threshold else 0
#
# D/E's second stage runs INSIDE the pipeline, before the GNN, so its score
# feeds the GNN as a feature and as the regression target.  It is not a
# post-processing step over finished artifacts.
#
# Each stage runs in the background while a monitor loop writes a heartbeat
# (elapsed, CPU/RSS, per-region progress, vLLM health, last log line) every
# MONITOR_INTERVAL seconds.  When a stage exits the next one starts.
#
# Environment overrides:
#   EXPERIMENTS=b,c,d,e     which experiments to run (a is already done)
#   MONITOR_INTERVAL=60     seconds between heartbeats
#   WORKERS=8               LLM concurrency
#   CONTINUE_ON_ERROR=0     1 = keep going after a failed stage
#   RESUME=0                1 = skip stages whose .done marker exists
#   CHAIN_DIR=outputs_experiment_chain_monitored
#   VAE_EPOCHS=3 GNN_EPOCHS=5 RCA_GNN_EPOCHS=40 SECOND_STAGE_EPOCHS=20
#   NETFLOW_ROWS=0          0 = topology scans the whole netflow capture
#   DE_PIPELINE_MODE=rca_gnn  D/E pipeline mode (rca_gnn = GNN + RCA-GNN,
#                             incident = point GNN only)
#
# Usage:
#   setsid nohup ./run_chain_monitored.sh > /dev/null 2>&1 < /dev/null &
#   tail -f outputs_experiment_chain_monitored/chain.log

set -uo pipefail

cd /202131510121/lyt/aiops_diagnosis

CHAIN_DIR="${CHAIN_DIR:-outputs_experiment_chain_monitored}"
EXPERIMENTS="${EXPERIMENTS:-b,c,d,e}"
MONITOR_INTERVAL="${MONITOR_INTERVAL:-60}"
WORKERS="${WORKERS:-8}"
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

log() {
    # The whole main block is redirected into $CHAIN_LOG, so a plain echo is
    # enough.  Writing to the file here as well duplicated every line.
    echo "$(date -Is) [chain] $*"
}

wanted() {
    case ",$EXPERIMENTS," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

vllm_up() {
    "$PY" - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen('http://127.0.0.1:8000/v1/models', timeout=3).read()
PY
}

ensure_vllm() {
    if vllm_up; then
        log "vLLM already serving at $VLLM_URL"
        return 0
    fi
    log "vLLM not responding; starting it"
    nohup "$PY_VLLM" -m vllm.entrypoints.openai.api_server \
        --model "$VLLM_MODEL_PATH" \
        --served-model-name "$VLLM_MODEL" \
        --host 0.0.0.0 --port 8000 \
        --dtype bfloat16 --max-model-len 8192 \
        --gpu-memory-utilization 0.45 \
        --enable-prefix-caching --enforce-eager --disable-log-requests \
        --enable-auto-tool-choice --tool-call-parser hermes \
        > "$CHAIN_DIR/vllm.log" 2>&1 &
    for _ in $(seq 1 120); do
        sleep 5
        if vllm_up; then log "vLLM is up"; return 0; fi
    done
    log "ERROR: vLLM failed to start; see $CHAIN_DIR/vllm.log"
    return 1
}

progress() {
    local dir="$1" pat="$2"
    if [ ! -d "$dir" ]; then echo -n "progress=no-output-yet"; return; fi
    local n
    n=$(find "$dir" -maxdepth 3 -name "$pat" 2>/dev/null | wc -l)
    echo -n "progress=${n}/${N_REGIONS} ${pat}"
}

# run_and_monitor <name> <logfile> <progress_dir> <progress_pattern> <cmd...>
run_and_monitor() {
    local name="$1" logfile="$2" pdir="$3" ppat="$4"
    shift 4
    local mon="$CHAIN_DIR/${name}.monitor.log"
    local marker="$CHAIN_DIR/${name}.done"

    if [ "$RESUME" = "1" ] && [ -f "$marker" ]; then
        log "SKIP $name (marker exists)"
        return 0
    fi

    log "START $name"
    mkdir -p "$(dirname "$logfile")"
    {
        echo "===== $name START $(date -Is) ====="
        echo "cmd: $*"
        echo "log: $logfile"
    } >> "$mon"

    "$@" > "$logfile" 2>&1 &
    local pid=$!
    local start
    start=$(date +%s)

    while kill -0 "$pid" 2>/dev/null; do
        sleep "$MONITOR_INTERVAL"
        kill -0 "$pid" 2>/dev/null || break
        local now elapsed stat prog health last
        now=$(date +%s); elapsed=$(( now - start ))
        stat=$(ps -o pcpu=,rss=,stat=,etime= -p "$pid" 2>/dev/null | tr -s ' ' | sed 's/^ //')
        prog=$(progress "$pdir" "$ppat")
        if vllm_up; then health="vllm=up"; else health="vllm=DOWN"; fi
        last=$(tail -n 1 "$logfile" 2>/dev/null | tr -d '\r' | cut -c1-170)
        {
            echo "$(date -Is) elapsed=${elapsed}s pid=${pid} cpu/rss/stat=${stat:-gone} ${prog} ${health}"
            echo "        last: ${last}"
        } >> "$mon"
    done

    wait "$pid"
    local rc=$?
    local dur=$(( $(date +%s) - start ))
    {
        echo "===== $name END rc=${rc} duration=${dur}s $(date -Is) ====="
        echo "final: $(progress "$pdir" "$ppat")"
        echo "last log line: $(tail -n 1 "$logfile" 2>/dev/null | cut -c1-200)"
    } >> "$mon"

    if [ "$rc" -eq 0 ]; then
        log "DONE $name rc=0 in ${dur}s"
        touch "$marker"
    else
        log "FAIL $name rc=${rc} in ${dur}s (see $logfile)"
    fi
    return "$rc"
}

after() {
    local rc=$1 name=$2
    if [ "$rc" -eq 0 ]; then return 0; fi
    if [ "$CONTINUE_ON_ERROR" = "1" ]; then
        log "CONTINUE_ON_ERROR=1, carrying on after $name failure"
        return 0
    fi
    log "stopping chain: $name failed"
    return 1
}

# Incident-level LLM verification shared by A/B/D/E.
incident_llm() {
    local artifacts="$1" outdir="$2"
    "$PY" run_experiment_a_incident_llm.py \
        --workspace "$WS" \
        --artifacts-dir "$artifacts" \
        --output-dir "$outdir" \
        --llm-base-url "$VLLM_URL" --llm-model "$VLLM_MODEL" \
        --threshold 0.5 --merge-gap-minutes 5 \
        --workers "$WORKERS" --llm-max-new-tokens 256
}

# ---------------------------------------------------------------- main
{
    log "===== MONITORED CHAIN START $(date -Is) ====="
    log "experiments=$EXPERIMENTS chain_dir=$CHAIN_DIR interval=${MONITOR_INTERVAL}s workers=$WORKERS"
    log "epochs: vae=$VAE_EPOCHS gnn=$GNN_EPOCHS rca_gnn=$RCA_GNN_EPOCHS second_stage=$SECOND_STAGE_EPOCHS netflow_rows=$NETFLOW_ROWS"
    log "D/E pipeline mode=$DE_PIPELINE_MODE"

    # ============================ A (optional) ============================
    if wanted a; then
        ensure_vllm || exit 1
        run_and_monitor a_base \
            "$CHAIN_DIR/a_base.log" \
            outputs_experiment_a_full "incident_candidates.json" \
            "$PY" run_pipeline.py --workspace "$WS" \
                --output-dir outputs_experiment_a_full --experiment rca_gnn --use-gnn \
                --vae-epochs "$VAE_EPOCHS" --gnn-epochs "$GNN_EPOCHS" \
                --rca-gnn-epochs "$RCA_GNN_EPOCHS" --max-netflow-rows "$NETFLOW_ROWS" \
                --llm-workers "$WORKERS" \
                --llm-backend none
        after $? a_base || exit 1

        run_and_monitor a_llm \
            "outputs_experiment_a_incident_llm/run.log" \
            outputs_experiment_a_incident_llm "predictions_high_conf.jsonl" \
            incident_llm outputs_experiment_a_full outputs_experiment_a_incident_llm
        after $? a_llm || exit 1
    fi

    # ============================ B ============================
    # LLM Teacher + RCA-GNN Student + Task A / Task B, incident-level output.
    if wanted b; then
        ensure_vllm || exit 1

        run_and_monitor b_base \
            "$CHAIN_DIR/b_base.log" \
            outputs_experiment_b_full/_ablation/full "incident_candidates.json" \
            "$PY" run_ablation.py \
                --workspace "$WS" \
                --output-dir outputs_experiment_b_full \
                --experiments full \
                --vae-epochs "$VAE_EPOCHS" \
                --gnn-epochs "$GNN_EPOCHS" \
                --rca-gnn-epochs "$RCA_GNN_EPOCHS" \
                --max-netflow-rows "$NETFLOW_ROWS" \
                --llm-workers "$WORKERS" \
                --llm-backend openai_compatible \
                --llm-base-url "$VLLM_URL" \
                --llm-model "$VLLM_MODEL"
        after $? b_base || exit 1

        run_and_monitor b_llm \
            "outputs_experiment_b_incident_llm/run.log" \
            outputs_experiment_b_incident_llm "predictions_high_conf.jsonl" \
            incident_llm outputs_experiment_b_full/_ablation/full outputs_experiment_b_incident_llm
        after $? b_llm || exit 1
    fi

    # ============================ C ============================
    # Own IF/VAE + Episode/Incident, NO GNN / RCA-GNN.
    if wanted c; then
        run_and_monitor c_base \
            "$CHAIN_DIR/c_base.log" \
            outputs_experiment_c_base "incident_candidates.json" \
            "$PY" run_pipeline.py --workspace "$WS" \
                --output-dir outputs_experiment_c_base --experiment incident --no-gnn \
                --vae-epochs "$VAE_EPOCHS" --gnn-epochs "$GNN_EPOCHS" \
                --llm-workers "$WORKERS" \
                --max-netflow-rows "$NETFLOW_ROWS" \
                --llm-backend none
        after $? c_base || exit 1

        ensure_vllm || exit 1
        run_and_monitor c_llm \
            "outputs_experiment_c_full_llm/run.log" \
            outputs_experiment_c_full_llm "predictions_high_conf.jsonl" \
            "$PY" run_experiment_c_dependency_llm.py \
                --workspace "$WS" \
                --artifacts-dir outputs_experiment_c_base \
                --output-dir outputs_experiment_c_full_llm \
                --llm-base-url "$VLLM_URL" --llm-model "$VLLM_MODEL" \
                --threshold 0.5 --workers "$WORKERS"
        after $? c_llm || exit 1
    fi

    # ============================ D ============================
    # TWO unsupervised stages, THEN the GNN, then the LLM.
    if wanted d; then
        run_and_monitor d_base \
            "$CHAIN_DIR/d_base.log" \
            outputs_experiment_d_base "incident_candidates.json" \
            "$PY" run_pipeline.py --workspace "$WS" \
                --output-dir outputs_experiment_d_base --experiment "$DE_PIPELINE_MODE" --use-gnn \
                --second-stage-mode weighted --second-stage-alpha 1.0 \
                --second-stage-epochs "$SECOND_STAGE_EPOCHS" \
                --vae-epochs "$VAE_EPOCHS" --gnn-epochs "$GNN_EPOCHS" \
                --rca-gnn-epochs "$RCA_GNN_EPOCHS" --max-netflow-rows "$NETFLOW_ROWS" \
                --llm-workers "$WORKERS" \
                --llm-backend none
        after $? d_base || exit 1

        ensure_vllm || exit 1
        run_and_monitor d_llm \
            "outputs_experiment_d_llm/run.log" \
            outputs_experiment_d_llm "predictions_high_conf.jsonl" \
            incident_llm outputs_experiment_d_base outputs_experiment_d_llm
        after $? d_llm || exit 1
    fi

    # ============================ E ============================
    # As D, but the second stage hard-excludes first-stage anomalies.
    if wanted e; then
        run_and_monitor e_base \
            "$CHAIN_DIR/e_base.log" \
            outputs_experiment_e_base "incident_candidates.json" \
            "$PY" run_pipeline.py --workspace "$WS" \
                --output-dir outputs_experiment_e_base --experiment "$DE_PIPELINE_MODE" --use-gnn \
                --second-stage-mode threshold --second-stage-threshold 0.7 \
                --second-stage-epochs "$SECOND_STAGE_EPOCHS" \
                --vae-epochs "$VAE_EPOCHS" --gnn-epochs "$GNN_EPOCHS" \
                --rca-gnn-epochs "$RCA_GNN_EPOCHS" --max-netflow-rows "$NETFLOW_ROWS" \
                --llm-workers "$WORKERS" \
                --llm-backend none
        after $? e_base || exit 1

        ensure_vllm || exit 1
        run_and_monitor e_llm \
            "outputs_experiment_e_llm/run.log" \
            outputs_experiment_e_llm "predictions_high_conf.jsonl" \
            incident_llm outputs_experiment_e_base outputs_experiment_e_llm
        after $? e_llm || exit 1
    fi

    log "===== MONITORED CHAIN COMPLETE $(date -Is) ====="
} >> "$CHAIN_LOG" 2>&1
