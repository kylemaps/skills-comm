#!/bin/bash
# collect_results.sh TASK
#   TASK   a task id, or <task>@<subject> for a task graded per subject (run_ids.py)
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
# One run is one task and, for a task graded per subject, one subject. Runs are
# named by the run id; the grader takes its parts: fetch_reference.py --subject
# selects that subject's reference and rubric, grade_wrapper.py --subject reads
# that subject's output path. Split as run_ids.py splits, at the first '@', and
# refused as it refuses.
case "$TASK" in
  *[[:space:]]*|*/*|*__*) echo "FAIL: run id '$TASK' holds whitespace, '/' or '__'" >&2; exit 1 ;;
  *@*) TASK_BASE="${TASK%%@*}"; SUBJECT="${TASK#*@}"
       [ -n "$TASK_BASE" ] && [ -n "$SUBJECT" ] && [ "${SUBJECT#*@}" = "$SUBJECT" ] \
         || { echo "FAIL: run id '$TASK' is not <task> or <task>@<subject>" >&2; exit 1; } ;;
  *)   TASK_BASE="$TASK"; SUBJECT="" ;;
esac
# A blinded run names a case (run_ids.py is_case): the grader takes --case and maps it
# to the subject through the gated cases.json; the agent wrote under the case id, so
# the output directory is renamed to the subject before grading (below).
SUBJ=(); CASE=""
if [ -n "$SUBJECT" ]; then
  case "$SUBJECT" in
    case-[0-9][0-9][0-9]) CASE="$SUBJECT"; SUBJ=(--case "$SUBJECT") ;;
    *) SUBJ=(--subject "$SUBJECT") ;;
  esac
fi
cd "$HARNESS" || { echo "no harness at $HARNESS"; exit 1; }
# The submission's contracted path, read as grade_wrapper.py reads it: the task's own in
# run_manifest.json, else the default for a task graded per subject or for one with a
# single reference. A file for most tasks, a directory for a task that submits a
# dataset. Without a manifest, the image at the default place.
OUT_REL=$(python - "$TASK_BASE" "$SUBJECT" <<'PY' 2>/dev/null
import json, sys
task, subject = sys.argv[1], sys.argv[2]
m = json.load(open("run_manifest.json", encoding="utf-8"))
d = m.get("defaults", {})
tpl = m.get("tasks", {}).get(task, {}).get("submission_output_path") \
    or d["submission_path" if subject else "submission_output_path"]
print(tpl.format(task_id=task, subject=subject))
PY
) || OUT_REL=""
[ -n "$OUT_REL" ] || OUT_REL="submissions/$TASK_BASE${SUBJECT:+/$SUBJECT}/output.nii.gz"
# The grading plane's one external dependency, and its output and exit code were
# both being discarded. A failed fetch then graded every run against a missing or
# stale reference, which floors a whole task at zero and reads as a bad model --
# the failure shape that has cost this project the most time. Hard stop instead.
#
# Absent script means an older grader pack that ships its references, so that is a
# note rather than an error. A present script that fails is fatal.
FETCH_LOG="$BENCH_HOME/fetch_reference_$TASK.log"
if [ -f fetch_reference.py ]; then
  if python fetch_reference.py --task "$TASK_BASE" "${SUBJ[@]}" > "$FETCH_LOG" 2>&1; then
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
# A subject run is graded with --subject. A wrapper without it would read the task's
# flat path, find nothing, and score every run zero: the missing-reference shape
# again. Refused before anything is graded.
if [ -n "$SUBJECT" ] && ! python grade_wrapper.py grade --help 2>/dev/null | grep -q -- "${SUBJ[0]}"; then
  echo "ABORT: $HARNESS/grade_wrapper.py takes no ${SUBJ[0]}; $TASK cannot be graded per subject."
  echo "       Nothing has been graded."
  exit 1
fi
# The subject behind the case, from the reference fetch_reference.py just selected
# (its marker file). Never printed: the job log is not the place for it.
REAL=""
if [ -n "$CASE" ]; then
  REAL=$(cat "$(sed -n 's/^reference ready: //p' "$FETCH_LOG" | tail -1)/.subject" 2>/dev/null || true)
  [ -n "$REAL" ] || { echo "ABORT: the reference fetch did not record the case's subject."; exit 1; }
fi

TOTAL=$(ls -d "$BENCH_HOME"/runs/"$TASK"__*/ 2>/dev/null | wc -l)
# Zero runs is not a valid grading job, it is a wrong path. Without this the loop
# body never executes, the script prints "graded 0, reused 0 cached" and exits 0,
# and summarize then writes a summary of an empty set. That is the exit-0-with-no-
# artefact shape this tool exists to catch, in the tool itself. Found by someone
# reading the script before running it, which is the only reason it was found.
if [ "$TOTAL" -eq 0 ]; then
  echo "FAIL: no run directories match $BENCH_HOME/runs/${TASK}__*" >&2
  echo "      BENCH_HOME is ${BENCH_HOME}. Set it to the directory CONTAINING runs/." >&2
  exit 1
fi
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
  # idempotent, and it means old runs appear in the report with full detail. For a
  # blinded run the subject is passed in the environment, so the record can say
  # whether the agent ever named it (subject_named), without the record naming it.
  BLIND_SUBJECT="$REAL" OUTPUT_REL="$OUT_REL" python "$HERE/finalize_run.py" "$d" >/dev/null 2>&1
  # The agent wrote under the case id; grade_wrapper.py reads under the subject.
  if [ -n "$CASE" ] && [ -d "$d/submissions/$TASK_BASE/$CASE" ] && [ ! -e "$d/submissions/$TASK_BASE/$REAL" ]; then
    mv "$d/submissions/$TASK_BASE/$CASE" "$d/submissions/$TASK_BASE/$REAL"
    ln -s "$REAL" "$d/submissions/$TASK_BASE/$CASE"
  fi

  if [ ! -e "$d/$OUT_REL" ]; then
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
    python grade_wrapper.py grade --task "$TASK_BASE" "${SUBJ[@]}" --submission-dir "$d" \
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

# SWEEP and WAVE, when both set, classify with that wave's rules.
if [ -n "${SWEEP:-}" ] && [ -n "${WAVE:-}" ]; then
  python "$HERE/summarize.py" "$BENCH_HOME/runs" "$TASK" --sweep "$SWEEP" --wave "$WAVE"
else
  python "$HERE/summarize.py" "$BENCH_HOME/runs" "$TASK"
fi
