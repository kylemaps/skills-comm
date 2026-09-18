# What "automated" has to mean — the benchmark side

**Status: draft, 2026-09-18.** Paired with cluster-explorer's pool spec, which covers
*how a job runs*. This covers *what runs, and what counts*. Where they touch, §7.

---

## 1. The loop we do not have

```
dispatch → run.yml → agent → grade → summarise → ARTIFACT
                                                    ↓
                                          [ a human downloads it ]
                                          [ re-summarises ]
                                          [ commits to results/ ]
                                                    ↓
                                    pages.yml (on: push, paths: results/**) → site
```

`run.yml` uploads an artifact and never commits. `pages.yml` triggers on `results/**`.
**So the trigger that publishes is never fired by the thing that produces results.**
Every number on the public board got there because a person moved it.

That is not an oversight to patch over. The human step is currently the only thing
enforcing every rule in §2, and automation has to replace those rules with mechanism,
not remove them.

---

## 2. The unit, and the rules the human is currently enforcing

**A cell is one task × one model × one arm × N repeats. It is complete or it does not
count.** No averaging over uneven denominators, ever. A cell with 7 of 10 runs is not
a weaker cell, it is not a cell.

What a person checks today, in their head, before a result is published:

| rule | why it exists |
|---|---|
| every rep present | a cell short three runs has a different denominator and nobody sees it |
| zero unresolved exclusions | excluded-and-never-re-run is strictly worse than scored generously |
| provenance constant within the cell | otherwise the cell averages over an environment change |
| the arm's skill hash is the one intended | a snapshot whose contents drifted from its name publishes a result naming bytes it did not use |
| the grader ref matches what the arm's other cells used | grading is pure, but not across grader versions |

**Each of these must become a gate that runs without a person, or it is not automated —
it is unsupervised.**

---

## 3. Who writes the result record

Three options. Recommending (b).

**(a) The cell job commits its own `summary_<task>.json`.**
Needs `contents: write` on a workflow that also holds `NEURODESK_API_KEY` and executes
a model's shell commands. That is a large permission in a job whose entire purpose is
running untrusted-ish output. Ten reps as ten jobs means ten writers racing one file.
Rejected.

**(b) The cell job uploads an artifact. A separate `assemble` workflow, triggered on
`workflow_run` completion, gates it and commits.** ← recommended
The gates in §2 run in a job with no gateway access, no API key, and no agent — the
same separation that makes grading validatable offline. It is the only writer, so
there is no race. It can refuse.

**(c) A human keeps committing.**
Honest about what we have. Fine while the sweep is 4 tasks; not fine at 5 tasks × 15
cells × 10 reps, which is 750 jobs.

**The assemble job is where "complete or it does not count" stops living in a head.**
It reads every artifact for the cell, applies §2, and either commits or fails with the
reason. A failed assemble leaves the artifacts intact for inspection — the evidence
must survive the gate rejecting it.

---

## 4. What stops a half-finished cell publishing

`pages.yml` triggers on `results/**`. If assemble is the only thing that writes there,
the gate and the trigger are the same event and there is nothing to keep in sync.

Two failure modes worth naming because they are not obvious:

- **A cell that never reports at all.** Nothing publishes, which is correct, and
  nothing complains, which is not. Absence is invisible on a dashboard that renders
  what exists. Needs a declared expected-cell list to diff against — otherwise a
  quietly missing cell is indistinguishable from one nobody asked for.
- **A cell that reports complete against the wrong denominator.** If `reps` is an
  input and the record just says "n=10", the record is self-certifying. The expected
  count has to come from the sweep definition, not from the job that ran.

---

## 5. Retries

**Retries stay explicit.** `retry_failed.sh` re-runs our own failures, and in CI that
is a loop that can spend without anyone deciding.

The rule that keeps it safe: **a retry is a new job for a named rep, never a re-run of
a cell.** Re-running a cell overwrites run directories and destroys transcripts, which
is how 7t and motion lost the evidence that would have settled the timeout argument.
One rep per job (§7) makes this natural rather than a discipline.

Exclusions that are ours — timeout, blank exit code, contamination, misassignment —
are re-run. `RETRYABLE_EXCLUSIONS` names that set in one place so a wording change
cannot silently drop a class, which it once did.

---

## 6. Provenance and pooling

Every run records `image_version`, `opencode_version`, `skills_sha`, `skills_hash`,
`prompt_hash`, `tasks_sha`. All but `skills_sha` decide pooling.

**The thing CI actually buys us is not speed. It is a pinned image.** Across 431 runs
we have three environment combinations and we chose none of them — the 2026-09-01
upgrade landed mid-sweep. Only one of four tasks is internally single-image. No pooled
statement is available from the data we have, and on the VM it never will be, because
the image changes under us.

`pool_check.py` answers the cross-task question; `summarize.py`'s `poolable` answers it
within a task. Both must run in assemble, and a cross-task pooling failure is a
**warning on the board, not a refusal to publish** — per-task results stay valid.

---

## 7. The seams with the pool spec

Settled by cluster-explorer reading the live cluster:

- **Concurrency.** ARC runner pods are ephemeral, one job each, own PVC. The shared
  `BENCH_HOME` collision does not exist there. Keep `concurrency: group: run` anyway —
  it is load-bearing on the VM and on any classic long-lived runner.
- **Retention.** The PVC dies with the pod. **Export-per-cell is therefore forced, not
  chosen:** a run tree not exported before the pod exits is gone. Design it as a hard
  requirement with a failure mode, not as cleanup.
- **Eviction.** The pool can evict mid-job by default; protection is a per-pod
  annotation that lives only in per-cluster overrides. An evicted job must land in the
  same bucket as a blank exit code — excluded, re-run, never graded. **To verify, not
  assume.**

Open, and coupled:

- **One rep per job vs one cell per job.** Explorer prefers one rep, conditional on a
  baked image: scale-to-zero works, eviction blast radius drops to a rep, parallelism
  becomes possible, and a failed rep becomes individually re-runnable. Against it,
  ~10× the cold starts. **The image decision and the granularity decision are one
  decision.** The image breaks a standing no-custom-image precedent and is Kyle's.
- **Egress.** `llm.neurodesk.org` is needed at pod start, before the agent runs, and it
  is absent from our 24-host list because that list came from transcripts — what agents
  mentioned, not what the harness did. Treat 24 as a floor.

---

## 8. Sequence

1. `run.yml` executable at all — blocked on `GRADER_REF` (upstream) and a pool.
2. One cell end to end on the pool, committed by hand. Proves the plane, changes nothing.
3. `assemble` with the §2 gates, running but not committing. Compare its verdict to the
   human's on cells we already published. **A gate that has never disagreed with a human
   has not been tested.**
4. Let it commit.
5. Expected-cell list and the missing-cell diff (§4).

Steps 3 and 4 are separate on purpose. Shipping a gate straight into the write path
means its first real decision is also its first unreviewed one.
