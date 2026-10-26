#!/usr/bin/env python
"""A wave's result pack, reproducible from the published record alone.

    python wave_report.py --results DIR --wave N --sweep FILE [--speed-dbs DIR] [--out FILE]

Reads `<results>/runs_<task>.csv` for every task the wave declares (sweep.json
`waves.<N>.tasks`), plus each run's `run.json` and `envelope.json` underneath
`<results>/runs/<task>/`. Statistics are the harness's own (stratified_effect,
summarize), imported read-only -- nothing here reimplements a p-value. Prints
markdown; with --out also writes it there. Safe to run while the wave is in
flight: a cell short of its declared rep count is reported as incomplete.

Everything that varies between waves comes from the sweep, not from constants in
this file:
  tasks[t].models    per task, minus any model the wave lists `incomplete`
  tasks[t].arms      the control arm is env-only; the primary skill arm is the
                     first other arm in the list, unless `arms_compared.primary`
                     names a pair [skill, control] -- then that pair, as in
                     stratified_effect.py --control
  reps               wave-level, falling back to the sweep's top-level `reps`
  pilot_runs         CI run ids to drop for the "pilot runs removed" sensitivity
                     row; the row is left out entirely when undeclared
  speed_baseline     model -> tokens/second, for the Speed section's comparison

Sections (as the wave-6 hand-rolled version this replaces, "finish.py", printed):
  Completeness    n/reps per model per task, both arms
  Headline        MH risk difference + exact p per task, Holm/BH across tasks
  Per model per task   Newcombe CI + Fisher p, one model at a time
  Sensitivity     stratified_effect's scenarios (including "timeouts removed"),
                  plus "pilot runs removed" where the wave declares pilot_runs
  Behaviour       failures, failures-fixed %, minutes to output, ASTRA tool
                  decisions, off-spec, timeouts, gate failures
  Tool choice     methods run, ASTRA's decided tool, pass rate by first method
  Speed           only with --speed-dbs: output tokens per generation second,
                  from each run's opencode.db per-message timings

Dropped from finish.py, the wave-6 one-off: the hardcoded pilot-run id set and
speed baseline (now sweep fields), and the upload-eacces job-log-grades
sensitivity row -- that reads an acknowledgements file with no sweep-declared
equivalent, so it has no general form and is not reproducible from the record
alone.
"""
import argparse
import csv
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stratified_effect as se  # noqa: E402
from summarize import fisher_exact, newcombe  # noqa: E402
from model_ids import from_segment, segment  # noqa: E402

MIN_N = 5  # summarize.MIN_N_FOR_STATS; a per-model row below this is not compared


def pp(x):
    return "%+.0f" % (100 * x) if x == x else "n/a"


def fp(p):
    if p != p:
        return "n/a"
    return "< 0.0001" if p < 0.0001 else "%.4f" % p


def het(p, i2):
    return "n/a" if p != p else "%.4f (%.0f%%)" % (p, 100 * i2)


# --------------------------------------------------------------------- sweep

def wave_config(sweep, wave):
    """Everything this wave declares: per-task models/arms/control/skill, reps,
    pilot run ids and the speed baseline. Models the wave lists `incomplete` are
    dropped from every task's model list -- as assemble_cell.py refuses to run
    them, the record holds nothing for them worth reporting."""
    w = (sweep.get("waves") or {}).get(str(wave))
    if w is None:
        raise SystemExit("wave %s is not declared in the sweep" % wave)
    incomplete = {k for k in (w.get("incomplete") or {}) if not k.startswith("_")}
    reps = w.get("reps", sweep.get("reps"))
    pair = (w.get("arms_compared") or {}).get("primary")
    tasks = {}
    for t, td in (w.get("tasks") or {}).items():
        models = [m for m in (td.get("models") or []) if m not in incomplete]
        arms = td.get("arms") or []
        if pair:
            skill, control = pair[0], pair[1]
        else:
            control = "env-only"
            skill = next((a for a in arms if a != control), None)
        tasks[t] = {"models": models, "arms": arms, "skill": skill, "control": control}
    pilot_runs = w.get("pilot_runs")
    return tasks, reps, pilot_runs, (w.get("speed_baseline") or {})


# ---------------------------------------------------------------------- load

