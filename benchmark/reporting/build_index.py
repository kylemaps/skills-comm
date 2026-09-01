#!/usr/bin/env python3
"""Build the leaderboard overview: a task × model grid where each task links to its full report.

Reads one `summary.json` per task (the harness output) and emits a single self-contained
`index.html`. Each cell shows a model's pass rate; when a task has two arms (e.g. no-skill vs
with-skill) both are shown. Tasks with a `report_href` become clickable.

    python build_index.py \
        --entry "brain-extraction-7t" path/to/summary.json report_7t.html \
        --entry "tissue-seg-7t"       path/to/summary2.json ./report_tissue.html \
        --out index.html

`report_href` may be a relative path (committed alongside index.html) or a URL.

Or point it at a report directory and let it find the tasks itself:

    python build_index.py --report-dir ~/bench/report --out index.html

which picks up every `summary_<task>.json` the harness wrote, and links each task to
`report_<task>.html` when that file is sitting next to it. One command, so a finished sweep
lands in the dashboard without anyone editing an argument list.

THE UNIT IS A CELL
------------------
A cell is one task, one model, one arm, repeated N times. Two things follow, and both are
visible in the output rather than buried:

* **`k/n` is shown next to every rate.** 8/10 and 80/100 are both "80%" and mean very
  different things. A rate on its own lets a topped-up cell sit in the grid looking like a
  comparison while the denominators have quietly diverged.
* **Every rate carries its 95% Wilson interval**, drawn as a track under the badge. Wilson
  rather than the normal approximation because these cells hit 0/10 and 10/10 routinely, and
  the normal approximation returns nonsense at both ends.

Cells short of the expected repeat count are marked, not silently averaged.

The effects table reads `skill_effect` straight out of `summary.json` rather than recomputing
it, so the dashboard cannot drift from the numbers the analysis scripts publish.

Stdlib only.
"""
import argparse
import glob
import html
import json
import math
import os
import re
from pathlib import Path


ARM_LABEL = {"env-only": "no skill", "env+skill": "with skill",
             "baseline": "no skill", "skill": "with skill"}

# Baseline arms sort first so a cell reads "no skill then with skill" left to right,
# whatever order the harness happened to write them in.
BASELINE_ARMS = ("env-only", "baseline")


def is_skill_arm(arm):
    return arm not in BASELINE_ARMS


def wilson(k, n, z=1.96):
    """Wilson score interval for a proportion, as (lo, hi) in 0..1.

    Sane at k=0 and k=n, unlike the normal approximation. Identical to the one in
    summarize.py; the two must agree, so if you change one change both.
    """
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (centre - half) / d), min(1.0, (centre + half) / d))


def cell_matrix(summary):
    """summary.json -> {model: {arm: (passes, n)}}, preserving model order."""
    out = {}
    for key, v in summary.get("cells", {}).items():
        model, _, arm = key.partition("|")
        out.setdefault(model, {})[arm] = (v.get("passes", 0), v.get("n", 0))
    return out


def effect_rows(summary):
    """summary.json -> [(model, arm, effect_dict)], from `skill_effect`.

    The key is `model|arm` when the sweep had more than one skill arm and a bare `model`
    when it had one. Older summaries only ever wrote the bare form, so both are accepted.
    Head-to-head entries (`skillA_vs_skillB`) are skipped: this table is against baseline.
    """
    rows = []
    for key, v in (summary.get("skill_effect") or {}).items():
        model, _, arm = key.partition("|")
        if "_vs_" in arm:
            continue
        rows.append((model, arm or "env+skill", v))
    return rows


def expected_reps(summary):
    """The repeat count this task was run at: the most common n across its cells.

    Inferred rather than configured, because it varies by task and a stale constant would
    quietly mark every cell of a 5-repeat pilot as short.
    """
    ns = [v.get("n", 0) for v in summary.get("cells", {}).values() if v.get("n")]
    return max(set(ns), key=ns.count) if ns else 0


def pct_badge(passes, n, expect=0):
    """The rate, its denominator, and its interval."""
    r = passes / n if n else 0
    lo, hi = wilson(passes, n)
    short = bool(expect) and n < expect
    tip = "%d of %d passed. 95%% CI %.0f–%.0f%%." % (passes, n, lo * 100, hi * 100)
    if short:
        tip += " Short cell: %d runs, expected %d." % (n, expect)
    return (
        '<span class="badge{sh}" title="{tip}">'
        '<span class="kn">{k}/{n}</span>'
        '<span class="pct" style="--v:{v}">{p:.0f}%</span>'
        '<span class="ci"><i style="left:{lo:.1f}%;width:{w:.1f}%"></i></span>'
        "</span>"
    ).format(sh=" short" if short else "", tip=html.escape(tip), k=passes, n=n,
             v=r, p=r * 100, lo=lo * 100, w=max(hi - lo, 0.01) * 100)


