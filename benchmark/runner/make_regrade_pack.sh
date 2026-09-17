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
mkdir -p "$OUT/runs" "$OUT/expected" "$OUT/code"

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


done

# Copy the grader files run_manifest NAMES, at the paths it names them, rather than
# guessing the layout. Two things make guessing wrong:
#   - `scorer` and `pack_dir` are separate. 7t and motion both score with
#     graders/structural-brain-extraction/score_brain_mask.py, which is not in
#     either task's own directory.
#   - grade_wrapper resolves graders_root as the manifest's parent.parent and then
#     joins these paths verbatim, so they have to land exactly where it looks.
python3 - "$GRADER" "$OUT" $TASKS <<'PY'
import json, os, shutil, sys
grader, out = sys.argv[1], sys.argv[2]
tasks = sys.argv[3:]
man = json.load(open(os.path.join(grader, "benchmark/harness/run_manifest.json")))
for t in tasks:
    spec = man["tasks"][t]
    # The scorer, at the manifest's own relative path.
    src = os.path.join(grader, "benchmark", spec["scorer"])
    dst = os.path.join(out, "code", spec["scorer"])
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    # The pack: rubric, and the references where fetch_reference would have left
    # them, so it finds them present and skips the download. Without that the
    # re-grade reaches huggingface.co and an offline validation is not offline.
    pack = spec["pack_dir"]
    os.makedirs(os.path.join(out, "code", pack, "reference"), exist_ok=True)
    shutil.copy2(os.path.join(grader, "benchmark", pack, "rubric.json"),
                 os.path.join(out, "code", pack, "rubric.json"))
    for f in spec.get("reference_files", []):
        shutil.copy2(os.path.join(grader, "benchmark", pack, "reference", f),
                     os.path.join(out, "code", pack, "reference", f))
    print("  %-34s scorer=%s" % (t, spec["scorer"]))
PY

# Self-contained: the grader and the summariser travel with the data, so the pack
# does not depend on cloning two repos at the right refs to be usable.
cp -r "$GRADER/benchmark/harness" "$OUT/code/harness"
cp "$HERE/summarize.py" "$HERE/collect_results.sh" "$HERE/finalize_run.py" "$OUT/code/"

# A dependency check, shipped, because the pack's headline claim is that it is
# offline and the way it fails when a dependency is absent contradicts that claim.
#
# collect_results.sh runs fetch_reference.py unconditionally, and that module does
# `from huggingface_hub import hf_hub_download` at import time -- BEFORE the
# already-present check that would otherwise skip every download. So on a machine
# without the package, a fully offline re-grade against references that are all
# sitting on disk still dies, and it dies as:
#
#     ABORT: could not fetch the reference. Nothing has been graded.
#
# On a cluster with an egress policy in front of it that reads as the policy
# blocking huggingface.co. It is a missing pip package. Someone would spend a day
# on the firewall. Found by cluster-explorer, 2026-09-17.
#
# Deliberately NOT fixed by patching the vendored grader. A pack that ships a
# modified scorer is no longer validating the grader that produced our results,
# and the real fix is a one-line move upstream in nipreps/skills-comm.
cat > "$OUT/check_deps.sh" <<'DEPS'
#!/bin/sh
# Run this FIRST. Every import grading needs, named, before anything is graded.
echo "python3: $(python3 --version 2>&1)"
miss=0
for m in numpy scipy nibabel huggingface_hub; do
  if python3 -c "import $m" 2>/dev/null; then
    echo "  ok       $m"
  else
    echo "  MISSING  $m"
    miss=1
  fi
done
# Optional. summarize.py reports "(ASTRA records present but unread: PyYAML not
# installed)" rather than failing, and only when a run carries an astra.yaml, so
# its absence costs one analysis column and nothing else.
python3 -c "import yaml" 2>/dev/null \
  && echo "  ok       PyYAML (optional)" \
  || echo "  absent   PyYAML (optional: costs the ASTRA column, grades fine)"
if [ "$miss" != 0 ]; then
  echo
  echo "STOP. A missing package here surfaces later as"
  echo "  ABORT: could not fetch the reference. Nothing has been graded."
  echo "which reads as a network or egress problem and is not one. huggingface_hub"
  echo "is imported by fetch_reference.py at module load, before the check that"
  echo "would skip the download -- so it is required even though this pack is"
  echo "offline and every reference is already in place."
  exit 1
fi
echo "deps ok"
DEPS
chmod +x "$OUT/check_deps.sh"

