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
# <task>__<model>__<arm>__rN and carry no date and no node, so a re-run of a
# published cell produces exactly the names already in its results tree.
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
# A gate that stages and then refuses has published the thing it refused. Nothing
# asserted this, and the ordering is exactly the class of bug this file exists for.
grep -q 2026-08-05 "$T/stage/$TASK/${TASK}__kimi-k3__env+skill__r1/run.json" \
  && ok "  and the published tree is untouched by a refusal" \
  || bad "  a REFUSED cell still modified results/"

stage_gate yes; expect 0 $? "--replace allows it"
grep -q "3 published run(s) will be overwritten" "$T/out" && ok "  and says how many" || bad "  silent about the overwrite"
grep -q 2026-09-01 "$T/stage/$TASK/${TASK}__kimi-k3__env+skill__r1/run.json" \
  && ok "  and the published run really is the new one" \
  || bad "  --replace reported success and wrote nothing"
# The file the NEW run does not have must not survive from the old one. A no-output
# run has no envelope and is scored 0 rather than excluded, so it reaches staging;
# copying file-by-file would leave the previous run's passing grade attached to it
# and summarize.py would read the pair as a pass.
d="$T/runs/${TASK}__kimi-k3__env+skill__r1"; rm -rf "$d"; mkdir -p "$d"
cat > "$d/run.json" <<J
{"task_id":"$TASK","model":"kimi-k3","condition":"env+skill","repeat":"1","exit_code":1,
 "output_present":false,"skills_seen":[],"skills_installed":"brain-extraction",
 "image_version":"2026-09-01","opencode_version":"1.18.7","skills_sha":"a",
 "skills_hash":"291f844a43ec","prompt_hash":"p","tasks_sha":"t"}
J
stage_gate yes
[ -e "$T/stage/$TASK/${TASK}__kimi-k3__env+skill__r1/envelope.json" ] \
  && bad "  a replaced run kept the previous run's grade" \
  || ok "  and a run with no envelope does not inherit the old one's"

# Identical provenance is still a replacement: the score and the transcript that
# produced it are gone, and n=10 before and after says nothing happened.
stage_gate; expect 1 $? "refuses even when the environment is unchanged"
grep -q "same environment, different run" "$T/out" \
  && ok "  and says so, rather than naming a difference there isn't" \
  || bad "  refused for an unstated reason"

# A published directory holding only envelope.json. The staging loop produces these
# itself -- it copies each file only if the source has one -- so this is reachable,
# and keying the collision check on run.json missed it entirely: a published PASS
# was overwritten by a FAIL with the gate reporting ok.
rm -rf "$T/runs" "$T/stage"
mkdir -p "$T/stage/$TASK/${TASK}__kimi-k3__env+skill__r1"
echo '{"score":100,"verdict":"PASS"}' > "$T/stage/$TASK/${TASK}__kimi-k3__env+skill__r1/envelope.json"
for r in 1 2 3; do mkrun env+skill $r graded brain-extraction; done
stage_gate; expect 1 $? "refuses a published directory with no run.json"
grep -q "missing or unreadable" "$T/out" && ok "  and says it cannot show what changes" || bad "  wrong reason"

# `noskill` is a measurement: the skill directory was checked and was empty. If it
# is filtered as UNRECORDED then a control cell mixing skill-present and
# skill-absent runs passes silently, and the arm gate cannot catch it because
# env-only declares no expected hash.
rm -rf "$T/runs" "$T/stage"
for r in 1 2; do mkrun env-only $r graded "" 2026-08-05 noskill; done
mkrun env-only 3 graded "" 2026-08-05 291f844a43ec
"$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task "$TASK" --model kimi-k3 \
  --arm env-only --sweep "$SW" >"$T/out" 2>&1
expect 1 $? "refuses a control cell where one run had a skill present"
grep -q "skills_hash varies within the cell" "$T/out" && ok "  via 'one environment'" || bad "  refused by the wrong gate"
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
echo "=== finalize_run off_spec: commands count, prose does not"
# Each case changes one thing. The ones that must NOT fire are prose, a download
# inside a CVMFS container, an echoed command, and scripts inside a fetched dataset.
T=$(mktemp -d)
# ESC, for the ANSI codes opencode wraps around each `$ `.
E=$(printf '\033')
fin() { d="$T/$1"; mkdir -p "$d"; echo '{"task_id":"t"}' > "$d/run.json"
  printf '%s\n' "$2" > "$d/transcript.txt"
  "$PY" "$HERE/finalize_run.py" "$d" 0
  "$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['off_spec'])" "$d/run.json" | tr -d '\r'; }
has()  { case "$2" in *"$3"*) ok "$1";; *) bad "$1: got $2";; esac; }
hasnt() { case "$2" in *"$3"*) bad "$1: got $2";; *) ok "$1";; esac; }

o=$(fin a "${E}[0m\$ ${E}[0msudo apt-get install -y -q iptables 2>&1 | tail -2")
has   "flags sudo, through the ANSI codes" "$o" "sudo"
has   "and the package install on the same line" "$o" "package-install"
o=$(fin b '$ apptainer pull synthstrip.sif docker://freesurfer/synthstrip')
has   "flags apptainer pull from Docker Hub" "$o" "external-image"
o=$(fin c "\$ bash -c 'sudo whoami'")
has   "flags sudo inside a quoted bash -c" "$o" "sudo"
o=$(fin d "\$ apptainer exec --bind /a:/b docker://ubuntu:22.04 ls")
has   "flags exec of a remote image, past a flag with a value" "$o" "external-image"

