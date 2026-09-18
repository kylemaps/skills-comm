#!/usr/bin/env python3
"""Can these tasks be pooled into one statement? summarize.py cannot answer this.

    python3 pool_check.py report/summary_*.json

WHY A SEPARATE CHECK
`summarize.py` sets `poolable` per task: were the runs in THIS task made under the
same conditions. Moni asked a different question -- can we make a pooled statement
across tasks -- and the per-task flag cannot answer it, because it is computed inside
one task and never sees the others.

The two questions also need different rules, which is the real reason this is not a
flag on the existing one:

  image_version, opencode_version, tasks_sha
      The environment. Must be identical everywhere. A difference here means the
      tasks were measured on different machines-in-effect and pooling them averages
      over that difference without saying so.

  skills_hash
      Must be constant WITHIN AN ARM across tasks, and is expected to differ BETWEEN
      arms -- that is what an arm IS. Requiring it to be globally constant would
      declare every multi-arm comparison unpoolable, which is the opposite of useful.

  prompt_hash
      Expected to DIFFER between tasks. Each task has its own prompt; that is what
      makes it a different task. So it is checked inverted: constant within a task,
      and two tasks sharing one is a finding, not a pass.

WHAT A FAILURE MEANS
Not "these numbers are wrong". It means a pooled number would silently average over a
condition that changed, and the honest move is to report per task or to re-run under
one pinned environment. Our own history: across 431 runs there were three
image/opencode/grader combinations and nobody chose any of them -- an image upgrade
landed mid-sweep.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Same key list and same "not recorded is not a difference" rule as the per-task
# check. Two copies would drift, and the drift would show up as a disagreement
# between two files that are supposed to answer the same family of question.
from summarize import PROVENANCE_KEYS, UNRECORDED  # noqa: E402

ENVIRONMENT = ["image_version", "opencode_version", "tasks_sha"]
PER_ARM = ["skills_hash"]
PER_TASK = ["prompt_hash"]


def load(paths):
    """[(task, model, arm, {key: Counter})] -- one row per cell."""
    rows = []
    for p in paths:
        s = json.load(open(p, encoding="utf-8"))
        task = s.get("task") or os.path.basename(p)
        for cell_key, cell in (s.get("cells") or {}).items():
            model, _, arm = cell_key.partition("|")
            rows.append((task, model, arm, cell.get("provenance") or {}, cell.get("n", 0)))
    return rows


def spread(rows, key, pick=lambda r: True):
    """{value: total runs} over the cells matching `pick`, ignoring unrecorded."""
    c = Counter()
    for r in rows:
        if not pick(r):
            continue
        for val, n in (r[3].get(key) or {}).items():
            if val not in UNRECORDED:
                c[val] += n
    return c


def where(rows, key, val):
    return sorted({"%s/%s/%s" % (r[0], r[1], r[2])
                   for r in rows if val in (r[3].get(key) or {})})


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("summaries", nargs="+")
    ap.add_argument("--quiet", action="store_true", help="verdict only")
    a = ap.parse_args()

    rows = load(a.summaries)
    tasks = sorted({r[0] for r in rows})
    arms = sorted({r[2] for r in rows})
    if len(tasks) < 2:
        raise SystemExit("Nothing to check: pooling is a question about two or more "
                         "tasks and %d was given." % len(tasks))

    print("%d tasks, %d cells, %d runs"
          % (len(tasks), len(rows), sum(r[4] for r in rows)))
    print("  tasks: %s" % ", ".join(tasks))
    print("  arms : %s\n" % ", ".join(arms))

    blocking = []

    print("--- the environment: must be identical everywhere")
    for k in ENVIRONMENT:
        c = spread(rows, k)
        if len(c) <= 1:
            print("  ok       %-18s %s" % (k, next(iter(c), "(nothing recorded)")))
        else:
            blocking.append(k)
            print("  BLOCKING %-18s %d values" % (k, len(c)))
            for v, n in c.most_common():
                print("             %-16s %4d runs   %s"
                      % (v, n, ", ".join(where(rows, k, v)[:3])
                         + (" ..." if len(where(rows, k, v)) > 3 else "")))

    print("\n--- the skill: constant within an arm, expected to differ between arms")
    for k in PER_ARM:
        for arm in arms:
            c = spread(rows, k, lambda r, _a=arm: r[2] == _a)
            if len(c) <= 1:
                print("  ok       %-18s %-18s %s"
                      % (k, arm, next(iter(c), "(nothing recorded)")))
            else:
                blocking.append("%s/%s" % (k, arm))
                print("  BLOCKING %-18s %-18s %d values within one arm: %s"
                      % (k, arm, len(c), ", ".join(c)))

    print("\n--- the prompt: expected to DIFFER per task, checked inverted")
    for k in PER_TASK:
        for t in tasks:
            c = spread(rows, k, lambda r, _t=t: r[0] == _t)
            if len(c) > 1:
                blocking.append("%s/%s" % (k, t))
                print("  BLOCKING %-18s %-34s %d values inside one task: %s"
                      % (k, t, len(c), ", ".join(c)))
            else:
                print("  ok       %-18s %-34s %s"
                      % (k, t, next(iter(c), "(nothing recorded)")))
        # Two tasks with the same prompt are not two tasks. Worth saying out loud
        # rather than passing silently, because it would mean a task id is wrong
        # somewhere -- and a near-miss task id has already produced fourteen graded
        # outputs belonging to nothing.
        seen = defaultdict(list)
        for t in tasks:
            for v in spread(rows, k, lambda r, _t=t: r[0] == _t):
                seen[v].append(t)
        for v, ts in seen.items():
            if len(ts) > 1:
                blocking.append("%s shared by %s" % (k, "+".join(ts)))
                print("  BLOCKING %-18s %s share prompt %s -- they are not two tasks"
                      % (k, " and ".join(ts), v))

    unrecorded = [k for k in PROVENANCE_KEYS if not spread(rows, k)]
    if unrecorded:
        print("\n  not recorded anywhere, so untestable: %s" % ", ".join(unrecorded))
        print("  Absence is not agreement. These runs predate the field.")

    print()
    if blocking:
        print("NOT POOLABLE ACROSS TASKS: %s" % ", ".join(blocking))
        print("A pooled number here would average over a condition that changed")
        print("without saying so. Report per task, or re-run under one pinned")
        print("environment. Note we never CHOSE the variation below -- an image")
        print("upgrade landed mid-sweep and nobody picked it.")
        return 1
    print("POOLABLE ACROSS TASKS: every environment key is constant, each arm has one")
    print("skill, and each task has its own prompt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
