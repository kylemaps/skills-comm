#!/bin/sh
# Negative controls: prove each check can FAIL. Run it: sh test_checks.sh
#
# WHY THIS EXISTS, AND WHY IT IS NEGATIVE CONTROLS SPECIFICALLY
# Five checks were written in one day. Each was tested by running it and seeing a
# sensible answer, which demonstrates it can pass and says nothing about whether it
# can ever fail. A check that cannot fail is indistinguishable from one that works,
# and it is the most expensive object in this repo: it consumes attention, licenses
# confidence, and reports nothing.
#
# Three real instances from the day these were written, all mine:
#   - container_opts_check.sh was described as "meant for preflight" and preflight
#     did not call it.
#   - egress_assert.sh was wired into preflight AFTER the ENVFAIL evaluation, so its
#     result was computed and discarded. A blocked host would have printed BLOCKED
#     and then PREFLIGHT OK.
#   - display_audit.py matched `afni` in prose and reported 66 runs invoking a GUI,
#     concentrated in one arm.
#
# The first two were found by looking again, not by any test. The exception is the
# exclusion-ordering test in test_analysis.py, where I deliberately reordered the
# source to confirm the test went red -- and that is the only technique used that
# day which would have caught any of the three. So: do it for all of them.
#
# The rule this encodes: a check is not finished when it passes on good input. It is
# finished when it has been shown to fail on bad input.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PASS=0
FAIL=0
PY=$(command -v python3 || command -v python)

