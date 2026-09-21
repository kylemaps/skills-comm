#!/usr/bin/env python3
"""Did this machine grade the pack the same way the machine that built it did?

    python3 regrade_diff.py --expected expected --got out --task <task>

WHAT THIS IS
The re-grade pack ships known inputs and a known-correct output. Re-grading and
diffing is the cheapest end-to-end check of a new execution environment, because
grading is pure: run directories in, verdicts out, no network, no agent, no tokens.

MANIFEST.md has always said which fields to compare and which to ignore. Nothing
implemented it. Both re-grades so far were diffed by hand, which works for 20 runs
and does not work in a workflow, and "compare these fields, and only these" is
precisely the instruction a person carries out slightly differently each time.

COMPARE THESE, AND ONLY THESE
`verdict` `score` `dice` `passed`, joined on (model, arm, rep). Everything else in
runs.csv is environment-dependent and differs on every machine: durations,
timestamps, token counts, session ids, paths. A difference there is not a finding,
and reporting it would bury the four fields that are.

WHAT A DIFFERENCE MEANS
A Dice differing in the fourth decimal is floating-point noise between library
versions. A flipped `passed` is not. Both are reported; only the second is fatal by
default, because a check that fails on noise gets ignored and an ignored check is
worse than none.
"""
import argparse
import csv
import os
import sys

# The only fields that are a property of the grader rather than of the machine.
FIELDS = ["verdict", "score", "dice", "passed"]

# Below this, a float difference is library noise rather than a different answer.
# MANIFEST.md says a fourth-decimal Dice difference is expected between BLAS builds.
# It has never actually appeared -- the two independent re-grades so far were exact
# -- so this is headroom, not an observed need.
FLOAT_EPS = 1e-4


def load(path, task):
    """{(model, arm, rep): row} from runs_<task>.csv."""
    f = os.path.join(path, "runs_%s.csv" % task)
    if not os.path.exists(f):
        raise SystemExit("FAIL: no %s\n      Nothing to compare. If the grading step "
                         "succeeded, it wrote somewhere else." % f)
    out = {}
    for r in csv.DictReader(open(f, encoding="utf-8")):
        # The run directory name carries a `neurodesk-` prefix the csv does not, and
        # the two sides of this comparison can come from different harness versions.
        # Normalise rather than trusting them to agree.
        key = (r["model"].replace("neurodesk-", "").replace("neurodesk/", ""),
               r["arm"], str(r["rep"]))
        out[key] = r
    return out


def differs(field, a, b):
    """True if these disagree. Returns (bool, is_noise)."""
    if a == b:
        return False, False
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return True, False
    if fa == fb:
        return False, False
    return True, abs(fa - fb) < FLOAT_EPS


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expected", required=True)
    ap.add_argument("--got", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--allow-noise", action="store_true",
                    help="exit 0 when the only differences are below %g" % FLOAT_EPS)
    a = ap.parse_args()

    exp, got = load(a.expected, a.task), load(a.got, a.task)

    print("=== %s" % a.task)
    print("  expected %d runs, got %d" % (len(exp), len(got)))

    # Missing and extra are failures in their own right, and they are the ones a
    # field-by-field diff silently skips: iterating the intersection would compare
    # nine runs, find them identical, and report success about a ten-run pack.
    missing = sorted(set(exp) - set(got))
    extra = sorted(set(got) - set(exp))
    for k in missing:
        print("  MISSING  %s/%s/r%s -- expected, not graded here" % k)
    for k in extra:
        print("  EXTRA    %s/%s/r%s -- graded here, not in expected" % k)

    hard, noise = [], []
    for k in sorted(set(exp) & set(got)):
        for f in FIELDS:
            if f not in exp[k] or f not in got[k]:
                continue
            d, is_noise = differs(f, exp[k][f], got[k][f])
            if not d:
                continue
            msg = "  %-8s %s/%s/r%s  %s: expected %r got %r" % (
                "noise" if is_noise else "DIFFERS", k[0], k[1], k[2],
                f, exp[k][f], got[k][f])
            (noise if is_noise else hard).append(msg)

    for m in noise + hard:
        print(m)

    print()
    n = len(set(exp) & set(got))
    if not hard and not missing and not extra:
        if noise:
            print("%d runs match on %s, with %d float difference(s) below %g."
                  % (n, "/".join(FIELDS), len(noise), FLOAT_EPS))
            print("That is library noise between BLAS builds, not a different answer.")
            return 0
        print("%d of %d runs identical on %s." % (n, len(exp), "/".join(FIELDS)))
        print("Grading on this machine is equivalent to grading on the one that built")
        print("the pack. That is the only claim this makes -- it says nothing about")
        print("CVMFS, module load, the gateway or the agent, which is where every")
        print("remaining unknown lives.")
        return 0

    print("NOT EQUIVALENT.")
    if missing or extra:
        print("  %d missing, %d extra. A run-set difference is a bigger problem than"
              % (len(missing), len(extra)))
        print("  a field difference: something did not grade, or graded twice.")
    if hard:
        print("  %d field difference(s) above the noise threshold. A flipped verdict"
              % len(hard))
        print("  or passed means the grading environment is not equivalent, and any")
        print("  number produced here cannot be compared to one produced there.")
    if noise and a.allow_noise and not hard and not missing and not extra:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
