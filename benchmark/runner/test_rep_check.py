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

    def test_another_rep_or_wave_is_ignored(self):
        recs = self.good() + [rec(M1, "zzz", repeat=2), rec(M1, "zzz", wave="9")]
        self.assertEqual(run(recs)[0], {})


if __name__ == "__main__":
    unittest.main()
