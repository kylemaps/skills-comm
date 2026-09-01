#!/usr/bin/env python3
"""Build the leaderboard: a ranked skill-effect table plus a task × model pass-rate grid.

Reads one `summary.json` per task (the harness output) and emits a single self-contained
`index.html` — sortable, filterable, no server and no external request.

    python build_index.py --report-dir ~/bench/report --out index.html

picks up every `summary_<task>.json` in the directory and links each task to
`report_<task>.html` when that file is there. Adding a task edits no arguments.

    python build_index.py \
        --entry "brain-extraction-7t" path/to/summary.json report_7t.html \
        --out index.html

names tasks explicitly instead. `REPORT_HREF` may be a relative path, a URL, or ''.

The unit is a cell: one task, one model, one arm, repeated N times. So `k/n` is shown beside
every rate (8/10 and 80/100 are both "80%"), each rate carries its 95% Wilson interval, and a
cell short of the task's repeat count is flagged. Effect sizes are read from `skill_effect`
rather than recomputed, so the page cannot drift from what the analysis scripts publish.

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

    Keyed `model` for a single skill arm and `model|arm` once there is more than one; older
    summaries only wrote the bare form. Head-to-head entries (`skillA_vs_skillB`) answer a
    different question and are skipped.
    """
    rows = []
    for key, v in (summary.get("skill_effect") or {}).items():
        model, _, arm = key.partition("|")
        if "_vs_" in arm:
            continue
        rows.append((model, arm or "env+skill", v))
    return rows


def expected_reps(summary):
    """The repeat count this task ran at: the most common n across its cells.

    Inferred rather than configured, so a 5-repeat pilot is not flagged against a stale
    10-repeat constant.
    """
    ns = [v.get("n", 0) for v in summary.get("cells", {}).values() if v.get("n")]
    return max(set(ns), key=ns.count) if ns else 0


def fmt_pp(v, force_dp=False):
    """A percentage-point figure. Trailing .0 is dropped unless the pair needs it.

    Rounding to whole points would print a bound of +0.4 as "+0", which reads as an interval
    that excludes no effect when it does not.
    """
    t = "%+.1f" % v
    if t.endswith(".0") and not force_dp:
        t = t[:-2]
    return "0" if t in ("+0", "-0") else t


def fmt_ci(lo, hi):
    """Both bounds at one precision, so a single interval never reads as two."""
    dp = not (("%+.1f" % lo).endswith(".0") and ("%+.1f" % hi).endswith(".0"))
    return "%s, %s" % (fmt_pp(lo, dp), fmt_pp(hi, dp))


def rate_cell(passes, n, expect=0, bar=True):
    """Pass rate, its denominator, and a 95% Wilson interval drawn underneath.

    bar=False in the effect table, where the difference already carries a CI and a third
    hairline per row is noise rather than information.
    """
    r = passes / n if n else 0
    lo, hi = wilson(passes, n)
    short = bool(expect) and n < expect
    tip = "%d of %d passed. 95%% CI %.0f-%.0f%%." % (passes, n, lo * 100, hi * 100)
    if short:
        tip += " Short cell: %d of %d runs." % (n, expect)
    return (
        '<span class="rate{sh}" title="{tip}" style="--v:{v:.3f}">'
        '<span class="num">{p:.0f}%</span>'
        '<span class="kn">{k}/{n}{dag}</span>'
        '{ci}'
        "</span>"
    ).format(
        # The floor is in proportion units, like lo and hi. A floor of 0.8 here would be
        # 80% of the track, which drew every interval the same width.
        ci=("" if not bar else
            '<span class="ci"><i style="left:%.1f%%;width:%.1f%%"></i></span>'
            % (lo * 100, max(hi - lo, 0.008) * 100)),
        sh=" short" if short else "", tip=html.escape(tip), v=r, p=r * 100,
        k=passes, n=n, dag="†" if short else "")


