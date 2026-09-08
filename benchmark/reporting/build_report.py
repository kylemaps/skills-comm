#!/usr/bin/env python3
"""Render one benchmark task's grading outputs into a single self-contained HTML report.

Consumes the durable, tiny records the harness emits — `summary.json` (per-cell aggregates +
provenance) and `runs.csv` (one row per run) — which survive an environment purge, plus optional
extras: a grader `rubric.json` (rendered as a scoring card), analysis figures, and per-run QC
thumbnails. Everything is inlined as base64, so the output needs no server and no external asset.

    python build_report.py --summary summary.json --runs runs.csv \
        [--rubric rubric.json] [--figure analysis.png ...] [--thumbs qc/] \
        --title "7T brain extraction" --out report.html

Input schema (summary.json):
    { "task": str, "n_runs": int, "n_valid": int,
      "provenance": {"image_version": {...}, "opencode_version": {...},
                     "skills_sha": {...}, "tasks_sha": {...}},
      "poolable": bool,
      "cells": { "<model>|<arm>": {"n","passes","mean","sd","uptake",
                                   "not_found_claims","methods":{tool:count}} },
      "skill_effect": { "<model>": {"delta_pp","ci95_pp":[lo,hi],"fisher_p", ...} } }

runs.csv must contain at least: model, arm, verdict, score, dice, passed (extra columns are ignored).
"""
import argparse
import base64
import csv
import html
import json
import sys
from collections import defaultdict
from pathlib import Path

# Imported, not re-derived. This report and the leaderboard render the same
# summary.json, and the one failure worth designing against is the two of them
# disagreeing about a number. Sharing the formatting and the significance test
# makes that impossible rather than merely unlikely.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_index import (  # noqa: E402
    AGREEMENT_NOTE, BASELINE_ARMS, agreement, fmt_ci, fmt_pp, is_skill_arm,
    usable_ci)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def img_tag(path: Path) -> str:
    return f'<img alt="" src="data:image/png;base64,{b64(path.read_bytes())}">'


def one(d: dict) -> str:
    """Collapse a provenance histogram {value: count} to a compact string."""
    if not d:
        return "—"
    return ", ".join(str(k) for k in d)


# An ordinal scale, so it gets one hue stepped light-to-dark plus a neutral for the
# non-results. White-on-amber measured 1.9:1; the label now wears an ink token and the
# hue rides on a dot beside it, which also survives greyscale and colour-blindness.
VERDICT_COLORS = {
    "indistinguishable": "#184f95", "acceptable": "#2a78d6", "marginal": "#86b6ef",
    "unacceptable": "#d03b3b", "invalid": "#8b8a84", "fail": "#8b8a84",
}


def verdict_pill(verdict: str) -> str:
    c = VERDICT_COLORS.get(verdict.lower(), "#8b8a84")
    return (f'<span class="pill"><i style="background:{c}"></i>'
            f'{html.escape(verdict)}</span>')


def parse_cells(cells: dict):
    """-> (models_in_order, arms_in_order, lookup[(model,arm)])."""
    models, arms = [], []
    lut = {}
    for key, v in cells.items():
        model, _, arm = key.partition("|")
        if model not in models:
            models.append(model)
        if arm not in arms:
            arms.append(arm)
        lut[(model, arm)] = v
    return models, arms, lut


def pass_rate(cell: dict) -> float:
    n = cell.get("n", 0)
    return cell.get("passes", 0) / n if n else 0.0


def effect_lookup(skill_effect):
    """{(model, arm): effect}, from keys written either `model` or `model|arm`.

    summarize.py switched to the second form when a sweep gained a second skill arm.
    This looked up the bare model name, so on every summary written since, the whole
    column rendered as em-dashes: a populated heading over no data, which reads as "no
    effect measured" rather than "renderer is broken". Head-to-head `a_vs_b` entries
    answer a different question and are excluded.
    """
    out = {}
    for key, v in (skill_effect or {}).items():
        model, _, arm = key.partition("|")
        if "_vs_" in arm:
            continue
        out[(model, arm or "env+skill")] = v
    return out


