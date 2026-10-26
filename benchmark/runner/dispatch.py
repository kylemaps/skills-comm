#!/usr/bin/env python3
"""Stateless wave dispatcher: one poll, re-derived live from GitHub and the
committed plan/sweep every time -- no kept run-id ledger, no re-run offset file.
Design: one poll recomputes everything from GitHub and the published record; no state of its own.

    python dispatch.py --sweep sweep.json --wave 6 --plan wave6_plan.txt \
        --order wave6_order_seed20261016.txt [--lanes 4] [--since ISO] \
        [--dry-run] [--state-json out.json] [--results results/]

Plan file: one block per committed line, "<task> reps=<r,r,...> cells=<n,n,...>
[rerunN]" (cell numbers of the order file); "#" lines and blanks are ignored.
Order file: "<cell> <task> <model> <arm>", one line per declared cell; <model> is
the wave id (model_ids.py): EGI.kimi-k3 through the gateway, nrp/glm-5 direct.

A (cell, rep) pair's validity is recomputed every poll from the job whose name
carries it ("rep <n> <task> <model> <arm>"): a job's own "Agent outcome" step
line when present, else its conclusion and the legacy log patterns. A pair is
*owed* when it has no valid run and nothing in flight; a block is dispatched
only when some of its pairs are owed, so a block nobody has touched yet still
counts as owed (not a stall) and a block whose re-run is already queued does
not get a second one.

Exit 0  continue (something dispatched, assembled, or still in flight)
     10 wave complete (every non-incomplete cell is already published)
     20 needs a human (an unknown, lost-candidate, or newly fingerprint-changed
        job; reasons printed and in --state-json)
     30 stall (nothing in flight, nothing owed, nothing to dispatch)
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections import namedtuple

from model_ids import opencode_id, segment, wave_id

DEFAULT_REPO = "neurodesk/skills-benchmark"

JOB_RE = re.compile(r"^rep (\d+) (\S+) (\S+) (\S+)$")
AGENT_OUTCOME_RE = re.compile(r"agent_outcome=(\S+) run=(\S+)")
# The gateway served a different model or serving engine than the wave declared
# (wave_check.py's two wordings: the probe before a run, the fingerprint check after).
FINGERPRINT_RE = re.compile(
    r"is served with fingerprint [0-9a-f]{12}|and the probe recorded [0-9a-f]{12}")
RATE_KILL = "Rate limit exceeded for api_key"
OUTAGE = "Model '' was not found"
PROBE_NONE = "and the probe recorded none"
ROSTER_MISSING = "is not resolvable after writing the config"
UPLOAD_EACCES = "EACCES: permission denied, scandir"
OOM_RE = re.compile(r"OOMKilled|Out of memory|oom-kill|Killed process", re.I)
# Among completed jobs with no "Agent outcome" line, the worst classification
# any single attempt reached wins (see _merge).
_BAD_RANK = {"rerun": 1, "lost-candidate": 2, "fingerprint-changed": 3, "unknown": 4}

Block = namedtuple("Block", "task cells reps line")


# --- plan, order and sweep -----------------------------------------------------

def read_order(path):
    """Cell number -> (task, bare model id, arm), from the wave's order file."""
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        if "\r" in line:
            raise SystemExit("CR in " + path)
        w = line.split()
        if w:
            out[int(w[0])] = (w[1], w[2], w[3])
    return out