def delta_bar(delta, lo, hi):
    """A difference in percentage points on a track centred at zero, CI as a whisker.

    -100..+100 pp maps across the track. When the whisker crosses the centre line the
    experiment cannot separate the arms.
    """
    def x(pp):
        return min(100.0, max(0.0, 50.0 + pp / 2.0))

    b0, b1 = sorted((x(0), x(delta)))
    w0, w1 = sorted((x(lo), x(hi)))
    cls = "pos" if delta > 0 else ("neg" if delta < 0 else "nil")
    return ('<span class="dbar"><i class="axis"></i>'
            '<i class="whisk" style="left:{w0:.1f}%;width:{ww:.1f}%"></i>'
            '<i class="fill {cls}" style="left:{b0:.1f}%;width:{bw:.1f}%"></i></span>'
            ).format(w0=w0, ww=max(w1 - w0, 0.8), cls=cls, b0=b0, bw=max(b1 - b0, 0.8))


STYLE = """<style>
:root{--bg:#fff;--head:#f7f7f8;--ink:#1a1a1c;--muted:#6c6c76;--faint:#9a9aa4;--line:#e6e6ea;
--accent:#3b6fd4;--good:#20a56a;--bad:#d14b3c;--track:#e8e8ec;--sel:#f2f6fd;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#0d0d0f;--head:#161619;--ink:#e7e7ea;--muted:#9494a0;
--faint:#6a6a76;--line:#26262c;--accent:#6f9bec;--good:#35b97f;--bad:#e0685a;--track:#2a2a31;--sel:#15202f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:14px;
-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:1.35rem;font-weight:650;margin:0 0 6px;letter-spacing:-.01em}
.meta{color:var(--muted);font-size:.82rem;margin:0;font-variant-numeric:tabular-nums}
.meta b{color:var(--ink);font-weight:600}
.key{color:var(--faint);font-size:.75rem;margin:10px 0 0;display:flex;gap:18px;flex-wrap:wrap;align-items:center}
.key code{font-family:var(--mono);color:var(--muted)}
.key .sw{display:inline-block;width:16px;height:3px;border-radius:2px;background:var(--muted);
opacity:.6;vertical-align:middle;margin-right:5px}
.bar{display:flex;align-items:center;gap:12px;margin:30px 0 10px}
h2{font-size:.74rem;font-weight:650;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin:0}
input[type=search]{margin-left:auto;width:230px;padding:6px 10px;font:inherit;font-size:.82rem;
background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:7px;outline:none}
input[type=search]:focus{border-color:var(--accent)}
.card{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:var(--bg)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.83rem}
thead th{background:var(--head);color:var(--muted);font-weight:600;font-size:.72rem;
text-transform:uppercase;letter-spacing:.04em;text-align:right;padding:9px 12px;
border-bottom:1px solid var(--line);white-space:nowrap}
th.l{text-align:left}th.c{text-align:center}
th[data-s]{cursor:pointer;user-select:none}
th[data-s]:hover{color:var(--ink)}
th[data-s]::after{content:"";margin-left:5px}
th[data-s].asc::after{content:"\\2191"}th[data-s].desc::after{content:"\\2193"}
tbody td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:right;vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--sel)}
td.l{text-align:left}td.c{text-align:center}
td.rank{color:var(--faint);font-family:var(--mono);font-size:.76rem;width:34px}
td.name{white-space:nowrap}
td.name a{color:var(--ink);text-decoration:none}td.name a:hover{color:var(--accent);text-decoration:underline}
.model{font-family:var(--mono);font-size:.8rem}
.arm{font-family:var(--mono);font-size:.68rem;color:var(--muted);border:1px solid var(--line);
border-radius:4px;padding:1px 4px;margin-left:6px;white-space:nowrap}
.n{font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
.dim{color:var(--faint)}
.na{color:var(--faint)}
.rate{display:inline-grid;grid-template-columns:auto auto;gap:1px 6px;justify-items:end;
align-items:baseline;min-width:74px;padding:2px 5px;border-radius:5px;
background:color-mix(in srgb,var(--good) calc(var(--v)*16%),transparent)}
.rate .num{font:600 12.5px/1.25 var(--sans);font-variant-numeric:tabular-nums}
.rate .kn{font:10.5px/1.25 var(--mono);color:var(--muted);font-variant-numeric:tabular-nums}
.rate.short .kn{color:var(--bad)}
.rate .ci{grid-column:1/-1;position:relative;width:100%;height:2px;margin-top:3px;
border-radius:1px;background:var(--track)}
.rate .ci i{position:absolute;top:0;height:2px;border-radius:1px;background:var(--muted);opacity:.6}
.cell{display:flex;flex-direction:column;gap:6px;align-items:flex-end}
.crow{display:flex;align-items:center;justify-content:flex-end;gap:8px;width:100%}
.clab{font:10px/1 var(--mono);color:var(--muted);white-space:nowrap}
.clab i{display:inline-block;width:5px;height:5px;border-radius:1px;margin-right:5px;vertical-align:middle}
.clab i.base{background:var(--faint)}.clab i.skill{background:var(--good)}
.dbar{position:relative;display:block;width:130px;height:11px;margin:0 auto}
.dbar i{position:absolute}
.dbar .axis{left:50%;top:0;width:1px;height:11px;background:var(--line)}
.dbar .whisk{top:5px;height:1px;background:var(--faint)}
.dbar .fill{top:2.5px;height:6px;border-radius:1.5px}
.dbar .fill.pos{background:var(--good)}.dbar .fill.neg{background:var(--bad)}
.dbar .fill.nil{background:var(--faint)}
tr.ns td{color:var(--faint)}tr.ns .dbar .fill{opacity:.45}tr.ns .model{color:var(--muted)}
.empty{padding:26px 14px;color:var(--muted);font-size:.83rem;text-align:center}
.foot{color:var(--faint);font-size:.75rem;margin-top:26px}
.foot code{font-family:var(--mono)}
</style>"""

