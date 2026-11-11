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

    def __init__(self, runs, jobs_by_run, logs_by_job, assemble_runs=None, record=False):
        self.runs = runs
        self.jobs_by_run = jobs_by_run
        self.logs_by_job = logs_by_job
        self.assemble_runs = assemble_runs
        self.record = record  # a dispatch creates a queued run, as GitHub does
        self.next_id = 900
        self.dispatched_run = []
        self.dispatched_assemble = []
        self.log_reads = []  # job ids job_log() was actually called for

    def run_list(self, workflow, since=None):
        if workflow == "assemble.yml" and self.assemble_runs is not None:
            return list(self.assemble_runs)
        return list(self.runs)

    def _new_run(self):
        self.next_id += 1
        return {"id": self.next_id, "status": "queued", "conclusion": None,
                "created_at": "2026-11-07T09:00:00Z"}

    def jobs(self, run_id):
        return list(self.jobs_by_run.get(run_id, []))

    def job_log(self, job_id):
        self.log_reads.append(job_id)
        return self.logs_by_job.get(job_id)

    def dispatch_run(self, fields):
        self.dispatched_run.append(dict(fields))
        if self.record:
            self.runs.append(self._new_run())

    def dispatch_assemble(self, fields):
        self.dispatched_assemble.append(dict(fields))
        if self.record:
            self.assemble_runs.append(self._new_run())


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

    # (b2) a run whose plan job has not run yet shows no "rep ..." job: its
    # pairs are invisible, so nothing is dispatched until its jobs appear.
    def test_run_without_its_jobs_yet_holds_every_dispatch(self):
        order = d.read_order(self.order_path([
            "1 taskA EGI.kimi-k3 env-only",
            "2 taskB EGI.kimi-k3 env-only",
        ]))
        blocks = d.read_plan(self.plan_path(["taskA reps=1 cells=1", "taskB reps=1 cells=2"]), order)
        run = {"id": 201, "status": "queued", "conclusion": None, "created_at": "2026-11-07T01:56:43Z"}
        plan_job = job("plan", 2001, status="queued", conclusion=None, runner="")
        gh = FakeGh(runs=[run], jobs_by_run={201: [plan_job]}, logs_by_job={})
        rc, state = d.poll(gh, "o/r", 9, order, blocks, self.wave(reps=1), lanes=6, dry_run=True)
        self.assertEqual(rc, 0)                      # not a stall: something is in flight
        self.assertEqual(state["dispatched"], [])
        self.assertTrue(any(x.startswith("HOLD every block: run 201") for x in state["log"]))

        # A run with no jobs at all (pending in its concurrency group) holds too.
        pending = {"id": 202, "status": "pending", "conclusion": None, "created_at": "2026-11-07T02:02:51Z"}
        gh_p = FakeGh(runs=[pending], jobs_by_run={}, logs_by_job={})
        rc_p, state_p = d.poll(gh_p, "o/r", 9, order, blocks, self.wave(reps=1), lanes=6, dry_run=True)
        self.assertEqual((rc_p, state_p["dispatched"]), (0, []))

        # Once its rep job shows, its pair is in flight and the other block goes.
        rep_job = job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 2002, status="queued", conclusion=None, runner="")
        gh2 = FakeGh(runs=[run], jobs_by_run={201: [job("plan", 2001), rep_job]}, logs_by_job={})
        rc2, state2 = d.poll(gh2, "o/r", 9, order, blocks, self.wave(reps=1), lanes=6, dry_run=True)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(state2["dispatched"]), 1)
        self.assertIn("taskB", state2["dispatched"][0])
        self.assertIn("1:1", state2["in_flight"])

        # A finished run with no jobs (cancelled before it started) holds nothing.
        cancelled = {"id": 203, "status": "completed", "conclusion": "cancelled", "created_at": "2026-11-07T02:02:56Z"}
        gh3 = FakeGh(runs=[cancelled], jobs_by_run={}, logs_by_job={})
        rc3, state3 = d.poll(gh3, "o/r", 9, order, blocks, self.wave(reps=1), lanes=6, dry_run=True)
        self.assertEqual(len(state3["dispatched"]), 2)

    # (b3) a blind run this dispatcher sent is its remembered block: only that
    # task waits, every other lane still fills.
    def test_remembered_blind_run_holds_only_its_task(self):
        order = d.read_order(self.order_path([
            "1 taskA EGI.kimi-k3 env-only",
            "2 taskB EGI.kimi-k3 env-only",
        ]))
        blocks = d.read_plan(self.plan_path(["taskA reps=1 cells=1", "taskB reps=1 cells=2"]), order)
        run = {"id": 301, "status": "queued", "conclusion": None, "created_at": "2026-11-07T05:11:07Z"}
        gh = FakeGh(runs=[run], jobs_by_run={301: [job("plan", 3001, status="queued", conclusion=None, runner="")]},
                    logs_by_job={})
        sent = {"301": {"task": "taskA", "pairs": [[1, 1]]}}
        rc, state = d.poll(gh, "o/r", 9, order, blocks, self.wave(reps=1), lanes=6, dry_run=True, sent_runs=sent)
        self.assertEqual(rc, 0)
        self.assertEqual(len(state["dispatched"]), 1)
        self.assertIn("taskB", state["dispatched"][0])
        self.assertIn("1:1", state["in_flight"])
        self.assertFalse(any(x.startswith("HOLD") for x in state["log"]))
        self.assertEqual(state["sent_runs"], sent)

    # (b4) what a poll sends is remembered by run id, in send order, and forgotten
    # once the run's jobs show.
    def test_sent_runs_are_remembered_then_forgotten(self):
        order = d.read_order(self.order_path([
            "1 taskA EGI.kimi-k3 env-only",
            "2 taskB EGI.kimi-k3 env-only",
        ]))
        blocks = d.read_plan(self.plan_path(["taskA reps=1 cells=1", "taskB reps=1 cells=2"]), order)
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={}, record=True)
        rc, state = d.poll(gh, "o/r", 9, order, blocks, self.wave(reps=1), lanes=6, dry_run=False)
        self.assertEqual(len(gh.dispatched_run), 2)
        sent = state["sent_runs"]
        self.assertEqual(sorted(sent), ["901", "902"])
        self.assertEqual({sent["901"]["task"], sent["902"]["task"]}, {"taskA", "taskB"})

        # Next poll: both runs still blind, both remembered: no HOLD, no second send.
        rc2, state2 = d.poll(gh, "o/r", 9, order, blocks, self.wave(reps=1), lanes=6, dry_run=False, sent_runs=sent)
        self.assertEqual(len(gh.dispatched_run), 2)
        self.assertFalse(any(x.startswith("HOLD") for x in state2["log"]))
        self.assertEqual(sorted(state2["in_flight"]), ["1:1", "2:1"])

        # The taskA run shows its rep job: it is forgotten, its pair is in flight from the job.
        rid_a = [k for k, v in sent.items() if v["task"] == "taskA"][0]
        gh.jobs_by_run[int(rid_a)] = [job("plan", 9001), job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 9002,
                                                              status="in_progress", conclusion=None)]
        rc3, state3 = d.poll(gh, "o/r", 9, order, blocks, self.wave(reps=1), lanes=6, dry_run=False,
                             sent_runs=state2["sent_runs"])
        self.assertNotIn(rid_a, state3["sent_runs"])
        self.assertEqual(len(state3["sent_runs"]), 1)
        self.assertEqual(sorted(state3["in_flight"]), ["1:1", "2:1"])
        self.assertEqual(len(gh.dispatched_run), 2)

    # (b5) an assemble run sent for a cell is not sent again while it is queued
    # or running; once it ends without publishing, the cell is sent again.
    def test_assemble_not_sent_twice_while_queued(self):
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path([]), order)
        run1 = {"id": 201, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        jobs, logs = [], {}
        for rep in range(1, 4):
            jid = 2000 + rep
            jobs.append(job("rep %d taskA neurodesk/EGI.kimi-k3 env-only" % rep, jid))
            logs[jid] = outcome_log("completed")
        gh = FakeGh(runs=[run1], jobs_by_run={201: jobs}, logs_by_job=logs, assemble_runs=[], record=True)
        rc, state = d.poll(gh, "o/r", 9, order, blocks, self.wave(reps=3), lanes=4, dry_run=False)
        self.assertEqual(len(gh.dispatched_assemble), 1)
        self.assertEqual(list(state["sent_assembles"]), ["1"])

        rc2, state2 = d.poll(gh, "o/r", 9, order, blocks, self.wave(reps=3), lanes=4, dry_run=False,
                             sent_assembles=state["sent_assembles"])
        self.assertEqual(len(gh.dispatched_assemble), 1)
        self.assertEqual(rc2, 0)
        self.assertTrue(any(x.startswith("WAIT assemble cell 1") for x in state2["log"]))

        gh.assemble_runs[0]["status"], gh.assemble_runs[0]["conclusion"] = "completed", "failure"
        rc3, state3 = d.poll(gh, "o/r", 9, order, blocks, self.wave(reps=3), lanes=4, dry_run=False,
                             sent_assembles=state2["sent_assembles"])
        self.assertEqual(len(gh.dispatched_assemble), 2)

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

    def test_a_tasks_own_reps_decide_when_its_cell_is_complete(self):
        """Wave reps 10, taskA declares 5: five valid reps complete taskA's cell, while
        taskB's five are half of its ten."""
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only",
                                              "2 taskB EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path([]), order)
        run1 = {"id": 211, "status": "completed", "conclusion": "success", "created_at": "2026-10-22T00:00:00Z"}
        jobs, logs = [], {}
        for task, base in (("taskA", 2100), ("taskB", 2200)):
            for rep in range(1, 6):
                jobs.append(job("rep %d %s neurodesk/EGI.kimi-k3 env-only" % (rep, task), base + rep))
                logs[base + rep] = outcome_log("completed")
        gh = FakeGh(runs=[run1], jobs_by_run={211: jobs}, logs_by_job=logs)
        wave = dict(self.wave(reps=10), task_reps={"taskA": 5})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, wave, lanes=4, dry_run=True)
        self.assertEqual(len(state["assembled"]), 1)
        self.assertIn("-f task=taskA", state["assembled"][0])

        # taskA published with its 5 reps: complete; the wave is not, taskB owes 5.
        results = os.path.join(self.tmp, "results")
        for rep in range(1, 6):
            rundir = os.path.join(results, "wave6", "runs", "taskA",
                                  "taskA__neurodesk-EGI.kimi-k3__env-only__r%d" % rep)
            os.makedirs(rundir)
            write(os.path.join(rundir, "run.json"), "{}")
        rc2, state2 = d.poll(gh, "o/r", 6, order, blocks, wave, lanes=4,
                            dry_run=True, results_dir=results)
        self.assertEqual(state2["published_cells"], [1])
        self.assertEqual(state2["assembled"], [])
        self.assertNotEqual(rc2, 10)

    def test_published_cells_count_each_tasks_own_reps(self):
        order = {1: ("a", "m", "x"), 2: ("b", "m", "x"), 3: ("b", "m", "y")}
        published = {1: {1, 2, 3}, 2: {1, 2, 3}, 3: {1, 2}}
        lost = {"b__m__y__r3": "acknowledged"}
        reps_for = {"a": 3}.get
        got = d._published_cells(order, set(), published, lost, lambda t: reps_for(t, 3))
        self.assertEqual(got, [1, 2, 3])  # cell 3: two published + one acknowledged lost
        got = d._published_cells(order, set(), published, lost, lambda t: reps_for(t, 4))
        self.assertEqual(got, [1])

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

    # An agent's tool output is echoed into the Run step's log, indented; an agent
    # that prints dispatch.py carries AGENT_OUTCOME_RE's own source text. Only
    # the "Agent outcome" step's line, at the start of a log line, is the outcome.
    def test_agent_transcript_quoting_the_outcome_pattern_is_not_the_outcome(self):
        j1 = job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 5001)
        log = "\n".join([
            '2026-11-01T00:22:47.9088745Z   | AGENT_OUTCOME_RE = re.compile(r"agent_outcome=(\\S+) run=(\\S+)")',
            "2026-11-01T00:22:48.0000000Z   | agent_outcome=gateway_cut run=x",
            "2026-11-01T01:15:47.6123672Z agent_outcome=timeout run=taskA__EGI-kimi-k3__env-only__r1",
        ])
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5001: log})
        self.assertEqual(d.classify_job(gh, j1), ("valid", "timeout"))

    # The provider config step could not reach a roster, before the agent: re-run.
    # The same words in an agent's indented transcript are not that step.
    def test_unreachable_roster_is_a_rerun_and_a_transcript_quoting_it_is_not(self):
        j1 = job("rep 6 taskA egi/kimi-k3 env+skill", 5001, conclusion="failure")
        log = "\n".join([
            "2026-11-01T10:19:02.6359277Z FAIL: could not reach https://llm.ai.egi.eu/v1/models:"
            " <urlopen error [Errno 104] Connection reset by peer>",
            "2026-11-01T10:19:02.6489967Z ##[error]Process completed with exit code 1.",
        ])
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5001: log})
        self.assertEqual(d.classify_job(gh, j1), ("rerun", "roster-unreachable"))
        quoted = "2026-11-01T10:30:00.0000000Z   | FAIL: could not reach https://llm.ai.egi.eu/v1/models: x"
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5001: quoted})
        self.assertEqual(d.classify_job(gh, j1), ("unknown", "unclassified failure"))

    def test_finished_run_whose_upload_failed_is_a_rerun_and_a_transcript_quoting_it_is_not(self):
        j1 = job("rep 5 taskA egi/kimi-k3 env+skill", 5002, conclusion="failure")
        outcome = "2026-11-01T04:08:19.1000000Z agent_outcome=completed run=taskA__egi-kimi-k3__env+skill__r5"
        upload = ("2026-11-01T04:08:32.5617505Z ##[error]EACCES: permission denied, scandir"
                  " '/home/runner/_work/_temp/bench/r5/runs/x/containers/sandbox/usr/lib/ssl/private'")
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5002: outcome + "\n" + upload})
        self.assertEqual(d.classify_job(gh, j1), ("rerun", "upload-eacces"))
        quoted = "2026-11-01T04:00:00.0000000Z   | ##[error]EACCES: permission denied, scandir '/x'"
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5002: quoted + "\n" + outcome})
        self.assertEqual(d.classify_job(gh, j1), ("valid", "completed"))

    def test_finished_run_whose_upload_refused_a_path_is_a_rerun(self):
        j1 = job("rep 4 taskA nrp/minimax-m2 env+skill-michele", 6004, conclusion="failure")
        outcome = "2026-11-06T07:09:40.1000000Z agent_outcome=completed run=taskA__nrp-minimax-m2__env+skill-michele__r4"
        upload = ("2026-11-06T07:10:18.2912027Z ##[error]The path for one of the files in artifact is not valid:"
                  " /runs/x/r4/mamba/envs/afnifull/lib/a:b")
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={6004: outcome + "\n" + upload})
        self.assertEqual(d.classify_job(gh, j1), ("rerun", "upload-path"))
        quoted = "2026-11-06T07:00:00.0000000Z   | ##[error]The path for one of the files in artifact is not valid: /x"
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={6004: quoted + "\n" + outcome})
        self.assertEqual(d.classify_job(gh, j1), ("valid", "completed"))

    # The key proxy refused the lock before the agent: the pod's fault, re-run the pair.
    # A stop before the agent for any other reason still needs a human.
    def test_refused_proxy_lock_is_a_rerun(self):
        j1 = job("rep 1 taskA nrp/qwen3-small env-only", 5003, conclusion="failure")
        outcome = "2026-11-03T01:00:01.0000000Z agent_outcome=not_started run=taskA__nrp-qwen3-small__env-only__r1"
        abort = "2026-11-03T01:00:00.0000000Z ABORT: the key proxy did not lock nrp/qwen3-small. No tokens spent."
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5003: abort + "\n" + outcome})
        self.assertEqual(d.classify_job(gh, j1), ("rerun", "proxy-failed"))
        upstream = "2026-11-03T01:00:00.0000000Z ABORT: the key proxy reaches no provider for nrp/qwen3-small."
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5003: upstream + "\n" + outcome})
        self.assertEqual(d.classify_job(gh, j1), ("rerun", "proxy-failed"))
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5003: outcome})
        self.assertEqual(d.classify_job(gh, j1)[0], "unknown")
        quoted = "2026-11-03T01:00:00.0000000Z   | ABORT: the key proxy did not lock x"
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5003: quoted + "\n" + outcome})
        self.assertEqual(d.classify_job(gh, j1)[0], "unknown")

    # A finished run without the proxy's summary cannot show which models it called.
    def test_finished_run_without_the_proxy_summary_is_a_rerun(self):
        j1 = job("rep 1 taskA nrp/qwen3-small env-only", 5004)
        outcome = "2026-11-03T02:00:01.0000000Z agent_outcome=completed run=taskA__nrp-qwen3-small__env-only__r1"
        invalid = ("2026-11-03T02:00:00.0000000Z INVALID: the key proxy's summary is unavailable;"
                   " the run cannot show which models it called")
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5004: invalid + "\n" + outcome})
        self.assertEqual(d.classify_job(gh, j1), ("rerun", "proxy-summary"))
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={5004: outcome})
        self.assertEqual(d.classify_job(gh, j1), ("valid", "completed"))

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

    # An unreadable log needs a human, and a state that remembers one is read
    # again instead of trusted.
    def test_unreadable_log_stops_and_is_read_again(self):
        order = d.read_order(self.order_path(["1 taskA EGI.kimi-k3 env-only"]))
        blocks = d.read_plan(self.plan_path(["taskA reps=1 cells=1"]), order)
        run1 = {"id": 701, "status": "completed", "conclusion": "failure", "created_at": "2026-10-22T00:00:00Z"}
        j1 = job("rep 1 taskA neurodesk/EGI.kimi-k3 env-only", 7001, conclusion="failure")
        gh = FakeGh(runs=[run1], jobs_by_run={701: [j1]}, logs_by_job={})
        rc, state = d.poll(gh, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4, dry_run=True)
        self.assertEqual(rc, 20)
        self.assertEqual(state["pairs"]["1:1"], "lost-candidate")

        cache = {int(jid): tuple(v) for jid, v in state["job_cache"].items()}
        self.assertEqual(cache[7001], ("lost-candidate", d.LOG_UNREADABLE))
        gh2 = FakeGh(runs=[run1], jobs_by_run={701: [j1]}, logs_by_job={7001: outcome_log("completed")})
        rc2, state2 = d.poll(gh2, "o/r", 6, order, blocks, self.wave(reps=1), lanes=4,
                             dry_run=True, job_cache=cache)
        self.assertEqual(gh2.log_reads, [7001])
        self.assertEqual(state2["pairs"]["1:1"], "valid")
        self.assertEqual(state2["job_cache"]["7001"], ["valid", "completed"])

    # A direct provider's model keeps its prefix in the order file, the dispatched
    # cells, the job name and the run directory; a gateway model keeps neurodesk/.
    def test_direct_provider_model_ids(self):
        order = d.read_order(self.order_path([
            "1 taskA nrp/glm-5 env-only",
            "2 taskA EGI.kimi-k3 env-only",
        ]))
        fields = d.build_run_fields("taskA", [1, 2], order, [1], "s", "c", 7)
        self.assertEqual(fields["cells"], "nrp/glm-5:env-only neurodesk/EGI.kimi-k3:env-only")
        blocks = d.read_plan(self.plan_path([]), order)
        run1 = {"id": 801, "status": "completed", "conclusion": "success", "created_at": "2026-10-27T00:00:00Z"}
        j1 = job("rep 1 taskA nrp/glm-5 env-only", 8001)
        gh = FakeGh(runs=[run1], jobs_by_run={801: [j1]}, logs_by_job={8001: outcome_log("completed")})
        rc, state = d.poll(gh, "o/r", 7, order, blocks, self.wave(reps=1), lanes=4, dry_run=True)
        self.assertEqual(state["pairs"]["1:1"], "valid")
        results = os.path.join(self.tmp, "results")
        rundir = os.path.join(results, "wave7", "runs", "taskA", "taskA__nrp-glm-5__env-only__r1")
        os.makedirs(rundir)
        write(os.path.join(rundir, "run.json"), "{}")
        self.assertEqual(d.published_reps(results, 7, "taskA", "nrp/glm-5", "env-only"), {1})
        self.assertEqual(d.published_reps(results, 7, "taskA", "glm-5", "env-only"), set())

    # A provider's cap bounds its jobs in flight across blocks: a block that would
    # push it over waits, blocks on an uncapped provider go, and the plan's
    # parallel token sets the block's max_parallel.
    def test_provider_cap_holds_a_block_and_parallel_token_is_passed(self):
        order = d.read_order(self.order_path([
            "1 taskA egi/kimi-k3 env-only",
            "2 taskB egi/kimi-k3 env-only",
            "3 taskC nrp/glm-5 env-only",
            "4 taskC nrp/qwen3-small env-only",
        ]))
        blocks = d.read_plan(self.plan_path([
            "taskA reps=1,2,3,4,5 cells=1 parallel=3",
            "taskB reps=1,2,3,4,5 cells=2 parallel=3",
            "taskC reps=1,2,3 cells=3,4 parallel=6",
        ]), order)
        self.assertEqual([b.parallel for b in blocks], [3, 3, 6])
        self.assertEqual(d.block_demand(blocks[0], order), {"egi": 3})
        self.assertEqual(d.block_demand(blocks[2], order), {"nrp": 6})
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={})
        rc, state = d.poll(gh, "o/r", 7, order, blocks, self.wave(reps=5), lanes=4,
                           dry_run=True, provider_caps={"egi": 3})
        self.assertEqual(rc, 0)
        cmds = state["dispatched"]
        self.assertEqual(len(cmds), 2)                       # one EGI block + the NRP block
        self.assertEqual(sum("egi/kimi-k3" in c for c in cmds), 1)
        self.assertTrue(any("nrp/glm-5" in c and "max_parallel=6" in c for c in cmds))
        self.assertTrue(any("HOLD" in l and "egi" in l for l in state["log"]))
        # With 2 EGI jobs already in flight, even the first EGI block must wait.
        run1 = {"id": 901, "status": "in_progress", "conclusion": None, "created_at": "2026-10-28T00:00:00Z"}
        jobs = [job("rep %d taskA egi/kimi-k3 env-only" % r, 9000 + r, status="in_progress", conclusion=None)
                for r in (1, 2)]
        gh2 = FakeGh(runs=[run1], jobs_by_run={901: jobs}, logs_by_job={})
        rc2, state2 = d.poll(gh2, "o/r", 7, order, blocks, self.wave(reps=5), lanes=4,
                             dry_run=True, provider_caps={"egi": 3})
        self.assertEqual(sum("egi/kimi-k3" in c for c in state2["dispatched"]), 0)
        self.assertEqual(sum("nrp/" in c for c in state2["dispatched"]), 1)

    # Each block is dispatched with its own skill arm's snapshot (sweep.json arms);
    # a block with no skill arm keeps the default; two skill arms in one block refuse.
    def test_snapshot_follows_the_blocks_skill_arm(self):
        order = d.read_order(self.order_path([
            "1 taskA nrp/minimax-m2 env+skill",
            "2 taskA nrp/qwen3-small env-only",
            "3 taskB nrp/minimax-m2 env+skill-michele",
            "4 taskC nrp/minimax-m2 env-only",
        ]))
        blocks = d.read_plan(self.plan_path([
            "taskA reps=1 cells=1,2", "taskB reps=1 cells=3", "taskC reps=1 cells=4",
        ]), order)
        snaps = d.arm_snapshots_of({"arms": {
            "_comment": ["arms"], "env-only": {"skills_hash": None},
            "env+skill": {"skills_hash": "291f844a43ec"},
            "env+skill-michele": {"skills_hash": "5ba66f0c7ddf"}}})
        self.assertEqual(snaps, {"env+skill": "291f844a", "env+skill-michele": "5ba66f0c"})
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={})
        rc, state = d.poll(gh, "o/r", 7, order, blocks, self.wave(reps=1), lanes=3,
                           dry_run=True, snapshot="default0", arm_snapshots=snaps)
        self.assertEqual(rc, 0)
        snap = {c.split("-f task=")[1].split()[0]: c.split("-f snapshot=")[1].split()[0]
                for c in state["dispatched"]}
        self.assertEqual(snap, {"taskA": "291f844a", "taskB": "5ba66f0c", "taskC": "default0"})
        with self.assertRaises(ValueError):
            d.block_snapshot([1, 3], order, snaps, "default0")

    def test_load_wave_takes_the_waves_own_reps(self):
        sweep = {"reps": 10, "waves": {"6": {"tasks": {}}, "7": {"reps": 7, "tasks": {}}}}
        self.assertEqual(d.load_wave(sweep, 6)["reps"], 10)
        self.assertEqual(d.load_wave(sweep, 7)["reps"], 7)

    def test_load_wave_takes_a_tasks_own_reps(self):
        sweep = {"reps": 10, "waves": {"10": {"reps": 7, "tasks": {
            "t1": {"reps": 5}, "7t": {"reps": 14}, "motion": {}, "_comment": ["x"]}}}}
        w = d.load_wave(sweep, 10)
        self.assertEqual(w["task_reps"], {"t1": 5, "7t": 14})
        self.assertEqual([d.reps_of(w, t) for t in ("t1", "7t", "motion")], [5, 14, 7])
        self.assertEqual(d.reps_of({"reps": 7}, "t1"), 7)  # a wave state without task_reps

    def test_load_wave_refuses_a_bad_task_reps(self):
        for bad in (0, -1, "5", 5.0, True, None):
            sweep = {"reps": 7, "waves": {"10": {"tasks": {"t1": {"reps": bad}}}}}
            with self.assertRaises(SystemExit, msg=repr(bad)):
                d.load_wave(sweep, 10)

    # A dry-run smoke test against wave 6's real order and plan files, named by
    # WAVE6_ORDER and WAVE6_PLAN; skipped when they are not given.
    def test_dry_run_against_real_wave6_files(self):
        here = os.path.dirname(os.path.abspath(__file__))
        order_path = os.environ.get("WAVE6_ORDER", "")
        plan_path = os.environ.get("WAVE6_PLAN", "")
        sweep_path = os.path.join(here, "..", "ci", "sweep.json")
        if not (os.path.isfile(order_path) and os.path.isfile(plan_path)):
            self.skipTest("WAVE6_ORDER and WAVE6_PLAN not given")
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