o=$(fin e 'I could run sudo apt-get install iptables, but I will not.
$ echo done')
[ "$o" = "[]" ] && ok "prose mentioning sudo and apt-get is NOT flagged" \
                || bad "prose was flagged: $o"
# A suggested command in the agent's text starts a line exactly as a real one does.
# Only the `$ ` marker separates them.
o=$(fin e2 'You could fix this with:
sudo apt-get install -y iptables
$ echo done')
[ "$o" = "[]" ] && ok "a command the agent only SUGGESTED is not flagged" \
                || bad "suggested command was flagged: $o"
o=$(fin f '$ apptainer exec /cvmfs/neurodesk.ardc.edu.au/containers/fsl_6.0.7.22_20260416/fsl.simg curl -sO https://example.org/x.nii.gz')
hasnt "a download INSIDE a CVMFS container is not an external image" "$o" "external-image"
o=$(fin g '$ echo "docker pull freesurfer/synthstrip"')
hasnt "an echoed docker pull is not a pull" "$o" "external-image"
o=$(fin g2 '$ grep -n "sudo" /etc/group')
hasnt "a quoted word is an argument, not a command" "$o" "sudo"
o=$(fin g3 '$ if true; then sudo apt-get install -y x; fi')
has   "sudo after then is a command" "$o" "sudo"
o=$(fin g4 '$ apptainer pull x.sif "docker://freesurfer/synthstrip"')
has   "a quoted image URI is still an external image" "$o" "external-image"
o=$(fin g5 '$ docker run --rm -v /a:/b freesurfer/synthstrip -h')
has   "docker run pulls an image" "$o" "external-image"

# A script the agent wrote is run by name, so its content is read from disk.
mkdir -p "$T/h"; printf '#!/bin/bash\nsudo apt-get install -y curl\n' > "$T/h/analysis_01.sh"
o=$(fin h '$ bash analysis_01.sh')
has   "reads the scripts the agent wrote, not just the transcript" "$o" "sudo"
mkdir -p "$T/i/data/ds"; printf 'sudo make install\n' > "$T/i/data/ds/setup.sh"
o=$(fin i '$ ls')
[ "$o" = "[]" ] && ok "  but not scripts inside a fetched dataset" || bad "  dataset script counted: $o"

# No command lines: unreadable, reported as None rather than clean.
o=$(fin j 'some output with no command lines at all')
[ "$o" = "None" ] && ok "an unreadable transcript is None, not a clean []" \
                  || bad "unreadable transcript reported as $o"
rm -rf "$T"

echo
echo "=== finalize_run skill loads: only successful calls count"
T=$(mktemp -d)
fk() { d="$T/$1"; mkdir -p "$d"; echo '{"task_id":"t"}' > "$d/run.json"
  printf '%s\n' "$3" > "$d/transcript.txt"
  "$PY" "$HERE/finalize_run.py" "$d" 0
  "$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$d/run.json" "$2" | tr -d '\r'; }
FAILED='✗ Skill "brain-extraction" failed
Error: Skill "brain-extraction" not found. Available skills: customize-opencode'
[ "$(fk a skill_loads "$FAILED")" = 0 ] && ok "a failed call is not a load" || bad "a failed call counted as a load"
[ "$(fk b skill_load_failures "$FAILED")" = 1 ] && ok "  and counts once, not once per line naming the skill" \
                                                || bad "  failure count wrong"
[ "$(fk c skills_available "$FAILED")" = "['customize-opencode']" ] \
  && ok "  and records what opencode offered" || bad "  offered list not parsed"
[ "$(fk d skill_loads '→ Skill "brain-extraction"')" = 1 ] && ok "a marker line naming a skill is a load" \
                                                          || bad "a successful call was not counted"
[ "$(fk e skill_loads 'I will load Skill "brain-extraction" next.')" = 0 ] \
  && ok "prose naming a skill is not a load" || bad "prose counted as a load"
[ "$(fk e2 skill_loads '- Skill "brain-extraction" was not available')" = 0 ] \
  && ok "a markdown bullet naming a skill is not a load" || bad "a markdown bullet counted as a load"
[ "$(fk e3 skill_load_failures '✗ Skill "brain-extraction"')" = 1 ] \
  && ok "the ✗ marker is a failure without the word failed" || bad "an ✗ line not counted as a failure"
[ "$(fk e4 skills_available 'Error: Skill "x" not found. Available skills:')" = "[]" ] \
  && ok "an empty offered list is [] (offered nothing), not None" || bad "an empty offered list read as unknown"
[ "$(fk m1 tools_loaded '$ module load fsl/6.0.7.22 && bet in.nii out')" = "['fsl/6.0.7.22']" ] \
  && ok "tools_loaded: a module loaded in a command counts" || bad "tools_loaded missed a real module load"
[ "$(fk m2 tools_loaded '-#   the container replaces `module load synthstrip/7.4.1`)')" = "[]" ] \
  && ok "  a module named in a printed diff does not" || bad "  a diff line counted as a load"
mkdir -p "$T/m3"; printf '# module load old/1.0\nmodule load ants/2.5.4 fsl/6.0.7.22\n' > "$T/m3/a.sh"
[ "$(fk m3 tools_loaded '$ bash a.sh')" = "['ants/2.5.4', 'fsl/6.0.7.22']" ] \
  && ok "  a script's load line counts, every module on it, and its comments do not" || bad "  script modules wrong: $(fk m3 tools_loaded '$ bash a.sh')"
[ "$(fk m4 tools_loaded '$ ml synthstrip/7.4.1')" = "['synthstrip/7.4.1']" ] \
  && ok "  so does the ml shorthand" || bad "  ml shorthand missed"
o=$(fk f skill_files_read '→ Read /w/skills/brain-extraction/291f844a/brain-extraction/SKILL.md')
has "a Read of SKILL.md is a direct skill-file read" "$o" "SKILL.md"
o=$(fk g skill_files_read '$ cat /w/plugins/brain-extraction/brain-extraction/references/fsl-bet.md')
has "  so is cat of a reference file" "$o" "fsl-bet.md"
o=$(fk g2 skill_files_read '$ git show HEAD:skills/brain-extraction/291f844a/brain-extraction/SKILL.md')
has "  so is git show of a skill file from history" "$o" "brain-extraction/SKILL.md"
[ "$(fk h skill_files_read "\$ find / -path '*/brain-extraction/SKILL.md' | head")" = "[]" ] \
  && ok "  a search that lists paths is not a read" || bad "  a find counted as a read"
rm -rf "$T"

echo
echo "=== summarize: skill delivery and contamination exclusions"
T=$(mktemp -d); TK=structural-brain-extraction
sk() { d="$T/runs/${TK}__glm-5.2__$1__r$2"; mkdir -p "$d/submissions/$TK"
  echo x > "$d/submissions/$TK/output.nii.gz"
  echo '{"score":100,"verdict":"indistinguishable"}' > "$d/envelope.json"
  echo "{\"task_id\":\"$TK\",\"model\":\"glm-5.2\",\"condition\":\"$1\",\"repeat\":$2,\"exit_code\":0,\"output_present\":true,$3}" > "$d/run.json"; }
sk env+skill 1 '"skills_installed":"brain-extraction brain-extraction-qc ","skills_available":["customize-opencode"]'
sk env+skill 2 '"skills_installed":"brain-extraction brain-extraction-qc ","skills_offered":["brain-extraction","brain-extraction-qc"]'
sk env+skill 3 '"skills_installed":"brain-extraction brain-extraction-qc "'
sk env+skill 4 '"skills_installed":"brain-extraction brain-extraction-qc ","skills_offered":["brain-extraction-qc"]'
sk env-only 1 '"skills_installed":"","skill_files_read":["/w/skills/brain-extraction/SKILL.md"]'
sk env-only 4 '"skills_installed":"","skill_files_read":["/w/.agents/skills/hf-cli/SKILL.md"]'
sk env-only 2 '"skills_installed":"","skills_available":["brain-extraction"]'
sk env-only 3 '"skills_installed":""'
"$PY" "$HERE/summarize.py" "$T/runs" "$TK" --out-dir "$T/out" >/dev/null 2>&1
why() { "$PY" -c "import csv,sys
for r in csv.DictReader(open(sys.argv[1])):
    if r['arm']==sys.argv[2] and r['rep']==sys.argv[3]: print(r['exclude_reason'] or 'VALID')" \
  "$T/out/runs_$TK.csv" "$1" "$2" | tr -d '\r'; }
has "skill installed but not offered by opencode: excluded" "$(why env+skill 1)" "not offered"
[ "$(why env+skill 2)" = VALID ] && ok "skill offered, not used: kept (intent to treat)" || bad "offered skill excluded: $(why env+skill 2)"
[ "$(why env+skill 3)" = VALID ] && ok "no offered list recorded (runs before the field): kept" || bad "unrecorded list excluded: $(why env+skill 3)"
has "only part of the skill offered: excluded" "$(why env+skill 4)" "not offered"
[ "$(why env-only 4)" = VALID ] && ok "a control run that read some other skill's file: kept" || bad "other skill counted: $(why env-only 4)"
has "control arm that read skill files: excluded" "$(why env-only 1)" "read skill files"
has "control arm where opencode offered the skill: excluded" "$(why env-only 2)" "misassigned"
[ "$(why env-only 3)" = VALID ] && ok "clean control run: kept" || bad "clean control excluded: $(why env-only 3)"
rm -rf "$T"

echo
echo "=== run_bench: what the agent can see while it runs"
# A fake opencode. `debug skill` offers the built-in plus FAKE_OFFER; `debug config`
# prints FAKE_CONFIG; `run` records the working directory and environment into the
# transcript, the way the real agent would see them.
T=$(mktemp -d)
cat > "$T/opencode" <<'EOF'
#!/bin/bash
[ "$1" = --version ] && { echo 1.18.32; exit 0; }
if [ "$1 $2" = "debug skill" ]; then
  printf '[{"name":"customize-opencode","location":"<built-in>"}'
  for s in $(echo "${FAKE_OFFER:-}" | tr ',' ' '); do printf ',{"name":"%s","location":"/x/%s/SKILL.md"}' "$s" "$s"; done
  echo ']'; exit 0
fi
if [ "$1 $2" = "debug config" ]; then c=${FAKE_CONFIG:-}; [ -n "$c" ] || c='{}'; echo "$c"; exit 0; fi
echo "CWD: $(ls -A "$3" | tr '\n' ' ')"
echo "ENV: ARM=${ARM-unset} SKILLS_SRC=${SKILLS_SRC-unset} RUN_LABEL=${RUN_LABEL-unset} BENCH_HOME=${BENCH_HOME-unset} KEY=${NEURODESK_API_KEY-unset}"
EOF
chmod +x "$T/opencode"
cat > "$T/tasks.json" <<'EOF'
{"categories":{"c":{"tasks":{"t-x":{"prompt":{"goal":"extract","dataset":{"id":"ds1"}}}}}}}
EOF
rb() { a=$1; shift; rm -rf "$T/bench" "$T/skills"
  env BENCH_HOME="$T/bench" TASKS_JSON="$T/tasks.json" OPENCODE_BIN="$T/opencode" \
    SKILLS_SRC="" SKILLS_DST="$T/skills" RUN_TIMEOUT=60 NEURODESK_API_KEY=k-present \
    ARM="$a" RUN_LABEL=exploratory OPENCODE_ISOLATE=0 AGENT_VIEW_CHECK=1 \
    NEURODESK_GATEWAY=http://127.0.0.1:9 "$@" \
    bash "$HERE/run_bench.sh" t-x neurodesk/m "$a" 1 >/dev/null 2>&1; }
rb env-only; rc=$?
R="$T/bench/runs/t-x__neurodesk-m__env-only__r1"
tr_=$(cat "$R/transcript.txt" 2>/dev/null)
expect 0 $rc "a control run whose skills match (none) goes ahead"
hasnt "run.json is not in the agent's working directory" "$tr_" "run.json"
hasnt "prompt.txt is not in the agent's working directory" "$tr_" "prompt.txt"
has   "harness variables are removed from the agent's environment" "$tr_" "ARM=unset SKILLS_SRC=unset RUN_LABEL=unset BENCH_HOME=unset"
has   "  but the gateway key is still there" "$tr_" "KEY=k-present"
[ -s "$R/run.json" ] && [ -s "$R/prompt.txt" ] && ok "both are in the run directory once the agent exits" \
                                               || bad "run.json or prompt.txt missing after the run"
"$PY" -c "import json,sys; r=json.load(open(sys.argv[1])); assert r['exit_code']==0 and r['label']=='exploratory' and r['skills_offered']==[]" \
  "$R/run.json" 2>/dev/null && ok "  and the record is complete, with skills_offered" || bad "  record incomplete"

# The skill arm, with the skill installed where opencode does not discover it.
rb env+skill SKILLS_SRC="$T/src" FAKE_OFFER=""; rc=$?
R="$T/bench/runs/t-x__neurodesk-m__env+skill__r1"
expect 3 $rc "a skill arm whose skill opencode does not offer stops before the agent"
[ ! -e "$R/transcript.txt" ] && ok "  the agent was never started" || bad "  the agent ran anyway"
[ -s "$R/run.json" ] && ok "  and the record still lands in the run directory" || bad "  record left behind in .meta"
rb env+skill SKILLS_SRC="$T/src" FAKE_OFFER="brain-extraction,brain-extraction-qc"
expect 0 $? "the skill arm goes ahead when opencode offers exactly its skills"
[ -z "$(ls "$T/bench/.meta" 2>/dev/null)" ] && ok "  opencode's debug output (it holds the key) is deleted" \
                                           || bad "  left in .meta: $(ls "$T/bench/.meta")"
[ -z "$(ls -A "$T/skills" 2>/dev/null)" ] && ok "  and the skills directory is empty again" \
                                          || bad "  skill links left behind: $(ls -A "$T/skills")"
rb env-only FAKE_OFFER="astra"
expect 3 $? "a control run offered any skill stops"
rb env-only FAKE_CONFIG='{"instructions":["/opt/AGENTS.md"]}'
expect 3 $? "a run whose resolved config declares instructions stops"
rb env-only FAKE_OFFER="astra" AGENT_VIEW_CHECK=0
expect 0 $? "without AGENT_VIEW_CHECK the mismatch is recorded, not enforced"
rm -rf "$T"

echo
echo "=== stratified_effect: effect per task, stratified by model"
T=$(mktemp -d)
"$PY" - "$HERE" > "$T/strat.out" 2>&1 <<'EOF'
import math, sys
sys.path.insert(0, sys.argv[1])
from stratified_effect import mh_rd, exact_stratified_p, holm, bh
from summarize import fisher_exact
def check(name, cond):
    print(("OK:" if cond else "FAIL:") + name)
one = [("m", 1, 10, 7, 10)]
rd, ci = mh_rd(one)
check("one model: the MH difference is the plain difference", abs(rd - 0.6) < 1e-12)
check("one model: the CI is the Wald interval", abs((ci[1] - ci[0]) / 2 - 1.96 * math.sqrt(0.03)) < 1e-9)
check("one model: the exact p is Fisher's", abs(exact_stratified_p(one) - fisher_exact(1, 9, 7, 3)) < 1e-12)
same = [("a", 3, 10, 3, 10), ("b", 7, 10, 7, 10)]
check("identical arms: difference 0, p 1", abs(mh_rd(same)[0]) < 1e-12 and exact_stratified_p(same) > 0.999)
simpson = [("a", 1, 2, 5, 10), ("b", 9, 10, 9, 10)]
pooled = (5 + 9) / 20 - (1 + 9) / 12
check("no difference within any model reads as 0, where pooling reads %+.2f" % pooled,
      abs(mh_rd(simpson)[0]) < 1e-12 and abs(pooled) > 0.1)
check("a model with no passes in either arm leaves the exact p unchanged",
      abs(exact_stratified_p(one + [("z", 0, 10, 0, 10)]) - exact_stratified_p(one)) < 1e-12)
ps = [0.01, 0.04, 0.03, 0.5]
h, b = holm(ps), bh(ps)
check("Holm and BH never lower a p-value, and Holm is the stricter",
      all(x >= p for x, p in zip(h, ps)) and all(y >= p for y, p in zip(b, ps))
      and all(x >= y - 1e-12 for x, y in zip(h, b)))
EOF
while IFS= read -r line; do
  case "$line" in OK:*) ok "${line#OK:}";; FAIL:*) bad "${line#FAIL:}";; esac
done < "$T/strat.out"
# A crash prints no OK lines; count them so a crash cannot pass as silence.
[ "$(grep -c '^OK:\|^FAIL:' "$T/strat.out")" = 7 ] || bad "stratified_effect checks did not all run: $(tail -3 "$T/strat.out")"
rm -rf "$T"

echo
echo "=== stratified_effect --runs: the pre-registered sensitivity analyses"
T=$(mktemp -d); TK=diffusion-brain-mask
# m1: control 2/5 valid + 1 timed out (excluded under the default rules); skill 5/6,
# one pass flagged answer-key and one pass off-spec. m2: control 0/5, skill 5/5.
sv() { d="$T/runs/${TK}__$1__$2__r$3"; mkdir -p "$d/submissions/$TK"
  echo x > "$d/submissions/$TK/output.nii.gz"
  echo "{\"score\":$4,\"verdict\":\"$5\"}" > "$d/envelope.json"
  echo "{\"task_id\":\"$TK\",\"model\":\"$1\",\"condition\":\"$2\",\"repeat\":$3,\"exit_code\":${6:-0},\"output_present\":true,\"skills_installed\":\"$([ "$2" = env+skill ] && echo brain-extraction)\"${7:-}}" > "$d/run.json"
  : > "$d/transcript.txt"; }
P="100 indistinguishable"; F="0 invalid"
sv m1 env-only 1 $P; sv m1 env-only 2 $P; for r in 3 4 5; do sv m1 env-only $r $F; done
sv m1 env-only 6 $P 124
sv m1 env+skill 1 $P 0 ',"answer_key_reads":["→ Read /ws/tasks.json"]'
sv m1 env+skill 2 $P 0 ',"off_spec":["sudo"]'
for r in 3 4 5; do sv m1 env+skill $r $P 0 ',"off_spec":[]'; done
sv m1 env+skill 6 $F 0 ',"off_spec":[]'
for r in 1 2 3 4 5; do sv m2 env-only $r $F; sv m2 env+skill $r $P; done
# m3: 3 runs per arm, below the minimum; left out by summarize and here alike.
for r in 1 2 3; do sv m3 env-only $r $F; sv m3 env+skill $r $P; done
"$PY" "$HERE/summarize.py" "$T/runs" "$TK" --out-dir "$T/out" >/dev/null 2>&1
"$PY" - "$HERE" "$T/out" "$TK" > "$T/sens.out" 2>&1 <<'EOF'
import csv, json, sys
sys.path.insert(0, sys.argv[1])
from stratified_effect import strata_of, strata_from_runs
rows = list(csv.DictReader(open("%s/runs_%s.csv" % (sys.argv[2], sys.argv[3]), encoding="utf-8")))
summ = json.load(open("%s/summary_%s.json" % (sys.argv[2], sys.argv[3]), encoding="utf-8"))
def got(sc):
    return {m: (x0, n0, x1, n1) for m, x0, n0, x1, n1 in strata_from_runs(rows, "env+skill", sc)}
def check(name, cond):
    print(("OK:" if cond else "FAIL:") + name)
prim = set(strata_from_runs(rows, "env+skill", "primary"))
check("primary equals the summary's skill_effect (both models)",
      prim == set(strata_of(summ, "env+skill")) and len(prim) == 2)
check("primary: the timed-out control run is left out", got("primary")["m1"] == (2, 5, 5, 6))
check("exclusions as fail: it counts as a control fail", got("exclusions as fail")["m1"] == (2, 6, 5, 6))
check("exclusions as pass: it counts as a control pass", got("exclusions as pass")["m1"] == (3, 6, 5, 6))
check("flagged removed: the answer-key run is dropped", got("flagged removed")["m1"] == (2, 5, 4, 5))
check("on-spec only: the sudo run is dropped", got("on-spec only")["m1"] == (2, 5, 4, 5))
check("the other model is untouched", all(got(s)["m2"] == (0, 5, 5, 5) for s in
      ("primary", "exclusions as fail", "flagged removed", "on-spec only")))
EOF
while IFS= read -r line; do
  case "$line" in OK:*) ok "${line#OK:}";; FAIL:*) bad "${line#FAIL:}";; esac