SCRIPT = """<script>
(function () {
  function val(td) {
    var v = td.getAttribute('data-v');
    if (v === null) return td.textContent.trim().toLowerCase();
    var f = parseFloat(v);
    return isNaN(f) ? v.toLowerCase() : f;
  }
  document.querySelectorAll('table').forEach(function (tbl) {
    tbl.querySelectorAll('th[data-s]').forEach(function (th) {
      th.addEventListener('click', function () {
        var i = Array.prototype.indexOf.call(th.parentNode.children, th);
        var desc = !th.classList.contains('desc');
        tbl.querySelectorAll('th[data-s]').forEach(function (o) {
          o.classList.remove('asc', 'desc');
        });
        th.classList.add(desc ? 'desc' : 'asc');
        var body = tbl.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = val(a.cells[i]), y = val(b.cells[i]);
          if (x < y) return desc ? 1 : -1;
          if (x > y) return desc ? -1 : 1;
          return 0;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      });
    });
  });
  document.querySelectorAll('input[data-filter]').forEach(function (box) {
    var tbl = document.getElementById(box.getAttribute('data-filter'));
    box.addEventListener('input', function () {
      var q = box.value.trim().toLowerCase();
      Array.prototype.forEach.call(tbl.tBodies[0].rows, function (r) {
        r.style.display = !q || r.textContent.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
      });
    });
  });
})();
</script>"""


