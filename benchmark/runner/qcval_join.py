#!/usr/bin/env python
"""Does the agent's own QC predict whether the mask is actually good?

    python qcval_join.py [qcval_out_dir] [runs_dir] [--task T] [--csv F]

Joins Michele's numeric QC battery (brain-extraction-qc/scripts/qc_metrics.py,
one JSON per run under qcval/out/) to the grader's envelope.json for the same
run, and asks the only question that matters for an unattended agent: when the
skill tells the agent "FAIL, try another tool", is it right?

WHY THIS EXISTS
---------------
On the 39 runs where the agent happened to write its own QC, the battery
returned PASS zero times: 20 BORDERLINE and 19 FAIL, and 17 of the FAILs were
masks the grader accepted. That sample had only 2 genuinely bad masks in it, so
it could show the false alarms and could not show the catches. Running the same
battery over every mask on disk fixes that -- roughly a third of them fail the
grader, which is enough to put a number on both sides.

READ THE VERDICT COLUMN, NOT THE VERDICT
----------------------------------------
`numeric_verdict` is a strict AND over the criteria, so a single over-firing
criterion sinks the whole battery and the verdict tells you almost nothing. The
useful output is per criterion and per sub-metric: how often each one fires on a
mask the grader accepted (false alarm) versus on one it rejected (a catch).
A criterion that fires on 20 of 37 good masks is not a quality signal, it is a
threshold in the wrong place.

THRESHOLDS ARE INFERRED, NOT READ
---------------------------------
Each sub-metric appears in the JSON twice: as a number in `metrics` and as a
PASS/FAIL rating in the criterion. Sort the runs by the number and the rating
flips at the threshold, so we recover it from the data rather than parsing her
source. That means this script keeps working when she retunes, and the recovered
bound can be compared against the one that would actually separate good from bad.

WHAT IS AND IS NOT A DISAGREEMENT
---------------------------------
Her battery was written against her notion of a good mask; the grader encodes
Moni's consensus reference. Where they disagree that is a real disagreement
between two defensible definitions, not automatically miscalibration. This
script measures the disagreement and does not adjudicate it.
"""
import argparse
import glob
import json
import os
import statistics
from collections import Counter, defaultdict

# Same rule summarize.py publishes, so these numbers reconcile with the report.
FAIL_VERDICTS = {"invalid", "unacceptable", "marginal", "fail", "failed", "error",
                 "no-output"}


def med(xs):
    return statistics.median(xs) if xs else None


def fmt(x, nd=3):
    return "-" if x is None else ("%.*f" % (nd, x))


def grader(run_dir):
    """(passed, dice, gate_failures) for a run, or None if it was never graded."""
    ej = os.path.join(run_dir, "envelope.json")
    if not os.path.exists(ej):
        return None
    try:
        with open(ej, encoding="utf-8") as fh:
            e = json.load(fh)
    except Exception:
        return None
    verdict = str(e.get("verdict") or "no-output").lower()
    passed = (bool(e.get("valid")) and float(e.get("score") or 0) > 0
              and verdict not in FAIL_VERDICTS)
    dice = ((e.get("detail") or {}).get("metrics") or {}).get("dice")
    return passed, dice, (e.get("gate_failures") or [])


def metric_for(sub, metrics):
    """Map a sub-metric rating name back to its numeric value.

    Names carry the direction of the bound as a suffix -- brain_to_head_ratio
    appears as both `_low` and `_high` because it is bounded on both sides -- so
    strip the suffix to find the number. Returns (key, value, direction).
    """
    for suffix, direction in (("_low", "low"), ("_high", "high")):
        if sub.endswith(suffix) and sub[:-len(suffix)] in metrics:
            return sub[:-len(suffix)], metrics[sub[:-len(suffix)]], direction
    if sub in metrics:
        return sub, metrics[sub], None
    return None, None, None


def infer_threshold(rows, direction):
    """Recover the bound from where the rating flips along the sorted values.

    rows: [(value, rating)]. Returns (lo, hi) bracketing the flip, so a gap in
    the sampled values is visible rather than hidden behind a made-up midpoint.
    """
    passes = [v for v, r in rows if r == "PASS"]
    fails = [v for v, r in rows if r == "FAIL"]
    if not passes or not fails:
        return None
    if direction == "low":          # fails when the value is too small
        return (max(fails), min(passes))
    return (max(passes), min(fails))  # fails when the value is too large


