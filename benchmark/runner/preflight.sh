#!/bin/bash
# preflight.sh MODEL [MODEL...]
#
# Assert the environment is sane BEFORE burning a multi-hour matrix. Added after
# a neurodesktop image upgrade silently reset ~/.config/opencode/opencode.json,
# leaving the requested model undeclared — six runs died in 37 seconds.
#
# Exit codes are meaningful, because "one model vanished" and "the gateway is down"
# deserve different responses from an unattended overnight sweep:
#   0  everything resolved
#   1  environment is broken (no key / gateway down / NO model resolved) -- fatal
#   2  environment is fine but SOME models did not resolve -- caller's call
#
# RESOLVED_OUT=<file>  write the models that DID resolve, one per line. run_sweep.sh
#                      uses this to continue with the survivors.
set -u

# Secrets. Some Neurodesk images ship a root-owned ~/.bashrc, so the opencode
# wrapper cannot persist NEURODESK_API_KEY there. ~/bench/.env always works.
[ -f "${BENCH_HOME:-$HOME/bench}/.env" ] && . "${BENCH_HOME:-$HOME/bench}/.env"
ENVFAIL=0

if [ -z "${NEURODESK_API_KEY:-}" ]; then
  echo "FAIL: NEURODESK_API_KEY not set (run 'opencode' once, then 'source ~/.bashrc')"
  ENVFAIL=1
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
  -H "Authorization: Bearer ${NEURODESK_API_KEY:-}" \
  https://llm.neurodesk.org/openai/models 2>/dev/null)
if [ "$code" != "200" ]; then
  echo "FAIL: llm.neurodesk.org returned $code"
  ENVFAIL=1
else
  echo "ok   gateway 200"
fi

# Called with no models, this used to run every gateway check, resolve nothing, and
# print PREFLIGHT FAILED -- which reads as "the gateway is down" when the truth is
# "you gave me nothing to check". Usage, and exit 2 for a usage error rather than 1,
# so a caller can tell a mistake from a real failure.
if [ "$#" -eq 0 ]; then
  echo "usage: preflight.sh MODEL [MODEL ...]"
  echo
  echo "Checks the gateway is reachable and that each model is served, before a"
  echo "sweep spends tokens discovering otherwise."
  echo
  echo "  RESOLVED_OUT=FILE   write the models that resolved, one per line"
  echo
  echo "exit 0 all resolved | 1 gateway or key failure | 2 usage, or partial"
  exit 2
fi

OC_CHECK="$HOME/.config/opencode/opencode.json"
KG_CHECK="${BENCH_HOME:-$HOME/bench}/opencode.json.known-good"
if [ ! -f "$OC_CHECK" ]; then
  if [ -f "$KG_CHECK" ]; then
    echo "FAIL: $OC_CHECK is missing. A known-good copy is at $KG_CHECK"
    echo "      restore with: cp $KG_CHECK $OC_CHECK"
  else
    echo "FAIL: $OC_CHECK is missing and no known-good copy exists"
  fi
  ENVFAIL=1
fi

AVAIL=$(timeout 60 /usr/bin/opencode models 2>/dev/null)
OK_MODELS=()
MISSING=()
for m in "$@"; do
  if echo "$AVAIL" | grep -qx "$m"; then
    echo "ok   $m"
    OK_MODELS+=("$m")
  else
    echo "FAIL: $m is not available from the gateway"
    # The usual cause is that the gateway retired a model generation. Show the
    # siblings so the fix is obvious instead of a guessing game.
    fam=$(printf '%s' "$m" | sed 's#.*/##' | grep -oE '^[a-zA-Z]+' || true)
    if [ -n "${fam:-}" ]; then
      sib=$(echo "$AVAIL" | grep -i "$fam" | paste -sd' ' -)
      [ -n "$sib" ] && echo "      available in that family: $sib"
    fi
    MISSING+=("$m")
  fi
done

if [ -n "${RESOLVED_OUT:-}" ]; then
  : > "$RESOLVED_OUT"
  for m in ${OK_MODELS+"${OK_MODELS[@]}"}; do echo "$m" >> "$RESOLVED_OUT"; done
fi

if [ "$ENVFAIL" != 0 ] || [ "${#OK_MODELS[@]}" -eq 0 ]; then
  echo "PREFLIGHT FAILED"
  exit 1
fi
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "PREFLIGHT PARTIAL — ${#OK_MODELS[@]} of $# models resolved"
  exit 2
fi
# What the container wrappers pass to apptainer.
#
# NOT a pass/fail for this machine. The VM runs setuid apptainer, so an --overlay
# wrapper works fine here and failing preflight over it would be crying wolf.
#
# It runs here because this is the only script that executes before every sweep, and
# because the thing being checked is a property of the NEURODESK IMAGES rather than
# of our code -- it can change under us with nobody doing anything, exactly as the
# 2026-09-01 image upgrade reset opencode.json. If it changes, we have told the
# cluster team something that is no longer true, and the place to find that out is
# here rather than from a tool's pass rate on a pool that runs userns.
#
# Wired in deliberately. I wrote this check, described it as "meant for preflight",
# and left preflight not calling it -- which is cluster-prod's null-route lesson
# verbatim: proving the pipe works is not proving anything is in the pipe.
if [ -x "$(dirname "$0")/container_opts_check.sh" ]; then
  if ! sh "$(dirname "$0")/container_opts_check.sh" >/tmp/.copts.$$ 2>&1; then
    echo
    cat /tmp/.copts.$$
    echo "NOTE: this does not block a sweep on this machine -- apptainer is setuid"
    echo "      here. It DOES invalidate what we told the cluster team, which is"
    echo "      that these wrappers are plain read-only SIF execs and their pool"
    echo "      could therefore run userns. Tell them before they build."
  else
    echo "ok   container wrappers: plain read-only exec"
  fi
  rm -f /tmp/.copts.$$
fi

# Snapshot LAST, and only on a clean pass.
#
# This block used to sit before the model checks and gate on the gateway returning
# 200. A neurodesktop image upgrade reset opencode.json, wiping every model
# definition; the gateway still answered 200, so the first run of this script saved
# the broken config as "known-good". A backup of a broken state, labelled good, is
# worse than no backup.
#
# 200 from /models says the host is up. It says nothing about whether the configured
# model names resolve, which is the thing that actually broke.
OC="$HOME/.config/opencode/opencode.json"
KG="${BENCH_HOME:-$HOME/bench}/opencode.json.known-good"
if [ -f "$OC" ] && ! cmp -s "$OC" "$KG" 2>/dev/null; then
  mkdir -p "$(dirname "$KG")" && cp "$OC" "$KG" && echo "ok   config snapshotted"
fi

echo "PREFLIGHT OK"
exit 0
