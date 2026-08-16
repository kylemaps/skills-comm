#!/bin/bash
# run_sweep.sh TASK[,TASK2,...] REPEATS MODEL [MODEL...]
#
# One correct sweep: every task x every model x both arms x N repeats.
#
# Tasks run one at a time, each completing both arms before the next starts. That
# ordering is deliberate: if the sweep dies overnight you have complete data for the
# earlier tasks rather than half an experiment for all of them. List them in priority
# order.
#
# The arms run STRICTLY SEQUENTIALLY (all env-only, then all env+skill) because
# skills live in a container-global path -- two concurrent runs on different arms
# would silently contaminate each other. Within an arm, everything runs in
# parallel up to MAXPAR, since the bottleneck is LLM latency, not compute.
#
# A lock file prevents a second sweep from starting while one is in flight; that
# is exactly the mistake this script exists to make impossible.
#
#   nohup ~/skills-comm/benchmark/runner/run_sweep.sh \
#       structural-brain-extraction-7t 10 \
#       neurodesk/minimax-m2 neurodesk/qwen3.5-122b \
#       neurodesk/glm-5.2 neurodesk/kimi-k3 \
#       < /dev/null > ~/bench/sweep.log 2>&1 &
#   disown
#
# env:
#   MAXPAR=8        concurrent runs within an arm
#   ARMS="env-only env+skill"
#                   which arms to run. Set to a single arm to reuse an existing
#                   baseline instead of re-measuring it -- a changed skill does not
#                   change the no-skill arm, and when gateway credit is scarce that
#                   halves the cost.
#
#                   The reused baseline is then a HISTORICAL control: different day,
#                   possibly different gateway state and runner version. The report
#                   compares provenance across arms and will say so. Do not silence
#                   that -- it is the whole reason the comparison needs care.
#   RETRY_INFRA=2   after each arm, re-run any run lost to a harness failure
#                   (gateway outage, agent-runtime contention, a run that never
#                   made a model call). Set 0 to disable. Retries run serially,
#                   because concurrency is what causes most of these failures.
#   SKIP_MISSING=1  (default) drop models the gateway no longer serves and run the
#                   rest. Set 0 to abort instead.
#
#                   Default is "continue" because of how this fails in practice: a
#                   whole overnight sweep once aborted because the gateway had
#                   retired one model generation. Losing 4 models' data to save the
#                   5th is the worse trade. Dropped models are logged here, recorded
#                   in sweep_manifest.json, and visible in the final report as cells
#                   with no data -- there is no way to lose track of them.
set -u

if [ "$#" -lt 3 ] || [ "$1" = "--help" ]; then
  echo "usage: run_sweep.sh TASK[,TASK2,...] REPEATS MODEL [MODEL...]"
  echo "env: MAXPAR=8  SKIP_MISSING=1"
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"
MAXPAR="${MAXPAR:-8}"
SKIP_MISSING="${SKIP_MISSING:-1}"
RETRY_INFRA="${RETRY_INFRA:-2}"
read -r -a ARM_LIST <<< "${ARMS:-env-only env+skill}"
LOCK="$BENCH_HOME/.sweep.lock"

# env+skill may carry a suffix naming which skill is under test, e.g.
# `env+skill-michele`. That keeps run directories distinct so two skills can be
# compared head-to-head against one shared baseline instead of overwriting each
# other. Reject ':' -- tar and rsync treat `foo:bar` as a remote path.
for a in "${ARM_LIST[@]}"; do
  case "$a" in
    *:*) echo "ABORT: arm '$a' contains ':' -- breaks tar/rsync paths"; exit 1 ;;
    env-only|env+skill|env+skill-*) ;;
    *) echo "ABORT: unknown arm '$a' (want env-only, env+skill, or env+skill-<label>)"
       exit 1 ;;
  esac
done

IFS=',' read -r -a TASKS <<< "$1"; shift
N="$1"; shift
REQUESTED=("$@")

mkdir -p "$BENCH_HOME"
if [ -e "$LOCK" ]; then
  echo "ABORT: a sweep is already running (pid $(cat "$LOCK" 2>/dev/null))."
  echo "       Two sweeps would contaminate each other's arms."
  echo "       If that pid is dead: rm $LOCK"
  exit 1
fi

