#!/usr/bin/env python3
"""Did any agent invoke a tool that needs a display, and did we ever hear about it?

    python3 display_audit.py ~/bench/runs
    python3 display_audit.py ~/bench/runs --task structural-brain-extraction-7t --show

WHY
The VM runs with DISPLAY unset and no X11 sockets, so a GL tool cannot work there.
The QC path the skills document is matplotlib with the Agg backend, which needs no
display. Neither fact stops an agent inventing its own visual check -- it has a shell.

cluster-explorer found one in a 20-run sample:

    module load fsl/6.0.7.18 && fsleyes render <mask> &>/dev/null \\
      || fslview <mask> 2>&1 | head -5 \\
      || echo "Visual check via fslinfo:"

`&>/dev/null` discards the error, the chain falls through to another GL tool which
also fails, then to an echo, and the run continues and produces a gradeable mask. The
agent believed it had performed a visual check. It had not. Nothing recorded that.

This did not change a score -- grading reads the mask, not a QC image. It is still our
own silent-failure class firing in published data, and it is worth knowing how often.

MENTION IS NOT INVOCATION
The first entry in INFRA_LEDGER.md is this exact mistake: the tool detector matched
`references/synthstrip.md`, so reading about a tool scored as running it, and only
skill arms have a references/ directory. `module avail` prints fsleyes in a column of
available tools, and counting that as use would manufacture GL usage in every run that
listed modules. So this matches command form only, and reports mentions separately and
clearly labelled.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

# Tools that cannot work without a display or a GL context.
#
# (name, case_sensitive). Case matters for the ones that are also acronyms or English
# words. The first version of this list had a bare `afni`, and on the real corpus it
# produced 96 hits of which almost none were invocations: "AFNI prescribes 3dcalc",
# "AFNI version available lacks", "AFNI QUITTs!". Prose writes AFNI, a shell writes
# afni, and matching case-insensitively turned an acronym into a finding.
#
# `suma` and `3dedge3` are dropped. 3dedge3 is a compute tool, not a viewer -- it was
# in the list by association with AFNI rather than for a reason.
GL_TOOLS = [
    ("fsleyes", False),
    ("fslview", False),
    ("freeview", False),
    ("mricrogl", False),
    ("mricron", False),
    ("itksnap", False),
    ("vglrun", False),
    ("tkmedit", False),
    ("tksurfer", False),
    ("afni", True),      # the GUI. Lowercase only -- see above.
    ("suma", True),
]

# Command form: at a command position -- line start, or after && || ; | ( or $( --
# and followed by something that looks like an argument rather than punctuation or
# end of line. This is what separates `fsleyes render foo.nii.gz` from a module
# listing that happens to contain the word.
#
# The tool name is a capture group so the reported line starts at the COMMAND and not
# at the newline the pattern had to consume to prove it was at a command position.
# Without that, most reported lines came back empty.
#
# The trailing (?![\w_]) rejects afni_outline_qc, which is the documented matplotlib
# QC step and the opposite of a GL invocation -- it was the single largest false
# positive, and it was concentrated in one arm, which is exactly how a detector
# manufactures an arm difference.
CMD = r"(?:^|[\n;&|(`]|\$\()\s*(%s)(?![\w_])(?=\s+[-\w./$'\"])"

# How the invocation's failure was handled. Order matters: the more specific
# discard patterns are tested before the general ones.
DISCARDS = [
    ("stderr+stdout to /dev/null", r"&>\s*/dev/null|>\s*/dev/null\s+2>&1|>&\s*/dev/null"),
    ("stderr to /dev/null", r"2>\s*/dev/null"),
    ("|| fallback", r"\|\|"),
    ("piped to head/tail", r"\|\s*(head|tail)\b"),
]

MENTION_CONTEXT = re.compile(r"module\s+(avail|spider|list|whatis)", re.I)


def audit_run(run_dir, task_hint=None):
    rec = {}
    rj = os.path.join(run_dir, "run.json")
    if os.path.exists(rj):
        try:
            rec = json.load(open(rj, encoding="utf-8"))
        except Exception:
            rec = {}
    base = os.path.basename(run_dir.rstrip("/\\")).split("__")
    out = {
        "dir": os.path.basename(run_dir.rstrip("/\\")),
        "task": rec.get("task_id") or (base[0] if base else task_hint),
        "model": rec.get("model", base[1] if len(base) > 1 else "?").replace("neurodesk/", ""),
        "arm": rec.get("condition", base[2] if len(base) > 2 else "?"),
        "rep": str(rec.get("repeat", base[3].lstrip("r") if len(base) > 3 else "?")),
        "invocations": [],
        "mentions": Counter(),
    }
    tr = os.path.join(run_dir, "transcript.txt")
    if not os.path.exists(tr):
        return out
    try:
        txt = open(tr, encoding="utf-8", errors="replace").read()
    except Exception:
        return out

    for name, case_sensitive in GL_TOOLS:
        flags = 0 if case_sensitive else re.I
        pat = re.compile(CMD % re.escape(name), flags)
        for m in pat.finditer(txt):
            # From the start of the CAPTURED tool name to end of line. Slicing from
            # m.start() instead put the newline the pattern had to consume at the
            # front, and most reported lines came back empty.
            eol = txt.find("\n", m.end(1))
            line = txt[m.start(1):eol if eol > 0 else len(txt)].strip()
            if MENTION_CONTEXT.search(line):
                out["mentions"][name] += 1
                continue
            handling = [lbl for lbl, p in DISCARDS if re.search(p, line)]
            out["invocations"].append({
                "tool": name,
                "line": line[:300],
                "discarded": bool(handling),
                "handling": handling,
                "offset": m.start(),
            })
        # Mentions: the tool named anywhere, minus what we counted as an invocation.
        total = len(re.findall(re.escape(name), txt, flags))
        out["mentions"][name] += max(
            0, total - sum(1 for i in out["invocations"] if i["tool"] == name)
            - out["mentions"][name])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs_dir")
    ap.add_argument("--task", action="append", default=[],
                    help="restrict to a task id; repeatable. Default: every run.")
    ap.add_argument("--show", action="store_true", help="print each invocation line")
    ap.add_argument("--json", metavar="PATH")
    a = ap.parse_args()

    pats = ["%s__*" % t for t in a.task] or ["*__*"]
    dirs = sorted({d for p in pats for d in glob.glob(os.path.join(a.runs_dir, p))
                   if os.path.isdir(d)})
    if not dirs:
        raise SystemExit("no run directories under %s" % a.runs_dir)

    rows = [audit_run(d) for d in dirs]
    hits = [r for r in rows if r["invocations"]]
    n_ment = sum(sum(r["mentions"].values()) for r in rows)

    print("%d run directories scanned" % len(rows))
    print("%d mention a display-dependent tool without invoking it  (NOT usage --"
          % sum(1 for r in rows if sum(r["mentions"].values()) and not r["invocations"]))
    print("   %d such mentions in total, mostly `module avail` listings)" % n_ment)
    print("%d INVOKED one\n" % len(hits))

    if not hits:
        print("No agent ran a display-dependent tool. A headless pod changes nothing")
        print("for these runs, and this is measured rather than inferred from the")
        print("skill's intent.")
        return 0

    discarded = [r for r in hits
                 if any(i["discarded"] for i in r["invocations"])]
    print("Of those, %d discarded the error, so the transcript records the attempt"
          % len(discarded))
    print("and not the failure. The agent then proceeded as if it had succeeded.\n")

    by_arm = Counter(r["arm"] for r in hits)
    by_tool = Counter(i["tool"] for r in hits for i in r["invocations"])
    by_handling = Counter(h for r in hits for i in r["invocations"]
                          for h in (i["handling"] or ["error was visible"]))
    print("  by arm     : %s" % dict(by_arm))
    print("  by tool    : %s" % dict(by_tool))
    print("  error shown: %s" % dict(by_handling))

    print("\n  %-38s %-16s %-4s %s" % ("task", "arm", "rep", "tools"))
    for r in sorted(hits, key=lambda r: (r["task"] or "", r["arm"], r["rep"])):
        print("  %-38s %-16s %-4s %s"
              % ((r["task"] or "?")[:38], r["arm"][:16], r["rep"],
                 ",".join(sorted({i["tool"] for i in r["invocations"]}))))
        if a.show:
            for i in r["invocations"]:
                print("        %s%s" % ("[error discarded] " if i["discarded"] else "",
                                        i["line"]))

    print("\nThis is the same shape as the notebook CI that discards")
    print("CalledProcessError: the tool ran, the tool failed, the failure went to")
    print("/dev/null, and the run carried on. It does not move a score here --")
    print("grading reads the mask, not a QC image -- but the agent believed it had")
    print("checked its work and had not, and that belief is inside the measurement.")

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2)
        print("\nwrote %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