def fmt_pp(v, force_dp=False):
    """A percentage-point figure at the precision the harness published it.

    Rounding to whole points would print a lower bound of +0.4 as "+0", which reads as an
    interval that excludes no effect when it does not. Trailing .0 is dropped so the common
    case stays terse.
    """
    t = "%+.1f" % v
    if t.endswith(".0") and not force_dp:
        t = t[:-2]
    return "0" if t in ("+0", "-0") else t


def fmt_ci(lo, hi):
    """Both bounds at the same precision, so one interval never reads as two."""
    dp = not (("%+.1f" % lo).endswith(".0") and ("%+.1f" % hi).endswith(".0"))
    return "[%s, %s]" % (fmt_pp(lo, dp), fmt_pp(hi, dp))


def delta_bar(delta, lo, hi):
    """A difference in percentage points, drawn on a track centred at zero.

    -100..+100 pp maps to 0..100% of the track. The whisker is the 95% CI; when it
    straddles the centre line the experiment cannot separate the arms, and that is the
    single most useful thing to be able to see without reading a number.
    """
    def x(pp):
        return min(100.0, max(0.0, 50.0 + pp / 2.0))

    b0, b1 = sorted((x(0), x(delta)))
    w0, w1 = sorted((x(lo), x(hi)))
    cls = "pos" if delta > 0 else ("neg" if delta < 0 else "nil")
    return (
        '<span class="dbar"><i class="axis"></i>'
        '<i class="whisk" style="left:{w0:.1f}%;width:{ww:.1f}%"></i>'
        '<i class="fill {cls}" style="left:{b0:.1f}%;width:{bw:.1f}%"></i>'
        "</span>"
    ).format(w0=w0, ww=max(w1 - w0, 0.6), cls=cls, b0=b0, bw=max(b1 - b0, 0.6))


STYLE = """<style>
:root{--bg:#f5f8fb;--panel:#fff;--ink:#16222e;--muted:#5b6b7a;--line:#e0e8ef;--accent:#2c7fb8;
--accent-soft:#e7f1f8;--pend:#aeb9c4;--base:#7fa8c9;--skill:#2ca25f;--neg:#c14a3a;--track:#dbe4ec;
--mono:ui-monospace,Menlo,Consolas,monospace;--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
--shadow:0 1px 2px rgba(20,40,60,.06),0 10px 30px rgba(20,40,60,.05)}
@media(prefers-color-scheme:dark){:root{--bg:#0e1620;--panel:#16212e;--ink:#dce6ef;--muted:#8ea0b2;--line:#26333f;
--accent:#4aa3d6;--accent-soft:#16303f;--pend:#3a4756;--base:#6f98ba;--skill:#3cb371;--neg:#d0604e;--track:#2b3947;
--shadow:0 1px 2px rgba(0,0,0,.3),0 12px 32px rgba(0,0,0,.4)}}
:root[data-theme=light]{--bg:#f5f8fb;--panel:#fff;--ink:#16222e;--muted:#5b6b7a;--line:#e0e8ef;--accent:#2c7fb8;--base:#7fa8c9;--skill:#2ca25f;--track:#dbe4ec}
:root[data-theme=dark]{--bg:#0e1620;--panel:#16212e;--ink:#dce6ef;--muted:#8ea0b2;--line:#26333f;--accent:#4aa3d6;--base:#6f98ba;--skill:#3cb371;--track:#2b3947}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:15px}
.wrap{max-width:1120px;margin:0 auto;padding:44px 22px 70px}h1{font-size:1.7rem;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
h2{font-size:1.05rem;margin:34px 0 4px}h2+.sub{margin-bottom:14px;font-size:.88rem}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:.8rem;color:var(--muted);margin-bottom:14px;align-items:center}
.legend .sw{width:11px;height:11px;border-radius:3px;display:inline-block;vertical-align:middle;margin-right:5px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:.9rem;min-width:640px}
th{font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600;text-align:center;padding:16px 12px 12px;border-bottom:1px solid var(--line)}
th.task,th.l{text-align:left}th .model{font-family:var(--mono);text-transform:none;font-size:.82rem;color:var(--ink)}
td{padding:12px;border-bottom:1px solid var(--line);text-align:center;vertical-align:middle}tr:last-child td{border-bottom:none}
tr:hover td{background:color-mix(in srgb,var(--accent-soft) 55%,transparent)}
.task a{color:var(--ink);text-decoration:none;font-weight:600}.task a:hover{color:var(--accent)}
.task span.na{color:var(--muted);font-weight:400}
.cell{display:inline-flex;flex-direction:column;gap:9px;min-width:132px}
.row{display:flex;align-items:center;justify-content:space-between;gap:8px}
.clab{font:600 10px/1 var(--mono);padding:3px 6px;border-radius:5px;color:#fff;white-space:nowrap}
.clab.base{background:var(--base)}.clab.skill{background:var(--skill)}
.badge{display:inline-grid;grid-template-columns:auto auto;gap:2px 7px;justify-items:end;align-items:center}
.badge.short .kn{color:var(--neg);border-bottom:1px dashed var(--neg)}
.kn{font:600 10.5px/1 var(--mono);color:var(--muted);font-variant-numeric:tabular-nums}
.pct{font:700 12px/1 var(--sans);color:#fff;padding:4px 8px;border-radius:20px;min-width:44px;text-align:center;font-variant-numeric:tabular-nums;background:color-mix(in srgb,#2ca25f calc(var(--v)*100%),#c14a3a)}
.ci{grid-column:1/-1;position:relative;width:100%;height:3px;border-radius:2px;background:var(--track)}
.ci i{position:absolute;top:0;height:3px;border-radius:2px;background:var(--muted);opacity:.75}
.dbar{position:relative;display:block;width:100%;min-width:150px;height:14px}
.dbar i{position:absolute}
.dbar .axis{left:50%;top:0;width:1px;height:14px;background:var(--muted);opacity:.45}
.dbar .whisk{top:6.5px;height:1px;background:var(--muted);opacity:.7}
.dbar .fill{top:3px;height:8px;border-radius:2px}
.dbar .fill.pos{background:var(--skill)}.dbar .fill.neg{background:var(--neg)}.dbar .fill.nil{background:var(--muted);opacity:.4}
td.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:.82rem;white-space:nowrap}
td.l{text-align:left}td.l .model{font-family:var(--mono);font-size:.85rem}
.tag{font:600 9.5px/1 var(--mono);padding:2px 5px;border-radius:4px;background:var(--accent-soft);color:var(--accent);margin-left:6px}
.flat{color:var(--muted)}
.foot{color:var(--muted);font-size:.8rem;margin-top:18px}.foot code{font-family:var(--mono);background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:1px 5px}
.empty{padding:22px;color:var(--muted);font-size:.88rem}
</style>"""