done < "$T/sens.out"
[ "$(grep -c '^OK:\|^FAIL:' "$T/sens.out")" = 7 ] || bad "sensitivity checks did not all run: $(tail -3 "$T/sens.out")"
"$PY" "$HERE/stratified_effect.py" --runs "$T/out/runs_$TK.csv" >/dev/null 2>&1
expect 0 $? "the --runs table prints"
rm -rf "$T"

echo
echo "=== model_probe: what the gateway serves, recorded per run"
T=$(mktemp -d)
echo '{"data":[{"id":"qwen3"},{"id":"kimi-k3"}]}' > "$T/roster.json"
echo '{"model":"Qwen/Qwen3-235B","system_fingerprint":"fp_1"}' > "$T/resp.json"
echo '{"model":"Qwen/Qwen3-235B","system_fingerprint":"fp_2"}' > "$T/resp2.json"
echo '{"x-litellm-model-group":"qwen3","x-litellm-model-api-base":"http://10.0.0.5:8000","x-request-id":"a1","x-litellm-response-duration-ms":"123"}' > "$T/hdr.json"
echo '{"x-litellm-model-group":"qwen3","x-litellm-model-api-base":"http://10.0.0.5:8000","x-request-id":"b2","x-litellm-response-duration-ms":"456"}' > "$T/hdr2.json"
mp() { echo '{"task_id":"t"}' > "$T/r.json"
  "$PY" "$HERE/model_probe.py" --model neurodesk/qwen3 --record "$T/r.json" --offline "$T/roster.json" "$1" "$2" >/dev/null 2>&1
  "$PY" -c "import json,sys; r=json.load(open(sys.argv[1])); print(r[sys.argv[2]])" "$T/r.json" "$3" | tr -d '\r'; }
[ "$(mp "$T/resp.json" "$T/hdr.json" gateway_roster)" = "['kimi-k3', 'qwen3']" ] && ok "records the roster" || bad "roster not recorded"
[ "$(mp "$T/resp.json" "$T/hdr.json" model_served)" = "Qwen/Qwen3-235B" ] && ok "records what the gateway served" || bad "model_served not recorded"
f1=$(mp "$T/resp.json" "$T/hdr.json" model_fingerprint); f2=$(mp "$T/resp.json" "$T/hdr2.json" model_fingerprint)
f3=$(mp "$T/resp2.json" "$T/hdr.json" model_fingerprint)
[ -n "$f1" ] && [ "$f1" = "$f2" ] && ok "per-request headers do not change the fingerprint" || bad "fingerprint varies per request: $f1 $f2"
[ "$f1" != "$f3" ] && ok "a changed system_fingerprint does" || bad "fingerprint blind to system_fingerprint"
o=$(mp "$T/resp.json" "$T/hdr.json" model_probe)
hasnt "a backend address is not stored in the clear" "$o" "10.0.0.5"
[ "$(mp "$T/missing.json" "$T/hdr.json" model_fingerprint)" = "None" ] && ok "no response: null, not a guess" || bad "fingerprint from nothing"
echo '{"task_id":"t"}' > "$T/r.json"
"$PY" "$HERE/model_probe.py" --model neurodesk/qwen3 --record "$T/r.json" --gateway http://127.0.0.1:9 >/dev/null 2>&1
expect 0 $? "an unreachable gateway records nulls and does not stop the run"
rm -rf "$T"

