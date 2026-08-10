#!/bin/bash
# run_bench.sh TASK MODEL CONDITION REPEAT
#
# Runs one benchmark cell headlessly and leaves a submission the grader can score.
#   TASK      e.g. structural-brain-extraction-7t
#   MODEL     e.g. neurodesk/minimax-m2
#   CONDITION env-only | env+skill
#   REPEAT    integer
#
# Invariants that make the arms comparable:
#   - fresh working directory per run (no state leaks between runs)
#   - prompt = task contract + wrapper.txt, byte-identical across arms
#   - the ONLY difference between arms is whether skills are symlinked
set -u

# Secrets. Some Neurodesk images ship a root-owned ~/.bashrc, so the opencode
# wrapper cannot persist NEURODESK_API_KEY there. ~/bench/.env always works.
[ -f "${BENCH_HOME:-$HOME/bench}/.env" ] && . "${BENCH_HOME:-$HOME/bench}/.env"

TASK="$1"; MODEL="$2"; COND="$3"; REP="$4"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"
TASKS="${TASKS_JSON:-$HOME/grader-repo/benchmark/tasks.json}"
SKILLSRC="${SKILLS_SRC:-$HOME/skills-comm/plugins/brain-extraction}"
SKILLDST="${SKILLS_DST:-$HOME/.config/opencode/skills}"
TIMEOUT="${RUN_TIMEOUT:-2700}"

RUN="$BENCH_HOME/runs/${TASK}__${MODEL//\//-}__${COND}__r${REP}"

# --- condition: skills are a GLOBAL path, so set them explicitly every run ---
rm -f "$SKILLDST/brain-extraction" "$SKILLDST/brain-extraction-qc"
if [ "$COND" = "env+skill" ]; then
  mkdir -p "$SKILLDST"
  ln -sfn "$SKILLSRC/brain-extraction"    "$SKILLDST/brain-extraction"
  ln -sfn "$SKILLSRC/brain-extraction-qc" "$SKILLDST/brain-extraction-qc"
fi

# --- fresh working dir. git-annex marks its objects read-only, so chmod first
#     or a leftover datalad clone survives rm -rf and silently contaminates. ---
chmod -R u+w "$RUN" 2>/dev/null
rm -rf "$RUN" 2>/dev/null
if [ -e "$RUN" ]; then echo "FATAL: could not clean $RUN" >&2; exit 1; fi
mkdir -p "$RUN"

python "$HERE/mkprompt.py" "$TASKS" "$TASK" > "$RUN/prompt.txt"
if [ ! -s "$RUN/prompt.txt" ]; then echo "FATAL: empty prompt for $TASK" >&2; exit 1; fi
cat "$HERE/wrapper.txt" >> "$RUN/prompt.txt"

printf '{"task_id":"%s","model":"%s","condition":"%s","repeat":%s,"image_version":"%s","opencode_version":"%s","skills_sha":"%s","tasks_sha":"%s","skills_installed":"%s","start":"%s"}\n' \
  "$TASK" "$MODEL" "$COND" "$REP" "${NEURODESKTOP_VERSION:-unknown}" \
  "$(/usr/bin/opencode --version 2>/dev/null)" \
  "$(git -C "$(dirname "$SKILLSRC")/.." rev-parse --short HEAD 2>/dev/null)" \
  "$(git -C "$(dirname "$(dirname "$TASKS")")" rev-parse --short HEAD 2>/dev/null)" \
  "$(ls -1 "$SKILLDST" 2>/dev/null | tr '\n' ' ')" \
  "$(date -u +%FT%TZ)" > "$RUN/run.json"

echo "[run] $TASK | $MODEL | $COND | r$REP"
# < /dev/null is REQUIRED: backgrounded runs inherit a terminal stdin that goes
# bad once disowned, and opencode dies with "EBADF: bad file descriptor".
timeout "$TIMEOUT" /usr/bin/opencode run --dir "$RUN" -m "$MODEL" "$(cat "$RUN/prompt.txt")" \
  < /dev/null > "$RUN/transcript.txt" 2>&1
RC=$?
echo "  exit=$RC out=$(ls "$RUN/submissions/$TASK/output.nii.gz" 2>/dev/null || echo MISSING)"
