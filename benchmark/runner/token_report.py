#!/usr/bin/env python
"""What has this benchmark actually cost, in tokens.

    python token_report.py [runs_dir] [--task T] [--model M] [--since YYYY-MM-DD]
                           [--cells]

Reads the `tokens_*` fields finalize_run.py recovers from opencode's SQLite
database, so it works on every run already on disk -- nothing is re-run to
produce this.

Why tokens and not dollars: `cost` is 0.0 on the Neurodesk gateway (self-hosted,
no pricing configured), so the token count IS the budget. And per-run cost varies
roughly 20x across models on the same task, which makes run count a useless proxy
for spend -- the per-model table below is the one to read before launching a sweep.

Runs with no token record are counted separately and NOT silently dropped: a
total that quietly excludes them reads as complete when it is a floor.
"""
import argparse
import glob
import json
import os
from collections import defaultdict

TOK_IN, TOK_OUT = "tokens_input", "tokens_output"


def _fmt(n):
    return "{:,}".format(int(n))


def load(runs_dir, task=None, model=None, since=None):
    """Yield (record, tokens_in, tokens_out) for matching runs."""
    pattern = os.path.join(runs_dir, (task or "*") + "__*", "run.json")
    for f in sorted(glob.glob(pattern)):
        try:
            with open(f, encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        if model and rec.get("model", "").replace("neurodesk/", "") != \
                model.replace("neurodesk/", ""):
            continue
        # `start` is ISO-8601 UTC, so a lexicographic prefix compare is a date compare.
        if since and (rec.get("start") or "") < since:
            continue
        yield rec, rec.get(TOK_IN) or 0, rec.get(TOK_OUT) or 0


def table(title, rows, width=42):
    """rows: {key: [n_runs, tokens_in, tokens_out, n_missing]}"""
    if not rows:
        return
    print("\n=== %s %s" % (title, "=" * max(0, 60 - len(title))))
    for k in sorted(rows, key=lambda k: -(rows[k][1] + rows[k][2])):
        n, ti, to, miss = rows[k]
        tot = ti + to
        scored = n - miss
        per = "  ~%s/run" % _fmt(tot // scored) if scored else ""
        flag = "  (%d no data)" % miss if miss else ""
        print("  %-*s %4d run%s %15s%s%s"
              % (width, k[:width], n, " " if n == 1 else "s", _fmt(tot), per, flag))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir", nargs="?",
                    default=os.path.expanduser("~/bench/runs"))
    ap.add_argument("--task", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="only runs started on or after this date")
    ap.add_argument("--cells", action="store_true",
                    help="also break down by task/model/arm cell")
    a = ap.parse_args()

    by_task = defaultdict(lambda: [0, 0, 0, 0])
    by_model = defaultdict(lambda: [0, 0, 0, 0])
    by_arm = defaultdict(lambda: [0, 0, 0, 0])
    by_cell = defaultdict(lambda: [0, 0, 0, 0])
    n = ti_all = to_all = missing = 0

    for rec, ti, to in load(a.runs_dir, a.task, a.model, a.since):
        n += 1
        ti_all += ti
        to_all += to
        gap = 1 if (ti + to) == 0 else 0
        missing += gap
        task = rec.get("task_id", "?")
        mdl = rec.get("model", "?").replace("neurodesk/", "")
        arm = rec.get("condition", "?")
        for d, k in ((by_task, task), (by_model, mdl), (by_arm, arm),
                     (by_cell, "%s / %s / %s" % (task, mdl, arm))):
            row = d[k]
            row[0] += 1
            row[1] += ti
            row[2] += to
            row[3] += gap

    if not n:
        print("no runs matched")
        return

    scope = a.task or "all tasks"
    if a.since:
        scope += " since %s" % a.since
    print("=== TOTAL (%s) %s" % (scope, "=" * max(0, 48 - len(scope))))
    print("  %d runs   %s tokens   (in %s / out %s)"
          % (n, _fmt(ti_all + to_all), _fmt(ti_all), _fmt(to_all)))
    if missing:
        print("  %d run(s) have no token record -- this total is a FLOOR." % missing)

    table("by task", by_task)
    table("by model  (spend per run varies ~20x -- read this before a sweep)",
          by_model, width=20)
    table("by arm", by_arm, width=20)
    if a.cells:
        table("by cell", by_cell, width=58)


if __name__ == "__main__":
    main()
