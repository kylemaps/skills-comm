#!/usr/bin/env python3
"""Gate one cell before it is allowed to become a published result.

    python3 assemble_cell.py --runs bench/runs --task T --model M --arm A \\
        --sweep benchmark/ci/sweep.json --stage results/runs
    python3 assemble_cell.py ... --explain      # print the gates and stop

WHAT THIS IS FOR
`run.yml` produces an artifact. `pages.yml` publishes `results/**`. Nothing connects
them, so every number on the public board got there because a person moved it -- and
that person was also, in their head, the only thing enforcing "a cell is complete or
it does not count".

This is that person's checklist, made mechanical. It runs in a job with no gateway,
no API key and no agent: the same separation that lets grading be validated offline,
and the reason the cell job is not allowed to commit its own result.

IT REFUSES. That is the whole value. A gate that cannot refuse is a transport step
with a log line.

WHY IT RE-DERIVES RATHER THAN TRUSTING THE ARTIFACT
The cell job uploads `bench/report/` alongside the raw runs. This ignores it and
recomputes from `run.json` + `envelope.json`. Grading is a pure function of the runs,
so the recomputation is free, and a summary that travelled with the thing it
summarises is not independent evidence about it.

WHAT IT DOES NOT DO
It does not decide whether a result is interesting, and it does not look at pass
rates. Every gate here is about whether the cell is a valid measurement, not about
what it measured. A gate that could be influenced by the outcome is not a gate.
"""
import argparse
import glob
import json
import os
import shutil
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from summarize import (PROVENANCE_KEYS, UNRECORDED, RETRYABLE_EXCLUSIONS,  # noqa: E402
                       load_run)

# Provenance that must not vary inside one cell. skills_sha is excluded for the same
# reason summarize.py excludes it from pooling: the commit a snapshot came from can
# differ while the bytes are identical, and it is the bytes that define the arm.
DECIDES = [k for k in PROVENANCE_KEYS if k != "skills_sha"]


class Gate:
    """One check, its verdict, and what it is protecting against."""

    def __init__(self, name, why):
        self.name, self.why = name, why
        self.failures = []

    def fail(self, msg):
        self.failures.append(msg)

    @property
    def ok(self):
        return not self.failures


