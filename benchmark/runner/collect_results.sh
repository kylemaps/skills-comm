#!/bin/bash
# collect_results.sh TASK
#
# Grades every run for TASK with the task's grader pack, then prints the four
# things we track per run: outcome/score, tool chosen, whether the skill was
# opened, and whether the agent falsely declared a tool missing.
set -u

TASK="${1:?usage: collect_results.sh TASK}"
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"
HARNESS="${HARNESS_DIR:-$HOME/grader-repo/benchmark/harness}"

cd "$HARNESS" || { echo "no harness at $HARNESS"; exit 1; }
python fetch_reference.py --task "$TASK" >/dev/null 2>&1

for d in "$BENCH_HOME"/runs/"$TASK"__*/; do
  [ -d "$d" ] || continue
  COND=$(basename "$d" | awk -F'__' '{print $3}')
  MODEL=$(basename "$d" | awk -F'__' '{print $2}')
  python grade_wrapper.py grade --task "$TASK" --submission-dir "$d" \
    --model "$MODEL" --condition "$COND" --out "$d/envelope.json" >/dev/null 2>&1
done

echo "=== RESULTS: $TASK ==="
printf "%-22s %-11s %-4s %-19s %-7s %-7s %-22s %-6s %s\n" \
  MODEL ARM REP VERDICT SCORE DICE TOOL SKILL-LOADS "FALSE-MISS"

for d in "$BENCH_HOME"/runs/"$TASK"__*/; do
  [ -d "$d" ] || continue
  b=$(basename "$d")
  MODEL=$(echo "$b" | awk -F'__' '{print $2}')
  ARM=$(echo "$b"   | awk -F'__' '{print $3}')
  REP=$(echo "$b"   | awk -F'__' '{print $4}')
  T="$d/transcript.txt"

  # Prefer the recorded provenance (finalize_run.py writes it); fall back to grep
  # for runs made before that existed.
  TOOL=$(python -c "
import json,sys
try:
    d=json.load(open('$d/run.json'))
    t=d.get('tools_loaded') or []
    print(','.join(t)[:30] if t else '')
except Exception: print('')" 2>/dev/null)
  [ -z "$TOOL" ] && TOOL=$(grep -oE "module load +[A-Za-z0-9_.-]+/[A-Za-z0-9_.]+" "$T" 2>/dev/null           | sed 's/module load *//' | sort -u | paste -sd, - | cut -c1-30)
  [ -z "$TOOL" ] && TOOL="(none)"
  SKILL=$(grep -c 'Skill "' "$T" 2>/dev/null)
  FALSE=$(grep -icE "(module|tool|command)[^.]{0,25}not (found|available|installed)" "$T" 2>/dev/null)

  if [ -f "$d/envelope.json" ]; then
    python - "$d/envelope.json" "$MODEL" "$ARM" "$REP" "$TOOL" "$SKILL" "$FALSE" <<'PY'
import json, sys
e = json.load(open(sys.argv[1]))
_, _, model, arm, rep, tool, skill, false_missing = sys.argv[:8]
d = e.get("detail", {}).get("metrics", {})
print("%-22s %-11s %-4s %-19s %-7s %-7s %-22s %-6s %s" % (
    model, arm, rep, e.get("verdict", "?"), e.get("score", "?"),
    d.get("dice", "-"), tool, skill, false_missing))
PY
  else
    printf "%-22s %-11s %-4s %-19s %-7s %-7s %-22s %-6s %s\n" \
      "$MODEL" "$ARM" "$REP" "NO-OUTPUT" "-" "-" "$TOOL" "$SKILL" "$FALSE"
  fi
done

echo
echo "=== SUMMARY (pass = valid and verdict != unacceptable) ==="
python - "$BENCH_HOME/runs" "$TASK" <<'PY'
import json, glob, os, sys, statistics as st
root, task = sys.argv[1], sys.argv[2]
cells = {}
for p in sorted(glob.glob(os.path.join(root, task + "__*", "envelope.json"))):
    b = os.path.basename(os.path.dirname(p)).split("__")
    model, arm = b[1], b[2]
    e = json.load(open(p))
    cells.setdefault((model, arm), []).append(e.get("score", 0.0))
print("%-22s %-11s %-8s %-10s %s" % ("MODEL", "ARM", "N", "MEAN", "PASSES"))
for (model, arm), scores in sorted(cells.items()):
    passes = sum(1 for s in scores if s and s > 0)
    mean = round(st.mean(scores), 1) if scores else 0
    print("%-22s %-11s %-8s %-10s %s/%s" % (model, arm, len(scores), mean, passes, len(scores)))
PY
