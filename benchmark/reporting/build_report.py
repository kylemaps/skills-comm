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


VERDICT_COLORS = {
    "indistinguishable": "#2ca25f", "acceptable": "#3aa0a0", "marginal": "#d9a441",
    "unacceptable": "#d9784a", "invalid": "#7f8896", "fail": "#7f8896",
}


def verdict_pill(verdict: str) -> str:
    c = VERDICT_COLORS.get(verdict.lower(), "#7f8896")
    return f'<span class="pill" style="--pc:{c}">{html.escape(verdict)}</span>'


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
    return ('<td class="num %s">%s pp<span class="sub"> CI[%s] %s &middot; '
            '<span class="st" title="%s">%s</span></span></td>'
            % (state, fmt_pp(se.get("delta_pp", 0.0)), fmt_ci(lo, hi), ptxt,
               html.escape(AGREEMENT_NOTE[state]), state))


def leaderboard_table(models, arms, lut, skill_effect, poolable):
    eff = effect_lookup(skill_effect)
    skill_arms = [a for a in arms if is_skill_arm(a)]
    # One effect column per skill arm. A single column cannot hold two skills, and
    # showing an arbitrary one of them under a heading that names neither is worse
    # than showing both.
    eff_arms = [a for a in skill_arms if any((m, a) in eff for m in models)]

    head = "".join("<th>%s<br><span class='sub'>pass · score</span></th>" % html.escape(a)
                   for a in arms)
    head += "".join("<th>effect<br><span class='sub'>%s vs baseline</span></th>"
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
                '<td class="num"><b class="pr" style="--v:%.4f">%d/%d</b>%s</td>'
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
            pooled.append('<td class="num"><b class="pr" style="--v:%.4f">%d/%d</b></td>'
                          % (pr, tot_p, tot_n))
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
:root{--bg:#f5f8fb;--panel:#fff;--ink:#16222e;--muted:#5b6b7a;--line:#e0e8ef;--accent:#2c7fb8;
--accent-soft:#e7f1f8;--mono:ui-monospace,Menlo,Consolas,monospace;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
--shadow:0 1px 2px rgba(20,40,60,.06),0 8px 24px rgba(20,40,60,.05)}
@media(prefers-color-scheme:dark){:root{--bg:#0e1620;--panel:#16212e;--ink:#dce6ef;--muted:#8ea0b2;
--line:#26333f;--accent:#4aa3d6;--accent-soft:#16303f;--shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35)}}
:root[data-theme=light]{--bg:#f5f8fb;--panel:#fff;--ink:#16222e;--muted:#5b6b7a;--line:#e0e8ef;--accent:#2c7fb8;--accent-soft:#e7f1f8}
:root[data-theme=dark]{--bg:#0e1620;--panel:#16212e;--ink:#dce6ef;--muted:#8ea0b2;--line:#26333f;--accent:#4aa3d6;--accent-soft:#16303f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);font-size:15px;line-height:1.55}
.wrap{max-width:1080px;margin:0 auto;padding:40px 24px 80px}h1{font-size:1.8rem;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:1.12rem;margin:0 0 14px}h4{font-size:.8rem;color:var(--muted);margin:0 0 8px;font-weight:600}
.sub{color:var(--muted);font-size:.82em}.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 26px}
.provchip{font:600 12px/1 var(--mono);background:var(--panel);border:1px solid var(--line);color:var(--muted);padding:7px 10px;border-radius:7px}
.provchip b{color:var(--ink)}
section{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px 24px;margin:0 0 22px;box-shadow:var(--shadow)}
.two{display:grid;grid-template-columns:1.3fr 1fr;gap:24px}@media(max-width:720px){.two{grid-template-columns:1fr}}
.prompt{background:var(--accent-soft);border-left:3px solid var(--accent);padding:12px 16px;border-radius:0 8px 8px 0;margin:0 0 14px}
.chip{display:inline-block;font:600 11px/1 var(--mono);background:var(--bg);border:1px solid var(--line);padding:5px 8px;border-radius:6px;margin:0 5px 6px 0;color:var(--muted)}
.wrow{display:grid;grid-template-columns:120px 1fr 40px;gap:10px;align-items:center;margin:6px 0;font-size:.85rem}
.wbar{height:8px;background:var(--bg);border:1px solid var(--line);border-radius:5px;overflow:hidden}.wbar i{display:block;height:100%;background:var(--accent)}
table{width:100%;border-collapse:collapse;font-size:.88rem}.scroll{overflow-x:auto}
th{text-align:left;font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);font-weight:600;padding:0 12px 10px;border-bottom:1px solid var(--line)}
td{padding:9px 12px;border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:none}
.num{font-variant-numeric:tabular-nums;font-family:var(--mono);text-align:right;white-space:nowrap}
tr.pooled td{border-top:2px solid var(--line);font-weight:700}
.pr{padding:2px 7px;border-radius:5px;color:#fff;background:color-mix(in srgb,#2ca25f calc(var(--v)*100%),#c14a3a)}
.score{color:var(--muted);font-size:.85em}.sig{color:#2ca25f}
.pill{display:inline-block;font:600 11px/1 var(--sans);color:#fff;background:var(--pc);padding:4px 8px;border-radius:20px}
figure.fig{margin:0 0 16px}figure.fig img{width:100%;border-radius:10px;border:1px solid var(--line);display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:9px}
.cell{margin:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#000}.cell img{width:100%;display:block}
.cell figcaption{background:var(--panel);color:var(--muted);font:600 10px var(--mono);padding:3px 5px;text-align:center}
footer{color:var(--muted);font-size:.8rem;line-height:1.7}footer code{font-family:var(--mono);background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:1px 5px}
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
