#!/usr/bin/env python
"""Tests for wave_report.py.

    python test_wave_report.py

A synthetic results tree exercises completeness, the MIN_N gate on small cells,
failures-fixed and the timeouts-removed scenario. A second test, skipped unless
the real wave 6 record is present on disk, reproduces its published headline
numbers from the sweep and the record alone -- the point of the port.
"""
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import wave_report as wr  # noqa: E402

CSV_COLS = ["task", "model", "arm", "rep", "valid", "passed", "lost", "timed_out",
           "flags", "off_spec", "methods", "decided_tools"]


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        for r in rows:
            row = {c: "" for c in CSV_COLS}
            row.update(off_spec="none", methods="", decided_tools="", flags="",
                      lost="", timed_out="False", valid="True")
            row.update(r)
            w.writerow(row)


def call(argv):
    """Run wave_report.main() with argv, capture stdout."""
    old = sys.argv
    sys.argv = ["wave_report.py"] + argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = wr.main()
    finally:
        sys.argv = old
    return buf.getvalue(), rc


class TestWaveReportSynthetic(unittest.TestCase):
    """Two tasks, two models, 2 arms x 3 reps -- deliberately below MIN_N (5), so
    the point here is as much "small cells report counts, not invented stats" as
    it is the arithmetic. One lost run, one timeout.
    """

    def setUp(self):
        self.d = tempfile.mkdtemp()
        sweep = {"reps": 3, "waves": {"1": {"tasks": {
            "task-a": {"models": ["P.m1", "P.m2"], "arms": ["env-only", "env+skill"]},
            "task-b": {"models": ["P.m1", "P.m2"], "arms": ["env-only", "env+skill"]},
        }}}}
        self.sweep_path = os.path.join(self.d, "sweep.json")
        with open(self.sweep_path, "w", encoding="utf-8") as fh:
            json.dump(sweep, fh)

        # task-a: m1 1/3 -> 3/3 (a real, if underpowered, gain). m2's third
        # env-only run is lost, not merely failed.
        a_rows = (
            [{"model": "P.m1", "arm": "env-only", "rep": "1", "passed": "True"},
             {"model": "P.m1", "arm": "env-only", "rep": "2", "passed": "False"},
             {"model": "P.m1", "arm": "env-only", "rep": "3", "passed": "False"}]
            + [{"model": "P.m1", "arm": "env+skill", "rep": str(i), "passed": "True"}
               for i in (1, 2, 3)]
            + [{"model": "P.m2", "arm": "env-only", "rep": "1", "passed": "False"},
               {"model": "P.m2", "arm": "env-only", "rep": "2", "passed": "False"},
               {"model": "P.m2", "arm": "env-only", "rep": "3", "passed": "False",
                "lost": "OOM-killed: pod memory limit"}]
            + [{"model": "P.m2", "arm": "env+skill", "rep": "1", "passed": "True"},
               {"model": "P.m2", "arm": "env+skill", "rep": "2", "passed": "False"},
               {"model": "P.m2", "arm": "env+skill", "rep": "3", "passed": "True"}])
        # task-b: m1's second env+skill run hit the wall (counted as a failed,
        # valid run, per the "timeout: fail" rule -- not excluded).
        b_rows = (
            [{"model": "P.m1", "arm": "env-only", "rep": str(i), "passed": "False"}
             for i in (1, 2, 3)]
            + [{"model": "P.m1", "arm": "env+skill", "rep": "1", "passed": "True"},
               {"model": "P.m1", "arm": "env+skill", "rep": "2", "passed": "False",
                "timed_out": "True"},
               {"model": "P.m1", "arm": "env+skill", "rep": "3", "passed": "True"}]
            + [{"model": "P.m2", "arm": "env-only", "rep": "1", "passed": "True"},
               {"model": "P.m2", "arm": "env-only", "rep": "2", "passed": "True"},
               {"model": "P.m2", "arm": "env-only", "rep": "3", "passed": "False"}]
            + [{"model": "P.m2", "arm": "env+skill", "rep": str(i), "passed": "True"}
               for i in (1, 2, 3)])
        for t, rows in (("task-a", a_rows), ("task-b", b_rows)):
            for r in rows:
                r["task"] = t
            write_csv(os.path.join(self.d, "runs_%s.csv" % t), rows)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def report(self):
        out, rc = call(["--results", self.d, "--wave", "1", "--sweep", self.sweep_path])
        self.assertEqual(rc, 0)
        return out

    def test_completeness_counts_and_marks_the_lost_run(self):
        out = self.report()
        self.assertIn("| task-a | P.m1 | 3/3 | 3/3 |", out)
        self.assertIn("| task-a | P.m2 | 3/3 (1 lost) | 3/3 |", out)
        self.assertIn("All 8 cells complete.", out)

    def test_headline_row_below_min_n_reports_zero_not_invented_stats(self):
        # 3 valid runs per arm is below MIN_N_FOR_STATS (5): the honest output is
        # a count of zero comparable models, not a p-value built on noise.
        out = self.report()
        self.assertIn("| task-a | 0 | | | | | | |", out)
        self.assertIn("| task-b | 0 | | | | | | |", out)

    def test_failures_fixed_is_not_gated_by_min_n(self):
        # Unlike the stratified tests, this is a plain count over all valid runs
        # in an arm and must report a real number even where MIN_N blocks the
        # per-model comparison above it.
        out = self.report()
        self.assertIn("| task-a | failures fixed | +80% of no-skill failures", out)
        self.assertIn("| task-b | failures fixed | +75% of no-skill failures", out)

    def test_timed_out_run_is_counted_in_behaviour(self):
        out = self.report()
        # task-b | env+skill | failures (both models pooled, 1 of 6) | ... |
        # timeouts column | ... -- one run hit the wall.
        self.assertIn("| task-b | env+skill | 1/6 |", out)
        lines = [l for l in out.splitlines() if l.startswith("| task-b | env+skill | 1/6 |")]
        self.assertEqual(len(lines), 1)
        cells = [c.strip() for c in lines[0].split("|")]
        # | task | arm | failures | minutes | astra | off-spec | timeouts | gates |
        self.assertEqual(cells[7], "1")

    def test_timeouts_removed_scenario_runs_end_to_end(self):
        # Exercises the new stratified_effect.py scenario through wave_report;
        # still MIN_N-gated at n=3, so the honest row is the zero-model one.
        out = self.report()
        self.assertIn("| timeouts removed | 0 | | | | | | |", out)

    def test_incomplete_model_is_dropped(self):
        sweep = json.load(open(self.sweep_path, encoding="utf-8"))
        sweep["waves"]["1"]["incomplete"] = {"P.m2": "fingerprint changed mid-wave"}
        with open(self.sweep_path, "w", encoding="utf-8") as fh:
            json.dump(sweep, fh)
        out = self.report()
        self.assertNotIn("P.m2", out)
        self.assertIn("All 4 cells complete.", out)

    def test_arms_compared_primary_overrides_the_default_pair(self):
        # arms_compared.primary = [skill, control] names env-only as the ARM and
        # env+skill as the CONTROL -- the reverse of the default pairing. The
        # plain run counts are symmetric either way (3 vs 3), but "failures
        # fixed" divides by the control arm's own failures, so reversing which
        # arm is control must change the number, not just its sign.
        sweep = json.load(open(self.sweep_path, encoding="utf-8"))
        sweep["waves"]["1"]["arms_compared"] = {"primary": ["env-only", "env+skill"]}
        with open(self.sweep_path, "w", encoding="utf-8") as fh:
            json.dump(sweep, fh)
        out = self.report()
        self.assertIn("| task-a | failures fixed | -400% of no-skill failures", out)

    def test_missing_wave_is_refused(self):
        with self.assertRaises(SystemExit):
            call(["--results", self.d, "--wave", "99", "--sweep", self.sweep_path])


