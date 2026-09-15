# Infrastructure failures that presented as model failures

Every fault in our environment or our harness that produced data reading as a model
performing badly, or that silently changed what a number meant.

**Why this file exists.** Each of these was diagnosed once, fixed, and then recalled
only approximately. Written down, the pattern is the argument: an agent benchmark
measures the environment as much as the agent, and a failure in the environment is
indistinguishable from incompetence unless something separates them. Every entry below
was initially read as "the model did badly".

**Inclusion rule.** The fault had to reach the data. Outages that cost time but
corrupted nothing are not here.

The count was informally quoted as five, then six, then nine while this list lived in
memory. Writing it out gave twelve, and a thirteenth was diagnosed the same afternoon.
That gap is the reason for the file.

---

### 1. Skill documentation counted as tool use

- **Presented as:** skill arms reaching for SynthStrip far more often than baselines.
- **Actually:** the tool detector matched `references/synthstrip.md`, and only skill
  arms have a `references/` directory. Reading about a tool scored as running it.
- **Cost:** a mechanism claim, briefly. Recomputed numbers were identical.
- **Prevented by:** detection restricted to command-form invocations in the transcript.

### 2. Ungraded runs scored as failures

- **Presented as:** models producing nothing.
- **Actually:** `NO-OUTPUT` was in `FAIL_VERDICTS`, so a run nobody had scored counted
  as a run that failed.
- **Prevented by:** a distinct `NOT-GRADED` state, guarded on `output_present`.

### 3. The 45-minute `RUN_TIMEOUT`

- **Presented as:** 51 runs across four tasks where the model delivered nothing.
- **Actually:** our own `timeout` killed them at exit 124. `INFRA_ERROR_RES` matched
  stderr strings, and a timeout is an exit code, so it never matched.
- **Cost:** the largest single distortion so far. On `diffusion-brain-mask` it hit 11 of
  80 runs and **unevenly**: 7 in baseline arms scored 0 as agent failures, while 4 in a
  skill arm had written a mask before dying and scored as passes. It inflated the effect
  from one side and deflated it from the other. On 7t it truncated kimi's baseline, whose
  runs have a 35-minute median and a 46.7-minute maximum, taking that cell from 9/10 to
  2/5 and the published kimi effect from +10pp to +50pp.
- **Found by:** noticing successful runs clustered just under the wall.
- **Prevented by:** exit 124 excluded even when output exists, since a killed run's
  output is an unknown intermediate; `RUN_TIMEOUT=5400` in `~/bench/.env` rather than in
  a shell.

### 4. Excluded runs were never re-run

- **Presented as:** complete-looking sweeps with quietly uneven denominators.
- **Actually:** `find_failed.py` selected on the literal prefix `harness failure`, but
  `summarize.py` excludes for four reasons we cause and only two are worded that way.
  Contaminated and misassigned runs were excluded and then forgotten.
- **Prevented by:** `RETRYABLE_EXCLUSIONS` names the set in one place.

### 5. A run killed by a restart, scored as a model failure

- **Presented as:** `NO-OUTPUT`, 7 runs across three tasks.
- **Actually:** blank exit code. `run_bench.sh` records an exit code for every run that
  finishes; blank means the process never got to report.
- **Prevented by:** blank exit code excluded, checked **before** the `== 124`
  comparison, since `"" == 124` is false and the run would otherwise fall through.

### 6. The gateway dropped the `neurodesk/` prefix

- **Presented as:** every run since 14 September dying instantly with no output. 10 runs
  across two arms, all counted as our harness failing.
- **Actually:** gateway model ids became bare (`glm-5.2`, not `neurodesk/glm-5.2`).
  A prefixed name is rejected as `Model ''`, a 404 that names nothing recognisable.
- **Found by:** `preflight.sh`, in one line, after the runs had already been spent.
  The tool existed and worked. It was not run before launching.
- **Prevented by:** running preflight before every sweep. This is a process fix, not a
  code fix.

### 7. The gateway behind an oauth2 proxy

- **Presented as:** the API key "stopped working"; 403 with or without it.
- **Actually:** `/v1/models` returned a sign-in page instead of JSON. `/health` stayed
  200, so anything checking only liveness saw nothing wrong.
