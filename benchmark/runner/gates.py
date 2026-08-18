#!/usr/bin/env python
"""Which gate failed, and what the mask actually looked like.

    python gates.py <runs_dir> <task> [--arm ARM]

summarize.py reports pass/fail. A gate is pass/fail by construction, so a run
that produced an excellent mask and tripped one criterion is indistinguishable
there from a run that produced nothing.

That distinction turned out to be the whole story on 7t-nodura. Its rubric adds
a criterion the plain 7t rubric does not have -- the mask must exclude dura,
under 30 cm3 -- and a kimi run with Dice 0.94, core recall 0.9999 and every
subscore at or near 1.0 scored 0.0 on `no_dura_inclusion` alone, at 111 cm3.
Read as a pass rate that is a failure. Read as a mask it is a good skull-strip
that nobody asked to remove dura.

So this prints three things per task: the gate failures by arm, the continuous
Dice by arm, and both side by side per cell. If Dice rises with the skill while
the pass rate does not, the gate is hiding a real effect and the report should
say which gate.
"""
import argparse
import glob
import json
import os
import statistics
import sys
from collections import Counter, defaultdict


def med(xs):
    return statistics.median(xs) if xs else None


def fmt(x, nd=3):
    return "-" if x is None else ("%.*f" % (nd, x))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir")
    ap.add_argument("task")
    ap.add_argument("--arm", default=None)
    a = ap.parse_args()

    cells = defaultdict(lambda: {"n": 0, "pass": 0, "dice": [], "gates": Counter(),
                                 "extra": defaultdict(list)})
    by_arm = defaultdict(lambda: {"n": 0, "pass": 0, "dice": [], "gates": Counter()})
    seen = 0

    for d in sorted(glob.glob(os.path.join(a.runs_dir, a.task + "__*"))):
        ej = os.path.join(d, "envelope.json")
        if not os.path.isdir(d) or not os.path.exists(ej):
            continue
        try:
            with open(ej, encoding="utf-8") as fh:
                e = json.load(fh)
        except Exception:
            continue
        parts = os.path.basename(d).split("__")
        if len(parts) < 3:
            continue
        model, arm = parts[1].replace("neurodesk-", ""), parts[2]
        if a.arm and arm != a.arm:
            continue
        seen += 1

        passed = bool(e.get("valid")) and float(e.get("score") or 0) > 0
        metrics = ((e.get("detail") or {}).get("metrics") or {})
        dice = metrics.get("dice")
        gates = e.get("gate_failures") or []

        for tgt in (cells[(model, arm)], by_arm[arm]):
            tgt["n"] += 1
            tgt["pass"] += 1 if passed else 0
            if dice is not None:
                tgt["dice"].append(dice)
            tgt["gates"].update(gates or ["(none)"])
        for k, v in metrics.items():
            if k != "dice" and isinstance(v, (int, float)):
                cells[(model, arm)]["extra"][k].append(v)

    if not seen:
        sys.exit("no graded runs for %s in %s" % (a.task, a.runs_dir))

    arms = sorted(by_arm, key=lambda x: (x != "env-only", x))

    print("=== %s -- %d graded runs %s" % (a.task, seen, "=" * 12))

    print("\n--- gate failures by arm (a run can trip more than one) ---")
    allg = sorted({g for v in by_arm.values() for g in v["gates"]})
    print("  %-28s%s" % ("GATE", "".join("%20s" % x for x in arms)))
    for g in allg:
        print("  %-28s%s" % (g, "".join("%20s" % ("%d/%d" % (by_arm[x]["gates"][g],
                                                            by_arm[x]["n"]))
                                        for x in arms)))

    print("\n--- pass rate vs Dice, by arm ---")
    print("  %-20s %8s %12s %12s" % ("ARM", "N", "PASS", "MEDIAN DICE"))
    for x in arms:
        v = by_arm[x]
        print("  %-20s %8d %12s %12s"
              % (x, v["n"], "%d/%d" % (v["pass"], v["n"]), fmt(med(v["dice"]))))
    if len(arms) >= 2:
        base, other = by_arm[arms[0]], by_arm[arms[1]]
        db, do = med(base["dice"]), med(other["dice"])
        pb = base["pass"] / base["n"] if base["n"] else 0
        po = other["pass"] / other["n"] if other["n"] else 0
        if db is not None and do is not None and do > db and po <= pb:
            print("\n  !! Dice RISES with the skill (%s -> %s) while the pass rate does"
                  % (fmt(db), fmt(do)))
            print("     not (%d%% -> %d%%). A gate is hiding a real effect. Report which"
                  % (round(100 * pb), round(100 * po)))
            print("     gate, not just the null.")

    print("\n--- per cell ---")
    print("  %-16s %-20s %5s %8s %10s  %s"
          % ("MODEL", "ARM", "N", "PASS", "MED-DICE", "GATES TRIPPED"))
    for (model, arm) in sorted(cells):
        v = cells[(model, arm)]
        g = ", ".join("%s x%d" % (k, n) for k, n in v["gates"].most_common()
                      if k != "(none)") or "-"
        print("  %-16s %-20s %5d %8s %10s  %s"
              % (model[:16], arm[:20], v["n"], "%d/%d" % (v["pass"], v["n"]),
                 fmt(med(v["dice"])), g[:44]))


if __name__ == "__main__":
    main()
