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

Layout notes, because they are load-bearing rather than decorative:

* Every effect row is two lines — headline value on top, its supporting detail beneath.
  That collapses four numeric columns (Δ, CI, p, verdict) into two without hiding a
  single figure, which is what lets the table survive 20 tasks × 10 models.
* The only hue in the effect table is the diverging blue/red on the effect bar, so the
  eye lands on the one column that answers the question. Red/green is avoided outright:
  it is the worst pair for the ~8% of male readers with a red-green deficiency.
* Meaning is never carried by colour alone. The verdict is a word, a shape and a hue;
  a short cell is a dagger as well as a colour.
* Every text colour on this page clears WCAG AA (4.5:1) against the surface it sits on,
  in both modes. The muted greys are 5.3:1, not the 2.8:1 they used to be — and it was
  the "unclear" rows, the ones a reader should scrutinise hardest, that were faintest.

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
    """Both bounds at one precision, so a single interval never reads as two.

    An absent interval prints as a dash. It must not borrow a number from anywhere.
    """
    if lo is None or hi is None:
        return '<span class="dim">not computed</span>'
    dp = not (("%+.1f" % lo).endswith(".0") and ("%+.1f" % hi).endswith(".0"))
    return "%s, %s" % (fmt_pp(lo, dp), fmt_pp(hi, dp))


def usable_ci(ci):
    """A pair of finite bounds, or None. Never a substitute pair.

    Defaulting a missing interval to [0, 0] renders a zero-width whisker on the centre
    line next to a real effect size, which reads as an extraordinarily precise result
    rather than an absent one. A NaN bound is worse: every comparison against NaN is
    False, so `not (lo <= 0 <= hi)` reported an uncomputable interval as excluding zero,
    i.e. as a positive finding. summarize.newcombe returns NaN when an arm has no runs.
    """
    if not isinstance(ci, (list, tuple)) or len(ci) != 2:
        return None
    try:
        lo, hi = float(ci[0]), float(ci[1])
    except (TypeError, ValueError):
        return None
    if math.isnan(lo) or math.isnan(hi) or math.isinf(lo) or math.isinf(hi):
        return None
    return (lo, hi) if lo <= hi else (hi, lo)


def separates(lo, hi):
    """Does this interval exclude no-effect? Absent means no, never yes."""
    if lo is None or hi is None:
        return False
    return lo > 0 or hi < 0


def agreement(lo, hi, p, alpha=0.05):
    """How the interval and the test line up: "clear", "split" or "unclear".

    Newcombe on the difference of proportions and Fisher on the 2x2 are different
    tests and can disagree. That is not a bug in either, and it is live in this data:
    qwen3 on the 7T task has CI [+3.8, +68.7] with p=0.087. Marking such a row
    significant because the interval excludes zero, or unmarked because p is above
    alpha, both pick a winner without saying so.

    "split" says the result sits on the boundary of what this many runs can resolve,
    which is the true statement and the useful one. A missing p is not a disagreement,
    so an interval alone can still read "clear".
    """
    sep = separates(lo, hi)
    if p is None:
        return "clear" if sep else "unclear"
    sig = p < alpha
    if sep and sig:
        return "clear"
    if sep or sig:
        return "split"
    return "unclear"


AGREEMENT_NOTE = {
    "clear": "Interval excludes no effect and Fisher agrees.",
    "split": "Interval and Fisher disagree. On the boundary of what this many runs resolve.",
    "unclear": "This experiment cannot separate the arms.",
}


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
    experiment cannot separate the arms. The whisker is drawn with end caps, so a wide
    interval reads as a measured range rather than as a smudge behind the bar.
    """
    def x(pp):
        return min(100.0, max(0.0, 50.0 + pp / 2.0))

    b0, b1 = sorted((x(0), x(delta)))
    cls = "pos" if delta > 0 else ("neg" if delta < 0 else "nil")
    # No interval, no whisker. Drawing a stub at the centre line would claim a
    # precision that was never computed.
    whisk = ""
    if lo is not None and hi is not None:
        w0, w1 = sorted((x(lo), x(hi)))
        whisk = ('<i class="whisk" style="left:%.1f%%;width:%.1f%%"></i>'
                 % (w0, max(w1 - w0, 0.8)))
    return ('<span class="dbar"><i class="axis"></i>{whisk}'
            '<i class="fill {cls}" style="left:{b0:.1f}%;width:{bw:.1f}%"></i></span>'
            ).format(whisk=whisk, cls=cls, b0=b0, bw=max(b1 - b0, 0.8))


STYLE = """<style>
:root{color-scheme:light dark;
--surface:#fcfcfb;--plane:#f4f4f1;--head:#f7f7f5;--hover:rgba(11,11,11,.032);
--rule:rgba(11,11,11,.10);--rule2:rgba(11,11,11,.17);
--ink:#0b0b0b;--ink2:#4a4945;--ink3:#6b6a64;
--link:#1c5cab;--pos:#2a78d6;--neg:#d03b3b;--nil:#8b8a84;
--track:#e8e7e3;--warn:#d06a00;--danger:#c8342c;
--shadow:0 1px 2px rgba(11,11,11,.05),0 8px 20px -14px rgba(11,11,11,.30);
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
@media(prefers-color-scheme:dark){:root{
--surface:#161617;--plane:#0b0b0c;--head:#1d1d1e;--hover:rgba(255,255,255,.048);
--rule:rgba(255,255,255,.11);--rule2:rgba(255,255,255,.21);
--ink:#f3f3f0;--ink2:#b6b5ae;--ink3:#92918b;
--link:#7fb0f0;--pos:#4f93ea;--neg:#e6564f;--nil:#8c8b85;
--track:#2c2c2d;--warn:#e0a83a;--danger:#ef7a72;
--shadow:0 1px 2px rgba(0,0,0,.55),0 10px 26px -16px rgba(0,0,0,.9)}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--plane);color:var(--ink);font-family:var(--sans);
font-size:13.5px;line-height:1.45;-webkit-font-smoothing:antialiased;
text-rendering:optimizeLegibility}
.wrap{max-width:1250px;margin:0 auto;padding:56px 28px 88px}