class TestBehaviourColumn(unittest.TestCase):
    """The behaviour table counts the runs CSV's tags, and every run that stopped on its
    own with no output (stopped-to-ask is one kind of it)."""

    def test_tags_and_stopped_no_output_are_counted_per_arm(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "sweep.json"), "w", encoding="utf-8") as fh:
            json.dump({"reps": 3, "waves": {"1": {"tasks": {"t": {
                "models": ["P.m1"], "arms": ["env-only", "env+skill"]}}}}}, fh)
        cols = CSV_COLS + ["behaviours", "output_present"]
        rows = [
            {"arm": "env-only", "rep": "1", "passed": "False", "output_present": "False",
             "behaviours": "stopped-to-ask"},
            {"arm": "env-only", "rep": "2", "passed": "False", "output_present": "True",
             "behaviours": "placeholder-output"},
            {"arm": "env-only", "rep": "3", "passed": "False", "output_present": "False",
             "timed_out": "True"},
            {"arm": "env+skill", "rep": "1", "passed": "True", "output_present": "True"},
            {"arm": "env+skill", "rep": "2", "passed": "False", "output_present": "True",
             "timed_out": "True", "behaviours": "timeout-after-output"},
            {"arm": "env+skill", "rep": "3", "passed": "False", "output_present": "False"},
        ]
        with open(os.path.join(d, "runs_t.csv"), "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                row = {c: "" for c in cols}
                row.update(task="t", model="P.m1", off_spec="none", timed_out="False", valid="True")
                row.update(r)
                w.writerow(row)
        out, rc = call(["--results", d, "--wave", "1", "--sweep", os.path.join(d, "sweep.json")])
        self.assertEqual(rc, 0)
        section = out.split("## Behaviour (valid runs)", 1)[1].split("\n## ", 1)[0]
        cell = {l.split("|")[2].strip(): l.split("|")[9].strip() for l in section.splitlines()
                if l.startswith("| t | env")}
        self.assertEqual(cell["env-only"], "placeholder-output 1, stopped-no-output 1, stopped-to-ask 1")
        self.assertEqual(cell["env+skill"], "stopped-no-output 1, timeout-after-output 1")


class TestFamilies(unittest.TestCase):
    """Subject tasks of one family pooled as strata model x subject, for the primary
    pair and each declared secondary pair; completeness counts every declared arm."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        arms = ["env-only", "env+skill", "env+skill-b"]
        sweep = {"reps": 5, "waves": {"1": {
            "arms_compared": {"primary": ["env+skill-b", "env+skill"],
                              "secondary": [["env+skill", "env-only"], ["env+skill-b", "env-only"]]},
            "tasks": {"t-s1": {"models": ["P.m1", "P.m2"], "arms": arms, "family": "fam", "subject": "s1"},
                      "t-s2": {"models": ["P.m1", "P.m2"], "arms": arms, "family": "fam", "subject": "s2"}}}}}
        self.sweep_path = os.path.join(self.d, "sweep.json")
        with open(self.sweep_path, "w", encoding="utf-8") as fh:
            json.dump(sweep, fh)
        passes = {"env-only": 0, "env+skill": 3, "env+skill-b": 5}
        for t in ("t-s1", "t-s2"):
            write_csv(os.path.join(self.d, "runs_%s.csv" % t), [
                {"task": t, "model": m, "arm": a, "rep": str(i), "passed": str(i <= passes[a])}
                for m in ("P.m1", "P.m2") for a in arms for i in range(1, 6)])

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def report(self):
        out, rc = call(["--results", self.d, "--wave", "1", "--sweep", self.sweep_path])
        self.assertEqual(rc, 0)
        return out

    def test_family_rows_pool_subjects_for_each_comparison(self):
        section = self.report().split("## Families", 1)[1].split("\n## ", 1)[0]
        rows = {l.split("|")[1].strip(): [c.strip() for c in l.split("|")[2:5]]
                for l in section.splitlines() if l.startswith("| fam: ")}
        self.assertEqual(rows["fam: env+skill-b vs env+skill"], ["4", "12/20", "20/20"])
        self.assertEqual(rows["fam: env+skill vs env-only"], ["4", "0/20", "12/20"])
        self.assertEqual(rows["fam: env+skill-b vs env-only"], ["4", "0/20", "20/20"])

    def test_completeness_counts_every_declared_arm(self):
        out = self.report()
        self.assertIn("| task | model | env+skill | env+skill-b | env-only |", out)
        self.assertIn("All 12 cells complete.", out)
        with open(os.path.join(self.d, "runs_t-s2.csv"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        with open(os.path.join(self.d, "runs_t-s2.csv"), "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(l for l in lines if not (",env-only,5," in l and "P.m2" in l)) + "\n")
        self.assertIn("NOT COMPLETE", self.report())   # the third arm's short cell is seen

    def test_a_wave_without_families_has_no_family_section(self):
        with open(self.sweep_path, encoding="utf-8") as fh:
            sweep = json.load(fh)
        for td in sweep["waves"]["1"]["tasks"].values():
            td.pop("family")
        with open(self.sweep_path, "w", encoding="utf-8") as fh:
            json.dump(sweep, fh)
        self.assertNotIn("## Families", self.report())


class TestRunDirectories(unittest.TestCase):
    """The run directory a CSV row names, and the model a run directory names, for
    a gateway model and a direct provider's (model_ids.py)."""

    SEGMENTS = ("neurodesk-EGI.kimi-k3", "nrp-glm-5")

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_rows_find_their_run_json(self):
        write_csv(os.path.join(self.d, "runs_t.csv"), [
            {"task": "t", "model": m, "arm": "env-only", "rep": "1", "passed": "True"}
            for m in ("EGI.kimi-k3", "nrp/glm-5")])
        for seg in self.SEGMENTS:
            rd = os.path.join(self.d, "runs", "t", "t__%s__env-only__r1" % seg)
            os.makedirs(rd)
            with open(os.path.join(rd, "run.json"), "w", encoding="utf-8") as fh:
                json.dump({"seg": seg}, fh)
        rows = wr.load(self.d, ["t"])["t"]
        self.assertEqual(sorted(r["_run"].get("seg") for r in rows), list(self.SEGMENTS))

    def test_speed_reads_the_model_off_the_run_directory(self):
        import sqlite3
        for seg in self.SEGMENTS:
            db_dir = os.path.join(self.d, "art", "runs", "t__%s__env-only__r1" % seg,
                                  ".xdg-data", "opencode")
            os.makedirs(db_dir)
            con = sqlite3.connect(os.path.join(db_dir, "opencode.db"))
            con.execute("create table message (data text)")
            con.execute("insert into message values (?)", (json.dumps(
                {"role": "assistant", "time": {"created": 1000, "completed": 3000},
                 "tokens": {"output": 20}}),))
            con.commit()
            con.close()
        self.assertEqual(dict(wr.speed(self.d)), {"EGI.kimi-k3": [10.0], "nrp/glm-5": [10.0]})
        self.assertEqual(dict(wr.speed(self.d, rep="10")), {})


# The published wave 6 record (a skills-benchmark checkout's results/wave6), by WAVE6_RESULTS.
REAL_RESULTS = os.environ.get("WAVE6_RESULTS", "")
REAL_SWEEP = os.path.join(REPO, "benchmark", "ci", "sweep.json")


@unittest.skipUnless(os.path.isdir(REAL_RESULTS),
                     "real wave 6 record not present on this checkout")
class TestWaveReportReproducesWave6(unittest.TestCase):
    """wave_report on the real wave 6 record must reproduce the published
    headline numbers exactly, from the sweep and the record alone -- nothing
    else is allowed to influence the arithmetic.
    """

    @classmethod
    def setUpClass(cls):
        out, rc = call(["--results", REAL_RESULTS, "--wave", "6", "--sweep", REAL_SWEEP])
        assert rc == 0
        cls.out = out

    def test_structural_brain_extraction(self):
        self.assertIn(
            "| structural-brain-extraction | 3 | 19/30 | 26/30 | +23 pp (+5, +42) "
            "| 0.0356 |", self.out)

    def test_7t(self):
        self.assertIn(
            "| structural-brain-extraction-7t | 3 | 20/30 | 27/30 | +23 pp (+5, +42) "
            "| 0.0356 |", self.out)

    def test_7t_nodura(self):
        self.assertIn(
            "| structural-brain-extraction-7t-nodura | 3 | 16/30 | 15/30 | "
            "-3 pp (-24, +17) | 1.0000 |", self.out)

    def test_pediatric(self):
        self.assertIn(
            "| structural-brain-extraction-pediatric | 3 | 16/30 | 29/30 | "
            "+43 pp (+25, +62) | < 0.0001 |", self.out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
