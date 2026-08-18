#!/bin/bash
# make_pack.sh TASK [TASK...]  -- assemble a results pack to send a collaborator.
#
#     ~/skills-comm/benchmark/runner/make_pack.sh \
#         structural-brain-extraction-7t structural-brain-extraction-motion
#
# Produces ~/bench/results_<date>.tar.gz containing, per task, the summary and
# the per-run CSV, plus a cross-task matrix and an auto-written MANIFEST.
#
# Per task, not pooled, on purpose: the recipient can pool per-task data herself,
# and cannot un-pool a pooled number. Her report builder also consumes exactly
# these two files per task, so this is the interface, not a courtesy.
#
# It refuses to build a pack from a task whose summary is missing or stale
# relative to its runs -- the last pack we sent went out before a re-collect and
# was superseded within days.
set -u
BENCH_HOME="${BENCH_HOME:-$HOME/bench}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNS="$BENCH_HOME/runs"
[ $# -ge 1 ] || { echo "usage: make_pack.sh TASK [TASK...]"; exit 1; }

STAMP=$(date -u +%Y-%m-%d)
PACK="$BENCH_HOME/pack_$STAMP"
rm -rf "$PACK"; mkdir -p "$PACK"

FAIL=0
for T in "$@"; do
  S="$RUNS/summary_$T.json"; C="$RUNS/runs_$T.csv"
  if [ ! -f "$S" ] || [ ! -f "$C" ]; then
    echo "!! $T: no summary/csv -- run collect_results.sh $T"; FAIL=1; continue
  fi
  # A run directory newer than the summary means the summary predates a run.
  NEWER=$(find "$RUNS" -maxdepth 1 -type d -name "${T}__*" -newer "$S" | wc -l)
  if [ "$NEWER" -gt 0 ]; then
    echo "!! $T: $NEWER run dir(s) newer than summary_$T.json -- re-collect first"; FAIL=1; continue
  fi
  cp "$S" "$C" "$PACK/"
  echo "   + $T"
done
[ "$FAIL" -eq 0 ] || { echo "ABORT: pack not built."; exit 1; }

python "$HERE/matrix.py" "$RUNS" > "$PACK/matrix.txt" 2>/dev/null
python "$HERE/matrix.py" "$RUNS" --csv > "$PACK/matrix.csv" 2>/dev/null
python "$HERE/pack_manifest.py" "$PACK" "$RUNS" "$@"

if [ ! -f "$PACK/NOTES.md" ]; then
  printf '%s\n' \
    "# Notes — our reading of these results" "" \
    "MANIFEST.md is measurement. This file is interpretation, and you should feel" \
    "free to disagree with it." "" \
    "_(replace this placeholder before sending)_" > "$PACK/NOTES.md"
  echo "   ! NOTES.md is a placeholder -- write it before sending"
fi

TAR="$BENCH_HOME/results_$STAMP.tar.gz"
tar -czf "$TAR" -C "$BENCH_HOME" "pack_$STAMP"
echo
echo "pack:    $PACK"
echo "tarball: $TAR  ($(du -h "$TAR" | cut -f1))"
ls -1 "$PACK" | sed 's/^/   /'
