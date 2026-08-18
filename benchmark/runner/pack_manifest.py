#!/usr/bin/env python
"""Write the MANIFEST for a results pack: what is in it, and what it rests on.

    python pack_manifest.py <pack_dir> <runs_dir> <task> [<task> ...]

Facts only. Every number here is read from the summaries, nothing is asserted by
hand. Our reading of the results goes in NOTES.md alongside, deliberately kept in
a separate file so the recipient can tell measurement from interpretation.

The provenance block is the point. A pack is only poolable if every run in it
shares one fingerprint -- same image, same agent version, same skill content,
same prompt bytes, same task definition. When that is not true the report should
say so rather than average across it, so this prints every distinct value it
finds and flags any key with more than one.
"""
import glob
import json
import os
import sys

PROV = ["image_version", "opencode_version", "skills_sha", "skills_hash",
        "prompt_hash", "tasks_sha"]


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
    allprov, total, totvalid = {k: set() for k in PROV}, 0, 0
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
        for key, counts in (d.get("provenance") or {}).items():
            for v in counts:
                if v not in ("", "unknown", "none", None):
                    allprov.setdefault(key, set()).add(v)
        for cell, c in (d.get("cells") or {}).items():
            if c.get("n") != 10:
                uneven.append("%s %s n=%d" % (t, cell.replace("|", " "), c.get("n")))
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
    add("One value per key means every run in this pack is directly comparable.")
    add("")
    add("| key | value(s) |")
    add("|---|---|")
    for k in PROV:
        vals = sorted(allprov.get(k) or [])
        flag = "" if len(vals) <= 1 else " ⚠ **differs**"
        add("| `%s` | %s%s |" % (k, ", ".join(vals) or "not recorded", flag))
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


if __name__ == "__main__":
    main()
