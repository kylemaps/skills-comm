#!/bin/bash
# collect_results.sh TASK
#
# Grade every run for TASK with the task's grader pack, then report.
#
# Two steps, deliberately separate:
#   1. grade   -- run each submission through the grader, producing envelope.json
#   2. report  -- summarize.py turns those into the numbers we quote
#
# The reporting used to live here as shell greps over the transcripts and was wrong
# twice (it counted tool *mentions* rather than tool *use*, and matched skill-load
# markers opencode never emits). It now reads the provenance the runner recorded.
# Grading is idempotent, so re-running this is free.
set -u

TASK="${1:?usage: collect_results.sh TASK}"
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"
HARNESS="${HARNESS_DIR:-$HOME/grader-repo/benchmark/harness}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$HARNESS" || { echo "no harness at $HARNESS"; exit 1; }
# The grading plane's one external dependency, and its output and exit code were
# both being discarded. A failed fetch then graded every run against a missing or
# stale reference, which floors a whole task at zero and reads as a bad model --
# the failure shape that has cost this project the most time. Hard stop instead.
#
# Absent script means an older grader pack that ships its references, so that is a
# note rather than an error. A present script that fails is fatal.
FETCH_LOG="$BENCH_HOME/fetch_reference_$TASK.log"
if [ -f fetch_reference.py ]; then
  if python fetch_reference.py --task "$TASK" > "$FETCH_LOG" 2>&1; then
    echo "reference ready for $TASK ($(grep -c "\[fetch\]" "$FETCH_LOG") fetched, $(grep -c "\[skip\]" "$FETCH_LOG") already present)"
  else
    echo "ABORT: could not fetch the reference for $TASK."
    sed "s/^/  /" "$FETCH_LOG"
    echo "  Grading against a missing reference scores every run zero, which is"
    echo "  indistinguishable from a bad model. Nothing has been graded."
    exit 1
  fi
else
  echo "note: no fetch_reference.py in $HARNESS; references must already be in the pack"
fi

TOTAL=$(ls -d "$BENCH_HOME"/runs/"$TASK"__*/ 2>/dev/null | wc -l)
GRADED=0
CACHED=0
NOOUT=0
I=0
for d in "$BENCH_HOME"/runs/"$TASK"__*/; do
  [ -d "$d" ] || continue
  I=$((I + 1))
  COND=$(basename "$d" | awk -F'__' '{print $3}')
  MODEL=$(basename "$d" | awk -F'__' '{print $2}')
  # Backfill provenance for runs made before finalize_run.py grew a field. Cheap,
  # idempotent, and it means old runs appear in the report with full detail.
  python "$HERE/finalize_run.py" "$d" >/dev/null 2>&1

  if [ ! -f "$d/submissions/$TASK/output.nii.gz" ]; then
    # No output is a failed run, not a missing one. summarize.py scores it 0.
    NOOUT=$((NOOUT + 1))
    printf '[%3d/%3d] no output  %s\n' "$I" "$TOTAL" "$(basename "$d")"
  elif [ -f "$d/envelope.json" ] && [ "${FORCE_REGRADE:-0}" != 1 ]; then
    # Grading a 7T volume is slow. Without this, every interrupted collection
    # restarts from run 1 and never reaches the end. FORCE_REGRADE=1 overrides
    # after a grader-pack change.
    CACHED=$((CACHED + 1))
    printf '[%3d/%3d] cached     %s\n' "$I" "$TOTAL" "$(basename "$d")"
  else
    printf '[%3d/%3d] grading    %s\n' "$I" "$TOTAL" "$(basename "$d")"
    python grade_wrapper.py grade --task "$TASK" --submission-dir "$d" \
      --model "$MODEL" --condition "$COND" --out "$d/envelope.json" >/dev/null 2>&1
    GRADED=$((GRADED + 1))
  fi
done
echo "graded $GRADED, reused $CACHED cached, $NOOUT produced no output (scored 0)"

if [ -f "$BENCH_HOME/sweep_manifest.json" ]; then
  echo
  echo "=== SWEEP MANIFEST ==="
  cat "$BENCH_HOME/sweep_manifest.json"
fi

python "$HERE/summarize.py" "$BENCH_HOME/runs" "$TASK"