def gates_for(runs, task, model, arm, spec):
    g = []

    # ---- complete ----------------------------------------------------------
    want = spec["reps"]
    c = Gate("complete", "A cell with 7 of 10 runs is not a weaker cell, it is not a "
                         "cell. Averaging over uneven denominators is the one thing "
                         "we never do, and nothing downstream can see it happened.")
    if len(runs) != want:
        c.fail("%d runs, expected %d (from sweep.json, not from the job's own input)"
               % (len(runs), want))
    reps = Counter(r["rep"] for r in runs)
    dupes = [k for k, n in reps.items() if n > 1]
    if dupes:
        c.fail("repeat(s) present more than once: %s" % ", ".join(sorted(dupes)))
    g.append(c)

    # ---- no unresolved exclusions -----------------------------------------
    e = Gate("no unresolved exclusions",
             "An exclusion is only defensible if the run comes back. A cell excluded "
             "and never re-run is strictly worse than one scored generously, because "
             "it looks complete.")
    for r in runs:
        if r.get("exclude_reason"):
            retryable = any(r["exclude_reason"].startswith(p)
                            for p in RETRYABLE_EXCLUSIONS)
            e.fail("%s/%s rep %s: %s%s"
                   % (r["model"], r["arm"], r["rep"], r["exclude_reason"],
                      "  [ours -- re-run it]" if retryable else ""))
    g.append(e)

    # ---- one environment ---------------------------------------------------
    p = Gate("one environment",
             "A cell whose runs span an image upgrade averages over the upgrade "
             "without saying so. We have three environment combinations across 431 "
             "runs and chose none of them.")
    for k in DECIDES:
        vals = {r[k] for r in runs if r.get(k) not in UNRECORDED}
        if len(vals) > 1:
            p.fail("%s varies within the cell: %s" % (k, ", ".join(sorted(vals))))
    g.append(p)

    # ---- the arm is the arm ------------------------------------------------
    a = Gate("the arm is what it claims",
             "A snapshot whose contents drifted from its directory name still runs, "
             "and its result names a hash describing different bytes. The arm is "
             "defined by content, not by a label.")
    want_hash = (spec.get("arms", {}).get(arm) or {}).get("skills_hash")
    if want_hash:
        got = {r["skills_hash"] for r in runs if r.get("skills_hash") not in UNRECORDED}
        if got and got != {want_hash}:
            a.fail("arm %s expects skills_hash %s, runs carry %s"
                   % (arm, want_hash, ", ".join(sorted(got))))
        elif not got:
            a.fail("arm %s expects skills_hash %s and no run recorded one"
                   % (arm, want_hash))
    g.append(a)

    # There was a sixth gate here, "everything is graded", and it was decoration.
    # summarize.py already excludes an ungraded run that produced output, with the
    # reason "not graded yet -- run the grader on this task", so the exclusions gate
    # above catches it first and says it better. A run that is ungraded with NO
    # output is not an ungraded measurement at all, it is a genuine no-output result
    # and should be scored as one.
    #
    # Found by asserting WHICH gate refused rather than only that something did: the
    # test passed on the exit code and failed on the gate name. A gate that can only
    # fire where another already fired adds a line to the output and nothing to the
    # decision.
    return g


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", required=True, help="a runs/ directory from the artifact")
    ap.add_argument("--task", required=True)
    ap.add_argument("--model", required=True, help="bare id, no neurodesk/ prefix")
    ap.add_argument("--arm", required=True)
    ap.add_argument("--sweep", default=os.path.join(HERE, "..", "ci", "sweep.json"))
    ap.add_argument("--stage", help="if the gates pass, copy the graded facts here")
    ap.add_argument("--explain", action="store_true",
                    help="print what each gate protects against, and stop")
    a = ap.parse_args()

    spec = json.load(open(a.sweep, encoding="utf-8"))
    if a.explain:
        for g in gates_for([], a.task, a.model, a.arm, spec):
            print("%-28s %s" % (g.name, g.why))
        return 0

    if a.task not in spec["tasks"]:
        raise SystemExit("REFUSED: %s is not a task in %s. A cell nobody declared is "
                         "not a result." % (a.task, a.sweep))
    if a.arm not in spec["tasks"][a.task]["arms"]:
        raise SystemExit("REFUSED: arm %s is not declared for %s" % (a.arm, a.task))

    model = a.model.replace("neurodesk/", "")
    dirs = [d for d in sorted(glob.glob(os.path.join(a.runs, "%s__*" % a.task)))
            if os.path.isdir(d)]
    runs = [load_run(d, a.task) for d in dirs]
    runs = [r for r in runs if r["model"] == model and r["arm"] == a.arm]

    print("cell: %s / %s / %s" % (a.task, model, a.arm))
    print("%d run directories matched\n" % len(runs))
    if not runs:
        raise SystemExit("REFUSED: no runs matched this cell. An empty cell is not a "
                         "complete one -- check the model prefix and the arm label.")

    gates = gates_for(runs, a.task, model, a.arm, spec)
    for g in gates:
        print("  %-5s %s" % ("ok" if g.ok else "FAIL", g.name))
        for f in g.failures:
            print("          %s" % f)

    bad = [g for g in gates if not g.ok]
    if bad:
        print("\nREFUSED: %s" % ", ".join(g.name for g in bad))
        print("\nThe artifact is untouched. Whatever is wrong here is diagnosable from")
        print("it, and a gate that deletes its own evidence on refusal is worse than")
        print("no gate. Fix or re-run the cell; do not hand-edit results/.")
        return 1

    print("\nPASSED all %d gates." % len(gates))
    if a.stage:
        # Only the graded facts. Transcripts stay in the artifact: they are the bulk,
        # and they are evidence rather than result. run.json and envelope.json are
        # what summarize.py reads, so the task summary stays fully re-derivable from
        # what is committed.
        dest = os.path.join(a.stage, a.task)
        os.makedirs(dest, exist_ok=True)
        n = 0
        for r in runs:
            d = os.path.join(dest, os.path.basename(r["dir"].rstrip("/\\")))
            os.makedirs(d, exist_ok=True)
            for f in ("run.json", "envelope.json"):
                src = os.path.join(r["dir"], f)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(d, f))
                    n += 1
        print("staged %d files to %s" % (n, dest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
