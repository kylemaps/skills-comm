#!/bin/bash
# preflight.sh MODEL [MODEL...]
#
# Assert the environment is sane BEFORE burning a multi-hour matrix. Added after
# a neurodesktop image upgrade silently reset ~/.config/opencode/opencode.json,
# leaving the requested model undeclared — six runs died in 37 seconds.
set -u

# Secrets. Some Neurodesk images ship a root-owned ~/.bashrc, so the opencode
# wrapper cannot persist NEURODESK_API_KEY there. ~/bench/.env always works.
[ -f "${BENCH_HOME:-$HOME/bench}/.env" ] && . "${BENCH_HOME:-$HOME/bench}/.env"
FAIL=0

if [ -z "${NEURODESK_API_KEY:-}" ]; then
  echo "FAIL: NEURODESK_API_KEY not set (run 'opencode' once, then 'source ~/.bashrc')"
  FAIL=1
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
  -H "Authorization: Bearer ${NEURODESK_API_KEY:-}" \
  https://llm.neurodesk.org/openai/models 2>/dev/null)
if [ "$code" != "200" ]; then
  echo "FAIL: llm.neurodesk.org returned $code"
  FAIL=1
else
  echo "ok   gateway 200"
fi

AVAIL=$(timeout 60 /usr/bin/opencode models 2>/dev/null)
for m in "$@"; do
  if echo "$AVAIL" | grep -qx "$m"; then
    echo "ok   $m"
  else
    echo "FAIL: $m not available (re-run 'opencode' so the wrapper rewrites the config)"
    FAIL=1
  fi
done

if [ "$FAIL" = 0 ]; then echo "PREFLIGHT OK"; else echo "PREFLIGHT FAILED"; fi
exit $FAIL