def build_effects(entries):
    """Every skill-versus-baseline comparison, ranked by effect size."""
    rows = []
    for name, s, href in entries:
        expect = expected_reps(s)
        for model, arm, e in effect_rows(s):
            ci = e.get("ci95_pp") or [0, 0]
            rows.append({
                "task": name, "href": href, "model": model, "arm": arm,
                "kb": e.get("env_only_pass", 0), "nb": e.get("env_only_n", 0),
                "ks": e.get("env_skill_pass", 0), "ns": e.get("env_skill_n", 0),
                "delta": e.get("delta_pp", 0.0), "lo": ci[0], "hi": ci[1],
                "p": e.get("fisher_p"), "expect": expect,
                "short": bool(expect) and min(e.get("env_only_n", 0),
                                              e.get("env_skill_n", 0)) < expect,
            })
    if not rows:
        return '<div class="card"><p class="empty">No task has both arms yet.</p></div>', 0, 0

    rows.sort(key=lambda r: (-r["delta"], r["task"], r["model"]))
    body = []
    for i, r in enumerate(rows, 1):
        # A CI spanning zero means the arms are not separated. Marked on the row rather
        # than explained in a caption.
        sep = not (r["lo"] <= 0 <= r["hi"])
        arm = "" if r["arm"] in ("env+skill", "skill") else \
            '<span class="arm">%s</span>' % html.escape(r["arm"])
        task = ('<a href="%s">%s</a>' % (html.escape(r["href"]), html.escape(r["task"]))
                if r["href"] else html.escape(r["task"]))
        p = '<span class="dim">&mdash;</span>' if r["p"] is None else ("%.3f" % r["p"])
        body.append(
            '<tr class="{ns}"><td class="rank">{i}</td>'
            '<td class="l name" data-v="{tsort}">{task}</td>'
            '<td class="l"><span class="model">{model}</span>{arm}</td>'
            '<td data-v="{rb:.4f}">{cb}</td><td data-v="{rs:.4f}">{cs}</td>'
            '<td class="c" data-v="{d:.4f}">{bar}</td>'
            '<td class="n" data-v="{d:.4f}">{dtxt}</td>'
            '<td class="n dim">{ci}</td>'
            '<td class="n" data-v="{psort}">{p}</td></tr>'.format(
                ns="ns" if not sep else "", i=i, tsort=html.escape(r["task"]), task=task,
                model=html.escape(r["model"]), arm=arm,
                rb=r["kb"] / r["nb"] if r["nb"] else 0,
                cb=rate_cell(r["kb"], r["nb"], r["expect"], bar=False),
                rs=r["ks"] / r["ns"] if r["ns"] else 0,
                cs=rate_cell(r["ks"], r["ns"], r["expect"], bar=False),
                d=r["delta"], bar=delta_bar(r["delta"], r["lo"], r["hi"]),
                dtxt=fmt_pp(r["delta"]), ci=fmt_ci(r["lo"], r["hi"]),
                psort=1.0 if r["p"] is None else r["p"], p=p))

    n_sep = sum(1 for r in rows if not (r["lo"] <= 0 <= r["hi"]))
    table = (
        '<div class="bar"><h2>Skill effect</h2>'
        '<input type="search" data-filter="fx" placeholder="Filter task or model" '
        'aria-label="Filter"></div>'
        '<div class="card scroll"><table id="fx"><thead><tr>'
        '<th></th><th class="l" data-s>Task</th><th class="l" data-s>Model</th>'
        '<th data-s>No skill</th><th data-s>With skill</th>'
        '<th class="c" data-s>&minus;100 &nbsp;0&nbsp; +100 pp</th>'
        '<th data-s>&Delta; pp</th><th>95%% CI</th><th data-s>Fisher p</th>'
        '</tr></thead><tbody>%s</tbody></table></div>' % "".join(body))
    return table, n_sep, len(rows)


def build_grid(entries):
    models = []
    for _, s, _ in entries:
        for m in cell_matrix(s):
            if m not in models:
                models.append(m)

    head = "".join('<th data-s><span class="model">%s</span></th>' % html.escape(m)
                   for m in models)
    rows = []
    for name, s, href in entries:
        mat = cell_matrix(s)
        expect = expected_reps(s)
        tds = []
        for m in models:
            arms = mat.get(m)
            if not arms:
                tds.append('<td data-v="-1"><span class="na">&mdash;</span></td>')
                continue
            lines, sort_v = [], 0.0
            for arm in sorted(arms, key=lambda a: (is_skill_arm(a), a)):
                p, n = arms[arm]
                lab = ARM_LABEL.get(arm, arm)
                skill = is_skill_arm(arm)
                if skill and not sort_v:
                    sort_v = p / n if n else 0.0
                lines.append(
                    '<span class="crow"><span class="clab"><i class="%s"></i>%s</span>%s</span>'
                    % ("skill" if skill else "base", html.escape(lab),
                       rate_cell(p, n, expect)))
            tds.append('<td data-v="%.4f"><span class="cell">%s</span></td>'
                       % (sort_v, "".join(lines)))
        label = html.escape(name)
        tcell = '<a href="%s">%s</a>' % (html.escape(href), label) if href else label
        rows.append('<tr><td class="l name" data-v="%s">%s</td>%s</tr>'
                    % (label, tcell, "".join(tds)))

    return ('<div class="bar"><h2>Pass rate</h2></div>'
            '<div class="card scroll"><table><thead><tr><th class="l" data-s>Task</th>%s'
            '</tr></thead><tbody>%s</tbody></table></div>' % (head, "".join(rows)))


