#!/usr/bin/env python
"""Turn a directory of graded runs into the numbers that go on a slide.

    python summarize.py <runs_dir> <task_id> [--out-dir DIR]

Reads every `<runs_dir>/<task>__*/run.json` (+ `envelope.json` if the run has been
graded) and reports: provenance homogeneity, run validity, per-cell outcomes, the
skill effect, tool choice, and the agent's own decision record. Writes
`summary_<task>.json` and `runs_<task>.csv` alongside.

Pure stdlib -- it must run on the Neurodesk image without installing anything.

---------------------------------------------------------------------------
Two decisions in here are methodological, not cosmetic.
---------------------------------------------------------------------------

1. ARMS ARE DEFINED BY AVAILABILITY, NOT UPTAKE  (intent-to-treat)

   `skills_installed` is written *before* the agent starts: it is the treatment
   assignment. `skills_seen` is observed *after*: it is whether the agent chose to
   open the skill.

   A run where the skill was available and the agent ignored it is a REAL OUTCOME of
   making the skill available, and it counts. Dropping those runs would keep only the
   agents that engaged with the skill -- which selects for attentive runs and biases
   the effect upward. We have already seen non-uptake matter: one model opened the
   skill in 4 of 9 runs and still passed 8 of 10.

   So uptake is REPORTED (as a rate), never used to exclude.

2. EXCLUSION IS ASYMMETRIC

   env-only + skill actually loaded    -> impossible unless two arms overlapped in the
                                          container-global skills dir. Proof of
                                          contamination. EXCLUDE.
   arm label disagrees with
   skills_installed                    -> the harness misassigned the cell. EXCLUDE.
   gateway died and no output          -> the agent never got to attempt the task.
                                          EXCLUDE (see INFRA_ERROR_RES).
   env+skill + skill never opened      -> non-uptake. KEEP.

   Excluding on the last case would be per-protocol analysis. See above.

A run that produced no output because the AGENT failed is a FAILED run, not a missing
one: it scores 0 and is counted. Only our own failures are excluded, and they are
always printed rather than quietly dropped.
"""
import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

# Verdicts that mean "the agent did not deliver a usable result". Everything else is
# treated as a pass. Kept as a set rather than a ladder because the vocabulary is
# owned by the grader pack, not by this script -- unknown verdicts pass through to the
# report verbatim so a vocabulary change is visible rather than silently mis-scored.
#
# The grader pack's bands are: indistinguishable / acceptable >= 60, marginal >= 30,
# invalid = 0. The published rule is "pass = valid and verdict >= acceptable", so
# MARGINAL IS A FAIL. It is listed here because leaving it out silently counted it as
# a pass -- no run has landed in that band yet, so nothing published was affected, but
# our pass rate would have drifted from the grader's the first time one did.
FAIL_VERDICTS = {"invalid", "unacceptable", "marginal", "fail", "failed", "error",
                 "no-output"}

SKILL_NAMES = ("brain-extraction", "brain-extraction-qc")

# Brain-extraction methods, detected from command-form invocations in the transcript.
# The module an agent loads is not the method it runs (`module load fsl` then `bet`),
# so these look for the call, not the module.
#   - the bet pattern's lookbehind rejects `hd-bet` and `deepbet`
#   - ordering is irrelevant; every pattern is tested and results are a set
METHOD_PATTERNS = [
    ("synthstrip", r"(?:mri_)?synthstrip"),
    ("hd-bet",     r"hd[-_]?bet"),
    ("bet",        r"(?<![-\w])bet2?(?![-\w])\s+[-\w./$\"']"),
    ("ants",       r"antsBrainExtraction(?:\.sh)?"),
    ("afni",       r"3dSkullStrip"),
    ("freesurfer", r"mri_watershed|recon-all"),
    ("robex",      r"(?<![-\w])ROBEX(?![-\w])"),
    ("deepbet",    r"deepbet"),
    ("brainchop",  r"brainchop"),
]

NOT_FOUND_RE = re.compile(
    r"(module|tool|command)[^.]{0,25}not (found|available|installed)", re.I)

# Signatures of the HARNESS failing, not the agent. A run killed by the gateway never
# got a chance to attempt the task, so scoring it 0 would blame the model for our
# infrastructure -- the same mistake as the permission-gating bug, where blocked tool
# calls read as "this model can't do 7T".
#
# We have actually seen `Model '' was not found`: the agent had already created its
# directories and written both scripts when the request went out with an empty model
# name. Nothing to do with the model's ability.
#
# Deliberately narrow. Anything ambiguous should stay in and be scored, because
# discarding real failures inflates the pass rate.
INFRA_ERROR_RES = [
    (re.compile(r"Model '' was not found"), "gateway returned empty model name"),
    (re.compile(r"\b429\b|rate.?limit", re.I), "gateway rate limit"),
    (re.compile(r"\b5\d\d\b[^\n]{0,40}(Bad Gateway|Service Unavailable|Gateway Time)",
                re.I), "gateway 5xx"),
    (re.compile(r"(ECONNREFUSED|ENOTFOUND|EBADF: bad file descriptor)"),
     "connection/descriptor failure"),
    # opencode keeps shared state -- one SQLite database and one local server -- and
    # every concurrent run touches the same `project` row. At MAXPAR=8 they collide:
    # runs die in 6-9 seconds having loaded no skill and spent no tokens. Scored as
    # written, five such runs turned a collaborator's skill from 5/5 into "5/10,
    # -50pp, p=0.033". It was never 5/10; five of those runs never started.
    (re.compile(r'Failed query:\s*(insert|update|delete)'), "opencode database contention"),
    (re.compile(r"Error: Session not found"), "opencode session lost"),
    (re.compile(r"Unexpected server error\. Check server logs"), "opencode server error"),
]

# skills_hash is here on purpose: a collaborator's skill drop leaves skills_sha
# unchanged while the skill content is entirely different, so the commit alone would
# silently pool two different experiments.
PROVENANCE_KEYS = ["image_version", "opencode_version", "skills_sha", "skills_hash",
                   "prompt_hash", "tasks_sha"]