echo
echo "=== env_check: the agent's environment matches the spec"
T=$(mktemp -d)
mkdir -p "$T/bin" "$T/home/.config/opencode" "$T/ws/bench/r1" "$T/skills"
printf '#!/bin/sh\necho bet\n' > "$T/bin/bet"; chmod +x "$T/bin/bet"
# Stand-in for Lmod: fsl/6.0.7.22 puts bet on PATH; any other module fails.
cat > "$T/lmod.sh" <<EOF
module() { [ "\$1" = load ] && [ "\$2" = fsl/6.0.7.22 ] || return 1; export PATH="$T/bin:\$PATH"; }
EOF
echo '{"provider":{}}' > "$T/home/.config/opencode/opencode.json"
# envc [BASH_ENV] [VAR=value ...]: extra assignments override the defaults.
envc() { be=${1-$T/lmod.sh}; [ $# -gt 0 ] && shift
  env HOME="$T/home" BASH_ENV="$be" FSL_VERSION=6.0.7.22 \
    BENCH_HOME="$T/ws/bench/r1" SKILLS_DST="$T/skills" PYTHON="$PY" \
    SKILLS_SRC="" SKILL_SEARCH_ROOT="$T/home" "$@" \
    bash "$HERE/env_check.sh" > "$T/out" 2>&1; }
envc; expect 0 $? "passes a matching environment"
envc ""; expect 1 $? "fails with no BASH_ENV"
: > "$T/empty.sh"; envc "$T/empty.sh"; expect 1 $? "fails when module is not defined"
# A `module` that loads anything passes the fsl check; only the unknown-module
# control catches it.
cat > "$T/noop.sh" <<EOF
module() { export PATH="$T/bin:\$PATH"; }
EOF
envc "$T/noop.sh"; expect 1 $? "fails when module accepts any name"
grep -q "accepted a nonexistent module" "$T/out" && ok "  via the nonexistent-module control" || bad "  wrong reason"
echo '{"instructions":["/opt/AGENTS.md"]}' > "$T/home/.config/opencode/opencode.json"
envc; expect 1 $? "fails when opencode declares instructions"
echo '{"provider":{}}' > "$T/home/.config/opencode/opencode.json"
touch "$T/ws/AGENTS.md"; envc; expect 1 $? "fails on AGENTS.md in a parent of BENCH_HOME"
rm "$T/ws/AGENTS.md"
mkdir -p "$T/home/.claude"; touch "$T/home/.claude/CLAUDE.md"
envc; expect 1 $? "fails on CLAUDE.md in the agent's config dir"
rm "$T/home/.claude/CLAUDE.md"
touch "$T/skills/astra"; envc; expect 1 $? "fails when a skill is installed before the arm's"
rm "$T/skills/astra"
mkdir -p "$T/home/.claude/skills/x"; envc; expect 1 $? "fails on a skill in ~/.claude/skills"
rm -r "$T/home/.claude/skills"
mkdir -p "$T/ws/.opencode/skills/x"; envc; expect 1 $? "fails on a project-level skill in a parent"
rm -r "$T/ws/.opencode"
# Skill files readable by path, outside any discovery directory.
mkdir -p "$T/home/work/skills/b/2c1e/brain-extraction"; touch "$T/home/work/skills/b/2c1e/brain-extraction/SKILL.md"
envc; expect 1 $? "fails on a SKILL.md left elsewhere on disk"
envc "$T/lmod.sh" SKILLS_SRC="$T/home/work/skills/b/2c1e"; expect 0 $? "  but not on the arm's own source"
rm -r "$T/home/work"
# A leftover skill in the default directory while SKILLS_DST points elsewhere: the
# skills directory is not the only place opencode looks.
mkdir -p "$T/home/.config/opencode/skills/x"; envc; expect 1 $? "fails on a skill in ~/.config/opencode/skills when SKILLS_DST is elsewhere"
rm -r "$T/home/.config/opencode/skills"
mkdir -p "$T/ws/bench/r1/runs"; touch "$T/ws/bench/r1/runs/AGENTS.md"
envc; expect 1 $? "fails on AGENTS.md in the run directory's own parent"
rm "$T/ws/bench/r1/runs/AGENTS.md"
touch "$T/ws/CONTEXT.md"; envc; expect 1 $? "fails on CONTEXT.md, which opencode also loads"
rm "$T/ws/CONTEXT.md"
echo '{}' > "$T/ws/opencode.json"; envc; expect 1 $? "fails on project-level opencode config above the run"
rm "$T/ws/opencode.json"
envc "$T/lmod.sh" OPENCODE_CONFIG_CONTENT='{"instructions":["x"]}'; expect 1 $? "fails when OPENCODE_CONFIG_CONTENT is set"
envc "$T/lmod.sh" SKILL_SEARCH_ROOT="$T/no-such-dir"; expect 1 $? "fails when the search root does not exist"
envc; expect 0 $? "passes again once each fixture is removed"
rm -rf "$T"

echo
echo "=== redact: secrets masked in text and binary, nothing else touched"
T=$(mktemp -d); mkdir -p "$T/r"
K=sk-test-0123456789abcdef
printf 'token=%s end\n' "$K" > "$T/r/transcript.txt"
printf 'abc\000%s\000xyz' "$K" > "$T/r/opencode.db"
printf 'nothing here\n' > "$T/r/clean.txt"
sz=$(wc -c < "$T/r/opencode.db")
SECRET="$K" "$PY" "$HERE/redact.py" "$T/r" SECRET > "$T/out" 2>&1
expect 0 $? "exits 0 after masking"
grep -rqF "$K" "$T/r" && bad "a copy of the secret survived" || ok "no copy of the secret remains"
[ "$(wc -c < "$T/r/opencode.db")" = "$sz" ] && ok "a binary file keeps its size" \
                                             || bad "a binary file changed size"
grep -q "::warning::" "$T/out" && ok "reports the hit as a warning" || bad "silent about the hit"
[ "$(cat "$T/r/clean.txt")" = "nothing here" ] && ok "a file without the secret is untouched" \
                                                || bad "a clean file changed"
SECRET=short "$PY" "$HERE/redact.py" "$T/r" SECRET >/dev/null 2>&1
expect 2 $? "refuses a value too short to mask safely"
"$PY" "$HERE/redact.py" "$T/r" NOT_SET_ANYWHERE >/dev/null 2>&1
expect 0 $? "skips an unset variable"
# One secret containing another: the longer one must be masked whole.
rm -rf "$T/r"; mkdir -p "$T/r"
printf 'x=sk-abc12345-extension-private\n' > "$T/r/t.txt"
A=sk-abc12345 B=sk-abc12345-extension-private "$PY" "$HERE/redact.py" "$T/r" A B >/dev/null 2>&1
grep -q "extension" "$T/r/t.txt" && bad "part of the longer secret survived" \
                                 || ok "a secret containing another is masked whole"
# A file that cannot be written must not stop the walk or be uploaded unmasked.
rm -rf "$T/r"; mkdir -p "$T/r"
printf 'k=%s\n' "$K" > "$T/r/a_readonly.txt"; chmod 444 "$T/r/a_readonly.txt"
printf 'k=%s\n' "$K" > "$T/r/z_after.txt"
SECRET="$K" "$PY" "$HERE/redact.py" "$T/r" SECRET >/dev/null 2>&1
expect 0 $? "a read-only file does not stop the run"
grep -rqF "$K" "$T/r" && bad "  a copy survived next to a read-only file" \
                      || ok "  and no copy of the secret remains anywhere"
rm -rf "$T"

echo
echo "=== finalize_run answer_key_reads: reads and fetches of grading material"
T=$(mktemp -d)
E=$(printf '\033')
fa() { d="$T/$1"; mkdir -p "$d"; echo '{"task_id":"t"}' > "$d/run.json"
  printf '%s\n' "$3" > "$d/transcript.txt"
  "$PY" "$HERE/finalize_run.py" "$d" 0
  PYTHONIOENCODING=utf-8 "$PY" -c "import json,sys; print(json.load(open(sys.argv[1], encoding='utf-8'))[sys.argv[2]])" \
    "$d/run.json" "$2" | tr -d '\r'; }
o=$(fa a answer_key_reads '→ Read /h/_work/ws/graders/benchmark/tasks.json')
has   "a Read of tasks.json is flagged" "$o" "tasks.json"
o=$(fa b answer_key_reads '$ cat /ws/graders/benchmark/graders/diffusion-brain-mask/rubric.json')
has   "cat of a rubric is flagged" "$o" "rubric.json"
o=$(fa c answer_key_reads '$ python3 -c "import json; json.load(open(\"/ws/tasks.json\"))"')
has   "a python one-liner reading tasks.json is flagged" "$o" "tasks.json"
o=$(fa d answer_key_reads '% WebFetch https://huggingface.co/datasets/neurodeskorg/skills-comm-ground-truth')
has   "fetching the reference dataset is flagged" "$o" "skills-comm-ground-truth"
o=$(fa e answer_key_reads '$ git clone https://github.com/nipreps/skills-comm.git')
has   "cloning skills-comm is flagged" "$o" "skills-comm"
o=$(fa f answer_key_reads '$ huggingface-cli download neurodeskorg/skills-comm-ground-truth --repo-type dataset')
has   "downloading the dataset by repo id is flagged" "$o" "ground-truth"
o=$(fa g answer_key_reads '◈ Exa Web Search "skills-comm benchmark diffusion mask"')
has   "a web search for the benchmark is flagged" "$o" "skills-comm"
mkdir -p "$T/h"; printf 'from huggingface_hub import hf_hub_download\nhf_hub_download(repo_id="neurodeskorg/skills-comm-ground-truth", filename="m.nii.gz")\n' > "$T/h/get.py"
o=$(fa h answer_key_reads '$ python3 get.py')
has   "a script the agent wrote that fetches the dataset is flagged" "$o" "script:"
o=$(fa i answer_key_reads '$ find / -name tasks.json 2>/dev/null | head')
[ "$o" = "[]" ] && ok "searching for tasks.json is not a read" || bad "a find was flagged: $o"
o=$(fa j answer_key_reads 'The grader probably keeps the answer in tasks.json.
$ ls')
[ "$o" = "[]" ] && ok "prose naming tasks.json is not a read" || bad "prose was flagged: $o"
o=$(fa k answer_key_reads '$ cat /home/jovyan/skills-comm/plugins/brain-extraction/brain-extraction/SKILL.md')
[ "$o" = "[]" ] && ok "a local skill path containing skills-comm is not a fetch" || bad "local path flagged: $o"
o=$(fa l answer_key_reads '$ curl -sO https://s3.amazonaws.com/openneuro.org/ds003642/sub-025/anat/x.nii.gz')
[ "$o" = "[]" ] && ok "fetching the task's input dataset is not flagged" || bad "input data flagged: $o"
W=/home/runner/_work/skills-benchmark/skills-benchmark
o=$(fa l2 answer_key_reads "→ Read $W/bench/r1/work/run-x/data/ds002790/README.md
\$ ls -la $W
\$ find /home/runner/_work/skills-benchmark -name '*.nii.gz'
→ Read /home/runner/_work/_temp/prompts.json")
[ "$o" = "[]" ] && ok "paths through a directory named skills-benchmark are not flagged" || bad "honest paths flagged: $o"
o=$(fa l3 answer_key_reads '→ Read /ws/graders/benchmark/graders/diffusion-brain-mask/PROVENANCE.md')
has   "  but a grader's PROVENANCE.md still is" "$o" "PROVENANCE.md"
mkdir -p "$T/l4/venv/lib/site-packages/pkg"
printf 'URL = "https://huggingface.co/datasets/neurodeskorg/skills-comm-ground-truth"\n' > "$T/l4/venv/lib/site-packages/pkg/x.py"
o=$(fa l4 answer_key_reads '$ ls')
[ "$o" = "[]" ] && ok "code inside installed packages is not the agent's script" || bad "site-packages flagged: $o"
d="$T/m"; mkdir -p "$d"; echo '{"task_id":"t"}' > "$d/run.json"; : > "$d/transcript.txt"
"$PY" "$HERE/finalize_run.py" "$d" 0
for k in answer_key_reads arm_seen opencode_errors; do
  o=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])" "$d/run.json" $k | tr -d '\r')
  [ "$o" = "None" ] && ok "an empty transcript: $k is None, not a clean []" || bad "empty transcript: $k is $o"
done

echo
echo "=== finalize_run arm_seen: the agent came across its arm label"
o=$(fa as1 arm_seen '$ ps aux | grep run_bench
runner  41  bash harness/benchmark/runner/run_bench.sh diffusion-brain-mask neurodesk/qwen3 env-only 3')
has   "a process listing naming the arm is recorded" "$o" "env-only"
o=$(fa as2 arm_seen '$ cat /home/runner/_work/_temp/_github_workflow/event.json
{"inputs": {"arm": "env+skill", "snapshot": "291f844a"}}')
has   "  so is the dispatch payload" "$o" "env+skill"
o=$(fa as3 arm_seen '$ ls
data  scripts  submissions
$ python3 -m venv venv-only && ls .venv-skill')
[ "$o" = "[]" ] && ok "a transcript without an arm label is clean, words like venv-only too" || bad "clean transcript flagged: $o"

echo
echo "=== finalize_run: tokens found after the working directory was renamed"
d="$T/tok"; mkdir -p "$d/.xdg-data/opencode"
echo '{"task_id":"t","workdir":"/w/bench/work/run-abc123"}' > "$d/run.json"; echo x > "$d/transcript.txt"
"$PY" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1])
c.execute('create table session (id text, directory text, time_created int, time_updated int, tokens_input int, tokens_output int)')
c.execute(\"insert into session values ('s1','/w/bench/work/run-abc123',1000,61000,500,20)\")
c.commit()" "$d/.xdg-data/opencode/opencode.db"
"$PY" "$HERE/finalize_run.py" "$d" 0
"$PY" -c "import json,sys; r=json.load(open(sys.argv[1])); assert r['tokens_input']==500 and r['session_id']=='s1' and r['session_duration_s']==60" \
  "$d/run.json" 2>/dev/null && ok "the session is found by the recorded workdir" || bad "session not found by workdir: $(cat "$d/run.json")"
echo '{"task_id":"t"}' > "$d/run.json"
"$PY" "$HERE/finalize_run.py" "$d" 0
"$PY" -c "import json,sys; r=json.load(open(sys.argv[1])); assert 'tokens_input' not in r" "$d/run.json" 2>/dev/null \
  && ok "  and without it, a session in another directory is not taken" || bad "  matched a session it should not have"

echo
echo "=== finalize_run opencode_errors: opencode's own errors, not tool errors or output"
o=$(fa n opencode_errors "${E}[91m${E}[1mError: ${E}[0mModel '' was not found")
has   "a standalone opencode error is recorded" "$o" "Model '' was not found"
o=$(fa p opencode_errors "${E}[0m✗ ${E}[0m failed
${E}[91m${E}[1mError: ${E}[0mTool execution aborted")
[ "$o" = "[]" ] && ok "an error following a failed tool call is not opencode's own" || bad "tool error recorded: $o"
o=$(fa q opencode_errors '$ astra paper add x
Error: Unpaywall HTTP error: 429 rate limit')
[ "$o" = "[]" ] && ok "an Error: line in command output is not recorded" || bad "command output recorded: $o"
rm -rf "$T"

echo
echo "=== summarize: a wave's rules decide timeouts, infra errors and control reads"
T=$(mktemp -d); TK=diffusion-brain-mask
cat > "$T/sweep.json" <<J
{"reps":1,"arms":{"env-only":{"skills_hash":null}},
 "waves":{"0":{"tasks":{}},
          "1":{"rules":{"timeout":"fail","infra_errors_from":"opencode","control_skill_reads":"keep"},"tasks":{}}}}
J
# sr ARM REP EXIT OUTPUT(out|none) EXTRA-JSON [TRANSCRIPT]
sr() { d="$T/runs/${TK}__m__$1__r$2"; mkdir -p "$d/submissions/$TK"
  if [ "$4" = out ]; then echo x > "$d/submissions/$TK/output.nii.gz"
    echo '{"score":100,"verdict":"indistinguishable"}' > "$d/envelope.json"; fi
  echo "{\"task_id\":\"$TK\",\"model\":\"m\",\"condition\":\"$1\",\"repeat\":$2,\"exit_code\":$3,\"output_present\":$([ "$4" = out ] && echo true || echo false),\"skills_installed\":\"\"$5}" > "$d/run.json"
  printf '%s\n' "${6:-}" > "$d/transcript.txt"; }
sr env-only 1 124 out ''
sr env-only 2 1 none ',"opencode_errors":[]' 'curl said: 429 rate limit exceeded'
sr env-only 3 1 none ',"opencode_errors":["429 Too Many Requests: rate limit"]'
sr env-only 4 0 out ',"skill_files_read":["/w/skills-comm/plugins/brain-extraction/brain-extraction/SKILL.md"]'
sr env-only 5 0 out ',"answer_key_reads":["→ Read /ws/tasks.json"]'
row() { "$PY" -c "import csv,sys
for r in csv.DictReader(open(sys.argv[1])):
    if r['rep']==sys.argv[2]: print(r['exclude_reason'] or 'VALID', r['passed'], r['flags'])" \
  "$T/$1/runs_$TK.csv" "$2" | tr -d '\r'; }
"$PY" "$HERE/summarize.py" "$T/runs" "$TK" --out-dir "$T/w0" --sweep "$T/sweep.json" --wave 0 >/dev/null 2>&1
"$PY" "$HERE/summarize.py" "$T/runs" "$TK" --out-dir "$T/w1" --sweep "$T/sweep.json" --wave 1 >/dev/null 2>&1
has   "default rules: a timed-out run is excluded" "$(row w0 1)" "timed out"
case "$(row w1 1)" in "VALID False "*) ok "timeout=fail: kept, and not a pass although it scored";; *) bad "timeout=fail: got $(row w1 1)";; esac
v=$("$PY" -c "import csv,sys
for r in csv.DictReader(open(sys.argv[1])):
    if r['rep']=='1': print(r['verdict'], r['score'], r['timed_out'])" "$T/w1/runs_$TK.csv" | tr -d '\r')
[ "$v" = "TIMEOUT 0.0 True" ] && ok "  scored as a failed run: verdict TIMEOUT, score 0" || bad "  timed-out run scored as: $v"
has   "default rules: rate limit in the transcript excludes a no-output run" "$(row w0 2)" "rate limit"
case "$(row w1 2)" in "VALID False"*) ok "infra_errors_from=opencode: agent text alone does not exclude";; *) bad "  got $(row w1 2)";; esac
has   "  but opencode's own rate-limit error does" "$(row w1 3)" "rate limit"
has   "default rules: a control run reading the skill is excluded" "$(row w0 4)" "read skill files"
case "$(row w1 4)" in "VALID True control-read-skill") ok "control_skill_reads=keep: kept and flagged";; *) bad "  got $(row w1 4)";; esac
case "$(row w1 5)" in "VALID True answer-key") ok "an answer-key read is flagged, not excluded";; *) bad "  got $(row w1 5)";; esac
"$PY" -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['rules']['timeout']=='fail'" \
  "$T/w1/summary_$TK.json" 2>/dev/null && ok "the summary records the rules it used" || bad "rules not in the summary"
# Every infra pattern must match an opencode error as finalize_run stores it
# (without the `Error: ` prefix), or that failure is scored against the model.
"$PY" - "$HERE" "$T" > "$T/infra.out" 2>&1 <<'EOF'
import json, os, sys
sys.path.insert(0, sys.argv[1])
from summarize import INFRA_ERROR_RES, load_run
msgs = {"gateway returned empty model name": "Model '' was not found",
        "gateway rate limit": "429 Too Many Requests",
        "gateway 5xx": "502 Bad Gateway",
        "connection/descriptor failure": "connect ECONNREFUSED 10.0.0.1:443",
        "opencode database contention": 'Failed query: insert into "project"',
        "opencode session lost": "Session not found",
        "opencode server error": "Unexpected server error. Check server logs"}
labels = [l for _, l in INFRA_ERROR_RES]
print(("OK:" if sorted(labels) == sorted(msgs) else "FAIL:") + "every infra label has a stored-format message")
for label, m in msgs.items():
    d = os.path.join(sys.argv[2], "infra", label.replace("/", "_").replace(" ", "_"))
    os.makedirs(d, exist_ok=True)
    json.dump({"task_id": "t", "model": "m", "condition": "env-only", "repeat": 1,
               "exit_code": 1, "output_present": False, "opencode_errors": [m]},
              open(os.path.join(d, "run.json"), "w"))
    r = load_run(d, "t", rules={"infra_errors_from": "opencode"})
    print(("OK:" if r["infra_error"] == label else "FAIL:") + "opencode error matches: " + label)
EOF
while IFS= read -r line; do
  case "$line" in OK:*) ok "${line#OK:}";; FAIL:*) bad "${line#FAIL:}";; esac