def load(results, tasks):
    """runs_<task>.csv rows, each augmented with its run.json (_run) and
    envelope.json (_env), and `_ci_run` (the bare CI run id, for pilot matching)."""
    rows = {}
    for t in tasks:
        path = os.path.join(results, "runs_%s.csv" % t)
        if not os.path.exists(path):
            rows[t] = []
            continue
        with open(path, encoding="utf-8", newline="") as fh:
            rows[t] = list(csv.DictReader(fh))
        for r in rows[t]:
            d = os.path.join(results, "runs", t, "%s__%s__%s__r%s"
                             % (t, segment(r["model"]), r["arm"], r["rep"]))
            r["_run"] = r["_env"] = {}
            for name, key in (("run.json", "_run"), ("envelope.json", "_env")):
                p = os.path.join(d, name)
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as fh:
                        r[key] = json.load(fh)
            r["_ci_run"] = str(r["_run"].get("ci_run", "")).split("/")[0]
    return rows


# -------------------------------------------------------------------- stats

def custom_strata(rows, models, control, arm, keep=lambda r: True,
                  passed=lambda r: r["passed"] == "True"):
    """(model, control passes, control n, skill passes, skill n), for a scenario
    stratified_effect.py has no concept of (e.g. "this run's CI id is a pilot
    run"). Mirrors strata_from_runs's own shape and MIN_N cutoff."""
    cells = defaultdict(lambda: {control: [0, 0], arm: [0, 0]})
    for r in rows:
        if r["valid"] != "True" or r["arm"] not in (control, arm) or not keep(r):
            continue
        if r["model"] not in models:
            continue
        c = cells[r["model"]]
        c[r["arm"]][0] += passed(r)
        c[r["arm"]][1] += 1
    return [(m, c[control][0], c[control][1], c[arm][0], c[arm][1])
            for m, c in sorted(cells.items())
            if min(c[control][1], c[arm][1]) >= MIN_N]


def row_dict(name, strata):
    """One sensitivity/headline row, shaped like stratified_effect.sensitivity()'s
    own rows, so one formatter prints both."""
    if not strata:
        return {"scenario": name, "models": 0}
    rd, ci = se.mh_rd(strata)
    q, het_p, i2 = se.heterogeneity(strata)
    return {"scenario": name, "models": len(strata),
            "control": [sum(s[1] for s in strata), sum(s[2] for s in strata)],
            "skill": [sum(s[3] for s in strata), sum(s[4] for s in strata)],
            "mh_rd": rd, "mh_ci95": list(ci),
            "exact_p": se.exact_stratified_p(strata),
            "positive": sum(1 for _, x0, n0, x1, n1 in strata if x1 / n1 > x0 / n0),
            "negative": sum(1 for _, x0, n0, x1, n1 in strata if x1 / n1 < x0 / n0),
            "het_q": q, "het_p": het_p, "i2": i2}


def fmt_row(r):
    if not r["models"]:
        return "| %s | 0 | | | | | | |" % r["scenario"]
    return "| %s | %d | %d/%d | %d/%d | %s pp (%s, %s) | %s | %d / %d | %s |" % (
        r["scenario"], r["models"], r["control"][0], r["control"][1],
        r["skill"][0], r["skill"][1], pp(r["mh_rd"]), pp(r["mh_ci95"][0]),
        pp(r["mh_ci95"][1]), fp(r["exact_p"]), r["positive"], r["negative"],
        het(r["het_p"], r["i2"]))


ROW_HEADER = ("| analysis | models | no skill | with skill | difference (95% CI) "
             "| exact p | + / - | het p (I2) |")
ROW_RULE = "|---|---|---|---|---|---|---|---|"


# -------------------------------------------------------------------- speed

