#!/usr/bin/env python
"""How much of the effect is agents that never opened the skill?

    python uptake.py runs_<task>.csv [--arm env+skill] [--by-model]

qwen3 passed 7T 10 out of 10 having opened the skill 3 times. So part of the
headline sits on runs where the treatment was available and unused, and the
obvious question is what the skill does for an agent that actually reads it.

The obvious way to answer it is wrong. Comparing runs that opened the skill
against runs that did not is not an experiment: the agent chose, and whatever
made it open the skill may be the same thing that made it succeed. That
comparison is reported here and labelled as confounded, because someone will
compute it anyway and it is better to show it next to the reason not to trust it.

THE DESIGN IS ONE-SIDED NON-COMPLIANCE, WHICH HAS A PROPER ESTIMATOR
--------------------------------------------------------------------
The control arm cannot open the skill; it is not installed. So uptake in control
is zero by construction, nobody can defy assignment, and the complier average
causal effect is exactly

    CACE = ITT / uptake_rate

which is the Wald estimator. It answers "what does the skill do for an agent
that reads it", using randomisation rather than the agent's own choice.

It rests on the exclusion restriction: being in the skill arm must affect the
outcome ONLY through opening the skill. A run that never opened it should behave
like a control run.

THAT ASSUMPTION IS TESTABLE HERE, SO IT IS TESTED
-------------------------------------------------
Non-openers in the skill arm are compared directly against the control arm. If
they differ, the exclusion restriction fails and CACE is not interpretable. Two
ways it can fail that apply to this harness:

  - the skill's presence changes behaviour without being read (files on disk,
    a directory listing, a mention in the environment)
  - uptake is mismeasured, and runs recorded as non-openers did read it

A CACE above 100 percentage points is arithmetically impossible for a
probability difference and is reported as a diagnostic rather than a number: it
means the assumption is broken, the uptake rate is understated, or the ITT
estimate is noise. It is never quoted as an effect size.
"""
import argparse
import csv
from collections import defaultdict
from math import comb


