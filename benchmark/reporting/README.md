# Benchmark reporting

Turns the grading harness's **durable outputs** into human-facing, **self-contained HTML** — a
per-task report and a cross-task leaderboard. Nothing here re-grades or needs the raw run data; it
consumes only the small records the harness already emits (`summary.json`, `runs.csv`), so it works
long after the execution environment (and its NIfTIs) are gone.

```
 grading harness  ──►  summary.json + runs.csv  ──►  build_report.py  ──►  report_<task>.html
 (benchmark/harness)   (+ optional rubric,             build_index.py   ──►  index.html
                         figures, QC thumbnails)
```

Two tiers, both static and self-contained (every asset inlined as base64 — no server, no external
request), so the whole thing is one folder you can open locally, upload as a CI artifact, or publish
to GitHub Pages:

```
index.html            ← overview: task × model pass-rate grid
      │  click a task
      ▼
report_<task>.html    ← one task: scoring card, leaderboard, figures, per-run table, (optional) QC gallery
```

## Usage

Per-task report:

```bash
python build_report.py \
  --summary path/to/summary.json \
  --runs    path/to/runs.csv \
  --rubric  ../graders/structural-brain-extraction-7t/rubric.json  \  # optional: scoring card
  --figure  analysis.png  [--figure more.png ...]                  \  # optional: embedded figures
  --thumbs  qc/                                                    \  # optional: per-run QC PNG gallery
  --title   "7T brain extraction" \
  --out     report.html
```

Leaderboard index (one `--entry` per task; `REPORT_HREF` may be a relative path or a URL, or `''`):

```bash
python build_index.py \
  --entry "Brain extraction — 7T" brain-extraction-7t/summary.json brain-extraction-7t/report.html \
  --entry "Tissue segmentation — 7T" tissue-seg-7t/summary.json tissue-seg-7t/report.html \
  --out index.html
```

Or point it at a harness report directory and let it find the tasks:

```bash
python build_index.py --report-dir ~/bench/report --out index.html
```

It picks up every `summary_<task>.json` in the directory and links each task to
`report_<task>.html` when that file is sitting alongside. One command, so a finished sweep lands
in the leaderboard without anyone editing an argument list. It also prints any cell that came in
short of the task's repeat count, which is what you want to know before quoting a number off the
page.

Only `numpy`-free stdlib is used (`json`, `csv`, `base64`, `html`) — no dependencies.

## Reading the index

Two tables. **Skill effect** is the leaderboard: every skill-versus-baseline comparison ranked by
effect size, the difference drawn on an axis centred at zero with the 95% CI as a whisker. Rows
greyed when that interval includes zero. **Pass rate** is the task × model grid, arms stacked.
Both sortable by any column; the effect table has a filter box.

The unit is a cell: one task, one model, one arm, repeated N times.

- **`k/n` sits beside every rate.** 8/10 and 80/100 are both "80%". A rate on its own lets a
  topped-up cell sit in the grid looking like a comparison while the denominators have quietly
  diverged. A cell short of the task's repeat count is marked `†`; the count is inferred from the
  task's own cells, so a 5-repeat pilot is not flagged against a 10-repeat constant.
- **Each rate in the grid carries its 95% Wilson interval** as a track under the number, bounds on
  hover. Wilson rather than the normal approximation, because these cells land on 0/10 and 10/10
  routinely and the normal approximation returns impossible bounds at both ends. It is the same
  function `summarize.py` uses, and a test pins the two together.
- **Effect sizes come out of `skill_effect`**, not recomputed here, so the page cannot drift from
  what the analysis scripts publish. A third arm keeps its own label.

Run the tests with `python test_build_index.py` (stdlib, no dependencies).

## Expected input schema

`summary.json` (per the harness):

```jsonc
{
  "task": "structural-brain-extraction-7t",
  "n_runs": 100,
  "provenance": { "image_version": {...}, "opencode_version": {...},
                  "skills_sha": {...}, "tasks_sha": {...} },
  "poolable": true,
  "cells": { "<model>|<arm>": { "n": 10, "passes": 8, "mean": 79.7, "sd": 42.0,
                                "uptake": 0, "not_found_claims": 9, "methods": {"synthstrip": 8} } },
  // keyed "<model>" for a single skill arm, "<model>|<arm>" once there is more than one
  "skill_effect": { "<model>": { "env_only_pass": 8, "env_only_n": 10,
                                 "env_skill_pass": 10, "env_skill_n": 10,
                                 "delta_pp": 20, "ci95_pp": [-11, 51], "fisher_p": 0.47 } }
}
```

`runs.csv` needs at least `model, arm, verdict, score, dice, passed` (extra columns are ignored).
The scoring card is rendered from the grader pack's `rubric.json` (`gates`, `weights`,
`verdict_thresholds`, `prompt`) when `--rubric` is given.

Arm labels `env-only` / `env+skill` (and `baseline` / `skill`) render as **no skill** / **with skill**.

## Example

`examples/` is a worked example and is safe to delete (`git rm -r examples/`) — the tools do not
depend on it.

![7T brain-extraction — 5-model skill sweep](examples/brain-extraction-7t/figure.png)

- `examples/brain-extraction-7t/{summary.json,runs.csv,figure.png}` — a 100-run headless sweep
  (5 open-weight models × 2 conditions × 10) on `structural-brain-extraction-7t`, run on Neurodesk
  Play. **Data courtesy of the benchmark-runner sweep; provenance is recorded inside `summary.json`
  (image, opencode, skills/task SHAs).**
- `examples/brain-extraction-7t/report.html` and `examples/index.html` — generated by the commands
  above.

## Notes

- **Self-contained** = no external hosts. A strict environment (CI artifact viewer, GitHub Pages)
  renders it with no network access.
- **Portable** = paths come from CLI args; nothing is hardcoded.
- The QC gallery is **optional**: pass `--thumbs DIR` of per-run PNGs (rendered on the grading plane
  before the environment is torn down). Without it, the report simply omits the gallery.
- This layer only *reads* grading outputs; the grader itself is `../harness/grade_wrapper.py`.