def build_grid(entries):
    models = []
    for _, s, _ in entries:
        for m in cell_matrix(s):
            if m not in models:
                models.append(m)

    head = "".join(f'<th><span class="model">{html.escape(m)}</span></th>' for m in models)
    rows = []
    for name, s, href in entries:
        mat = cell_matrix(s)
        expect = expected_reps(s)
        tds = []
        for m in models:
            arms = mat.get(m)
            if not arms:
                tds.append('<td><span class="na">—</span></td>')
                continue
            lines = []
            for arm in sorted(arms, key=lambda a: (is_skill_arm(a), a)):
                p, n = arms[arm]
                lab = ARM_LABEL.get(arm, arm)
                cls = "skill" if is_skill_arm(arm) else "base"
                lines.append(f'<span class="row"><span class="clab {cls}">{html.escape(lab)}</span>'
                             f'{pct_badge(p, n, expect)}</span>')
            tds.append(f'<td><span class="cell">{"".join(lines)}</span></td>')
        label = html.escape(name)
        tcell = f'<a href="{html.escape(href)}">{label}</a>' if href else label
        rows.append(f'<tr><td class="task">{tcell}</td>{"".join(tds)}</tr>')

    return (f'<div class="card scroll"><table><thead><tr><th class="task">Task</th>{head}</tr>'
            f'</thead><tbody>{"".join(rows)}</tbody></table></div>')


