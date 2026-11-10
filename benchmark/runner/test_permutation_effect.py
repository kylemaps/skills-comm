#!/usr/bin/env python
"""Tests for permutation_effect.py: the tested secondary outcomes of wave 8.

    python -m unittest test_permutation_effect -v

Each test shows the script can fail on bad input, in the sense the declaration fixes:
a run that does not pass counts the wall, a run without tokens is left out and
counted, a thin stratum is skipped and listed, the sign follows the arms, and a real
difference is found while none is invented.
"""
import csv
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import permutation_effect as pe  # noqa: E402

A, B = "env+skill", "env+skill-michele"


def row(arm, model="m1", subject="s1", valid="True", passed="True", lost="",
        seconds=600, tokens=1000):
    return {"arm": arm, "model": model, "_subject": subject, "valid": valid,
            "passed": passed, "lost": lost, "tokens_total": "" if tokens is None else str(tokens),
            "_run": {"seconds_to_output": seconds}}


class TimeValue(unittest.TestCase):
    def test_pass_is_minutes(self):
        self.assertEqual(pe.time_value(row(B, seconds=600), 90), (10.0, None))

    def test_fail_counts_the_wall(self):
        self.assertEqual(pe.time_value(row(B, passed="False", seconds=600), 90), (90.0, None))

    def test_lost_counts_the_wall(self):
        self.assertEqual(pe.time_value(row(B, valid="False", lost="OOM", passed="",
                                           seconds=None), 90), (90.0, None))

    def test_excluded_run_is_left_out(self):
        self.assertEqual(pe.time_value(row(B, valid="False"), 90), (None, None))

    def test_pass_without_time_counts_the_wall_and_is_noted(self):
        self.assertEqual(pe.time_value(row(B, seconds=None), 90), (90.0, "passed_without_time"))

    def test_time_is_capped_at_the_wall(self):
        self.assertEqual(pe.time_value(row(B, seconds=7200), 90), (90.0, None))


class Placements(unittest.TestCase):
    """output_time.py's records: the later of the two times counts; an unchecked run counts as
    recorded and is noted."""

    def r(self):
        x = row(B, seconds=600)
        x.update(_ci_run="9", rep="1")
        return x

    def test_a_later_placement_counts(self):
        r = self.r()
        self.assertEqual(pe.time_value(r, 90, {pe.run_key(r): {"checked": True, "placed_s": 3000}}),
                         (50.0, "placed_later"))

    def test_an_earlier_placement_changes_nothing(self):
        r = self.r()
        self.assertEqual(pe.time_value(r, 90, {pe.run_key(r): {"checked": True, "placed_s": 300}}), (10.0, None))
        self.assertEqual(pe.time_value(r, 90, {pe.run_key(r): {"checked": True, "placed_s": None}}), (10.0, None))

    def test_an_unchecked_run_counts_as_recorded_and_is_noted(self):
        r = self.r()
        self.assertEqual(pe.time_value(r, 90, {}), (10.0, "placement_unchecked"))
        self.assertEqual(pe.time_value(r, 90, {pe.run_key(r): {"checked": False, "placed_s": 3000}}),
                         (10.0, "placement_unchecked"))

    def test_without_placements_nothing_changes(self):
        self.assertEqual(pe.time_value(self.r(), 90), (10.0, None))

    def test_a_failed_run_still_counts_the_wall(self):
        r = self.r()
        r["passed"] = "False"
        self.assertEqual(pe.time_value(r, 90, {pe.run_key(r): {"checked": True, "placed_s": 60}}), (90.0, None))

    def test_the_key_names_the_job(self):
        r = self.r()
        self.assertEqual(pe.run_key(r), "9|m1|%s|1" % B)


class TokenValue(unittest.TestCase):
    def test_log_of_tokens(self):
        self.assertAlmostEqual(pe.token_value(row(B, tokens=1000)), math.log(1000))

    def test_no_record_is_left_out(self):
        self.assertIsNone(pe.token_value(row(B, tokens=None)))
        self.assertIsNone(pe.token_value(row(B, tokens=0)))

    def test_invalid_run_is_left_out(self):
        self.assertIsNone(pe.token_value(row(B, valid="False", tokens=1000)))


