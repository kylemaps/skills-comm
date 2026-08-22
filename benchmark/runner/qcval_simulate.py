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
The recomputation needs to know how sub-metric ratings combine. `verify()`
checks the assumed rule against every observed (sub-metric ratings -> criterion
rating) pair before anything is simulated, and stops if one mask disagrees: a
simulated operating point built on the wrong logic is worse than no operating
point, because it would look like a measurement.

That check earned its place immediately. The first model tried was a strict AND
over two values, and it was rejected on 84 of 640 criterion ratings -- her scale
is three-valued, and a criterion whose worst sub-metric is BORDERLINE is rated
BORDERLINE rather than FAIL. The rule that survives verification is
worst-rating-wins: PASS < BORDERLINE < FAIL, criterion = worst sub-metric,
verdict = worst criterion.

Overrides here are binary by construction, since we are proposing a hard bound
and have no way to recover the width of her borderline band. So a swept
sub-metric contributes only PASS or FAIL, while every other sub-metric keeps its
recorded three-valued rating. Outcomes are reported as PASS / BORDERLINE / FAIL
rather than collapsed, because whether BORDERLINE counts as acceptance is her
call and not ours.

WHAT THE SWEEP IS AND IS NOT
----------------------------
It reports, for each candidate bound, how the battery's verdict would line up
with the grader. That is an upper bound on achievable agreement, chosen with
full sight of the answers, over far fewer independent masks than the run count
suggests: md5 of the mask files puts the 81 accepted 7T runs at 13 distinct
masks, with one of them accounting for 51 runs. Deterministic tools on one input
converge, so runs replicate outputs even though each run is an independent
trajectory.

Treat the output as "here is where the bound would have to sit to be reachable
at all", not as a calibration. The honest use is to hand her the shape of the
curve and let her pick against her own definition of a good mask.