def build_effects(entries):
    """Every skill-versus-baseline comparison in the sweep, ranked by effect size."""
    rows = []
    for name, s, _ in entries:
        expect = expected_reps(s)
        for model, arm, e in effect_rows(s):
            kb, nb = e.get("env_only_pass", 0), e.get("env_only_n", 0)
            ks, ns = e.get("env_skill_pass", 0), e.get("env_skill_n", 0)
            ci = e.get("ci95_pp") or [0, 0]
            rows.append({
                "task": name, "model": model, "arm": arm,
                "kb": kb, "nb": nb, "ks": ks, "ns": ns,
                "delta": e.get("delta_pp", 0.0), "lo": ci[0], "hi": ci[1],
                "p": e.get("fisher_p"),
                "short": bool(expect) and min(nb, ns) < expect,
            })
    if not rows:
        return ('<div class="card"><p class="empty">No skill-versus-baseline comparison yet. '
                'A task needs both arms, each at the full repeat count.</p></div>')

    rows.sort(key=lambda r: (-r["delta"], r["task"], r["model"]))
    body = []
    for r in rows:
        # A CI that spans zero means this experiment cannot separate the arms. Say so in
        # words, because "the interval includes zero" is exactly the part that gets read
        # past when a result is being quoted.
        separates = not (r["lo"] <= 0 <= r["hi"])
        arm_tag = "" if r["arm"] in ("env+skill", "skill") else \
            f'<span class="tag">{html.escape(r["arm"])}</span>'
        short = '<span class="tag">short cell</span>' if r["short"] else ""
        p = "—" if r["p"] is None else ("%.3f" % r["p"])
        body.append(
            '<tr><td class="l">{task}</td>'
            '<td class="l"><span class="model">{model}</span>{arm}{short}</td>'
            '<td class="num">{kb}/{nb}</td><td class="num">{ks}/{ns}</td>'
            '<td>{bar}</td>'
            '<td class="num{flat}">{d} pp</td>'
            '<td class="num{flat}">{ci}</td>'
            '<td class="num">{p}</td></tr>'.format(
                task=html.escape(r["task"]), model=html.escape(r["model"]), arm=arm_tag,
                short=short, kb=r["kb"], nb=r["nb"], ks=r["ks"], ns=r["ns"],
                bar=delta_bar(r["delta"], r["lo"], r["hi"]),
                flat="" if separates else " flat", d=fmt_pp(r["delta"]),
                ci=fmt_ci(r["lo"], r["hi"]), p=p))

    n_sep = sum(1 for r in rows if not (r["lo"] <= 0 <= r["hi"]))
    note = (f"{n_sep} of {len(rows)} comparisons have a 95% interval clear of zero. "
            "The rest are shown greyed: this experiment cannot tell those arms apart.")
    return (f'<p class="sub">{note}</p><div class="card scroll"><table>'
            '<thead><tr><th class="l">Task</th><th class="l">Model</th><th>no skill</th>'
            '<th>with skill</th><th>effect</th><th>Δ</th><th>95% CI</th><th>Fisher p</th></tr>'
            f'</thead><tbody>{"".join(body)}</tbody></table></div>')


def build(entries):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Benchmark leaderboard</title>
{STYLE}</head><body><div class="wrap">
  <h1>Neurodesk agent benchmark — leaderboard</h1>
  <p class="sub">Pass rate per model on each task. Click a task for its full report.</p>
  <div class="legend"><span><span class="sw" style="background:var(--base)"></span>no skill</span>
    <span><span class="sw" style="background:var(--skill)"></span>with skill</span>
    <span>pass = valid &amp; verdict ≥ acceptable</span>
    <span><code>k/n</code> is the cell; the bar under it is the 95% Wilson interval</span></div>
  {build_grid(entries)}
  <h2>Does the skill help?</h2>
  {build_effects(entries)}
  <p class="foot">Generated by <code>build_index.py</code> from each task's <code>summary.json</code>.
  Effects are read from <code>skill_effect</code>, not recomputed here.</p>
</div></body></html>"""


def discover(report_dir):
    """Every `summary_<task>.json` in a harness report directory, with its report link."""
    entries = []
    for sj in sorted(glob.glob(os.path.join(report_dir, "summary_*.json"))):
        task = re.sub(r"^summary_|\.json$", "", os.path.basename(sj))
        s = json.load(open(sj))
        href = f"report_{task}.html"
        if not os.path.exists(os.path.join(report_dir, href)):
            href = ""
        entries.append((s.get("task") or task, s, href))
    return entries


def main():
    ap = argparse.ArgumentParser(description="Build the task × model leaderboard index.")
    ap.add_argument("--entry", nargs=3, action="append", metavar=("NAME", "SUMMARY_JSON", "REPORT_HREF"),
                    help="repeatable: one task entry (use '' for no report link)")
    ap.add_argument("--report-dir", metavar="DIR",
                    help="a harness report directory; every summary_<task>.json in it becomes an entry")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    if not a.entry and not a.report_dir:
        ap.error("give --report-dir, or at least one --entry")

    entries = [(name, json.load(open(sj)), href) for name, sj, href in (a.entry or [])]
    if a.report_dir:
        entries += discover(a.report_dir)
    if not entries:
        raise SystemExit("no summary_*.json found in %s" % a.report_dir)

    if a.out.parent:
        a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(build(entries), encoding="utf-8")
    print(f"wrote {a.out} ({a.out.stat().st_size // 1024} KB, {len(entries)} task(s))")
    for name, s, _ in entries:
        expect = expected_reps(s)
        short = [k for k, v in s.get("cells", {}).items() if expect and v.get("n", 0) < expect]
        if short:
            print("  %s: %d cell(s) short of %d runs -> %s"
                  % (name, len(short), expect, ", ".join(sorted(short))))


if __name__ == "__main__":
    main()