/* masthead: title, then the sweep in figures, then the vocabulary */
h1{font-size:1.55rem;font-weight:640;margin:0;letter-spacing:-.021em;line-height:1.15}
.stats{display:flex;flex-wrap:wrap;margin:22px 0 0;border:1px solid var(--rule);
border-radius:14px;background:var(--surface);box-shadow:var(--shadow);overflow:hidden}
.stat{display:flex;flex-direction:column;gap:3px;padding:14px 24px;min-width:106px;
border-left:1px solid var(--rule)}
.stat:first-child{border-left:0}
.stat b{font-size:1.34rem;font-weight:620;letter-spacing:-.022em;line-height:1.05;
font-variant-numeric:tabular-nums}
.stat span{font-size:.665rem;font-weight:600;text-transform:uppercase;
letter-spacing:.085em;color:var(--ink3)}
.key{display:flex;flex-wrap:wrap;gap:7px 22px;margin:16px 3px 0;font-size:.735rem;
color:var(--ink2);align-items:center}
.key>span{display:inline-flex;align-items:center;gap:6px}
.key code{font-family:var(--mono);font-size:.92em;color:var(--ink)}
.key .swatch{display:inline-block;width:18px;height:3px;border-radius:2px;
background:var(--ink3);opacity:.85}

/* section chrome */
.bar{display:flex;align-items:center;gap:14px;margin:48px 0 12px;flex-wrap:wrap}
h2{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
color:var(--ink2);margin:0}
.count{font-size:.7rem;color:var(--ink3);font-variant-numeric:tabular-nums}
.tools{margin-left:auto;display:flex;align-items:center;gap:9px}
.seg{display:inline-flex;border:1px solid var(--rule);border-radius:8px;overflow:hidden}
.seg button{font:inherit;font-size:.695rem;font-weight:600;letter-spacing:.02em;
padding:5px 11px;background:var(--surface);color:var(--ink2);border:0;
border-left:1px solid var(--rule);cursor:pointer}
.seg button:first-child{border-left:0}
.seg button:hover{color:var(--ink);background:var(--hover)}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--surface)}
input[type=search]{width:214px;padding:5px 11px;font:inherit;font-size:.75rem;
background:var(--surface);color:var(--ink);border:1px solid var(--rule);
border-radius:8px;outline:none}
input[type=search]::placeholder{color:var(--ink3)}
input[type=search]:focus{border-color:var(--link)}
.card{border:1px solid var(--rule);border-radius:14px;background:var(--surface);
box-shadow:var(--shadow)}
.scroll{overflow:auto;border-radius:13px;max-height:78vh}
@media(max-width:900px){.tall{overflow:auto;border-radius:13px}}

