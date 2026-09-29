#!/usr/bin/env python3
"""Skill effect per task, stratified by model.

    stratified_effect.py results/wave0/summary_*.json [--arm env+skill] [--json out.json]
    stratified_effect.py --runs results/wave1/runs_<task>.csv [--arm env+skill] [--json out.json]
    stratified_effect.py --power

Reads the `skill_effect` block of each task summary: per model, passes and runs in
env-only and in the chosen skill arm. Each model is a stratum; runs are never pooled
across models.

Per task:
  per model    risk difference (skill minus control) with a Newcombe 95% CI
  MH RD        Mantel-Haenszel risk difference across models, weights n1*n0/(n1+n0),
               95% CI from the Sato variance estimator
  exact p      two-sided exact conditional test of no association in every stratum:
               the number of skill-arm passes, summed over models, against its
               distribution given each model's margins (a product of
               hypergeometrics). With one model it equals Fisher's exact test.
  direction    models with a positive, zero and negative difference
Across tasks: Holm and Benjamini-Hochberg adjusted p-values.

--runs reads one task's runs CSV (summarize.py) and computes the same statistics for
the primary analysis and each sensitivity analysis:
  primary              valid runs
  exclusions as fail   excluded runs added, each counted as a fail
  exclusions as pass   excluded runs added, each counted as a pass
  flagged removed      valid runs without any flag (answer-key, arm-seen,
                       control-read-skill)
  on-spec only         valid runs whose off_spec is "none" or "unchecked"
A model with fewer than 5 runs in either arm is left out (MIN_N_FOR_STATS).

--power prints the probability that a single 10-vs-10 cell reaches p < 0.05 with
Fisher's exact test, for a range of true pass rates.
"""
import argparse
import csv
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from summarize import MIN_N_FOR_STATS, fisher_exact, newcombe  # noqa: E402


def strata_of(summary, arm):
    """(model, control passes, control n, skill passes, skill n) per model."""
    out = []
    for key, e in sorted((summary.get("skill_effect") or {}).items()):
        model, _, a = key.partition("|")
        if a != arm:
            continue
        s = (model, e["env_only_pass"], e["env_only_n"], e["env_skill_pass"], e["env_skill_n"])
        if s[2] and s[4]:
            out.append(s)
    return out


SCENARIOS = ["primary", "exclusions as fail", "exclusions as pass", "flagged removed",
             "on-spec only"]


def strata_from_runs(rows, arm, scenario, min_n=MIN_N_FOR_STATS):
    """(model, control passes, control n, skill passes, skill n) per model, from runs
    CSV rows, for one of SCENARIOS. A model with fewer than min_n runs in either arm
    is left out, as summarize.py leaves it out of skill_effect."""
    cells = {}
    for r in rows:
        if r["arm"] not in ("env-only", arm):
            continue
        valid = r["valid"] == "True"
        passed = r["passed"] == "True"
        if not valid:
            if scenario == "exclusions as fail":
                passed = False
            elif scenario == "exclusions as pass":
                passed = True
            else:
                continue
        elif scenario == "flagged removed" and r.get("flags"):
            continue
        elif (scenario == "on-spec only"
              and (r.get("off_spec") or "unchecked") not in ("none", "unchecked")):
            continue
        c = cells.setdefault(r["model"], {"env-only": [0, 0], arm: [0, 0]})
        c[r["arm"]][0] += passed
        c[r["arm"]][1] += 1
    return [(m, c["env-only"][0], c["env-only"][1], c[arm][0], c[arm][1])
            for m, c in sorted(cells.items())
            if min(c["env-only"][1], c[arm][1]) >= max(min_n, 1)]


def sensitivity(rows, arm):
    """One row of statistics per scenario, for one task's runs."""
    out = []
    for sc in SCENARIOS:
        st = strata_from_runs(rows, arm, sc)
        rd, ci = mh_rd(st) if st else (float("nan"), (float("nan"), float("nan")))
        out.append({"scenario": sc, "arm": arm, "models": len(st),
                    "control": [sum(s[1] for s in st), sum(s[2] for s in st)],
                    "skill": [sum(s[3] for s in st), sum(s[4] for s in st)],
                    "mh_rd": rd, "mh_ci95": list(ci),
                    "exact_p": exact_stratified_p(st) if st else float("nan"),
                    "positive": sum(1 for _, x0, n0, x1, n1 in st if x1 / n1 > x0 / n0),
                    "negative": sum(1 for _, x0, n0, x1, n1 in st if x1 / n1 < x0 / n0)})
    return out


def mh_rd(strata):
    """Mantel-Haenszel risk difference and its Sato-variance 95% CI."""
    w = [n0 * n1 / (n0 + n1) for _, _, n0, _, n1 in strata]
    sw = sum(w)
    if not sw:
        return float("nan"), (float("nan"), float("nan"))
    rd = sum((x1 * n0 - x0 * n1) / (n0 + n1) for _, x0, n0, x1, n1 in strata) / sw
    p = sum((n1 * n1 * x0 - n0 * n0 * x1 + n1 * n0 * (n0 - n1) / 2) / (n0 + n1) ** 2
            for _, x0, n0, x1, n1 in strata)
    q = sum((x1 * (n0 - x0) + x0 * (n1 - x1)) / (2 * (n0 + n1))
            for _, x0, n0, x1, n1 in strata)
    var = (rd * p + q) / (sw * sw)
    se = math.sqrt(max(var, 0.0))
    return rd, (rd - 1.96 * se, rd + 1.96 * se)


def hypergeom(m, n1, n0):
    """P(skill-arm passes = k), for m total passes, n1 skill runs, n0 control runs."""
    tot = math.comb(n1 + n0, m)
    return {k: math.comb(n1, k) * math.comb(n0, m - k) / tot
            for k in range(max(0, m - n0), min(m, n1) + 1)}


