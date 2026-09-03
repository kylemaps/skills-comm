#!/bin/bash
# disk_check.sh [--reclaim] [PLANNED_RUNS]
#
# Is there room for the sweep you are about to launch, and if not, what can be
# safely deleted.
#
# WHY
# ---
# Every run datalad-fetches its own copy of the dataset, so disk grows with run
# count and not with results. This filled to 78% once. The cleanup that fixed it
# deleted tmp/ and data/ from 727 run directories, freed 18 GB, and also removed
# the anat images -- which was only discovered weeks later when an analysis
# needed them and they were gone.
#
# So this does two things the ad-hoc cleanup did not: it projects the cost of the
# sweep you are about to start BEFORE you start it, and it refuses to delete
# anything from a run that has not been graded yet.
#
# WHAT IS SAFE TO DELETE, AND WHY
# -------------------------------
# Inside a run directory:
#
#   data/, tmp/   working copies of input data. Reproducible by re-fetching, and
#                 worthless once the run is graded. SAFE, but only after grading.
#   submissions/  the mask the agent produced. NEVER. It is the result.
#   transcript*   the only record of what the agent did. NEVER -- every mechanism
#                 finding on this project came out of transcripts, months later.
#   run.json      provenance. NEVER.
#   envelope.json the grade. NEVER.
#
# "Only after grading" is the load-bearing part. A run with data/ but no
# envelope.json may still need re-grading, and re-fetching costs gateway time we
# cannot always afford.
#
# Inode pressure is checked separately. Hundreds of runs each holding a datalad
# tree is a lot of small files, and running out of inodes looks like a disk-full
# error while df still shows free space.
set -u

RECLAIM=0
PLANNED=0
for arg in "$@"; do
  case "$arg" in
    --reclaim) RECLAIM=1 ;;
    ''|*[!0-9]*) ;;
    *) PLANNED="$arg" ;;
  esac
done

BENCH="${BENCH_HOME:-$HOME/bench}"
RUNS="$BENCH/runs"

echo "=== disk ==="
df -h "$HOME" | sed 's/^/  /'
echo
echo "=== inodes (a full inode table reads as disk-full while df shows space) ==="
df -i "$HOME" | sed 's/^/  /'

PCT=$(df --output=pcent "$HOME" 2>/dev/null | tail -1 | tr -dc '0-9')
# NOT `df -i --output=pcent`. GNU df rejects -i combined with --output and prints
# nothing but a usage hint, so that spelling left IPCT empty -- and ${IPCT:-0}
# then read as 0%, which is how this script printed "VERDICT: healthy" while the
# inode table was 86% full. The field for inodes is ipcent.
IPCT=$(df --output=ipcent "$HOME" 2>/dev/null | tail -1 | tr -dc '0-9')
AVAIL_K=$(df --output=avail "$HOME" 2>/dev/null | tail -1 | tr -dc '0-9')
AVAIL_G=$(( ${AVAIL_K:-0} / 1024 / 1024 ))

echo
echo "=== where it went (a few minutes: ~1.8M inodes on network storage) ==="
# du in kilobytes once, format for humans afterwards. Walking the runs tree is
# the expensive part here, and doing it twice doubled the wait for nothing.
TOT_K=0
for d in "$RUNS" "$BENCH/parked" "$BENCH/qcval" "$BENCH/report" \
         "$HOME/.local/share/opencode" "$HOME/grader-repo" "$HOME/skills-comm"; do
  [ -e "$d" ] || continue
  k=$(du -sk "$d" 2>/dev/null | cut -f1)
  [ "$d" = "$RUNS" ] && TOT_K=${k:-0}
  printf '  %6s MB  %s\n' "$(( ${k:-0} / 1024 ))" "$d"
done