# Fail loudly rather than ship a pack that cannot grade. Every one of these was
# a real omission in the first version.
python3 - "$OUT" $TASKS <<'PY'
import json, os, sys
out = sys.argv[1]
man = json.load(open(os.path.join(out, "code/harness/run_manifest.json")))
bad = []
for t in sys.argv[2:]:
    spec = man["tasks"][t]
    need = [spec["scorer"],
            os.path.join(spec["pack_dir"], "rubric.json")]
    need += [os.path.join(spec["pack_dir"], "reference", f)
             for f in spec.get("reference_files", [])]
    for rel in need:
        if not os.path.exists(os.path.join(out, "code", rel)):
            bad.append("%s: missing %s" % (t, rel))
if bad:
    sys.exit("PACK IS NOT GRADEABLE:\n  " + "\n  ".join(bad))
print("  verified: every path run_manifest names is present")
PY
# An envelope.json inside a run directory makes collect_results report "cached" and
# skip the work, so every run would agree with the expected output BY CONSTRUCTION
# and the diff would pass without grading anything. The copy list above excludes it;
# this asserts that rather than trusting it.
if find "$OUT/runs" -name envelope.json | grep -q .; then
  echo "FAIL: envelope.json in the pack would make every run report cached" >&2
  exit 1
fi

# Expected output, computed over EXACTLY the runs in this pack.
#
# Copying the full sweep's summary was wrong: the pack is a 10-run sample, so its
# cells hold 1-3 runs against an expected n=10. Every cell would mismatch on every
# field, on a correct machine, and the documented reading of a mismatch is "the
# grading environment is not equivalent".
#
# Generated in a throwaway copy, because grading writes envelope.json into each run
# directory and an envelope inside the pack would make collect_results report every
# run as cached -- the diff would then pass without grading anything.
TMP=$(mktemp -d)
mkdir -p "$TMP/runs"
cp -r "$OUT/runs/." "$TMP/runs/"
for t in $TASKS; do
  BENCH_HOME="$TMP" HARNESS_DIR="$OUT/code/harness" FORCE_REGRADE=1 \
    "$OUT/code/collect_results.sh" "$t" >/dev/null 2>&1 || {
      echo "FAIL: could not grade the sample to produce expected/" >&2; rm -rf "$TMP"; exit 1; }
  python3 "$OUT/code/summarize.py" "$TMP/runs" "$t" --out-dir "$OUT/expected" >/dev/null
done
rm -rf "$TMP"
for t in $TASKS; do
  [ -f "$OUT/expected/summary_$t.json" ] || { echo "FAIL: no expected output for $t" >&2; exit 1; }
done
echo "  expected/ computed over the sampled runs, not the full sweep"

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
- \`code/graders/<task>/\` - scorer, rubric, and the hidden ground truth
- \`code/\` - the grader harness and summariser, so the pack is self-contained

No \`envelope.json\` ships inside a run directory. If one did, collect_results would
report every run as cached, skip the grading, and the diff would pass without having
graded anything.

## Run it

\`\`\`sh
sh check_deps.sh                       # first, always

BENCH_HOME=\$PWD HARNESS_DIR=\$PWD/code/harness FORCE_REGRADE=1 \\
  code/collect_results.sh <task>
python3 code/summarize.py runs <task> --out-dir out
\`\`\`

\`check_deps.sh\` is not a formality. \`collect_results.sh\` runs
\`fetch_reference.py\` unconditionally, and that module imports \`huggingface_hub\`
at load time -- before the already-present check that would skip every download.
So this pack is offline and still hard-requires the package, and without it the
failure you see is:

    ABORT: could not fetch the reference. Nothing has been graded.

On a cluster with an egress policy in front of it, that reads as the policy
blocking huggingface.co. It is a missing pip package.

\`BENCH_HOME\` is required. It defaults to \`\$HOME/bench\`, which exists only on the
machine that built this; unset, collect_results now stops rather than grading zero
runs and exiting 0.

This is offline: the references are already in place, so fetch_reference skips the
download and nothing reaches the network.

## Compare these fields, and only these

Per run, from \`runs.csv\`, joined on (model, arm, rep). Note the run directory name
carries a \`neurodesk-\` prefix on the model that the csv does not:

- \`verdict\` \`score\` \`dice\` \`passed\`

This is the test that matters: independent gradings against known-correct answers,
covering both the pass and the fail path.

Per cell, from \`summary.json\`:

- \`n\` \`passes\` \`mean\` \`sd\` \`median\`

\`expected/\` is computed over EXACTLY the runs in this pack, not over the full sweep,
so cells hold 1-3 runs and those are the numbers to expect. An earlier version shipped
the full sweep's summary against a 10-run sample: every cell mismatched on every field
on a correct machine, which manufactured the false positive these criteria exist to
prevent.

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
