#!/bin/sh
# Assert the load-bearing hosts are reachable BEFORE a sweep spends anything.
#
#     sh egress_assert.sh              # fail if any load-bearing host is blocked
#     sh egress_assert.sh --warn       # report, exit 0
#     HOSTS="a.example b.example" sh egress_assert.sh
#
# WHY THIS IS AN ASSERTION AND NOT A REPORT
# `egress_report.py` tells you what the agents DID reach, after the fact, from
# transcripts. This tells you what they CAN reach, before you spend, and fails.
#
# The reason it has to fail rather than warn is the shape of the damage, which is
# not intuitive: a blocked host is not a uniform loss. It lands on whichever arm
# reaches it. `surfer.nmr.mgh.harvard.edu` serves SynthStrip's model weights and
# appears in 224 runs; routing to SynthStrip is the mechanism our skill's
# improvement is attributed to. So blocking it does not read as "the benchmark
# broke". It reads as "the skill stopped working", with clean logs and exit 0.
#
# We have already been through the same shape from the other direction. A 45-minute
# wall took 11 of 80 runs on one task and took them unevenly: 7 in baseline arms
# scored 0 as agent failures, 4 in a skill arm had written a mask before dying and
# scored as passes. It inflated the effect from one side and deflated it from the
# other. A distortion that is asymmetric is worse than one that is large.
#
# WHAT THIS CANNOT TELL YOU
# Reachability here is reachability from THIS shell, now. A CI job in a different
# pod, namespace or network policy is a different question, which is exactly why
# this belongs in the job that runs the sweep rather than in a setup step that ran
# somewhere else. It also cannot see a host a tool resolves internally without
# printing -- the host list comes from transcripts, so it is a floor, not a census.
set -u

WARN=0
[ "${1:-}" = "--warn" ] && WARN=1

# The load-bearing set: hosts whose absence changes a measurement rather than
# merely inconveniencing a run. Deliberately short. A long allowlist asserted here
# would fail on something harmless and train people to ignore it, which is how a
# check becomes decorative.
#
#   surfer.nmr.mgh.harvard.edu  SynthStrip weights. THE mechanism host: the skill's
#                               measured effect is routing to SynthStrip. 224 runs.
#   huggingface.co              grader reference data, and model weights for several
#                               extraction tools.
#   llm.neurodesk.org           the gateway. Without it nothing runs at all, which
#                               at least fails honestly -- included because it must
#                               be reachable at pod start, BEFORE the agent, and it
#                               is absent from the transcript-derived host list for
#                               exactly that reason: it is the harness talking.
#   github.com                  skills, datasets, and anything the agent clones.
#   files.pythonhosted.org      pip. An agent that cannot install falls back to a
#                               different tool, which is a mechanism change.
HOSTS="${HOSTS:-surfer.nmr.mgh.harvard.edu huggingface.co llm.neurodesk.org \
github.com files.pythonhosted.org}"

# Defined once, not inside the loop where it was first written.
_resolve_py() { python3 -c "import socket,sys;socket.gethostbyname(sys.argv[1])" "$1"; }

echo "=== egress assertion"

fail=0
for h in $HOSTS; do
  # Connectivity, not authorisation. A 403 means we reached the host, which is the
  # question; checking for 200 would fail on every gated endpoint and on anything
  # behind a login. --max-time so a black-holed route fails in 15s rather than
  # hanging a sweep that has not started yet.
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://$h/" 2>/dev/null)
  rc=$?
  if [ "$rc" -ne 0 ] || [ "$code" = "000" ]; then
    # Separate DNS from the connection, because a default-deny policy and a broken
    # resolver present identically at this level and are fixed in different places.
    #
    # Three outcomes, not two. The first version collapsed "no resolver tool here"
    # into the else branch and printed "resolves, connection refused" for a host
    # that does not resolve -- asserting the result of a check it had not run. That
    # is the whole failure class this script exists to catch, so it would have been
    # a poor place to reproduce it.
    # NOT nslookup. It exits 0 on NXDOMAIN -- measured, not assumed -- so using it
    # as a boolean reports every unresolvable host as resolving, which is the
    # wrong half of the distinction this branch exists to make.
    if command -v getent >/dev/null 2>&1; then
      resolver="getent hosts"
    elif command -v python3 >/dev/null 2>&1; then
      resolver="_resolve_py"
    else
      resolver=""
    fi
    if [ -z "$resolver" ]; then
      echo "  BLOCKED  $h  (unreachable; no getent or python3 here, so whether it"
      echo "                is DNS or egress is UNTESTED, not ruled out)"
    elif ! $resolver "$h" >/dev/null 2>&1; then
      echo "  BLOCKED  $h  (does not resolve -- DNS, not a firewall rule)"
    else
      echo "  BLOCKED  $h  (resolves, connection refused or timed out -- egress policy)"
    fi
    fail=1
  else
    echo "  ok       $h  (HTTP $code -- reached it; the code does not matter)"
  fi
done

echo
if [ "$fail" != 0 ]; then
  echo "A load-bearing host is unreachable."
  echo
  echo "Do not start a sweep. This will not fail loudly during the run: the agent"
  echo "falls back to another tool and produces a gradeable result, so the run"
  echo "exits 0 with clean logs and a worse number."
  echo
  echo "The loss is also NOT uniform. It lands on whichever arm reaches the blocked"
  echo "host, and for SynthStrip weights that is the treated arm -- the one whose"
  echo "improvement is the published claim. The result would read as the skill"
  echo "failing rather than as the environment failing."
  [ "$WARN" = 1 ] && { echo; echo "(--warn given, exiting 0 anyway)"; exit 0; }
  exit 1
fi
echo "Every load-bearing host is reachable from this shell, now. That is not a"
echo "statement about a different pod, namespace or network policy -- run this in"
echo "the job that runs the sweep, not in a setup step somewhere else."
exit 0
