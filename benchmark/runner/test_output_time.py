#!/usr/bin/env python
"""Tests for output_time.py: when a passing run last put its output in place.

    python -m unittest test_output_time -v

Each test shows the check can fail: a placement that keeps an older mtime is found, a
command that gives a fresh mtime or only reads the output is not, a run without its
artifact is unchecked, and end to end the declared test moves by exactly the planted
placements.
"""
import csv
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import output_time as ot  # noqa: E402

O = "submissions/task-a/case-012/output.nii.gz"
A, B = "env+skill", "env+skill-michele"


class Places(unittest.TestCase):
    YES = ["mv /tmp/mask.nii.gz " + O, "mv mask.nii.gz output.nii.gz", "cp -p m.nii.gz " + O,
           "cp -a m.nii.gz " + O, "cp --preserve=timestamps m.nii.gz " + O, "rsync -av m.nii.gz " + O,
           "ln -sf /w/m.nii.gz " + O, "install -p m.nii.gz " + O, "gzip -f submissions/task-a/case-012/output.nii",
           "mv /tmp/output.nii.gz submissions/task-a/case-012/", "sudo mv m.nii.gz " + O, 'mv m.nii.gz "$OUT"',
           "tar -xzf res.tgz " + O]
    NO = ["gzip -t " + O, "cp m.nii.gz " + O, "cp -f m.nii.gz " + O, "rsync m.nii.gz " + O, "ls -la " + O,
          "fslstats " + O + " -V", "python3 -c 'import gzip'", "cp -p " + O + " backup/copy.nii.gz",
          "gunzip -c " + O, "tar -tzf res.tgz", "sha256sum " + O, "mv " + O + " /tmp/old.nii.gz",
          "ln -s " + O + " results/mask.nii.gz", "echo 'unbalanced " + O]

    def test_placements_that_keep_an_older_mtime_are_found(self):
        for c in self.YES:
            self.assertTrue(ot.places_output(c, {"OUT": O}), c)

    def test_fresh_writes_and_reads_are_not(self):
        for c in self.NO:
            self.assertFalse(ot.places_output(c, {"OUT": O}), c)

    def test_python_calls(self):
        self.assertTrue(ot.PY_PLACE.search("shutil.move(src, '%s')" % O))
        self.assertTrue(ot.PY_PLACE.search("os.replace(a, b)"))
        self.assertFalse(ot.PY_PLACE.search("shutil.copy(src, dst)"))
        self.assertFalse(ot.PY_PLACE.search("s.replace('a', 'b')"))


def bash(cmd, t):
    return ("bash", {"command": cmd}, t)


class LastPlacement(unittest.TestCase):
    def test_the_last_placement_counts(self):
        calls = [bash("cp /w/m.nii.gz " + O, 600.0), bash("mv /w/m2.nii.gz " + O, 3000.0),
                 bash("fslstats %s -V" % O, 3100.0)]
        self.assertEqual(ot.last_placement(calls, 0.0), (3000.0, "shell"))

    def test_a_plain_copy_is_no_placement(self):
        self.assertEqual(ot.last_placement([bash("cp /w/m.nii.gz " + O, 600.0)], 0.0), (None, None))

    def test_a_variable_in_the_same_call_is_resolved(self):
        calls = [bash('OUT=%s && mv /w/m.nii.gz "$OUT"' % O, 900.0)]
        self.assertEqual(ot.last_placement(calls, 0.0), (900.0, "shell"))

    def test_python_inline_and_a_script_the_agent_ran(self):
        inline = [bash("python3 -c \"import shutil; shutil.move('/w/m.nii.gz', '%s')\"" % O, 700.0)]
        self.assertEqual(ot.last_placement(inline, 0.0), (700.0, "python"))
        script = [("write", {"filePath": "/w/place.py",
                             "content": "import shutil\nshutil.move('/w/m.nii.gz', '%s')\n" % O}, 100.0),
                  bash("python3 /w/place.py", 1200.0)]
        self.assertEqual(ot.last_placement(script, 0.0), (1200.0, "script"))

    def test_times_are_from_the_start(self):
        self.assertEqual(ot.last_placement([bash("mv a.nii.gz " + O, 1000.0)], 400.0), (600.0, "shell"))


def make_db(path, calls):
    """An opencode-like database: part(time_created, data) with tool parts."""
    con = sqlite3.connect(path)
    con.execute("create table part (id text, session_id text, time_created integer, data text)")
    for i, (tool, inp, t) in enumerate(calls):
        data = {"type": "tool", "tool": tool,
                "state": {"status": "completed", "input": inp,
                          "time": {"start": int((t - 5) * 1000), "end": int(t * 1000)}}}
        con.execute("insert into part values (?, ?, ?, ?)", ("p%d" % i, "s1", int((t - 5) * 1000), json.dumps(data)))
    con.execute("insert into part values ('x', 's1', 1, ?)", (json.dumps({"type": "text", "text": "mv a " + O}),))
    con.execute("insert into part values ('y', 's1', 2, 'not json')")
    con.commit()
    con.close()


