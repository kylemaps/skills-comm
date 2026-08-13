#!/usr/bin/env python
"""Add an unattended-execution clause to a SKILL.md.

    python patch_unattended.py <skill_dir_or_SKILL.md> [...]
    python patch_unattended.py --revert <...>

Why this exists
---------------
Skills written for interactive use tell the agent to stop and ask the user a
question. The benchmark runs with no human attached, so a skill that blocks on
confirmation produces no output and scores 0 -- and the result reads as "this
skill is bad" when the truth is "this skill was never designed to be run this
way". That is the same class of error as the sandbox blocking tool calls and
looking like model incompetence.

One skill under test states the rule as a "Non-Negotiable Interaction Gate" that
"applies even when general agent instructions encourage autonomy", with a
"Mandatory stop" before any script is written. It does have an escape clause for
users who delegate the choice, and the harness wrapper does delegate -- but that
puts a weaker model in the position of resolving "non-negotiable, unless..."
correctly under pressure, and getting it wrong costs the whole run.

What it does NOT do
-------------------
Delete or weaken the gate. The interactive behaviour is the skill author's
design and is right for humans. This prepends an explicit branch for the case
the author did not have in mind, so the agent has a defined action instead of an
apparent contradiction.

Honesty requirements
--------------------
- The clause is fenced by sentinel comments, so `--revert` restores the original
  byte-for-byte and anyone can see what was added.
- A patched skill is NOT the author's skill. run.json records skills_hash, so
  patched and unpatched runs cannot be silently pooled -- summarize.py will
  refuse to treat them as one experiment.
- Report results from a patched skill as such, and send the diff to the author.
"""
import argparse
import os
import sys

BEGIN = "<!-- BENCHMARK-UNATTENDED-CLAUSE:BEGIN -->"
END = "<!-- BENCHMARK-UNATTENDED-CLAUSE:END -->"

CLAUSE = """{begin}
## Unattended execution

This environment may run with **no human available to answer questions**. The
request itself will say so when that is the case.

When it does, any instruction in this skill to stop, wait, ask, or obtain
confirmation is satisfied by doing all of the following instead:

1. Make the decision yourself, using the guidance this skill already gives for
   recommending a tool.
2. State the decision and the reason for it in your response, and record it in
   the run's decision record if one is being kept.
3. Name the alternatives you rejected and why.
4. Continue to completion without pausing.

This is a substitution, not a suspension: the decision still has to be made
deliberately and justified in writing. It applies only when no human is
available. When a human *is* present, follow the interactive instructions below
exactly as written.
{end}
"""


def split_frontmatter(text):
    """Return (frontmatter_including_delimiters, body). Empty first if none."""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    cut = text.find("\n", end + 1)
    if cut == -1:
        return text, ""
    return text[:cut + 1], text[cut + 1:]


def patch(path, revert=False):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()

    has = BEGIN in text and END in text
    if revert:
        if not has:
            return "unchanged (no clause present)"
        i, j = text.index(BEGIN), text.index(END) + len(END)
        out = text[:i] + text[j:].lstrip("\n")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out)
        return "reverted"
    if has:
        return "unchanged (already patched)"

    # Immediately after the frontmatter: the clause has to be read before the
    # gate it qualifies, or a model that stops at the gate never reaches it.
    front, body = split_frontmatter(text)
    clause = CLAUSE.format(begin=BEGIN, end=END)
    out = front + "\n" + clause + "\n" + body.lstrip("\n")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    return "patched"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+",
                    help="SKILL.md files, or directories containing them")
    ap.add_argument("--revert", action="store_true")
    a = ap.parse_args()

    targets = []
    for p in a.paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                if "SKILL.md" in files:
                    targets.append(os.path.join(root, "SKILL.md"))
        elif os.path.isfile(p):
            targets.append(p)
        else:
            print("skip (not found): %s" % p, file=sys.stderr)

    if not targets:
        sys.exit("no SKILL.md found")
    for t in sorted(targets):
        print("%-12s %s" % (patch(t, a.revert), t))
    if not a.revert:
        print("\nThese are now MODIFIED copies. run.json records skills_hash, so")
        print("patched runs will not pool with unpatched ones. Say so when reporting,")
        print("and send the clause to the skill author.")


if __name__ == "__main__":
    main()
