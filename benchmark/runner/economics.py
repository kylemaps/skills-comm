#!/usr/bin/env python
"""What does one usable result cost, with the skill and without?

    python economics.py runs_<task>.csv [--by-model] [--baseline env-only]

Pass rate is the scientific result. It is not the number anyone deciding whether
to install a skill actually needs, which is closer to: what do I pay per mask I
can use?

Those come apart. A skill that reads its own references and runs QC costs more
per attempt. If it also converts failures into successes, the cost per SUCCESS
can fall even while the cost per RUN rises. Both directions are worth knowing
and the pass rate shows neither.

Everything here comes from fields summarize.py already writes, so this is a
re-reading of runs we have, not a new measurement.

  cost per run      total / runs      what an attempt costs
  cost per success  total / passes    what a usable result costs, failures included

The second is the honest one for planning, because failed runs are paid for too.

WHY ARM-VERSUS-ARM IS FAIR HERE AND CROSS-MODEL IS NOT
------------------------------------------------------
Token totals are not comparable across models on this gateway. Some report
prompt-cache reads separately and carry a tiny input count; others report no
cache and put everything in input. Measured on these runs, glm-5.2 shows ~106 K
input with ~2.70 M cache reads while qwen3.5-122b shows ~1.43 M input and no
cache. On input plus output alone glm looks 14x cheaper; counting everything the
model actually read, it processed about twice as much.

Comparing ARMS within a task is still fair, because every arm runs the same
models the same number of times, so the accounting difference appears identically
on both sides and cancels. Comparing MODELS is not fair and `--by-model` is
labelled accordingly.

BILLED and PROCESSED are both reported for the same reason. Neither is money:
cache reads are usually discounted but not free, and this gateway publishes no
prices.
"""
import argparse
import csv
import statistics
from collections import defaultdict


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fmt(n):
    if n is None:
        return "-"
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fk" % (n / 1e3)
    return "%.0f" % n


def med(xs):
    return statistics.median(xs) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--baseline", default="env-only")
    ap.add_argument("--by-model", action="store_true")
    a = ap.parse_args()

    with open(a.csv_path, encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh)
                if str(r.get("valid", "1")).lower() not in ("0", "false", "")]
    if not rows:
        raise SystemExit("no valid runs in %s" % a.csv_path)

    def passed(r):
        return str(r.get("passed", "")).lower() in ("1", "true", "yes")

    def billed(r):
        return (num(r.get("tokens_input")) + num(r.get("tokens_output"))
                + num(r.get("tokens_reasoning")))

    def processed(r):
        return billed(r) + num(r.get("tokens_cache_read"))

    arms = sorted({r["arm"] for r in rows}, key=lambda x: (x != a.baseline, x))

    def block(title, groups, order, comparable=True):
        print("")
        print("--- %s ---" % title)
        print("  %-20s%6s%7s%13s%14s%16s%11s%8s"
              % ("", "runs", "pass", "billed/run", "billed/PASS",
                 "processed/PASS", "min/PASS", "vs base"))
        base = None
        for key in order:
            rs = groups.get(key) or []
            if not rs:
                continue
            n = len(rs)
            k = sum(1 for r in rs if passed(r))
            tb = sum(billed(r) for r in rs)
            tp = sum(processed(r) for r in rs)
            mins = sum(num(r.get("duration_s")) for r in rs) / 60.0
            # No passes means the cost per success is undefined, not enormous.
            # Printing a huge number there invites someone to average it.
            bpp = (tb / k) if k else None
            ppp = (tp / k) if k else None
            mpp = (mins / k) if k else None
            note = ""
            if comparable and key != a.baseline and base and bpp and base[0]:
                note = "%.2fx" % (bpp / base[0])
            if key == a.baseline:
                base = (bpp, mpp)
            print("  %-20s%6d%7d%13s%14s%16s%11s%8s"
                  % (str(key)[:20], n, k, fmt(tb / n), fmt(bpp), fmt(ppp),
                     "-" if mpp is None else "%.0f" % mpp, note))
        if not comparable:
            print("  Not comparable down this column: models differ in whether")
            print("  they report cache reads at all. Read each row against itself.")

    print("=== %s: %d valid runs %s" % (a.csv_path, len(rows), "=" * 18))
    print("  billed    = input + output + reasoning")
    print("  processed = billed + prompt-cache reads")
    print("  /PASS divides by successes, so failed runs are paid for, which is")
    print("  what makes it the planning number.")

    by_arm = defaultdict(list)
    for r in rows:
        by_arm[r["arm"]].append(r)
    block("cost per arm (model mix is identical across arms, so this is fair)",
          by_arm, arms)

    # The single sentence someone installing a skill wants.
    b = by_arm.get(a.baseline) or []
    if b:
        bk = sum(1 for r in b if passed(r))
        bt = sum(billed(r) for r in b)
        for x in arms:
            if x == a.baseline or not by_arm.get(x):
                continue
            rs = by_arm[x]
            k = sum(1 for r in rs if passed(r))
            t = sum(billed(r) for r in rs)
            if not k or not bk:
                continue
            bp = sum(processed(r) for r in b)
            tpz = sum(processed(r) for r in rs)
            per_run = (t / len(rs)) / (bt / len(b)) if bt else 0
            per_pass = (t / k) / (bt / bk) if bt else 0
            proc_pass = (tpz / k) / (bp / bk) if bp else 0
            print("")
            print("  %s vs %s:" % (x, a.baseline))
            print("    an attempt costs           %.2fx" % per_run)
            print("    a usable result costs      %.2fx  (billed)" % per_pass)
            print("    a usable result costs      %.2fx  (processed)" % proc_pass)

            # The two accountings can point opposite ways, and on this data four
            # of six comparisons do. Cache reads are usually discounted but not
            # free, and this gateway publishes no prices, so there is no way to
            # pick between them from here. When they disagree, "the skill is
            # cheaper" and "the skill is dearer" are both defensible from the
            # same runs -- which means neither is a finding, and saying so is
            # the only honest output.
            if (per_pass < 1) != (proc_pass < 1):
                print("    -> ACCOUNTING-DEPENDENT: billed and processed disagree")
                print("       on the sign. Do not report a direction for this one")
                print("       without per-model pricing from the gateway.")
            elif per_pass < 1 <= per_run:
                print("    -> more expensive per attempt, CHEAPER per result,")
                print("       under both accountings")
            elif per_pass < 1:
                print("    -> cheaper both ways, under both accountings")
            else:
                print("    -> more expensive per result under both accountings")

    if a.by_model:
        models = sorted({r["model"] for r in rows})
        for m in models:
            g = defaultdict(list)
            for r in rows:
                if r["model"] == m:
                    g[r["arm"]].append(r)
            block("model %s (compare rows within this block only)" % m,
                  g, arms, comparable=False)


if __name__ == "__main__":
    main()
