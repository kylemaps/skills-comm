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
# Default only when unset. Empty means no skill source (env-only in CI).
SKILLSRC="${SKILLS_SRC-$HOME/skills-comm/plugins/brain-extraction}"
SKILLDST="${SKILLS_DST:-$HOME/.config/opencode/skills}"
# The wall. It was 2700 by default, and that default was a trap: the working value
# lives in ~/bench/.env on the VM, which exists on exactly one machine. A fresh pod
# has no .env, so CI would have fallen back to 45 minutes -- the wall that sat INSIDE
# the run-time distribution and scored 51 runs across four tasks as model failures.
# The fix that lasts is a default above the distribution, not a note telling people
# to set it.
#
# Measured over 409 completed runs: median 13.2 min, p90 37.5, p99 56.2, max 82.5.
# 5400 clears every run we have ever completed. A wall is only safe when it sits
# outside the distribution -- inside it, it censors, and the censoring is invisible
# because a truncated run looks exactly like a model that produced nothing.

TIMEOUT="${RUN_TIMEOUT:-5400}"
[ -n "${RUN_TIMEOUT:-}" ] || echo "note: RUN_TIMEOUT unset, using the ${TIMEOUT}s default" >&2

# The agent binary. Absolute on the VM because the neurodesktop image ships a
# wrapper called `opencode` earlier on PATH that is not the CLI. In CI there is
# no such wrapper and npm -g installs to the node toolchain, so /usr/bin/opencode
# does not exist -- the first real cluster run died with exit=127 out=MISSING.
# Overridable, defaulting to the VM behaviour so that machine is unaffected.
OPENCODE_BIN="${OPENCODE_BIN:-/usr/bin/opencode}"

# A truncated task id (`structural-br-extraction-stroke`) once produced a run
# directory and fourteen graded outputs under a name no grader pack contains. The
# runs looked fine and belonged to nothing. The id is the join key between the
# prompt, the grader and the results, so it is checked before a single token is
# spent rather than discovered when the results will not line up. Checked only when
# the pack is readable: an unreadable pack is a different failure, reported later
# by the code that needs it.
if [ -f "$TASKS" ] && ! grep -q "\"$TASK\"" "$TASKS"; then
  echo "ABORT: task id '$TASK' is not in $TASKS." >&2
  echo "       A near-miss id yields runs that no grader can score." >&2
  exit 2
fi

NAME="${TASK}__${MODEL//\//-}__${COND}__r${REP}"
RUN="$BENCH_HOME/runs/$NAME"

# --- condition: skills are a GLOBAL path, so set them explicitly every run ---
# Any arm named env+skill* installs the skill; the suffix names WHICH skill, e.g.
# `env+skill-michele`. Without that, two different skills produce identical run
# directory names, the second overwrites the first, and a head-to-head between
# two skills against one shared baseline is impossible to express.
# Avoid ':' as the separator -- tar and rsync read `foo:bar` as a remote path.
rm -f "$SKILLDST/brain-extraction" "$SKILLDST/brain-extraction-qc"
case "$COND" in
  env+skill*)
    mkdir -p "$SKILLDST"
    ln -sfn "$SKILLSRC/brain-extraction"    "$SKILLDST/brain-extraction"
    ln -sfn "$SKILLSRC/brain-extraction-qc" "$SKILLDST/brain-extraction-qc"
    ;;
esac

# --- fresh directories. git-annex marks its objects read-only, so chmod first
#     or a leftover datalad clone survives rm -rf and silently contaminates. ---
chmod -R u+w "$RUN" 2>/dev/null
rm -rf "$RUN" 2>/dev/null
if [ -e "$RUN" ]; then echo "FATAL: could not clean $RUN" >&2; exit 1; fi
mkdir -p "$BENCH_HOME/runs" "$BENCH_HOME/work"
# The agent works in a directory whose name says nothing about the run (not the arm,
# model or task). It is renamed to $RUN when the run ends.
# Resolved, as opencode records it.
WORK=$(mktemp -d "$BENCH_HOME/work/run-XXXXXX") && WORK=$(cd "$WORK" && pwd -P) \
  && [ -n "$WORK" ] || {
  echo "FATAL: could not create a working directory under $BENCH_HOME/work" >&2; exit 1; }