class TestJobNames(unittest.TestCase):
    def test_a_subject_run_id_is_one_token_of_the_job_name(self):
        """run.yml names a job 'rep <n> <task> <model> <arm>'; a subject run id
        (<task>@<subject>, run_ids.py) has to come back whole."""
        m = d.JOB_RE.match("rep 3 structural-brain-extraction@sub-9901 nrp/qwen3-small env+skill-michele")
        self.assertEqual(m.groups(), ("3", "structural-brain-extraction@sub-9901",
                                      "nrp/qwen3-small", "env+skill-michele"))


class TestFetchFailed(unittest.TestCase):
    """A blinded run whose scan could not be fetched is the network's fault and is re-run;
    a blind-check stop is not, and still stops the poll."""

    def classify(self, log):
        gh = FakeGh(runs=[], jobs_by_run={}, logs_by_job={3001: log})
        return d.classify_job(gh, job("rep 1 taskA@case-012 nrp/x env-only", 3001, conclusion="failure"))

    def test_a_fetch_that_failed_on_the_network_is_a_rerun(self):
        log = "2026-11-14T01:00:00Z FETCH FAILED: the scan after 4 tries (HTTPError 503)\n"
        self.assertEqual(self.classify(log), ("rerun", "fetch-failed"))

    def test_a_blind_check_stop_still_stops_the_poll(self):
        self.assertEqual(self.classify("2026-11-14T01:00:00Z FAIL: a blinded name appears in /x\n")[0], "unknown")