# A run_bench.sh that outlived a previous pkill will keep writing into the runs
# directory and show up in the results as a phantom cell from the old batch. We
# have already been bitten by exactly this.
STRAY=$(pgrep -f "run_bench.sh" | grep -v "^$$\$" | paste -sd' ' -)
if [ -n "$STRAY" ]; then
  echo "ABORT: run_bench.sh already running (pids: $STRAY)."
  echo "       Those runs would land in this sweep's results."
  echo "       Stop them first:  pkill -f run_bench.sh; pkill -f 'opencode run'"
  exit 1
fi

echo $$ > "$LOCK"
cleanup() { rm -f "$LOCK"; }
# A signal trap whose handler returns REPLACES the default terminate action, so
# `trap cleanup INT TERM` made this script immune to Ctrl-C and pkill: it deleted
# its lock and carried on spawning runs. One sweep survived thirty minutes of kill
# attempts that way, respawning children faster than they could be killed, while
# its missing lock file made a second sweep look safe to start. The handlers must
# exit.
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

# --- preflight: environment fatal, missing models negotiable -----------------
RESOLVED="$BENCH_HOME/.resolved_models"
RESOLVED_OUT="$RESOLVED" "$HERE/preflight.sh" "${REQUESTED[@]}"
PF=$?
if [ "$PF" = 1 ]; then
  echo "ABORT: preflight failed (environment, not models)."
  exit 1
fi

MODELS=()
while IFS= read -r line; do [ -n "$line" ] && MODELS+=("$line"); done < "$RESOLVED"
DROPPED=()
for m in "${REQUESTED[@]}"; do
  printf '%s\n' "${MODELS[@]}" | grep -qx "$m" || DROPPED+=("$m")
done

if [ "${#DROPPED[@]}" -gt 0 ]; then
  if [ "$SKIP_MISSING" != 1 ]; then
    echo "ABORT: ${#DROPPED[@]} model(s) unavailable and SKIP_MISSING=0: ${DROPPED[*]}"
    exit 1
  fi
  echo "!! DROPPED ${#DROPPED[@]} unavailable model(s): ${DROPPED[*]}"
  echo "!! Continuing with ${#MODELS[@]}: ${MODELS[*]}"
fi

# A typo'd task id would otherwise waste the whole sweep: every run would fail on an
# empty prompt, one at a time, for hours. Resolve all of them up front instead.
TASKS_JSON_PATH="${TASKS_JSON:-$HOME/grader-repo/benchmark/tasks.json}"
for t in "${TASKS[@]}"; do
  if ! python "$HERE/mkprompt.py" "$TASKS_JSON_PATH" "$t" 2>/dev/null | grep -q .; then
    echo "ABORT: task '$t' has no prompt in $TASKS_JSON_PATH"
    echo "       check the id against: $HOME/grader-repo/benchmark/harness/run_manifest.json"
    exit 1
  fi
  echo "ok   task $t"
done