done < "$T/infra.out"
[ "$(grep -c '^OK:\|^FAIL:' "$T/infra.out")" = 8 ] || bad "infra pattern checks did not all run: $(tail -2 "$T/infra.out")"
sed -i 's/"timeout":"fail"/"timeout":"maybe"/' "$T/sweep.json"
"$PY" "$HERE/summarize.py" "$T/runs" "$TK" --out-dir "$T/w1" --sweep "$T/sweep.json" --wave 1 >/dev/null 2>&1
expect 1 $? "an unknown rule value is refused"
"$PY" "$HERE/summarize.py" "$T/runs" "$TK" --out-dir "$T/w1" --sweep "$T/sweep.json" --wave 7 >/dev/null 2>&1
expect 1 $? "an undeclared wave is refused"
rm -rf "$T"

echo
echo "=== sweep.json: every wave is readable and consistent"
"$PY" - "$HERE" > "$T.sw" 2>&1 <<'EOF'
import json, os, sys
sys.path.insert(0, sys.argv[1])
from summarize import rules_for
sw = json.load(open(os.path.join(sys.argv[1], "..", "ci", "sweep.json"), encoding="utf-8"))
waves = [k for k in sw["waves"] if not k.startswith("_")]
print(("OK:" if waves else "FAIL:") + "waves declared: %s" % waves)
for w in waves:
    rules_for(sw, w)
    tasks = sw["waves"][w]["tasks"]
    bad = [(t, a) for t, s in tasks.items() for a in s["arms"] if a not in sw["arms"]]
    print(("FAIL:" if bad else "OK:") + "wave %s: rules valid, every arm declared %s" % (w, bad or ""))
