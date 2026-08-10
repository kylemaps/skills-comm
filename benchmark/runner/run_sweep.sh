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
#       neurodesk/minimax-m2 neurodesk/qwen3 neurodesk/qwen3.5-122b \
#       neurodesk/glm-5.2 neurodesk/kimi-k3 \
#       < /dev/null > ~/bench/sweep.log 2>&1 &
#   disown
set -u

if [ "$#" -lt 3 ] || [ "$1" = "--help" ]; then
  echo "usage: run_sweep.sh TASK REPEATS MODEL [MODEL...]"
  echo "env: MAXPAR=8  (concurrent runs within an arm)"
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"
MAXPAR="${MAXPAR:-8}"
LOCK="$BENCH_HOME/.sweep.lock"

TASK="$1"; shift
N="$1"; shift
MODELS=("$@")

mkdir -p "$BENCH_HOME"
if [ -e "$LOCK" ]; then
  echo "ABORT: a sweep is already running (pid $(cat "$LOCK" 2>/dev/null))."
  echo "       Two sweeps would contaminate each other's arms."
  echo "       If that pid is dead: rm $LOCK"
  exit 1
fi
echo $$ > "$LOCK"
cleanup() { rm -f "$LOCK"; }
trap cleanup EXIT INT TERM

"$HERE/preflight.sh" "${MODELS[@]}" || { echo "ABORT: preflight failed"; exit 1; }

TOTAL=$(( ${#MODELS[@]} * 2 * N ))
echo "=== SWEEP START $(date -u +%FT%TZ) — $TOTAL runs, MAXPAR=$MAXPAR ==="
echo "    task:   $TASK"
echo "    models: ${MODELS[*]}"
echo "    repeats: $N"

for COND in env-only env+skill; do
  echo "=== ARM: $COND  ($(date -u +%FT%TZ)) ==="
  running=0
  for M in "${MODELS[@]}"; do
    for r in $(seq 1 "$N"); do
      "$HERE/run_bench.sh" "$TASK" "$M" "$COND" "$r" &
      running=$((running + 1))
      if [ "$running" -ge "$MAXPAR" ]; then
        wait -n 2>/dev/null || wait
        running=$((running - 1))
      fi
    done
  done
  wait                      # arm barrier: never overlap arms
  echo "=== ARM $COND COMPLETE $(date -u +%FT%TZ) ==="
done

echo "=== SWEEP DONE $(date -u +%FT%TZ) ==="
echo "next: $HERE/collect_results.sh $TASK"