def read_plan(path, order):
    """Plan blocks, validated against the order file the way w6_sequence.py did:
    every cell belongs to the block's task, and a (cell, rep) appears twice only
    on a line tagged "rerun"."""
    blocks, pairs = [], set()
    raw_lines = []
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            raw_lines = f.readlines()
    for raw in raw_lines:
        if "\r" in raw:
            raise SystemExit("CR in " + path)
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        w = line.split()
        if len(w) not in (3, 4) or (len(w) == 4 and not re.fullmatch(r"rerun\d*", w[3])):
            raise SystemExit("bad block: " + line)
        task, r, c = w[:3]
        rerun = len(w) == 4
        if not (r.startswith("reps=") and c.startswith("cells=")):
            raise SystemExit("bad block: " + line)
        reps = sorted({int(x) for x in r[5:].split(",")})
        cells = [int(x) for x in c[6:].split(",")]
        if not reps or min(reps) < 1 or len(set(cells)) != len(cells):
            raise SystemExit("bad block: " + line)
        for cell in cells:
            if cell not in order or order[cell][0] != task:
                raise SystemExit("cell %d is not a %s cell: %s" % (cell, task, line))
            for rep in reps:
                if (cell, rep) in pairs and not rerun:
                    raise SystemExit("cell %d rep %d planned twice: %s" % (cell, rep, line))
                pairs.add((cell, rep))
        blocks.append(Block(task=task, cells=cells, reps=reps, line=line))
    return blocks


def load_wave(sweep, wave):
    """incomplete models, acknowledged lost (task/model/arm/rep) keys, and the
    rep count a wave declares. Both sweep dicts may carry a "_comment" key."""
    w = (sweep.get("waves") or {}).get(str(wave))
    if not isinstance(w, dict):
        raise SystemExit("wave %s is not declared in the sweep" % wave)
    return {
        "incomplete": {k for k in (w.get("incomplete") or {}) if not k.startswith("_")},
        "lost": {k: v for k, v in (w.get("lost") or {}).items() if not k.startswith("_")},
        "reps": sweep.get("reps", 10),
    }


# --- GitHub ---------------------------------------------------------------

class Gh:
    """The only place dispatch.py shells out to gh. Thin on purpose: every method
    is one call, so a test can swap this for canned responses."""

    def __init__(self, repo):
        self.repo = repo

    def _run(self, args):
        return subprocess.run(["gh"] + args, capture_output=True, text=True,
                              check=True, encoding="utf-8", errors="replace").stdout

    def run_list(self, workflow, since=None):
        out = json.loads(self._run([
            "run", "list", "-R", self.repo, "--workflow", workflow, "-L", "200",
            "--json", "databaseId,status,conclusion,createdAt"]))
        runs = [{"id": r["databaseId"], "status": r["status"],
                 "conclusion": r["conclusion"], "created_at": r["createdAt"]} for r in out]
        if since:
            runs = [r for r in runs if r["created_at"] >= since]
        return runs

    def jobs(self, run_id):
        data = json.loads(self._run([
            "api", "--paginate",
            "repos/%s/actions/runs/%s/jobs?per_page=100" % (self.repo, run_id)]))
        return [{"id": j["id"], "name": j["name"], "status": j["status"],
                 "conclusion": j["conclusion"], "runner_name": j.get("runner_name") or ""}
                for j in data["jobs"]]

    def job_log(self, job_id):
        """The job's log, or None when it cannot be read. Called only when a
        record field (the Agent outcome step line) is not enough on its own."""
        try:
            out = self._run(["api", "repos/%s/actions/jobs/%s/logs" % (self.repo, job_id)])
        except subprocess.CalledProcessError:
            return None
        return out or None

    def dispatch_run(self, fields):
        self._run(["workflow", "run", "run.yml", "-R", self.repo] + _f_args(fields))

    def dispatch_assemble(self, fields):
        self._run(["workflow", "run", "assemble.yml", "-R", self.repo] + _f_args(fields))


def _f_args(fields):
    out = []
    for k, v in fields.items():
        out += ["-f", "%s=%s" % (k, v)]
    return out


def _gh_command(workflow, repo, fields):
    """The exact `gh workflow run ...` line a dispatch would run."""
    return "gh workflow run %s -R %s %s" % (
        workflow, repo,
        " ".join("-f %s=%s" % (k, shlex.quote(str(v))) for k, v in fields.items()))


# --- classification ---------------------------------------------------------

def clean_log(text):
    """Drop the lines GitHub prints as a PREVIEW of a step's own script, before
    running it: that preview carries the step's whole script text regardless of
    what actually happened, and several of run.yml's own steps print strings
    that collide with the infra patterns below in their script source (e.g. the
    "opencode provider config" step's failure message literally contains "is not
    resolvable after writing the config"). Matching against the raw log reads
    those as every job's real output and misclassifies all of them alike (the
    Q68 trap). GitHub tags a script preview line with this ANSI colour code;
    same filter as w6_watch.py's job_log()."""
    return "\n".join(x for x in text.splitlines() if "[36;1m" not in x)