def fisher(a, b, c, d):
    n = a + b + c + d
    if not n:
        return float("nan")
    r1, c1 = a + b, a + c

    def p(x):
        return (comb(r1, x) * comb(n - r1, c1 - x)) / comb(n, c1)
    obs = p(a)
    lo, hi = max(0, c1 - (n - r1)), min(r1, c1)
    return sum(p(x) for x in range(lo, hi + 1) if p(x) <= obs * (1 + 1e-9))


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    ph = k / float(n)
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * ((ph * (1 - ph) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (max(0.0, c - h), min(1.0, c + h))


def newcombe(k1, n1, k2, n2):
    l1, u1 = wilson(k1, n1)
    l2, u2 = wilson(k2, n2)
    p1, p2 = k1 / float(n1), k2 / float(n2)
    d = p1 - p2
    return (100 * (d - ((p1 - l1) ** 2 + (u2 - p2) ** 2) ** 0.5),
            100 * (d + ((u1 - p1) ** 2 + (p2 - l2) ** 2) ** 0.5))


def rate(k, n):
    if not n:
        return "-"
    return "%d/%d %.0f%%" % (k, n, 100.0 * k / n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--baseline", default="env-only")
    ap.add_argument("--arm", default="env+skill")
    ap.add_argument("--by-model", action="store_true")
    a = ap.parse_args()

    with open(a.csv_path, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if str(r.get("valid", "1")).lower() not in ("0", "false", "")]
    if not rows:
        raise SystemExit("no valid runs in %s" % a.csv_path)

    def yes(r, f):
        return str(r.get(f, "")).lower() in ("1", "true", "yes")

    ctrl = [r for r in rows if r["arm"] == a.baseline]
    trt = [r for r in rows if r["arm"] == a.arm]
    if not ctrl or not trt:
        raise SystemExit("need both %s and %s" % (a.baseline, a.arm))

    opened = [r for r in trt if yes(r, "uptake")]
    unopened = [r for r in trt if not yes(r, "uptake")]

    kc, nc = sum(1 for r in ctrl if yes(r, "passed")), len(ctrl)
    kt, nt = sum(1 for r in trt if yes(r, "passed")), len(trt)
    ko, no_ = sum(1 for r in opened if yes(r, "passed")), len(opened)
    ku, nu = sum(1 for r in unopened if yes(r, "passed")), len(unopened)

    print("=== %s: %s vs %s %s"
          % (a.csv_path.split("/")[-1], a.arm, a.baseline, "=" * 14))
    print("")
    print("  uptake in %s: %s opened the skill" % (a.arm, rate(len(opened), nt)))
    print("")
    print("  %-34s%14s" % ("control (skill unavailable)", rate(kc, nc)))
    print("  %-34s%14s" % ("skill arm, all runs (ITT)", rate(kt, nt)))
    print("  %-34s%14s" % ("skill arm, opened it", rate(ko, no_)))
    print("  %-34s%14s" % ("skill arm, never opened it", rate(ku, nu)))

    itt = 100.0 * (kt / float(nt) - kc / float(nc))
    ilo, ihi = newcombe(kt, nt, kc, nc)
    print("")
    print("  ITT effect            %+.0f pp   [%+.0f, %+.0f]   p=%.4f"
          % (itt, ilo, ihi, fisher(kt, nt - kt, kc, nc - kc)))

    # --- exclusion restriction, tested rather than assumed ---------------
    print("")
    print("  --- is the exclusion restriction plausible? ---")
    if nu == 0:
        print("  Every run opened the skill, so there is nothing to test and")
        print("  ITT and CACE coincide.")
        ok = True
    else:
        d = 100.0 * (ku / float(nu) - kc / float(nc))
        lo, hi = newcombe(ku, nu, kc, nc)
        p = fisher(ku, nu - ku, kc, nc - kc)
        print("  non-openers vs control: %+.0f pp  [%+.0f, %+.0f]  p=%.4f"
              % (d, lo, hi, p))
        ok = p >= 0.05
        if ok:
            print("  They do not separate, which is consistent with the skill")
            print("  acting only through being read. Consistent, not proven:")
            print("  n is small and this test has little power.")
        else:
            print("  !! They DO separate. Being in the skill arm changes the")
            print("     outcome without the skill being opened, so CACE below is")
            print("     not interpretable. Either the skill's presence acts")
            print("     without being read, or uptake is mismeasured.")

    # --- CACE -------------------------------------------------------------
    u = len(opened) / float(nt)
    print("")
    print("  --- effect for an agent that reads it (CACE = ITT / uptake) ---")
    if u == 0:
        print("  Uptake is zero; CACE is undefined.")
    else:
        cace, clo, chi = itt / u, ilo / u, ihi / u
        if abs(cace) > 100:
            print("  CACE = %+.0f pp, which is impossible for a difference of"
                  % cace)
            print("  probabilities. Read it as a diagnostic, not an effect: with")
            print("  uptake at %.0f%% the ITT of %+.0f pp cannot be produced by"
                  % (100 * u, itt))
            print("  compliers alone. The uptake rate is understated, the")
            print("  exclusion restriction fails, or the ITT is noise.")
        else:
            print("  CACE = %+.0f pp   [%+.0f, %+.0f]" % (cace, clo, chi))
            if not ok:
                print("  (uninterpretable -- the exclusion restriction failed above)")

    print("")
    print("  The opened-versus-never-opened comparison above is CONFOUNDED and")
    print("  is shown only because someone will compute it: the agent chose")
    print("  whether to open the skill, so that split is not randomised. CACE")
    print("  uses assignment, which is.")

    if a.by_model:
        print("")
        print("  --- uptake by model ---")
        g = defaultdict(lambda: [0, 0])
        for r in trt:
            c = g[r["model"]]
            c[0] += 1
            c[1] += 1 if yes(r, "uptake") else 0
        print("  %-16s%14s%22s" % ("MODEL", "uptake", "ITT pass (skill/ctrl)"))
        for m in sorted(g):
            n, k = g[m]
            mc = [r for r in ctrl if r["model"] == m]
            mt = [r for r in trt if r["model"] == m]
            print("  %-16s%14s%22s"
                  % (m[:16], rate(k, n),
                     "%s / %s"
                     % (rate(sum(1 for r in mt if yes(r, "passed")), len(mt)),
                        rate(sum(1 for r in mc if yes(r, "passed")), len(mc)))))
        print("  A model passing at a high rate on low uptake is carrying part")
        print("  of the headline without using the treatment.")


if __name__ == "__main__":
    main()
