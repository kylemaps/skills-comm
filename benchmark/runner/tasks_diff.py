#!/usr/bin/env python3
"""What changed in tasks.json between two grader refs, for the tasks we have published.

    python tasks_diff.py ~/grader-repo 95459b7 origin/feat/benchmark-graders

WHY THIS EXISTS
---------------
The grader pack is pinned. Updating the pin is how a new task becomes runnable, and it is
also how a published result quietly stops being reproducible: tasks.json holds the prompt
the agent was given, so if a prompt changed between the pin and the new ref, runs from
before and after are not the same experiment and must not be pooled.

Between the current pin and the branch we want, tasks.json changed by 165 added and 108
removed lines. Almost all of that is new tasks. "Almost" is not good enough to bet four
published results on, and reading a 273-line diff by eye to decide is exactly the ad-hoc
check that has produced wrong numbers on this project before.

So this answers one question mechanically: did anything the agent sees change, for the
tasks we have already run?

WHAT IT SEPARATES, AND WHY
--------------------------
prompt      AGENT-VISIBLE: goal, dataset, required_output, required_tool. A change here
            means the old and new runs saw different instructions. Old results are not
            comparable to new ones and cannot be fixed by re-grading. BLOCKING.
everything  GRADER-ONLY: ground truth, metric, pass criterion, tool suggestions. A change
else        here means old grades may not reproduce, but the runs themselves are still
            valid and can be re-graded. Worth knowing, not blocking.

Exit status is 1 if any watched task's prompt changed, so this can gate a pin bump in a
script rather than relying on someone reading the output.
"""
import argparse
import json
import subprocess
import sys

# The tasks with published results. A prompt change to any of these invalidates a
# comparison we have already made, so they are the ones worth blocking on. Everything
# else in the pack is free to change.
PUBLISHED = [
    "structural-brain-extraction-7t",
    "structural-brain-extraction-7t-nodura",
    "structural-brain-extraction-motion",
    "structural-brain-extraction-stroke",
]

PROMPT_KEY = "prompt"


def at_ref(repo, ref, path):
    """tasks.json as parsed JSON at a git ref, or exit with why it could not be read."""
    try:
        raw = subprocess.check_output(["git", "-C", repo, "show", "%s:%s" % (ref, path)],
                                      stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        sys.exit("cannot read %s at %s in %s:\n  %s"
                 % (path, ref, repo, e.stderr.decode("utf-8", "replace").strip()))
    return json.loads(raw.decode("utf-8"))


def flatten(doc):
    """{task_id: task_object} across every category.

    Tasks are nested one level under categories. Flattening means a task that MOVED
    category still compares as the same task, which is what we care about -- the agent
    never sees the category.
    """
    out = {}
    for cat in (doc.get("categories") or {}).values():
        for tid, obj in (cat.get("tasks") or {}).items():
            out[tid] = obj
    return out


def canon(obj):
    """Stable text for a subtree, so comparison is not sensitive to key order."""
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False)


def field_diff(a, b):
    """Which top-level keys of a task object differ."""
    return sorted(k for k in set(a) | set(b) if canon(a.get(k)) != canon(b.get(k)))


def main():
    ap = argparse.ArgumentParser(
        description="Did any published task's prompt change between two grader refs?")
    ap.add_argument("repo", help="path to the grader repo, e.g. ~/grader-repo")
    ap.add_argument("old_ref", help="the currently pinned ref")
    ap.add_argument("new_ref", help="the ref you want to pin to")
    ap.add_argument("--path", default="benchmark/tasks.json")
    ap.add_argument("--tasks", default=",".join(PUBLISHED),
                    help="comma-separated task ids to treat as published")
    ap.add_argument("--show", action="store_true",
                    help="print the changed prompt fields in full")
    a = ap.parse_args()

    watched = [t.strip() for t in a.tasks.split(",") if t.strip()]
    old = flatten(at_ref(a.repo, a.old_ref, a.path))
    new = flatten(at_ref(a.repo, a.new_ref, a.path))

    print("%s\n  old %s  %d tasks\n  new %s  %d tasks"
          % (a.path, a.old_ref, len(old), a.new_ref, len(new)))

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    print("\nacross the whole pack: %d added, %d removed, %d in both"
          % (len(added), len(removed), len(set(old) & set(new))))
    for t in added:
        print("  + %s" % t)
    for t in removed:
        print("  - %s" % t)

    print("\npublished tasks (%d):" % len(watched))
    blocking = []
    for t in watched:
        o, n = old.get(t), new.get(t)
        if o is None and n is None:
            print("  ??  %-42s in neither ref -- check the id" % t)
            continue
        if o is None:
            print("  NEW %-42s not in the old ref, so nothing to invalidate" % t)
            continue
        if n is None:
            # Removing a task does not retract results already gathered, but it does mean
            # the task cannot be re-run at the new pin.
            print("  DEL %-42s REMOVED at the new ref" % t)
            blocking.append((t, ["<task removed>"]))
            continue
        changed = field_diff(o, n)
        if not changed:
            print("  ok  %-42s identical" % t)
            continue
        prompt_changed = PROMPT_KEY in changed
        other = [c for c in changed if c != PROMPT_KEY]
        if prompt_changed:
            sub = field_diff(o.get(PROMPT_KEY) or {}, n.get(PROMPT_KEY) or {})
            print("  !!  %-42s PROMPT CHANGED: %s" % (t, ", ".join(sub)))
            blocking.append((t, sub))
            if a.show:
                for k in sub:
                    print("      --- %s @ %s" % (k, a.old_ref))
                    print("      " + canon((o.get(PROMPT_KEY) or {}).get(k)).replace("\n", "\n      "))
                    print("      +++ %s @ %s" % (k, a.new_ref))
                    print("      " + canon((n.get(PROMPT_KEY) or {}).get(k)).replace("\n", "\n      "))
        if other:
            print("  ~   %-42s grader-side only: %s" % (t, ", ".join(other)))

    print()
    if blocking:
        print("BLOCKING: %d published task(s) changed what the agent sees." % len(blocking))
        print("  Runs from before and after this pin are not the same experiment.")
        print("  Do not pool them, and do not bump the pin without re-running:")
        for t, fields in blocking:
            print("    %s  (%s)" % (t, ", ".join(fields)))
        return 1
    print("SAFE: every published task is byte-identical on the agent-visible side.")
    print("  Grader-side changes marked ~ above, if any, mean old grades may not")
    print("  reproduce; the runs themselves are still valid and can be re-graded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
