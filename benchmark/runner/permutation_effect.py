#!/usr/bin/env python3
"""The tested secondary outcomes a wave declares for two arms: minutes to a passing
output and tokens per run, each compared B against A by a permutation test within
strata of model x subject.

    python3 permutation_effect.py --results results/wave8 --sweep ../ci/sweep.json --wave 8
        [--arm env+skill-michele] [--control env+skill] [--permutations 10000]
        [--seed 20261103] [--timeout-min 90] [--placements FILE] [--json OUT]

The declaration (results/wave<N>/README.md) fixes what is computed; this is that text as
code, so the numbers in the write-up come from one place that was written before the data.

  minutes to a passing output   per run, seconds_to_output / 60 (run.json: the last write
                                of the output file, from the run's start on the pod's
                                clock) for a run whose verdict passed; every other run
                                counts the wall (--timeout-min), lost runs included. A run
                                that passed without a recorded time counts the wall too,
                                and is counted (passed_without_time). An excluded run
                                (not valid, not lost) is left out.
                                With --placements (output_time.py's JSON), a passing run
                                counts the later of that time and the last placement of
                                its output that keeps an older mtime (placed_later); a
                                passing run the file does not show as checked counts its
                                recorded time and is counted (placement_unchecked).
                                Statistic per stratum: median(B) - median(A).
  tokens per run                ln(tokens_total) for a valid run with a token record
                                (tokens_total above 0); a run without one is left out and
                                counted per arm (no_tokens). Statistic per stratum:
                                mean(B) - mean(A).

A wave that declares waves.<n>.primary_family also gets Holm-adjusted p values across
families (p_holm): minutes over every family but the primary one, whose single test is not
adjusted, and tokens over every family.

The effect is the mean of the per-stratum statistics weighted by the stratum's run count.
A stratum with fewer than two runs in either arm contributes nothing and is listed. The p
value is two-sided: the arm labels are permuted within each stratum (--permutations times,
seeded), and p = (1 + the number of permutations whose effect is at least as far from zero
as the observed one) / (permutations + 1).

Pure Python, as stratified_effect.py: it runs anywhere the results do.
"""
import argparse
import json
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wave_report import load, wave_config  # noqa: E402

MIN_EACH = 2


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def run_key(row):
    """One job of a wave: '<CI run id>|<model>|<arm>|<rep>' (output_time.py's keys)."""
    ci = row.get("_ci_run") or str((row.get("_run") or {}).get("ci_run", "")).split("/")[0]
    return "%s|%s|%s|%s" % (ci, row.get("model"), row.get("arm"), row.get("rep"))


def time_value(row, timeout_min, placements=None):
    """Minutes to a passing output for one runs-CSV row (with its run.json as _run), or
    None when the row is left out. Returns (minutes, note) where note is
    "passed_without_time" when a passing run had no seconds_to_output, and with
    placements (output_time.py's record per run_key) "placed_later" when the output was
    put in place after its recorded time, "placement_unchecked" when the run was not
    checked."""
    lost = bool(row.get("lost"))
    if row.get("valid") != "True" and not lost:
        return None, None
    if row.get("passed") == "True" and not lost:
        s = _num((row.get("_run") or {}).get("seconds_to_output"))
        if s is None:
            return float(timeout_min), "passed_without_time"
        note = None
        if placements is not None:
            rec = placements.get(run_key(row)) or {}
            p = _num(rec.get("placed_s")) if rec.get("checked") else None
            if not rec.get("checked"):
                note = "placement_unchecked"
            elif p is not None and p > s:
                s, note = p, "placed_later"
        return min(s / 60.0, float(timeout_min)), note
    return float(timeout_min), None


def token_value(row):
    """ln(tokens_total) for one row, or None when the run has no token record."""
    if row.get("valid") != "True":
        return None
    t = _num(row.get("tokens_total"))
    if t is None or t <= 0:
        return None
    return math.log(t)


