#!/usr/bin/env python
"""Every task x model x arm in one grid.

    python matrix.py [runs_dir] [--metric pass|minutes|tokens] [--csv]

Reads every summary_<task>.json that collect_results.sh has written and lays the
whole benchmark out as one table, so a result can be checked by eye instead of
by scrolling four separate reports.

Why a separate view
-------------------
summarize.py answers "what happened in this task". Nobody was answering "what
happened across all of them", and that is where the actual finding lives: the
skill helps on 7t and motion and does nothing on nodura, which only means
something when the three sit side by side.

An uneven denominator is marked with `!`. That is not cosmetic. Unequal n
between arms was the first thing a collaborator challenged about these results,
and a grid that quietly prints 87% for 41/47 next to 60% for 30/50 hides exactly
what she was asking to see.

--csv emits the same grid as long-form rows for a report builder to consume.
"""
import argparse
import csv
import glob
import json
import os
import sys

ARM_ORDER = {"env-only": 0, "env+skill": 1}


def arm_key(a):
    return (ARM_ORDER.get(a, 2), a)


def load_summaries(runs_dir):
    out = {}
    for p in sorted(glob.glob(os.path.join(runs_dir, "summary_*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        if d.get("cells"):
            out[d.get("task") or os.path.basename(p)] = d
    return out


def cell_value(c, metric):
    if c is None:
        return "-"
    if metric == "minutes":
        return "%.1f" % c.get("median_minutes", 0)
    if metric == "tokens":
        t = c.get("median_tokens_total") or 0
        return "%.0fk" % (t / 1000.0) if t else "-"
    n, p = c.get("n", 0), c.get("passes", 0)
    return "%d/%d" % (p, n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir", nargs="?",
                    default=os.path.expanduser("~/bench/runs"))
    ap.add_argument("--metric", default="pass",
                    choices=["pass", "minutes", "tokens"])
    ap.add_argument("--csv", action="store_true",
                    help="long-form rows on stdout instead of a grid")
    a = ap.parse_args()

    summaries = load_summaries(a.runs_dir)
    if not summaries:
        sys.exit("no summary_*.json in %s -- run collect_results.sh first" % a.runs_dir)

    if a.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["task", "model", "arm", "n", "passes", "pass_rate",
                    "median_minutes", "median_tokens"])
        for task, d in summaries.items():
            for key, c in sorted(d["cells"].items()):
                model, arm = key.split("|", 1)
                n, p = c.get("n", 0), c.get("passes", 0)
                w.writerow([task, model, arm, n, p,
                            round(p / n, 4) if n else "",
                            c.get("median_minutes", ""),
                            c.get("median_tokens_total", "")])
        return

    arms, models = [], []
    for d in summaries.values():
        for key in d["cells"]:
            m, arm = key.split("|", 1)
            if arm not in arms:
                arms.append(arm)
            if m not in models:
                models.append(m)
    arms.sort(key=arm_key)
    models.sort()

    label = {"pass": "PASSES / RUNS", "minutes": "MEDIAN MINUTES",
             "tokens": "MEDIAN TOKENS (in+out)"}[a.metric]
    print("=== %s %s" % (label, "=" * max(0, 58 - len(label))))
    hdr = "  %-16s" % "" + "".join("%18s" % x for x in arms)
    uneven = False

    for task in sorted(summaries):
        d = summaries[task]
        print("\n%s" % task)
        print(hdr)
        tot = {arm: [0, 0] for arm in arms}
        for m in models:
            cells = {arm: d["cells"].get("%s|%s" % (m, arm)) for arm in arms}
            if not any(cells.values()):
                continue
            row = "  %-16s" % m[:16]
            for arm in arms:
                c = cells[arm]
                v = cell_value(c, a.metric)
                if c:
                    tot[arm][0] += c.get("passes", 0)
                    tot[arm][1] += c.get("n", 0)
                    if c.get("n", 0) not in (0, 10):
                        v += "!"
                        uneven = True
                row += "%18s" % v
            print(row)
        if a.metric == "pass":
            row = "  %-16s" % "TOTAL"
            for arm in arms:
                p, n = tot[arm]
                row += "%18s" % ("%d/%d = %d%%" % (p, n, round(100.0 * p / n))
                                 if n else "-")
            print(row)

    if uneven and a.metric == "pass":
        print("\n  ! = cell is not n=10. Unequal denominators between arms were the")
        print("      first thing challenged about these results; do not report a")
        print("      pooled rate across a flagged row without saying which cell is short.")


if __name__ == "__main__":
    main()