def score_summary(c):
    """The score half of a cell, or a marker when the mean describes no run.

    summarize.py flags a cell bimodal when its scores cluster at both ends, and says in
    as many words: quote the pass rate, not the mean. Rendering 30±48 there is worse
    than rendering nothing. One decimal, because 99.81 at zero decimals is 100, and no
    run scored 100.
    """
    mean, sd = c.get("mean"), c.get("sd")
    if mean is None:
        return ""
    if c.get("bimodal"):
        tip = "Bimodal: mean %.1f, sd %.1f, median %.1f. The mean describes no run." % (
            mean, sd or 0.0, c.get("median", mean))
        return '<span class="score" title="%s"> · bimodal</span>' % html.escape(tip)
    return '<span class="score"> · %.1f±%.1f</span>' % (mean, sd or 0.0)


def effect_cell(se):
    if not se:
        return '<td class="num">&mdash;</td>'
    ci = usable_ci(se.get("ci95_pp"))
    lo, hi = ci if ci else (None, None)
    p = se.get("fisher_p")
    ptxt = "p n/a" if p is None else ("p=%.3f" % p)
    # Same verdict function as the leaderboard, so the two pages cannot reach
    # different conclusions about the same row. Named in text rather than signalled
    # by colour alone.
    state = agreement(lo, hi, p)
    return ('<td class="num %s"><span class="delta">%s pp</span>'
            '<span class="sub">%s &middot; %s'
            '<span class="vd %s" title="%s"><i></i>%s</span></span></td>'
            % (state, fmt_pp(se.get("delta_pp", 0.0)), fmt_ci(lo, hi), ptxt,
               state, html.escape(AGREEMENT_NOTE[state]), state))


def leaderboard_table(models, arms, lut, skill_effect, poolable):
    eff = effect_lookup(skill_effect)
    skill_arms = [a for a in arms if is_skill_arm(a)]
    # One effect column per skill arm. A single column cannot hold two skills, and
    # showing an arbitrary one of them under a heading that names neither is worse
    # than showing both.
    eff_arms = [a for a in skill_arms if any((m, a) in eff for m in models)]

    head = "".join("<th class='num'>%s<span class='sub'>k/n · score</span></th>"
                   % html.escape(a) for a in arms)
    head += "".join("<th class='num'>effect<span class='sub'>%s vs baseline</span></th>"
                    % html.escape(a) for a in eff_arms)
    rows = []
    for m in models:
        cellhtml = []
        for a in arms:
            c = lut.get((m, a))
            if not c:
                cellhtml.append('<td class="num">&mdash;</td>')
                continue
            n, k = c.get("n", 0), c.get("passes", 0)
            pr = k / n if n else 0.0
            cellhtml.append(
                '<td class="num"><span class="pr" style="--v:%.4f">'
                '<b>%d/%d</b><i></i></span>%s</td>'
                % (pr, k, n, score_summary(c)))
        for a in eff_arms:
            cellhtml.append(effect_cell(eff.get((m, a))))
        rows.append("<tr><td><b>%s</b></td>%s</tr>"
                    % (html.escape(m), "".join(cellhtml)))

    # Pooling across models needs every model present in every arm, which `poolable`
    # does not check -- it means the provenance is homogeneous. Summing a ragged design
    # puts two different populations side by side as though they were a comparison.
    balanced = all((m, a) in lut for m in models for a in arms)
    if poolable and balanced and len(models) > 1:
        pooled = []
        for a in arms:
            tot_p = sum(lut[(m, a)].get("passes", 0) for m in models)
            tot_n = sum(lut[(m, a)].get("n", 0) for m in models)
            pr = tot_p / tot_n if tot_n else 0
            pooled.append('<td class="num"><span class="pr" style="--v:%.4f">'
                          '<b>%d/%d</b><i></i></span></td>' % (pr, tot_p, tot_n))
        pooled += ['<td class="num">&mdash;</td>'] * len(eff_arms)
        rows.append('<tr class="pooled"><td><b>ALL</b></td>%s</tr>' % "".join(pooled))

    return ("<table><thead><tr><th>model</th>%s</tr></thead><tbody>%s</tbody></table>"
            % (head, "".join(rows)))


