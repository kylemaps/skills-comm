#!/bin/bash
# status.sh -- one screen: what is running, how far along, and can it still run.
#
#     ~/skills-comm/benchmark/runner/status.sh
#
# Exists because the answer to "how is it going" needs five separate commands,
# and the two things that actually go wrong are invisible in any one of them: a
# chain that was never launched (its log file simply does not exist), and a model
# that vanished from the gateway mid-sweep. Both are checked here explicitly.
set -u
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"

hr() { printf '\n=== %s %s\n' "$1" "$(printf '=%.0s' $(seq 1 $((56 - ${#1}))))"; }

hr "RUNNING"
SW=$(pgrep -fa 'run_sweep.sh' | grep -v 'kill -0' | cut -c1-100)
if [ -n "$SW" ]; then echo "$SW"; else echo "  no sweep running"; fi
WAIT=$(pgrep -fa 'while kill -0' | cut -c1-100)
[ -n "$WAIT" ] && { echo "  -- chained, waiting:"; echo "$WAIT" | sed 's/^/     /'; }
echo "  agents in flight: $(pgrep -fc 'opencode run' 2>/dev/null || echo 0)"
if [ -f "$BENCH_HOME/.sweep.lock" ]; then
  L=$(cat "$BENCH_HOME/.sweep.lock" 2>/dev/null)
  kill -0 "$L" 2>/dev/null && echo "  lock held by pid $L" \
    || echo "  !! STALE LOCK (pid $L is gone) -- rm $BENCH_HOME/.sweep.lock"
fi

hr "CHAIN LOGS"
shopt -s nullglob
LOGS=("$BENCH_HOME"/chain_*.log "$BENCH_HOME"/sweep_*.log "$BENCH_HOME"/retry_*.log)
if [ "${#LOGS[@]}" -eq 0 ]; then
  echo "  none found -- if you launched a chain, IT DID NOT START."
else
  for f in "${LOGS[@]}"; do
    printf '  %-26s %6s runs started  |  %s\n' "$(basename "$f")" \
      "$(grep -c '^\[run\]' "$f" 2>/dev/null)" \
      "$(tail -n 1 "$f" 2>/dev/null | cut -c1-58)"
  done
fi

hr "RUNS ON DISK (dirs / with output)"
for t in $(ls -d "$BENCH_HOME"/runs/*/ 2>/dev/null | sed 's|.*/runs/||;s|__.*||' | sort -u); do
  n=$(ls -d "$BENCH_HOME"/runs/"$t"__*/ 2>/dev/null | wc -l)
  o=$(ls "$BENCH_HOME"/runs/"$t"__*/submissions/*/output.nii.gz 2>/dev/null | wc -l)
  printf '  %-42s %4s / %-4s\n' "$t" "$n" "$o"
done

hr "GATEWAY (a missing model = exhausted or retired)"
curl -s --max-time 20 -H "Authorization: Bearer ${NEURODESK_API_KEY:-}" \
  https://llm.neurodesk.org/openai/models 2>/dev/null \
 | python -c "import json,sys
try: print('  ' + ', '.join(m['id'] for m in json.load(sys.stdin)['data']))
except Exception: print('  !! could not read roster')" 2>/dev/null \
 || echo "  !! gateway unreachable"

hr "SPEND"
python "$(dirname "${BASH_SOURCE[0]}")/token_report.py" "$BENCH_HOME/runs" 2>/dev/null \
  | sed -n '2,3p'
echo
