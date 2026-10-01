#!/bin/bash
# Experiment F -- all stages after the base run, in spec order.
#
#   Usage:  bash run_f_pipeline.sh [output_dir] [regions]
#
# Each stage is a separate process with its own log, so a failure is isolated
# and re-runnable (the stage list below is idempotent: it overwrites its own
# artifacts and never touches another stage's).  The base stage is deliberately
# NOT included -- it is run on its own so its wall-clock cost stays measurable.
#
# Spec 5.9's ordering constraint is respected: evidence -> flow/predictive
# (evidence producers) -> candidates (the only consumer) -> the two measurement
# stages.  prompt_ablation is also excluded: it needs the vLLM server and takes
# ~13 minutes per region, so it is run deliberately rather than as part of a
# batch.
set -u

cd /202531630503/lyt/aiops_diagnosis || exit 1
WS=/202531630503/lyt/workspace
OUT=${1:-outputs_experiment_f_full}
REGIONS=${2:-all}
LOGDIR=/202531630503/lyt/aiops_diagnosis/f_logs
mkdir -p "$LOGDIR"

NETFLOW_ROWS=${NETFLOW_ROWS:-14000000}

run_stage() {
  local stage=$1
  shift
  echo "[$(date +%H:%M:%S)] ===== stage: $stage ====="
  python3 run_experiment_f_evidence_agent.py \
      --workspace "$WS" \
      --regions "$REGIONS" \
      --output-dir "$OUT" \
      --stage "$stage" \
      --torch-threads 1 \
      "$@" > "$LOGDIR/$stage.log" 2>&1
  local rc=$?
  echo "[$(date +%H:%M:%S)] stage $stage -> rc=$rc   (log: $LOGDIR/$stage.log)"
  # Surface the stage's own summary line so a batch run is readable at a glance.
  grep -aE "^\[(FE|FF|FP|FC|F1)\]" "$LOGDIR/$stage.log" | tail -3
  return $rc
}

run_stage evidence || { echo "!! evidence failed"; exit 1; }
run_stage flow --max-netflow-rows "$NETFLOW_ROWS" || { echo "!! flow failed"; exit 1; }
run_stage predictive --max-lag-minutes 5 || { echo "!! predictive failed"; exit 1; }
run_stage candidates || { echo "!! candidates failed"; exit 1; }
run_stage f1_stability --n-repeats 20 --drop-fraction 0.05 || { echo "!! f1_stability failed"; exit 1; }

echo "[$(date +%H:%M:%S)] ALL_STAGES_DONE -> $OUT"