print(("OK:" if sw["waves"]["1"].get("pins", {}).get("image_version") else "FAIL:")
      + "wave 1 pins its image_version")
EOF
while IFS= read -r line; do
  case "$line" in OK:*) ok "${line#OK:}";; FAIL:*) bad "${line#FAIL:}";; *) bad "sweep check: $line";; esac
done < "$T.sw"
rm -f "$T.sw"

echo
echo "=== assemble_cell --wave: the wave's tasks, pins and rules"
T=$(mktemp -d); TASK=diffusion-brain-mask; SW="$T/sweep.json"
cat > "$SW" <<J
{"reps":2,"arms":{"env-only":{"skills_hash":null}},
 "waves":{"0":{"tasks":{"$TASK":{"models":["m"],"arms":["env-only"]},
                         "only-in-0":{"models":["m"],"arms":["env-only"]}}},
          "1":{"pins":{"image_version":"ci-env2-x","tasks_sha":"e98e3b6"},
               "rules":{"timeout":"fail"},
               "tasks":{"$TASK":{"models":["m"],"arms":["env-only"]}}}}}
J
mw() { d="$T/runs/${TASK}__m__env-only__r$1"; mkdir -p "$d/submissions/$TASK"
  echo x > "$d/submissions/$TASK/output.nii.gz"; echo '{"score":100}' > "$d/envelope.json"
  cat > "$d/run.json" <<J
{"task_id":"$TASK","model":"m","condition":"env-only","repeat":"$1","exit_code":${3:-0},
 "output_present":true,"skills_seen":[],"skills_installed":"","image_version":"${2:-ci-env2-x}",
 "opencode_version":"o","skills_sha":"a","skills_hash":"noskill","prompt_hash":"p","tasks_sha":"e98e3b6"}
J
}
gw() { "$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task "${2:-$TASK}" --model m \
  --arm env-only --sweep "$SW" $1 >"$T/o" 2>&1; }
rm -rf "$T/runs"; mw 1; mw 2
gw "--wave 1"; expect 0 $? "a wave-1 cell in the pinned environment passes"
gw ""; expect 1 $? "a sweep with waves refuses without --wave"
gw "--wave 1" only-in-0; expect 1 $? "a task declared only in another wave is refused"
rm -rf "$T/runs"; mw 1; mw 2 ci-env1-x
gw "--wave 1"; expect 1 $? "a run from another environment is refused"
grep -q "FAIL  the wave's environment" "$T/o" && ok "  via the wave's environment gate" || bad "  refused by the wrong gate"
rm -rf "$T/runs"; mw 1 ci-env1-x; mw 2 ci-env1-x
gw "--wave 1"; expect 1 $? "a consistent cell from another environment is still refused"
grep -q "FAIL  the wave's environment" "$T/o" && ok "  via the wave's environment gate" || bad "  refused by the wrong gate"
rm -rf "$T/runs"; mw 1; mw 2 ci-env2-x 124
gw "--wave 1"; expect 0 $? "wave 1 counts a timeout as a fail, not an exclusion to resolve"
gw "--wave 0"; expect 1 $? "wave 0 rules leave the same timeout unresolved"
grep -q "FAIL  no unresolved exclusions" "$T/o" && grep -q "timed out" "$T/o" \
  && ok "  via the exclusions gate, naming the timeout" || bad "  refused by the wrong gate"
"$PY" "$HERE/assemble_audit.py" --runs "$T/runs" --results "$T/none" --sweep "$SW" >/dev/null 2>&1
expect 1 $? "assemble_audit refuses a sweep with waves without --wave"
"$PY" "$HERE/assemble_audit.py" --runs "$T/runs" --results "$T/none" --sweep "$SW" --wave 1 >/dev/null 2>&1
expect 0 $? "  and runs with it"
rm -rf "$T"

echo
echo "=== wave_check: a run that assemble would refuse is stopped before it is paid for"
T=$(mktemp -d); SW="$T/sweep.json"
cat > "$SW" <<'J'
{"arms":{"env-only":{"skills_hash":null},"env+skill":{"skills_hash":"291f844a43ec"}},
 "waves":{"1":{"pins":{"image_version":"ci-env2-x","tasks_sha":"e98e3b6"},
  "tasks":{"diffusion-brain-mask":{"models":["qwen3"],"arms":["env-only","env+skill"]}}}}}
J
wc_() { "$PY" "$HERE/wave_check.py" --sweep "$SW" "$@" >"$T/o" 2>&1; }
wc_ --wave 1 --label benchmark --task diffusion-brain-mask --model neurodesk/qwen3 --arm env+skill \
  --skills-hash 291f844a43ec image_version=ci-env2-x tasks_sha=e98e3b6
expect 0 $? "a declared cell in the pinned environment passes"
wc_ --wave 1 --label benchmark --task diffusion-brain-mask --model neurodesk/qwen3 --arm env+skill \
  --skills-hash 5ba66f0c7ddf image_version=ci-env2-x tasks_sha=e98e3b6
expect 1 $? "a skill arm dispatched with another snapshot is stopped"
wc_ --wave 1 --label benchmark --task diffusion-brain-mask --model neurodesk/qwen3 --arm env-only \
  --skills-hash "" image_version=ci-env2-x tasks_sha=e98e3b6
expect 0 $? "  the control arm needs no snapshot"
wc_ --wave 1 image_version=ci-env1-x tasks_sha=e98e3b6
expect 1 $? "another environment is stopped"
wc_ --wave 1 image_version=ci-env2-x
expect 1 $? "a pin with no value to check is stopped"
wc_ --wave 2 image_version=ci-env2-x tasks_sha=e98e3b6
expect 1 $? "an undeclared wave is stopped"
wc_ --wave 1 --label benchmark --task diffusion-brain-mask --model neurodesk/kimi-k3 --arm env-only \
  image_version=ci-env2-x tasks_sha=e98e3b6
expect 1 $? "a benchmark run of an undeclared model is stopped"
grep -q "model kimi-k3 is not declared" "$T/o" && ok "  and says which" || bad "  wrong reason: $(cat "$T/o")"
wc_ --wave 1 --label exploratory --task other-task --model neurodesk/kimi-k3 --arm env-only \
  image_version=ci-env2-x tasks_sha=e98e3b6
expect 0 $? "an exploratory run needs only the pins"
"$PY" "$HERE/wave_check.py" --sweep "$HERE/../ci/sweep.json" --wave 1 --label benchmark \
  --task diffusion-brain-mask --model neurodesk/NRP.qwen3 --arm env+skill --skills-hash 291f844a43ec \
  image_version=ci-env3-apptainer1.4.3-fsl6.0.7.22-opencode1.18.32 tasks_sha=e98e3b6 >/dev/null 2>&1
expect 0 $? "the real sweep accepts the environment run.yml declares for wave 1"
rm -rf "$T"

echo
echo "=== model fingerprint: the run stops, and the cell is refused, when the model changed"
T=$(mktemp -d); SW="$T/sweep.json"
cat > "$SW" <<'J'
{"reps":2,"arms":{"env-only":{"skills_hash":null}},
 "waves":{"1":{"model_fingerprints":{"_comment":["x"],"NRP.qwen3":"757ac9b30f73"},
  "tasks":{"diffusion-brain-mask":{"models":["NRP.qwen3","m"],"arms":["env-only"]}}}}}
J
fp() { echo "{\"model_fingerprint\":$1}" > "$T/r.json"
  "$PY" "$HERE/wave_check.py" --sweep "$SW" --wave 1 --model "$2" --record "$T/r.json" --fingerprint-only >"$T/o" 2>&1; }
fp '"757ac9b30f73"' neurodesk/NRP.qwen3; expect 0 $? "the declared fingerprint passes"
fp '"c0e7d1c7d27c"' neurodesk/NRP.qwen3; expect 1 $? "another fingerprint (engine changed) is stopped"
grep -q "engine changed" "$T/o" && ok "  and says why" || bad "  wrong reason: $(cat "$T/o")"
fp 'null' neurodesk/NRP.qwen3; expect 1 $? "no fingerprint recorded (probe failed) is stopped"
fp '"anything"' neurodesk/m; expect 0 $? "a model the wave declares no fingerprint for is not checked"
fp '"757ac9b30f73"' neurodesk/NRP.qwen3
"$PY" "$HERE/wave_check.py" --sweep "$SW" --wave 9 --model neurodesk/NRP.qwen3 --record "$T/r.json" --fingerprint-only >/dev/null 2>&1
expect 1 $? "an undeclared wave is stopped in fingerprint-only mode"
# assemble: one fingerprint per cell, none changed during a rep, the declared one.
mf() { d="$T/runs/diffusion-brain-mask__neurodesk-NRP.qwen3__env-only__r$1"; mkdir -p "$d/submissions/diffusion-brain-mask"
  echo x > "$d/submissions/diffusion-brain-mask/output.nii.gz"; echo '{"score":100}' > "$d/envelope.json"
  cat > "$d/run.json" <<J
{"task_id":"diffusion-brain-mask","model":"neurodesk/NRP.qwen3","condition":"env-only","repeat":"$1",
 "exit_code":0,"output_present":true,"skills_seen":[],"skills_installed":"","image_version":"i",
 "opencode_version":"o","skills_sha":"a","skills_hash":"noskill","prompt_hash":"p","tasks_sha":"t",
 "model_fingerprint":"$2","model_changed":${3:-false}}
J
}
ga() { "$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task diffusion-brain-mask --model NRP.qwen3 \
  --arm env-only --sweep "$SW" --wave 1 >"$T/o" 2>&1; }
rm -rf "$T/runs"; mf 1 757ac9b30f73; mf 2 757ac9b30f73
ga; expect 0 $? "a cell on the declared fingerprint passes"
rm -rf "$T/runs"; mf 1 757ac9b30f73; mf 2 c0e7d1c7d27c
ga; expect 1 $? "a cell answered by two fingerprints is refused"
grep -q "FAIL  one model" "$T/o" && ok "  via the one-model gate" || bad "  refused by the wrong gate"
# With no fingerprint declared, only the mixed-fingerprint check can refuse.
sed 's/"NRP.qwen3":"757ac9b30f73"/"other":"x"/' "$SW" > "$T/sw_nodecl.json"
rm -rf "$T/runs"; mf 1 757ac9b30f73; mf 2 c0e7d1c7d27c
"$PY" "$HERE/assemble_cell.py" --runs "$T/runs" --task diffusion-brain-mask --model NRP.qwen3 \
  --arm env-only --sweep "$T/sw_nodecl.json" --wave 1 >"$T/o" 2>&1
expect 1 $? "two fingerprints are refused even when the wave declares none"
grep -q "varies within the cell" "$T/o" && ok "  by the mixed-fingerprint check" || bad "  wrong reason"
rm -rf "$T/runs"; mf 1 c0e7d1c7d27c; mf 2 c0e7d1c7d27c
ga; expect 1 $? "a consistent cell on an undeclared fingerprint is refused"
rm -rf "$T/runs"; mf 1 757ac9b30f73; mf 2 757ac9b30f73 true
ga; expect 1 $? "a rep whose fingerprint changed during the run is refused"
grep -q "changed during rep" "$T/o" && ok "  and names the rep" || bad "  wrong reason"
rm -rf "$T"

echo
echo "=== mkprompt --prompts-only: the task pack without its answers"
T=$(mktemp -d)
cat > "$T/tasks.json" <<'EOF'
{"benchmark":"b","categories":{"c":{"tasks":{"t-x":{"title":"T","description":"uses bet",
 "grading":{"x":1},"prompt":{"goal":"extract","dataset":{"id":"ds1","subjects":["01"]},
 "required_output":"write out.nii.gz"},"solution":{"reference":"secret-mask"}}}}}}
EOF
"$PY" "$HERE/mkprompt.py" --prompts-only "$T/tasks.json" > "$T/p.json"
grep -q "secret-mask\|solution\|grading\|uses bet" "$T/p.json" && bad "answer material kept in the prompt-only file" \
                                                           || ok "solution, grading and description are dropped"
[ "$("$PY" "$HERE/mkprompt.py" "$T/tasks.json" t-x | md5sum)" = "$("$PY" "$HERE/mkprompt.py" "$T/p.json" t-x | md5sum)" ] \
  && ok "  and the prompt built from it is byte-identical" || bad "  the prompt changed"
rm -rf "$T"

echo
echo "=== model_probe --phase end: a model that changes during the run"
T=$(mktemp -d)
echo '{"data":[{"id":"qwen3"}]}' > "$T/roster.json"
echo '{"model":"Qwen/Qwen3-235B","system_fingerprint":"fp_1"}' > "$T/r1.json"
echo '{"model":"Qwen/Qwen3-235B","system_fingerprint":"fp_2"}' > "$T/r2.json"
echo '{"x-litellm-model-group":"qwen3","x-litellm-attempted-fallbacks":"1"}' > "$T/h.json"
pe() { echo '{"task_id":"t"}' > "$T/r.json"
  "$PY" "$HERE/model_probe.py" --model neurodesk/qwen3 --record "$T/r.json" --offline "$T/roster.json" "$1" "$T/h.json" >/dev/null 2>&1
  "$PY" "$HERE/model_probe.py" --model neurodesk/qwen3 --record "$T/r.json" --offline "$T/roster.json" "$2" "$T/h.json" --phase end >/dev/null 2>&1
  "$PY" -c "import json,sys; r=json.load(open(sys.argv[1])); print(r[sys.argv[2]])" "$T/r.json" "$3" | tr -d '\r'; }
[ "$(pe "$T/r1.json" "$T/r1.json" model_changed)" = False ] && ok "the same model before and after: model_changed False" || bad "unchanged model flagged"
[ "$(pe "$T/r1.json" "$T/r2.json" model_changed)" = True ] && ok "a different fingerprint after: model_changed True" || bad "a change was missed"
[ "$(pe "$T/r1.json" "$T/missing.json" model_changed)" = None ] && ok "no probe after: None, not False" || bad "a missing probe read as no change"
[ "$(pe "$T/r1.json" "$T/r1.json" model_fingerprint)" != None ] && ok "  and the start fields are kept" || bad "  the start fingerprint was overwritten"
o=$(pe "$T/r1.json" "$T/r1.json" model_probe)
has   "fallback headers are recorded as routing" "$o" "attempted-fallbacks"
rm -rf "$T"

echo
echo "=== env_record: the run's software environment"
T=$(mktemp -d); echo '{"task_id":"t"}' > "$T/r.json"
CVMFS_ROOT="$T/no-cvmfs" HARNESS_SHA=abc1234 BENCH_SESSION_ID=s123 GITHUB_RUN_ID=9 GITHUB_RUN_ATTEMPT=2 GITHUB_JOB=rep \
  "$PY" "$HERE/env_record.py" --record "$T/r.json" >/dev/null 2>&1
expect 0 $? "exits 0"
"$PY" -c "import json,sys; r=json.load(open(sys.argv[1]))
assert r['cvmfs_revision'] is None and r['harness_sha']=='abc1234' and r['ci_run']=='9/2/rep' and r['session_id_sent']=='s123'
assert 'python_packages' in r and 'kernel' in r" "$T/r.json" 2>/dev/null \
  && ok "records harness, CI run and session id; an unreadable CVMFS revision is None" || bad "record wrong: $(cat "$T/r.json")"
rm -rf "$T"

echo
echo "=== run_bench: neutral working directory, records around the agent"
T=$(mktemp -d)
cat > "$T/opencode" <<'EOF'
#!/bin/bash
[ "$1" = --version ] && { echo 1.18.32; exit 0; }
if [ "$1 $2" = "debug skill" ]; then echo '[{"name":"customize-opencode","location":"<built-in>"}]'; exit 0; fi
if [ "$1 $2" = "debug config" ]; then echo '{}'; exit 0; fi
echo "DIR: $3"
echo "CWD: $(ls -A "$3" | tr '\n' ' ')"
echo "META: $(ls -A "$(dirname "$(dirname "$3")")/.meta" | tr '\n' ' ')"
echo "RECORD: $(cat "$(dirname "$(dirname "$3")")"/.meta/*.run.json)"
echo "ENV: SID=${BENCH_SESSION_ID-unset} HARNESS_SHA=${HARNESS_SHA-unset} TASKS_SHA=${TASKS_SHA-unset} GRADER_REPO=${GRADER_REPO-unset} HARNESS_REF=${HARNESS_REF-unset} GITHUB_EVENT_PATH=${GITHUB_EVENT_PATH-unset} RUNNER_TEMP=${RUNNER_TEMP-unset}"
echo made > "$3/out.txt"; ln -s "$3/out.txt" "$3/link.txt"
# Left running after the agent exits, in the working directory.
(cd "$3" && sleep 3 && echo late > late.txt) > /dev/null 2>&1 &
EOF
chmod +x "$T/opencode"
echo '{"categories":{"c":{"tasks":{"t-x":{"prompt":{"goal":"extract","dataset":{"id":"ds1"}}}}}}}' > "$T/tasks.json"
env BENCH_HOME="$T/bench" TASKS_JSON="$T/tasks.json" OPENCODE_BIN="$T/opencode" \
  SKILLS_SRC="" SKILLS_DST="$T/skills" RUN_TIMEOUT=60 NEURODESK_API_KEY=k \
  OPENCODE_ISOLATE=0 AGENT_VIEW_CHECK=1 NEURODESK_GATEWAY=http://127.0.0.1:9 \
  HARNESS_SHA=abc1234 TASKS_SHA=e98e3b6 GRADER_REPO=owner/tasks HARNESS_REF=deadbeef \
  GITHUB_EVENT_PATH=/x/event.json RUNNER_TEMP=/x/temp RUN_WAVE=1 \
  bash "$HERE/run_bench.sh" t-x neurodesk/m env-only 1 >/dev/null 2>&1
R="$T/bench/runs/t-x__neurodesk-m__env-only__r1"
tr_=$(cat "$R/transcript.txt" 2>/dev/null)
dir_=$(echo "$tr_" | sed -n 's/^DIR: //p')
case "$dir_" in */work/run-*) ok "the agent worked in $(basename "$dir_")";; *) bad "working dir: $dir_";; esac
hasnt "  whose path names no arm" "$dir_" "env-only"
hasnt "  no model" "$dir_" "neurodesk-m"
hasnt "  and no task" "$dir_" "t-x"
hasnt "the transcript is not in the working directory while the agent runs" "$tr_" "CWD: transcript"
meta_=$(echo "$tr_" | sed -n 's/^META: //p')
case "$meta_" in *run-*.transcript.txt*) ok "the files kept aside during the run are there under the neutral name";; *) bad "meta listing: $meta_";; esac
hasnt "  and their names do not name the arm" "$meta_" "env-only"
hasnt "  or the task" "$meta_" "t-x"
hasnt "  opencode's debug output is gone before the agent starts" "$meta_" "config.json"
rec_=$(echo "$tr_" | sed -n 's/^RECORD: //p')
case "$rec_" in *'"workdir"'*) ok "the record the agent could find holds the environment";; *) bad "record not readable in the test: $rec_";; esac
hasnt "  but not the arm" "$rec_" "env-only"
hasnt "  the task" "$rec_" "t-x"
hasnt "  or the model" "$rec_" "neurodesk/m"
has   "harness and CI variables are removed from the agent's environment" "$tr_" \
      "HARNESS_SHA=unset TASKS_SHA=unset GRADER_REPO=unset HARNESS_REF=unset GITHUB_EVENT_PATH=unset RUNNER_TEMP=unset"
