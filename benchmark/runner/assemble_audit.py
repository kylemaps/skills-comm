#!/usr/bin/env python3
"""Run the assemble gates over every declared cell, and compare to what we published.

    python3 assemble_audit.py --runs ~/bench/runs --results ~/bench/report
    python3 assemble_audit.py --runs ~/bench/runs --results ~/bench/report --why

THE TEST THIS IS
`assemble_cell.py` is meant to replace a human's judgement about whether a cell may
be published. It has never disagreed with that human, because it has never been run
against anything a human already judged. A gate in that state is untested, and
shipping it straight into the write path makes its first real decision also its
first unreviewed one.

So: run it over the cells that ARE published and see where it disagrees.

HOW TO READ THE RESULT
Neither column is the truth.

  gate PASS,  published    agreement. The common case, and the least informative.
  gate FAIL,  published    the interesting one. Either the gate is too strict, or
                           we published something we should not have. Both have
                           happened -- 7t is live and un-poolable, deliberately,
                           with a badge saying so.
  gate PASS,  not published a cell nobody has got round to, or one the sweep
                           declares and nobody ran.
  gate FAIL,  not published agreement, the other way.

A disagreement is a question, not a verdict. The point of running this BEFORE the
gate can commit is that every disagreement gets read by a person once, and either
the gate changes or the published result does.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def published_cells(results_dir):
    """{(task, model, arm)} that currently appear in a published summary."""
    out = set()
    if not os.path.isdir(results_dir):
        return out
    for f in sorted(os.listdir(results_dir)):
        if not (f.startswith("summary_") and f.endswith(".json")):
            continue
        try:
            s = json.load(open(os.path.join(results_dir, f), encoding="utf-8"))
        except Exception:
            continue
        task = s.get("task") or f[len("summary_"):-len(".json")]
        for key in (s.get("cells") or {}):
            model, _, arm = key.partition("|")
            out.add((task, model, arm))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", required=True)
    ap.add_argument("--results", required=True,
                    help="directory holding summary_<task>.json as published")
    ap.add_argument("--sweep", default=os.path.join(HERE, "..", "ci", "sweep.json"))
    ap.add_argument("--why", action="store_true",
                    help="print the gate output for every disagreement")
    a = ap.parse_args()

    spec = json.load(open(a.sweep, encoding="utf-8"))
    live = published_cells(a.results)

    rows = []
    for task, t in sorted(spec["tasks"].items()):
        for model in t["models"]:
            for arm in t["arms"]:
                p = subprocess.run(
                    [sys.executable, os.path.join(HERE, "assemble_cell.py"),
                     "--runs", a.runs, "--task", task, "--model", model,
                     "--arm", arm, "--sweep", a.sweep],
                    capture_output=True, text=True)
                rows.append({
                    "task": task, "model": model, "arm": arm,
                    "gate": "PASS" if p.returncode == 0 else "FAIL",
                    "published": (task, model, arm) in live,
                    "output": p.stdout + p.stderr,
                })

    agree = [r for r in rows if (r["gate"] == "PASS") == r["published"]]
    gate_stricter = [r for r in rows if r["gate"] == "FAIL" and r["published"]]
    gate_looser = [r for r in rows if r["gate"] == "PASS" and not r["published"]]

    print("%d declared cells: %d agree, %d the gate would have BLOCKED, "
          "%d the gate would allow that are not published\n"
          % (len(rows), len(agree), len(gate_stricter), len(gate_looser)))

    print("  %-38s %-18s %-16s %-5s %s"
          % ("task", "model", "arm", "gate", "published"))
    for r in rows:
        mark = " " if (r["gate"] == "PASS") == r["published"] else "*"
        print("%s %-38s %-18s %-16s %-5s %s"
              % (mark, r["task"][:38], r["model"][:18], r["arm"][:16],
                 r["gate"], "yes" if r["published"] else "no"))

    if gate_stricter:
        print("\n%d PUBLISHED cells the gate would have blocked." % len(gate_stricter))
        print("Read every one. Either the gate is wrong, or the result is -- and we")
        print("already know 7t is live and un-poolable on purpose, with a badge, so")
        print("'the gate is stricter than we were' is not automatically a bug.")
        for r in gate_stricter:
            reason = [l.strip() for l in r["output"].splitlines()
                      if l.strip().startswith("REFUSED")]
            print("  %s / %s / %s -- %s"
                  % (r["task"], r["model"], r["arm"],
                     reason[0] if reason else "(no REFUSED line; read --why)"))

    if a.why:
        for r in rows:
            if (r["gate"] == "PASS") == r["published"]:
                continue
            print("\n" + "=" * 76)
            print("%s / %s / %s   gate=%s published=%s"
                  % (r["task"], r["model"], r["arm"], r["gate"],
                     "yes" if r["published"] else "no"))
            print("=" * 76)
            print(r["output"].rstrip())

    # Deliberately exits 0 whatever it finds. This is a comparison between two
    # fallible judgements, not a test with a correct answer, and making it fail the
    # build would turn "the gate disagrees with a human" into something to silence.
    return 0


if __name__ == "__main__":
    sys.exit(main())