TOTAL=$(( ${#TASKS[@]} * ${#MODELS[@]} * ${#ARM_LIST[@]} * N ))
START=$(date -u +%FT%TZ)

# The manifest is the answer to "what did last night actually attempt?" -- it
# survives even if the log is truncated or the sweep is killed mid-flight.
{
  printf '{"tasks":["%s"],' "$(printf '%s' "${TASKS[*]}" | sed 's/ /","/g')"
  printf '"repeats":%s,"maxpar":%s,"arms":["%s"],"start":"%s",' "$N" "$MAXPAR" "$(printf '%s' "${ARM_LIST[*]}" | sed 's/ /","/g')" "$START"
  printf '"requested":["%s"],' "$(printf '%s' "${REQUESTED[*]}" | sed 's/ /","/g')"
  printf '"models":["%s"],' "$(printf '%s' "${MODELS[*]}" | sed 's/ /","/g')"
  if [ "${#DROPPED[@]}" -gt 0 ]; then
    printf '"dropped":["%s"],' "$(printf '%s' "${DROPPED[*]}" | sed 's/ /","/g')"
  else
    printf '"dropped":[],'
  fi
  printf '"planned_runs":%s}\n' "$TOTAL"
} > "$BENCH_HOME/sweep_manifest.json"

echo "=== SWEEP START $START — $TOTAL runs, MAXPAR=$MAXPAR ==="
echo "    tasks:   ${TASKS[*]}"
echo "    models:  ${MODELS[*]}"
echo "    repeats: $N"
echo "    arms:    ${ARM_LIST[*]}"
if [ "${#ARM_LIST[@]}" -lt 2 ]; then
  echo "    NOTE: single arm. The other arm must come from earlier runs, which makes"
  echo "          it a historical control -- check the provenance block in the report."
fi

# `wait -n` needs bash >= 4.3. Without it we must fall back to a full barrier;
# blindly assuming it exists would decrement the counter without waiting and let
# parallelism run away.
if [ "${BASH_VERSINFO[0]}" -gt 4 ] || \
   { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 3 ]; }; then
  HAVE_WAIT_N=1
else
  HAVE_WAIT_N=0
  echo "    note: bash ${BASH_VERSION} has no 'wait -n'; throttling in full batches"
fi

for TASK in "${TASKS[@]}"; do
  echo "=== TASK: $TASK  ($(date -u +%FT%TZ)) ==="
  for COND in "${ARM_LIST[@]}"; do
    echo "=== ARM: $COND  ($(date -u +%FT%TZ)) ==="
    running=0
    for M in "${MODELS[@]}"; do
      for r in $(seq 1 "$N"); do
        "$HERE/run_bench.sh" "$TASK" "$M" "$COND" "$r" &
        running=$((running + 1))
        if [ "$running" -ge "$MAXPAR" ]; then
          if [ "$HAVE_WAIT_N" = 1 ]; then
            # `|| true` matters: wait -n returns the finished job's exit status, so a
            # single failed run would otherwise fall through to a full barrier and
            # quietly serialise the rest of the arm.
            wait -n 2>/dev/null || true
            running=$((running - 1))
          else
            wait; running=0
          fi
        fi
      done
    done
    wait                    # arm barrier: never overlap arms

    # Retry runs that failed for OUR reasons -- gateway outage, agent-runtime
    # contention, runs that never made a model call. Excluding them keeps the
    # numbers honest but leaves the cell short, and unequal denominators are not
    # something to caption around: if our harness broke the run, run it again.
    #
    # Retries are serial. Concurrency is what causes most of these failures, so
    # retrying in parallel would reproduce the conditions that lost the runs.
    for attempt in $(seq 1 "$RETRY_INFRA"); do
      mapfile -t FAILED < <(python "$HERE/find_failed.py" "$BENCH_HOME/runs" "$TASK" \
                              --arm "$COND" 2>/dev/null)
      [ "${#FAILED[@]}" -eq 0 ] && break
      echo "=== RETRY $attempt/$RETRY_INFRA: ${#FAILED[@]} run(s) lost to harness failure ==="
      for d in "${FAILED[@]}"; do
        b=$(basename "$d")
        M="neurodesk/$(echo "$b" | awk -F'__' '{print $2}' | sed 's/^neurodesk-//')"
        r=$(echo "$b" | awk -F'__' '{print $4}' | tr -d 'r')
        echo "    retry $b"
        "$HERE/run_bench.sh" "$TASK" "$M" "$COND" "$r"
      done
    done
    STILL=$(python "$HERE/find_failed.py" "$BENCH_HOME/runs" "$TASK" --arm "$COND" \
              2>/dev/null | wc -l)
    [ "$STILL" -gt 0 ] && echo "!! $STILL run(s) still failing after $RETRY_INFRA retries"

    echo "=== ARM $COND COMPLETE $(date -u +%FT%TZ) ==="
  done
  TDONE=$(ls -d "$BENCH_HOME"/runs/"$TASK"__*/ 2>/dev/null | wc -l)
  echo "=== TASK $TASK COMPLETE $(date -u +%FT%TZ) — $TDONE run dirs ==="
done

DONE=0
for TASK in "${TASKS[@]}"; do
  DONE=$(( DONE + $(ls -d "$BENCH_HOME"/runs/"$TASK"__*/ 2>/dev/null | wc -l) ))
done
echo "=== SWEEP DONE $(date -u +%FT%TZ) — $DONE/$TOTAL run dirs present ==="
[ "${#DROPPED[@]}" -gt 0 ] && echo "    (dropped models: ${DROPPED[*]})"
for TASK in "${TASKS[@]}"; do
  echo "next: $HERE/collect_results.sh $TASK"
done
