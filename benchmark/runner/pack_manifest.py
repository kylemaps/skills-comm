#!/usr/bin/env python
"""Write the MANIFEST for a results pack: what is in it, and what it rests on.

    python pack_manifest.py <pack_dir> <runs_dir> <task> [<task> ...]

Facts only. Every number here is read from the summaries and the per-run
records, nothing is asserted by hand. Our reading of the results goes in
NOTES.md alongside, deliberately kept in a separate file so the recipient can
tell measurement from interpretation.

Provenance is checked PER ARM, PER TASK, which is the only level at which it
means anything. Checked across a whole pack instead, the first version of this
file reported prompt_hash, skills_hash and skills_sha as all "differing" and
looked like a broken experiment -- when in fact three tasks legitimately have
three prompts, two skills legitimately have two content hashes, and skills_sha
is our repo HEAD moving with unrelated harness commits.

A key differing BETWEEN arms is usually the experiment. A key differing WITHIN
one arm means runs in the same condition were not comparable, and that is the
only case worth an alarm.
"""
import glob
import json
import os
import sys
from collections import defaultdict

PROV = ["image_version", "opencode_version", "skills_sha", "skills_hash",
        "prompt_hash", "tasks_sha"]
UNRECORDED = {"", "unknown", "none", None}
ARM_ORDER = {"env-only": 0, "env+skill": 1}


def read_provenance(runs_dir, task):
    """{arm: {key: set(values)}} from every run.json of one task."""
    out = defaultdict(lambda: defaultdict(set))
    for p in sorted(glob.glob(os.path.join(runs_dir, task + "__*", "run.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        arm = rec.get("condition") or "?"
        for k in PROV:
            v = rec.get(k)
            if v not in UNRECORDED:
                out[arm][k].add(str(v))
    return out


def main():
    if len(sys.argv) < 4:
        sys.exit("usage: pack_manifest.py <pack_dir> <runs_dir> <task> [...]")
    pack_dir, runs_dir, tasks = sys.argv[1], sys.argv[2], sys.argv[3:]

    L = []
    add = L.append
    add("# Results pack — nipreps/skills-comm agent benchmark")
    add("")
    add("Autonomous LLM agents run neuroimaging tasks with and without an Agent Skill.")
    add("Graded by the grader packs in `benchmark/graders/`; pass = valid and verdict")
    add("at least `acceptable`. Arms are intent-to-treat: defined by whether the skill")
    add("was **available**, not whether the agent chose to open it.")
    add("")
    add("## Files")
    add("")
    add("| file | what |")
    add("|---|---|")
    for t in tasks:
        add("| `summary_%s.json` | per-cell counts, effects, provenance |" % t)
        add("| `runs_%s.csv` | one row per run |" % t)
    add("| `matrix.txt` / `matrix.csv` | all tasks x models x arms in one grid |")
    add("| `NOTES.md` | our reading of the results — interpretation, not data |")
    add("")

    add("## Runs")
    add("")
    add("| task | runs | valid | excluded |")
    add("|---|---|---|---|")
    total = totvalid = 0
    exclusions, uneven = {}, []
    for t in tasks:
        p = os.path.join(runs_dir, "summary_%s.json" % t)
        if not os.path.exists(p):
            add("| %s | **MISSING** | | |" % t)
            continue
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        add("| `%s` | %d | %d | %d |"
            % (t, d.get("n_runs", 0), d.get("n_valid", 0), d.get("n_excluded", 0)))
        total += d.get("n_runs", 0)
        totvalid += d.get("n_valid", 0)
        for reason, k in (d.get("exclusions") or {}).items():
            if reason:
                exclusions[reason] = exclusions.get(reason, 0) + k
        for cell, c in (d.get("cells") or {}).items():
            if c.get("n") != 10:
                uneven.append("`%s` %s — n=%d" % (t, cell.replace("|", " "), c.get("n")))
    add("| **total** | **%d** | **%d** | |" % (total, totvalid))
    add("")

    if exclusions:
        add("### Excluded, and why")
        add("")
        add("Excluded runs are **our** failures, not agent failures — a gateway outage or")
        add("an agent-runtime fault. Scoring them zero would blame the model for our")
        add("infrastructure. They are detected, excluded, **and re-run**.")
        add("")
        for reason, k in sorted(exclusions.items(), key=lambda kv: -kv[1]):
            add("- %s — %d" % (reason, k))
        add("")

    if uneven:
        add("### Cells that are not n=10")
        add("")
        for u in uneven:
            add("- %s" % u)
        add("")

    add("## Provenance")
    add("")
    add("Checked **within each arm of each task**, which is the only level at which it")
    add("means anything. A key that differs *between* arms is usually the experiment —")
    add("`skills_hash` differs between arms because that is the treatment. A key that")
    add("differs *within* one arm would mean runs in the same condition were not")
    add("comparable, and only that is flagged.")
    add("")
    add("`skills_sha` is our repo HEAD and moves with unrelated harness commits;")
    add("`skills_hash` is the hash of the skill files and is the one that carries")
    add("meaning. `prompt_hash` is per task by construction.")
    add("")
    alarms = []
    for t in tasks:
        prov = read_provenance(runs_dir, t)
        if not prov:
            continue
        arms = sorted(prov, key=lambda a: (ARM_ORDER.get(a, 2), a))
        add("### `%s`" % t)
        add("")
        add("| key | " + " | ".join("`%s`" % a for a in arms) + " |")
        add("|---" * (len(arms) + 1) + "|")
        for k in PROV:
            cells = []
            for a in arms:
                vals = sorted(prov[a].get(k) or [])
                if not vals:
                    cells.append("—")
                elif len(vals) == 1:
                    cells.append("`%s`" % vals[0])
                else:
                    cells.append("⚠ %s" % ", ".join("`%s`" % v for v in vals))
                    alarms.append("%s / %s / %s: %s" % (t, a, k, ", ".join(vals)))
            add("| `%s` | " % k + " | ".join(cells) + " |")
        add("")
    if alarms:
        add("> ⚠ **A key differs within a single arm.** Runs in that condition are not")
        add("> directly comparable and the affected cell should not be pooled:")
        add(">")
        for x in alarms:
            add("> - %s" % x)
        add("")
    else:
        add("No key differs within any arm. Every cell is internally comparable.")
        add("")

    add("## Reading these numbers")
    add("")
    add("- Quote **pass rate**, not the mean. Most cells are bimodal — runs score near")
    add("  0 or near 100 — so a mean of 70 can describe no run that happened.")
    add("- No statistics below **n=5** per arm.")
    add("- Token counts are comparable **within a model only**. Some models on this")
    add("  gateway report prompt-cache reads separately and others fold everything into")
    add("  input, so a cross-model token ranking is an artefact of accounting.")

    with open(os.path.join(pack_dir, "MANIFEST.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("wrote %s/MANIFEST.md" % pack_dir)
    if alarms:
        print("  !! %d within-arm provenance mismatch(es) -- see MANIFEST" % len(alarms))


if __name__ == "__main__":
    main()