# Values meaning "we did not record this", as opposed to a real differing value.
# Runs predating a field carry these, and treating them as a difference would flag
# every newly-added field as a divergence across historical runs.
UNRECORDED = {"", "unknown", "none", None}

# Below this many valid runs in either arm, report the counts but not a delta, CI or
# p-value. A cell that lost nine of ten runs to a gateway outage once produced
# "-100pp, p=0.091" from a single surviving run -- a number that looks like a finding
# and is nothing of the sort. The honest output there is "n too small".
MIN_N_FOR_STATS = 5


# --------------------------------------------------------------------------- stats

def wilson(k, n, z=1.96):
    """Wilson score interval for a proportion. Sane at k=0 and k=n, unlike normal."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((centre - half) / d, (centre + half) / d)


def newcombe(k1, n1, k2, n2):
    """CI for a difference of two proportions (p2 - p1), Newcombe method 10."""
    if n1 == 0 or n2 == 0:
        return (float("nan"), float("nan"))
    l1, u1 = wilson(k1, n1)
    l2, u2 = wilson(k2, n2)
    d = k2 / n2 - k1 / n1
    return (d - math.sqrt((k2 / n2 - l2) ** 2 + (u1 - k1 / n1) ** 2),
            d + math.sqrt((u2 - k2 / n2) ** 2 + (k1 / n1 - l1) ** 2))


def fisher_exact(a, b, c, d):
    """Two-sided Fisher exact p for [[a,b],[c,d]]. Exact -- these tables are tiny."""
    n = a + b + c + d
    if n == 0:
        return float("nan")
    r1, r2, c1 = a + b, c + d, a + c

    def prob(x):
        return (math.comb(r1, x) * math.comb(r2, c1 - x)) / math.comb(n, c1)

    lo, hi = max(0, c1 - r2), min(r1, c1)
    observed = prob(a)
    # Sum every table at least as extreme as the observed one. 1e-9 absorbs the float
    # error that would otherwise drop a table exactly as likely as the observed.
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1)
                        if prob(x) <= observed * (1 + 1e-9)))


def duration_s(start, end):
    """Wall-clock seconds for a run, or None if it was killed before finalizing."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return int((dt.datetime.strptime(end, fmt)
                    - dt.datetime.strptime(start, fmt)).total_seconds())
    except Exception:
        return None


def mean_sd(xs):
    if not xs:
        return (0.0, 0.0)
    m = sum(xs) / len(xs)
    if len(xs) < 2:
        return (m, 0.0)
    return (m, math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1)))


def median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    h = len(s) // 2
    return s[h] if len(s) % 2 else (s[h - 1] + s[h]) / 2


def bimodal(xs, lo=20.0, hi=80.0):
    """True if scores cluster at the extremes with nothing in between.

    Matters because a bimodal cell's mean describes no actual run: a model that scores
    100 seven times and 0 three times has a mean of 70 and has never once scored 70.
    Where this fires, quote the pass rate, not the mean.
    """
    if len(xs) < 3:
        return False
    return not any(lo < x < hi for x in xs) and any(x <= lo for x in xs) and any(x >= hi for x in xs)


# ----------------------------------------------------------------------- load runs

# Lines that MENTION a tool in order to find out whether it exists, rather than to
# run it. A skill that opens with broad capability discovery -- `command -v bet`,
# `module spider afni`, `pip list | grep ants` -- would otherwise register as having
# run every tool it looked for, in every run. That silently destroys the tool-choice
# measurement, which is our headline mechanism, and it destroys it only for the arms
# that discover carefully. Caught when a pilot run showed a model "using" all five
# extraction tools.
DISCOVERY_RE = re.compile(
    r"command\s+-v\b|\bwhich\s+|\btype\s+-[pP]\b|module\s+(spider|avail|-t\s+avail)"
    r"|conda\s+env\s+list|pip\s+list|--help\b|\bls\s+", re.I)


# A markdown filename is documentation, never a command. Our own reference files
# are named after the tools they describe -- references/synthstrip.md,
# references/hd-bet.md, references/afni-3dskullstrip.md -- so an agent that merely
# READ about a tool was being counted as having RUN it.
#
# That error is arm-asymmetric, which is what makes it dangerous rather than
# merely wrong: only the skill arms have a references/ directory to open, so it
# inflates tool counts in exactly the arm where we claim tool use rose. The
# mechanism behind the headline -- "SynthStrip use rose 15/50 to 41/50 with the
# skill" -- runs through this function.
DOC_PATH_RE = re.compile(r"[A-Za-z0-9_./-]*[.]md")


def strip_docs(text):
    """Remove markdown filenames so reading about a tool is not running it."""
    return DOC_PATH_RE.sub(" ", text)

def detect_methods(text):
    """Which extraction methods were actually invoked, per the transcript.

    Discovery lines are dropped first (see DISCOVERY_RE); what remains is scanned
    for command-form invocations. Still a heuristic -- the agent's own ASTRA record
    is the stronger signal where one exists.
    """
    body = strip_docs(text)
    lines = [ln for ln in body.splitlines() if not DISCOVERY_RE.search(ln)]
    body = "\n".join(lines)
    return sorted({name for name, pat in METHOD_PATTERNS
                   if re.search(pat, body, re.I)})


def normalize_tool(s):
    """Map an ASTRA option key/label onto our method vocabulary.

    Order matters: `hd-bet` and `deepbet` both contain "bet", and ASTRA keys look
    like `fsl_bet_6_0_7_22` / `hdbet_2_0_1` / `synthstrip_7_4_1`, so the bare `bet`
    test has to come last.
    """
    s = (s or "").lower()
    if "synthstrip" in s:
        return "synthstrip"
    if re.search(r"hd[-_ ]?bet", s):
        return "hd-bet"
    if "deepbet" in s:
        return "deepbet"
    if "ants" in s:
        return "ants"
    if "3dskullstrip" in s or "afni" in s:
        return "afni"
    if "robex" in s:
        return "robex"
    if "recon-all" in s or "watershed" in s or "freesurfer" in s:
        return "freesurfer"
    if "bet" in s:
        return "bet"
    return None


