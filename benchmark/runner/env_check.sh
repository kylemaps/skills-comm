#!/bin/bash
# env_check.sh: assert the agent's environment matches the benchmark spec before
# any tokens are spent. Exits 1 on the first mismatch.
#
#   BASH_ENV     file sourced by every non-interactive bash the agent starts; must
#                make `module` (Lmod) and the Neurodesk module tree available
#   FSL_VERSION  module version that must load, e.g. 6.0.7.22
#   BENCH_HOME   the agent works in a directory under $BENCH_HOME/work, which becomes
#                a run directory under $BENCH_HOME/runs; both, and every parent, must
#                be free of instruction files and project config
#   SKILLS_DST   where the arm's skill will be installed, if not the default
#                ~/.config/opencode/skills; checked along with every other directory
#                opencode discovers skills in
#   SKILLS_SRC   the arm's skill source; empty for env-only
#   SKILL_SEARCH_ROOT  tree searched for leftover SKILL.md files and grading material
#                (default $HOME)
#
# The spec: Neurodesk tools through Lmod, no standing instructions, no skills except
# the arm's, no grading material. run_bench.sh also checks, after installing the
# skill, what opencode itself resolves (`opencode debug skill`, `opencode debug config`).
set -u
fail() { echo "FAIL: $*"; exit 1; }
ok()   { echo "ok    $*"; }

# --- Lmod, in a fresh shell, as the agent gets it ------------------------------
[ -n "${BASH_ENV:-}" ] && [ -f "$BASH_ENV" ] || fail "BASH_ENV is unset or missing"
out=$(bash -c "module load fsl/${FSL_VERSION:?} && command -v bet" 2>&1) \
  || fail "module load fsl/$FSL_VERSION failed in a fresh bash: $out"
ok "module load fsl/$FSL_VERSION in a fresh bash ($out)"
# A `module` that accepts any name would pass the line above without loading
# anything, so an unknown module must fail.
if bash -c "module load env-check-no-such-module/0.0" >/dev/null 2>&1; then
  fail "\`module\` accepted a nonexistent module"
fi
ok "loading a nonexistent module fails"

# --- no standing instructions --------------------------------------------------
for v in OPENCODE_CONFIG OPENCODE_CONFIG_CONTENT; do
  [ -z "${!v:-}" ] || fail "$v is set; it adds config the agent would load"
done
cfg="$HOME/.config/opencode/opencode.json"
if [ -f "$cfg" ]; then
  n=$("${PYTHON:-python3}" -c 'import json,sys; print(len(json.load(open(sys.argv[1])).get("instructions") or []))' "$cfg") \
    || fail "cannot parse $cfg"
  [ "$n" = 0 ] || fail "$cfg declares $n instructions file(s)"
fi
ok "opencode config declares no instructions"

# The agent's working directory is a child of $BENCH_HOME/work; opencode reads
# project files from there upward. $BENCH_HOME/runs is where it ends up.
d="${BENCH_HOME:?}/work"
chain=("$BENCH_HOME/runs")
while :; do chain+=("$d"); [ "$d" = / ] && break; d=$(dirname "$d"); done
for d in "$HOME" "$HOME/.config/opencode" "$HOME/.claude" "${chain[@]}"; do
  for f in AGENTS.md CLAUDE.md CONTEXT.md; do
    p="${d%/}/$f"
    [ -e "$p" ] && fail "$p exists; it would be loaded as standing instructions"
  done
done
for d in "${chain[@]}"; do
  for f in opencode.json opencode.jsonc .opencode/opencode.json; do
    p="${d%/}/$f"
    [ -e "$p" ] && fail "$p exists; project config the agent would load"
  done
done
ok "no instruction files or project config above the run directory"

# --- no skills yet -------------------------------------------------------------
# Every directory opencode discovers skills in, globally and at project level from
# the run directory's parent upward. SKILLS_DST is checked as well, if set.
sk=("$HOME/.config/opencode/skills" "$HOME/.config/opencode/skill"
    "$HOME/.claude/skills" "$HOME/.agents/skills")
[ -n "${SKILLS_DST:-}" ] && sk+=("$SKILLS_DST")
for d in "${chain[@]}"; do
  for p in .opencode/skills .opencode/skill .claude/skills .agents/skills; do
    sk+=("${d%/}/$p")
  done
done
for s in "${sk[@]}"; do
  if [ -d "$s" ] && [ -n "$(ls -A "$s" 2>/dev/null)" ]; then
    fail "$s is not empty: $(ls -A "$s" | tr '\n' ' ')"
  fi
done
ok "no skill in any directory opencode discovers skills in"

# --- no other skill content on disk ----------------------------------------------
# Outside the discovery directories the agent can still read skill files by path.
# The only SKILL.md allowed under SKILL_SEARCH_ROOT is the arm's own source.
root="${SKILL_SEARCH_ROOT:-$HOME}"
[ -d "$root" ] || fail "SKILL_SEARCH_ROOT $root does not exist"
found=$(find "$root" -name SKILL.md -not -path '*/node_modules/*' 2>/dev/null \
        | grep -v "^${SKILLS_SRC:-/nonexistent}/" || true)
[ -z "$found" ] || fail "skill files readable outside the arm's source: $(echo "$found" | head -3 | tr '\n' ' ')"
ok "no skill files under $root except the arm's own"

# --- no grading material on disk -----------------------------------------------
# The task pack with its solution blocks, rubrics, grader scripts, published results
# and graded runs are not readable while the agent works. A task pack without
# solution blocks (the prompt-only copy the run uses) is allowed.
key=$(find "$root" -not -path '*/node_modules/*' \( -name tasks.json -o -name rubric.json \
        -o -name PROVENANCE.md -o -name CLAIMS.md -o -name INFRA_LEDGER.md \
        -o -name grade_wrapper.py -o -name fetch_reference.py -o -name run_manifest.json \
        -o -name envelope.json \) 2>/dev/null \
      | while IFS= read -r f; do
          case "$f" in
            */tasks.json) grep -q '"solution"' "$f" 2>/dev/null && echo "$f" ;;
            *) echo "$f" ;;
          esac
        done)
[ -z "$key" ] || fail "grading material readable: $(echo "$key" | head -3 | tr '\n' ' ')"
ok "no grading material under $root"