sid=$(echo "$tr_" | sed -n 's/.*SID=\([^ ]*\).*/\1/p')
"$PY" -c "import json,sys; r=json.load(open(sys.argv[1]))
assert r['session_id_sent']==sys.argv[2] and len(sys.argv[2])==32
assert r['workdir'].endswith(sys.argv[3]) and r['harness_sha']=='abc1234'
assert 'model_changed' in r and 'model_fingerprint_end' in r" "$R/run.json" "$sid" "$(basename "$dir_")" 2>/dev/null \
  && ok "the record holds the session id the agent had, the workdir, and the end probe" \
  || bad "record incomplete: sid=$sid"
[ -d "$R" ] && [ ! -d "$R/.meta" ] && [ -s "$R/transcript.txt" ] \
  && ok "  and the working directory became the run directory" \
  || bad "  working directory left behind or transcript missing"
# Only where ln -s makes real symlinks (Git Bash on Windows copies instead).
if [ -L "$R/link.txt" ]; then
  [ "$(cat "$R/link.txt" 2>/dev/null)" = made ] && ok "  an absolute link the agent made still resolves after the rename" \
                                                || bad "  the agent's absolute link dangles after the rename"
else
  echo "  skip  symlink check: ln -s does not make symlinks here"
fi
"$PY" -c "import json,sys; r=json.load(open(sys.argv[1]))
assert r['wave']=='1' and r['condition']=='env-only' and r['task_id']=='t-x' and r['model']=='neurodesk/m'
assert r['skills_hash']=='noskill' and r['skills_offered']==[]" "$R/run.json" 2>/dev/null \
  && ok "  the landed record names the run: task, model, arm, wave" || bad "  identity fields missing after landing: $(cat "$R/run.json")"
