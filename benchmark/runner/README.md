# Benchmark runner (execution plane)

Runs an LLM agent through a benchmark task **unattended**, and leaves a submission that the
[grading plane](../harness/) can score. The two planes are deliberately separate: the agent needs
the full neuroimaging stack (Lmod, SLURM, CVMFS), the grader needs only `numpy`/`nibabel`/`scipy`
and must never share a machine with the ground truth the agent could read.

## Setup on a fresh Neurodesk instance

```bash
git clone https://github.com/nipreps/skills-comm.git ~/skills-comm
bash ~/skills-comm/benchmark/runner/setup.sh
```

If `setup.sh` reports `api key: NOT SET`, run `opencode` once interactively (enter your
llm.neurodesk.org key, pick any model, decline the Brain Researcher MCP, exit the TUI), then
`source ~/.bashrc`. That step also makes the wrapper write all available models into
`~/.config/opencode/opencode.json`, which the runner needs.

## Running

```bash
R=~/skills-comm/benchmark/runner

# 1. assert the environment is sane before spending hours
$R/preflight.sh neurodesk/minimax-m2 neurodesk/kimi-k3

# 2. one cell, foreground (good for a smoke test)
$R/run_bench.sh structural-brain-extraction-7t neurodesk/kimi-k3 env-only 1

# 3. a matrix, detached
nohup $R/run_matrix.sh structural-brain-extraction-7t 3 \
    neurodesk/minimax-m2 neurodesk/qwen3.5-122b neurodesk/kimi-k3 neurodesk/glm-5.2 \
    < /dev/null > ~/bench/matrix.log 2>&1 &
disown

# 4. grade + tabulate
$R/collect_results.sh structural-brain-extraction-7t
```

Runs land in `~/bench/runs/<task>__<model>__<condition>__r<N>/`, each containing `prompt.txt`,
`run.json` (provenance), `transcript.txt`, and `submissions/<task>/output.nii.gz`.

## Experimental design

| arm | agent context | task skill symlinked |
|---|---|---|
| `env-only` | whatever the image ships | no |
| `env+skill` | same | yes |

`env-only` is the headline baseline — users always have an agent context, so
**`env+skill − env-only` is the real-world value of a task skill.**

Three invariants make the arms comparable, and each was learned the hard way:

- **Fresh working directory per run.** A reused directory lets one run read the previous run's
  `logs/`, scripts and outputs.
- **The prompt is byte-identical across arms.** It is assembled from the task contract in
  `tasks.json` plus [`wrapper.txt`](wrapper.txt); the *only* thing that varies is whether the
  skills are symlinked into the discovery path.
- **Everything is pinned in `run.json`** — image version, opencode version, skills SHA, tasks SHA,
  which skills were installed. An image upgrade once changed the agent context from 88 to 222
  lines mid-experiment; without this field those runs would have been silently pooled with
  incomparable ones.

## Gotchas encoded here

| Symptom | Cause | Handled by |
|---|---|---|
| `EBADF: bad file descriptor` on every backgrounded run | `nohup … &` leaves stdin inherited from the terminal; the fd goes bad once disowned | `< /dev/null` on the opencode call |
| `rm -rf` leaves a run dir behind | git-annex marks its objects read-only | `chmod -R u+w` before `rm -rf`, then assert |
| Whole matrix fails in seconds | an image upgrade reset `opencode.json`, so the requested model no longer resolves | [`preflight.sh`](preflight.sh) |
| Arms contaminate each other | skills live in a container-global path | matrix runs arms sequentially |

## Notes

- Skills are discovered from `~/.config/opencode/skills/`, `~/.claude/skills/` or
  `~/.agents/skills/`. The frontmatter `name` **must** match the directory name.
- Long sweeps outlive a browser session, but not the container. On hosted instances with idle
  culling, prefer several short batches over one long one.
- `neurodesk/neurodesk` looks like an alias/router rather than a pinned model — avoid it in a
  leaderboard, where reproducibility depends on knowing exactly which model ran.
