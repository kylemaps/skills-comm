#!/bin/bash
# run_sweep.sh TASK REPEATS MODEL [MODEL...]
#
# One correct sweep: every model x both arms x N repeats.
#
# The arms run STRICTLY SEQUENTIALLY (all env-only, then all env+skill) because
# skills live in a container-global path -- two concurrent runs on different arms
# would silently contaminate each other. Within an arm, everything runs in
# parallel up to MAXPAR, since the bottleneck is LLM latency, not compute.
#
# A lock file prevents a second sweep from starting while one is in flight; that
# is exactly the mistake this script exists to make impossible.
#
#   nohup ~/skills-comm/benchmark/runner/run_sweep.sh \
#       structural-brain-extraction-7t 10 \
#       neurodesk/minimax-m2 neurodesk/qwen3.5-122b \
#       neurodesk/glm-5.2 neurodesk/kimi-k3 \
#       < /dev/null > ~/bench/sweep.log 2>&1 &
#   disown
#
# env:
#   MAXPAR=8        concurrent runs within an arm
#   SKIP_MISSING=1  (default) drop models the gateway no longer serves and run the
#                   rest. Set 0 to abort instead.
#
#                   Default is "continue" because of how this fails in practice: a
#                   whole overnight sweep once aborted because the gateway had
#                   retired one model generation. Losing 4 models' data to save the
#                   5th is the worse trade. Dropped models are logged here, recorded
#                   in sweep_manifest.json, and visible in the final report as cells
#                   with no data -- there is no way to lose track of them.
set -u

if [ "$#" -lt 3 ] || [ "$1" = "--help" ]; then
  echo "usage: run_sweep.sh TASK REPEATS MODEL [MODEL...]"
  echo "env: MAXPAR=8  SKIP_MISSING=1"
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"
MAXPAR="${MAXPAR:-8}"
SKIP_MISSING="${SKIP_MISSING:-1}"
LOCK="$BENCH_HOME/.sweep.lock"

TASK="$1"; shift
N="$1"; shift
REQUESTED=("$@")

mkdir -p "$BENCH_HOME"
if [ -e "$LOCK" ]; then
  echo "ABORT: a sweep is already running (pid $(cat "$LOCK" 2>/dev/null))."
  echo "       Two sweeps would contaminate each other's arms."
  echo "       If that pid is dead: rm $LOCK"
  exit 1
fi

# A run_bench.sh that outlived a previous pkill will keep writing into the runs
# directory and show up in the results as a phantom cell from the old batch. We
# have already been bitten by exactly this.
STRAY=$(pgrep -f "run_bench.sh" | grep -v "^$$\$" | paste -sd' ' -)
if [ -n "$STRAY" ]; then
  echo "ABORT: run_bench.sh already running (pids: $STRAY)."
  echo "       Those runs would land in this sweep's results."
  echo "       Stop them first:  pkill -f run_bench.sh; pkill -f 'opencode run'"
  exit 1
fi

echo $$ > "$LOCK"
cleanup() { rm -f "$LOCK"; }
trap cleanup EXIT INT TERM

# --- preflight: environment fatal, missing models negotiable -----------------
RESOLVED="$BENCH_HOME/.resolved_models"
RESOLVED_OUT="$RESOLVED" "$HERE/preflight.sh" "${REQUESTED[@]}"
PF=$?
if [ "$PF" = 1 ]; then
  echo "ABORT: preflight failed (environment, not models)."
  exit 1
fi

MODELS=()
while IFS= read -r line; do [ -n "$line" ] && MODELS+=("$line"); done < "$RESOLVED"
DROPPED=()
for m in "${REQUESTED[@]}"; do
  printf '%s\n' "${MODELS[@]}" | grep -qx "$m" || DROPPED+=("$m")
done

if [ "${#DROPPED[@]}" -gt 0 ]; then
  if [ "$SKIP_MISSING" != 1 ]; then
    echo "ABORT: ${#DROPPED[@]} model(s) unavailable and SKIP_MISSING=0: ${DROPPED[*]}"
    exit 1
  fi
  echo "!! DROPPED ${#DROPPED[@]} unavailable model(s): ${DROPPED[*]}"
  echo "!! Continuing with ${#MODELS[@]}: ${MODELS[*]}"
fi

TOTAL=$(( ${#MODELS[@]} * 2 * N ))
START=$(date -u +%FT%TZ)

# The manifest is the answer to "what did last night actually attempt?" -- it
# survives even if the log is truncated or the sweep is killed mid-flight.
{
  printf '{"task":"%s","repeats":%s,"maxpar":%s,"start":"%s",' "$TASK" "$N" "$MAXPAR" "$START"
  printf '"requested":["%s"],' "$(printf '%s' "${REQUESTED[*]}" | sed 's/ /","/g')"
  printf '"models":["%s"],' "$(printf '%s' "${MODELS[*]}" | sed 's/ /","/g')"
  if [ "${#DROPPED[@]}" -gt 0 ]; then
    printf '"dropped":["%s"],' "$(printf '%s' "${DROPPED[*]}" | sed 's/ /","/g')"
  else
    printf '"dropped":[],'
  fi
  printf '"planned_runs":%s}\n' "$TOTAL"
} > "$BENCH_HOME/sweep_manifest.json"

echo "=== SWEEP START $START — $TOTAL runs, MAXPAR=$MAXPAR ==="
echo "    task:    $TASK"
echo "    models:  ${MODELS[*]}"
echo "    repeats: $N"

# `wait -n` needs bash >= 4.3. Without it we must fall back to a full barrier;
# blindly assuming it exists would decrement the counter without waiting and let
# parallelism run away.
if [ "${BASH_VERSINFO[0]}" -gt 4 ] || \
   { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 3 ]; }; then
  HAVE_WAIT_N=1
else
  HAVE_WAIT_N=0
  echo "    note: bash ${BASH_VERSION} has no 'wait -n'; throttling in full batches"
fi

for COND in env-only env+skill; do
  echo "=== ARM: $COND  ($(date -u +%FT%TZ)) ==="
  running=0
  for M in "${MODELS[@]}"; do
    for r in $(seq 1 "$N"); do
      "$HERE/run_bench.sh" "$TASK" "$M" "$COND" "$r" &
      running=$((running + 1))
      if [ "$running" -ge "$MAXPAR" ]; then
        if [ "$HAVE_WAIT_N" = 1 ]; then
          # `|| true` matters: wait -n returns the finished job's exit status, so a
          # single failed run would otherwise fall through to a full barrier and
          # quietly serialise the rest of the arm.
          wait -n 2>/dev/null || true
          running=$((running - 1))
        else
          wait; running=0
        fi
      fi
    done
  done
  wait                      # arm barrier: never overlap arms
  echo "=== ARM $COND COMPLETE $(date -u +%FT%TZ) ==="
done

DONE=$(ls -d "$BENCH_HOME"/runs/"$TASK"__*/ 2>/dev/null | wc -l)
echo "=== SWEEP DONE $(date -u +%FT%TZ) — $DONE/$TOTAL run dirs present ==="
[ "${#DROPPED[@]}" -gt 0 ] && echo "    (dropped models: ${DROPPED[*]})"
echo "next: $HERE/collect_results.sh $TASK"
