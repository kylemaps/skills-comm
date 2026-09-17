#!/bin/sh
# Build a self-checking re-grade pack: known inputs, known correct output.
#
#     bash make_regrade_pack.sh [N_PER_TASK]
#
# Grading is pure. Run directories in, summary.json and runs.csv out, no network,
# no agent, no tokens. So a machine can be validated by re-grading runs whose answer
# is already known, and the diff is the test.
#
# That makes this the cheapest end-to-end check of a new execution environment:
# it exercises python, the grader's imports, file IO and the workspace against a
# KNOWN-CORRECT ANSWER rather than against "did it exit 0". It needs no gateway, no
# API key and no runner pool, so it can run long before a full cell can.
#
# WHAT TO COMPARE, AND WHAT NOT TO
# Only the grader's own outputs are deterministic. runs.csv also carries durations,
# timestamps and token counts, which differ every time and are not evidence of
# anything. The manifest names the fields that matter.
set -eu

N="${1:-5}"
RUNS="${BENCH_HOME:-$HOME/bench}/runs"
REPORT="${BENCH_HOME:-$HOME/bench}/report"
GRADER="${GRADER_REPO_DIR:-$HOME/grader-repo}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HOME/regrade_pack_$(date -u +%Y%m%d)"

TASKS="${TASKS:-structural-brain-extraction-7t diffusion-brain-mask}"

rm -rf "$OUT"
mkdir -p "$OUT/runs" "$OUT/expected" "$OUT/reference" "$OUT/code"

for t in $TASKS; do
  # Spread the sample over models and arms rather than taking the first N
  # alphabetically, which would be one model's runs and would not exercise the
  # pass and fail paths. `sort -R` is fine here: the pack records exactly which
  # runs it contains, so reproducibility comes from the manifest, not the draw.
  ls -d "$RUNS/${t}__"* 2>/dev/null | sort -R | head -"$N" | while read -r d; do
    n=$(basename "$d")
    mkdir -p "$OUT/runs/$n"
    # Only what grading reads. No data/, no tmp/ -- those are the fetched dataset
    # and scratch, they are large and the grader never looks at them.
    for keep in run.json prompt.txt transcript.txt submissions; do
      [ -e "$d/$keep" ] && cp -r "$d/$keep" "$OUT/runs/$n/"
    done
  done

  [ -f "$REPORT/summary_$t.json" ] && cp "$REPORT/summary_$t.json" "$OUT/expected/"
  [ -f "$REPORT/runs_$t.csv" ] && cp "$REPORT/runs_$t.csv" "$OUT/expected/"

  mkdir -p "$OUT/reference/$t"
  cp "$GRADER/benchmark/graders/$t/reference/"*.nii.gz "$OUT/reference/$t/" 2>/dev/null || true
  cp "$GRADER/benchmark/graders/$t/rubric.json" "$OUT/reference/$t/" 2>/dev/null || true
done

# Self-contained: the grader and the summariser travel with the data, so the pack
# does not depend on cloning two repos at the right refs to be usable.
cp -r "$GRADER/benchmark/harness" "$OUT/code/harness"
cp "$HERE/summarize.py" "$HERE/collect_results.sh" "$HERE/finalize_run.py" "$OUT/code/"

cat > "$OUT/MANIFEST.md" <<MANIFEST
# Re-grade pack

Known inputs, known correct output. Re-grade these and diff.

- built: $(date -u '+%Y-%m-%d %H:%M UTC')
- built on: $(hostname)
- runs: $(ls "$OUT/runs" | wc -l)
- tasks: $TASKS

## Contents

- \`runs/\` - run directories, stripped to what the grader reads
- \`expected/\` - the graded output this machine produced from those runs
- \`reference/\` - the hidden ground truth and rubric per task
- \`code/\` - the grader and summariser, so the pack is self-contained

## Run it

\`\`\`sh
HARNESS_DIR=\$PWD/code/harness code/collect_results.sh <task>
python3 code/summarize.py runs <task> --out-dir out
\`\`\`

## Compare these fields, and only these

Per run, from \`runs.csv\`:

- \`verdict\` \`score\` \`dice\` \`passed\`

Per cell, from \`summary.json\`:

- \`n\` \`passes\` \`mean\` \`sd\` \`median\`

Everything else in those files is environment-dependent and will differ on any
machine: durations, timestamps, token counts, session ids, paths. A difference
there is not a finding.

A Dice differing in the fourth decimal is floating-point noise between library
versions. A flipped \`passed\` is not, and means the grading environment is not
equivalent.

## Provenance of the expected output

$(for t in $TASKS; do
  f="$REPORT/summary_$t.json"
  [ -f "\$f" ] && python3 -c "
import json,sys
s=json.load(open('$f'))
p=s.get('provenance') or {}
print('- **$t** - %s runs, %s valid, %s excluded' % (s.get('n_runs'), s.get('n_valid'), s.get('n_excluded')))
for k in ('image_version','opencode_version','tasks_sha'):
    print('  - %s: %s' % (k, p.get(k)))
" 2>/dev/null
done)
MANIFEST

du -sh "$OUT"
echo
echo "runs packed: $(ls "$OUT/runs" | wc -l)"
echo "wrote $OUT/MANIFEST.md"
echo
echo "tar it:  tar czf ${OUT}.tar.gz -C \"$(dirname "$OUT")\" \"$(basename "$OUT")\""
