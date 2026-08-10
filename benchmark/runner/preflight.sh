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
echo "PREFLIGHT OK"
exit 0
