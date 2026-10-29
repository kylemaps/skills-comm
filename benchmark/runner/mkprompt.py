#!/usr/bin/env python
"""Build an agent prompt for one benchmark task.

    python mkprompt.py <tasks.json> <task_id>
    python mkprompt.py <tasks.json> <task_id>@<subject>
    python mkprompt.py --prompts-only <tasks.json> > prompts.json

Prints the agent-visible contract only: goal + dataset + required_output.
The grader-only `solution` block is never read, so ground truth cannot leak
into the prompt. The invocation wrapper (wrapper.txt) is appended separately
by run_bench.sh so it stays byte-identical across all arms.

A task graded per subject carries `{subject}` in its prompt and lists its
subjects under `dataset.subjects`. It is run as <task_id>@<subject> (run_ids.py):
`{subject}` is rendered with that subject and the DATASET line names it alone,
so the agent sees one subject and nothing of the others. The pack's `subject_note`
is written for readers of the pack and is never printed.

--prompts-only writes a copy of the task pack holding only each task's `prompt`
block. The run uses that copy, so the solution blocks, grading settings and
descriptions are not on disk while the agent works.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_ids import split  # noqa: E402

PLACEHOLDER = "{subject}"


def prompts_only(data):
    out = {"categories": {}}
    for cat, body in data.get("categories", {}).items():
        tasks = {k: {"prompt": spec.get("prompt", {})}
                 for k, spec in (body.get("tasks") or {}).items()}
        out["categories"][cat] = {"tasks": tasks}
    return out


def render(prompt, task, subject):
    """The printed parts (goal, dataset, subject list, required_output), with the
    subject in place. Refuses an id that does not fit the task: a per-subject task
    without a subject, a subject the task does not list, or a subject for a task
    with one reference."""
    ds = prompt.get("dataset", {})
    listed = list(ds.get("subjects", []) or [])
    goal = prompt.get("goal", "").strip()
    required = prompt.get("required_output", "").strip()
    per_subject = PLACEHOLDER in goal or PLACEHOLDER in required
    if per_subject and not subject:
        sys.exit("task %s is graded per subject: run it as %s@<subject>, one of %s"
                 % (task, task, ", ".join(listed) or "(none listed)"))
    if subject and not per_subject:
        sys.exit("task %s has one reference and takes no subject (%s@%s)"
                 % (task, task, subject))
    if subject:
        if subject not in listed:
            sys.exit("%s is not a subject of %s (it lists %s)"
                     % (subject, task, ", ".join(listed) or "none"))
        goal = goal.replace(PLACEHOLDER, subject)
        required = required.replace(PLACEHOLDER, subject)
        listed = [subject]
    return goal, ds, ",".join(listed), required


if len(sys.argv) == 3 and sys.argv[1] == "--prompts-only":
    json.dump(prompts_only(json.load(open(sys.argv[2], encoding="utf-8"))),
              sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    sys.exit(0)

if len(sys.argv) != 3:
    sys.exit("usage: mkprompt.py <tasks.json> <task_id>[@<subject>]\n"
             "       mkprompt.py --prompts-only <tasks.json>")

tasks_path, run_id = sys.argv[1], sys.argv[2]
try:
    tid, subject = split(run_id)
except ValueError as e:
    sys.exit(str(e))
data = json.load(open(tasks_path, encoding="utf-8"))

for _cat, body in data.get("categories", {}).items():
    for key, spec in (body.get("tasks") or {}).items():
        if key != tid:
            continue
        goal, ds, subs, required = render(spec.get("prompt", {}), tid, subject)
        print("TASK: " + goal + "\n")
        print(
            "DATASET: {} {} (version {}), subject(s) {}. Fetch it yourself.\n".format(
                ds.get("source", ""), ds.get("id", ""), ds.get("version_pin", ""), subs
            )
        )
        print(required + "\n")
        sys.exit(0)

sys.exit("task not found: " + tid)