def build_strata(rows, arm, control, value, min_each=MIN_EACH):
    """Per (model, subject): {arm: [values], control: [values]}. Returns (used, skipped)
    where each is a sorted list of (stratum, {arm: values, control: values}); a stratum
    with fewer than min_each values in either arm is skipped."""
    cells = {}
    for r in rows:
        if r["arm"] not in (arm, control):
            continue
        v = value(r)
        if v is None:
            continue
        c = cells.setdefault((r["model"], r["_subject"]), {arm: [], control: []})
        c[r["arm"]].append(v)
    used, skipped = [], []
    for k in sorted(cells):
        c = cells[k]
        (used if min(len(c[arm]), len(c[control])) >= min_each else skipped).append((k, c))
    return used, skipped


def effect(strata, arm, control, stat):
    """Weighted mean over strata of stat(arm values) - stat(control values), weights =
    the stratum's run count. nan without strata."""
    num = den = 0.0
    for _, c in strata:
        n = len(c[arm]) + len(c[control])
        num += n * (stat(c[arm]) - stat(c[control]))
        den += n
    return num / den if den else float("nan")


def permutation_test(strata, arm, control, stat, permutations, seed):
    """(observed effect, two-sided p, permutations) with the arm labels permuted within
    each stratum."""
    obs = effect(strata, arm, control, stat)
    if obs != obs or not permutations:
        return obs, float("nan"), 0
    rng = random.Random(seed)
    pools = [(c[arm] + c[control], len(c[arm])) for _, c in strata]
    weights = [len(p) for p, _ in pools]
    total = float(sum(weights))
    extreme = 0
    for _ in range(permutations):
        acc = 0.0
        for (pool, nb), w in zip(pools, weights):
            rng.shuffle(pool)
            acc += w * (stat(pool[:nb]) - stat(pool[nb:]))
        if abs(acc / total) >= abs(obs) - 1e-12:
            extreme += 1
    return obs, (1 + extreme) / (permutations + 1), permutations


def analyse(rows, arm, control, permutations, seed, timeout_min, placements=None):
    """Both outcomes for one family's rows (each row carries _subject and _run)."""
    out = {"arm": arm, "control": control, "permutations": permutations, "seed": seed}
    kinds = ("passed_without_time",) + (("placed_later", "placement_unchecked") if placements is not None else ())
    notes = {k: {arm: 0, control: 0} for k in kinds}
    counting = [True]

    def tv(r):
        v, note = time_value(r, timeout_min, placements)
        if counting[0] and note and r["arm"] in notes[note]:
            notes[note][r["arm"]] += 1
        return v

    used, skipped = build_strata(rows, arm, control, tv)
    counting[0] = False
    obs, p, n = permutation_test(used, arm, control, statistics.median, permutations, seed)
    out["minutes_to_pass"] = {
        "effect_minutes": obs, "p": p, "strata": len(used),
        "strata_skipped": ["%s x %s" % k for k, _ in skipped],
        "n": {a: sum(len(c[a]) for _, c in used) for a in (arm, control)}}
    out["minutes_to_pass"].update(notes)

    no_tok = {arm: 0, control: 0}
    for r in rows:
        if r["arm"] in no_tok and r.get("valid") == "True" and token_value(r) is None:
            no_tok[r["arm"]] += 1
    # The one sensitivity row of the time test: runs whose transcript or scripts named
    # the blinded subject (subject_named, blinded waves only) left out.
    kept = [r for r in rows if str((r.get("_run") or {}).get("subject_named", r.get("subject_named"))) != "True"]
    used_s, _ = build_strata(kept, arm, control, tv)
    obs_s, p_s, _ = permutation_test(used_s, arm, control, statistics.median, permutations, seed)
    out["minutes_to_pass_subject_named_removed"] = {
        "effect_minutes": obs_s, "p": p_s, "strata": len(used_s),
        "removed": len(rows) - len(kept)}

    used, skipped = build_strata(rows, arm, control, token_value)
    obs, p, n = permutation_test(used, arm, control, statistics.fmean, permutations, seed)
    out["log_tokens"] = {
        "effect_log": obs, "ratio": math.exp(obs) if obs == obs else float("nan"),
        "p": p, "strata": len(used),
        "strata_skipped": ["%s x %s" % k for k, _ in skipped],
        "n": {a: sum(len(c[a]) for _, c in used) for a in (arm, control)},
        "no_tokens": no_tok}
    return out