sleep 4
[ ! -e "$R/late.txt" ] && ok "a process the agent left running is stopped when it exits" \
                       || bad "a background process kept writing after the run"
rm -rf "$T"

echo
echo "=== run_bench: a model the wave declares must match its fingerprint before the agent"
T=$(mktemp -d)
cat > "$T/opencode" <<'EOF'
#!/bin/bash
[ "$1" = --version ] && { echo 1.18.32; exit 0; }
if [ "$1 $2" = "debug skill" ]; then echo '[{"name":"customize-opencode","location":"<built-in>"}]'; exit 0; fi
if [ "$1 $2" = "debug config" ]; then echo '{}'; exit 0; fi
echo "AGENT STARTED"
EOF
chmod +x "$T/opencode"
echo '{"categories":{"c":{"tasks":{"t-x":{"prompt":{"goal":"extract","dataset":{"id":"ds1"}}}}}}}' > "$T/tasks.json"
rbf() { env BENCH_HOME="$T/bench" TASKS_JSON="$T/tasks.json" OPENCODE_BIN="$T/opencode" \
    SKILLS_SRC="" SKILLS_DST="$T/skills" RUN_TIMEOUT=60 NEURODESK_API_KEY=k \
    OPENCODE_ISOLATE=0 AGENT_VIEW_CHECK=1 NEURODESK_GATEWAY=http://127.0.0.1:9 RUN_WAVE=1 \
    bash "$HERE/run_bench.sh" t-x "$1" env-only 1 >/dev/null 2>&1; }
# The gateway is unreachable, so the probe records no fingerprint: a model whose
# fingerprint wave 1 declares must stop; one it does not declare goes ahead.
rbf neurodesk/NRP.qwen3; expect 3 $? "a declared model with no confirmed fingerprint stops"
grep -q "AGENT STARTED" "$T/bench/runs/t-x__neurodesk-NRP.qwen3__env-only__r1/transcript.txt" 2>/dev/null \
  && bad "  the agent ran anyway" || ok "  before the agent started"
rbf neurodesk/m; expect 0 $? "a model with no declared fingerprint goes ahead"
rm -rf "$T"

echo
echo "=== env_check: no grading material on disk"
T=$(mktemp -d)
mkdir -p "$T/bin" "$T/home/.config/opencode" "$T/ws/bench/r1"
printf '#!/bin/sh\necho bet\n' > "$T/bin/bet"; chmod +x "$T/bin/bet"
cat > "$T/lmod.sh" <<EOF
module() { [ "\$1" = load ] && [ "\$2" = fsl/6.0.7.22 ] || return 1; export PATH="$T/bin:\$PATH"; }
EOF
ek() { env HOME="$T/home" BASH_ENV="$T/lmod.sh" FSL_VERSION=6.0.7.22 \
    BENCH_HOME="$T/ws/bench/r1" PYTHON="$PY" SKILLS_SRC="" SKILL_SEARCH_ROOT="$T/home" \
    bash "$HERE/env_check.sh" > "$T/out" 2>&1; }
ek; expect 0 $? "passes with no grading material"
mkdir -p "$T/home/tmp"; echo '{"categories":{"c":{"tasks":{"t":{"prompt":{}}}}}}' > "$T/home/tmp/tasks.json"
ek; expect 0 $? "  and with the prompt-only task pack"
echo '{"categories":{"c":{"tasks":{"t":{"prompt":{},"solution":{}}}}}}' > "$T/home/tmp/tasks.json"
ek; expect 1 $? "fails on a task pack with solution blocks"
grep -q "grading material readable" "$T/out" && ok "  and says why" || bad "  wrong reason"
rm "$T/home/tmp/tasks.json"
mkdir -p "$T/home/g/diffusion"; touch "$T/home/g/diffusion/rubric.json"
ek; expect 1 $? "fails on a rubric"
rm -r "$T/home/g"; mkdir -p "$T/home/h/benchmark/runner"; touch "$T/home/h/benchmark/runner/CLAIMS.md"
ek; expect 1 $? "fails on CLAIMS.md"
rm -r "$T/home/h"; mkdir -p "$T/ws/bench/r1/runs/old"; touch "$T/ws/bench/r1/runs/old/envelope.json"
env HOME="$T/home" BASH_ENV="$T/lmod.sh" FSL_VERSION=6.0.7.22 BENCH_HOME="$T/ws/bench/r1" \
  PYTHON="$PY" SKILLS_SRC="" SKILL_SEARCH_ROOT="$T/ws" bash "$HERE/env_check.sh" >/dev/null 2>&1
expect 1 $? "fails on a graded run left from an earlier job"
rm -r "$T/ws/bench/r1/runs"
mkdir -p "$T/ws/bench/r1/work"; touch "$T/ws/bench/r1/work/AGENTS.md"
ek; expect 1 $? "fails on AGENTS.md where the agent's working directory is created"
rm -rf "$T"

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" = 0 ] || exit 1
