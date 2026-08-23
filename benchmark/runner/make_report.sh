#!/bin/bash
# make_report.sh [RUNS_DIR] [OUT_DIR]
#
# Regenerate every published number from the runs on disk, in one command.
#
# WHY THIS EXISTS
# ---------------
# A paranoid pass over the results found nine wrong numbers before they reached
# anyone, including two headline figures. Every one of them came from an ad-hoc
# one-liner typed into a terminal rather than from a script under version
# control: a subset quoted as if it were the whole arm, a count taken from
# rounded printed values instead of the files, a p-value from the wrong slice.
#
# None of those are hard mistakes to make once. They are impossible to make
# repeatedly if the numbers can only come from here.
#
# So: no figure goes into a message, a poster or a paper unless this script
# produced it. Re-running is then also the audit -- regenerate, diff against the
# last report, and anything that moved while the inputs did not is a bug.
#
# WHAT IT WRITES
# --------------
# One directory per invocation, one file per analysis, plus a manifest pinning
# the commit and the input state. Files are ordered by prefix so a diff of two
# report directories reads top to bottom.
#
# DETERMINISM
# -----------
# Output must be byte-identical for identical inputs, or diffing it is
# worthless. Nothing here prints a timestamp into a file: the date lives in the
# directory name and the provenance lives in the manifest.
set -u

RUNS="${1:-$HOME/bench/runs}"
OUT="${2:-$HOME/bench/report/$(date -u +%Y-%m-%d)}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
QCVAL="${QCVAL:-$HOME/bench/qcval}"

TASKS=(
  structural-brain-extraction-7t
  structural-brain-extraction-7t-nodura
  structural-brain-extraction-motion
  structural-brain-extraction-stroke
)

# The tools each grader pack designates as its kept reference panel, read from
# the pack's PROVENANCE.md. NOT chosen by which tools happened to pass -- that
# would make the whole selection-versus-execution split circular. All four packs
# curate to the same panel, and the split is insensitive to whether AFNI is in
# it, because every run outside the panel used FSL BET alone.
ROBUST="synthstrip,hd-bet,afni"

# Grading a sweep while agents are running is what exhausted memory on this box
# once already, and a report built mid-sweep describes a state that never
# existed. Refuse rather than warn.
if pgrep -f "run_bench.sh|run_sweep.sh" > /dev/null 2>&1; then
  echo "ABORT: a sweep is running. A report built mid-sweep is a snapshot of"
  echo "       nothing, and grading alongside agents has OOM'd this box before."
  exit 1
fi

if [ ! -d "$RUNS" ]; then
  echo "ABORT: no runs dir at $RUNS"
  exit 1
fi

mkdir -p "$OUT"
echo "=== report -> $OUT"

# --- manifest ---------------------------------------------------------
# Enough to reconstruct what was measured: the harness commit, the grader
# commit, and the input counts. A report whose numbers cannot be traced to a
# harness version is an anecdote.
{
  echo "runs_dir           $RUNS"
  echo "harness_commit     $(git -C "$HERE" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "harness_dirty      $(test -n "$(git -C "$HERE" status --porcelain 2>/dev/null)" && echo yes || echo no)"
  echo "grader_commit      $(git -C "$HOME/grader-repo" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "robust_panel       $ROBUST"
  echo "run_dirs           $(ls -1 "$RUNS" 2>/dev/null | wc -l)"
  echo "graded_runs        $(ls -1 "$RUNS"/*/envelope.json 2>/dev/null | wc -l)"
  echo "masks_on_disk      $(ls -1 "$RUNS"/*/submissions/*/output.nii.gz 2>/dev/null | wc -l)"
  for t in "${TASKS[@]}"; do
    echo "task $t  $(ls -1d "$RUNS/$t"__* 2>/dev/null | wc -l) run dirs"
  done
} > "$OUT/00_MANIFEST.txt"
echo "--- 00_MANIFEST.txt"

# --- per task ---------------------------------------------------------
for t in "${TASKS[@]}"; do
  n=$(ls -1d "$RUNS/$t"__* 2>/dev/null | wc -l)
  if [ "$n" -eq 0 ]; then
    echo "--- skip $t (no runs)"
    continue
  fi
  python "$HERE/summarize.py" "$RUNS" "$t" --out-dir "$OUT" \
      > "$OUT/10_summary_$t.txt" 2>&1
  python "$HERE/gates.py" "$RUNS" "$t" \
      > "$OUT/20_gates_$t.txt" 2>&1
  python "$HERE/mechanism.py" "$OUT/runs_$t.csv" --robust "$ROBUST" --by-model \
      > "$OUT/30_mechanism_$t.txt" 2>&1
  echo "--- $t"
done

# --- cross task -------------------------------------------------------
python "$HERE/matrix.py" "$RUNS" > "$OUT/40_matrix.txt" 2>&1
python "$HERE/token_report.py" "$RUNS" --cells --full \
    > "$OUT/41_tokens.txt" 2>&1
echo "--- matrix, tokens"

# --- agent self-QC, only where the battery has been run ---------------
# Absent qcval output is normal (it needs the anat, which the disk cleanup
# removed once), so this is skipped rather than failed. A skip is printed so an
# empty section is never mistaken for an empty result.
for pair in "out:7t" "out_nodura:nodura"; do
  d="${pair%%:*}"
  lbl="${pair##*:}"
  if [ ! -d "$QCVAL/$d" ]; then
    echo "--- skip qcval $lbl (no $QCVAL/$d)"
    continue
  fi
  python "$HERE/qcval_join.py" "$QCVAL/$d" "$RUNS" \
      --csv "$OUT/qcval_$lbl.csv" > "$OUT/50_qcval_$lbl.txt" 2>&1
  python "$HERE/qcval_simulate.py" "$QCVAL/$d" "$RUNS" \
      --relax brain_to_head_ratio_low \
      --relax rim_brightness_high \
      --relax bright_rim_frac_pct \
      > "$OUT/51_qcsim_$lbl.txt" 2>&1
  echo "--- qcval $lbl"
done

# --- normalise paths so a diff shows only numbers ----------------------
# Several tools echo where they wrote to, which lands inside the captured
# output. Two reports of identical data then differ on every one of those lines
# and the diff drowns the thing it exists to surface. Replace the two paths that
# vary with tokens. The manifest is excluded on purpose: recording the real
# paths is its job.
for f in "$OUT"/*.txt; do
  case "$(basename "$f")" in
    00_MANIFEST.txt) continue ;;
  esac
  sed -i "s|$OUT|<OUT>|g; s|$RUNS|<RUNS>|g" "$f"
done

# --- did anything fail? -----------------------------------------------
# Each analysis captured stderr into its own file, so a crash lands as a
# traceback inside an otherwise plausible-looking report. Surface it here
# instead of leaving it for someone to find while quoting the file.
BROKEN=$(grep -l "Traceback (most recent call last)" "$OUT"/*.txt 2>/dev/null | wc -l)
echo
if [ "$BROKEN" -gt 0 ]; then
  echo "!! $BROKEN analysis file(s) contain a traceback:"
  grep -l "Traceback (most recent call last)" "$OUT"/*.txt | sed 's|^|   |'
  echo "   Do not quote this report until they are fixed."
else
  echo "=== OK: $(ls -1 "$OUT" | wc -l) files, no tracebacks"
fi
echo "=== $OUT"
echo
echo "To audit against the previous report:"
echo "  diff -r <previous_report_dir> $OUT"
