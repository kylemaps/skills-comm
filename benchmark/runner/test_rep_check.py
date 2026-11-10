"""rep_check.py on records held in memory: a clean rep passes, and each gate fails on the
record that breaks it."""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rep_check  # noqa: E402

M1, M2 = "nrp/m1", "nrp/m2"
T = "task-a@case-012"
WAVE = {"model_fingerprints": {M1: "aaa", M2: "bbb"},
        "pins": {"tasks_sha": "abc1234", "image_version": "img"},
        "measured": {"cpus": 7, "work_volume_gib_min": 55},
        "primary_family": "task-a",
        "tasks": {T: {"family": "task-a", "subject": "case-012"}}}
ORDER = {(T, M1, "env-only"): 1, (T, M2, "env-only"): 2}


def rec(model, fp, **over):
    r = {"task_id": T, "model": model, "condition": "env-only", "repeat": 1, "wave": "10",
         "ci_run": "1/1", "agent_outcome": "completed",
         "llm_proxy": {"locked": [model], "calls": {model: 5}, "unlocked_calls": 0},
         "model_fingerprint": fp, "model_fingerprint_end": fp, "model_changed": False,
         "prompt_hash": "p1", "tasks_sha": "abc1234", "image_version": "img",
         "harness_sha": "feedbeef00", "measured": {"cpus": 7, "work_volume_gib": 58},
         "model_limit": {"context": 131000, "output": 32000},
         "input": {"case": "case-012", "files": [{"as": "anat/T1w.nii.gz", "sha256": "d1"}]},
         "subject_named": False, "answer_key_reads": []}
    r.update(over)
    return r


def run(recs, **kw):
    fail, note, *_ = rep_check.check(WAVE, "10", ORDER, [("x", r) for r in recs], 1, **kw)
    return dict(fail), dict(note)