def adjust_families(report, primary):
    """Holm across the families of each outcome, for a wave that declares a primary family:
    minutes to a passing output over every family but the primary (its one test is not
    adjusted), log tokens over every family. Adds p_holm beside each p; a family without a p
    (no strata) is left out of its adjustment."""
    import stratified_effect as se
    for key, fams in (("minutes_to_pass", [f for f in sorted(report) if f != primary]),
                      ("log_tokens", sorted(report))):
        ps = [(f, report[f][key]["p"]) for f in fams if report[f][key]["p"] == report[f][key]["p"]]
        for (f, _), h in zip(ps, se.holm([p for _, p in ps])):
            report[f][key]["p_holm"] = h
    if primary in report:
        report[primary]["minutes_to_pass"]["primary"] = True


def families(tasks):
    """{family: [task ids]} from wave_config's tasks."""
    fam = {}
    for t, td in tasks.items():
        fam.setdefault(td.get("family") or t, []).append(t)
    return fam


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="results/wave<N> directory")
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--wave", required=True)
    ap.add_argument("--arm", default="env+skill-michele")
    ap.add_argument("--control", default="env+skill")
    ap.add_argument("--permutations", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=20261103)
    ap.add_argument("--timeout-min", type=float, default=90.0)
    ap.add_argument("--placements", help="output_time.py's JSON: count the later of the two times")
    ap.add_argument("--json")
    a = ap.parse_args()

    sweep = json.load(open(a.sweep, encoding="utf-8"))
    tasks, _reps, _pilot, _speed = wave_config(sweep, a.wave)
    rows = load(a.results, tasks)
    placements = None
    if a.placements:
        with open(a.placements, encoding="utf-8") as fh:
            placements = json.load(fh)
    report = {}
    for fam, ts in sorted(families(tasks).items()):
        frows = []
        for t in ts:
            for r in rows.get(t, []):
                r["_subject"] = tasks[t]["subject"]
                frows.append(r)
        report[fam] = analyse(frows, a.arm, a.control, a.permutations, a.seed, a.timeout_min,
                              placements)
        m, k = report[fam]["minutes_to_pass"], report[fam]["log_tokens"]
        print("%s: %s vs %s" % (fam, a.arm, a.control))
        print("  minutes to a passing output: %+.1f min (p %.4f; %d strata, n %s)"
              % (m["effect_minutes"], m["p"], m["strata"], m["n"]))
        if placements is not None:
            print("  output placed later than recorded %s; placement not checked %s"
                  % (m["placed_later"], m["placement_unchecked"]))
        s = report[fam]["minutes_to_pass_subject_named_removed"]
        if s["removed"]:
            print("  minutes, subject-named runs removed (%d): %+.1f min (p %.4f; %d strata)"
                  % (s["removed"], s["effect_minutes"], s["p"], s["strata"]))
        print("  tokens per run: x%.2f (p %.4f; %d strata, n %s; without tokens %s)"
              % (k["ratio"], k["p"], k["strata"], k["n"], k["no_tokens"]))
        for key in ("minutes_to_pass", "log_tokens"):
            if report[fam][key]["strata_skipped"]:
                print("  %s: strata skipped: %s" % (key, ", ".join(report[fam][key]["strata_skipped"])))
    primary = (sweep.get("waves", {}).get(str(a.wave)) or {}).get("primary_family")
    if primary:
        adjust_families(report, primary)
        print("Holm across families (the primary family's minutes test is not adjusted):")
        for fam in sorted(report):
            m, k = report[fam]["minutes_to_pass"], report[fam]["log_tokens"]
            print("  %s: minutes p %.4f %s; tokens p %.4f, Holm %s" % (
                fam, m["p"], "(primary)" if m.get("primary") else "Holm %s" % (
                    "%.4f" % m["p_holm"] if "p_holm" in m else "-"),
                k["p"], "%.4f" % k["p_holm"] if "p_holm" in k else "-"))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
