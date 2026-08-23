#!/usr/bin/env python
"""Does the skill improve the work, or just pick the tool?

    python mechanism.py runs_<task>.csv [--robust synthstrip,hd-bet] [--by-model]

Pass rate says a skill helped. It does not say how, and the two candidate
explanations call for different write-ups:

  selection  the agent chooses a tool that works on this data
  execution  the agent drives whatever tool it chose more carefully

Splitting them needs no new runs. summarize.py already records which tools each
transcript invoked, so the runs can be cut by tool class and the pass rate read
down the rows (does the tool decide the outcome?) and across the columns (does
the skill change which tool is reached?).

On structural-brain-extraction-7t the answer was unambiguous: runs that reached
SynthStrip or HD-BET passed 81 of 107, runs that used only FSL BET passed 0 of
20, and the skill lifted robust-tool use from 67% to 94% (p=0.002) while moving
the pass rate GIVEN a robust tool not at all (75% to 80%, p=0.77). The measured
effect of the skill is tool selection.

WHY --robust IS NOT FITTED FROM THE DATA
----------------------------------------
Picking the "robust" set by which tools happen to pass would make the mediation
circular. For 7T the split is external and predates our runs: the grader pack's
own PROVENANCE.md documents FSL BET as a catastrophic outlier at 7T (546 cm3,
about half a brain) that is kept as a scored candidate specifically to validate
the grader. So --robust encodes the task's published design, not our results.
With no --robust the script only reports per-tool pass rates and draws no
mediation at all.

WHAT THIS IS NOT
----------------
Per-protocol, not intent-to-treat. Only runs that produced a mask have a tool to
classify, and the skill arms produce masks more often, so the denominators differ
by arm for a real reason. This explains the headline effect; it does not replace
it. Quote the ITT pass rate as the result and this as the mechanism.
"""
import argparse
import csv
from collections import defaultdict
from math import comb


def fisher(a, b, c, d):
    """Two-sided Fisher exact on [[a,b],[c,d]]. Exact, so it is honest at n=3."""
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
    """Wilson interval. Sane at k=0, unlike the normal approximation."""
    if not n:
        return (0.0, 0.0)
    ph = k / float(n)
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * ((ph * (1 - ph) / n + z * z / (4 * n * n)) ** 0.5) / d
    return (100 * max(0.0, c - h), 100 * min(1.0, c + h))