def classify_job(gh, job):
    """(kind, detail) for a job that has finished: valid, rerun (infra -- re-run
    the same pair), lost-candidate (OOM or no log -- needs a human),
    fingerprint-changed or unknown (both stop the poll). Reads the job log once."""
    log = gh.job_log(job["id"])
    if log:
        log = clean_log(log)
    outcome = None
    if log:
        m = AGENT_OUTCOME_RE.search(log)
        if m:
            outcome = m.group(1)
    if outcome in ("completed", "timeout"):
        return "valid", outcome
    if outcome in ("gateway_cut", "no_model_call"):
        return "rerun", outcome
    if outcome == "not_started":
        # The record carries why (stopped_before_agent), but the printed step
        # line does not forward it -- only fingerprint-changed is distinguishable
        # here, by its own wording elsewhere in the log.
        if log and FINGERPRINT_RE.search(log):
            return "fingerprint-changed", "fingerprint changed"
        return "unknown", "stopped before the agent, reason not in the printed line"
    if outcome is not None:
        return "unknown", "unrecognized agent_outcome " + outcome
    # No "Agent outcome" line at all: an older job, or run.json was never
    # written. Fall back to the job conclusion and the legacy log patterns
    # w6_watch.py read.
    if log is None:
        return "lost-candidate", "job log unreadable"
    if FINGERPRINT_RE.search(log):
        return "fingerprint-changed", "fingerprint changed"
    if RATE_KILL in log:
        return "rerun", "rate limit"
    if OUTAGE in log:
        return "rerun", "outage"
    if PROBE_NONE in log:
        return "rerun", "probe-none"
    if ROSTER_MISSING in log:
        return "rerun", "roster-missing"
    if UPLOAD_EACCES in log:
        return "rerun", "upload-eacces"
    if job["conclusion"] == "cancelled":
        return (("rerun", "never ran") if not job["runner_name"]
                else ("lost-candidate", "cancelled after starting"))
    if OOM_RE.search(log):
        return "lost-candidate", "possible OOM, no run record"
    if job["conclusion"] == "success":
        return "valid", None
    return "unknown", "unclassified failure"


def _merge(old, new):
    """valid beats everything; in-flight beats the rest; among the "needs
    attention" kinds the worse one wins, since several runs may have tried the
    same pair."""
    if old is None:
        return new
    if "valid" in (old, new):
        return "valid"
    if "in-flight" in (old, new):
        return "in-flight"
    return old if _BAD_RANK[old] >= _BAD_RANK[new] else new


# --- dispatch and assemble decisions ----------------------------------------

_REP_SUFFIX_RE = re.compile(r"__r(\d+)$")


def published_reps(results_dir, wave, task, model, arm):
    """Rep numbers this cell already has a run.json for under
    results/wave<n>/runs/<task>/<task>__<segment>__<arm>__r<rep>/ -- read
    straight off disk. A pair found here is valid with no job and no log read at
    all. The segment is the model's opencode id with '/' turned into '-'
    (run_bench.sh's NAME=, model_ids.segment): neurodesk-EGI.kimi-k3, nrp-glm-5."""
    if not results_dir:
        return set()
    d = os.path.join(results_dir, "wave%s" % wave, "runs", task)
    if not os.path.isdir(d):
        return set()
    prefix = "%s__%s__%s__r" % (task, segment(model), arm)
    out = set()
    for name in os.listdir(d):
        if not name.startswith(prefix):
            continue
        m = _REP_SUFFIX_RE.search(name)
        if m and os.path.exists(os.path.join(d, name, "run.json")):
            out.add(int(m.group(1)))
    return out


