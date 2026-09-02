#!/usr/bin/env python
"""Tests for the analysis scripts.

    python -m unittest discover -s benchmark/runner -p "test_*.py" -v

Plain unittest and the standard library only, so this runs on the laptop and
inside the Neurodesktop image without installing anything.

WHY THESE TESTS AND NOT OTHERS
------------------------------
Each one pins a mistake that actually happened, or an invariant whose breakage
would be silent. Nothing here tests that Python works.

  - The statistics are hand-rolled. They were verified once against scipy and
    against textbook cases; this freezes that so a refactor cannot quietly
    change a p-value we have published.
  - `NO-OUTPUT` used to be scored as a failure for runs that simply had not been
    graded yet, which reported a task as 0/70 with 50 masks sitting on disk.
  - Tool detection used to match `references/synthstrip.md`, so *reading about*
    a tool counted as running it -- and only skill arms ship references, making
    the error arm-asymmetric and pointed straight at the headline mechanism.
  - Her QC ratings are three-valued. A boolean model of them was wrong on 84 of
    640 criterion ratings, and would have produced a clean-looking table built
    on fiction.
  - A count-only sort reordered a table between two runs over identical data,
    because Python randomises string hashing per process. That defeats
    diff-as-audit, which is the whole point of make_report.sh.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib import util

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    """Import a sibling script by path -- they are scripts, not a package."""
    spec = util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(script, *args):
    p = subprocess.run([sys.executable, os.path.join(HERE, script)] + list(args),
                       capture_output=True, text=True)
    return p.stdout + p.stderr


class TestStatistics(unittest.TestCase):
    """Frozen against published values, because these produce quoted p-values."""

    def setUp(self):
        self.m = load("mechanism")

    def test_fisher_textbook(self):
        # Fisher's tea tasting, the Wikipedia worked example, and a fully
        # separated table -- three published answers.
        for table, want in (((3, 1, 1, 3), 0.4857142857),
                            ((1, 9, 11, 3), 0.0027594),
                            ((0, 5, 5, 0), 0.0079365)):
            self.assertAlmostEqual(self.m.fisher(*table), want, places=6,
                                   msg="fisher%s" % (table,))

    def test_fisher_published_results(self):
        # The exact tables behind numbers in CLAIMS.md.
        self.assertAlmostEqual(self.m.fisher(47, 3, 31, 19), 0.000179, places=6)
        self.assertAlmostEqual(self.m.fisher(35, 15, 21, 29), 0.008471, places=6)
        self.assertAlmostEqual(self.m.fisher(43, 7, 30, 20), 0.006251, places=6)

    def test_fisher_symmetry(self):
        # Swapping rows or columns must not move a two-sided p.
        self.assertAlmostEqual(self.m.fisher(7, 3, 2, 8), self.m.fisher(3, 7, 8, 2))
        self.assertAlmostEqual(self.m.fisher(7, 3, 2, 8), self.m.fisher(2, 8, 7, 3))

    def test_wilson(self):
        for k, n, want in ((0, 20, (0.0, 16.11)), (1, 1, (20.65, 100.0)),
                           (10, 20, (29.93, 70.07))):
            lo, hi = self.m.wilson(k, n)
            self.assertAlmostEqual(lo, want[0], places=1)
            self.assertAlmostEqual(hi, want[1], places=1)

    def test_wilson_never_leaves_zero_to_one(self):
        # The normal approximation goes negative at k=0; Wilson must not.
        for k, n in ((0, 3), (3, 3), (0, 1), (1, 1)):
            lo, hi = self.m.wilson(k, n)
            self.assertGreaterEqual(lo, 0.0)
            self.assertLessEqual(hi, 100.0)


class TestMechanism(unittest.TestCase):
    """Tool classification, and the doc-filename false positive."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, rows):
        p = os.path.join(self.d, "runs_t.csv")
        cols = ["task", "model", "arm", "rep", "valid", "verdict", "score",
                "dice", "passed", "uptake", "skill_loads", "methods"]
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                base = {c: "" for c in cols}
                base.update({"task": "t", "valid": "1", "score": "100"})
                base.update(r)
                w.writerow(base)
        return p

    def test_splits_selection_from_execution(self):
        rows = []
        # baseline: 2 of 4 reach the panel, both pass
        rows += [{"model": "m", "arm": "env-only", "rep": str(i),
                  "methods": "synthstrip", "passed": "1"} for i in range(2)]
        rows += [{"model": "m", "arm": "env-only", "rep": str(i + 2),
                  "methods": "bet", "passed": "0"} for i in range(2)]
        # skill: 4 of 4 reach the panel, all pass
        rows += [{"model": "m", "arm": "env+skill", "rep": str(i),
                  "methods": "synthstrip", "passed": "1"} for i in range(4)]
        out = run("mechanism.py", self.write(rows), "--robust", "synthstrip")
        self.assertIn("env-only           2/4", out)
        self.assertIn("env+skill          4/4", out)
        # bet-only must be a clean zero, and must appear as its own class
        self.assertIn("other tool only", out)

    def test_no_robust_flag_claims_no_mechanism(self):
        # Guards the anti-circularity contract: without an externally supplied
        # panel the script must refuse to draw the split at all.
        rows = [{"model": "m", "arm": "env-only", "rep": "1",
                 "methods": "bet", "passed": "0"}]
        out = run("mechanism.py", self.write(rows))
        self.assertIn("no mechanism is claimed", out)
        self.assertNotIn("SELECTION", out)

    def test_doc_filename_is_not_a_tool_invocation(self):
        # summarize.py strips `.md` paths before detecting tools. If that ever
        # regresses, a skill-arm run that merely read references/synthstrip.md
        # is scored as having used SynthStrip -- and only skill arms have
        # references, so the error runs straight through the headline claim.
        s = load("summarize")
        self.assertNotIn("synthstrip",
                         s.detect_methods("cat references/synthstrip.md"))
        self.assertIn("synthstrip",
                      s.detect_methods("mri_synthstrip -i in.nii -o out.nii"))

    def test_bet_pattern_does_not_match_hd_bet(self):
        s = load("summarize")
        self.assertNotIn("bet", s.detect_methods("hd-bet -i in.nii -o out.nii"))
        self.assertIn("hd-bet", s.detect_methods("hd-bet -i in.nii -o out.nii"))
        self.assertIn("bet", s.detect_methods("bet in.nii out.nii -f 0.5"))


