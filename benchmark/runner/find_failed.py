#!/usr/bin/env python
"""List runs that failed for OUR reasons and should be re-run.

    python find_failed.py <runs_dir> <task> [--arm ARM] [--model M]

Prints one run directory per line. Empty output means nothing needs retrying.

Why this exists
---------------
Classifying an infrastructure failure separately from a model failure keeps a
gateway outage from being scored as incompetence. But classification is a
diagnosis, not a fix -- it leaves the cell short of runs, and unequal
denominators are not something to caption around, they are a signal that the
obvious thing has not been done. If a run failed because our harness broke,
re-run it.

Reuses summarize.py's classifier so "what counts as our fault" is defined in
exactly one place. A run qualifies when summarize excluded it for a reason we caused: a harness
failure, our timeout killing it, or the wrong skill being in place.
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from summarize import is_retryable, load_run  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir")
    ap.add_argument("task")
    ap.add_argument("--arm", default=None)
    ap.add_argument("--model", default=None)
    a = ap.parse_args()

    dirs = sorted(d for d in glob.glob(os.path.join(a.runs_dir, a.task + "__*"))
                  if os.path.isdir(d))
    if not dirs:
        return

    # Same two-pass shape as summarize: only apply the zero-token test once we
    # know token extraction works here, or an unreadable database would mark
    # every failed run as retryable and loop forever.
    probe = [load_run(d, a.task) for d in dirs]
    tokens_available = any(r.get("tokens_total") for r in probe)
    runs = [load_run(d, a.task, tokens_available) for d in dirs] if tokens_available else probe

    for r in runs:
        # Every exclusion we caused, not just the ones spelled "harness failure".
        # A contaminated or misassigned run is our bug too, and selecting on that
        # one prefix left those excluded and then never re-run, which is the worst
        # of both: the cell loses the run and never gets it back.
        if not is_retryable(r["exclude_reason"]):
            continue
        if a.arm and r["arm"] != a.arm:
            continue
        if a.model and r["model"] != a.model.replace("neurodesk/", ""):
            continue
        print(r["dir"])


if __name__ == "__main__":
    main()