class TestJobLog(unittest.TestCase):
    """Gh.job_log against a gh that refuses escape sequences without the option, and
    one that does not know the option."""

    def gh(self, respond):
        g = d.Gh("o/r")
        g.calls = []

        def run(args):
            g.calls.append(args)
            return respond(args)
        g._run = run
        return g

    def fail(self, message):
        raise d.subprocess.CalledProcessError(1, "gh", stderr=message)

    def test_newer_gh_gets_the_option(self):
        g = self.gh(lambda a: "\x1b[36mlog\x1b[0m" if "--allow-escape-sequences" in a
                    else self.fail("the response contains terminal escape sequences"))
        self.assertEqual(g.job_log(7), "\x1b[36mlog\x1b[0m")
        self.assertEqual(len(g.calls), 1)
        self.assertEqual(g.log_errors, {})

    def test_older_gh_is_asked_again_without_it(self):
        g = self.gh(lambda a: self.fail("unknown flag: --allow-escape-sequences")
                    if "--allow-escape-sequences" in a else "log")
        self.assertEqual(g.job_log(7), "log")
        self.assertEqual(len(g.calls), 2)
        self.assertEqual(g.log_errors, {})

    def test_a_failed_read_keeps_its_reason_without_an_address(self):
        g = self.gh(lambda a: self.fail("gh: Not Found (HTTP 404) https://host.example/a?sig=abc\nmore"))
        self.assertIsNone(g.job_log(7))
        self.assertEqual(g.log_errors[7], "gh: Not Found (HTTP 404) <address>")

    def test_an_empty_log_is_unreadable(self):
        g = self.gh(lambda a: "")
        self.assertIsNone(g.job_log(7))
        self.assertEqual(g.log_errors[7], "empty")


class TestRunList(unittest.TestCase):
    def test_every_run_since_the_wave_start_is_listed(self):
        seen = []

        class G(d.Gh):
            def _run(self, args):
                seen.append(args)
                return json.dumps([{"databaseId": 1, "status": "completed", "conclusion": "success",
                                    "createdAt": "2026-11-14T02:00:00Z"},
                                   {"databaseId": 2, "status": "completed", "conclusion": "success",
                                    "createdAt": "2026-11-13T02:00:00Z"}])

        runs = G("o/r").run_list("run.yml", since="2026-11-14T00:00:00Z")
        args = seen[0]
        self.assertEqual(args[args.index("-L") + 1], "1000")
        self.assertEqual(args[args.index("--created") + 1], ">=2026-11-14T00:00:00Z")
        self.assertEqual([r["id"] for r in runs], [1])
        G("o/r").run_list("run.yml")
        self.assertNotIn("--created", seen[1])


if __name__ == "__main__":
    unittest.main()