def next_undispatched_block(task, blocks, pair_status, order, incomplete):
    """The task's first plan block none of whose (incomplete-filtered) pairs has
    ever been attempted. None once every block has at least one job recorded."""
    for b in blocks:
        if b.task != task:
            continue
        cells = [c for c in b.cells if order[c][1] not in incomplete]
        if not cells:
            continue
        pairs = [(c, r) for c in cells for r in b.reps]
        if all(p not in pair_status for p in pairs):
            return Block(task=task, cells=cells, reps=b.reps, line=b.line)
    return None


def pick_retry_block(owed_pairs, order):
    """One re-run dispatch for a task's owed pairs: the lowest owed rep and its
    cells, keeping at most one skill arm (run.yml's own rule for a block). Any
    cells left out by that rule wait for a later poll."""
    if not owed_pairs:
        return None
    rep = min(r for _, r in owed_pairs)
    cells = sorted({c for c, r in owed_pairs if r == rep})
    kept, skill_arm = [], None
    for c in cells:
        arm = order[c][2]
        if arm == "env-only" or skill_arm in (None, arm):
            if arm != "env-only":
                skill_arm = arm
            kept.append(c)
    if not kept:
        return None
    return Block(task=order[kept[0]][0], cells=kept, reps=[rep], line=None)


def build_run_fields(task, cells, order, reps, snapshot, capability, wave):
    cells_arg = " ".join("%s:%s" % (opencode_id(order[c][1]), order[c][2]) for c in cells)
    # A single rep sent bare ("5") is read by run.yml as a COUNT (reps 1-5), not
    # the one rep it names; doubling it ("5 5") forces the list parse instead.
    # Matches w6_sequence.py's dispatch().
    reps_arg = (" ".join(map(str, reps)) if len(reps) > 1
                else "%d %d" % (reps[0], reps[0]))
    return {
        "task": task, "cells": cells_arg, "snapshot": snapshot, "capability": capability,
        "reps": reps_arg, "max_parallel": "1", "label": "benchmark", "wave": str(wave),
    }


def build_assemble_fields(wave, run_ids, task, model, arm):
    return {
        "wave": str(wave), "run_id": ",".join(str(r) for r in sorted(run_ids)),
        "task": task, "model": model, "arm": arm, "commit": "true", "replace": "false",
    }


def _published_cells(order, incomplete, published, lost, reps_total):
    """Cells already fully under results/ -- every rep's directory present, or
    acknowledged lost in sweep.json. Disk and sweep.json only: no job, no gh
    call. Used to skip re-assembling a cell and to decide the wave is done."""
    out = []
    for cell, (task, model, arm) in order.items():
        if model in incomplete:
            continue
        have = published[cell] | {
            rep for rep in range(1, reps_total + 1)
            if "%s__%s__%s__r%d" % (task, model, arm, rep) in lost}
        if len(have) >= reps_total:
            out.append(cell)
    return out


def _state(wave, lanes, pair_status, acked_lost, owed, in_flight, dispatched, assembled,
           reasons, log, job_cache, published_cells):
    return {
        "wave": wave,
        "lanes": lanes,
        "pairs": {"%d:%d" % p: s for p, s in pair_status.items()},
        "acked_lost": sorted("%d:%d" % p for p in acked_lost),
        "owed": sorted("%d:%d" % p for p in owed),
        "in_flight": sorted("%d:%d" % p for p in in_flight),
        "dispatched": dispatched,
        "assembled": assembled,
        "reasons": reasons,
        "log": log,
        # Already-finished jobs are read once, ever: a job's own log never
        # changes, so a classification found here is trusted without a re-read
        # (see --state-in). Keyed by job id, stringified for JSON.
        "job_cache": {str(jid): list(v) for jid, v in job_cache.items()},
        # Cells already under results/ -- not "assembled this poll" (that is
        # `assembled` above), but the wave's actual publication count.
        "published_cells": sorted(published_cells),
    }


def load_job_cache(path):
    """The job_cache a previous poll's --state-json wrote, for --state-in."""
    if not path:
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {int(jid): tuple(v) for jid, v in (data.get("job_cache") or {}).items()}