def speed(dbs_dir, rep="1"):
    """model -> per-run output tokens per generation second, for one rep.

    Generation time is the sum of (completed - created) over assistant messages
    that recorded both; output tokens are summed over the same messages. The
    model is read off the run directory, <task>__<segment>__<arm>__r<rep>, as its
    wave id (model_ids.from_segment), not assumed to be one of a fixed list.
    """
    out = defaultdict(list)
    dbs = [os.path.join(d, "opencode.db") for d, _, fs in os.walk(dbs_dir) if "opencode.db" in fs]
    for db in dbs:                      # os.walk, not glob "**": databases sit under .xdg-data
        run = [p for p in db.replace("\\", "/").split("/")
               if len(p.split("__")) == 4 and p.split("__")[3] == "r%s" % rep]
        if not run:
            continue
        o = secs = 0.0
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            for (data,) in con.execute("select data from message"):
                x = json.loads(data)
                tm, tk = x.get("time") or {}, x.get("tokens") or {}
                if x.get("role") == "assistant" and tm.get("created") and tm.get("completed"):
                    secs += (tm["completed"] - tm["created"]) / 1000
                    o += tk.get("output") or 0
        finally:
            con.close()
        if secs > 0:
            out[from_segment(run[-1].split("__")[1])].append(o / secs)
    return out


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="results/wave<N> directory")
    ap.add_argument("--wave", required=True, type=int)
    ap.add_argument("--sweep", required=True, help="sweep.json")
    ap.add_argument("--speed-dbs", help="directory of rep-1 run artifacts (agent databases)")
    ap.add_argument("--speed-reps", default="1", help="comma list; rep 1 is the declared check")
    ap.add_argument("--out")
    a = ap.parse_args()

    with open(a.sweep, encoding="utf-8") as fh:
        sweep = json.load(fh)
    tasks, reps, pilot_runs, speed_baseline = wave_config(sweep, a.wave)
    if reps is None:
        raise SystemExit("wave %s declares no reps, and neither does the sweep" % a.wave)
    task_order = list(tasks)
    rows = load(a.results, task_order)
    L = []

    # -- completeness ---------------------------------------------------
    L += ["## Completeness", "", "| task | model | no skill | with skill |", "|---|---|---|---|"]
    complete = True
    for t in task_order:
        cfg = tasks[t]
        for m in cfg["models"]:
            cells = []
            for arm in (cfg["control"], cfg["skill"]):
                rs = [r for r in rows[t] if r["model"] == m and r["arm"] == arm]
                n, lost = len(rs), sum(1 for r in rs if r["lost"])
                complete &= n == reps
                cells.append("%d/%d%s" % (n, reps, " (%d lost)" % lost if lost else ""))
            L.append("| %s | %s | %s | %s |" % (t, m, cells[0], cells[1]))
    n_cells = sum(len(tasks[t]["models"]) * 2 for t in task_order)
    L += ["", ("All %d cells complete." % n_cells) if complete
          else "NOT COMPLETE: numbers below are interim.", ""]

    # -- headline + per model --------------------------------------------
    L += ["## Headline (per task, models stratified)", "", ROW_HEADER, ROW_RULE]
    ps = []
    for t in task_order:
        cfg = tasks[t]
        st = se.strata_from_runs(rows[t], cfg["skill"], "primary", control=cfg["control"])
        L.append(fmt_row(row_dict(t, st)))
        ps.append(se.exact_stratified_p(st) if st else float("nan"))
    tested = [(t, p) for t, p in zip(task_order, ps) if p == p]
    if tested:
        holm, bh = se.holm([p for _, p in tested]), se.bh([p for _, p in tested])
        L += ["", "Adjusted across %d tasks (Holm / BH): " % len(tested) + "; ".join(
            "%s %s / %s" % (t, fp(h), fp(b)) for (t, _), h, b in zip(tested, holm, bh))]
    L += ["", "## Per model per task", ""]
    for t in task_order:
        cfg = tasks[t]
        L += ["**%s**" % t, "", "| model | no skill | with skill | difference (95% CI) | Fisher p |",
              "|---|---|---|---|---|"]
        for m in cfg["models"]:
            v = [r for r in rows[t] if r["model"] == m and r["valid"] == "True"]
            x0 = sum(r["passed"] == "True" for r in v if r["arm"] == cfg["control"])
            n0 = sum(1 for r in v if r["arm"] == cfg["control"])
            x1 = sum(r["passed"] == "True" for r in v if r["arm"] == cfg["skill"])
            n1 = sum(1 for r in v if r["arm"] == cfg["skill"])
            if min(n0, n1) < MIN_N:
                L.append("| %s | %d/%d | %d/%d | not tested | |" % (m, x0, n0, x1, n1))
                continue
            d, (lo, hi) = x1 / n1 - x0 / n0, newcombe(x0, n0, x1, n1)
            L.append("| %s | %d/%d | %d/%d | %s pp (%s, %s) | %s |" % (
                m, x0, n0, x1, n1, pp(d), pp(lo), pp(hi),
                fp(fisher_exact(x1, n1 - x1, x0, n0 - x0))))
        L.append("")

    # -- sensitivity ------------------------------------------------------
    L += ["## Sensitivity", ""]
    for t in task_order:
        cfg = tasks[t]
        L += ["**%s**" % t, "", ROW_HEADER, ROW_RULE]
        for r in se.sensitivity(rows[t], cfg["skill"], control=cfg["control"]):
            L.append(fmt_row(r))
        if pilot_runs is not None:
            st = custom_strata(rows[t], cfg["models"], cfg["control"], cfg["skill"],
                               keep=lambda r: r["_ci_run"] not in pilot_runs)
            L.append(fmt_row(row_dict("pilot runs removed", st)))
        L.append("")
    if pilot_runs is not None:
        pil = sum(1 for t in task_order for r in rows[t] if r["_ci_run"] in pilot_runs)
        L += ["Pilot runs in the published record: %d." % pil, ""]

    # -- behaviour ----------------------------------------------------------
    L += ["## Behaviour (valid runs)", "",
          "| task | arm | failures | minutes to output (median, n) | ASTRA names the decided tool | off-spec | timeouts | gate failures |",
          "|---|---|---|---|---|---|---|---|"]
    for t in task_order:
        cfg = tasks[t]
        fails = {}
        for arm in (cfg["control"], cfg["skill"]):
            v = [r for r in rows[t] if r["arm"] == arm and r["valid"] == "True"
                 and r["model"] in cfg["models"]]
            if not v:
                continue
            fails[arm] = sum(r["passed"] != "True" for r in v)
            sto = [r["_run"].get("seconds_to_output") for r in v if r["passed"] in ("True", "False")]
            sto = [s / 60 for s in sto if isinstance(s, (int, float)) and s > 0]
            gates = Counter(g.split(":")[0] for r in v for g in (r["_env"].get("gate_failures") or []))
            L.append("| %s | %s | %d/%d | %s | %d | %d | %d | %s |" % (
                t, arm, fails[arm], len(v),
                "%.1f (%d)" % (statistics.median(sto), len(sto)) if sto else "",
                sum(1 for r in v if r["decided_tools"]),
                sum(1 for r in v if (r["off_spec"] or "none") not in ("none", "unchecked")),
                sum(1 for r in v if r["timed_out"] == "True"),
                ", ".join("%s %d" % kv for kv in gates.most_common(4))))
        if len(fails) == 2 and fails[cfg["control"]]:
            L.append("| %s | failures fixed | %+.0f%% of no-skill failures | | | | | |" % (
                t, 100 * (fails[cfg["control"]] - fails[cfg["skill"]]) / fails[cfg["control"]]))
    L.append("")

    # -- tool choice ----------------------------------------------------------
    L += ["## Tool choice (valid runs)", "",
          "| task | arm | methods run (runs) | ASTRA decided tool (runs) | pass rate by first method |",
          "|---|---|---|---|---|"]
    for t in task_order:
        cfg = tasks[t]
        for arm in (cfg["control"], cfg["skill"]):
            v = [r for r in rows[t] if r["arm"] == arm and r["valid"] == "True"]
            if not v:
                continue
            meth = Counter(x for r in v for x in set(filter(None, r["methods"].split(";"))))
            dec = Counter(x for r in v for x in set(filter(None, r["decided_tools"].split(";"))))
            first = defaultdict(lambda: [0, 0])
            for r in v:
                seq = r["_run"].get("method_sequence") or ["none"]
                first[seq[0]][0] += r["passed"] == "True"
                first[seq[0]][1] += 1
            L.append("| %s | %s | %s | %s | %s |" % (
                t, arm, ", ".join("%s %d" % kv for kv in meth.most_common(5)),
                ", ".join("%s %d" % kv for kv in dec.most_common(4)),
                ", ".join("%s %d/%d" % (k, x, n)
                         for k, (x, n) in sorted(first.items(), key=lambda kv: -kv[1][1])[:4])))
    L.append("")

    # -- speed ----------------------------------------------------------------
    if a.speed_dbs:
        L += ["## Speed (output tokens per generation second; rep 1 is the declared check)", "",
              "| rep | model | runs | median | baseline | two thirds | below? |",
              "|---|---|---|---|---|---|---|"]
        for rep in a.speed_reps.split(","):
            sp = speed(a.speed_dbs, rep)
            for m in sorted(sp):
                med, base = statistics.median(sp[m]), speed_baseline.get(m)
                L.append("| %s | %s | %d | %.1f | %s | %s | %s |" % (
                    rep, m, len(sp[m]), med, base or "", "%.1f" % (2 * base / 3) if base else "",
                    ("yes" if med < 2 * base / 3 else "no") if base else ""))
        L.append("")

    text = "\n".join(L)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
