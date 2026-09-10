#!/bin/sh
# One screen of "is the server in a state we can work from", run at the top of a session.
#
#     bash ~/skills-comm/benchmark/runner/hygiene.sh
#
# READ-ONLY. It deletes nothing and pulls nothing. Every state it can find has, at some
# point, been discovered late and cost a day:
#
#   - disk and INODES. Inodes are the binding constraint, not gigabytes: a run tree
#     retains ~5,500 of them, and df -h looks calm right up until writes start failing.
#   - repo drift. A stale ~/skills-comm silently runs an old harness; a moved
#     ~/grader-repo silently regrades against a different task pack.
#   - a sweep in flight. Pulling or collecting during one is how we broke a run and
#     how we OOM'd the box. If this says a sweep is up, do nothing else.
#
# The reclaim figures are reported, never acted on. What is safe to delete depends on
# which results have been packed, and that is a decision, not a check.
set -u

SC="$HOME/skills-comm"
GR="$HOME/grader-repo"
RUNS="$HOME/bench/runs"

echo "=== HYGIENE $(date -u '+%Y-%m-%d %H:%M UTC') on $(hostname)"

echo
echo "--- is anything running"
BUSY=0
PROCS=$(pgrep -c -f 'opencode|run_bench|run_sweep' 2>/dev/null || echo 0)
[ "$PROCS" -gt 0 ] && BUSY=1
echo "  agent/sweep processes : $PROCS"
for L in "$HOME/bench/.sweep.lock" "$HOME/bench/sweep.lock"; do
  [ -e "$L" ] && { echo "  LOCK PRESENT          : $L"; BUSY=1; }
done
if [ "$BUSY" -eq 1 ]; then
  echo "  >> A SWEEP MAY BE LIVE. Do not pull, do not collect, do not delete."
else
  echo "  nothing in flight"
fi

echo
echo "--- space"
df -h "$HOME" | tail -1 | awk '{print "  disk   : "$3" of "$2" used ("$5"), "$4" free"}'
IP=$(df --output=ipcent "$HOME" 2>/dev/null | tail -1 | tr -dc '0-9')
IF=$(df --output=iavail "$HOME" 2>/dev/null | tail -1 | tr -dc '0-9')
case "$IP" in
  "" ) echo "  inodes : cannot read -- treat as unknown, not as fine" ;;
  *  ) echo "  inodes : ${IP}% used, ${IF:-?} free" ;;
esac

echo
echo "--- repos (local vs origin)"
for D in "$SC" "$GR"; do
  [ -d "$D/.git" ] || { echo "  $D: not a git checkout"; continue; }
  B=$(git -C "$D" rev-parse --abbrev-ref HEAD 2>/dev/null)
  H=$(git -C "$D" rev-parse --short HEAD 2>/dev/null)
  DIRTY=$(git -C "$D" status --porcelain 2>/dev/null | grep -vc '^??' || echo 0)
  UNTR=$(git -C "$D" status --porcelain 2>/dev/null | grep -c '^??' || echo 0)
  BEHIND=$(git -C "$D" rev-list --count "HEAD..origin/$B" 2>/dev/null || echo "?")
  AHEAD=$(git -C "$D" rev-list --count "origin/$B..HEAD" 2>/dev/null || echo "?")
  echo "  $(basename "$D"): $B @ $H  behind=$BEHIND ahead=$AHEAD  modified=$DIRTY untracked=$UNTR"
done
echo "  (behind counts are against the last fetch; they are stale until you fetch)"

echo
echo "--- runs"
echo "  run dirs        : $(ls "$RUNS" 2>/dev/null | wc -l)"
echo "  parked          : $(ls "$HOME/bench/parked" 2>/dev/null | wc -l)"
echo "  newest          : $(ls -1t "$RUNS" 2>/dev/null | head -1)"

echo
echo "--- what COULD be reclaimed (reported only, nothing is deleted)"
if [ "$BUSY" -eq 1 ]; then
  echo "  skipped: a sweep may be live"
else
  for P in tmp data; do
    N=$(find "$RUNS" -maxdepth 2 -type d -name "$P" 2>/dev/null | wc -l)
    [ "$N" -gt 0 ] && echo "  $N x runs/*/$P  $(du -shc $(find "$RUNS" -maxdepth 2 -type d -name "$P" 2>/dev/null) 2>/dev/null | tail -1 | cut -f1)"
  done
  find "$HOME" -maxdepth 1 -name '*.tar.gz' -printf '  %10s  %p\n' 2>/dev/null | sort -rn | head -5
fi

echo
echo "=== nothing above was changed. Decide, then act."
