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
except Exception:
    pass
rec["dataset_pin"] = sorted(set(
    re.findall(r"checkout\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", txt)))
skills = re.findall(r'Skill "([^"]+)"', txt)
rec["skill_loads"] = len(skills)
rec["skills_seen"] = sorted(set(skills))
rec["transcript_lines"] = txt.count("\n")

task = rec.get("task_id", "")
out = os.path.join(run_dir, "submissions", task, "output.nii.gz")
rec["output_present"] = os.path.exists(out)
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
