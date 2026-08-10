#!/usr/bin/env python
"""Build an agent prompt for one benchmark task.

    python mkprompt.py <tasks.json> <task_id>

Prints the agent-visible contract only: goal + dataset + required_output.
The grader-only `solution` block is never read, so ground truth cannot leak
into the prompt. The invocation wrapper (wrapper.txt) is appended separately
by run_bench.sh so it stays byte-identical across all arms.
"""
import json
import sys

if len(sys.argv) != 3:
    sys.exit("usage: mkprompt.py <tasks.json> <task_id>")

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
