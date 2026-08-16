#!/bin/bash
# retry_failed.sh TASK [PASSES]
#
# Re-run every run of TASK that failed for OUR reasons -- gateway outage,
# agent-runtime contention, a run that never made a model call -- until none are
# left or PASSES attempts are used up (default 3).
#
# run_sweep.sh does this automatically per arm now (RETRY_INFRA). This is the
# standalone version for filling gaps in sweeps that already finished, so a cell
# short of runs can be topped up without re-running the whole thing.
#
# Serial on purpose: concurrency causes most of these failures, so retrying in
# parallel would recreate the conditions that lost the runs.
set -u

TASK="${1:?usage: retry_failed.sh TASK [PASSES]}"
PASSES="${2:-3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"

for pass in $(seq 1 "$PASSES"); do
  mapfile -t FAILED < <(python "$HERE/find_failed.py" "$BENCH_HOME/runs" "$TASK")
  if [ "${#FAILED[@]}" -eq 0 ]; then
    echo "=== nothing left to retry (pass $pass) ==="
    break
  fi
  echo "=== PASS $pass/$PASSES: ${#FAILED[@]} run(s) to retry ==="
  for d in "${FAILED[@]}"; do
    b=$(basename "$d")
    M="neurodesk/$(echo "$b" | awk -F'__' '{print $2}' | sed 's/^neurodesk-//')"
    C=$(echo "$b" | awk -F'__' '{print $3}')
    r=$(echo "$b" | awk -F'__' '{print $4}' | tr -d 'r')
    echo "--- $b"
    # The arm label carries which skill to install; run_bench.sh reads SKILLS_SRC
    # from the environment, so export it before calling this for a labelled arm.
    "$HERE/run_bench.sh" "$TASK" "$M" "$C" "$r"
  done
done

LEFT=$(python "$HERE/find_failed.py" "$BENCH_HOME/runs" "$TASK" | wc -l)
echo "=== DONE: $LEFT run(s) still failing ==="
