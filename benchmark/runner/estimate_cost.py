#!/usr/bin/env python
"""Price a sweep before running it, using what past runs actually cost.

    python estimate_cost.py <runs_dir> --task T --models M [M...] \
        --repeats N [--arms env-only env+skill] [--reference-task T2]

Prints median in+out tokens per cell and a projected total. Exits 1 if any cell
has no historical data to project from, so an unbudgeted sweep fails loudly
rather than silently guessing.

Why bother
----------
Cost per run varies by more than an order of magnitude and not in the direction
you would guess. Measured on this benchmark: one model's median run is ~54k
in+out tokens, another's is ~1.09M on the same task -- 20x. Adding a skill moved
one model 1.04x and another 3.47x. Estimating from run count alone is therefore
useless; a "small" 40-run sweep can cost more than a "large" 100-run one.

Caveats this cannot fix
-----------------------
- Absolute token counts are NOT comparable across models. Some report reasoning
  and cache tokens, others report zero for both. Within-model ratios are the
  trustworthy quantity, and that is all this projects.
- A new skill can change cost substantially, which is exactly why it is worth
  piloting a couple of runs and re-estimating rather than trusting a projection
  built from a different skill.
"""
import argparse
import glob
import json
import os
import statistics
import sys


def load(runs_dir, task):
    """Median in+out tokens per (model, arm) for one task's historical runs."""
    per_cell = {}
    for p in sorted(glob.glob(os.path.join(runs_dir, task + "__*", "run.json"))):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        tin, tout = d.get("tokens_input"), d.get("tokens_output")
        if not tin and not tout:
            continue
        model = (d.get("model") or "").replace("neurodesk/", "")
        arm = d.get("condition", "?")
        per_cell.setdefault((model, arm), []).append((tin or 0) + (tout or 0))
    return {k: statistics.median(v) for k, v in per_cell.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir")
    ap.add_argument("--task", required=True)
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--repeats", type=int, required=True)
    ap.add_argument("--arms", nargs="+", default=["env-only", "env+skill"])
    ap.add_argument("--reference-task", default=None,
                    help="draw historical costs from a different task")
    a = ap.parse_args()

    ref_task = a.reference_task or a.task
    hist = load(a.runs_dir, ref_task)
    if not hist:
        sys.exit("no historical token data for %s in %s" % (ref_task, a.runs_dir))

    print("=== PROJECTED COST: %s x %d repeats ===" % (a.task, a.repeats))
    if a.reference_task:
        print("    costs projected from %s\n" % a.reference_task)
    print("  %-16s %-10s %14s %16s" % ("MODEL", "ARM", "MED/RUN", "CELL TOTAL"))

    total, missing = 0, []
    for m in [x.replace("neurodesk/", "") for x in a.models]:
        for arm in a.arms:
            med = hist.get((m, arm))
            if med is None:
                # Fall back to the model's other arm; better a flagged estimate
                # than none, but say so.
                other = [v for (mm, _aa), v in hist.items() if mm == m]
                if other:
                    med = statistics.median(other)
                    note = "  (from other arm)"
                else:
                    missing.append("%s/%s" % (m, arm))
                    print("  %-16s %-10s %14s %16s" % (m, arm, "NO DATA", "-"))
                    continue
            else:
                note = ""
            cell = med * a.repeats
            total += cell
            print("  %-16s %-10s %14s %16s%s" % (
                m, arm, "{:,}".format(int(med)), "{:,}".format(int(cell)), note))

    print("\n  RUNS   %d" % (len(a.models) * len(a.arms) * a.repeats))
    print("  TOKENS %s  (in+out, %.1fM)" % ("{:,}".format(int(total)), total / 1e6))
    if missing:
        print("\n  !! no historical data for: %s" % ", ".join(missing))
        print("     Pilot those cells before committing to the full sweep.")
        sys.exit(1)


if __name__ == "__main__":
    main()
