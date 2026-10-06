#!/usr/bin/env python3
"""Check, before any tokens are spent, that a run belongs to the wave it is for.

    wave_check.py --sweep sweep.json --wave 1 [--label benchmark --task T --model M --arm A \\
        --skills-hash H] image_version=ci-env2-... tasks_sha=e98e3b6

Fails (exit 1) when:
  - the wave is not declared in sweep.json;
  - a pinned field (`waves.<n>.pins`) is not given, or differs from the value given;
  - a measured field (`waves.<n>.measured`: what the pod has, read from the kernel)
    is not given or does not match: equal, or at least the value for a key ending in
    `_min`, compared as numbers when both sides are numbers;
  - the label is `benchmark` and the task, model or arm is not declared for the wave,
    or the arm declares a skills_hash (`arms.<arm>`) and --skills-hash differs.
--measured-only checks only the measured fields (preflight, before any run).

--record RUN_JSON also checks the run's model_fingerprint (written by model_probe.py)
against `waves.<n>.model_fingerprints` for the model. It fails when the wave declares a
fingerprint for the model and the record's differs or is missing: the gateway serves a
different model or serving engine than the wave's. A model the wave declares no
fingerprint for is not checked. --fingerprint-only skips the other checks.

assemble_cell.py applies the same pins and declarations when a cell is published; this
stops a run that would be refused there from being paid for.
"""
import argparse
import json
import sys


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def measured_problems(declared, given):
    """Problems between a wave's `measured` declaration and values read on the pod.

    Keys ending in `_min` pass when the given value is at least the declared one;
    other keys must be equal. Numbers compare as numbers, anything else as the
    lower-cased string (so true/True/"true" agree). A key with no given value is a
    problem: an unmeasured environment is not a matching one.
    """
    out = []
    for k, want in sorted((declared or {}).items()):
        if k.startswith("_"):
            continue
        # `work_volume_gib_min` is checked against the measured `work_volume_gib`.
        field = k[:-4] if k.endswith("_min") else k
        if field not in given or given[field] in ("", None):
            out.append("the wave declares %s=%s and nothing was measured" % (k, want))
            continue
        got = given[field]
        gn, wn = _num(got), _num(want)
        if k.endswith("_min"):
            if gn is None or wn is None or gn < wn:
                out.append("%s is %s; wave declares at least %s" % (field, got, want))
        elif gn is not None and wn is not None:
            if gn != wn:
                out.append("%s is %s; wave declares %s" % (k, got, want))
        elif str(got).strip().lower() != str(want).strip().lower():
            out.append("%s is %s; wave declares %s" % (k, got, want))
    return out


def check(sweep, wave, given, label=None, task=None, model=None, arm=None,
          skills_hash=None):
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
    out += measured_problems(w.get("measured"), given)
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
        want = ((sweep.get("arms") or {}).get(arm) or {}).get("skills_hash")
        if want and skills_hash != want:
            out.append("arm %s is defined by skills_hash %s; the snapshot hashes to %s"
                       % (arm, want, skills_hash or "nothing"))
    return out


def declared_fingerprint(sweep, wave, model):
    """The fingerprint the wave declares for a model (provider prefix stripped), or None."""
    w = (sweep.get("waves") or {}).get(str(wave)) or {}
    fps = w.get("model_fingerprints") or {}
    return fps.get((model or "").split("/", 1)[-1])


def check_fingerprint(sweep, wave, model, record):
    """A list of problems with the record's model_fingerprint; empty when it matches or
    the wave declares none for this model."""
    want = declared_fingerprint(sweep, wave, model)
    if not want:
        return []
    got = (record or {}).get("model_fingerprint")
    if got is None:
        return ["wave %s declares fingerprint %s for %s and the probe recorded none"
                % (wave, want, model)]
    if got != want:
        return ["%s is served with fingerprint %s; wave %s declares %s (model or serving "
                "engine changed)" % (model, got, wave, want)]
    return []


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--wave", required=True)
    ap.add_argument("--label")
    ap.add_argument("--task")
    ap.add_argument("--model")
    ap.add_argument("--arm")
    ap.add_argument("--skills-hash", help="content hash of the arm's snapshot; empty for none")
    ap.add_argument("--record", help="run.json whose model_fingerprint is checked")
    ap.add_argument("--fingerprint-only", action="store_true",
                    help="check only the record's fingerprint")
    ap.add_argument("--measured-only", action="store_true",
                    help="check only the measured fields given as KEY=VALUE")
    ap.add_argument("fields", nargs="*", metavar="KEY=VALUE")
    a = ap.parse_args()
    given = {}
    for f in a.fields:
        k, sep, v = f.partition("=")
        if not sep:
            ap.error("expected KEY=VALUE, got %r" % f)
        given[k] = v
    sweep = json.load(open(a.sweep, encoding="utf-8"))
    w = (sweep.get("waves") or {}).get(str(a.wave))
    if a.fingerprint_only or a.measured_only:
        problems = [] if isinstance(w, dict) else [
            "wave %s is not declared in the sweep" % a.wave]
        if a.measured_only and isinstance(w, dict):
            problems += measured_problems(w.get("measured"), given)
    else:
        problems = check(sweep, a.wave, given, a.label, a.task, a.model, a.arm,
                         a.skills_hash)
    if a.record:
        try:
            rec = json.load(open(a.record, encoding="utf-8"))
        except (OSError, ValueError):
            rec = {}
        problems += check_fingerprint(sweep, a.wave, a.model, rec)
    for p in problems:
        print("FAIL: " + p)
    if problems:
        print("Stopping before any tokens are spent: assemble would refuse this run.")
        return 1
    print("ok    the run matches wave %s" % a.wave)
    return 0


if __name__ == "__main__":
    sys.exit(main())
