#!/usr/bin/env python
"""Build an agent prompt for one benchmark task.

    python mkprompt.py <tasks.json> <task_id>
    python mkprompt.py --prompts-only <tasks.json> > prompts.json

Prints the agent-visible contract only: goal + dataset + required_output.
The grader-only `solution` block is never read, so ground truth cannot leak
into the prompt. The invocation wrapper (wrapper.txt) is appended separately
by run_bench.sh so it stays byte-identical across all arms.

--prompts-only writes a copy of the task pack holding only each task's `prompt`
block. The run uses that copy, so the solution blocks, grading settings and
descriptions are not on disk while the agent works.
"""
import json
import sys


def prompts_only(data):
    out = {"categories": {}}
    for cat, body in data.get("categories", {}).items():
        tasks = {k: {"prompt": spec.get("prompt", {})}
                 for k, spec in (body.get("tasks") or {}).items()}
        out["categories"][cat] = {"tasks": tasks}
    return out


if len(sys.argv) == 3 and sys.argv[1] == "--prompts-only":
    json.dump(prompts_only(json.load(open(sys.argv[2], encoding="utf-8"))),
              sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    sys.exit(0)

if len(sys.argv) != 3:
    sys.exit("usage: mkprompt.py <tasks.json> <task_id>\n"
             "       mkprompt.py --prompts-only <tasks.json>")

tasks_path, tid = sys.argv[1], sys.argv[2]
data = json.load(open(tasks_path, encoding="utf-8"))

for _cat, body in data.get("categories", {}).items():
    for key, spec in (body.get("tasks") or {}).items():
        if key != tid:
            continue
        prompt = spec.get("prompt", {})
        ds = prompt.get("dataset", {})
        subs = ",".join(ds.get("subjects", []) or [])
        print("TASK: " + prompt.get("goal", "").strip() + "\n")
        print(
            "DATASET: {} {} (version {}), subject(s) {}. Fetch it yourself.\n".format(
                ds.get("source", ""), ds.get("id", ""), ds.get("version_pin", ""), subs
            )
        )
        print(prompt.get("required_output", "").strip() + "\n")
        sys.exit(0)

sys.exit("task not found: " + tid)
