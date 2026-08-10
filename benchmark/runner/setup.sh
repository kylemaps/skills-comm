#!/bin/bash
# setup.sh — prepare a fresh Neurodesk instance to run the benchmark harness.
# Idempotent: safe to re-run.
set -u

SKILLS_REPO="${SKILLS_REPO:-https://github.com/nipreps/skills-comm.git}"
GRADER_REPO="${GRADER_REPO:-https://github.com/MoniDoerig/skills-comm.git}"
GRADER_BRANCH="${GRADER_BRANCH:-feat/brain-extraction-grader}"

echo "=== 1. skills source (~/skills-comm) ==="
if [ -d "$HOME/skills-comm/.git" ]; then
  git -C "$HOME/skills-comm" pull --quiet 2>/dev/null && echo "  updated" || echo "  present (pull skipped)"
else
  git clone --quiet "$SKILLS_REPO" "$HOME/skills-comm" && echo "  cloned"
fi

echo "=== 2. grader + task definitions (~/grader-repo) ==="
if [ -d "$HOME/grader-repo/.git" ]; then
  git -C "$HOME/grader-repo" pull --quiet 2>/dev/null && echo "  updated" || echo "  present (pull skipped)"
else
  git clone --quiet --branch "$GRADER_BRANCH" --single-branch "$GRADER_REPO" "$HOME/grader-repo" \
    && echo "  cloned"
fi

echo "=== 3. working dirs ==="
mkdir -p "$HOME/bench/runs" "$HOME/.config/opencode/skills"
echo "  ok"

echo "=== 4. environment ==="
echo "  image:    ${NEURODESKTOP_VERSION:-unknown}"
echo "  opencode: $(/usr/bin/opencode --version 2>/dev/null || echo MISSING)"
echo "  sbatch:   $(command -v sbatch || echo MISSING)"
for m in numpy nibabel scipy; do
  python -c "import $m" 2>/dev/null && echo "  python:   $m ok" || echo "  python:   $m MISSING"
done
if [ -n "${NEURODESK_API_KEY:-}" ]; then
  echo "  api key:  set"
else
  echo "  api key:  NOT SET  ->  run 'opencode' once (enter key, pick model, exit), then 'source ~/.bashrc'"
fi

echo
echo "Next:"
echo "  ~/skills-comm/benchmark/runner/preflight.sh neurodesk/minimax-m2 neurodesk/kimi-k3"
echo "  ~/skills-comm/benchmark/runner/run_matrix.sh --help"