def poll(gh, repo, wave, order, blocks, wave_sweep, lanes, since=None,
         results_dir=None, dry_run=True, snapshot="", capability="brain-extraction",
         job_cache=None):
    """One stateless poll. Returns (exit_code, state)."""
    log = []
    job_cache = dict(job_cache or {})
    incomplete = wave_sweep["incomplete"]
    lost = wave_sweep["lost"]
    reps_total = wave_sweep["reps"]
    rev = {(t, m, a): c for c, (t, m, a) in order.items()}

    # Read straight off disk first, before any gh call: a published rep needs
    # no job and no log at all. `published` (disk only) drives "already
    # published, don't assemble again" and exit 10; `pair_status[...] =
    # "valid"` folds it into "owed" and "done" the same way a freshly
    # classified valid job would.
    published = {cell: published_reps(results_dir, wave, task, model, arm)
                for cell, (task, model, arm) in order.items()}
    pair_status, task_run_ids = {}, {}
    for cell, reps in published.items():
        for rep in reps:
            pair_status[(cell, rep)] = "valid"

    published_cells = _published_cells(order, incomplete, published, lost, reps_total)
    declared_cells = [c for c, (_, m, _) in order.items() if m not in incomplete]
    if results_dir is not None and len(published_cells) >= len(declared_cells):
        # Nothing left to learn from GitHub: every declared cell is already
        # published. Skip gh run list / jobs entirely -- this is the common
        # "wave complete" poll and must be fast, not one API call per run.
        return 10, _state(wave, lanes, pair_status, set(), set(), set(), [], [], [],
                          log, job_cache, published_cells)

    runs = gh.run_list("run.yml", since=since)
    jobs_by_run = {r["id"]: gh.jobs(r["id"]) for r in runs}

    for run in runs:
        for job in jobs_by_run[run["id"]]:
            m = JOB_RE.match(job["name"])
            if not m:
                continue
            rep, task, model, arm = int(m.group(1)), m.group(2), m.group(3), m.group(4)
            cell = rev.get((task, wave_id(model), arm))
            if cell is None:
                continue  # a job for a cell outside this wave's declared grid
            task_run_ids.setdefault(task, set()).add(run["id"])
            pair = (cell, rep)
            if pair_status.get(pair) == "valid":
                continue  # already known valid from results/: no job, no log
            if job["status"] != "completed":
                pair_status[pair] = _merge(pair_status.get(pair), "in-flight")
                continue
            jid = job["id"]
            if jid in job_cache:
                kind, _ = job_cache[jid]  # a finished job's log never changes
            else:
                kind, detail = classify_job(gh, job)
                job_cache[jid] = (kind, detail)
            pair_status[pair] = _merge(pair_status.get(pair), kind)

    # Acknowledge what sweep.json already declares: a fingerprint-changed job on
    # a model already marked incomplete, or a lost-candidate job already in
    # `lost`, needs no human and no re-run.
    acked_lost, reasons = set(), []
    for pair, status in pair_status.items():
        cell, rep = pair
        task, model, arm = order[cell]
        if status == "fingerprint-changed" and model in incomplete:
            continue
        if status == "lost-candidate" and "%s__%s__%s__r%d" % (task, model, arm, rep) in lost:
            acked_lost.add(pair)
            continue
        if status in ("fingerprint-changed", "lost-candidate", "unknown"):
            reasons.append("cell %d rep %d (%s %s %s): %s" % (cell, rep, task, model, arm, status))

    if reasons:
        return 20, _state(wave, lanes, pair_status, acked_lost, set(), set(), [], [], reasons,
                          log, job_cache, published_cells)

    # In flight is read off every job seen, regardless of the plan: a run
    # dispatched out of band is still a reason not to dispatch another. Owed is
    # bounded to what the committed plan actually declares -- the dispatcher
    # proposes nothing a human has not put in the plan; reps_total only decides
    # when a cell is DONE (below), not what is dispatchable now.
    in_flight = {pair for pair, status in pair_status.items() if status == "in-flight"}
    plan_pairs = {(c, r) for b in blocks for c in b.cells for r in b.reps}
    owed = set()
    for cell, rep in plan_pairs:
        pair = (cell, rep)
        if order[cell][1] in incomplete or pair in in_flight or pair in acked_lost:
            continue
        status = pair_status.get(pair)
        if status in ("valid", "fingerprint-changed", "lost-candidate", "unknown"):
            continue  # done, or acknowledged above: not owed, not re-dispatched
        owed.add(pair)  # status is "rerun", or no job has ever been seen

    # published_cells was computed off disk before any gh call, above; it
    # cannot have changed since (nothing re-reads the directory this poll).
    active_tasks = {order[c][0] for c, _ in in_flight}
    owed_by_task = {}
    for cell, rep in owed:
        owed_by_task.setdefault(order[cell][0], []).append((cell, rep))
    free = max(0, lanes - len(active_tasks))
    ranked = sorted(owed_by_task, key=lambda t: (-len(owed_by_task[t]), t))

    dispatched = []
    for task in [t for t in ranked if t not in active_tasks][:free]:
        block = next_undispatched_block(task, blocks, pair_status, order, incomplete)
        if block is None:
            block = pick_retry_block(owed_by_task[task], order)
        if block is None or not block.cells:
            continue
        fields = build_run_fields(task, block.cells, order, block.reps, snapshot, capability, wave)
        cmd = _gh_command("run.yml", repo, fields)
        dispatched.append(cmd)
        log.append(("DRY " if dry_run else "") + cmd)
        if not dry_run:
            gh.dispatch_run(fields)

    assembled = []
    for cell, (task, model, arm) in order.items():
        if model in incomplete or cell in published_cells:
            continue
        done = sum(1 for rep in range(1, reps_total + 1)
                   if pair_status.get((cell, rep)) == "valid" or (cell, rep) in acked_lost)
        if done < reps_total:
            continue
        run_ids = task_run_ids.get(task)
        if not run_ids:
            continue  # a cell complete by acked-lost reps alone, no run to cite
        fields = build_assemble_fields(wave, run_ids, task, model, arm)
        cmd = _gh_command("assemble.yml", repo, fields)
        assembled.append(cmd)
        log.append(("DRY " if dry_run else "") + cmd)
        if not dry_run:
            gh.dispatch_assemble(fields)

    state = _state(wave, lanes, pair_status, acked_lost, owed, in_flight, dispatched, assembled,
                   [], log, job_cache, published_cells)
    if dispatched or assembled or in_flight:
        return 0, state
    if not owed:
        return 30, state
    return 0, state


