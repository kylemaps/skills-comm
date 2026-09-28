#!/usr/bin/env python3
"""Check, before any tokens are spent, that a run belongs to the wave it is for.

    wave_check.py --sweep sweep.json --wave 1 [--label benchmark --task T --model M --arm A] \\
        image_version=ci-env2-... tasks_sha=e98e3b6

Fails (exit 1) when:
  - the wave is not declared in sweep.json;
  - a pinned field (`waves.<n>.pins`) is not given, or differs from the value given;
  - the label is `benchmark` and the task, model or arm is not declared for the wave.

assemble_cell.py applies the same pins and declarations when a cell is published; this
stops a run that would be refused there from being paid for.
"""
import argparse
import json
import sys


def check(sweep, wave, given, label=None, task=None, model=None, arm=None):
    """A list of problems; empty when the run belongs to the wave."""
    w = (sweep.get("waves") or {}).get(str(wave))
    if not isinstance(w, dict):
        return ["wave %s is not declared in the sweep" % wave]
    out = []
    for k, want in sorted((w.get("pins") or {}).items()):
        if k not in given:
            out.append("the wave pins %s=%s and no value was given to check" % (k, want))
        elif given[k] != want:
            out.append("%s is %s; wave %s pins %s" % (k, given[k], wave, want))
    if label == "benchmark":
        t = (w.get("tasks") or {}).get(task)
        if t is None:
            out.append("task %s is not declared in wave %s" % (task, wave))
        else:
            bare = (model or "").split("/", 1)[-1]
            if bare not in t.get("models", []):
                out.append("model %s is not declared for %s in wave %s" % (bare, task, wave))
            if arm not in t.get("arms", []):
                out.append("arm %s is not declared for %s in wave %s" % (arm, task, wave))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--wave", required=True)
    ap.add_argument("--label")
    ap.add_argument("--task")
    ap.add_argument("--model")
    ap.add_argument("--arm")
    ap.add_argument("fields", nargs="*", metavar="KEY=VALUE")
    a = ap.parse_args()
    given = {}
    for f in a.fields:
        k, sep, v = f.partition("=")
        if not sep:
            ap.error("expected KEY=VALUE, got %r" % f)
        given[k] = v
    problems = check(json.load(open(a.sweep, encoding="utf-8")), a.wave, given,
                     a.label, a.task, a.model, a.arm)
    for p in problems:
        print("FAIL: " + p)
    if problems:
        print("Stopping before any tokens are spent: assemble would refuse this run.")
        return 1
    print("ok    the run matches wave %s" % a.wave)
    return 0


if __name__ == "__main__":
    sys.exit(main())