N_RUNS=$(ls -1d "$RUNS"/*__* 2>/dev/null | wc -l)
if [ "$N_RUNS" -gt 0 ] && [ "$TOT_K" -gt 0 ]; then
  PER_M=$(( TOT_K / N_RUNS / 1024 ))
  echo
  echo "=== per-run cost ==="
  echo "  $N_RUNS runs, ~${PER_M} MB each"
  if [ "$PLANNED" -gt 0 ]; then
    NEED_G=$(( PLANNED * PER_M / 1024 ))
    echo "  $PLANNED planned runs need roughly ${NEED_G} GB"
    echo "  ${AVAIL_G} GB free"
    if [ "$NEED_G" -gt "$AVAIL_G" ]; then
      echo "  !! NOT ENOUGH ROOM. Reclaim below, or the sweep dies partway"
      echo "     and the runs it already paid for may be unusable."
    elif [ $(( NEED_G * 2 )) -gt "$AVAIL_G" ]; then
      echo "  !! Tight. Under 2x headroom. Reclaim first."
    else
      echo "  OK: enough room with headroom to spare."
    fi
  fi
fi

# --- what is reclaimable ----------------------------------------------
echo
echo "=== reclaimable: data/ and tmp/ in runs that are already graded ==="
# One du call over every candidate at once. Calling du per directory took
# minutes on the Longhorn volume: 1.8 M inodes across a few hundred datalad
# trees, and network-backed storage makes every stat a round trip. A check that
# takes ten minutes is a check people skip.
GRADED=0
RECLAIM_K=0
CANDIDATES=""
CAND_LIST=$(mktemp)
for d in "$RUNS"/*__*; do
  [ -d "$d" ] || continue
  [ -f "$d/envelope.json" ] || continue          # ungraded: leave alone
  for sub in data tmp; do
    [ -d "$d/$sub" ] && echo "$d/$sub" >> "$CAND_LIST"
  done
done
if [ -s "$CAND_LIST" ]; then
  GRADED=$(wc -l < "$CAND_LIST")
  # Default whitespace splitting is fine: run directory names are
  # task__model__arm__rep and never contain spaces.
  RECLAIM_K=$(xargs -a "$CAND_LIST" du -sk 2>/dev/null \
              | awk '{s+=$1} END {print s+0}')
  CANDIDATES=$(cat "$CAND_LIST")
fi
rm -f "$CAND_LIST"
RECLAIM_G=$(( RECLAIM_K / 1024 / 1024 ))
echo "  $GRADED director(ies), ~${RECLAIM_G} GB"

UNGRADED=$(ls -1d "$RUNS"/*__* 2>/dev/null | while read -r d; do
             [ -f "$d/envelope.json" ] || echo "$d"; done | wc -l)
echo "  $UNGRADED run(s) not yet graded: skipped, they may need re-grading"

if [ "$RECLAIM" = 1 ]; then
  if [ -z "$CANDIDATES" ]; then
    echo "  nothing to reclaim"
  else
    echo
    echo "  deleting..."
    printf '%s' "$CANDIDATES" | while read -r p; do
      [ -n "$p" ] && rm -rf -- "$p"
    done
    echo "  done. Disk now:"
    df -h "$HOME" | sed 's/^/  /'
  fi
else
  echo
  echo "  Dry run. Add --reclaim to delete."
  echo "  Nothing else is touched: submissions/, transcript*, run.json and"
  echo "  envelope.json are the result and the evidence."
fi

echo
# A threshold check that cannot take its measurement must not report the safe
# answer. Defaulting an unreadable percentage to 0 is what made the inode gate
# invisible for as long as it was broken.
for v in PCT IPCT; do
  case "${!v}" in
    "" | *[!0-9]* )
      echo "=== VERDICT: cannot tell. $v did not parse from df on this system."
      echo "    Refusing to say it is safe to launch. Check df manually."
      exit 1 ;;
  esac
done
if [ "$PCT" -ge 85 ] || [ "$IPCT" -ge 85 ]; then
  echo "=== VERDICT: do not launch. ${PCT}% disk, ${IPCT}% inodes. Reclaim first."
  exit 1
elif [ "$PCT" -ge 70 ] || [ "$IPCT" -ge 70 ]; then
  echo "=== VERDICT: launch only after reclaiming. ${PCT}% disk, ${IPCT}% inodes."
  exit 0
else
  echo "=== VERDICT: healthy. ${PCT}% disk, ${IPCT}% inodes."
fi
