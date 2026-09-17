#!/usr/bin/env python3
"""Of the runs our 45-minute wall killed, which ones had already finished?

    python3 timeout_forensics.py ~/bench/runs structural-brain-extraction-7t-nodura
    python3 timeout_forensics.py ~/bench/runs <task> --tails      # read them yourself

WHY THIS EXISTS
`summarize.py` excludes every exit-124 run, including the ones that wrote a mask,
on the grounds that a killed run's output is an unknown intermediate. Moni's
objection is that this discards real successes: 27 of the 53 visible timeouts wrote
a gradeable mask and 15 of those score 0.94-0.986.

Both can be true. A run killed mid-extraction and a run killed while writing up its
answer are not the same event, and the exclusion is only defensible for the first.
Nobody had looked. This looks.

**Run this BEFORE re-running anything.** A retry overwrites the run directory, and
the transcript is the only evidence. 7t and motion are already gone.

WHAT IT CAN AND CANNOT TELL YOU
The transcript is opencode's stdout with no timestamps, so "after the mask was
written" here means "after the last textual mention of the output path" -- ordering
in the text, not in time. That is good enough to separate a QC-and-summarise tail
from a start-another-extraction tail, which is the distinction that matters, and it
is not good enough to date anything. Where the script is guessing it says GUESS.

VERDICTS
  finished     a mask, written with time to spare, and nothing after it tried to
               make another one. The agent had its answer. Gradeable.
  intermediate a mask, then a further extraction command. The agent was replacing
               it when the wall hit, so the file on disk is not its answer. This is
               the case the exclusion rule was written for.
  marginal     a mask written inside the last MARGIN seconds. Possibly a partial
               write; the grader may be scoring half a file.
  no-output    nothing to grade either way.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# Reuse the sweep's own detectors rather than restating them. The bet pattern in
# particular carries a lookbehind that rejects `hd-bet` and `deepbet`; a second
# copy of these regexes would drift and would be wrong in a way nobody notices.
from summarize import METHOD_PATTERNS, RUN_TIMEOUT_EXIT  # noqa: E402

METHOD_RES = [(n, re.compile(p, re.I)) for n, p in METHOD_PATTERNS]

# Settling on the answer rather than producing it: QC, reporting, cleanup.
DONE_RE = re.compile(
    r"qc_metrics|afni_outline_qc|qc_mosaic|brain-extraction-qc|fslstats|fsleyes"
    r"|dice|jaccard|summary|in conclusion|final(?:ly|ised|ized)?\b"
    r"|the mask (?:is|has been)|successfully (?:created|wrote|extracted|completed)"
    r"|complete[d]?\b", re.I)

# A mask written this close to the wall may be a partial write. One minute is a
# judgement call, not a measurement -- it is roughly how long the slower methods
# here take to write a compressed volume. Override with --margin.
DEFAULT_MARGIN = 60


def tail_after_output(txt, task):
    """The part of the transcript that follows the last mention of the output path.

    Falls back to the whole transcript if the path is never named, which happens
    when the agent writes it from a shell variable. That fallback makes the
    re-extraction test conservative in the right direction: it will call a run
    `intermediate` on evidence from before the write, i.e. it errs towards the
    exclusion we already apply.
    """
    marker = "submissions/%s/output.nii.gz" % task
    i = txt.rfind(marker)
    if i < 0:
        i = txt.rfind("output.nii.gz")
    return (txt[i:], i >= 0)


def classify(run_dir, task, margin_s):
    d = {"dir": os.path.basename(run_dir.rstrip("/\\"))}
    rec = {}
    rj = os.path.join(run_dir, "run.json")
    if os.path.exists(rj):
        try:
            rec = json.load(open(rj, encoding="utf-8"))
        except Exception:
            rec = {}

    base = d["dir"].split("__")
    d["model"] = rec.get("model", base[1] if len(base) > 1 else "?").replace("neurodesk/", "")
    d["arm"] = rec.get("condition", base[2] if len(base) > 2 else "?")
    d["rep"] = str(rec.get("repeat", base[3].lstrip("r") if len(base) > 3 else "?"))
    d["exit_code"] = rec.get("exit_code")

    # The disk is the ground truth. A stale output_present field is exactly how
    # 7t-nodura once reported 0/70 with 50 masks sitting on it.
    outs = glob.glob(os.path.join(run_dir, "submissions", "*", "output.nii.gz"))
    d["output_present"] = bool(rec.get("output_present")) or bool(outs)
    d["output_bytes"] = os.path.getsize(outs[0]) if outs else 0

    d["seconds_to_output"] = rec.get("seconds_to_output")
    d["duration_s"] = rec.get("duration_s") or rec.get("session_duration_s")
    d["margin_s"] = None
    if d["seconds_to_output"] is not None and d["duration_s"]:
        d["margin_s"] = d["duration_s"] - d["seconds_to_output"]

    # What the grader made of it, if it ever saw it. Printed for information only:
    # a score on a run we excluded is the thing under argument, not the answer to it.
    d["score"] = d["verdict_graded"] = None
    env = os.path.join(run_dir, "envelope.json")
    if os.path.exists(env):
        try:
            e = json.load(open(env, encoding="utf-8"))
            d["score"] = e.get("score", e.get("dice"))
            d["verdict_graded"] = e.get("verdict")
        except Exception:
            pass

    txt = ""
    tr = os.path.join(run_dir, "transcript.txt")
    if os.path.exists(tr):
        try:
            txt = open(tr, encoding="utf-8", errors="replace").read()
        except Exception:
            txt = ""
    d["transcript_lines"] = txt.count("\n")

    tail, located = tail_after_output(txt, task)
    d["tail_located"] = located
    d["tail"] = tail
    d["methods_after"] = sorted({n for n, r in METHOD_RES if r.search(tail)})
    d["settling"] = bool(DONE_RE.search(tail))

    if not d["output_present"]:
        d["verdict"] = "no-output"
    elif d["methods_after"]:
        d["verdict"] = "intermediate"
    elif d["margin_s"] is not None and d["margin_s"] < margin_s:
        d["verdict"] = "marginal"
    elif d["margin_s"] is None:
        # No timing recorded, so the one test that would separate marginal from
        # finished cannot run. Do not promote it on the strength of the other two.
        d["verdict"] = "marginal"
        d["note"] = "GUESS: no seconds_to_output recorded, margin untestable"
    else:
        d["verdict"] = "finished"
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs_dir")
    ap.add_argument("tasks", nargs="+")
    ap.add_argument("--margin", type=int, default=DEFAULT_MARGIN,
                    help="seconds before the wall inside which a write is 'marginal'"
                         " (default %d)" % DEFAULT_MARGIN)
    ap.add_argument("--tail-lines", type=int, default=25)
    ap.add_argument("--tails", action="store_true",
                    help="print each run's post-output transcript tail, to read")
    ap.add_argument("--json", metavar="PATH", help="write the full records")
    a = ap.parse_args()

    allrows = []
    for task in a.tasks:
        dirs = sorted(glob.glob(os.path.join(a.runs_dir, "%s__*" % task)))
        rows = []
        for d in dirs:
            rec = {}
            rj = os.path.join(d, "run.json")
            if os.path.exists(rj):
                try:
                    rec = json.load(open(rj, encoding="utf-8"))
                except Exception:
                    pass
            if rec.get("exit_code") != RUN_TIMEOUT_EXIT:
                continue
            rows.append(classify(d, task, a.margin))

        print("\n=== %s %s" % (task, "=" * max(0, 60 - len(task))))
        print("%d run directories, %d killed at exit %d\n"
              % (len(dirs), len(rows), RUN_TIMEOUT_EXIT))
        if not rows:
            continue

        print("  %-22s %-16s %-4s %9s %8s %7s  %-12s %s"
              % ("model", "arm", "rep", "to_output", "margin", "score",
                 "verdict", "extraction after the write"))
        for r in sorted(rows, key=lambda r: (r["arm"], r["model"], r["rep"])):
            print("  %-22s %-16s %-4s %9s %8s %7s  %-12s %s"
                  % (r["model"][:22], r["arm"][:16], r["rep"],
                     r["seconds_to_output"] if r["seconds_to_output"] is not None else "-",
                     r["margin_s"] if r["margin_s"] is not None else "-",
                     ("%.3f" % r["score"]) if isinstance(r["score"], (int, float)) else "-",
                     r["verdict"],
                     ",".join(r["methods_after"]) or ("settling" if r["settling"] else "")))

        print("\n  %s" % dict(Counter(r["verdict"] for r in rows)))

        # The asymmetry is the argument, not the total. On diffusion the wall took
        # 7 baseline runs as zeros and 4 skill-arm runs as passes, which moved the
        # effect from both ends at once. Whether keeping the finished ones is safe
        # depends on how they fall between arms, so print that and not just a count.
        print("\n  by arm:")
        for arm in sorted({r["arm"] for r in rows}):
            c = Counter(r["verdict"] for r in rows if r["arm"] == arm)
            print("    %-18s %s" % (arm, dict(c)))

        nf = [r for r in rows if r["verdict"] == "finished"]
        if nf:
            print("\n  %d run%s gradeable on this reading. Before promoting any of"
                  % (len(nf), " is" if len(nf) == 1 else "s are"))
            print("  them, read the tails: --tails. A regex cannot tell a genuine")
            print("  sign-off from the agent narrating what it was about to do.")
        unloc = [r for r in rows if r["output_present"] and not r["tail_located"]]
        if unloc:
            print("\n  GUESS on %d run(s): the output path is never named in the"
                  % len(unloc))
            print("  transcript, so 'after the write' fell back to the whole file.")

        if a.tails:
            for r in sorted(rows, key=lambda r: (r["arm"], r["model"], r["rep"])):
                print("\n" + "-" * 78)
                print("%s  [%s]%s" % (r["dir"], r["verdict"],
                                      "" if r["tail_located"] else "  (tail not located)"))
                print("-" * 78)
                lines = [l for l in r["tail"].splitlines() if l.strip()]
                for l in lines[-a.tail_lines:]:
                    print("  " + l[:160])

        for r in rows:
            r.pop("tail", None)
            r["task"] = task
        allrows += rows

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(allrows, fh, indent=2)
        print("\nwrote %s" % a.json)


if __name__ == "__main__":
    main()