- **Prevented by:** preflight parsing the response rather than the status code.

### 8. Two `env-only` runs loaded the skill

- **Presented as:** baseline runs performing suspiciously well.
- **Actually:** unknown. Never reproduced.
- **Prevented by:** arms serialised so it is structurally impossible, plus
  `contaminated:` detection on any baseline run that reads a skill file.

### 9. Two arms run concurrently

- **Presented as:** would have been a baseline run outperforming its arm.
- **Actually:** skills install to one container-global path. A second invocation created
  the symlinks while a baseline run was live. Caught 30 seconds in by the symlink
  timestamps, before any run completed.
- **Prevented by:** one arm per invocation, and the lock in `run_sweep.sh`.

### 10. A truncated task name

- **Presented as:** nothing. That is the problem.
- **Actually:** `structural-br-extraction-stroke` produced a run directory and fourteen
  graded outputs under a name no grader pack contains. The runs looked fine and belonged
  to nothing.
- **Prevented by:** `run_bench.sh` checks the task id against `tasks.json` before
  spending anything.

### 11. Disk and inode exhaustion

- **Presented as:** runs failing to write output.
- **Actually:** 78% disk, and inodes are the binding constraint rather than gigabytes:
  a retained run tree holds roughly 5,500 of them.
- **Prevented by:** `hygiene.sh` reports inodes, not only `df -h`, at the top of every
  session.

### 12. The shared opencode session database

- **Presented as:** the server would not start, for about a day. Diagnosed first as
  memory, then investigated as a rogue MCP server.
- **Actually:** our runs grew the shared session DB to 1 GB. Neurodesk compacts it at
  every login, taking about 7 minutes against a 2-minute startup limit.
- **Prevented by:** `OPENCODE_ISOLATE=1` by default, one DB per run.

### 13. An image upgrade reset `opencode.json`

- **Presented as:** every run from 14 September dying in seconds. `exit=1`, no output,
  no logs. 10 runs across two arms, all classified as our harness failing.
- **Actually:** the `2026-09-01` image upgrade reset `~/.config/opencode/opencode.json`.
  The `neurodesk` provider was left declaring one placeholder model called `neurodesk`,
  so `-m neurodesk/glm-5.2` resolved to nothing and the gateway answered
  `Model '' was not found`.
- **This is a recurrence.** `preflight.sh` has carried a comment about exactly this
  since it was written. A backup step was added on 10 September and had never run,
  because preflight had never been run.
- **Cost:** a day of runs, and an hour chasing the gateway. The 404 names an empty
  string, so the error points at the server rather than at the client's config.
- **Found by:** `preflight.sh`, again in one line, again after the spend.
- **Prevented by:** the config snapshot now happens **after** the model checks pass,
  at the end of the script. It previously gated on the gateway returning 200, so its
  first run saved the broken config as "known-good". A backup of a broken state,
  labelled good, is worse than no backup.

---

## What the list says

- **Six of the thirteen changed a published number.** Entries 1, 2, 3, 4, 5 and 6.
- **Entry 3 alone moved 51 runs**, and moved them unevenly between arms, which is worse
  than moving them all one way.
- **Four were caught by a tool that already existed** (3, 6, 11, 13) but was not run, or
  was run after the spend rather than before. Entry 13 is the sharpest case: the fault,
  the warning comment, and the backup that would have fixed it were all already in
  `preflight.sh`.
- **Two were caught only because someone looked at timestamps** (9) or at a duration
  distribution (3). Nothing would have flagged them.

## What follows from it

- **Preflight before every sweep**, hygiene at the top of every session. Most of the cost
  above is not missing tooling, it is tooling not run.
- **An infrastructure failure must never be scorable as a model result.** Where the two
  are indistinguishable in the data, the run is excluded and re-run, not graded.
- **Provenance has to be recorded per run and checked before pooling.** Entries 3, 5 and
  6 were all detectable in `run.json` before anyone looked at a pass rate.
- **Split the planes.** The agent and the grader share a filesystem on the Play server.
  CI separates them, which removes a whole class of this.
