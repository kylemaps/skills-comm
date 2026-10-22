#!/usr/bin/env python3
"""Unit tests for dispatch.py, run with a fake Gh (no network, no gh binary).

    python test_dispatch.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dispatch as d


def write(path, text):
    with open(path, "w", newline="\n") as f:
        f.write(text)


def outcome_log(outcome, extra=""):
    return "agent_outcome=%s run=20261022-000000\n%s" % (outcome, extra)


class FakeGh:
    """Canned gh responses: run_list/jobs/job_log return exactly what the test
    set up; dispatch_run/dispatch_assemble record the fields instead of calling
    gh for real."""

    def __init__(self, runs, jobs_by_run, logs_by_job):
        self.runs = runs
        self.jobs_by_run = jobs_by_run
        self.logs_by_job = logs_by_job
        self.dispatched_run = []
        self.dispatched_assemble = []
        self.log_reads = []  # job ids job_log() was actually called for

    def run_list(self, workflow, since=None):
        return list(self.runs)

    def jobs(self, run_id):
        return list(self.jobs_by_run.get(run_id, []))

    def job_log(self, job_id):
        self.log_reads.append(job_id)
        return self.logs_by_job.get(job_id)

    def dispatch_run(self, fields):
        self.dispatched_run.append(dict(fields))

    def dispatch_assemble(self, fields):
        self.dispatched_assemble.append(dict(fields))


def job(name, jid, status="completed", conclusion="success", runner="r1"):
    return {"id": jid, "name": name, "status": status, "conclusion": conclusion,
            "runner_name": runner}


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def order_path(self, lines):
        p = os.path.join(self.tmp, "order.txt")
        write(p, "\n".join(lines) + "\n")
        return p

    def plan_path(self, lines):
        p = os.path.join(self.tmp, "plan.txt")
        write(p, "\n".join(lines) + "\n")
        return p

    def wave(self, reps=10, incomplete=(), lost=()):
        return {"incomplete": set(incomplete), "lost": dict(lost), "reps": reps}

    # (a) fresh wave: nothing has run yet, rep-1 blocks dispatch within lanes.
    def test_fresh_wave_dispatches_within_lanes(self):
        order = d.read_order(self.order_path([
            "1 taskA EGI.kimi-k3 env-only",
            "2 taskB EGI.kimi-k3 env-only",
            "3 taskC EGI.kimi-k3 env-only",
        ]))
        blocks = d.read_plan(self.plan_path([
            "taskA reps=1 cells=1",
            "taskB reps=1 cells=2",
            "taskC reps=1 cells=3",
        ]), order)
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(), lanes=2, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(state["dispatched"]), 2)  # capped at lanes, not 3 tasks

    # (b) Q76: a gateway_cut success job becomes owed and is re-dispatched once,
    # not twice once its re-run is already in flight.
    def test_gateway_cut_redispatched_once_not_twice(self):
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path(["taskA reps=1 cells=1"]), order)
        run1 = {"id": 101, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        j1 = job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 1001)
        gh = FakeGh(runs=[run1], jobs_by_run={101: [j1]},
                   logs_by_job={1001: outcome_log("gateway_cut")})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(state["dispatched"]), 1)
        self.assertIn("1:1", state["owed"])

        # Now a second run exists (the re-run), still queued: the pair is in
        # flight, so a second poll must not dispatch it again.
        run2 = {"id": 102, "status": "queued", "conclusion": None, "created_at": "2026-10-22T00:05:00Z"}
        j2 = job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 1002, status="queued", conclusion=None)
        gh2 = FakeGh(runs=[run1, run2], jobs_by_run={101: [j1], 102: [j2]},
                    logs_by_job={1001: outcome_log("gateway_cut")})
        rc2, state2 = d.poll(gh2, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4, dry_run=True)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(state2["dispatched"]), 0)
        self.assertIn("1:1", state2["in_flight"])

    # (c) Q75: a block nobody has dispatched yet must not read as a stall.
    def test_undispatched_block_is_not_a_stall(self):
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path(["taskA reps=1 cells=1"]), order)
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={})
        # lanes=0: nothing CAN be dispatched this poll, but it is still owed, so
        # this must read as "continue", not "stall".
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=1), lanes=0, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(state["dispatched"], [])
        self.assertIn("1:1", state["owed"])

    # (d) an incomplete model is never dispatched.
    def test_incomplete_model_never_dispatched(self):
        order = d.read_order(self.order_path(["1 taskA EGI.glm-5.3 env-only"]))
        blocks = d.read_plan(self.plan_path(["taskA reps=1 cells=1"]), order)
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={})
        rc, state = d.poll(gh, "o/r", 6, order, blocks,
                           self.wave(reps=1, incomplete=["EGI.glm-5.3"]), lanes=4, dry_run=True)
        self.assertEqual(state["dispatched"], [])
        self.assertEqual(state["owed"], [])
        self.assertEqual(rc, 30)  # nothing owed, nothing in flight: a true stall

    # (e) a cell with 10 valid runs yields one assemble command, and not a second
    # once it shows up in results/.
    def test_complete_cell_assembles_once(self):
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path([]), order)
        run1 = {"id": 201, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        jobs, logs = [], {}
        for rep in range(1, 11):
            jid = 2000 + rep
            jobs.append(job("rep %d taskA neurodesk/EGI.kimi-k3 env-only" % rep, jid))
            logs[jid] = outcome_log("completed")
        gh = FakeGh(runs=[run1], jobs_by_run={201: jobs}, logs_by_job=logs)
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=10), lanes=4, dry_run=True)
        self.assertEqual(len(state["assembled"]), 1)
        self.assertEqual(rc, 0)

        # Publish it (all 10 reps), then poll again: no second assemble.
        # Directory name is run_bench.sh's NAME=: the provider prefix lands IN
        # the model segment ("neurodesk-EGI.kimi-k3"), not before it.
        results = os.path.join(self.tmp, "results")
        for rep in range(1, 11):
            rundir = os.path.join(results, "wave6", "runs", "taskA",
                                  "taskA__neurodesk-EGI.kimi-k3__env-only__r%d" % rep)
            os.makedirs(rundir)
            write(os.path.join(rundir, "run.json"), "{}")
        rc2, state2 = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=10), lanes=4,
                            dry_run=True, results_dir=results)
        self.assertEqual(state2["assembled"], [])
        self.assertEqual(rc2, 10)  # it was the only cell: the wave is complete
        self.assertEqual(state2["published_cells"], [1])

    # (f) a newly fingerprint-changed job (model not yet declared incomplete)
    # stops the poll for a human.
    def test_fingerprint_changed_needs_a_human(self):
        order = d.read_order(self.order_path(["1 taskA EGI.glm-5.3 env-only"]))
        blocks = d.read_plan(self.plan_path(["taskA reps=1 cells=1"]), order)
        run1 = {"id": 301, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        j1 = job("rep 1 taskA neurodesk/EGI.glm-5.3 env-only", 3001)
        log = outcome_log("not_started", "neurodesk/EGI.glm-5.3 is served with fingerprint "
                          "54d6fbaad411; wave 6 declares 8479d72b9276\n")
        gh = FakeGh(runs=[run1], jobs_by_run={301: [j1]}, logs_by_job={3001: log})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4, dry_run=True)
        self.assertEqual(rc, 20)
        self.assertTrue(any("fingerprint-changed" in r for r in state["reasons"]))

    # (g) every declared cell already published: wave complete.
    def test_all_published_exits_10(self):
        order = d.read_order(self.order_path([
            "1 taskA EGI.kimi-k3 env-only",
            "2 taskA EGI.kimi-k3 env+skill",
        ]))
        blocks = d.read_plan(self.plan_path([]), order)
        results = os.path.join(self.tmp, "results")
        for arm in ("env-only", "env+skill"):
            rundir = os.path.join(results, "wave6", "runs", "taskA",
                                  "taskA__neurodesk-EGI.kimi-k3__%s__r1" % arm)
            os.makedirs(rundir)
            write(os.path.join(rundir, "run.json"), "{}")
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4,
                          dry_run=True, results_dir=results)
        self.assertEqual(rc, 10)
        self.assertEqual(state["dispatched"], [])
        self.assertEqual(state["assembled"], [])
        self.assertEqual(gh.log_reads, [])  # published from disk: no log ever read

    # A fingerprint-changed job on a model sweep.json already declares
    # incomplete is acknowledged, not a new stop.
    def test_fingerprint_changed_already_incomplete_is_acknowledged(self):
        order = d.read_order(self.order_path(["1 taskA EGI.glm-5.3 env-only"]))
        blocks = d.read_plan(self.plan_path([]), order)
        run1 = {"id": 401, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        j1 = job("rep 1 taskA neurodesk/EGI.glm-5.3 env-only", 4001)
        log = outcome_log("not_started", "is served with fingerprint 54d6fbaad411\n")
        gh = FakeGh(runs=[run1], jobs_by_run={401: [j1]}, logs_by_job={4001: log})
        rc, state = d.poll(gh, "o/r", 6, order, blocks,
                           self.wave(reps=1, incomplete=["EGI.glm-5.3"]), lanes=4, dry_run=True)
        self.assertEqual(rc, 30)  # excluded model: nothing owed, nothing to do
        self.assertEqual(state["reasons"], [])

    # Q68: GitHub echoes a step's own script text (tagged with this ANSI colour
    # code) before running it; run.yml's "opencode provider config" step's
    # script literally contains "is not resolvable after writing the config"
    # regardless of whether that branch ever executes. clean_log() must drop
    # those lines so matching sees only what actually printed.
    def test_clean_log_strips_script_preview_lines(self):
        raw = "\x1b[36;1mecho \"is not resolvable after writing the config\"\nreal output\n"
        cleaned = d.clean_log(raw)
        self.assertNotIn("is not resolvable", cleaned)
        self.assertIn("real output", cleaned)

    def test_script_echo_text_does_not_misclassify_a_successful_job(self):
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path([]), order)
        run1 = {"id": 501, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        j1 = job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 5001)
        # The infra patterns appear only inside echoed (cyan-tagged) script
        # text; the real printed line further down says the job was fine.
        log = "\n".join([
            "\x1b[36;1mecho \"FAIL: $MODEL is not resolvable after writing the config\"",
            "\x1b[36;1mecho \"Model '' was not found\"",
            "ok   the gateway resolved the model",
            outcome_log("completed"),
        ])
        gh = FakeGh(runs=[run1], jobs_by_run={501: [j1]}, logs_by_job={5001: log})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4, dry_run=True)
        self.assertEqual(state["pairs"]["1:1"], "valid")
        self.assertEqual(state["reasons"], [])

    # A pair already published on disk needs no job and no log read, even when
    # a job happens to exist for it (requirement 2 and the speed requirement 3).
    def test_published_pair_skips_the_log_read(self):
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path([]), order)
        results = os.path.join(self.tmp, "results")
        rundir = os.path.join(results, "wave6", "runs", "taskA",
                              "taskA__neurodesk-EGI.kimi-k3__env-only__r1")
        os.makedirs(rundir)
        write(os.path.join(rundir, "run.json"), "{}")
        run1 = {"id": 601, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        j1 = job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 6001)
        gh = FakeGh(runs=[run1], jobs_by_run={601: [j1]}, logs_by_job={6001: outcome_log("completed")})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4,
                          dry_run=True, results_dir=results)
        self.assertEqual(rc, 10)
        self.assertEqual(state["pairs"]["1:1"], "valid")
        self.assertEqual(gh.log_reads, [])  # the job existed but was never read

    # Requirement 3: a completed job's classification, once read, is reused
    # from --state-in rather than read again.
    def test_job_cache_reused_across_polls(self):
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path([]), order)
        run1 = {"id": 701, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        j1 = job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 7001)
        gh = FakeGh(runs=[run1], jobs_by_run={701: [j1]}, logs_by_job={7001: outcome_log("completed")})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4, dry_run=True)
        self.assertEqual(gh.log_reads, [7001])
        self.assertEqual(state["job_cache"]["7001"], ["valid", "completed"])

        cache = {int(jid): tuple(v) for jid, v in state["job_cache"].items()}
        gh2 = FakeGh(runs=[run1], jobs_by_run={701: [j1]}, logs_by_job={})  # no log to read now
        rc2, state2 = d.poll(gh2, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4,
                             dry_run=True, job_cache=cache)
        self.assertEqual(gh2.log_reads, [])  # reused from the cache, not re-read
        self.assertEqual(state2["pairs"]["1:1"], "valid")

    # A dry-run smoke test against the real wave 6 files, if present; skipped
    # otherwise (not every checkout carries _local).
    def test_dry_run_against_real_wave6_files(self):
        here = os.path.dirname(os.path.abspath(__file__))
        local = os.path.normpath(os.path.join(here, "..", "..", "_local"))
        order_path = os.path.join(local, "wave6_order_seed20261016.txt")
        plan_path = os.path.join(local, "wave6_plan.txt")
        sweep_path = os.path.join(here, "..", "ci", "sweep.json")
        if not (os.path.exists(order_path) and os.path.exists(plan_path)):
            self.skipTest("no _local/wave6 files in this checkout")
        order = d.read_order(order_path)
        blocks = d.read_plan(plan_path, order)
        with open(sweep_path, encoding="utf-8") as f:
            sweep = json.load(f)
        wave_sweep = d.load_wave(sweep, 6)
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={})
        rc, state = d.poll(gh, "neurodesk/skills-benchmark", 6, order, blocks, wave_sweep,
                          lanes=4, dry_run=True)
        self.assertIn(rc, (0, 10, 20, 30))
        self.assertIsInstance(state["dispatched"], list)


if __name__ == "__main__":
    unittest.main()
