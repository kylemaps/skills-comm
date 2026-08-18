#!/usr/bin/env python
"""Append post-run facts to a run's provenance record.

    python finalize_run.py <run_dir> [exit_code]

`run.json` is written *before* the agent starts, so it cannot know which tools the
agent chose. This reads them back out of the transcript once the run is over:

  tools_loaded   every `module load <tool>/<version>` the agent issued. Matters more
                 than it looks -- we have already seen one model load fsl/6.0.7.14
                 while another loaded fsl/6.0.7.22 in the same experiment.
  methods_used   which brain-extraction method it actually RAN. Not the same thing as
                 the module it loaded: an agent can `module load fsl` and then call
                 `bet`, and one model loaded hd-bet and still fell back to BET.
  not_found_claims  times the agent claimed a tool/module was unavailable
  tokens_*       token accounting, read out of opencode's own SQLite database. The
                 gateway returns a `usage` block on every call and opencode totals it
                 per session, so this works retroactively on runs already on disk --
                 nothing has to be re-run to get it.
  dataset_pin    dataset version the agent pinned itself (e.g. `git checkout 1.1.0`)
  skill_loads    count of opencode's `Skill "<name>"` markers, i.e. did the skill
                 actually load (the ground truth for the with/without arm)
  skills_seen    which skills those were
  exit_code / output_present / end

Safe to re-run: it only adds keys. Also usable to backfill older runs.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

if len(sys.argv) < 2:
    sys.exit("usage: finalize_run.py <run_dir> [exit_code]")

run_dir = sys.argv[1]
rc = sys.argv[2] if len(sys.argv) > 2 else None

rj = os.path.join(run_dir, "run.json")
if not os.path.exists(rj):
    sys.exit(0)

try:
    rec = json.load(open(rj, encoding="utf-8"))
except Exception:
    sys.exit(0)

tr = os.path.join(run_dir, "transcript.txt")
txt = ""
if os.path.exists(tr):
    with open(tr, encoding="utf-8", errors="replace") as fh:
        txt = fh.read()

rec["tools_loaded"] = sorted(set(
    re.findall(r"module load\s+([A-Za-z0-9_.\-]+/[A-Za-z0-9_.]+)", txt)))

# Method detection lives in summarize.py so the two never drift apart. Guarded
# because this runs in the hot path of every run: a missing sibling must not cost
# us the rest of the provenance record.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from summarize import detect_methods, NOT_FOUND_RE
    rec["methods_used"] = detect_methods(txt)
    rec["not_found_claims"] = len(NOT_FOUND_RE.findall(txt))
    # Ordered by first appearance, and counted. The set tells you WHICH tools a
    # run touched; the order tells you what it fell back FROM, and the count is a
    # proxy for how many times it re-extracted after a failed QC. A run that went
    # BET -> SynthStrip is a different story from one that only ever ran
    # SynthStrip, and the set cannot tell them apart.
    from summarize import METHOD_PATTERNS
    seen = []
    total = 0
    for name, pat in METHOD_PATTERNS:
        hits = list(re.finditer(pat, txt, re.I))
        total += len(hits)
        if hits:
            seen.append((hits[0].start(), name))
    rec["method_sequence"] = [n for _, n in sorted(seen)]
    rec["method_invocations"] = total
except Exception:
    pass
# --- effort and behaviour, read back out of the transcript -------------------
# Deliberately format-agnostic. These match on CONTENT (paths, error text, tool
# names) rather than on how opencode frames a tool call, so they survive an agent
# runtime upgrade. Where a signal is a heuristic it is named as one: error_lines
# counts lines that look like failures, which is not the same as counting commands
# that returned non-zero, and pretending otherwise would put a wrong number in a
# table nobody could check.
rec["error_lines"] = len(re.findall(
    "command not found|No such file or directory|Permission denied|Traceback"
    "|is not recognized|cannot access|Segmentation fault", txt, re.I))

# Progressive disclosure is the central design claim of a skill: a lean SKILL.md
# that pulls detail from references/ on demand. Nothing measured whether agents
# ever open those files.
rec["references_opened"] = sorted(set(
    re.findall("references/([a-z0-9_-]+[.]md)", txt, re.I)))

# Every kimi run touched the scheduler, in every arm including no-skill, in a
# container where slurmd cannot start. That is effort spent on a dead end and it
# should be a column, not something we grep for by hand each time we wonder.
rec["scheduler_mentions"] = len(re.findall("slurm|sbatch|squeue|scontrol", txt, re.I))

# Did the agent QC its own output, as both skills instruct?
rec["qc_ran"] = bool(re.search(
    "qc_metrics|afni_outline_qc|qc_mosaic|ai_eval|brain-extraction-qc", txt, re.I))

rec["dataset_pin"] = sorted(set(
    re.findall(r"checkout\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", txt)))
skills = re.findall(r'Skill "([^"]+)"', txt)
rec["skill_loads"] = len(skills)
rec["skills_seen"] = sorted(set(skills))
rec["transcript_lines"] = txt.count("\n")

# --- token accounting from opencode's own database ---------------------------
# opencode records per-session token totals in SQLite, keyed by the working
# directory -- which is exactly our run directory, since run_bench.sh passes
# --dir "$RUN". So this is recoverable for every run ever made, not just future
# ones. Read-only and best-effort: a locked or missing DB must never cost us the
# rest of the provenance record.
#
# `cost` is 0.0 on this gateway (self-hosted vLLM with no pricing configured), so
# tokens are the currency, not dollars.
def _session_row(run_dir):
    # With OPENCODE_ISOLATE=1 each run has its own database under the run
    # directory; otherwise every run shares the one in $HOME. Prefer the local
    # copy so isolated runs still get token accounting.
    candidates = [
        os.environ.get("OPENCODE_DB"),
        os.path.join(run_dir, ".xdg-data", "opencode", "opencode.db"),
        os.path.expanduser("~/.local/share/opencode/opencode.db"),
    ]
    db = next((c for c in candidates if c and os.path.exists(c)), None)
    if db is None:
        return None
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        # A directory can be reused across sweeps, so take the newest session.
        cur = con.execute(
            "select * from session where directory = ? order by time_created desc limit 1",
            (os.path.abspath(run_dir),))
        row = cur.fetchone()
        con.close()
        return row
    except Exception:
        return None


srow = _session_row(run_dir)
if srow is not None:
    keys = srow.keys()
    for k in ("tokens_input", "tokens_output", "tokens_reasoning",
              "tokens_cache_read", "tokens_cache_write", "cost"):
        if k in keys and srow[k] is not None:
            rec[k] = srow[k]
    if "id" in keys:
        rec["session_id"] = srow["id"]
    if "model" in keys and srow["model"]:
        try:
            rec["session_model"] = json.loads(srow["model"]).get("id")
        except Exception:
            rec["session_model"] = str(srow["model"])
    # opencode's own millisecond timestamps beat our transcript-mtime estimate.
    if {"time_created", "time_updated"} <= set(keys):
        try:
            rec["session_duration_s"] = int(
                (srow["time_updated"] - srow["time_created"]) / 1000)
        except Exception:
            pass
    rec["tokens_total"] = sum(
        rec.get(k, 0) or 0 for k in ("tokens_input", "tokens_output"))

task = rec.get("task_id", "")
out = os.path.join(run_dir, "submissions", task, "output.nii.gz")
rec["output_present"] = os.path.exists(out)

# Time to a usable result, which is not the same as run duration. A run that
# produced its mask in 5 minutes and then spent 40 more trying to repair a batch
# scheduler took 45 minutes and was useful after 5. Total runtime reports that as
# slow; this reports it as fast-then-distracted, which is what actually happened
# and what a user would feel.
rec["seconds_to_output"] = None
if rec["output_present"] and rec.get("start"):
    try:
        t0 = datetime.strptime(rec["start"], "%Y-%m-%dT%H:%M:%SZ")
        t0 = t0.replace(tzinfo=timezone.utc).timestamp()
        rec["seconds_to_output"] = max(0, int(os.path.getmtime(out) - t0))
    except Exception:
        pass
if rc is not None:
    try:
        rec["exit_code"] = int(rc)
    except ValueError:
        pass
# The transcript is opencode's stdout redirect, so its last write is the moment the
# agent stopped. Use that rather than "now": this script is re-run during collection
# to backfill fields, and stamping now() there measured time-from-start-to-GRADING,
# not run duration. That produced runs "lasting" 12 hours inside a 4.5 hour sweep,
# and -- because the arms are graded in order -- a fake 25% speedup for the second
# arm. Deriving it from the file makes the value idempotent and repairs old runs.
if os.path.exists(tr):
    rec["end"] = datetime.fromtimestamp(
        os.path.getmtime(tr), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
else:
    rec.setdefault("end", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

with open(rj, "w", encoding="utf-8") as fh:
    json.dump(rec, fh, indent=None)
    fh.write("\n")