def scoring_card(rubric: dict) -> str:
    if not rubric:
        return ""
    gates = "".join(f'<span class="chip">{html.escape(g)}</span>' for g in rubric.get("gates", []))
    weights = rubric.get("weights", {})
    wbars = "".join(
        f'<div class="wrow"><span>{html.escape(k)}</span>'
        f'<span class="wbar"><i style="width:{v*100:.0f}%"></i></span>'
        f'<span class="num">{v:.2f}</span></div>'
        for k, v in sorted(weights.items(), key=lambda kv: -kv[1]))
    vt = rubric.get("verdict_thresholds", {})
    prompt = html.escape(rubric.get("prompt", ""))
    return f"""<section><h2>The task &amp; how it's scored</h2>
      <p class="prompt">{prompt}</p>
      <div class="two">
        <div><h4>Validity gates — any failure ⇒ invalid, score 0</h4>{gates}</div>
        <div><h4>Quality score (weighted, only if gates pass)</h4>{wbars}
          <p class="sub">acceptable ≥ {vt.get('acceptable','?')} · marginal ≥ {vt.get('marginal','?')}</p></div>
      </div></section>"""


def per_run_table(runs):
    cols = ["model", "arm", "verdict", "score", "dice", "passed"]
    have = [c for c in cols if runs and c in runs[0]]
    head = "".join(f"<th>{c}</th>" for c in have)
    rows = []
    for r in runs:
        cells = []
        for c in have:
            val = r.get(c, "")
            if c == "verdict":
                cells.append(f"<td>{verdict_pill(val)}</td>")
            elif c in ("score", "dice"):
                # Escaped like every other column. These come from the grader parsing
                # model-produced output, so they are not trusted input, and a stray
                # "<" silently swallows the rest of the row.
                cells.append(f'<td class="num">{html.escape(str(val))}</td>')
            else:
                cells.append(f"<td>{html.escape(str(val))}</td>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"<div class='scroll'><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def gallery(thumbs_dir: Path) -> str:
    if not thumbs_dir or not thumbs_dir.is_dir():
        return ""
    pngs = sorted(thumbs_dir.glob("*.png"))
    if not pngs:
        return ""
    cells = "".join(
        f'<figure class="cell"><img alt="{html.escape(p.stem)}" '
        f'src="data:image/png;base64,{b64(p.read_bytes())}">'
        f'<figcaption>{html.escape(p.stem)}</figcaption></figure>' for p in pngs)
    return f"<section><h2>Run snapshot</h2><div class='grid'>{cells}</div></section>"


STYLE = """<style>
/* Same tokens as build_index.py, so the two pages read as one system. Every text
   colour clears WCAG AA (4.5:1) on the surface behind it, in both modes; hue is
   reserved for marks, and dark mode is stepped for its own surface, not inverted. */
:root{color-scheme:light dark;
--surface:#fcfcfb;--plane:#f4f4f1;--head:#f7f7f5;--hover:rgba(11,11,11,.032);
--rule:rgba(11,11,11,.10);--rule2:rgba(11,11,11,.17);
--ink:#0b0b0b;--ink2:#4a4945;--ink3:#6b6a64;
--link:#1c5cab;--pos:#2a78d6;--neg:#d03b3b;--nil:#8b8a84;
--track:#e8e7e3;--warn:#d06a00;--danger:#c8342c;--wash:#eef4fd;
--shadow:0 1px 2px rgba(11,11,11,.05),0 8px 20px -14px rgba(11,11,11,.30);
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
@media(prefers-color-scheme:dark){:root{
--surface:#161617;--plane:#0b0b0c;--head:#1d1d1e;--hover:rgba(255,255,255,.048);
--rule:rgba(255,255,255,.11);--rule2:rgba(255,255,255,.21);
--ink:#f3f3f0;--ink2:#b6b5ae;--ink3:#92918b;
--link:#7fb0f0;--pos:#4f93ea;--neg:#e6564f;--nil:#8c8b85;
--track:#2c2c2d;--warn:#e0a83a;--danger:#ef7a72;--wash:#13202f;
--shadow:0 1px 2px rgba(0,0,0,.55),0 10px 26px -16px rgba(0,0,0,.9)}}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);font-family:var(--sans);
font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1100px;margin:0 auto;padding:56px 28px 88px}
h1{font-size:1.55rem;font-weight:640;margin:0 0 4px;letter-spacing:-.021em}
h2{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
color:var(--ink2);margin:0 0 16px}
h4{font-size:.685rem;font-weight:650;text-transform:uppercase;letter-spacing:.07em;
color:var(--ink3);margin:0 0 10px}
.sub{color:var(--ink2);font-size:.78em}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0 28px}
.provchip{font:500 11.5px/1 var(--mono);background:var(--surface);
border:1px solid var(--rule);color:var(--ink2);padding:8px 11px;border-radius:8px}
.provchip b{color:var(--ink);font-weight:640}
section{background:var(--surface);border:1px solid var(--rule);border-radius:14px;
padding:24px 26px;margin:0 0 22px;box-shadow:var(--shadow)}
.two{display:grid;grid-template-columns:1.3fr 1fr;gap:28px}
@media(max-width:720px){.two{grid-template-columns:1fr}.wrap{padding:34px 16px 64px}}
.prompt{background:var(--wash);border-left:2px solid var(--pos);padding:13px 17px;
border-radius:0 8px 8px 0;margin:0 0 18px;font-size:.9rem}
.chip{display:inline-block;font:500 11px/1 var(--mono);background:var(--plane);
border:1px solid var(--rule);padding:6px 9px;border-radius:7px;margin:0 5px 6px 0;
color:var(--ink2)}
.wrow{display:grid;grid-template-columns:130px 1fr 42px;gap:12px;align-items:center;
margin:7px 0;font-size:.82rem}
.wbar{height:6px;background:var(--track);border-radius:3px;overflow:hidden}
.wbar i{display:block;height:100%;background:var(--pos);border-radius:3px}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:.83rem}
.scroll{overflow:auto}
th{text-align:left;font-size:.655rem;text-transform:uppercase;letter-spacing:.07em;
color:var(--ink3);font-weight:650;padding:0 13px 10px;
border-bottom:1px solid var(--rule2);vertical-align:bottom}
th .sub{display:block;font-size:.95em;font-weight:500;letter-spacing:.045em;
text-transform:none;margin-top:3px}
td{padding:10px 13px;border-bottom:1px solid var(--rule);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--hover)}
.num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
tr.pooled td{border-top:1px solid var(--rule2);font-weight:650}
.pr{display:inline-grid;grid-template-columns:1fr;justify-items:end;gap:4px;
min-width:52px}
.pr b{font:600 .92rem/1.2 var(--sans);font-variant-numeric:tabular-nums;
letter-spacing:-.016em}
.pr i{display:block;width:100%;height:3px;border-radius:2px;background:var(--track);
background-image:linear-gradient(90deg,var(--pos) 0,var(--pos) 100%);
background-size:calc(var(--v)*100%) 100%;background-repeat:no-repeat}
.delta{font:600 .92rem/1.2 var(--sans);font-variant-numeric:tabular-nums;
letter-spacing:-.016em}
.unclear .delta{color:var(--ink2);font-weight:560}
.score{color:var(--ink2);font-size:.85em}
.vd{display:inline-flex;align-items:center;gap:5px;font-weight:640;color:var(--ink);
margin-left:8px}
.vd i{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
.vd.clear i{background:var(--pos)}
.vd.split i{background:conic-gradient(var(--warn) 180deg,transparent 0);
box-shadow:inset 0 0 0 1.5px var(--warn)}
.vd.unclear{color:var(--ink2);font-weight:560}
.vd.unclear i{background:transparent;box-shadow:inset 0 0 0 1.5px var(--nil)}
.pill{display:inline-flex;align-items:center;gap:6px;font:600 11.5px/1 var(--sans);
color:var(--ink)}
.pill i{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
figure.fig{margin:0 0 16px}
figure.fig img{width:100%;border-radius:10px;border:1px solid var(--rule);display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:10px}
.cell{margin:0;border:1px solid var(--rule);border-radius:9px;overflow:hidden;
background:#000}
.cell img{width:100%;display:block}
.cell figcaption{background:var(--surface);color:var(--ink2);
font:500 10px var(--mono);padding:4px 5px;text-align:center}
footer{color:var(--ink2);font-size:.735rem;line-height:1.6;max-width:76ch}
footer code{font-family:var(--mono);color:var(--ink)}
</style>"""


def build(summary, runs, rubric, figures, thumbs, title):
    models, arms, lut = parse_cells(summary.get("cells", {}))
    prov = summary.get("provenance", {})
    chips = [
        f'<span class="provchip">task <b>{html.escape(summary.get("task", "—"))}</b></span>',
        f'<span class="provchip">runs <b>{summary.get("n_runs", len(runs))}</b></span>',
    ]
    for label, key in (("image", "image_version"), ("opencode", "opencode_version"),
                       ("skills", "skills_sha"), ("task", "tasks_sha")):
        if prov.get(key):
            chips.append(f'<span class="provchip">{label} <b>{html.escape(one(prov[key]))}</b></span>')

    figs = "".join(f'<figure class="fig">{img_tag(f)}</figure>' for f in figures if f.exists())
    fig_section = f"<section><h2>Analysis</h2>{figs}</section>" if figs else ""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
{STYLE}</head><body><div class="wrap">
  <h1>{html.escape(title)}</h1>
  <div class="chips">{''.join(chips)}</div>
  {scoring_card(rubric)}
  <section><h2>Leaderboard</h2>{leaderboard_table(models, arms, lut, summary.get("skill_effect"), summary.get("poolable"))}
    <p class="sub">pass = valid &amp; verdict ≥ acceptable · score is mean quality (0–100) ± sd</p></section>
  {fig_section}
  {gallery(thumbs)}
  <section><h2>All runs</h2>{per_run_table(runs)}</section>
  <footer>Generated by <code>build_report.py</code> from the harness <code>summary.json</code> / <code>runs.csv</code>.
  Self-contained: every asset is inlined, so this file is portable and needs no server.</footer>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Render a task's grading outputs into a self-contained HTML report.")
    ap.add_argument("--summary", required=True, type=Path)
    ap.add_argument("--runs", required=True, type=Path)
    ap.add_argument("--rubric", type=Path)
    ap.add_argument("--figure", type=Path, action="append", default=[], help="analysis figure(s) to embed")
    ap.add_argument("--thumbs", type=Path, help="dir of per-run QC PNGs for the gallery (optional)")
    ap.add_argument("--title", default="Benchmark report")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    # Encoding is explicit on every one of these. Without it Python uses the platform
    # locale, so a rubric prompt containing a single ≥ crashes the build on Windows
    # (cp1252) and under a C locale on Linux, after truncating the output file to zero
    # bytes. That leaves a report the index will happily link to.
    with open(a.summary, encoding="utf-8") as fh:
        summary = json.load(fh)
    with open(a.runs, encoding="utf-8", newline="") as fh:
        runs = list(csv.DictReader(fh))
    rubric = None
    if a.rubric:
        with open(a.rubric, encoding="utf-8") as fh:
            rubric = json.load(fh)
    a.out.write_text(build(summary, runs, rubric, a.figure, a.thumbs, a.title),
                     encoding="utf-8")
    print(f"wrote {a.out} ({a.out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
