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

A blinded run names a case instead, <task_id>@case-NNN: `{subject}` is rendered
with the case id, no DATASET line is printed, and an INPUT line says where the
harness put the scan (input_spec.json: the neutral file name and one sentence
about the scan). The subject list is not consulted; the case's subject is known
only to the steps that hold the gated token.

--prompts-only writes a copy of the task pack holding only each task's `prompt`
block. The run uses that copy, so the solution blocks, grading settings and
descriptions are not on disk while the agent works.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_ids import split, is_case  # noqa: E402

PLACEHOLDER = "{subject}"
INPUT_SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_spec.json")


def input_line(task, case):
    """The INPUT line of a blinded run, from input_spec.json."""
    spec = json.load(open(INPUT_SPEC, encoding="utf-8")).get(task)
    if not spec:
        sys.exit("task %s has no entry in input_spec.json, so it cannot run blinded" % task)
    paths = ["input/%s/%s" % (case, f["as"]) for f in spec["files"]]
    return ("INPUT: %s, already in your working directory at %s. "
            "Use it as it is; do not fetch anything else for it.\n\n"
            % (spec["scan"], " and ".join("`%s`" % p for p in paths)))


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
    ds = dict(prompt.get("dataset", {}))
    listed = list(ds.get("subjects", []) or [])
    goal = prompt.get("goal", "").strip()
    required = prompt.get("required_output", "").strip()
    # Every printed field, so a placeholder anywhere in them marks the task and is
    # rendered; none may remain in what the agent reads.
    printed = [goal, required] + [v for v in ds.values() if isinstance(v, str)]
    per_subject = any(PLACEHOLDER in v for v in printed)
    if per_subject and not subject:
        sys.exit("task %s is graded per subject: run it as %s@<subject>, one of %s"
                 % (task, task, ", ".join(listed) or "(none listed)"))
    if subject and not per_subject:
        sys.exit("task %s has one reference and takes no subject (%s@%s)"
                 % (task, task, subject))
    if subject:
        if not is_case(subject) and subject not in listed:
            sys.exit("%s is not a subject of %s (it lists %s)"
                     % (subject, task, ", ".join(listed) or "none"))
        goal = goal.replace(PLACEHOLDER, subject)
        required = required.replace(PLACEHOLDER, subject)
        ds = {k: v.replace(PLACEHOLDER, subject) if isinstance(v, str) else v
              for k, v in ds.items()}
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
        if subject and is_case(subject):
            source = input_line(tid, subject)
        else:
            source = "DATASET: {} {} (version {}), subject(s) {}. Fetch it yourself.\n\n".format(
                ds.get("source", ""), ds.get("id", ""), ds.get("version_pin", ""), subs)
        text = "TASK: " + goal + "\n\n" + source + required + "\n\n"
        if PLACEHOLDER in text:
            sys.exit("task %s: a %s placeholder is left in the prompt" % (tid, PLACEHOLDER))
        sys.stdout.write(text)
        sys.exit(0)

sys.exit("task not found: " + tid)