class Strata(unittest.TestCase):
    def test_thin_stratum_is_skipped_and_listed(self):
        rows = [row(B, subject="s1"), row(B, subject="s1"), row(A, subject="s1"), row(A, subject="s1"),
                row(B, subject="s2"), row(A, subject="s2"), row(A, subject="s2")]
        used, skipped = pe.build_strata(rows, B, A, lambda r: 1.0)
        self.assertEqual([k for k, _ in used], [("m1", "s1")])
        self.assertEqual([k for k, _ in skipped], [("m1", "s2")])

    def test_other_arms_are_ignored(self):
        rows = [row(B), row(B), row(A), row(A), row("env-only"), row("env-only")]
        used, _ = pe.build_strata(rows, B, A, lambda r: 1.0)
        self.assertEqual(len(used[0][1][B]) + len(used[0][1][A]), 4)

    def test_effect_is_the_weighted_mean_of_stratum_differences(self):
        strata = [(("m", "s1"), {B: [10, 10], A: [30, 30]}),          # diff -20, weight 4
                  (("m", "s2"), {B: [50, 50, 50, 50], A: [40, 40]})]  # diff +10, weight 6
        self.assertAlmostEqual(pe.effect(strata, B, A, statistics.median),
                               (4 * -20 + 6 * 10) / 10.0)


class Permutation(unittest.TestCase):
    def strata(self, b, a, n=6):
        return [(("m%d" % i, "s"), {B: list(b), A: list(a)}) for i in range(n)]

    def test_a_real_difference_is_found(self):
        st = self.strata([5, 6, 7, 8, 9, 10, 11], [40, 50, 60, 70, 80, 90, 90])
        obs, p, _ = pe.permutation_test(st, B, A, statistics.median, 2000, 1)
        self.assertLess(obs, 0)
        self.assertLess(p, 0.01)

    def test_no_difference_is_not_invented(self):
        st = self.strata([10, 20, 30, 40, 50, 60, 70], [10, 20, 30, 40, 50, 60, 70])
        obs, p, _ = pe.permutation_test(st, B, A, statistics.median, 2000, 1)
        self.assertEqual(obs, 0.0)
        self.assertEqual(p, 1.0)

    def test_sign_follows_the_arms(self):
        st = self.strata([5, 6, 7, 8, 9, 10, 11], [40, 50, 60, 70, 80, 90, 90])
        obs_ba, p_ba, _ = pe.permutation_test(st, B, A, statistics.median, 500, 1)
        obs_ab, p_ab, _ = pe.permutation_test(st, A, B, statistics.median, 500, 1)
        self.assertAlmostEqual(obs_ba, -obs_ab)
        self.assertAlmostEqual(p_ba, p_ab)

    def test_seed_fixes_the_answer(self):
        # A small difference, so p sits mid-range and depends on the draw.
        st = self.strata([10, 20, 30, 40, 50, 60, 70], [15, 25, 35, 45, 55, 65, 75])
        r1 = pe.permutation_test(st, B, A, statistics.median, 300, 7)
        r2 = pe.permutation_test(st, B, A, statistics.median, 300, 7)
        self.assertEqual(r1, r2)
        others = [pe.permutation_test(st, B, A, statistics.median, 300, s)[1] for s in range(8, 13)]
        self.assertTrue(any(p != r1[1] for p in others))

    def test_p_is_never_zero(self):
        st = self.strata([1, 2, 3, 4, 5, 6, 7], [91, 92, 93, 94, 95, 96, 97])
        _, p, _ = pe.permutation_test(st, B, A, statistics.median, 100, 1)
        self.assertGreater(p, 0)
        self.assertGreaterEqual(p, 1 / 101.0)
        self.assertLess(p, 0.05)

    def test_no_strata_is_nan(self):
        obs, p, n = pe.permutation_test([], B, A, statistics.median, 100, 1)
        self.assertNotEqual(obs, obs)
        self.assertNotEqual(p, p)