class RepCheck(unittest.TestCase):
    def good(self):
        return [rec(M1, "aaa"), rec(M2, "bbb")]

    def test_a_clean_rep_passes(self):
        fail, note = run(self.good(), harness="feedbeef")
        self.assertEqual(fail, {})
        self.assertEqual(note["subject_named, arm env-only"], ["true 0, false 2, unchecked 0"])

    def test_a_missing_cell_fails_g1_and_the_panel(self):
        fail, _ = run(self.good()[:1])
        self.assertIn("G1 no started record", fail)
        self.assertIn("G9 panel", fail)

    def test_a_changed_end_fingerprint_fails_g4(self):
        recs = self.good()
        recs[1]["model_fingerprint_end"] = "ccc"
        self.assertIn("G4 fingerprint", run(recs)[0])
        recs = self.good()
        recs[1]["model_changed"] = True
        self.assertIn("G4 fingerprint", run(recs)[0])

    def test_a_proxy_lock_failure_fails_g2_and_an_upstream_refusal_does_not(self):
        recs = self.good() + [rec(M1, "aaa", agent_outcome="not_started", stopped_before_agent="proxy-lock-failed")]
        self.assertIn("G2 stopped by the proxy", run(recs)[0])
        recs = self.good() + [rec(M1, "aaa", agent_outcome="not_started", stopped_before_agent="proxy-upstream-failed",
                                  model_probe_error="HTTP 429")]
        self.assertEqual(run(recs)[0], {})

    def test_calls_for_another_model_fail_g3(self):
        recs = self.good()
        recs[0]["llm_proxy"]["calls"]["nrp/other"] = 1
        self.assertIn("G3 proxy summary", run(recs)[0])

    def test_two_prompts_for_one_run_id_fail_g5(self):
        recs = self.good()
        recs[1]["prompt_hash"] = "p2"
        self.assertIn("G5 prompt_hash", run(recs)[0])

    def test_a_missing_or_different_input_fails_g6(self):
        recs = self.good()
        recs[0]["input"] = None
        self.assertIn("G6 input record", run(recs)[0])
        recs = self.good()
        recs[1]["input"] = copy.deepcopy(recs[1]["input"])
        recs[1]["input"]["files"][0]["sha256"] = "d2"
        self.assertIn("G6 input record", run(recs)[0])

    def test_a_blind_check_stop_fails_g7(self):
        self.assertIn("G7 stopped by the blind check", run(self.good(), blind_stops=["rep 1 x (run 9)"])[0])

    def test_a_wrong_pin_measure_or_harness_fails_g8(self):
        for over in ({"tasks_sha": "zzz"}, {"measured": {"cpus": 4, "work_volume_gib": 58}},
                     {"measured": {"cpus": 7, "work_volume_gib": 40}}):
            recs = self.good()
            recs[0].update(over)
            self.assertIn("G8 pins and measured", run(recs)[0], over)
        self.assertIn("G8 pins and measured", run(self.good(), harness="0000")[0])

    def test_subject_named_is_reported_and_is_not_a_gate(self):
        recs = self.good()
        recs[0]["subject_named"] = True
        recs[1]["subject_named"] = None
        fail, note = run(recs)
        self.assertEqual(fail, {})
        self.assertEqual(note["subject_named, arm env-only"], ["true 1, false 0, unchecked 1"])

    def test_calls_for_another_model_the_proxy_refused_are_a_note(self):
        recs = self.good()
        recs[0]["llm_proxy"]["calls"]["nrp/vision"] = 2
        recs[0]["llm_proxy"]["mismatches"] = 2
        fail, note = run(recs)
        self.assertEqual(fail, {})
        self.assertIn("calls for another model, refused by the proxy", note)

    def test_more_calls_for_another_model_than_refusals_fail_g3(self):
        recs = self.good()
        recs[0]["llm_proxy"]["calls"]["nrp/vision"] = 3
        recs[0]["llm_proxy"]["mismatches"] = 1
        self.assertIn("G3 proxy summary", run(recs)[0])

    def test_an_end_probe_without_an_answer_is_a_note_and_counts(self):
        recs = self.good()
        recs[1].update(model_fingerprint_end=None, model_changed=None, model_probe_error_end="HTTP 503")
        fail, note = run(recs)
        self.assertEqual(fail, {})
        self.assertEqual(note["end probe not answered, run counted"], ["%s %s env-only r1 (run 1): HTTP 503" % (T, M2)])

    def test_a_wrong_start_fingerprint_still_fails_without_an_end_answer(self):
        recs = self.good()
        recs[1].update(model_fingerprint="zzz", model_fingerprint_end=None, model_changed=None)
        self.assertIn("G4 fingerprint", run(recs)[0])

    def test_a_network_fetch_failure_is_a_note_not_g7(self):
        fail, note = run(self.good(), fetch_failures=["rep 1 x (run 9)"])
        self.assertEqual(fail, {})
        self.assertIn("fetch failed on the network after its retries, re-run", note)

    def test_the_gate_is_the_fail_lines_alone(self):
        self.assertTrue(rep_check.gate_passes({}))
        self.assertTrue(rep_check.gate_passes({"G3 proxy summary": []}))
        self.assertFalse(rep_check.gate_passes({"G3 proxy summary": ["x"]}))

    def test_download_lists_every_run_since_and_tells_a_fetch_failure_from_the_blind_check(self):
        calls = []
        step = {"name": rep_check.BLIND_STEP, "conclusion": "failure"}

        def fake_gh_json(args):
            calls.append(args)
            if args[:2] == ["run", "list"]:
                return [{"databaseId": 7, "createdAt": "2026-11-14T01:00:00Z", "status": "completed"},
                        {"databaseId": 6, "createdAt": "2026-11-13T23:00:00Z", "status": "completed"}]
            if args[1].endswith("/jobs?per_page=100"):
                return {"jobs": [{"id": 71, "name": "rep 1 a", "steps": [step]},
                                 {"id": 72, "name": "rep 1 b", "steps": [step]},
                                 {"id": 73, "name": "rep 1 c", "steps": [{"name": "x", "conclusion": "success"}]}]}
            return {"artifacts": [{"id": 1, "name": "cell-a", "expired": False},
                                  {"id": 2, "name": "cell-b", "expired": False}]}

        logs = {71: "2026-11-14T01:01:00Z FETCH FAILED: the scan after 4 tries (HTTPError 503)\n",
                72: "2026-11-14T01:01:00Z FAIL: a blinded name appears in /x\n"}

        class P:
            def __init__(self, rc, out=""):
                self.returncode, self.stdout = rc, out

        def fake_run(args, **kw):
            if args[:2] == ["gh", "api"] and args[2].endswith("/logs"):
                return P(0, logs[int(args[2].split("/")[-2])])
            return P(1 if "cell-b" in args else 0)

        old = rep_check.gh_json, rep_check.subprocess.run
        rep_check.gh_json, rep_check.subprocess.run = fake_gh_json, fake_run
        try:
            nruns, pending, n, blind, fetch_failed, failed = rep_check.download(
                "o/r", "2026-11-14T00:00:00Z", "/nonexistent-work")
        finally:
            rep_check.gh_json, rep_check.subprocess.run = old
        self.assertIn("--created", calls[0])
        self.assertEqual(calls[0][calls[0].index("--created") + 1], ">=2026-11-14T00:00:00Z")
        self.assertEqual(calls[0][calls[0].index("--limit") + 1], "1000")
        self.assertEqual(nruns, 1)                      # the older run is filtered out
        self.assertEqual(fetch_failed, ["rep 1 a (run 7)"])
        self.assertEqual(blind, ["rep 1 b (run 7)"])
        self.assertEqual((n, failed), (1, ["cell-b (run 7)"]))

    def test_another_rep_or_wave_is_ignored(self):
        recs = self.good() + [rec(M1, "zzz", repeat=2), rec(M1, "zzz", wave="9")]
        self.assertEqual(run(recs)[0], {})


if __name__ == "__main__":
    unittest.main()
