#!/usr/bin/env python3
"""Check what opencode will give the agent, as opencode itself resolves it.

    agent_view.py --skills SKILLS.json --config CONFIG.json --record run.json \
        [--expect a,b] [--enforce]

SKILLS.json is the output of `opencode debug skill`, CONFIG.json of `opencode debug
config`, both run from the agent's working directory with the agent's environment.

Writes `skills_offered` (sorted names, built-in skills excluded) into the run record.
With --enforce, exits 1 unless:
  - the skills offered are exactly --expect (empty for env-only);
  - the resolved config declares no `instructions`, `skills` or `references`.
A file that cannot be parsed records `skills_offered: null` and, with --enforce,
also exits 1.
"""
import argparse
import json
import sys


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skills", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--record", required=True)
    ap.add_argument("--expect", default="")
    ap.add_argument("--enforce", action="store_true")
    a = ap.parse_args()

    expect = sorted(s for s in a.expect.split(",") if s)
    skills = load(a.skills)
    config = load(a.config)

    offered = None
    if isinstance(skills, list):
        offered = sorted(s.get("name", "") for s in skills
                         if isinstance(s, dict) and s.get("location") != "<built-in>")

    rec = load(a.record)
    if isinstance(rec, dict):
        rec["skills_offered"] = offered
        with open(a.record, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
            fh.write("\n")

    problems = []
    if offered is None:
        problems.append("could not read the skills opencode offers (%s)" % a.skills)
    elif offered != expect:
        problems.append("opencode offers skills %s, the arm expects %s" % (offered, expect))
    if not isinstance(config, dict):
        problems.append("could not read opencode's resolved config (%s)" % a.config)
    else:
        for k in ("instructions", "skills", "references"):
            if config.get(k):
                problems.append("resolved config declares %s: %s" % (k, config[k]))

    print("skills offered: %s" % (offered if offered is not None else "unknown"))
    for p in problems:
        print("%s: %s" % ("FAIL" if a.enforce else "note", p))
    return 1 if (a.enforce and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
