#!/usr/bin/env python
"""What would the QC battery say if the two unreachable bounds were moved?

    python qcval_simulate.py <qcval_out_dir> <runs_dir> [--sweep SUB-METRIC]

The battery rated FAIL on 210 of 210 masks because two sub-metric bounds sit
outside the whole observed range. The obvious next question is what it would say
with those bounds somewhere reachable, and the obvious way to answer it is to
edit her thresholds and re-run 210 masks.

That is not necessary. Moving one bound changes exactly one sub-metric's rating;
every other rating in the JSON is unaffected. So the outcome is computable from
the JSONs already on disk, exactly, with no re-run and no re-derivation.

IT DEPENDS ON A MODEL OF HER LOGIC, SO THE MODEL IS TESTED FIRST
---------------------------------------------------------------
The recomputation assumes each criterion is a strict AND over its sub-metrics
and the verdict a strict AND over criteria. That is inferred from reading her
output, not from her source, so `verify()` checks it against all 210 observed
(sub-metric ratings -> criterion rating) pairs before anything is simulated. If
a single mask disagrees the script says so and stops: a simulated operating
point built on the wrong logic is worse than no operating point, because it
would look like a measurement.

WHAT THE SWEEP IS AND IS NOT
----------------------------
It reports, for each candidate bound, how the battery's verdict would line up
with the grader. That is an upper bound on achievable agreement, chosen with
full sight of the answers, on masks that are far fewer than the run count
suggests -- the 81 good 7T masks are 6 distinct masks. Treat the output as
"here is where the bound would have to sit to be reachable at all", not as a
calibration. The honest use is to hand her the shape of the curve and let her
pick, against her own definition of a good mask.
"""
import argparse
import glob
import json
import os
from collections import defaultdict

FAIL_VERDICTS = {"invalid", "unacceptable", "marginal", "fail", "failed", "error",
                 "no-output"}


def grader_passed(run_dir):
    ej = os.path.join(run_dir, "envelope.json")
    if not os.path.exists(ej):
        return None
    try:
        with open(ej, encoding="utf-8") as fh:
            e = json.load(fh)
    except Exception:
        return None
    return (bool(e.get("valid")) and float(e.get("score") or 0) > 0
            and str(e.get("verdict") or "").lower() not in FAIL_VERDICTS)


def load(qcval_dir, runs_dir):
    out = []
    for f in sorted(glob.glob(os.path.join(qcval_dir, "*.json"))):
        name = os.path.basename(f)[:-5]
        if name.startswith("_"):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                q = json.load(fh)
        except Exception:
            continue
        ok = grader_passed(os.path.join(runs_dir, name))
        if ok is None:
            continue
        out.append({"run": name, "q": q, "passed": ok})
    return out


def verify(recs):
    """Is a criterion the AND of its sub-metrics, and the verdict the AND of those?

    Returns (n_checked, [disagreements]). Anything non-empty invalidates the
    simulation below, so the caller must stop.
    """
    bad = []
    n = 0
    for r in recs:
        crits = r["q"].get("criteria") or []
        verdict_parts = []
        for c in crits:
            subs = (c.get("metrics") or {})
            if not subs:
                continue
            n += 1
            expect = "PASS" if all(v == "PASS" for v in subs.values()) else "FAIL"
            got = c.get("rating")
            if got != expect:
                bad.append((r["run"], c.get("criterion"), got, expect, dict(subs)))
            verdict_parts.append(got)
        # UNVERIFIED criteria carry no sub-metrics and are excluded above; the
        # verdict is checked only against the rated ones.
        if verdict_parts:
            ev = "PASS" if all(v == "PASS" for v in verdict_parts) else "FAIL"
            gv = str(r["q"].get("numeric_verdict") or "").upper()
            if gv not in ("PASS", "FAIL") and ev == "FAIL":
                pass          # BORDERLINE is a softening of FAIL, not a conflict
            elif gv != ev:
                bad.append((r["run"], "(verdict)", gv, ev, {}))
    return n, bad


def sub_value(sub, metrics):
    for sfx in ("_low", "_high"):
        if sub.endswith(sfx) and sub[:-len(sfx)] in metrics:
            return metrics[sub[:-len(sfx)]]
    return metrics.get(sub)


def simulate(recs, overrides):
    """Recompute each verdict with {sub_metric: (direction, bound)} applied.

    direction 'high' means FAIL when value > bound; 'low' means FAIL when
    value < bound. Sub-metrics not overridden keep their recorded rating.
    """
    res = []
    for r in recs:
        ok = True
        for c in r["q"].get("criteria") or []:
            for sub, rating in (c.get("metrics") or {}).items():
                if sub in overrides:
                    d, bound = overrides[sub]
                    v = sub_value(sub, r["q"].get("metrics") or {})
                    if v is None:
                        continue
                    rating = "FAIL" if ((v > bound) if d == "high"
                                        else (v < bound)) else "PASS"
                if rating != "PASS":
                    ok = False
        res.append((r["run"], ok, r["passed"]))
    return res


def confusion(res):
    t = defaultdict(int)
    for _run, qc_ok, g_ok in res:
        t[(qc_ok, g_ok)] += 1
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("qcval_dir")
    ap.add_argument("runs_dir", nargs="?",
                    default=os.path.expanduser("~/bench/runs"))
    ap.add_argument("--sweep", action="append", default=[],
                    metavar="SUB:DIR", help="sub-metric and direction to sweep, "
                                            "e.g. brain_to_head_ratio_low:low")
    a = ap.parse_args()

    recs = load(a.qcval_dir, a.runs_dir)
    if not recs:
        raise SystemExit("nothing loaded")
    print("=== %d masks with both a battery result and a grade" % len(recs))

    n, bad = verify(recs)
    print("--- logic model: criterion = AND(sub-metrics), verdict = AND(criteria)")
    print("    checked %d criterion ratings, %d disagreements" % (n, len(bad)))
    if bad:
        for row in bad[:5]:
            print("    MISMATCH %s / %s: recorded %s, model says %s %s" % row)
        raise SystemExit("model rejected -- simulation would be fiction, stopping")
    print("    model holds, simulation below is exact")

    for spec in a.sweep:
        sub, _, d = spec.partition(":")
        d = d or "high"
        vals = sorted({sub_value(sub, r["q"].get("metrics") or {}) for r in recs}
                      - {None})
        if not vals:
            print("\n!! no values for %s" % sub)
            continue
        # Candidate bounds sit between observed values, so a bound is never
        # placed exactly on a data point where the comparison is ambiguous.
        cands = [vals[0] - abs(vals[0] or 1) * 0.1]
        cands += [(vals[i] + vals[i + 1]) / 2.0 for i in range(len(vals) - 1)]
        cands += [vals[-1] + abs(vals[-1] or 1) * 0.1]
        print("\n--- sweeping %s (%s), %d distinct values ---" % (sub, d, len(vals)))
        print("  %12s%10s%10s%10s%10s%12s"
              % ("bound", "qcPASS", "agree", "false+", "false-", "accuracy"))
        for b in cands:
            t = confusion(simulate(recs, {sub: (d, b)}))
            tp, tn = t[(True, True)], t[(False, False)]
            fp, fn = t[(True, False)], t[(False, True)]
            tot = tp + tn + fp + fn
            print("  %12.4g%10d%10d%10d%10d%11.0f%%"
                  % (b, tp + fp, tp + tn, fp, fn, 100.0 * (tp + tn) / tot))
        print("  qcPASS = masks the battery would accept. false+ = accepted but the")
        print("  grader rejected. false- = rejected but the grader accepted.")


if __name__ == "__main__":
    main()