def build(entries):
    effects, n_sep, n_cmp = build_effects(entries)

    models, runs, reps = set(), 0, set()
    for _, s, _ in entries:
        for key, v in s.get("cells", {}).items():
            models.add(key.partition("|")[0])
            runs += v.get("n", 0)
        if expected_reps(s):
            reps.add(expected_reps(s))

    def plural(n, w):
        return "%d %s%s" % (n, w, "" if n == 1 else "s")

    meta = " &middot; ".join(x for x in [
        plural(len(entries), "task"),
        plural(len(models), "model"),
        "<b>%d</b> runs" % runs,
        ("%s per cell" % "/".join(str(r) for r in sorted(reps))) if reps else "",
        ("<b>%d/%d</b> effects clear of zero" % (n_sep, n_cmp)) if n_cmp else "",
    ] if x)

    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neurodesk agent benchmark</title>
%s</head><body><div class="wrap">
  <h1>Neurodesk agent benchmark</h1>
  <p class="meta">%s</p>
  <p class="key">
    <span><span class="sw"></span>95%% Wilson CI</span>
    <span><code>k/n</code> passed / runs</span>
    <span><code>&dagger;</code> short cell</span>
    <span>greyed row: CI includes 0</span>
  </p>
  %s
  %s
  <p class="foot">Pass = valid output and verdict at or above acceptable.
  Built by <code>build_index.py</code> from each task's <code>summary.json</code>;
  effects read from <code>skill_effect</code>.</p>
</div>%s</body></html>""" % (STYLE, meta, effects, build_grid(entries), SCRIPT)


def discover(report_dir):
    """Every `summary_<task>.json` in a harness report directory, with its report link."""
    entries = []
    for sj in sorted(glob.glob(os.path.join(report_dir, "summary_*.json"))):
        task = re.sub(r"^summary_|\.json$", "", os.path.basename(sj))
        with open(sj, encoding="utf-8") as fh:
            s = json.load(fh)
        href = "report_%s.html" % task
        # Zero bytes counts as absent. build_report truncates its output before it writes,
        # so a crash leaves an empty file, and linking that gives a row that looks
        # clickable and opens nothing.
        full = os.path.join(report_dir, href)
        if not (os.path.exists(full) and os.path.getsize(full) > 0):
            href = ""
        entries.append((s.get("task") or task, s, href))
    return entries


def main():
    ap = argparse.ArgumentParser(description="Build the benchmark leaderboard index.")
    ap.add_argument("--entry", nargs=3, action="append",
                    metavar=("NAME", "SUMMARY_JSON", "REPORT_HREF"),
                    help="repeatable: one task entry (use '' for no report link)")
    ap.add_argument("--report-dir", metavar="DIR",
                    help="a harness report directory; every summary_<task>.json becomes an entry")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    if not a.entry and not a.report_dir:
        ap.error("give --report-dir, or at least one --entry")

    entries = []
    for name, sj, href in (a.entry or []):
        with open(sj, encoding="utf-8") as fh:
            entries.append((name, json.load(fh), href))
    if a.report_dir:
        entries += discover(a.report_dir)
    if not entries:
        raise SystemExit("no summary_*.json found in %s" % a.report_dir)

    if a.out.parent:
        a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(build(entries), encoding="utf-8")
    print("wrote %s (%d KB, %d task(s))"
          % (a.out, a.out.stat().st_size // 1024, len(entries)))
    for name, s, _ in entries:
        expect = expected_reps(s)
        short = [k for k, v in s.get("cells", {}).items() if expect and v.get("n", 0) < expect]
        if short:
            print("  %s: %d cell(s) short of %d runs -> %s"
                  % (name, len(short), expect, ", ".join(sorted(short))))


if __name__ == "__main__":
    main()