/* tables */
table{width:100%;border-collapse:separate;border-spacing:0;font-size:.815rem}
thead th{position:sticky;top:0;z-index:3;background:var(--head);color:var(--ink3);
font-weight:650;font-size:.655rem;text-transform:uppercase;letter-spacing:.07em;
text-align:right;padding:10px 14px;border-bottom:1px solid var(--rule2);
white-space:nowrap;vertical-align:bottom}
thead th:first-child{border-top-left-radius:13px}
thead th:last-child{border-top-right-radius:13px}
th.l{text-align:left}th.c{text-align:center}
th .model{text-transform:none;font-size:.735rem;font-weight:600;color:var(--ink2)}
th .sub{display:block;font-size:.95em;font-weight:500;letter-spacing:.045em;
text-transform:none;color:var(--ink3);margin-top:3px}
th[data-s]{cursor:pointer;user-select:none}
th[data-s]:hover{color:var(--ink)}
th[data-s]::after{content:"";margin-left:5px;opacity:.6}
th[data-s].asc::after{content:"\\2191"}th[data-s].desc::after{content:"\\2193"}
tbody td{padding:10px 14px;border-bottom:1px solid var(--rule);text-align:right;
vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:last-child td:first-child{border-bottom-left-radius:13px}
tbody tr:last-child td:last-child{border-bottom-right-radius:13px}
tbody tr:hover td{background:var(--hover)}
td.l{text-align:left}td.c{text-align:center}
td.rank{color:var(--ink3);font-family:var(--mono);font-size:.72rem;width:42px;
font-variant-numeric:tabular-nums}
td.name{white-space:nowrap;font-weight:500;letter-spacing:-.006em}
td.name a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--rule2);
padding-bottom:1px}
td.name a:hover{color:var(--link);border-bottom-color:var(--link)}
.model{font-family:var(--mono);font-size:.775rem;letter-spacing:-.01em;white-space:nowrap}
.arm{display:block;font-family:var(--mono);font-size:.635rem;color:var(--ink2);
margin-top:3px;white-space:nowrap}
.n{font-variant-numeric:tabular-nums;white-space:nowrap}
.term{position:relative;white-space:nowrap}
.term .q{display:inline-block;width:12px;height:12px;line-height:12px;margin-left:4px;
border:1px solid currentColor;border-radius:50%;font-size:8.5px;font-style:normal;
text-align:center;opacity:.5;vertical-align:1px;cursor:help}
.term:hover .q,.term:focus .q{opacity:1}
.term:focus{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
.dim{color:var(--ink3)}
.na{color:var(--ink3)}
.stick{position:sticky;left:0;z-index:2;background:var(--surface);box-shadow:1px 0 0 var(--rule)}
table.grid tbody td{vertical-align:top}
table.grid tbody td.name{vertical-align:middle}
table.grid tbody td{min-width:126px}
table.grid tbody td.name{min-width:210px}
thead th.stick{z-index:4;background:var(--head)}
tbody tr:hover td.stick{background:var(--surface)}

/* a rate: value, denominator, and — in the grid — its Wilson interval */
.rate{display:inline-grid;grid-template-columns:1fr;justify-items:end;gap:1px;
min-width:54px}
.rate .num{font-size:.93rem;font-weight:600;font-variant-numeric:tabular-nums;
letter-spacing:-.016em;line-height:1.2}
.rate .kn{font:10.5px/1.3 var(--mono);color:var(--ink3);font-variant-numeric:tabular-nums}
.rate.short .kn{color:var(--danger)}
.rate .ci{position:relative;width:100%;min-width:54px;height:3px;margin-top:5px;
border-radius:2px;background:var(--track)}
.rate .ci i{position:absolute;top:0;height:3px;border-radius:2px;background:var(--ink3)}

/* grid cell: one stacked arm per line, label left, figures right */
.cell{display:flex;flex-direction:column;gap:10px;align-items:stretch}
.crow{display:grid;grid-template-columns:1fr auto;gap:10px;align-items:start}
.clab{font:10px/1.4 var(--mono);color:var(--ink2);white-space:nowrap;text-align:left;
padding-top:2px}
.clab i{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}
.clab i.base{background:transparent;box-shadow:inset 0 0 0 1.5px var(--nil)}
.clab i.skill{background:var(--pos)}

/* the effect bar: zero-centred, CI as a capped whisker */
.dbar{position:relative;display:block;width:174px;height:16px;margin:0 auto}
.dbar i{position:absolute}
.dbar .axis{left:50%;top:0;width:1px;height:16px;background:var(--rule2)}
.dbar .whisk{top:7.5px;height:1px;background:var(--ink3)}
.dbar .whisk::before,.dbar .whisk::after{content:"";position:absolute;top:-3px;width:1px;
height:7px;background:var(--ink3)}
.dbar .whisk::before{left:0}
.dbar .whisk::after{right:0}
.dbar .fill{top:4px;height:8px;border-radius:2px;box-shadow:0 0 0 1.5px var(--surface)}
.dbar .fill.pos{background:var(--pos)}
.dbar .fill.neg{background:var(--neg)}
.dbar .fill.nil{background:var(--nil)}
.ax{display:flex;justify-content:space-between;width:174px;margin:5px auto 0;
font:9.5px/1 var(--mono);font-weight:500;letter-spacing:0;color:var(--ink3);
text-transform:none}
.delta{font-size:.93rem;font-weight:600;font-variant-numeric:tabular-nums;
letter-spacing:-.016em;line-height:1.2}
.ci95{display:block;font:10.5px/1.35 var(--mono);color:var(--ink2);
font-variant-numeric:tabular-nums;margin-top:2px}

/* verdict: a word, a shape and a hue — readable in grey and colour-blind */
.vd{display:inline-flex;align-items:center;gap:6px;font-size:.755rem;font-weight:620;
color:var(--ink)}
.vd i{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.vd.clear i{background:var(--pos)}
.vd.split i{background:conic-gradient(var(--warn) 180deg,transparent 0);
box-shadow:inset 0 0 0 1.5px var(--warn)}
.vd.unclear{color:var(--ink2);font-weight:560}
.vd.unclear i{background:transparent;box-shadow:inset 0 0 0 1.5px var(--nil)}
.pv{display:block;font:10.5px/1.35 var(--mono);color:var(--ink2);
font-variant-numeric:tabular-nums;margin-top:3px}
tr.unclear .delta{color:var(--ink2);font-weight:560}
tr.unclear .dbar .fill{opacity:.42}
tr.split .dbar .fill{opacity:.72}

figure{margin:0}
.charts{padding:22px 26px 18px}
.chart+.chart{margin-top:0}
.chart figcaption{font-size:.7rem;font-weight:700;text-transform:uppercase;
letter-spacing:.09em;color:var(--ink2);margin:0 0 20px}
.chart figcaption .unit{font-weight:500;text-transform:none;letter-spacing:0;
color:var(--ink3);margin-left:8px}
.plot{display:flex;flex-direction:column;gap:2px}
.drow{display:grid;grid-template-columns:132px 1fr 128px;align-items:center;gap:16px;
padding:9px 0;border-bottom:1px solid var(--rule)}
.drow:last-child{border-bottom:0}
.dm{font-family:var(--mono);font-size:.775rem;color:var(--ink);white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.dtrack{position:relative;height:14px;background:linear-gradient(var(--track),var(--track))
center/100% 1px no-repeat;border-radius:2px}
.dtrack i{position:absolute}
.dtrack .link{top:6px;height:2px;background:var(--ink3);opacity:.5;border-radius:1px}
.dtrack .dot{top:3px;width:9px;height:9px;margin-left:-4.5px;border-radius:50%;
box-shadow:0 0 0 2px var(--surface)}
.dot.a0{background:transparent;box-shadow:0 0 0 2px var(--surface),inset 0 0 0 2px var(--nil)}
.dot.a1{background:var(--pos)}.dot.a2{background:var(--warn)}.dot.a3{background:var(--neg)}
.dvs{display:flex;gap:10px;justify-content:flex-end;font-variant-numeric:tabular-nums}
.dv{font:11px/1.3 var(--mono);color:var(--ink2)}
.dv.a1{color:var(--pos);font-weight:600}.dv.a2{color:var(--warn);font-weight:600}
.dv.a3{color:var(--neg);font-weight:600}
.lgs{display:flex;gap:20px;flex-wrap:wrap;margin-top:16px;font-size:.72rem;color:var(--ink2)}
.lg{display:inline-flex;align-items:center;gap:7px}
.sw{width:10px;height:10px;border-radius:50%;display:inline-block}
.sw.a0{background:transparent;box-shadow:inset 0 0 0 2px var(--nil)}
.sw.a1{background:var(--pos)}.sw.a2{background:var(--warn)}.sw.a3{background:var(--neg)}
@media(max-width:720px){.drow{grid-template-columns:96px 1fr 104px;gap:10px}}
@media print{
:root{--surface:#fff;--plane:#fff;--head:#fff;--hover:transparent;
--ink:#000;--ink2:#333;--ink3:#555;--rule:#bbb;--rule2:#888}
*{-webkit-print-color-adjust:exact;print-color-adjust:exact}
.tools,.seg,input[type=search],select{display:none}
.wrap{max-width:none;padding:0}
.card,.scroll,.tall{max-height:none;overflow:visible;box-shadow:none;break-inside:auto}
thead th{position:static}
.stick{position:static;box-shadow:none}
tr{break-inside:avoid}
.chart[hidden]{display:none}
}
.empty{padding:34px 16px;color:var(--ink2);font-size:.82rem;text-align:center}
.note{color:var(--ink2)}
.note b{color:var(--ink);font-weight:640;font-variant-numeric:tabular-nums}
.foot{color:var(--ink2);font-size:.735rem;margin-top:36px;max-width:76ch;line-height:1.6}
.foot code{font-family:var(--mono);color:var(--ink)}
@media(max-width:720px){.wrap{padding:34px 16px 64px}
.tools{margin-left:0;width:100%}input[type=search]{flex:1;width:auto}}
</style>"""

SCRIPT = """<script>
(function () {
  function val(td) {
    var v = td.getAttribute('data-v');
    if (v === null) return td.textContent.trim().toLowerCase();
    var f = parseFloat(v);
    return isNaN(f) ? v.toLowerCase() : f;
  }
  // A comparison row may be followed by its detail row. Everything below moves,
  // hides and counts the pair as one unit -- sorting them independently would
  // silently attach a panel of runtimes to the wrong comparison.
  function detailOf(tr) {
    var n = tr.nextElementSibling;
    return (n && n.classList.contains('det')) ? n : null;
  }
  function comparisons(tbl) {
    return Array.prototype.filter.call(tbl.tBodies[0].rows, function (r) {
      return !r.classList.contains('det');
    });
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
        var rows = comparisons(tbl);
        var pairs = rows.map(function (r) { return [r, detailOf(r)]; });
        pairs.sort(function (a, b) {
          var x = val(a[0].cells[i]), y = val(b[0].cells[i]);
          if (x < y) return desc ? 1 : -1;
          if (x > y) return desc ? -1 : 1;
          return 0;
        });
        pairs.forEach(function (p) {
          body.appendChild(p[0]);
          if (p[1]) body.appendChild(p[1]);
        });
      });
    });
  });

  // Click or keyboard opens the detail panel: runtime, tokens, uptake, tools, dates.
  document.querySelectorAll('tr.exp').forEach(function (tr) {
    var det = detailOf(tr);
    if (!det) { tr.removeAttribute('role'); tr.removeAttribute('tabindex'); return; }
    function toggle() {
      var open = det.hasAttribute('hidden');
      if (open) { det.removeAttribute('hidden'); } else { det.setAttribute('hidden', ''); }
      tr.setAttribute('aria-expanded', open ? 'true' : 'false');
      tr.classList.toggle('open', open);
    }
    tr.addEventListener('click', function (e) {
      if (e.target.closest('a')) return;   // task links still navigate
      toggle();
    });
    tr.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });

  // Text box, two dropdowns and the verdict facets share one pass. The count is
  // always rendered, so a filtered table can never be mistaken for the whole sweep.
  // Exactly one chart visible. Every combination is already in the DOM, rendered
  // server-side, so switching never recomputes a number in the browser.
  (function () {
    var ct = document.getElementById('ct'), cm = document.getElementById('cm');
    if (!ct || !cm) return;
    var figs = document.querySelectorAll('figure.chart');
    function show() {
      Array.prototype.forEach.call(figs, function (f) {
        var hit = f.getAttribute('data-task') === ct.value &&
                  f.getAttribute('data-metric') === cm.value;
        if (hit) { f.removeAttribute('hidden'); } else { f.setAttribute('hidden', ''); }
      });
    }
    ct.addEventListener('change', show);
    cm.addEventListener('change', show);
    show();
  })();

  document.querySelectorAll('input[data-filter]').forEach(function (box) {
    var id = box.getAttribute('data-filter');
    var tbl = document.getElementById(id);
    var out = document.querySelector('[data-count="' + id + '"]');
    var chips = document.querySelectorAll('[data-vf][data-for="' + id + '"]');
    var sels = document.querySelectorAll('select[data-for="' + id + '"]');
    var pick = 'all';
    function apply() {
      var q = box.value.trim().toLowerCase();
      var rows = comparisons(tbl), shown = 0;
      rows.forEach(function (r) {
        var keep = !q || r.textContent.toLowerCase().indexOf(q) >= 0;
        if (keep && pick !== 'all') keep = r.classList.contains(pick);
        if (keep) {
          Array.prototype.forEach.call(sels, function (sel) {
            var want = sel.value;
            if (want !== 'all' && r.getAttribute(sel.getAttribute('data-key')) !== want) {
              keep = false;
            }
          });
        }
        r.style.display = keep ? '' : 'none';
        var det = detailOf(r);
        if (det) {
          // A hidden row's panel is hidden and collapsed, so re-showing the row
          // never reveals a panel the reader did not open.
          det.style.display = keep ? '' : 'none';
          if (!keep) {
            det.setAttribute('hidden', '');
            r.setAttribute('aria-expanded', 'false');
            r.classList.remove('open');
          }
        }
        if (keep) shown += 1;
      });
      if (out) {
        out.textContent = shown === rows.length
          ? shown + ' comparisons'
          : shown + ' of ' + rows.length + ' comparisons';
      }
    }
    box.addEventListener('input', apply);
    Array.prototype.forEach.call(sels, function (sel) {
      sel.addEventListener('change', apply);
    });
    Array.prototype.forEach.call(chips, function (c) {
      c.addEventListener('click', function () {
        pick = c.getAttribute('data-vf');
        Array.prototype.forEach.call(chips, function (o) {
          o.setAttribute('aria-pressed', o === c ? 'true' : 'false');
        });
        apply();
      });
    });
  });
})();
</script>"""


# Hover definitions. Every label on the page that is jargon gets one, so the page can
# stay terse without assuming the reader knows our vocabulary. Kept as supplementary
# detail only: nothing here is needed to read a number correctly, because a definition
# reachable only by hovering is invisible in print and on a touchscreen.
DEFINITIONS = {
    "Median runtime": "Wall-clock time for a run, middle value across the cell.",
    "Median tokens": "Input plus output tokens per run. Comparable within a model only: "
                     "providers count reasoning and cache tokens differently.",
    "Opened the skill": "Runs that actually read the skill file. Having it installed and "
                        "reading it are different things, and both are measured.",
    "Tools reached for": "Neuroimaging tools invoked, counting runs not invocations. A run "
                         "that tried two tools appears under both.",
    "Not-found claims": "Times the agent reported a file or tool as missing. High counts "
                        "usually mean it was looking in the wrong place.",
    "Ran": "Dates the runs in this cell were executed.",
    "Task": "One benchmark problem: a dataset, a goal, and a hidden reference to score "
            "against.",
    "Model": "The LLM driving the agent. Models are the population measured across, not "
             "competitors.",
    "No skill": "Baseline arm. The agent gets the environment and the task, no skill file.",
    "With skill": "Same task and environment, with the skill installed.",
    "Effect": "Difference in pass rate, drawn on an axis centred at zero. The whisker is "
              "the 95% interval.",
    "Δ pp": "Change in pass rate, in percentage points. 20% to 70% is +50 pp.",
    "95% CI": "Newcombe interval on the difference. If it spans zero, this many runs "
              "cannot separate the arms.",
    "Verdict": "clear: interval and Fisher agree. split: they disagree, so the result is "
               "on the boundary of what this many runs resolve. unclear: neither.",
    "Excluded and not pooled": "What the harness set aside, and where it refused to "
                               "combine results. The tables above show only runs that "
                               "survived, so a sweep can look complete while a fifth of "
                               "it was discarded.",
    "runs excluded": "Runs dropped because WE broke them, not the model: our timeout "
                     "killed the agent mid-run, the wrong skill was in place, or the "
                     "gateway died. Scoring them zero would blame the model for our "
                     "infrastructure, so they are set aside and re-run instead.",
    "not poolable": "Results for this task cannot be summed across models. Something "
                    "that should have been held constant varied inside a single arm: "
                    "the environment version, or the skill's own content. Per-model "
                    "comparisons are still sound; only a pooled total is not.",
    "reason not recorded": "This summary predates the field naming which value caused "
                           "the flag. Re-run summarize.py and it will say which.",
    "Fisher p": "Fisher exact test on the 2x2 table. Probability of a difference this "
                "large if the skill did nothing.",
}


def term(label):
    """A label, with a hover definition when we have one for it."""
    d = DEFINITIONS.get(label)
    if not d:
        return html.escape(label)
    return ('<span class="term" tabindex="0" title="%s">%s<i class="q">?</i></span>'
            % (html.escape(d), html.escape(label)))


def baseline_arm_for(summary, model):
    """The baseline arm this model actually ran, or None.

    Not hardcoded to "env-only": a task can name its control differently, and
    guessing produces a detail panel that silently compares a cell to nothing.
    """
    for arm in cell_matrix(summary).get(model, {}):
        if not is_skill_arm(arm):
            return arm
    return None


def _num(v, unit="", dp=0):
    if v in (None, "", 0):
        return '<span class="dim">&mdash;</span>'
    return ("%.*f%s" % (dp, v, unit)) if dp else ("%s%s" % ("{:,}".format(int(v)), unit))


def detail_panel(summary, model, skill_arm):
    """Everything the harness measured about this comparison beyond pass rate.

    Runtime, token cost, uptake, which tools were reached for, and when the runs
    happened. All read straight from the cell -- nothing is derived here, because a
    figure computed two ways is a figure that eventually disagrees with itself.
    Tokens per passing run is deliberately absent: summarize computes it from raw
    runs and summary.json carries only a median, so it cannot be reproduced exactly.
    """
    cells = summary.get("cells", {})
    base = baseline_arm_for(summary, model)
    arms = [a for a in (base, skill_arm) if a]
    got = [(a, cells.get("%s|%s" % (model, a))) for a in arms]
    got = [(a, c) for a, c in got if c]
    if not got:
        return ""

    def row(label, fn, hint=""):
        tds = "".join('<td class="n">%s</td>' % fn(c) for _, c in got)
        return ('<tr><th scope="row">%s%s</th>%s</tr>'
                % (term(label),
                   ' <span class="hint">%s</span>' % html.escape(hint) if hint else "",
                   tds))

    head = "".join('<th>%s</th>' % html.escape(ARM_LABEL.get(a, a)) for a, _ in got)
    body = [
        row("Median runtime", lambda c: _num(c.get("median_minutes"), " min", 1)),
        row("Median tokens", lambda c: _num(c.get("median_tokens_total")), "per run"),
        row("Opened the skill", lambda c: "%d/%d" % (c.get("uptake", 0), c.get("n", 0))),
        # Counts in brackets. "bet 9, afni 3" reads as two numbers with a comma
        # between them; "bet (9), afni (3)" reads as a tool and its count.
        row("Tools reached for",
            lambda c: html.escape(", ".join(
                "%s (%d)" % (k, v) for k, v in
                sorted((c.get("methods") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
            ) or "none")),
        row("Not-found claims", lambda c: _num(c.get("not_found_claims"))),
        row("Ran", lambda c: html.escape(
            c["first_run"] if c.get("first_run") == c.get("last_run")
            else "%s to %s" % (c.get("first_run"), c.get("last_run")))
            if c.get("first_run") else '<span class="dim">&mdash;</span>'),
    ]
    return ('<table class="detail"><thead><tr><th scope="col"></th>%s</tr></thead>'
            '<tbody>%s</tbody></table>' % (head, "".join(body)))


# What can be plotted per cell, straight from summary.json. Nothing is derived: a
# figure computed here and also computed by summarize is a figure that eventually
# disagrees with itself.
# The last field is `within_model`: scale each model's bars against that model's own
# maximum rather than a shared axis.
#
# Token counts need it. Every report this project produces says absolute counts are
# NOT comparable across models, because some providers report reasoning and cache
# tokens and others report zero. qwen3's median is 150x glm's, so a shared axis both
# flattens glm to nothing AND asserts the cross-model comparison we tell people not to
# make. Scaled within a model, the bar shows what the skill did to that model's bill,
# which is the comparison that holds. The absolute figure is printed on every bar either
# way, so nothing is hidden.
METRICS = [
    ("pass",   "Pass rate",        lambda c: 100.0 * c.get("passes", 0) / c["n"]
                                   if c.get("n") else None, "%",     0, False),
    ("mins",   "Median runtime",   lambda c: c.get("median_minutes"),  " min",  1, False),
    ("tokens", "Median tokens",    lambda c: c.get("median_tokens_total"), "",  0, True),
    ("uptake", "Opened the skill", lambda c: 100.0 * c.get("uptake", 0) / c["n"]
                                   if c.get("n") else None, "%",     0, False),
    ("nf",     "Not-found claims", lambda c: c.get("not_found_claims"), "",     0, False),
]


def _compact(v, dp, unit):
    """42118 -> 42.1k, 6306539 -> 6.31M. A seven-digit label does not fit a bar."""
    a = abs(v)
    if not unit and a >= 1000:
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
            if a >= div:
                q = v / div
                return ("%.2f%s" if q < 10 else "%.1f%s") % (q, suf)
    return ("%.*f%s" % (dp, v, unit))


def build_charts(entries):
    """Models across the bottom, a selectable measure up the side, one bar per arm.

    Every combination is rendered server-side and all but one hidden, rather than
    computing bar heights in the browser. The numbers on this chart then come from the
    same place as the numbers in the tables, and a chart cannot quietly disagree with
    the row above it.

    Bars are CSS boxes, not SVG: an <svg> carries an xmlns URL, and this page is
    required to reach no external host at all.
    """
    charts = []
    for name, s, _ in entries:
        mat = cell_matrix(s)
        cells = s.get("cells", {})
        models = list(mat)
        arms = []
        for m in models:
            for a in sorted(mat[m], key=lambda x: (is_skill_arm(x), x)):
                if a not in arms:
                    arms.append(a)
        if not models:
            continue
        for key, label, fn, unit, dp, within in METRICS:
            vals = {}
            for m in models:
                for a in arms:
                    c = cells.get("%s|%s" % (m, a))
                    if c:
                        try:
                            v = fn(c)
                        except (TypeError, ZeroDivisionError, KeyError):
                            v = None
                        if v is not None:
                            vals[(m, a)] = float(v)
            if not vals:
                continue
            shared = max(vals.values()) or 1.0
            rows_html = []
            for m in models:
                top = shared
                if within:
                    mine = [vals[(m, a)] for a in arms if (m, a) in vals]
                    top = (max(mine) if mine else 0) or 1.0
                pts, txt = [], []
                for i, a in enumerate(arms):
                    v = vals.get((m, a))
                    lab = ARM_LABEL.get(a, a)
                    if v is None:
                        txt.append('<span class="dv dim">&mdash;</span>')
                        continue
                    x = 100.0 * v / top
                    pts.append((x, i, lab, v))
                    txt.append('<span class="dv a%d">%s</span>'
                               % (min(i, 3), html.escape(_compact(v, dp, unit))))
                bar = ""
                if len(pts) > 1:
                    lo = min(p[0] for p in pts)
                    hi = max(p[0] for p in pts)
                    # The connector IS the effect: its length is the change and its
                    # direction is the sign. That is the thing grouped bars make you
                    # work out by eye.
                    bar += ('<i class="link" style="left:%.2f%%;width:%.2f%%"></i>'
                            % (lo, max(hi - lo, 0.4)))
                for x, i, lab, v in pts:
                    bar += ('<i class="dot a%d" style="left:%.2f%%" title="%s: %s"></i>'
                            % (min(i, 3), x,
                               html.escape("%s, %s" % (m, lab)),
                               html.escape(_compact(v, dp, unit))))
                rows_html.append(
                    '<div class="drow"><span class="dm">%s</span>'
                    '<span class="dtrack">%s</span><span class="dvs">%s</span></div>'
                    % (html.escape(m), bar, "".join(txt)))
            legend = "".join('<span class="lg"><i class="sw a%d"></i>%s</span>'
                             % (min(i, 3), html.escape(ARM_LABEL.get(a, a)))
                             for i, a in enumerate(arms))
            charts.append(
                '<figure class="chart" data-task="%s" data-metric="%s" hidden>'
                '<figcaption>%s<span class="unit">%s</span>%s</figcaption>'
                '<div class="plot">%s</div><div class="lgs">%s</div></figure>'
                % (html.escape(name), key, html.escape(label),
                   html.escape(unit.strip() or ""),
                   '<span class="unit">scaled within each model, not across them</span>'
                   if within else "",
                   "".join(rows_html), legend))

    if not charts:
        return ""

    tasks = [name for name, _, _ in entries]
    tsel = "".join('<option value="%s">%s</option>' % (html.escape(t), html.escape(t))
                   for t in tasks)
    msel = "".join('<option value="%s">%s</option>' % (k, html.escape(l))
                   for k, l, _, _, _, _ in METRICS)
    return ('<div class="bar"><h2>Compare</h2><span class="tools">'
            '<select id="ct" aria-label="Task">%s</select>'
            '<select id="cm" aria-label="Measure">%s</select></span></div>'
            '<div class="card charts">%s</div>' % (tsel, msel, "".join(charts)))


def build_effects(entries):
    """Every skill-versus-baseline comparison, ranked by effect size.

    Eight columns, two lines each: the headline figure on the first line and the number
    that qualifies it on the second (k/n under a rate, the interval under Δ, Fisher p
    under the verdict). That is the density decision — it pairs each figure with its own
    caveat instead of spreading them across four columns that scroll off the right edge.
    """
    rows = []
    for name, s, href in entries:
        expect = expected_reps(s)
        for model, arm, e in effect_rows(s):
            # An absent interval used to default to [0, 0], which drew a zero-width
            # whisker on the centre line beside a real effect size: a fabricated
            # result, not a missing one. summarize.py withholds the interval below
            # MIN_N_FOR_STATS, so this is reachable from the ordinary producer.
            ci = usable_ci(e.get("ci95_pp"))
            rows.append({
                "task": name, "href": href, "model": model, "arm": arm,
                "summary": s,
                "kb": e.get("env_only_pass", 0), "nb": e.get("env_only_n", 0),
                "ks": e.get("env_skill_pass", 0), "ns": e.get("env_skill_n", 0),
                "delta": e.get("delta_pp", 0.0),
                "lo": ci[0] if ci else None, "hi": ci[1] if ci else None,
                "p": e.get("fisher_p"), "expect": expect,
                "short": bool(expect) and min(e.get("env_only_n", 0),
                                              e.get("env_skill_n", 0)) < expect,
            })
    if not rows:
        return '<div class="card"><p class="empty">No task has both arms yet.</p></div>', 0, 0

    rows.sort(key=lambda r: (-r["delta"], r["task"], r["model"]))
    body = []
    for i, r in enumerate(rows, 1):
        # Marked on the row rather than explained in a caption, and as text rather
        # than by colour alone: greying was the only signal, which fails anyone who
        # cannot perceive it and disappears entirely in print.
        state = agreement(r["lo"], r["hi"], r["p"])
        arm = "" if r["arm"] in ("env+skill", "skill") else \
            '<span class="arm">%s</span>' % html.escape(r["arm"])
        task = ('<a href="%s">%s</a>' % (html.escape(r["href"]), html.escape(r["task"]))
                if r["href"] else html.escape(r["task"]))
        p = ('<span class="pv dim">&mdash;</span>' if r["p"] is None
             else '<span class="pv">%.3f</span>' % r["p"])
        body.append(
            '<tr class="{ns} exp" tabindex="0" role="button" aria-expanded="false" title="Show runtime, tokens, tools" data-task="{tsort}" data-model="{model}"><td class="rank">{i}</td>'
            '<td class="l name" data-v="{tsort}">{task}</td>'
            '<td class="l"><span class="model">{model}</span>{arm}</td>'
            '<td data-v="{rb:.4f}">{cb}</td><td data-v="{rs:.4f}">{cs}</td>'
            '<td class="c" data-v="{d:.4f}">{bar}</td>'
            '<td class="n" data-v="{d:.4f}"><span class="delta">{dtxt}</span>'
            '<span class="ci95">{ci}</span></td>'
            '<td class="c" data-v="{psort}"><span class="vd {state}" title="{note}">'
            '<i></i>{state}</span>{p}</td></tr>'.format(
                state=state, note=html.escape(AGREEMENT_NOTE[state]),
                ns=state, i=i, tsort=html.escape(r["task"]), task=task,
                model=html.escape(r["model"]), arm=arm,
                rb=r["kb"] / r["nb"] if r["nb"] else 0,
                cb=rate_cell(r["kb"], r["nb"], r["expect"], bar=False),
                rs=r["ks"] / r["ns"] if r["ns"] else 0,
                cs=rate_cell(r["ks"], r["ns"], r["expect"], bar=False),
                d=r["delta"], bar=delta_bar(r["delta"], r["lo"], r["hi"]),
                dtxt=fmt_pp(r["delta"]), ci=fmt_ci(r["lo"], r["hi"]),
                psort=1.0 if r["p"] is None else r["p"], p=p))
        panel = detail_panel(r["summary"], r["model"], r["arm"])
        if panel:
            body.append('<tr class="det" hidden><td colspan="8">%s</td></tr>' % panel)

    def picker(key, label, values):
        # Populated from the data, never a hardcoded list: a fixed roster keeps
        # offering models that are gone and never offers the ones just added.
        opts = "".join('<option value="%s">%s</option>' % (html.escape(v), html.escape(v))
                       for v in values)
        return ('<select data-for="fx" data-key="data-%s" aria-label="Filter by %s">'
                '<option value="all">All %ss</option>%s</select>'
                % (key, label, label, opts))

    pickers = (picker("task", "task", sorted({r["task"] for r in rows}))
               + picker("model", "model", sorted({r["model"] for r in rows})))

    n_sep = sum(1 for r in rows
                if agreement(r["lo"], r["hi"], r["p"]) == "clear")
    # Header built first, as its own string. Inlining term() into the table literal
    # made the % operator bind across every adjacent literal, so the count's %d
    # collected a header cell instead of a number.
    thead = (
        '<th></th><th class="l" data-s>' + term("Task") + '</th>'
        '<th class="l" data-s>' + term("Model") + '</th>'
        '<th data-s>' + term("No skill") + '<span class="sub">' + term("k/n") + '</span></th>'
        '<th data-s>' + term("With skill") + '<span class="sub">' + term("k/n") + '</span></th>'
        '<th class="c" data-s>' + term("Effect") + '<span class="ax">'
        '<span>&minus;100</span><span>0</span><span>+100</span></span></th>'
        '<th data-s>' + term("Δ pp") + '<span class="sub">' + term("95% CI") + '</span></th>'
        '<th class="c" data-s>' + term("Verdict") + '<span class="sub">'
        + term("Fisher p") + '</span></th>')

    table = (
        '<div class="bar"><h2>Skill effect</h2>'
        '<span class="count" data-count="fx">%d comparisons</span>'
        '<span class="tools">'
        '<span class="seg" role="group" aria-label="Filter by verdict">'
        '<button type="button" data-for="fx" data-vf="all" aria-pressed="true">All</button>'
        '<button type="button" data-for="fx" data-vf="clear" aria-pressed="false">Clear</button>'
        '<button type="button" data-for="fx" data-vf="split" aria-pressed="false">Split</button>'
        '<button type="button" data-for="fx" data-vf="unclear" aria-pressed="false">Unclear</button>'
        '</span>'
        '%s'
        '<input type="search" data-filter="fx" placeholder="Search" '
        'aria-label="Search task or model"></span></div>'
        '<div class="card tall"><table id="fx"><thead><tr>%s</tr></thead>'
        '<tbody>%s</tbody></table></div>'
        % (len(rows), pickers, thead, "".join(body)))
    return table, n_sep, len(rows)


def load_roster(path):
    """Model ids the gateway currently serves. Plain lines, or its /v1/models JSON.

    Retirement is read from a roster, never inferred from gaps in the data, because
    "not run yet" and "cannot be run again" are indistinguishable from runs alone.
    With no roster, nothing is marked: a benchmark should not guess that a model is
    gone.
    """
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw.startswith("{"):
        try:
            return [m.get("id", "") for m in json.loads(raw).get("data", [])]
        except (ValueError, AttributeError):
            return None
    return [l.strip() for l in raw.splitlines() if l.strip() and not l.startswith("#")]


def is_retired(model, roster):
    """The gateway names models `provider/model`; our cells hold the bare name."""
    if roster is None:
        return False
    return not any(model == r or r.endswith("/" + model) or model in r for r in roster)


def build_grid(entries, roster=None):
    """Task × model, arms stacked inside the cell.

    The task column is sticky, so at ten models the row you are reading keeps its name
    while the models scroll. Each rate keeps its denominator and its Wilson interval;
    a heatmap would fit more models per screen by throwing both away.
    """
    models = []
    for _, s, _ in entries:
        for m in cell_matrix(s):
            if m not in models:
                models.append(m)

    # A retired model keeps its cells forever -- they are evidence of what was true
    # then -- but it must not read as if it could be re-run.
    head = "".join(
        '<th data-s><span class="model">%s</span>%s</th>'
        % (html.escape(m),
           '<span class="sub">retired</span>' if is_retired(m, roster) else "")
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
        rows.append('<tr><td class="l name stick" data-v="%s">%s</td>%s</tr>'
                    % (label, tcell, "".join(tds)))

    return ('<div class="bar"><h2>Pass rate</h2>'
            '<span class="count">%d tasks &times; %d models</span></div>'
            '<div class="card scroll"><table class="grid">'
            '<thead><tr><th class="l stick" data-s>Task</th>%s'
            '</tr></thead><tbody>%s</tbody></table></div>'
            % (len(entries), len(models), head, "".join(rows)))


def notices(entries):
    """What summarize.py flagged and this page used to discard.

    The grid renders `cells`, which holds only the runs that survived. A sweep can
    therefore look complete while a fifth of it was thrown out, and un-poolable data
    can sit under a pooled-looking heading. Both facts are in summary.json and both
    were being ignored. Terse rows, no prose: the numbers say it.
    """
    rows = []
    for name, s, _ in entries:
        bits = []
        n_ex = s.get("n_excluded") or 0
        if n_ex:
            why = s.get("exclusions") or {}
            top = sorted(why.items(), key=lambda kv: (-kv[1], kv[0]))
            detail = "; ".join("%s &times;%d" % (html.escape(k.split(":")[0]), v)
                               for k, v in top[:3])
            # No mixing of % with +: the % binds tighter, so its arguments would be
            # applied to the trailing literal alone rather than the whole string.
            bits.append("<b>%d</b> of %d " % (n_ex, s.get("n_runs") or n_ex)
                        + term("runs excluded")
                        + (" &mdash; " + detail if detail else ""))
        # poolable is absent in older summaries; absent is not the same as false.
        if s.get("poolable") is False:
            because = s.get("not_poolable_because") or s.get("provenance_varies") or []
            bits.append(term("not poolable")
                        + (" &mdash; " + ", ".join(html.escape(b) for b in because)
                           if because
                           else " &mdash; " + term("reason not recorded")))
        if bits:
            rows.append('<tr><td class="l name">%s</td><td class="l note">%s</td></tr>'
                        % (html.escape(name), " &middot; ".join(bits)))
    if not rows:
        return ""
    return ('<div class="bar"><h2>' + term("Excluded and not pooled") + '</h2>'
            '<span class="count">%d tasks</span></div>'
            '<div class="card scroll"><table><thead><tr>'
            '<th class="l">Task</th><th class="l">What summarize flagged</th>'
            '</tr></thead><tbody>%s</tbody></table></div>' % (len(rows), "".join(rows)))


def provenance_table(entries):
    """What produced each number: image, agent, skill and task versions.

    A published benchmark that cannot say which version produced a figure is an
    anecdote. Every value is shown with its run count, and "not recorded" is kept
    distinct from a value that differs -- one is a measurement we did not take.
    """
    keys = [("image_version", "image"), ("opencode_version", "agent"),
            ("skills_hash", "skill"), ("tasks_sha", "tasks")]
    rows = []
    for name, s, _ in entries:
        prov = s.get("provenance") or {}
        if not prov:
            continue
        tds = []
        for k, _lab in keys:
            counts = prov.get(k) or {}
            known = [(v, n) for v, n in counts.items()
                     if v not in ("", "unknown", "none", None)]
            known.sort(key=lambda vn: (-vn[1], vn[0]))
            miss = sum(n for v, n in counts.items() if (v, n) not in known)
            bits = ["%s <span class=\"dim\">(%d)</span>" % (html.escape(str(v)[:12]), n)
                    for v, n in known[:2]]
            if miss:
                bits.append('<span class="dim">not recorded (%d)</span>' % miss)
            tds.append('<td class="n">%s</td>' % (", ".join(bits) or
                                                  '<span class="dim">&mdash;</span>'))
        first, last = s.get("first_run"), s.get("last_run")
        when = ("%s to %s" % (first, last)) if first and last and first != last else (first or "")
        tds.append('<td class="n">%s</td>'
                   % (html.escape(when) if when else '<span class="dim">&mdash;</span>'))
        rows.append('<tr><td class="l name">%s</td>%s</tr>' % (html.escape(name), "".join(tds)))
    if not rows:
        return ""
    head = "".join('<th>%s</th>' % lab for _, lab in keys) + "<th>ran</th>"
    return ('<div class="bar"><h2>Provenance</h2></div>'
            '<div class="card scroll"><table><thead><tr><th class="l">Task</th>%s</tr>'
            '</thead><tbody>%s</tbody></table></div>' % (head, "".join(rows)))


def build(entries, roster=None):
    effects, n_sep, n_cmp = build_effects(entries)

    models, runs, reps, n_excluded = set(), 0, set(), 0
    for _, s, _ in entries:
        for key, v in s.get("cells", {}).items():
            models.add(key.partition("|")[0])
            runs += v.get("n", 0)
        n_excluded += s.get("n_excluded") or 0
        if expected_reps(s):
            reps.add(expected_reps(s))

    def stat(value, label):
        return '<div class="stat"><b>%s</b><span>%s</span></div>' % (value, label)

    def plural(n, w):
        return "%s%s" % (w, "" if n == 1 else "s")

    # The same figures the meta line always carried, set as a row of tiles so the page
    # opens on the size of the sweep. No number here is computed differently.
    tiles = "".join(x for x in [
        stat(len(entries), plural(len(entries), "task")),
        stat(len(models), plural(len(models), "model")),
        stat(runs, "runs"),
        stat(n_excluded, "excluded") if n_excluded else "",
        stat("/".join(str(r) for r in sorted(reps)), "per cell") if reps else "",
        stat("%d/%d" % (n_sep, n_cmp), "clear") if n_cmp else "",
    ] if x)

    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Neurodesk agent benchmark</title>
%s</head><body><div class="wrap">
  <h1>Neurodesk agent benchmark</h1>
  <div class="stats">%s</div>
  <p class="key">
    <span><code>k/n</code> passed / runs</span>
    <span><span class="swatch"></span>95%% Wilson CI</span>
    <span><code>&dagger;</code> short cell</span>
    <span><span class="vd clear"><i></i>clear</span> interval and Fisher agree</span>
    <span><span class="vd split"><i></i>split</span> they disagree</span>
    <span><span class="vd unclear"><i></i>unclear</span> neither</span>
  </p>
  %s
  %s
  %s
  %s
  %s
  <p class="foot">Pass = valid output and verdict at or above acceptable.
  Built by <code>build_index.py</code> from each task's <code>summary.json</code>;
  effects read from <code>skill_effect</code>.</p>
</div>%s</body></html>""" % (STYLE, tiles, effects, build_charts(entries),
                            build_grid(entries, roster),
                            notices(entries), provenance_table(entries), SCRIPT)


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
    ap.add_argument("--roster", metavar="FILE",
                    help="models the gateway currently serves: one per line, or its "
                         "/v1/models JSON. Models on the board but absent from it are "
                         "marked retired. Omit it and nothing is marked.")
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
    a.out.write_text(build(entries, load_roster(a.roster)), encoding="utf-8")
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