Count distinct masks by hashing the files, not by comparing printed metrics.
Rounded metric values collapse masks that genuinely differ, which understated
the count here as 6 before the hashes were checked.
"""
import argparse
import glob
import hashlib
import json
import os
from collections import defaultdict

FAIL_VERDICTS = {"invalid", "unacceptable", "marginal", "fail", "failed", "error",
                 "no-output"}


def grader_passed(run_dir):
    """(passed, gate_failures) or None if the run was never graded."""
    ej = os.path.join(run_dir, "envelope.json")
    if not os.path.exists(ej):
        return None
    try:
        with open(ej, encoding="utf-8") as fh:
            e = json.load(fh)
    except Exception:
        return None
    ok = (bool(e.get("valid")) and float(e.get("score") or 0) > 0
          and str(e.get("verdict") or "").lower() not in FAIL_VERDICTS)
    return ok, list(e.get("gate_failures") or [])


def mask_md5(run_dir):
    """Hash the mask so replicated outputs can be counted once.

    Deterministic tools on one input converge, so 81 accepted runs can be far
    fewer distinct masks -- 13 here. Counting distinct outputs off printed metric
    values instead of the files understates it, because rounding merges masks
    that differ.
    """
    m = glob.glob(os.path.join(run_dir, "submissions", "*", "output.nii.gz"))
    if not m:
        return None
    h = hashlib.md5()
    with open(m[0], "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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
        rd = os.path.join(runs_dir, name)
        g = grader_passed(rd)
        if g is None:
            continue
        out.append({"run": name, "q": q, "passed": g[0], "gates": g[1],
                    "md5": mask_md5(rd)})
    return out


# Her scale is ordered, not boolean. UNVERIFIED is not on it -- the dura
# criterion reports UNVERIFIED because the script genuinely cannot measure it,
# which is an absence of evidence and must not be folded in as a rating.
RANK = {"PASS": 0, "BORDERLINE": 1, "FAIL": 2}
UNRANK = {v: k for k, v in RANK.items()}


def worst(ratings):
    vals = [RANK[r] for r in ratings if r in RANK]
    return UNRANK[max(vals)] if vals else None


def verify(recs):
    """Does worst-rating-wins reproduce every criterion rating and every verdict?

    Returns (n_checked, [disagreements]). Anything non-empty invalidates the
    simulation below, so the caller must stop.
    """
    bad = []
    n = 0
    for r in recs:
        verdict_parts = []
        for c in r["q"].get("criteria") or []:
            subs = (c.get("metrics") or {})
            if not subs:
                continue
            n += 1
            expect, got = worst(subs.values()), c.get("rating")
            if got != expect:
                bad.append((r["run"], c.get("criterion"), got, expect, dict(subs)))
            verdict_parts.append(got)
        if verdict_parts:
            n += 1
            expect = worst(verdict_parts)
            got = str(r["q"].get("numeric_verdict") or "").upper()
            if got != expect:
                bad.append((r["run"], "(verdict)", got, expect, {}))
    return n, bad


def sub_value(sub, metrics):
    for sfx in ("_low", "_high"):
        if sub.endswith(sfx) and sub[:-len(sfx)] in metrics:
            return metrics[sub[:-len(sfx)]]
    return metrics.get(sub)


def simulate(recs, overrides):
    """Recompute each verdict with {sub_metric: (direction, bound)} applied.

    direction 'high' means FAIL when value > bound; 'low' means FAIL when
    value < bound. Sub-metrics not overridden keep their recorded rating,
    BORDERLINE included, so the only thing the sweep changes is the bound.
    """
    res = []
    for r in recs:
        crit_ratings = []
        for c in r["q"].get("criteria") or []:
            subs = (c.get("metrics") or {})
            if not subs:
                continue
            ratings = []
            for sub, rating in subs.items():
                if sub in overrides:
                    d, bound = overrides[sub]
                    if d == "pass":
                        rating = "PASS"
                    else:
                        v = sub_value(sub, r["q"].get("metrics") or {})
                        if v is not None:
                            rating = "FAIL" if ((v > bound) if d == "high"
                                                else (v < bound)) else "PASS"
                ratings.append(rating)
            crit_ratings.append(worst(ratings))
        res.append((r["run"], worst(crit_ratings), r["passed"]))
    return res


def confusion(res):
    t = defaultdict(int)
    for _run, verdict, g_ok in res:
        t[(verdict, g_ok)] += 1
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("qcval_dir")
    ap.add_argument("runs_dir", nargs="?",
                    default=os.path.expanduser("~/bench/runs"))
    ap.add_argument("--sweep", action="append", default=[],
                    metavar="SUB:DIR", help="sub-metric and direction to sweep, "
                                            "e.g. brain_to_head_ratio_low:low")
    ap.add_argument("--relax", action="append", default=[], metavar="SUB",
                    help="force this sub-metric to PASS; repeatable. Use to ask "
                         "what the battery would say if a bound were fixed, "
                         "without asserting where the new bound goes")
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

    # --- which sub-metrics reject the masks the grader accepted? ---------
    # Sweeping one bound while another still rejects everything produces a table
    # of zeroes that looks like "no bound helps", when it means "you moved the
    # wrong one". Find the blockers from the data instead of by eye: the first
    # attempt at this swept two bounds and missed a third that also fired on
    # every accepted mask.
    good = [r for r in recs if r["passed"]]
    blockers = defaultdict(int)
    for r in good:
        for c in r["q"].get("criteria") or []:
            for sub, rating in (c.get("metrics") or {}).items():
                if rating != "PASS":
                    blockers[sub] += 1
    print("")
    print("--- sub-metrics that reject masks the grader ACCEPTED (n=%d) ---"
          % len(good))
    if not blockers:
        print("  none")
    for sub in sorted(blockers, key=lambda s: -blockers[s]):
        flag = "  <-- rejects every one" if blockers[sub] == len(good) else ""
        print("  %-30s %3d/%d%s" % (sub, blockers[sub], len(good), flag))
    print("  Any sub-metric rejecting all of them is a hard blocker: while it")
    print("  stands, no change to any other bound can make a mask pass.")

    relax = {s: ("pass", None) for s in a.relax}
    if relax:
        res = simulate(recs, relax)
        t = confusion(res)
        print("")
        print("--- with %s forced to PASS ---" % ", ".join(a.relax))
        print("  %-12s%14s%14s" % ("verdict", "grader PASS", "grader FAIL"))
        for v in ("PASS", "BORDERLINE", "FAIL"):
            print("  %-12s%14d%14d" % (v, t[(v, True)], t[(v, False)]))

        # The same table over DISTINCT masks. Runs replicate outputs, so a
        # run-level count reports 51 independent successes where there is one
        # mask seen 51 times. Both are true and only one of them is a sample
        # size.
        by = {r["run"]: r for r in recs}
        seen = {}
        for run, verdict, g_ok in res:
            h = by[run]["md5"] or run
            seen[h] = (verdict, g_ok)
        dt = defaultdict(int)
        for v, g in seen.values():
            dt[(v, g)] += 1
        print("")
        print("  over DISTINCT masks (%d of %d runs):" % (len(seen), len(res)))
        print("  %-12s%14s%14s" % ("verdict", "grader PASS", "grader FAIL"))
        for v in ("PASS", "BORDERLINE", "FAIL"):
            print("  %-12s%14d%14d" % (v, dt[(v, True)], dt[(v, False)]))

        # A mask the battery would accept and the grader rejected is the only
        # error that matters for an unattended agent -- it stops there. Which
        # gate caught it says whether the battery is missing something it
        # measures or something it openly cannot.
        fa = [by[run] for run, verdict, g_ok in res
              if verdict == "PASS" and not g_ok]
        if fa:
            gc = defaultdict(int)
            for r in fa:
                for g in (r["gates"] or ["(none recorded)"]):
                    gc[g] += 1
            print("")
            print("  %d run(s) the battery would ACCEPT but the grader rejected;"
                  % len(fa))
            print("  %d distinct mask(s). Gates they tripped:"
                  % len({r["md5"] or r["run"] for r in fa}))
            for g in sorted(gc, key=lambda k: -gc[k]):
                print("    %-34s %d" % (g, gc[g]))

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
        print("  %12s%22s%22s%14s" % ("bound", "verdict on GOOD masks",
                                      "verdict on BAD masks", "would accept"))
        print("  %12s%22s%22s%14s" % ("", "P / B / F", "P / B / F", "P+B of good"))
        for b in cands:
            ov = dict(relax)
            ov[sub] = (d, b)
            t = confusion(simulate(recs, ov))
            g = [t[(v, True)] for v in ("PASS", "BORDERLINE", "FAIL")]
            bd = [t[(v, False)] for v in ("PASS", "BORDERLINE", "FAIL")]
            ng = sum(g) or 1
            print("  %12.4g%22s%22s%13.0f%%"
                  % (b, "%d / %d / %d" % tuple(g), "%d / %d / %d" % tuple(bd),
                     100.0 * (g[0] + g[1]) / ng))
        print("  Columns are the battery's verdict, split by what the grader said.")
        print("  A bound is useful when the GOOD column moves toward P while the")
        print("  BAD column stays at F. Whether BORDERLINE counts as acceptance is")
        print("  hers to decide, so it is reported and never folded in.")


if __name__ == "__main__":
    main()