ok()   { PASS=$((PASS+1)); printf '  ok    %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$1"; }
# The whole point: `want` is the exit code we require, and for most of these it is
# non-zero. Asserting 0 here would be re-testing the happy path.
expect() { want=$1; got=$2; name=$3
  [ "$got" = "$want" ] && ok "$name (exit $got)" || bad "$name: wanted $want, got $got"; }

echo "=== container_opts_check: does it notice --overlay"
T=$(mktemp -d)
mkdir -p "$T/containers/fsl_1_x" "$T/containers/hdbet_1_x"
printf 'singularity exec --cleanenv img.simg bet "$@"\n' > "$T/containers/fsl_1_x/bet"
printf 'singularity exec --overlay /tmp/o.img img.simg hd-bet "$@"\n' > "$T/containers/hdbet_1_x/hd-bet"
CVMFS_ROOT="$T" TOOLS="fsl hdbet" sh "$HERE/container_opts_check.sh" >/dev/null 2>&1
expect 1 $? "flags a wrapper passing --overlay"
CVMFS_ROOT="$T" TOOLS="fsl" sh "$HERE/container_opts_check.sh" >/dev/null 2>&1
expect 0 $? "passes a clean wrapper"
# A named tool with no matching directory must not silently succeed: "we checked and
# found nothing" and "there was nothing to check" are different answers.
CVMFS_ROOT="$T" TOOLS="notarealtool" sh "$HERE/container_opts_check.sh" >/dev/null 2>&1
expect 1 $? "refuses when no container matched the tool list"
rm -rf "$T"

echo
echo "=== egress_assert: does it notice an unreachable host"
HOSTS="definitely-not-a-real-host-xyz.invalid" sh "$HERE/egress_assert.sh" >/dev/null 2>&1
expect 1 $? "fails on an unreachable host"
HOSTS="definitely-not-a-real-host-xyz.invalid" sh "$HERE/egress_assert.sh" --warn >/dev/null 2>&1
expect 0 $? "--warn reports and exits 0"

echo
echo "=== display_audit: invocation vs mention"
T=$(mktemp -d); TASK=t
mk() { d="$T/${TASK}__$1__$2__r$3"; mkdir -p "$d"
  printf '{"task_id":"t","model":"%s","condition":"%s","repeat":"%s"}\n' "$1" "$2" "$3" >"$d/run.json"
  cat > "$d/transcript.txt"; }
# Prose, a module listing, and the documented Agg QC step. All three were false
# positives in the first version; `afni_outline_qc` was the largest and it was
# concentrated in one arm, which is how a detector manufactures an arm difference.
mk m1 env+skill 1 <<'X'
AFNI prescribes `3dcalc -a VOL -b MASK`, so it should work
AFNI outline QC PNG saved: qc/sub-01_afni_outline_qc.png
module avail | grep fsleyes
   fsleyes/1.9.0   freeview/7.4.1
python3 afni_outline_qc.py in.nii mask.nii
X
inv=$("$PY" "$HERE/display_audit.py" "$T" 2>/dev/null | sed -n 's/^\([0-9]*\) INVOKED one/\1/p')
[ "$inv" = "0" ] && ok "prose, module avail and afni_outline_qc are not invocations" \
                 || bad "counted $inv invocations in a transcript with none"
mk m2 env-only 1 <<'X'
fsleyes render out.nii.gz &>/dev/null || echo fallback
X
inv=$("$PY" "$HERE/display_audit.py" "$T" 2>/dev/null | sed -n 's/^\([0-9]*\) INVOKED one/\1/p')
[ "$inv" = "1" ] && ok "a real invocation is still caught" \
                 || bad "expected 1 invocation, got $inv"
rm -rf "$T"

echo
echo "=== timeout_forensics: all four verdicts are reachable"
T=$(mktemp -d); TASK=t
mkr() { d="$T/${TASK}__$1__$2__r$3"; mkdir -p "$d/submissions/$TASK"
  [ "$4" = out ] && echo x > "$d/submissions/$TASK/output.nii.gz"
  printf '{"exit_code":124,"model":"%s","condition":"%s","repeat":"%s","seconds_to_output":%s,"duration_s":2700}\n' \
    "$1" "$2" "$3" "$5" > "$d/run.json"; cat > "$d/transcript.txt"; }
mkr a env-only 1 out 1200 <<'X'
wrote submissions/t/output.nii.gz
fslstats QC, the mask is complete
X
mkr b env-only 2 out 1200 <<'X'
wrote submissions/t/output.nii.gz
that looks wrong, retry with mri_synthstrip -i in.nii
X
mkr c env-only 3 out 2680 <<'X'
wrote submissions/t/output.nii.gz
X
mkr d env-only 4 noout 0 <<'X'
bet failed
X
out=$(PYTHONPATH="$HERE" "$PY" "$HERE/timeout_forensics.py" "$T" $TASK 2>/dev/null)
for v in finished intermediate marginal no-output; do
  echo "$out" | grep -q "$v" && ok "verdict '$v' reachable" || bad "verdict '$v' never produced"
done
rm -rf "$T"

echo
echo "=== pool_check: does it notice a mixed environment"
T=$(mktemp -d)
gen() { cat > "$T/summary_$1.json" <<J
{"task":"$1","cells":{"m|env-only":{"n":10,"provenance":{
 "image_version":{"$2":10},"opencode_version":{"1.0":10},"skills_sha":{"a":10},
 "skills_hash":{"h":10},"prompt_hash":{"$1p":10},"tasks_sha":{"t":10}}}}}
J
}
gen taskA 2026-08-05; gen taskB 2026-08-27
"$PY" "$HERE/pool_check.py" "$T"/summary_*.json >/dev/null 2>&1
expect 1 $? "fails when two tasks used different images"
gen taskB 2026-08-05
"$PY" "$HERE/pool_check.py" "$T"/summary_*.json >/dev/null 2>&1
expect 0 $? "passes when the environment is constant"
# One task is not a pooling question. Silently returning "poolable" would be the
# worst possible answer here.
"$PY" "$HERE/pool_check.py" "$T/summary_taskA.json" >/dev/null 2>&1
expect 1 $? "refuses a single task rather than calling it poolable"
rm -rf "$T"


echo
echo "=== assemble_cell: every gate must be able to REFUSE"
# The gate exists to say no. Each block below makes exactly one thing wrong and
# asserts the refusal, because a gate only shown to pass is a transport step.
T=$(mktemp -d); TASK=structural-brain-extraction-7t; SW="$T/sweep.json"
cat > "$SW" <<J
{"reps":3,
 "arms":{"env-only":{"skills_hash":null},"env+skill":{"skills_hash":"291f844a43ec"}},
 "tasks":{"$TASK":{"models":["kimi-k3"],"arms":["env-only","env+skill"]}}}
J
# One run. `over` is a JSON fragment merged in to break exactly one field.
mkrun() { d="$T/runs/${TASK}__kimi-k3__$1__r$2"; mkdir -p "$d/submissions/$TASK"
  echo x > "$d/submissions/$TASK/output.nii.gz"
  [ "${3:-graded}" = graded ] && echo '{"score":100,"verdict":"PASS"}' > "$d/envelope.json"
  cat > "$d/run.json" <<J
{"task_id":"$TASK","model":"kimi-k3","condition":"$1","repeat":"$2","exit_code":0,
 "output_present":true,"skills_seen":[],"skills_installed":"$4",
 "image_version":"${5:-2026-08-05}","opencode_version":"1.18.7","skills_sha":"a",
 "skills_hash":"${6:-291f844a43ec}","prompt_hash":"p","tasks_sha":"t"}
J
}
run_gate() { "$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task "$TASK" \
  --model kimi-k3 --arm "$1" --sweep "$SW" >"$T/out" 2>&1; }

rm -rf "$T/runs"; for r in 1 2 3; do mkrun env+skill $r graded brain-extraction; done
run_gate env+skill; expect 0 $? "passes a clean, complete cell"

rm -rf "$T/runs"; for r in 1 2; do mkrun env+skill $r graded brain-extraction; done
run_gate env+skill; expect 1 $? "refuses 2 of 3 runs"
grep -q "complete" "$T/out" && ok "  and names the 'complete' gate" || bad "  wrong gate named"

rm -rf "$T/runs"; for r in 1 2 3; do mkrun env+skill $r ungraded brain-extraction; done
run_gate env+skill; expect 1 $? "refuses an ungraded run"
# Via the exclusions gate, not a separate "graded" one. summarize.py already marks
# an ungraded run with output as "not graded yet", so a dedicated gate could only
# fire where this one already had. Asserting the gate NAME is what exposed that.
grep -q "not graded yet" "$T/out" && ok "  via the exclusions gate, naming why" || bad "  refused by the wrong gate"

rm -rf "$T/runs"; for r in 1 2; do mkrun env+skill $r graded brain-extraction; done
mkrun env+skill 3 graded brain-extraction 2026-09-01
run_gate env+skill; expect 1 $? "refuses a cell spanning two images"
grep -q "FAIL  one environment" "$T/out" && ok "  via the 'one environment' gate" || bad "  refused by the wrong gate"

rm -rf "$T/runs"; for r in 1 2; do mkrun env+skill $r graded brain-extraction; done
mkrun env+skill 3 graded brain-extraction 2026-08-05 deadbeefcafe
run_gate env+skill; expect 1 $? "refuses runs whose skills_hash is not the arm's"
grep -q "FAIL  the arm is what it claims" "$T/out" && ok "  via the 'arm is what it claims' gate" || bad "  refused by the wrong gate"

# An env+skill run with no skill installed is misassigned -- summarize excludes it,
# so this also exercises the unresolved-exclusion gate.
rm -rf "$T/runs"; for r in 1 2 3; do mkrun env+skill $r graded ""; done
run_gate env+skill; expect 1 $? "refuses a skill arm with no skill installed"

rm -rf "$T/runs"; for r in 1 2 3; do mkrun env-only $r graded ""; done
"$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task not-a-task --model kimi-k3 \
  --arm env-only --sweep "$SW" >/dev/null 2>&1
expect 1 $? "refuses a task nobody declared"
"$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task "$TASK" --model kimi-k3 \
  --arm env+skill-nonexistent --sweep "$SW" >/dev/null 2>&1
expect 1 $? "refuses an arm nobody declared"

# Staging over a cell that is already published. Run directories are named
# <task>__<model>__<arm>__rN and carry no date and no node, so a CI re-run of any
# declared cell produces exactly the ten names already in results/runs/.
rm -rf "$T/runs" "$T/stage"; mkdir -p "$T/stage"
for r in 1 2 3; do mkrun env+skill $r graded brain-extraction; done
stage_gate() { "$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task "$TASK" \
  --model kimi-k3 --arm env+skill --sweep "$SW" --stage "$T/stage" \
  ${1:+--replace} >"$T/out" 2>&1; }
stage_gate; expect 0 $? "stages into an empty tree"

rm -rf "$T/runs"
for r in 1 2 3; do mkrun env+skill $r graded brain-extraction 2026-09-01; done
stage_gate; expect 1 $? "refuses to overwrite a published cell"
grep -q "FAIL  not a replacement" "$T/out" && ok "  via the 'not a replacement' gate" || bad "  refused by the wrong gate"
grep -q "image_version 2026-08-05 -> 2026-09-01" "$T/out" && ok "  and names what would change" || bad "  does not say what would change"

stage_gate yes; expect 0 $? "--replace allows it"
grep -q "3 published run(s) will be overwritten" "$T/out" && ok "  and says how many" || bad "  silent about the overwrite"

# Identical provenance is still a replacement: the score and the transcript that
# produced it are gone, and n=10 before and after says nothing happened.
stage_gate; expect 1 $? "refuses even when the environment is unchanged"
rm -rf "$T"


# The distinction the gate got wrong on its first real run: unrecorded is not wrong.
T=$(mktemp -d); TASK=structural-brain-extraction-7t; SW="$T/sweep.json"
cat > "$SW" <<J
{"reps":3,"arms":{"env+skill":{"skills_hash":"291f844a43ec"}},
 "tasks":{"$TASK":{"models":["kimi-k3"],"arms":["env+skill"]}}}
J
mkh() { d="$T/runs/${TASK}__kimi-k3__env+skill__r$1"; mkdir -p "$d/submissions/$TASK"
  echo x > "$d/submissions/$TASK/output.nii.gz"
  echo '{"score":100}' > "$d/envelope.json"
  cat > "$d/run.json" <<J
{"task_id":"$TASK","model":"kimi-k3","condition":"env+skill","repeat":"$1","exit_code":0,
 "output_present":true,"skills_seen":[],"skills_installed":"brain-extraction",
 "image_version":"2026-08-05","opencode_version":"1.18.7","skills_sha":"a",
 "skills_hash":"$2","prompt_hash":"p","tasks_sha":"t"}
J
}
for r in 1 2 3; do mkh $r unknown; done
"$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task "$TASK" --model kimi-k3 \
  --arm env+skill --sweep "$SW" >"$T/o" 2>&1
expect 0 $? "a cell predating skills_hash is unverifiable, not refused"
grep -q "UNVERIFIABLE" "$T/o" && ok "  and says so rather than passing silently" \
                              || bad "  passed with no note"
rm -rf "$T/runs"; for r in 1 2; do mkh $r 291f844a43ec; done; mkh 3 unknown
"$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task "$TASK" --model kimi-k3 \
  --arm env+skill --sweep "$SW" >"$T/o" 2>&1
expect 1 $? "but PARTIAL recording is refused: two harness versions in one cell"
rm -rf "$T"


echo
echo "=== regrade_diff: the comparison MANIFEST.md described and nobody implemented"
T=$(mktemp -d); mkdir -p "$T/exp" "$T/got"
hdr="task,model,arm,rep,verdict,score,dice,passed,duration_s,tokens_total"
mkcsv() { printf '%s\n' "$hdr" > "$1/runs_t.csv"
  printf 't,kimi-k3,env-only,1,PASS,%s,%s,True,600,1000\n' "$2" "$3" >> "$1/runs_t.csv"
  printf 't,neurodesk-glm-5.2,env+skill,1,PASS,100,0.98,True,700,2000\n' >> "$1/runs_t.csv"; }
mkcsv "$T/exp" 100 0.9812
mkcsv "$T/got" 100 0.9812
"$PY" "$HERE/regrade_diff.py" --expected "$T/exp" --got "$T/got" --task t >/dev/null 2>&1
expect 0 $? "identical grading passes"

# Fields that differ on every machine must NOT be a finding.
sed -i 's/,600,1000/,9999,4242/' "$T/got/runs_t.csv"
"$PY" "$HERE/regrade_diff.py" --expected "$T/exp" --got "$T/got" --task t >/dev/null 2>&1
expect 0 $? "duration and tokens differing is not a finding"

# The neurodesk- prefix appears in run dirs and not the csv; both sides must join.
mkcsv "$T/got" 100 0.9812
sed -i 's/^t,kimi-k3,/t,neurodesk-kimi-k3,/' "$T/got/runs_t.csv"
"$PY" "$HERE/regrade_diff.py" --expected "$T/exp" --got "$T/got" --task t >/dev/null 2>&1
expect 0 $? "the neurodesk- prefix does not break the join"

mkcsv "$T/got" 100 0.98121      # 4th-decimal drift
"$PY" "$HERE/regrade_diff.py" --expected "$T/exp" --got "$T/got" --task t >"$T/o" 2>&1
expect 0 $? "fourth-decimal dice drift is noise, not failure"
grep -q "noise" "$T/o" && ok "  and is reported rather than hidden" || bad "  silently ignored"

mkcsv "$T/got" 100 0.55         # a real difference
"$PY" "$HERE/regrade_diff.py" --expected "$T/exp" --got "$T/got" --task t >/dev/null 2>&1
expect 1 $? "a real dice difference fails"

mkcsv "$T/got" 0 0.9812; sed -i 's/,PASS,0,/,FAIL,0,/' "$T/got/runs_t.csv"
"$PY" "$HERE/regrade_diff.py" --expected "$T/exp" --got "$T/got" --task t >/dev/null 2>&1
expect 1 $? "a flipped verdict fails"

# The one a field-by-field diff skips: nine runs compared, all equal, ten expected.
mkcsv "$T/got" 100 0.9812; sed -i '/env+skill/d' "$T/got/runs_t.csv"
"$PY" "$HERE/regrade_diff.py" --expected "$T/exp" --got "$T/got" --task t >"$T/o" 2>&1
expect 1 $? "a MISSING run fails rather than comparing the intersection"
grep -q "MISSING" "$T/o" && ok "  and says which" || bad "  did not name it"

"$PY" "$HERE/regrade_diff.py" --expected "$T/exp" --got "$T" --task t >/dev/null 2>&1
expect 1 $? "a missing csv fails instead of comparing nothing"
rm -rf "$T"


# The gate that did not exist until a test run nearly reached the dashboard.
T=$(mktemp -d); TASK=structural-brain-extraction-7t; SW="$T/sweep.json"
cat > "$SW" <<J
{"reps":2,"arms":{"env-only":{"skills_hash":null}},
 "tasks":{"$TASK":{"models":["kimi-k3"],"arms":["env-only"]}}}
J
mklab() { d="$T/runs/${TASK}__kimi-k3__env-only__r$1"; mkdir -p "$d/submissions/$TASK"
  echo x > "$d/submissions/$TASK/output.nii.gz"; echo '{"score":100}' > "$d/envelope.json"
  cat > "$d/run.json" <<J
{"task_id":"$TASK","model":"kimi-k3","condition":"env-only","repeat":"$1","exit_code":0,
 "output_present":true,"skills_seen":[],"skills_installed":"","image_version":"i",
 "opencode_version":"o","skills_sha":"a","skills_hash":"none","prompt_hash":"p",
 "tasks_sha":"t"$2}
J
}
gate_lab() { "$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task "$TASK" \
  --model kimi-k3 --arm env-only --sweep "$SW" >"$T/o" 2>&1; }

rm -rf "$T/runs"; mklab 1 ',"label":"benchmark"'; mklab 2 ',"label":"benchmark"'
gate_lab; expect 0 $? "a cell of benchmark runs passes"

rm -rf "$T/runs"; mklab 1 ',"label":"benchmark"'; mklab 2 ',"label":"exploratory"'
gate_lab; expect 1 $? "ONE exploratory run refuses the whole cell"
grep -q "meant for publication" "$T/o" && ok "  via the publication gate" || bad "  wrong gate"

# Absent is not wrong: every run made before the field existed was real.
rm -rf "$T/runs"; mklab 1 ''; mklab 2 ''
gate_lab; expect 0 $? "runs predating the label field still pass"
grep -q "predate the label field" "$T/o" && ok "  and say so" || bad "  passed silently"
rm -rf "$T"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ] || exit 1