def parse_astra(run_dir):
    """Read the agent's own decision record, if it wrote one.

    This is a better measurement than grepping the transcript. The transcript tells
    us which binaries were *invoked* -- a run that tried BET and then SynthStrip
    counts in both, which is why the tool-choice rows sum past n. The ASTRA record
    says which tool the agent *committed to*, what it considered and rejected, and
    why. Decided and invoked are different questions and we want both.

    Returns {} when there is no record, so runs predating ASTRA just report nothing.
    """
    p = os.path.join(run_dir, "astra.yaml")
    if not os.path.exists(p):
        return {}
    try:
        import yaml                     # ships with datalad; not worth vendoring
    except ImportError:
        return {"astra_error": "PyYAML not installed"}
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception as exc:
        return {"astra_error": "unparseable: %s" % type(exc).__name__}
    if not isinstance(doc, dict):
        return {"astra_error": "not a mapping"}

    decided, considered = [], set()
    for key, dec in (doc.get("decisions") or {}).items():
        if not isinstance(dec, dict):
            continue
        opts = dec.get("options") or {}
        for okey, opt in opts.items():
            label = (opt or {}).get("label", "") if isinstance(opt, dict) else ""
            t = normalize_tool("%s %s" % (okey, label))
            if t:
                considered.add(t)
        default = dec.get("default")
        if default is not None:
            odef = opts.get(default) or {}
            label = odef.get("label", "") if isinstance(odef, dict) else ""
            t = normalize_tool("%s %s" % (default, label))
            if t:
                decided.append(t)

    # Citations are the clearest signal that the agent grounded its choice in
    # literature rather than asserting it.
    dois = set()
    for ins in (doc.get("prior_insights") or {}).values():
        if not isinstance(ins, dict):
            continue
        for ev in ins.get("evidence") or []:
            if isinstance(ev, dict) and ev.get("doi"):
                dois.add(str(ev["doi"]))

    return {
        "decided_tools": sorted(set(decided)),
        "considered_tools": sorted(considered),
        "astra_citations": len(dois),
        "astra_findings": len(doc.get("findings") or {}),
        "astra_error": "",
    }