class Analyse(unittest.TestCase):
    def test_counts_and_both_outcomes(self):
        rows = []
        for i in range(4):
            rows.append(row(B, seconds=300 + i, tokens=1000))
            rows.append(row(A, seconds=3000 + i, tokens=4000))
        rows.append(row(B, passed="False", seconds=None, tokens=None))   # fails, no tokens
        rows.append(row(A, seconds=None, tokens=2000))                   # passes, no time
        rows.append(row(A, valid="False", lost="OOM", passed="", seconds=None, tokens=None))
        rows.append(row(A, valid="False", passed="", seconds=None, tokens=None))  # excluded
        out = pe.analyse(rows, B, A, 500, 1, 90)
        m, k = out["minutes_to_pass"], out["log_tokens"]
        self.assertEqual(m["n"], {B: 5, A: 6})        # B: 4 passes + 1 fail; A: 4 + no-time + lost
        self.assertEqual(m["passed_without_time"], {B: 0, A: 1})
        self.assertLess(m["effect_minutes"], 0)
        self.assertEqual(k["n"], {B: 4, A: 5})
        self.assertEqual(k["no_tokens"], {B: 1, A: 0})  # the lost run is not valid: not counted here
        self.assertAlmostEqual(k["ratio"], 1000 / (4000 ** 4 * 2000) ** (1 / 5.0), places=6)


class EndToEnd(unittest.TestCase):
    """A results directory as assemble writes it, a sweep with the wave, the script."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_runs_from_files(self):
        fam = "structural-brain-extraction"
        tasks = {}
        for s in ("sub-1", "sub-2"):
            t = "%s@%s" % (fam, s)
            tasks[t] = {"family": fam, "subject": s, "models": ["nrp/x"], "arms": [A, B]}
        sweep = {"waves": {"8": {"tasks": tasks, "reps": 3,
                                 "arms_compared": {"primary": [B, A]}}}}
        sp = os.path.join(self.d, "sweep.json")
        json.dump(sweep, open(sp, "w"))
        res = os.path.join(self.d, "results")
        cols = ["task", "model", "arm", "rep", "valid", "passed", "lost", "tokens_total"]
        for t in tasks:
            with open(os.path.join(res_dir(res), "runs_%s.csv" % t), "w", newline="") as fh:
                w = csv.DictWriter(fh, cols)
                w.writeheader()
                for rep in (1, 2, 3):
                    for arm, sec, tok in ((B, 600, 1000), (A, 4000, 3000)):
                        w.writerow({"task": t, "model": "nrp/x", "arm": arm, "rep": rep,
                                    "valid": "True", "passed": "True", "lost": "",
                                    "tokens_total": tok})
                        rd = os.path.join(res, "runs", t, "%s__nrp-x__%s__r%d" % (t, arm, rep))
                        os.makedirs(rd)
                        json.dump({"seconds_to_output": sec}, open(os.path.join(rd, "run.json"), "w"))
        out = os.path.join(self.d, "out.json")
        p = subprocess.run([sys.executable, os.path.join(HERE, "permutation_effect.py"),
                            "--results", res, "--sweep", sp, "--wave", "8",
                            "--permutations", "200", "--json", out],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        rep = json.load(open(out))
        self.assertEqual(list(rep), [fam])
        self.assertEqual(rep[fam]["minutes_to_pass"]["strata"], 2)
        self.assertAlmostEqual(rep[fam]["minutes_to_pass"]["effect_minutes"], (600 - 4000) / 60.0)
        self.assertAlmostEqual(rep[fam]["log_tokens"]["ratio"], 1000 / 3000.0)
        self.assertIn("minutes to a passing output", p.stdout)


def res_dir(res):
    os.makedirs(res, exist_ok=True)
    return res


if __name__ == "__main__":
    unittest.main()
