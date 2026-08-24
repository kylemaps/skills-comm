#!/usr/bin/env python
"""Do skills help weak models most, or is that just the ceiling?

    python capability.py runs_<task>.csv [--arm env+skill] [--outcome pass|tool]
                                         [--robust synthstrip,hd-bet,afni]

"Skills substitute for model capability" is an attractive claim. It matters to
anyone running a small model locally, and it predicts the effect shrinks as
models improve, which is falsifiable rather than promotional.

It is also the claim most likely to be an artefact, so this script is built to
attack it.

THE CEILING PROBLEM
-------------------
On 7T tool selection, glm-5.2 starts at 8/10 and can gain at most 2. minimax-m2
starts at 1/10 and can gain 9. Ranking models by percentage-point improvement
therefore ranks them by how much room they had, and "skills help weak models
most" comes out true by arithmetic before any skill is involved.

Two metrics are reported side by side and neither is allowed to stand alone:

  ABSOLUTE   p_skill - p_base
             What you gain. Bounded by headroom, so it favours weak models.

  RFR        1 - (1 - p_skill) / (1 - p_base)
             Relative failure reduction: the share of the baseline's failures
             that the skill eliminated. Scale-free, so a model at 8/10 going to
             10/10 scores 100% -- it removed every failure it had.

On these runs the two metrics disagree about which models benefit, which is the
finding. Quoting either alone would be quoting an artefact of the other.

RFR is undefined at a baseline of 100% (no failures to remove) and reads 100%
whenever the skill arm is perfect, so it is printed as "-" and "100%" rather
than being smoothed. A model with one baseline failure has an RFR of either 0 or
100 and nothing between; the n column is there to keep that visible.

WHAT THE POOLED TEST IS
-----------------------
Models are split at the median baseline and the two groups pooled. With five
models any regression of improvement on capability would be n=5 dressed up, so
this reports two pooled deltas with intervals and says whether they separate. It
is descriptive. It is not a test of an interaction.
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
    """CI for a difference of proportions, method 10. Sane at 0 and at 1."""
    l1, u1 = wilson(k1, n1)
    l2, u2 = wilson(k2, n2)
    p1, p2 = k1 / float(n1), k2 / float(n2)
    d = p1 - p2
    lo = d - ((p1 - l1) ** 2 + (u2 - p2) ** 2) ** 0.5
    hi = d + ((u1 - p1) ** 2 + (p2 - l2) ** 2) ** 0.5
    return 100 * lo, 100 * hi


def rfr(kb, nb, ks, ns):
    """Share of the baseline's failures the skill removed, or None if undefined."""
    fb = 1 - kb / float(nb)
    if fb <= 0:
        return None
    fs = 1 - ks / float(ns)
    return 100.0 * (1 - fs / fb)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--baseline", default="env-only")
    ap.add_argument("--arm", default="env+skill")
    ap.add_argument("--outcome", default="pass", choices=("pass", "tool"))
    ap.add_argument("--robust", default="synthstrip,hd-bet,afni")
    a = ap.parse_args()

    with open(a.csv_path, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if str(r.get("valid", "1")).lower() not in ("0", "false", "")]
    if not rows:
        raise SystemExit("no valid runs in %s" % a.csv_path)

    robust = [t.strip() for t in a.robust.split(",") if t.strip()]

    def hit(r):
        if a.outcome == "pass":
            return str(r.get("passed", "")).lower() in ("1", "true", "yes")
        m = (r.get("methods") or "").replace(",", ";").split(";")
        return any(t in robust for t in m if t)

    cell = defaultdict(lambda: [0, 0])
    for r in rows:
        c = cell[(r["model"], r["arm"])]
        c[0] += 1
        c[1] += 1 if hit(r) else 0

    models = sorted({r["model"] for r in rows})
    models = [m for m in models
              if cell[(m, a.baseline)][0] and cell[(m, a.arm)][0]]
    if not models:
        raise SystemExit("no model has both %s and %s" % (a.baseline, a.arm))

    label = "passed" if a.outcome == "pass" else "reached a panel tool"
    print("=== %s: %s, %s vs %s %s"
          % (a.csv_path.split("/")[-1], label, a.arm, a.baseline, "=" * 8))

    rank = sorted(models,
                  key=lambda m: cell[(m, a.baseline)][1] / cell[(m, a.baseline)][0])
    print("")
    print("  weakest baseline first")
    print("  %-16s%12s%12s%11s%20s%9s%9s"
          % ("MODEL", "baseline", "skill", "absolute",
             "95% CI (abs)", "RFR", "p"))
    for m in rank:
        nb, kb = cell[(m, a.baseline)]
        ns, ks = cell[(m, a.arm)]
        d = 100.0 * (ks / float(ns) - kb / float(nb))
        lo, hi = newcombe(ks, ns, kb, nb)
        rr = rfr(kb, nb, ks, ns)
        print("  %-16s%12s%12s%+10.0f%s%20s%9s%9.3f"
              % (m[:16], "%d/%d" % (kb, nb), "%d/%d" % (ks, ns), d, "pp",
                 "[%+.0f, %+.0f]" % (lo, hi),
                 "-" if rr is None else "%.0f%%" % rr,
                 fisher(ks, ns - ks, kb, nb - kb)))

    print("")
    print("  ABSOLUTE is bounded by headroom, so it favours weak models by")
    print("  construction. RFR is the share of the baseline's failures removed,")
    print("  which is scale-free. Read both or neither.")

    # --- pooled, split at the median baseline ---------------------------
    half = len(rank) // 2
    groups = [("weaker half", rank[:half or 1]), ("stronger half", rank[half or 1:])]
    print("")
    print("  --- pooled at the median baseline (descriptive; %d models) ---"
          % len(rank))
    print("  %-16s%12s%12s%11s%20s%9s"
          % ("GROUP", "baseline", "skill", "absolute", "95% CI (abs)", "RFR"))
    res = []
    for name, ms in groups:
        if not ms:
            continue
        nb = sum(cell[(m, a.baseline)][0] for m in ms)
        kb = sum(cell[(m, a.baseline)][1] for m in ms)
        ns = sum(cell[(m, a.arm)][0] for m in ms)
        ks = sum(cell[(m, a.arm)][1] for m in ms)
        d = 100.0 * (ks / float(ns) - kb / float(nb))
        lo, hi = newcombe(ks, ns, kb, nb)
        rr = rfr(kb, nb, ks, ns)
        res.append((name, d, lo, hi, rr))
        print("  %-16s%12s%12s%+10.0f%s%20s%9s"
              % (name, "%d/%d" % (kb, nb), "%d/%d" % (ks, ns), d, "pp",
                 "[%+.0f, %+.0f]" % (lo, hi),
                 "-" if rr is None else "%.0f%%" % rr))

    if len(res) == 2:
        (_, d1, l1, u1, r1), (_, d2, l2, u2, r2) = res
        print("")
        overlap = not (u1 < l2 or u2 < l1)
        print("  absolute: %s"
              % ("intervals OVERLAP, so the two groups are not separated"
                 if overlap else "intervals are disjoint"))
        if r1 is not None and r2 is not None:
            direction = ("weaker" if r1 > r2 else "stronger")
            print("  RFR:      %.0f%% weaker vs %.0f%% stronger -- points to the %s half"
                  % (r1, r2, direction))
            if (d1 > d2) != (r1 > r2):
                print("")
                print("  !! THE TWO METRICS DISAGREE ON DIRECTION.")
                print("     Absolute favours one half, relative failure reduction the")
                print("     other. That is the ceiling effect made visible: do not")
                print("     claim skills help weak models most from this data.")


if __name__ == "__main__":
    main()