class Database(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_tool_calls_take_the_end_time_and_skip_other_parts(self):
        db = os.path.join(self.d, "o.db")
        make_db(db, [bash("mv a.nii.gz " + O, 1000.0)])
        calls = list(ot.tool_calls(db))
        self.assertEqual(calls, [("bash", {"command": "mv a.nii.gz " + O}, 1000.0)])

    def test_the_archive_is_read_under_fixed_names_only(self):
        db = os.path.join(self.d, "src.db")
        make_db(db, [bash("mv a.nii.gz " + O, 1000.0)])
        run = os.path.join(self.d, "archive", "1001")
        os.makedirs(run)
        z = os.path.join(run, "cell-task-a@case-012-nrp-x-%s-r1-1001.zip" % B)
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(db, "runs/task-a@case-012__nrp-x__%s__r1%s" % (B, ot.DB))
            zf.writestr("../../evil.txt", "x")
        self.assertEqual(ot.find_artifact(os.path.join(self.d, "archive"), "1001", "nrp/x", B, 1), z)
        self.assertIsNone(ot.find_artifact(os.path.join(self.d, "archive"), "1001", "nrp/x", A, 1))
        dest = os.path.join(self.d, "w")
        os.mkdir(dest)
        got = ot.extract_db(z, dest)
        self.assertEqual(got, os.path.join(dest, "opencode.db"))
        self.assertEqual(sorted(os.listdir(dest)), ["opencode.db"])
        self.assertFalse(os.path.exists(os.path.join(self.d, "evil.txt")))
        self.assertEqual(ot.last_placement(ot.tool_calls(got), 0.0), (1000.0, "shell"))


class EndToEnd(unittest.TestCase):
    """Results as assemble writes them, artifacts as archived, then both scripts as run:
    B's outputs were moved into place at 4000 s while their mtimes say 600 s; one A run
    has no artifact."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_the_declared_test_moves_by_the_planted_placements(self):
        fam, start = "task-a", "2026-01-01T00:00:00Z"
        t0 = 1767225600.0  # start as epoch seconds
        tasks = {}
        for s in ("case-012", "case-013"):
            tasks["%s@%s" % (fam, s)] = {"family": fam, "subject": s, "models": ["nrp/x"], "arms": [A, B]}
        sp = os.path.join(self.d, "sweep.json")
        json.dump({"waves": {"10": {"tasks": tasks, "reps": 3, "arms_compared": {"primary": [B, A]}}}},
                  open(sp, "w"))
        res, arc = os.path.join(self.d, "results"), os.path.join(self.d, "archive")
        os.makedirs(res)
        cols = ["task", "model", "arm", "rep", "valid", "passed", "lost", "tokens_total"]
        for n, t in enumerate(sorted(tasks)):
            ci = str(1001 + n)
            out = "submissions/%s/%s/output.nii.gz" % (fam, tasks[t]["subject"])
            with open(os.path.join(res, "runs_%s.csv" % t), "w", newline="") as fh:
                w = csv.DictWriter(fh, cols)
                w.writeheader()
                for rep in (1, 2, 3):
                    for arm, sec in ((B, 600), (A, 3000)):
                        w.writerow({"task": t, "model": "nrp/x", "arm": arm, "rep": rep, "valid": "True",
                                    "passed": "True", "lost": "", "tokens_total": 1000})
                        rd = os.path.join(res, "runs", t, "%s__nrp-x__%s__r%d" % (t, arm, rep))
                        os.makedirs(rd)
                        json.dump({"seconds_to_output": sec, "ci_run": ci + "/1/rep", "start": start},
                                  open(os.path.join(rd, "run.json"), "w"))
                        if arm == A and t.endswith("case-013") and rep == 3:
                            continue  # no artifact: unchecked
                        cmd = ("mv /w/m.nii.gz " if arm == B else "cp /w/m.nii.gz ") + out
                        db = os.path.join(self.d, "tmp.db")
                        make_db(db, [bash(cmd, t0 + (4000 if arm == B else 3000))])
                        os.makedirs(os.path.join(arc, ci), exist_ok=True)
                        z = os.path.join(arc, ci, "cell-%s-nrp-x-%s-r%d-%s.zip" % (t, arm, rep, ci))
                        with zipfile.ZipFile(z, "w") as zf:
                            zf.write(db, "runs/%s__nrp-x__%s__r%d%s" % (t, arm, rep, ot.DB))
                        os.remove(db)
        work, pj = os.path.join(self.d, "work"), os.path.join(self.d, "placements.json")
        os.mkdir(work)
        p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "output_time.py"), "--results", res,
                            "--sweep", sp, "--wave", "10", "--artifacts", arc, "--work", work, "--json", pj],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("checked 11, not checked 1; placed later than recorded 6", p.stdout)
        self.assertEqual(os.listdir(work), [])
        recs = json.load(open(pj))
        self.assertEqual(recs["1001|nrp/x|%s|1" % B], {"checked": True, "placed_s": 4000, "kind": "shell",
                                                        "recorded_s": 600})
        self.assertEqual(recs["1001|nrp/x|%s|1" % A]["placed_s"], None)

        def effect(*extra):
            out = os.path.join(self.d, "pe.json")
            q = subprocess.run([sys.executable, "-I", os.path.join(HERE, "permutation_effect.py"), "--results",
                                res, "--sweep", sp, "--wave", "10", "--permutations", "200", "--json", out]
                               + list(extra), capture_output=True, text=True)
            self.assertEqual(q.returncode, 0, q.stderr)
            return json.load(open(out))[fam]["minutes_to_pass"]

        self.assertAlmostEqual(effect()["effect_minutes"], (600 - 3000) / 60.0)
        m = effect("--placements", pj)
        self.assertAlmostEqual(m["effect_minutes"], (4000 - 3000) / 60.0)
        self.assertEqual(m["placed_later"], {B: 6, A: 0})
        self.assertEqual(m["placement_unchecked"], {B: 0, A: 1})


if __name__ == "__main__":
    unittest.main()