# --- CLI ----------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--wave", required=True, type=int)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--order", required=True)
    ap.add_argument("--lanes", type=int)
    ap.add_argument("--since", help="ISO timestamp: only runs created at or after this")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state-json", help="write the full snapshot here")
    ap.add_argument("--state-in", help="a previous --state-json: reuse its job classifications")
    ap.add_argument("--results", help="results/ root, to check what is already published")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--snapshot", default="291f844a")
    ap.add_argument("--capability", default="brain-extraction")
    a = ap.parse_args(argv)

    lanes = a.lanes if a.lanes is not None else int(os.environ.get("WAVE_LANES", "4"))
    with open(a.sweep, encoding="utf-8") as f:
        sweep = json.load(f)
    wave_sweep = load_wave(sweep, a.wave)
    order = read_order(a.order)
    blocks = read_plan(a.plan, order)
    gh = Gh(a.repo)
    job_cache = load_job_cache(a.state_in)

    rc, state = poll(gh, a.repo, a.wave, order, blocks, wave_sweep, lanes,
                     since=a.since, results_dir=a.results, dry_run=a.dry_run,
                     snapshot=a.snapshot, capability=a.capability, job_cache=job_cache)

    declared = sum(1 for _, m, _ in order.values() if m not in wave_sweep["incomplete"])
    for line in state["log"]:
        print(line)
    print("published %d/%d declared cells" % (len(state["published_cells"]), declared))
    if state["reasons"]:
        print("NEEDS A HUMAN:")
        for r in state["reasons"]:
            print("  " + r)
    if a.state_json:
        with open(a.state_json, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