class TestSummarizeVerdicts(unittest.TestCase):
    """The distinction that reported a task as 0/70 with 50 masks on disk."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.runs = os.path.join(self.d, "runs")
        os.makedirs(self.runs)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def mkrun(self, name, output=False, envelope=None, record_output=True):
        """record_output=False writes the mask but omits the run.json flag,
        which is how an older or interrupted finalize_run leaves a run."""
        p = os.path.join(self.runs, name)
        os.makedirs(p)
        rec = {"task_id": "t", "model": "neurodesk/m",
               "condition": name.split("__")[2], "rep": 1,
               "start": "2026-01-01T00:00:00Z"}
        if output and record_output:
            rec["output_present"] = True
        with open(os.path.join(p, "run.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        if output:
            sd = os.path.join(p, "submissions", "t")
            os.makedirs(sd)
            with open(os.path.join(sd, "output.nii.gz"), "wb") as fh:
                fh.write(b"x")
        if envelope is not None:
            with open(os.path.join(p, "envelope.json"), "w", encoding="utf-8") as fh:
                json.dump(envelope, fh)

    def test_ungraded_is_not_a_failure(self):
        # A mask on disk with no envelope means nobody has graded it, which is
        # not the same as the agent delivering nothing.
        self.mkrun("t__neurodesk-m__env-only__r1", output=True, envelope=None)
        self.mkrun("t__neurodesk-m__env-only__r2", output=False, envelope=None)
        out = run("summarize.py", self.runs, "t", "--out-dir", self.d)
        self.assertIn("NOT-GRADED", out)
        self.assertIn("NO-OUTPUT", out)

    def test_mask_on_disk_beats_a_missing_run_json_flag(self):
        # output_present comes from run.json. If it is missing or stale, a run
        # with a finished mask reads as NO-OUTPUT, which is scored as a failure.
        # The disk is the ground truth, so summarize falls back to looking.
        self.mkrun("t__neurodesk-m__env-only__r1", output=True,
                   record_output=False, envelope=None)
        out = run("summarize.py", self.runs, "t", "--out-dir", self.d)
        self.assertIn("NOT-GRADED", out)
        self.assertNotIn("NO-OUTPUT", out)

    def test_marginal_counts_as_a_failure(self):
        # The grader's published rule is pass = valid and verdict >= acceptable,
        # so marginal is a fail. Leaving it out silently inflated the pass rate.
        self.mkrun("t__neurodesk-m__env-only__r1", output=True,
                   envelope={"valid": True, "score": 45, "verdict": "marginal"})
        run("summarize.py", self.runs, "t", "--out-dir", self.d)
        with open(os.path.join(self.d, "runs_t.csv"), encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(rows[0]["passed"].lower(), "false")


class TestQcvalSimulate(unittest.TestCase):
    """Three-valued ratings, and the logic model that must be verified first."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.q = os.path.join(self.d, "qc")
        self.runs = os.path.join(self.d, "runs")
        os.makedirs(self.q)
        os.makedirs(self.runs)
        self.mod = load("qcval_simulate")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def mk(self, name, ratings, verdict, passed, metrics=None):
        rd = os.path.join(self.runs, name)
        os.makedirs(rd)
        with open(os.path.join(rd, "envelope.json"), "w", encoding="utf-8") as fh:
            json.dump({"valid": passed, "score": 100 if passed else 0,
                       "verdict": "acceptable" if passed else "invalid",
                       "gate_failures": [] if passed else ["volume_plausible"]}, fh)
        crit = {"criterion": "c1", "rating": self.mod.worst(ratings.values()),
                "metrics": ratings}
        with open(os.path.join(self.q, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"criteria": [crit], "numeric_verdict": verdict,
                       "metrics": metrics or {}}, fh)

    def test_worst_rating_wins(self):
        w = self.mod.worst
        self.assertEqual(w(["PASS", "PASS"]), "PASS")
        self.assertEqual(w(["PASS", "BORDERLINE"]), "BORDERLINE")
        self.assertEqual(w(["BORDERLINE", "FAIL"]), "FAIL")
        # UNVERIFIED is an absence of evidence, not a rating, and must not vote.
        self.assertEqual(w(["PASS", "UNVERIFIED"]), "PASS")
        self.assertIsNone(w(["UNVERIFIED"]))

    def test_borderline_is_not_fail(self):
        # The boolean model got this wrong on 84 of 640 real ratings.
        self.assertEqual(self.mod.worst(["PASS", "BORDERLINE"]), "BORDERLINE")
        self.assertNotEqual(self.mod.worst(["PASS", "BORDERLINE"]), "FAIL")

    def test_refuses_to_simulate_when_the_model_is_wrong(self):
        # A verdict inconsistent with its criteria means we have misunderstood
        # her logic, and a sweep built on that would look like a measurement.
        self.mk("r1", {"a": "FAIL"}, "PASS", True)
        out = run("qcval_simulate.py", self.q, self.runs)
        self.assertIn("model rejected", out)

    def test_relax_unblocks_and_reports_distinct_masks(self):
        self.mk("r1", {"a": "FAIL"}, "FAIL", True, {"a": 1.0})
        self.mk("r2", {"a": "FAIL"}, "FAIL", True, {"a": 2.0})
        out = run("qcval_simulate.py", self.q, self.runs, "--relax", "a")
        self.assertIn("rejects every one", out)
        self.assertIn("forced to PASS", out)
        self.assertIn("over DISTINCT masks", out)