# The run record, prompt and transcript are kept outside the agent's working
# directory while it runs, under the working directory's neutral name, and moved into
# $RUN when it exits. While the agent runs, the record holds nothing that names the
# run (task, model, arm, skill source): those fields (IDENT) are added when it lands.
META="$BENCH_HOME/.meta"
mkdir -p "$META"
ID=$(basename "$WORK")
PROMPT="$META/$ID.prompt.txt"
RECORD="$META/$ID.run.json"
TRANSCRIPT="$META/$ID.transcript.txt"
IDENT=""
# The working directory becomes $RUN, and the record, prompt and transcript move into
# it. A symlink from the old path keeps absolute paths the agent wrote (scripts,
# links to its output) resolving. Safe to call more than once.
land() {
  if [ -d "$WORK" ] && [ ! -L "$WORK" ] && [ ! -e "$RUN" ]; then
    mv "$WORK" "$RUN" && ln -s "$RUN" "$WORK"
  fi
  mkdir -p "$RUN"
  if [ -f "$RECORD" ] && [ -n "$IDENT" ]; then
    python -c "import json,sys; r=json.load(open(sys.argv[1])); r.update(json.loads(sys.argv[2])); json.dump(r, open(sys.argv[1], 'w')); open(sys.argv[1], 'a').write('\n')" \
      "$RECORD" "$IDENT" 2>/dev/null
  fi
  [ -f "$RECORD" ] && mv -f "$RECORD" "$RUN/run.json"
  [ -f "$PROMPT" ] && mv -f "$PROMPT" "$RUN/prompt.txt"
  [ -f "$TRANSCRIPT" ] && mv -f "$TRANSCRIPT" "$RUN/transcript.txt"
  return 0
}
# Processes the agent left running are stopped, so nothing changes the output or
# reads the graders after the run: `timeout`'s process group, and any process whose
# working directory is still inside $WORK (opencode can start commands in process
# groups of their own).
stop_agent() {
  [ -n "${AGENT_PID:-}" ] && kill -KILL -- "-$AGENT_PID" 2>/dev/null
  unset AGENT_PID
  for p in /proc/[0-9]*; do
    case "$(readlink "$p/cwd" 2>/dev/null)" in
      "$WORK"|"$WORK"/*) [ "${p#/proc/}" != "$$" ] && kill -KILL "${p#/proc/}" 2>/dev/null ;;
    esac
  done
  return 0
}
# On any exit (normal, abort or signal): the agent's processes are stopped, the run
# lands in $RUN, the opencode debug output (which contains the resolved gateway key)
# is deleted if still there, and the arm's skill links are removed so the skills
# directory is empty.
trap 'stop_agent
      land
      rm -f "$META/$ID.skills.json" "$META/$ID.config.json"
      rm -f "$SKILLDST/brain-extraction" "$SKILLDST/brain-extraction-qc"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

python "$HERE/mkprompt.py" "$TASKS" "$TASK" > "$PROMPT"
if [ ! -s "$PROMPT" ]; then echo "FATAL: empty prompt for $TASK" >&2; exit 1; fi
cat "$HERE/wrapper.txt" >> "$PROMPT"

# skills_sha only means anything when SKILLS_SRC is inside our git checkout. When we
# test a skill delivered some other way -- an unzipped drop from a collaborator, say --
# the commit is unchanged while the skill content is completely different, so the two
# experiments would be indistinguishable in the record. Hash the skill files as well.
# LC_ALL=C on BOTH find and sort. run.yml verifies the snapshot's hash with a
# C-collated sort and this recomputes it for the record; under a UTF-8 locale sort
# orders `brain-extraction/references/...` differently and the two disagree. The
# verification would still pass, the record would carry a different hash, and the
# arm gate would refuse all ten runs after they were paid for.
SKILLS_HASH=$(LC_ALL=C find "$SKILLSRC" -type f \( -name '*.md' -o -name '*.json' -o -name '*.py' \
  -o -name '*.sh' \) 2>/dev/null | LC_ALL=C sort | xargs cat 2>/dev/null | md5sum 2>/dev/null | cut -c1-12)
# d41d8cd98f00 is the md5 of nothing, which is what the above produces when
# SKILLS_SRC is empty or absent -- as it is for env-only in CI. It LOOKS like a
# hash, so it reads as a real and different skill rather than as no skill, and the
# first cluster run duly recorded it. On the VM the control arm inherited a default
# SKILLS_SRC and recorded the snapshot that was present but not installed, so the
# same condition would carry two different values and CI control runs would never
# pool with VM ones.
#
# `noskill`, NOT `none`. `none` is in summarize.py's UNRECORDED set, where it means
# "this run predates the field" -- and it has to stay there, because `none` is also
# what prompt_hash carries when md5sum fails. Writing it here would file a
# measurement we deliberately took under "we did not take this measurement", and
# every gate that skips UNRECORDED values would skip it: a cell mixing skill-present
# and skill-absent control runs would pass `one environment` silently, and the arm
# gate cannot catch it because env-only declares no expected hash. No published run
# carries `none`, so there is no history to migrate.
case "$SKILLS_HASH" in
  d41d8cd98f00|"") SKILLS_HASH=noskill ;;
esac

# "The prompt is byte-identical across arms" is the central claim of the whole
# comparison, and until now we asserted it rather than checked it. Hashing it
# means a divergence shows up in the report instead of being assumed away.
PROMPT_HASH=$(md5sum "$PROMPT" 2>/dev/null | cut -c1-12)

# node: which machine this ran on. Empty on the VM, where there is one host and it
# never changes; set in CI from the downward API, where it does not.
#
# Recorded because the kernel state our containers depend on is NODE-GLOBAL.
# kernel.apparmor_restrict_unprivileged_userns is what lets apptainer exec at a
# non-root uid; it is set per node by a privileged init container and persists for
# every pod on that node. Two reps of one cell can therefore land on machines in
# different states, and a rep on a bad node produces no output -- which in our data
# is indistinguishable from a model that produced nothing.
#
# NOT in PROVENANCE_KEYS. Scheduling is not ours to control, so making a multi-node
# cell un-poolable would fire constantly and be ignored. This exists to join against
# the cluster's own per-node logs when a result looks strange.
# label: benchmark | exploratory. run.yml has offered this as a dispatch input from
# the start and NOTHING recorded it -- the value was passed into this script as
# RUN_LABEL and dropped. So "exploratory" was a promise made at dispatch and absent
# from the data, and the only thing keeping a test run off the dashboard was a human
# choosing commit: false. That is exactly the kind of protection the publication
# gate exists to replace.
#
# Defaults to benchmark, so runs that predate this field read as real runs -- which
# they were.
# workdir: where the agent ran, before the rename to $RUN. opencode's session
# records it, and finalize_run.py looks the session up by it.
# wave: the wave the run was dispatched for (RUN_WAVE), empty outside one.
# The fields that name the run are held in IDENT and added to the record by land().
IDENT=$(printf '{"task_id":"%s","model":"%s","condition":"%s","repeat":%s,"skills_sha":"%s","skills_src":"%s","skills_hash":"%s","skills_installed":"%s","label":"%s","wave":"%s"}' \
  "$TASK" "$MODEL" "$COND" "$REP" \
  "$(git -C "$(dirname "$SKILLSRC")/.." rev-parse --short HEAD 2>/dev/null)" \
  "$SKILLSRC" "$SKILLS_HASH" "$(ls -1 "$SKILLDST" 2>/dev/null | tr '\n' ' ')" \
  "${RUN_LABEL:-benchmark}" "${RUN_WAVE:-}")
printf '{"image_version":"%s","opencode_version":"%s","prompt_hash":"%s","tasks_sha":"%s","node":"%s","workdir":"%s","start":"%s"}\n' \
  "${NEURODESKTOP_VERSION:-unknown}" "$("$OPENCODE_BIN" --version 2>/dev/null)" \
  "${PROMPT_HASH:-none}" \
  "${TASKS_SHA:-$(git -C "$(dirname "$(dirname "$TASKS")")" rev-parse --short HEAD 2>/dev/null)}" \
  "${NODE_NAME:-}" "$WORK" "$(date -u +%FT%TZ)" > "$RECORD"

# A random id for this run, sent to the gateway on every model call as
# x-litellm-session-id (write_opencode_config.py) and recorded by env_record.py.
BENCH_SESSION_ID=$(python -c "import uuid; print(uuid.uuid4().hex)" 2>/dev/null)
export BENCH_SESSION_ID
python "$HERE/env_record.py" --record "$RECORD" || true

# Keep tool scratch inside the working dir so it is cleaned with everything else.
export TMPDIR="$WORK/tmp"; mkdir -p "$TMPDIR"

# opencode keeps shared state under XDG_DATA_HOME -- one SQLite database and one
# local server -- and every concurrent run upserts the same `project` row. At
# MAXPAR=8 they collide and die in seconds with `Failed query: insert into
# "project"`; at MAXPAR=4 they instead BLOCK on the lock and hang until the
# timeout. Lowering concurrency does not fix that, it only makes the hangs rarer
# and longer.
#
# OPENCODE_ISOLATE gives each run its own data directory, so there is nothing to
# contend over. Costs a little startup time per run, and finalize_run.py already
# prefers the run-local database over the shared one for token accounting.
#
# ON BY DEFAULT since 2026-08-19. The shared database is not just a contention
# risk, it is a growing one: 344 runs took it to ~1 GB, and Neurodesk compacts it
# at every login. Compacting a file that size took ~7 minutes against a 2 minute
# startup limit, so the server was killed mid-start and could not be spawned at
# all. A day of the deadline went to diagnosing it as a memory fault, then as a
# rogue MCP server, before an admin found it. Set OPENCODE_ISOLATE=0 to go back to
# the shared database, but know that it is a per-run cost paid by the whole box.
if [ "${OPENCODE_ISOLATE:-1}" = 1 ]; then
  export XDG_DATA_HOME="$WORK/.xdg-data"
  mkdir -p "$XDG_DATA_HOME"
fi

echo "[run] $TASK | $MODEL | $COND | r$REP"
# < /dev/null is REQUIRED: backgrounded runs inherit a terminal stdin that goes
# bad once disowned, and opencode dies with "EBADF: bad file descriptor".
# --auto: opencode >=1.18 gates tool calls non-interactively and AUTO-REJECTS anything
#   outside the working dir, which looks exactly like a model failure (the agent tries
#   /tmp, is refused, and gives up). A benchmark agent must be able to run tools.
#   Only safe because each run is a disposable sandbox -- do NOT carry this to shared
#   infrastructure without an isolated runner pool.
# Harness and CI variables are removed from the agent's environment: they name the
# skill source, the arm, the run, the task and grader repositories, and the dispatch
# (GITHUB_EVENT_PATH). NEURODESK_API_KEY and BENCH_SESSION_ID stay; opencode reads them.
# RUNNER_TRACKING_ID stays: the runner uses it to stop leftover processes at job end.
AGENT_ENV=(env)
for v in SKILLS_SRC SKILLS_DST RUN_LABEL RUN_WAVE ARM TASK MODEL REP TASKS_JSON \
         BENCH_HOME TASKS_SHA NEURODESKTOP_VERSION AGENT_VIEW_CHECK AGENT_BASH_ENV \
         OPENCODE_BIN OPENCODE_ISOLATE RUN_TIMEOUT \
         $(compgen -e | grep -E '^(GITHUB_|RUNNER_|ACTIONS_|INPUT_|GRADER_|HARNESS_)' \
                      | grep -vx RUNNER_TRACKING_ID); do
  AGENT_ENV+=(-u "$v")
done

# What opencode will give the agent, resolved by opencode itself from the agent's
# working directory and environment: the skills it offers, and any instructions,
# skill paths or references in its config. Recorded as skills_offered. With
# AGENT_VIEW_CHECK=1 the run stops here, before any tokens are spent, unless the
# skills offered are exactly the arm's and none of those config keys are set.
case "$COND" in
  env+skill*) EXPECT="brain-extraction,brain-extraction-qc" ;;
  *)          EXPECT="" ;;
esac
(cd "$WORK" && "${AGENT_ENV[@]}" "$OPENCODE_BIN" debug skill)  > "$META/$ID.skills.json" 2>/dev/null
(cd "$WORK" && "${AGENT_ENV[@]}" "$OPENCODE_BIN" debug config) > "$META/$ID.config.json" 2>/dev/null
ENFORCE=""; [ "${AGENT_VIEW_CHECK:-0}" = 1 ] && ENFORCE=--enforce
python "$HERE/agent_view.py" --skills "$META/$ID.skills.json" \
  --config "$META/$ID.config.json" --record "$RECORD" --expect "$EXPECT" $ENFORCE
VIEW_RC=$?
# Deleted before the agent starts: the config holds the resolved gateway key, and the
# skill list names the arm's skills.
rm -f "$META/$ID.skills.json" "$META/$ID.config.json"
[ "$VIEW_RC" = 0 ] || {
  echo "ABORT: what opencode offers the agent does not match the arm. No tokens spent." >&2
  exit 3; }

# The gateway's roster and what it reports serving for this model, recorded before
# the agent starts (gateway_roster, model_served, model_fingerprint) and again after
# it ends (the same with _end, and model_changed). Record only.
python "$HERE/model_probe.py" --model "$MODEL" --record "$RECORD" || true

# The model must be the one the wave declares (waves.<n>.model_fingerprints in
# sweep.json). A different or missing fingerprint means the gateway serves another
# model or serving engine under this name; the run stops before the agent starts.
SWEEP_FILE="$HERE/../ci/sweep.json"
if [ -n "${RUN_WAVE:-}" ] && [ -f "$SWEEP_FILE" ]; then
  python "$HERE/wave_check.py" --sweep "$SWEEP_FILE" --wave "$RUN_WAVE" --model "$MODEL" \
    --record "$RECORD" --fingerprint-only >&2 || {
    echo "ABORT: the model served is not the one wave $RUN_WAVE declares. Only the probe was spent." >&2
    exit 3; }
fi

# -k: SIGKILL if the agent ignores the SIGTERM at the wall. Without it a hung
# child holds the run until the job's own timeout, which keeps no artifact.
timeout -k "${RUN_KILL_AFTER:-60}" "$TIMEOUT" "${AGENT_ENV[@]}" \
  "$OPENCODE_BIN" run --dir "$WORK" -m "$MODEL" --auto "$(cat "$PROMPT")" \
  < /dev/null > "$TRANSCRIPT" 2>&1 &
AGENT_PID=$!
wait "$AGENT_PID"
RC=$?
stop_agent
# Fold opencode's write-ahead log into the database, so the database is one file
# and masking a value inside it (redact.py) cannot break WAL frame checksums.
DB="$WORK/.xdg-data/opencode/opencode.db"
[ -f "$DB" ] && python -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).execute('PRAGMA wal_checkpoint(TRUNCATE)')" \
  "$DB" 2>/dev/null
python "$HERE/model_probe.py" --model "$MODEL" --record "$RECORD" --phase end || true
land
# run.json is written before the agent starts, so it cannot know which tools the
# agent picked. Read them back out of the transcript now.
python "$HERE/finalize_run.py" "$RUN" "$RC" 2>/dev/null
echo "  exit=$RC out=$(ls "$RUN/submissions/$TASK/output.nii.gz" 2>/dev/null || echo MISSING)"
