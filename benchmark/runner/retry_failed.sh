#!/bin/bash
# retry_failed.sh TASK [PASSES]        ARM=<arm> to restrict to one arm
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

TASK="${1:?usage: retry_failed.sh TASK [PASSES]   (ARM=<arm> to restrict)}"
PASSES="${2:-3}"
ARM="${ARM:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"
SKILLSRC="${SKILLS_SRC:-$HOME/skills-comm/plugins/brain-extraction}"

FILTER=()
[ -n "$ARM" ] && FILTER=(--arm "$ARM")

# SKILLS_SRC is a single value applied to every retry, but which skill an arm is
# supposed to install is encoded in the arm NAME. Retrying `env+skill` and
# `env+skill-michele` in one invocation would therefore install one skill under
# both labels: the directory says ours, the content is hers. `skills_hash` would
# catch it downstream as "differs within an arm", but only after the runs are
# spent. Refuse instead, and say exactly what to run.
#
# env-only in the list is harmless -- run_bench.sh only symlinks for env+skill*.
mapfile -t SKILL_ARMS < <(python "$HERE/find_failed.py" "$BENCH_HOME/runs" "$TASK" "${FILTER[@]}" \
  | sed 's|.*/||' | awk -F'__' '$3 ~ /^env\+skill/ {print $3}' | sort -u)
if [ "${#SKILL_ARMS[@]}" -gt 1 ]; then
  echo "ABORT: failed runs span ${#SKILL_ARMS[@]} skill arms, but SKILLS_SRC is one path."
  echo "       Retrying them together would install the same skill under both labels."
  echo "       Run once per arm, with the matching skill:"
  for a in "${SKILL_ARMS[@]}"; do
    echo "         ARM=$a SKILLS_SRC=<path-for-$a> $0 $TASK $PASSES"
  done
  exit 2
fi

echo "=== retry $TASK | arm=${ARM:-<all>} | skills_src=$SKILLSRC | passes=$PASSES ==="

for pass in $(seq 1 "$PASSES"); do
  mapfile -t FAILED < <(python "$HERE/find_failed.py" "$BENCH_HOME/runs" "$TASK" "${FILTER[@]}")
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

LEFT=$(python "$HERE/find_failed.py" "$BENCH_HOME/runs" "$TASK" "${FILTER[@]}" | wc -l)
echo "=== DONE: $LEFT run(s) still failing ==="