def exact_stratified_p(strata):
    """Two-sided exact conditional p: sum of P(T=t) over t no more likely than observed."""
    dist = {0: 1.0}
    obs = 0
    for _, x0, n0, x1, n1 in strata:
        h = hypergeom(x0 + x1, n1, n0)
        new = {}
        for t, pt in dist.items():
            for k, pk in h.items():
                new[t + k] = new.get(t + k, 0.0) + pt * pk
        dist = new
        obs += x1
    p_obs = dist.get(obs, 0.0)
    return min(1.0, sum(p for p in dist.values() if p <= p_obs * (1 + 1e-9)))


def holm(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    adj, running = [0.0] * len(ps), 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (len(ps) - rank) * ps[i]))
        adj[i] = running
    return adj


def bh(ps):
    order = sorted(range(len(ps)), key=lambda i: ps[i], reverse=True)
    adj, running = [0.0] * len(ps), 1.0
    for rank, i in enumerate(order):
        running = min(running, ps[i] * len(ps) / (len(ps) - rank))
        adj[i] = running
    return adj


def analyse(summaries, arm):
    rows = []
    for s in summaries:
        st = strata_of(s, arm)
        if not st:
            continue
        rd, ci = mh_rd(st)
        rows.append({
            "task": s.get("task"),
            "arm": arm,
            "models": [{"model": m, "control": [x0, n0], "skill": [x1, n1],
                        "rd": x1 / n1 - x0 / n0,
                        "ci95": list(newcombe(x0, n0, x1, n1))}
                       for m, x0, n0, x1, n1 in st],
            "mh_rd": rd, "mh_ci95": list(ci),
            "exact_p": exact_stratified_p(st),
            "positive": sum(1 for _, x0, n0, x1, n1 in st if x1 / n1 > x0 / n0),
            "zero": sum(1 for _, x0, n0, x1, n1 in st if x1 / n1 == x0 / n0),
            "negative": sum(1 for _, x0, n0, x1, n1 in st if x1 / n1 < x0 / n0),
        })
    ps = [r["exact_p"] for r in rows]
    for r, h, b in zip(rows, holm(ps), bh(ps)):
        r["holm_p"], r["bh_p"] = h, b
    return rows


def power_table(n=10, alpha=0.05):
    """P(Fisher p < alpha) for n vs n, by exact enumeration over both binomials."""
    def binom(k, p):
        return math.comb(n, k) * p ** k * (1 - p) ** (n - k)
    sig = {(a, b): fisher_exact(a, n - a, b, n - b) < alpha
           for a in range(n + 1) for b in range(n + 1)}
    out = []
    for p0 in (0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9):
        for p1 in (0.3, 0.5, 0.7, 0.9, 1.0):
            if p1 <= p0:
                continue
            pw = sum(binom(a, p0) * binom(b, p1)
                     for a in range(n + 1) for b in range(n + 1) if sig[(a, b)])
            out.append((p0, p1, pw))
    return out


def fmt_pp(x):
    return "%+.0f" % (100 * x)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("summaries", nargs="*")
    ap.add_argument("--arm", default="env+skill")
    ap.add_argument("--json")
    ap.add_argument("--runs", help="one task's runs CSV: primary and sensitivity analyses")
    ap.add_argument("--power", action="store_true")
    a = ap.parse_args()

    if a.runs:
        with open(a.runs, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        out = sensitivity(rows, a.arm)
        if not any(r["models"] for r in out):
            print("no model has both env-only and %s runs in %s" % (a.arm, a.runs))
            return 1
        print("| analysis | models | control | skill | MH RD, pp (95% CI) | exact p | + / - |")
        print("|---|---|---|---|---|---|---|")
        for r in out:
            print("| %s | %d | %d/%d | %d/%d | %s (%s, %s) | %.4f | %d / %d |" % (
                r["scenario"], r["models"], r["control"][0], r["control"][1],
                r["skill"][0], r["skill"][1], fmt_pp(r["mh_rd"]), fmt_pp(r["mh_ci95"][0]),
                fmt_pp(r["mh_ci95"][1]), r["exact_p"], r["positive"], r["negative"]))
        if a.json:
            with open(a.json, "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=2)
                fh.write("\n")
        return 0

    if a.power:
        print("P(p < 0.05, Fisher) for one 10-vs-10 cell")
        print("  control  skill   power")
        for p0, p1, pw in power_table():
            print("  %5.0f%%  %5.0f%%  %5.0f%%" % (100 * p0, 100 * p1, 100 * pw))
        return 0

    summaries = [json.load(open(p, encoding="utf-8")) for p in a.summaries]
    rows = analyse(summaries, a.arm)
    if not rows:
        print("no task has a %s arm" % a.arm)
        return 1
    print("| task | models | MH RD, pp (95% CI) | exact p | Holm | BH | + / 0 / - |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        print("| %s | %d | %s (%s, %s) | %.4f | %.4f | %.4f | %d / %d / %d |" % (
            r["task"], len(r["models"]), fmt_pp(r["mh_rd"]), fmt_pp(r["mh_ci95"][0]),
            fmt_pp(r["mh_ci95"][1]), r["exact_p"], r["holm_p"], r["bh_p"],
            r["positive"], r["zero"], r["negative"]))
    print()
    for r in rows:
        print("%s" % r["task"])
        for m in r["models"]:
            print("  %-14s %2d/%-2d -> %2d/%-2d  %s pp (%s, %s)" % (
                m["model"], m["control"][0], m["control"][1], m["skill"][0],
                m["skill"][1], fmt_pp(m["rd"]), fmt_pp(m["ci95"][0]), fmt_pp(m["ci95"][1])))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