def separation(good, bad, higher_is_worse):
    """Best achievable split of good from bad on one metric.

    Returns (auc, threshold, tpr, fpr) at the cut maximising Youden's J. AUC is
    the rank statistic, so 0.5 means the metric carries no information about
    correctness at all and no threshold will rescue it -- which is the finding
    for several of these.
    """
    if len(good) < 3 or len(bad) < 3:
        return None
    wins = sum(1 for b in bad for g in good
               if (b > g) == higher_is_worse and b != g)
    ties = sum(1 for b in bad for g in good if b == g)
    auc = (wins + 0.5 * ties) / float(len(bad) * len(good))
    best = None
    for t in sorted(set(good + bad)):
        if higher_is_worse:
            tpr = sum(1 for b in bad if b >= t) / float(len(bad))
            fpr = sum(1 for g in good if g >= t) / float(len(good))
        else:
            tpr = sum(1 for b in bad if b <= t) / float(len(bad))
            fpr = sum(1 for g in good if g <= t) / float(len(good))
        j = tpr - fpr
        if best is None or j > best[0]:
            best = (j, t, tpr, fpr)
    return (auc, best[1], best[2], best[3])


def direction_from_ratings(rows, suffix_dir):
    """Which way the bound points, taken from the data where possible.

    The `_high` / `_low` suffix says it, but not every sub-metric carries one.
    Comparing the median FAIL value to the median PASS value recovers it for the
    rest, and disagreeing with the suffix would itself be worth seeing.
    """
    passes = [v for v, r in rows if r == "PASS"]
    fails = [v for v, r in rows if r == "FAIL"]
    if passes and fails:
        return med(fails) > med(passes)
    return suffix_dir != "low"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("qcval_dir", nargs="?",
                    default=os.path.expanduser("~/bench/qcval/out"))
    ap.add_argument("runs_dir", nargs="?",
                    default=os.path.expanduser("~/bench/runs"))
    ap.add_argument("--csv", default=None, help="write the joined per-run table here")
    a = ap.parse_args()

    runs = []
    ungraded = 0
    for f in sorted(glob.glob(os.path.join(a.qcval_dir, "*.json"))):
        name = os.path.basename(f)[:-5]
        if name.startswith("_"):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                q = json.load(fh)
        except Exception:
            continue
        g = grader(os.path.join(a.runs_dir, name))
        if g is None:
            ungraded += 1
            continue
        parts = name.split("__")
        runs.append({
            "run": name,
            "model": parts[1].replace("neurodesk-", "") if len(parts) > 1 else "?",
            "arm": parts[2] if len(parts) > 2 else "?",
            "qc": str(q.get("numeric_verdict") or "?").upper(),
            "criteria": q.get("criteria") or [],
            "metrics": q.get("metrics") or {},
            "passed": g[0], "dice": g[1], "gates": g[2],
        })

    if not runs:
        raise SystemExit("nothing joined -- check the two directory arguments")

    good = [r for r in runs if r["passed"]]
    bad = [r for r in runs if not r["passed"]]
    print("=== agent self-QC vs grader %s" % ("=" * 40))
    print("  %d masks scored by the battery and graded" % len(runs))
    print("  grader: %d pass / %d fail" % (len(good), len(bad)))
    if ungraded:
        print("  %d scored but not graded, excluded" % ungraded)

    # --- confusion -------------------------------------------------------
    print("\n--- battery verdict vs grader ---")
    print("  %-14s%12s%12s" % ("AGENT SAYS", "grader PASS", "grader FAIL"))
    order = ["PASS", "BORDERLINE", "FAIL"]
    seen_v = [v for v in order if any(r["qc"] == v for r in runs)]
    seen_v += sorted({r["qc"] for r in runs} - set(order))
    for v in seen_v:
        rs = [r for r in runs if r["qc"] == v]
        print("  %-14s%12d%12d" % (v, sum(1 for r in rs if r["passed"]),
                                   sum(1 for r in rs if not r["passed"])))

    # --- per criterion ---------------------------------------------------
    # The number to read is FALSE ALARM. A criterion firing on most of the good
    # masks cannot be acted on, because acting on it means discarding masks that
    # were fine -- and in an unattended agent, discarding means burning a retry.
    print("\n--- per criterion: how often it fires (FAIL) ---")
    print("  %-56s%14s%14s" % ("CRITERION", "on GOOD", "on BAD"))
    crit_names = []
    for r in runs:
        for c in r["criteria"]:
            if c.get("criterion") not in crit_names:
                crit_names.append(c.get("criterion"))
    for cn in crit_names:
        fg = sum(1 for r in good
                 if any(c.get("criterion") == cn and c.get("rating") == "FAIL"
                        for c in r["criteria"]))
        fb = sum(1 for r in bad
                 if any(c.get("criterion") == cn and c.get("rating") == "FAIL"
                        for c in r["criteria"]))
        print("  %-56s%14s%14s"
              % (str(cn)[:56], "%d/%d" % (fg, len(good)), "%d/%d" % (fb, len(bad))))

    # --- per sub-metric --------------------------------------------------
    print("\n--- per sub-metric: current bound, and the best one available ---")
    print("  %-30s%9s%9s%14s%7s%17s" % ("SUB-METRIC", "onGOOD", "onBAD",
                                        "bound now", "AUC", "best cut (tpr/fpr)"))
    subs = []
    for r in runs:
        for c in r["criteria"]:
            for s in (c.get("metrics") or {}):
                if s not in subs:
                    subs.append(s)
    rows_out = []
    for s in subs:
        rated = []
        vals_good, vals_bad = [], []
        fg = fb = 0
        sfx = None
        for r in runs:
            rating = None
            for c in r["criteria"]:
                if s in (c.get("metrics") or {}):
                    rating = c["metrics"][s]
            key, val, d = metric_for(s, r["metrics"])
            sfx = d if d is not None else sfx
            if rating is None or val is None:
                continue
            rated.append((val, rating))
            if rating == "FAIL":
                if r["passed"]:
                    fg += 1
                else:
                    fb += 1
            (vals_good if r["passed"] else vals_bad).append(val)
        if not rated:
            continue
        hiw = direction_from_ratings(rated, sfx)
        thr = infer_threshold(rated, "high" if hiw else "low")
        bound = "-" if thr is None else ("%.4g..%.4g" % thr)
        sep = separation(vals_good, vals_bad, hiw)
        cut = "-"
        auc = "-"
        if sep:
            auc = "%.2f" % sep[0]
            cut = "%.4g  %.0f%%/%.0f%%" % (sep[1], 100 * sep[2], 100 * sep[3])
        print("  %-30s%9s%9s%14s%7s%17s"
              % (s[:30], "%d/%d" % (fg, len(good)), "%d/%d" % (fb, len(bad)),
                 bound, auc, cut))
        rows_out.append(s)

    print("\n  onGOOD is the false alarm rate: masks the grader accepted that this")
    print("  sub-metric called FAIL. AUC 0.50 means the metric does not separate")
    print("  good from bad at any threshold. 'best cut' is the value maximising")
    print("  tpr minus fpr, with the rates it buys.")

    # --- does the verdict track mask quality at all? ---------------------
    print("\n--- grader Dice by battery verdict ---")
    for v in seen_v:
        ds = [r["dice"] for r in runs if r["qc"] == v and r["dice"] is not None]
        print("  %-14s n=%-4d median Dice %s" % (v, len(ds), fmt(med(ds))))

    if a.csv:
        # One row per run, with every sub-metric's numeric value, so the join can
        # be re-cut by model or arm without re-running the battery.
        import csv as _csv
        cols = ["run", "model", "arm", "qc_verdict", "grader_passed", "dice",
                "gate_failures"] + rows_out
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            w.writerow(cols)
            for r in runs:
                vals = []
                for s_ in rows_out:
                    _k, v, _d = metric_for(s_, r["metrics"])
                    vals.append("" if v is None else v)
                w.writerow([r["run"], r["model"], r["arm"], r["qc"],
                            int(r["passed"]), r["dice"] if r["dice"] is not None else "",
                            ";".join(r["gates"])] + vals)
        print(chr(10) + "  wrote %s (%d rows)" % (a.csv, len(runs)))

if __name__ == "__main__":
    main()
