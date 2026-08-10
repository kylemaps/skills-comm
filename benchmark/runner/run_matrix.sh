#!/bin/bash
# run_matrix.sh TASK REPEATS MODEL [MODEL...]
#
# Runs preflight, then task x {env-only, env+skill} x repeats for each model.
# Designed to be launched detached:
#
#   nohup ~/skills-comm/benchmark/runner/run_matrix.sh \
#       structural-brain-extraction-7t 3 neurodesk/minimax-m2 neurodesk/kimi-k3 \
#       < /dev/null > ~/bench/matrix.log 2>&1 &
#   disown
#
# Runs sequentially on purpose: skills live in a container-global path, so two
# arms cannot run concurrently without contaminating each other.
set -u

if [ "$#" -lt 3 ] || [ "$1" = "--help" ]; then
  echo "usage: run_matrix.sh TASK REPEATS MODEL [MODEL...]"
  echo "example: run_matrix.sh structural-brain-extraction-7t 3 neurodesk/minimax-m2"
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASK="$1"; shift
N="$1"; shift
MODELS=("$@")

"$HERE/preflight.sh" "${MODELS[@]}" || { echo "ABORT: preflight failed"; exit 1; }

TOTAL=$(( ${#MODELS[@]} * 2 * N ))
echo "=== MATRIX START $(date -u +%FT%TZ) — $TOTAL runs ==="
i=0
for M in "${MODELS[@]}"; do
  for COND in env-only env+skill; do
    for r in $(seq 1 "$N"); do
      i=$((i+1))
      echo "--- [$i/$TOTAL] ---"
      "$HERE/run_bench.sh" "$TASK" "$M" "$COND" "$r"
    done
  done
done
echo "=== MATRIX DONE $(date -u +%FT%TZ) ==="