def rate(k, n):
    if not n:
        return "-"
    lo, hi = wilson(k, n)
    return "%d/%d %.0f%% [%.0f,%.0f]" % (k, n, 100.0 * k / n, lo, hi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", help="runs_<task>.csv written by summarize.py")
    ap.add_argument("--robust", default=None,
                    help="comma-separated tools the grader pack designates as "
                         "appropriate for this task; never inferred from results")
    ap.add_argument("--baseline", default="env-only")
    ap.add_argument("--by-model", action="store_true")
    a = ap.parse_args()

    with open(a.csv_path, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if str(r.get("valid", "1")).lower() not in ("0", "false", "")]
    if not rows:
        raise SystemExit("no valid runs in %s" % a.csv_path)

    def passed(r):
        return str(r.get("passed", "")).lower() in ("1", "true", "yes")

    def tools(r):
        return [t for t in (r.get("methods") or "").replace(",", ";").split(";") if t]

    arms = sorted({r["arm"] for r in rows}, key=lambda x: (x != a.baseline, x))

    # --- per tool, before any mechanism is asserted ----------------------
    # A tool appearing in a transcript is not proof it produced the output, so
    # read this as association. It is here to show whether a tool split exists
    # at all before --robust names one.
    print("=== %s: %d valid runs %s" % (a.csv_path, len(rows), "=" * 20))
    print("")
    print("--- pass rate by tool present in the transcript ---")
    per_tool = defaultdict(lambda: [0, 0])
    for r in rows:
        for t in set(tools(r)):
            per_tool[t][0] += 1
            per_tool[t][1] += 1 if passed(r) else 0
    # Tie-break on the name. per_tool is populated by iterating a set of tool
    # names, and Python randomises string hashing per process, so a count-only
    # key put two 6-run tools in a different order on consecutive runs of
    # identical data -- which is indistinguishable from a real change in a diff.
    for t in sorted(per_tool, key=lambda t: (-per_tool[t][0], t)):
        n, k = per_tool[t]
        print("  %-14s %s" % (t, rate(k, n)))
    notool = [r for r in rows if not tools(r)]
    if notool:
        print("  %-14s %s" % ("(none seen)",
                              rate(sum(1 for r in notool if passed(r)), len(notool))))

    if not a.robust:
        print("")
        print("  No --robust given, so no mechanism is claimed. Pass the tools the")
        print("  grader pack designates as appropriate to get the split.")
        return

    robust = [t.strip() for t in a.robust.split(",") if t.strip()]

    def cls(r):
        ts = tools(r)
        if any(t in robust for t in ts):
            return "robust tool used"
        return "other tool only" if ts else "no tool detected"

    CLASSES = ("robust tool used", "other tool only", "no tool detected")
    tab = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in rows:
        cell = tab[cls(r)][r["arm"]]
        cell[0] += 1
        cell[1] += 1 if passed(r) else 0

    print("")
    print("--- pass rate by tool class and arm (robust = %s) ---" % ",".join(robust))
    print("  %-18s%s%22s" % ("", "".join("%22s" % x for x in arms), "overall"))
    for c in CLASSES:
        n = sum(tab[c][x][0] for x in arms)
        if not n:
            continue
        cells = "".join("%22s" % ("%d/%d" % (tab[c][x][1], tab[c][x][0])) for x in arms)
        print("  %-18s%s%22s" % (c, cells, rate(sum(tab[c][x][1] for x in arms), n)))

    # --- selection: does the skill change which tool is reached? ---------
    print("")
    print("--- SELECTION: reached a robust tool ---")
    reach = {}
    for x in arms:
        reach[x] = (tab["robust tool used"][x][0],
                    sum(tab[c][x][0] for c in CLASSES))
    for x in arms:
        print("  %-18s %s" % (x, rate(*reach[x])))
    if a.baseline in reach:
        bk, bn = reach[a.baseline]
        for x in arms:
            if x == a.baseline:
                continue
            k, n = reach[x]
            print("  %-18s vs %-10s p = %.4f"
                  % (x, a.baseline, fisher(k, n - k, bk, bn - bk)))

    # --- execution: given the right tool, is the work any better? --------
    # If selection carries the effect and this does not move, the finding is that
    # the skill routes the agent rather than teaching it to drive the tool. That
    # is a sharper claim than "the skill improves masks", and a checkable one.
    print("")
    print("--- EXECUTION: passed, given a robust tool was used ---")
    ex = {x: (tab["robust tool used"][x][1], tab["robust tool used"][x][0])
          for x in arms}
    for x in arms:
        print("  %-18s %s" % (x, rate(*ex[x])))
    if a.baseline in ex:
        bk, bn = ex[a.baseline]
        for x in arms:
            if x == a.baseline:
                continue
            k, n = ex[x]
            print("  %-18s vs %-10s p = %.4f"
                  % (x, a.baseline, fisher(k, n - k, bk, bn - bk)))

    if a.by_model:
        # Uptake is not uniform across the panel -- one model passed 7T at 10/10
        # with 3/10 uptake -- so a mechanism claim that holds only in aggregate
        # should be visible as such.
        print("")
        print("--- reached a robust tool, by model ---")
        bym = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for r in rows:
            cell = bym[r["model"]][r["arm"]]
            cell[0] += 1
            cell[1] += 1 if cls(r) == "robust tool used" else 0
        print("  %-16s%s" % ("MODEL", "".join("%22s" % x for x in arms)))
        for m in sorted(bym):
            print("  %-16s%s" % (m, "".join(
                "%22s" % ("%d/%d" % (bym[m][x][1], bym[m][x][0])) for x in arms)))


if __name__ == "__main__":
    main()