def load_run(run_dir, task, tokens_available=False):
    """One run -> a flat record. Never raises; a broken run becomes a failed run."""
    rec = {}
    rj = os.path.join(run_dir, "run.json")
    if os.path.exists(rj):
        try:
            rec = json.load(open(rj, encoding="utf-8"))
        except Exception:
            rec = {}

    base = os.path.basename(run_dir.rstrip("/\\")).split("__")
    r = {
        "dir": run_dir,
        "task": rec.get("task_id") or (base[0] if base else task),
        "model": rec.get("model", base[1] if len(base) > 1 else "?").replace("neurodesk/", ""),
        "arm": rec.get("condition", base[2] if len(base) > 2 else "?"),
        "rep": str(rec.get("repeat", base[3].lstrip("r") if len(base) > 3 else "?")),
        "exit_code": rec.get("exit_code"),
        # Trust run.json, but fall back to looking. This flag decides whether a
        # run with no envelope reads as NOT-GRADED (an unfinished measurement,
        # excluded) or NO-OUTPUT (a genuine failure, scored 0) -- so a missing
        # or stale field silently converts a finished mask into a failure. That
        # is the same family as the bug that reported 7t-nodura as 0/70 with 50
        # masks on disk, and the disk is the ground truth here.
        "output_present": bool(rec.get("output_present")) or bool(
            glob.glob(os.path.join(run_dir, "submissions", "*", "output.nii.gz"))),
        "skill_loads": rec.get("skill_loads", 0),
        "skills_seen": rec.get("skills_seen") or [],
        "skills_installed": rec.get("skills_installed", ""),
        "tools_loaded": rec.get("tools_loaded") or [],
        "dataset_pin": rec.get("dataset_pin") or [],
        "start": rec.get("start", ""),
        "end": rec.get("end", ""),
    }
    for k in PROVENANCE_KEYS:
        r[k] = rec.get(k, "unknown")

    # methods: prefer the recorded field, fall back to the transcript so this works on
    # runs made before finalize_run.py learned to record it (i.e. the current sweep).
    r["methods"] = rec.get("methods_used") or []
    r["not_found_claims"] = rec.get("not_found_claims")
    tr = os.path.join(run_dir, "transcript.txt")
    text = ""
    if os.path.exists(tr):
        with open(tr, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    if not r["methods"]:
        r["methods"] = detect_methods(text)
    if r["not_found_claims"] is None:
        r["not_found_claims"] = len(NOT_FOUND_RE.findall(text))

    # Only treat a harness error as fatal when the run produced nothing. If the agent
    # recovered and still delivered a mask, the run is real and gets scored.
    r["infra_error"] = ""
    for rx, label in INFRA_ERROR_RES:
        if rx.search(text):
            r["infra_error"] = label
            break

    # grading. envelope.json is the grader's output, and it is only written for a
    # run that produced something to score. So a missing envelope means one of two
    # opposite things, and they must not be conflated:
    #
    #   output on disk, no envelope  -> nobody has graded it yet. An unfinished
    #                                   measurement, not a failed run. Excluded.
    #   no output, no envelope       -> the agent delivered nothing. A genuine
    #                                   failure. Scored 0 and COUNTED.
    #
    # Both used to default to NO-OUTPUT, which reported 7t-nodura as 0/70 with 50
    # finished masks sitting on disk. Treating both as NOT-GRADED is the opposite
    # error and is worse: it drops real failures out of the denominator and
    # inflates the pass rate, which is the uneven-denominator problem we are
    # supposed to be fixing.
    ej = os.path.join(run_dir, "envelope.json")
    r["graded"] = os.path.exists(ej)
    r["score"], r["dice"], r["metrics"] = 0.0, None, {}
    r["verdict"] = ("NO-OUTPUT" if r["graded"] or not r["output_present"]
                    else "NOT-GRADED")
    if r["graded"]:
        try:
            e = json.load(open(ej, encoding="utf-8"))
            r["verdict"] = e.get("verdict") or "NO-OUTPUT"
            r["score"] = float(e.get("score") or 0.0)
            r["dice"] = (e.get("detail", {}).get("metrics", {}) or {}).get("dice")
            # Keep the whole metric blob. The grader computes HD95, ASSD, NSD,
            # volume error and core recall on every run and we surfaced only Dice,
            # so a cell could be failing on boundary distance while its Dice looked
            # healthy and nothing said so.
            r["metrics"] = (e.get("detail", {}).get("metrics", {}) or {})
        except Exception:
            pass
    r["passed"] = r["score"] > 0 and str(r["verdict"]).lower() not in FAIL_VERDICTS
    # opencode's own millisecond session timestamps beat our transcript-mtime
    # estimate, so prefer them and keep the estimate as the fallback.
    r["duration_s"] = rec.get("session_duration_s") or duration_s(r["start"], r["end"])
    for k in ("tokens_input", "tokens_output", "tokens_reasoning",
              "tokens_cache_read", "tokens_cache_write", "tokens_total"):
        r[k] = rec.get(k)
    r["session_id"] = rec.get("session_id", "")

    a = parse_astra(run_dir)
    r["decided_tools"] = a.get("decided_tools") or []
    r["considered_tools"] = a.get("considered_tools") or []
    r["astra_citations"] = a.get("astra_citations", 0)
    r["astra_findings"] = a.get("astra_findings", 0)
    r["astra_error"] = a.get("astra_error", "no record")

    # --- validity (see module docstring) ---
    installed = r["skills_installed"] or ""
    seen = set(r["skills_seen"])
    has_skill_installed = any(s in installed for s in SKILL_NAMES)
    has_skill_seen = any(s in seen for s in SKILL_NAMES)

    r["uptake"] = has_skill_seen
    r["exclude_reason"] = ""
    if r["arm"] == "env-only" and has_skill_seen:
        r["exclude_reason"] = "contaminated: env-only run loaded the skill"
    elif r["arm"] == "env-only" and has_skill_installed:
        r["exclude_reason"] = "misassigned: skill installed in env-only run"
    elif r["arm"].startswith("env+skill") and not has_skill_installed:
        r["exclude_reason"] = "misassigned: skill absent in %s run" % r["arm"]
    elif r["infra_error"] and not r["output_present"]:
        r["exclude_reason"] = "harness failure: %s" % r["infra_error"]
    elif (not r["output_present"] and tokens_available
          and not r.get("tokens_total")):
        # An agent that spent no tokens never made a single model call, so it
        # cannot have attempted anything. Chasing error strings one at a time kept
        # missing new failure modes -- three separate ones so far, each producing a
        # confident wrong number. This is the physical version of the same test and
        # does not need to know how the runtime failed.
        #
        # Gated on tokens_available so that a missing or unreadable opencode
        # database cannot silently void an entire experiment.
        r["exclude_reason"] = "harness failure: no model call was ever made"
    elif not r["graded"] and r["output_present"]:
        # Last: a run broken for a known reason is reported as broken, not merely
        # as unscored. What lands here delivered a result that nobody has scored.
        # The output_present guard is load-bearing -- without it, every genuine
        # no-output failure is excluded too and the pass rate inflates.
        r["exclude_reason"] = "not graded yet -- run the grader on this task"
    r["valid"] = not r["exclude_reason"]
    return r


# -------------------------------------------------------------------------- report

def hr(title):
    print("\n=== %s ===" % title)


def run_dates(rs):
    """(first, last) UTC dates for a group of runs, as YYYY-MM-DD, or (None, None).

    Recorded per cell because a leaderboard that spans months needs to say when each
    number was measured. A model retired from the gateway keeps its cells forever -- they
    are evidence of what was true then -- so the date is what distinguishes a standing
    result from a stale one. Date, not timestamp: the extra precision would imply the
    runs in a cell were simultaneous, and they are not.
    """
    ds = sorted(d[:10] for r in rs for d in [r.get("end") or r.get("start") or ""] if d)
    return (ds[0], ds[-1]) if ds else (None, None)


def report(runs, task):
    valid = [r for r in runs if r["valid"]]
    excluded = [r for r in runs if not r["valid"]]

    # -- provenance ---------------------------------------------------------
    hr("PROVENANCE")
    heterogeneous = []
    for k in PROVENANCE_KEYS:
        vals = [r[k] for r in runs]
        known = [v for v in vals if v not in UNRECORDED]
        n_missing = len(vals) - len(known)
        c = Counter(known)
        shown = ", ".join("%s (%d)" % (v, n) for v, n in c.most_common()) or "-"
        # "Not recorded" is not a value that differs -- it is a measurement we did
        # not take. Counting it as a difference makes every field we add later look
        # like a divergence across older runs, which trains people to ignore the
        # warning that matters.
        if n_missing:
            shown += "%snot recorded (%d)" % (", " if known else "", n_missing)
        print("  %-17s %s" % (k, shown))
        if len(c) > 1:
            heterogeneous.append(k)
    if heterogeneous:
        # Two very different situations look identical if you only check "did this
        # field vary". Reusing an older baseline against a new skill arm is a
        # deliberate, defensible design; a single arm built from runs made under two
        # different images is a broken experiment. Separate them.
        arms = sorted({r["arm"] for r in runs})
        across_arms, within_arm = [], []
        for k in heterogeneous:
            per_arm = {a: {r[k] for r in runs
                           if r["arm"] == a and r[k] not in UNRECORDED} for a in arms}
            (across_arms if all(len(v) == 1 for v in per_arm.values())
             else within_arm).append(k)

        if within_arm:
            print("\n  !! NOT POOLABLE: %s vary WITHIN an arm."
                  % ", ".join(within_arm))
            print("     A single arm built from runs made under different conditions")
            print("     is not one experiment. Split or re-run before quoting anything.")

        if across_arms:
            # Changing the skill is the point of the experiment, so skill provenance
            # is expected to differ between arms. The environment is not.
            skill_keys = [k for k in across_arms if k.startswith("skills_")]
            env_keys = [k for k in across_arms if not k.startswith("skills_")]
            print("\n  Arms differ in: %s (each arm internally consistent)."
                  % ", ".join(across_arms))
            for k in across_arms:
                for a in arms:
                    vals = {r[k] for r in runs if r["arm"] == a}
                    print("      %-17s %-10s %s" % (k, a, ", ".join(sorted(vals))))
            if skill_keys and not env_keys:
                print("\n  Only the skill differs between arms -- that is the")
                print("  experiment. Valid, but the baseline is a HISTORICAL control:")
                print("  it was measured at a different time. Say so when reporting.")
            if env_keys:
                print("\n  !! %s differ between arms." % ", ".join(env_keys))
                print("     The environment is supposed to be the controlled variable.")
                print("     Any difference between arms may be the environment, not")
                print("     the skill. This comparison does not stand on its own.")
    else:
        print("\n  OK - every run shares one provenance fingerprint. Poolable.")

    # -- validity -----------------------------------------------------------
    hr("VALIDITY")
    print("  runs found   %4d" % len(runs))
    print("  valid        %4d" % len(valid))
    print("  excluded     %4d" % len(excluded))
    for reason, n in Counter(r["exclude_reason"] for r in excluded).most_common():
        print("      %-58s %d" % (reason, n))
    ungraded = [r for r in runs if not r.get("graded") and r["output_present"]]
    if ungraded:
        print("\n  !! %d of %d run(s) have an output.nii.gz but no envelope.json --"
              % (len(ungraded), len(runs)))
        print("     THE GRADER HAS NOT SCORED THEM. They are excluded, not failed.")
        print("     Run collect_results.sh before treating anything below as a result.")
    if [r for r in excluded if not r["exclude_reason"].startswith("not graded")]:
        print("\n  Excluded runs are OUR failures -- wrong skills in place, or the")
        print("  gateway dying mid-run -- not agent failures. Scoring them 0 would")
        print("  blame the model for our infrastructure. Re-run those cells.")
    infra = [r for r in runs if r["infra_error"]]
    if infra:
        by_cell = Counter("%s/%s" % (r["model"], r["arm"]) for r in infra)
        print("\n  harness errors seen in %d run(s): %s" % (
            len(infra), ", ".join("%s x%d" % (c, n) for c, n in by_cell.most_common())))
        recovered = [r for r in infra if r["output_present"]]
        if recovered:
            print("  %d of those still produced output and ARE scored." % len(recovered))
    uptake_pool = [r for r in valid if r["arm"].startswith("env+skill")]
    if uptake_pool:
        u = sum(1 for r in uptake_pool if r["uptake"])
        print("\n  skill uptake (skill arms): %d/%d runs opened the skill" % (u, len(uptake_pool)))
        print("  Runs that did not open it are KEPT - non-uptake is an outcome, not a defect.")

    # -- per run ------------------------------------------------------------
    hr("RUNS")
    print("  %-14s %-18s %-4s %-17s %7s %6s %6s  %-22s %-3s %s"
          % ("MODEL", "ARM", "REP", "VERDICT", "SCORE", "DICE", "MIN", "METHOD", "OPN", "NF"))
    for r in sorted(runs, key=lambda r: (r["model"], r["arm"], int(re.sub(r"\D", "", r["rep"]) or 0))):
        flag = "" if r["valid"] else "  <-- EXCLUDED"
        print("  %-14s %-18s %-4s %-17s %7s %6s %6s  %-22s %-3s %s%s" % (
            r["model"], r["arm"], r["rep"], r["verdict"],
            "%.2f" % r["score"],
            ("%.3f" % r["dice"]) if isinstance(r["dice"], (int, float)) else "-",
            ("%.1f" % (r["duration_s"] / 60.0)) if r["duration_s"] is not None else "-",
            ",".join(r["methods"])[:22] or "(none)",
            r["skill_loads"], r["not_found_claims"], flag))

    # -- per cell -----------------------------------------------------------
    cells = defaultdict(list)
    for r in valid:
        cells[(r["model"], r["arm"])].append(r)

    hr("CELLS (valid runs only)")
    print("  %-14s %-18s %3s %8s %14s %9s %8s %7s %6s"
          % ("MODEL", "ARM", "N", "PASS", "MEAN+-SD", "MED-SCORE", "MED-MIN",
             "UPTAKE", "NOTFND"))
    for (model, arm), rs in sorted(cells.items()):
        scores = [r["score"] for r in rs]
        m, sd = mean_sd(scores)
        k = sum(1 for r in rs if r["passed"])
        up = sum(1 for r in rs if r["uptake"])
        nf = sum(r["not_found_claims"] for r in rs)
        mins = [r["duration_s"] / 60.0 for r in rs if r["duration_s"] is not None]
        note = "  bimodal" if bimodal(scores) else ""
        print("  %-14s %-18s %3d %8s %14s %9s %8s %7s %6d%s" % (
            model, arm, len(rs), "%d/%d" % (k, len(rs)),
            "%.1f+-%.1f" % (m, sd), "%.1f" % median(scores),
            ("%.1f" % median(mins)) if mins else "-",
            "%d/%d" % (up, len(rs)), nf, note))
    if any(bimodal([r["score"] for r in rs]) for rs in cells.values()):
        print("\n  'bimodal' = every run scored near 0 or near 100, nothing between.")
        print("  For those cells the mean describes no run that happened. Quote PASS.")

    # Duration separates "did the work badly" from "never did the work". A run that
    # finishes in a fraction of the time a passing run takes did not fetch the data,
    # submit a job and wait for it -- whatever it wrote is not a real attempt.
    pmin = [r["duration_s"] / 60.0 for r in valid if r["passed"] and r["duration_s"]]
    fmin = [r["duration_s"] / 60.0 for r in valid if not r["passed"] and r["duration_s"]]
    if pmin:
        print("\n  median runtime: %.1f min passing, %s failing (fastest pass %.1f min)"
              % (median(pmin), ("%.1f min" % median(fmin)) if fmin else "n/a", min(pmin)))
        suspect = [r for r in valid if r["duration_s"] and r["passed"]
                   and r["duration_s"] / 60.0 < 0.25 * median(pmin)]
        if suspect:
            print("  !! %d passing run(s) finished in under a quarter of the median."
                  % len(suspect))
            print("     Check those transcripts before quoting them: %s"
                  % ", ".join(os.path.basename(r["dir"]) for r in suspect[:4]))

    # -- skill effect -------------------------------------------------------
    # There may be more than one skill arm -- two skills measured against one
    # shared baseline is the cheapest way to ask "do skills differ from each
    # other", not just "does a skill help".
    skill_arms = sorted({a for _m, a in cells if a.startswith("env+skill")})
    hr("SKILL EFFECT (intent-to-treat, pass rate)")
    print("  %-14s %-16s %9s %10s %8s %-18s %8s"
          % ("MODEL", "SKILL ARM", "env-only", "with skill", "DELTA",
             "95% CI (delta)", "FISHER p"))
    effects = {}
    for model in sorted({m for m, _ in cells}):
        a = cells.get((model, "env-only"), [])
        for sa in skill_arms:
            b = cells.get((model, sa), [])
            if not a or not b:
                print("  %-14s %-16s %9s %10s   (one arm missing - not comparable)"
                      % (model, sa, "%d runs" % len(a), "%d runs" % len(b)))
                continue
            ka, kb = sum(1 for r in a if r["passed"]), sum(1 for r in b if r["passed"])
            na, nb = len(a), len(b)
            if min(na, nb) < MIN_N_FOR_STATS:
                print("  %-14s %-16s %9s %10s   n too small to compare (lost runs)"
                      % (model, sa, "%d/%d" % (ka, na), "%d/%d" % (kb, nb)))
                continue
            delta = (kb / nb - ka / na) * 100
            lo, hi = newcombe(ka, na, kb, nb)
            p = fisher_exact(ka, na - ka, kb, nb - kb)
            print("  %-14s %-16s %9s %10s %+7.0fpp [%+6.0f, %+6.0f]pp %8.3f" % (
                model, sa, "%d/%d" % (ka, na), "%d/%d" % (kb, nb), delta,
                lo * 100, hi * 100, p))
            effects["%s|%s" % (model, sa)] = {
                "env_only_pass": ka, "env_only_n": na,
                "env_skill_pass": kb, "env_skill_n": nb,
                "delta_pp": round(delta, 1),
                "ci95_pp": [round(lo * 100, 1), round(hi * 100, 1)],
                "fisher_p": round(p, 4)}

    # Skill-vs-skill, when more than one was measured. Comparing each to the
    # baseline separately does not answer "is A better than B" -- that needs the
    # two skill arms tested against each other directly.
    if len(skill_arms) > 1:
        print("\n  Head-to-head between skills (same baseline, same data):")
        print("  %-14s %-16s %-16s %8s %-18s %8s"
              % ("MODEL", "SKILL A", "SKILL B", "B - A", "95% CI", "FISHER p"))
        for model in sorted({m for m, _ in cells}):
            for i, sa in enumerate(skill_arms):
                for sb in skill_arms[i + 1:]:
                    A, B = cells.get((model, sa), []), cells.get((model, sb), [])
                    if not A or not B:
                        continue
                    ka = sum(1 for r in A if r["passed"])
                    kb = sum(1 for r in B if r["passed"])
                    na, nb = len(A), len(B)
                    if min(na, nb) < MIN_N_FOR_STATS:
                        print("  %-14s %-16s %-16s  %s vs %s -- n too small" % (
                            model, sa, sb, "%d/%d" % (ka, na), "%d/%d" % (kb, nb)))
                        continue
                    lo, hi = newcombe(ka, na, kb, nb)
                    p = fisher_exact(ka, na - ka, kb, nb - kb)
                    print("  %-14s %-16s %-16s %+7.0fpp [%+6.0f, %+6.0f]pp %8.3f" % (
                        model, sa, sb, (kb / nb - ka / na) * 100,
                        lo * 100, hi * 100, p))
                    effects["%s|%s_vs_%s" % (model, sa, sb)] = {
                        "a_pass": ka, "a_n": na, "b_pass": kb, "b_n": nb,
                        "delta_pp": round((kb / nb - ka / na) * 100, 1),
                        "ci95_pp": [round(lo * 100, 1), round(hi * 100, 1)],
                        "fisher_p": round(p, 4)}
    if effects:
        print("\n  CI crossing 0 = this experiment cannot tell the arms apart.")
        print("  At N=10/arm the test only detects large effects; a null is")
        print("  'underpowered', not 'no effect'.")
        ceil = sorted({k.split("|")[0] for k, e in effects.items()
                       if "env_only_n" in e
                       and e["env_only_pass"] == e["env_only_n"]})
        if ceil:
            print("\n  CEILING: %s already pass every baseline run." % ", ".join(ceil))
            print("  No headroom exists, so no skill can show a gain here. This is a")
            print("  property of the task, not of the skill.")

    # -- tool choice --------------------------------------------------------
    hr("TOOL CHOICE (valid runs)")
    names = sorted({m for r in valid for m in r["methods"]})
    if not names:
        print("  no methods detected")
    else:
        print("  %-14s %-18s %s" % ("MODEL", "ARM", "  ".join("%-10s" % n for n in names + ["(none)"])))
        for (model, arm), rs in sorted(cells.items()):
            counts = Counter()
            for r in rs:
                if r["methods"]:
                    counts.update(r["methods"])
                else:
                    counts["(none)"] += 1
            print("  %-14s %-18s %s" % (model, arm, "  ".join(
                "%-10s" % (counts.get(n, 0) or "-") for n in names + ["(none)"])))
        print("\n  Counts runs, not invocations; a run that tried two tools counts in both.")
        print("  Detected from command-form invocations in the transcript.")

    # -- token cost ---------------------------------------------------------
    # Outcome alone cannot answer "is this skill worth using". A skill that adds
    # 20pp of pass rate for 3x the tokens is a different proposition from one that
    # adds 20pp for free, and on the motion task the skill multiplied runtime by 14
    # by steering models to heavier tools. Cost belongs next to the effect.
    with_tok = [r for r in valid if r.get("tokens_total")]
    if with_tok:
        hr("TOKEN COST (from opencode's session records)")
        print("  %-14s %-18s %4s %10s %10s %10s %10s"
              % ("MODEL", "ARM", "REC", "IN", "OUT", "REASONING", "CACHE-RD"))
        for (model, arm), rs in sorted(cells.items()):
            have = [r for r in rs if r.get("tokens_total")]
            if not have:
                continue
            def med(k):
                v = [r[k] for r in have if r.get(k) is not None]
                return int(median(v)) if v else 0
            print("  %-14s %-18s %4s %10s %10s %10s %10s" % (
                model, arm, "%d/%d" % (len(have), len(rs)),
                "{:,}".format(med("tokens_input")),
                "{:,}".format(med("tokens_output")),
                "{:,}".format(med("tokens_reasoning")),
                "{:,}".format(med("tokens_cache_read"))))
        print("\n  Median per run. Cache reads are billed differently or not at all")
        print("  depending on the provider; IN + OUT is the honest headline number.")

        # The question the team actually asks: what does the skill cost?
        tok_arms = sorted({a for _m, a in cells if a.startswith("env+skill")})
        rows = []
        for m in sorted({mm for mm, _ in cells}):
            a = [r for r in cells.get((m, "env-only"), []) if r.get("tokens_total")]
            if not a:
                continue
            ta = median([r["tokens_total"] for r in a])
            for sa in tok_arms:
                b = [r for r in cells.get((m, sa), []) if r.get("tokens_total")]
                if b:
                    tb = median([r["tokens_total"] for r in b])
                    rows.append((m, sa, ta, tb, (tb / ta) if ta else float("nan")))
        if rows:
            print("\n  Skill cost, median in+out tokens per run:")
            print("  %-14s %-16s %12s %12s %8s"
                  % ("MODEL", "SKILL ARM", "env-only", "with skill", "RATIO"))
            for m, sa, ta, tb, ratio in rows:
                print("  %-14s %-16s %12s %12s %7.2fx" % (
                    m, sa, "{:,}".format(int(ta)), "{:,}".format(int(tb)), ratio))
            print("\n  Ratios are within-model and trustworthy. Absolute counts are")
            print("  NOT comparable across models -- some report reasoning and cache")
            print("  tokens, others report zero for both. That is accounting, not work.")

        # Cost per RUN is the wrong denominator for "is the skill worth it". A skill
        # that doubles tokens per attempt but takes a model from 2/10 to 10/10 has made
        # correct output far cheaper, and cost-per-run reports that as a 2x regression.
        # Cost per PASSING run is what a user pays for a result they can actually use.
        eff = []
        for m in sorted({mm for mm, _ in cells}):
            for sa in ["env-only"] + tok_arms:
                rs = [r for r in cells.get((m, sa), []) if r.get("tokens_total")]
                if not rs:
                    continue
                spent = sum(r["tokens_total"] for r in rs)
                passes = sum(1 for r in rs if r["passed"])
                eff.append((m, sa, len(rs), passes,
                            (spent / passes) if passes else None))
        if eff:
            print()
            print("  Tokens per PASSING run -- what a usable result actually costs:")
            print("  %-14s %-18s %5s %7s %16s"
                  % ("MODEL", "ARM", "RUNS", "PASSES", "TOKENS/PASS"))
            for m, sa, n, k, per in eff:
                print("  %-14s %-18s %5d %7d %16s"
                      % (m, sa, n, k,
                         "{:,}".format(int(per)) if per else "no passes"))
            print()
            print("  A cell with no passes has no finite cost per result, however few")
            print("  tokens it spent. That is the honest reading of a 0/10 cell.")

            # The wall-clock twin, and the bill for the runs that produced nothing.
            # "Tokens burned on failure" is the number a user feels and no report
            # has ever shown: it is what you pay and throw away.
            print()
            print("  Minutes per passing run, and tokens spent on runs that failed:")
            print("  %-14s %-18s %14s %18s"
                  % ("MODEL", "ARM", "MIN/PASS", "TOKENS WASTED"))
            for m, sa, n, k, _ in eff:
                rs = cells.get((m, sa), [])
                mins = [r["duration_s"] / 60.0 for r in rs
                        if r["duration_s"] is not None]
                wasted = sum(r.get("tokens_total") or 0
                             for r in rs if not r["passed"])
                print("  %-14s %-18s %14s %18s"
                      % (m, sa,
                         ("%.1f" % (sum(mins) / k)) if (k and mins) else "-",
                         "{:,}".format(int(wasted))))

    # -- decided tool (ASTRA) ----------------------------------------------
    with_astra = [r for r in valid if r["decided_tools"]]
    if with_astra:
        hr("DECIDED TOOL (from the agent's own ASTRA record)")
        dnames = sorted({t for r in with_astra for t in r["decided_tools"]})
        print("  %-14s %-18s %4s %s" % ("MODEL", "ARM", "REC",
                                        "  ".join("%-10s" % n for n in dnames)))
        for (model, arm), rs in sorted(cells.items()):
            have = [r for r in rs if r["decided_tools"]]
            c = Counter(t for r in have for t in r["decided_tools"])
            print("  %-14s %-18s %4s %s" % (
                model, arm, "%d/%d" % (len(have), len(rs)),
                "  ".join("%-10s" % (c.get(n, 0) or "-") for n in dnames)))
        print("\n  'Decided' is the option the agent committed to in its decision")
        print("  record; the table above counts every tool it invoked. A run that")
        print("  tried BET and then SynthStrip appears twice there but once here.")

        # Committing to a tool and then running something else is a real failure
        # mode, and it is invisible to either measurement on its own.
        drift = [r for r in with_astra
                 if r["methods"] and not set(r["decided_tools"]) & set(r["methods"])]
        if drift:
            print("\n  !! %d run(s) never invoked the tool they decided on:" % len(drift))
            for r in drift[:6]:
                print("     %-14s %-10s r%-3s decided %-12s ran %s" % (
                    r["model"], r["arm"], r["rep"],
                    ",".join(r["decided_tools"]), ",".join(r["methods"])))

        cited = [r for r in with_astra if r["astra_citations"]]
        if cited:
            print("\n  %d/%d records cite literature for the choice (median %d DOIs)."
                  % (len(cited), len(with_astra),
                     median([r["astra_citations"] for r in cited])))
    else:
        missing = [r for r in valid if r["astra_error"] not in ("", "no record")]
        if missing:
            print("\n  (ASTRA records present but unread: %s)"
                  % Counter(r["astra_error"] for r in missing).most_common(1)[0][0])

    return {
        "task": task,
        "first_run": run_dates(runs)[0],
        "last_run": run_dates(runs)[1],
        "n_runs": len(runs),
        "n_valid": len(valid),
        "n_excluded": len(excluded),
        "exclusions": dict(Counter(r["exclude_reason"] for r in excluded)),
        "provenance": {k: dict(Counter(r[k] for r in runs)) for k in PROVENANCE_KEYS},
        "poolable": not heterogeneous,
        "cells": {
            "%s|%s" % (model, arm): {
                "n": len(rs),
                "first_run": run_dates(rs)[0],
                "last_run": run_dates(rs)[1],
                "passes": sum(1 for r in rs if r["passed"]),
                "mean": round(mean_sd([r["score"] for r in rs])[0], 2),
                "sd": round(mean_sd([r["score"] for r in rs])[1], 2),
                "median": round(median([r["score"] for r in rs]), 2),
                "bimodal": bimodal([r["score"] for r in rs]),
                "median_minutes": round(median([r["duration_s"] / 60.0 for r in rs
                                                if r["duration_s"] is not None]), 1),
                "median_tokens_total": int(median([r["tokens_total"] for r in rs
                                                   if r.get("tokens_total")]) or 0),
                "uptake": sum(1 for r in rs if r["uptake"]),
                "not_found_claims": sum(r["not_found_claims"] for r in rs),
                "methods": dict(Counter(m for r in rs for m in r["methods"])),
                "decided": dict(Counter(t for r in rs for t in r["decided_tools"])),
                "astra_records": sum(1 for r in rs if r["decided_tools"]),
                "decided_not_run": sum(
                    1 for r in rs if r["decided_tools"] and r["methods"]
                    and not set(r["decided_tools"]) & set(r["methods"])),
            } for (model, arm), rs in sorted(cells.items())
        },
        "skill_effect": effects,
    }


CSV_COLS = ["task", "model", "arm", "rep", "valid", "exclude_reason", "infra_error", "verdict",
            "score", "dice", "passed", "uptake", "skill_loads", "methods",
            "tools_loaded", "dataset_pin", "not_found_claims", "exit_code",
            "decided_tools", "considered_tools", "astra_citations", "astra_findings",
            "tokens_input", "tokens_output", "tokens_reasoning",
            "tokens_cache_read", "tokens_total", "session_id",
            "output_present", "image_version", "opencode_version", "skills_sha",
            # skills_hash and prompt_hash are the ones that carry meaning. skills_sha
            # is just our repo HEAD and moves with unrelated harness commits; the
            # content hashes are what prove which skill was tested and that the prompt
            # was byte-identical across arms.
            "skills_hash", "prompt_hash",
            "tasks_sha", "start", "end", "duration_s"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir")
    ap.add_argument("task")
    ap.add_argument("--out-dir", default=None,
                    help="where to write summary.json / runs.csv (default: runs_dir)")
    a = ap.parse_args()

    dirs = sorted(d for d in glob.glob(os.path.join(a.runs_dir, a.task + "__*"))
                  if os.path.isdir(d))
    if not dirs:
        sys.exit("no runs matching %s__* in %s" % (a.task, a.runs_dir))

    # Two passes: establish whether token extraction works for this dataset at
    # all, then classify. Without that check, an unreadable opencode database
    # would mark every no-output run as a harness failure and void the experiment.
    probe = [load_run(d, a.task) for d in dirs]
    tokens_available = any(r.get("tokens_total") for r in probe)
    runs = ([load_run(d, a.task, True) for d in dirs] if tokens_available else probe)
    summary = report(runs, a.task)

    # Task-scoped filenames. These used to be plain summary.json / runs.csv, so
    # collecting a second task silently overwrote the first task's results -- the
    # only copy of a 100-run sweep survived purely because it had been tarred up
    # by hand an hour earlier.
    out = a.out_dir or a.runs_dir
    os.makedirs(out, exist_ok=True)
    sp = os.path.join(out, "summary_%s.json" % a.task)
    cp = os.path.join(out, "runs_%s.csv" % a.task)
    with open(sp, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")
    with open(cp, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in runs:
            row = dict(r)
            for k in ("methods", "tools_loaded", "dataset_pin",
                      "decided_tools", "considered_tools"):
                row[k] = ";".join(row.get(k) or [])
            w.writerow(row)

    print("\nwrote %s" % sp)
    print("wrote %s" % cp)


if __name__ == "__main__":
    main()