class TestCapability(unittest.TestCase):
    """The ceiling effect must be surfaced, not silently resolved."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, spec):
        p = os.path.join(self.d, "runs_t.csv")
        cols = ["task", "model", "arm", "rep", "valid", "passed", "methods"]
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for model, arm, k, n in spec:
                for i in range(n):
                    w.writerow({"task": "t", "model": model, "arm": arm,
                                "rep": str(i), "valid": "1",
                                "passed": "1" if i < k else "0",
                                "methods": "synthstrip"})
        return p

    def test_flags_when_the_two_metrics_disagree(self):
        # Weak model gains more in percentage points; strong model removes a
        # larger share of its failures. Ranking on either alone is an artefact
        # of the other, so the script must refuse the claim.
        p = self.write([("weak", "env-only", 1, 10), ("weak", "env+skill", 7, 10),
                        ("strong", "env-only", 8, 10), ("strong", "env+skill", 10, 10)])
        out = run("capability.py", p)
        self.assertIn("THE TWO METRICS DISAGREE", out)

    def test_no_flag_when_both_metrics_agree(self):
        # Both point the same way, so there is nothing to warn about and a
        # warning here would train the reader to ignore it.
        p = self.write([("weak", "env-only", 1, 10), ("weak", "env+skill", 2, 10),
                        ("strong", "env-only", 5, 10), ("strong", "env+skill", 10, 10)])
        out = run("capability.py", p)
        self.assertNotIn("THE TWO METRICS DISAGREE", out)

    def test_rfr_undefined_at_a_perfect_baseline(self):
        # No failures to remove. Must print "-" rather than divide by zero or
        # invent a number that would then get averaged.
        c = load("capability")
        self.assertIsNone(c.rfr(10, 10, 10, 10))
        self.assertEqual(c.rfr(0, 10, 10, 10), 100.0)
        self.assertEqual(c.rfr(5, 10, 5, 10), 0.0)

    def test_skill_making_things_worse_is_reported_as_negative(self):
        # minimax on motion went 10/10 to 7/10. A harm must not be clipped to
        # zero or dropped, because it is the only falsification we have.
        p = self.write([("m", "env-only", 10, 10), ("m", "env+skill", 7, 10)])
        out = run("capability.py", p)
        self.assertIn("-30pp", out.replace(" ", ""))


class TestRunDates(unittest.TestCase):
    """Cells carry when they ran, so a retired model's numbers can be dated."""

    def setUp(self):
        self.f = load("summarize").run_dates

    def r(self, start="", end=""):
        return {"start": start, "end": end}

    def test_spans_first_to_last(self):
        rs = [self.r(end="2026-08-13T06:01:35Z"), self.r(end="2026-08-15T09:00:00Z"),
              self.r(end="2026-08-14T00:00:00Z")]
        self.assertEqual(self.f(rs), ("2026-08-13", "2026-08-15"))

    def test_date_only_no_false_precision(self):
        """A cell's runs are hours apart; a timestamp would imply they were not."""
        first, last = self.f([self.r(end="2026-08-13T06:01:35Z")])
        self.assertEqual((first, last), ("2026-08-13", "2026-08-13"))

    def test_falls_back_to_start_when_the_run_never_ended(self):
        """A killed run has a start and no end. It still happened on a date."""
        self.assertEqual(self.f([self.r(start="2026-08-13T06:00:00Z")]),
                         ("2026-08-13", "2026-08-13"))

    def test_no_dates_is_none_not_today(self):
        """Defaulting to now would silently date old results to the day of the rebuild."""
        self.assertEqual(self.f([self.r(), self.r()]), (None, None))

    def test_empty_group(self):
        self.assertEqual(self.f([]), (None, None))

    def test_undated_runs_do_not_drag_the_range(self):
        rs = [self.r(end="2026-08-15T00:00:00Z"), self.r()]
        self.assertEqual(self.f(rs), ("2026-08-15", "2026-08-15"))


class TestReportDeterminism(unittest.TestCase):
    """A report that changes when nothing changed defeats diff-as-audit."""

    def test_tie_broken_sorts_are_stable(self):
        # Two tools with equal counts must always print in the same order.
        # per_tool is built by iterating a set, and Python randomises string
        # hashing per process, so a count-only key reordered a real table
        # between consecutive runs of identical data.
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "runs_t.csv")
            cols = ["task", "model", "arm", "rep", "valid", "passed", "methods"]
            with open(p, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for i, m in enumerate(["afni", "ants", "robex", "deepbet"]):
                    w.writerow(["t", "m", "env-only", str(i), "1", "1", m])
            first = run("mechanism.py", p)
            for _ in range(6):
                self.assertEqual(run("mechanism.py", p), first,
                                 "per-tool table order is not stable")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
