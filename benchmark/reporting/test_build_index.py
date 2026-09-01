#!/usr/bin/env python3
"""Tests for build_index.py. Stdlib only; run with `python test_build_index.py`.

Each one pins something that would otherwise be checked by squinting at a rendered page,
which is how a dashboard ends up disagreeing with the numbers it displays. The Wilson test
pins agreement with summarize.py: the dashboard and the analysis scripts quote the same
interval, so the two implementations must not drift.
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "runner"))

import build_index as bi  # noqa: E402


def summary(cells, effects=None, task="t"):
    return {"task": task, "cells": cells, "skill_effect": effects or {}}


def effects_html(entries):
    return bi.build_effects(entries)[0]


class Wilson(unittest.TestCase):
    def test_matches_summarize(self):
        """The dashboard's interval must be the analysis scripts' interval."""
        try:
            import summarize
        except ImportError:
            self.skipTest("summarize.py not importable from here")
        for k, n in [(0, 10), (1, 10), (5, 10), (8, 10), (10, 10), (80, 100), (3, 7)]:
            lo, hi = summarize.wilson(k, n)
            mlo, mhi = bi.wilson(k, n)
            self.assertAlmostEqual(lo, mlo, places=9, msg="lo at %d/%d" % (k, n))
            self.assertAlmostEqual(hi, mhi, places=9, msg="hi at %d/%d" % (k, n))

    def test_sane_at_the_ends(self):
        """0/10 and 10/10 are where the normal approximation returns impossible bounds."""
        lo, hi = bi.wilson(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertTrue(0 < hi < 0.5)
        lo, hi = bi.wilson(10, 10)
        self.assertEqual(hi, 1.0)
        self.assertTrue(0.5 < lo < 1)

    def test_n_zero_does_not_divide_by_zero(self):
        self.assertEqual(bi.wilson(0, 0), (0.0, 0.0))

    def test_more_runs_narrows_the_interval(self):
        """The whole reason n is shown: 8/10 and 80/100 are both 80%."""
        a = bi.wilson(8, 10)
        b = bi.wilson(80, 100)
        self.assertLess(b[1] - b[0], a[1] - a[0])


class RateCell(unittest.TestCase):
    def test_shows_the_denominator(self):
        h = bi.rate_cell(8, 10)
        self.assertIn("8/10", h)
        self.assertIn("80%", h)

    def test_same_rate_different_n_renders_differently(self):
        self.assertNotEqual(bi.rate_cell(8, 10), bi.rate_cell(80, 100))

    def test_short_cell_is_marked(self):
        self.assertIn("short", bi.rate_cell(2, 3, expect=10))
        self.assertIn("†", bi.rate_cell(2, 3, expect=10))
        self.assertNotIn("short", bi.rate_cell(8, 10, expect=10))

    def test_short_cell_needs_an_expectation(self):
        """Without a repeat count there is nothing to be short of; do not guess."""
        self.assertNotIn("short", bi.rate_cell(2, 3, expect=0))

    def test_the_bar_is_the_width_of_the_interval(self):
        """The floor on the bar width is in proportion units. A floor of 0.8 there drew
        every interval at 80% of the track, which is the opposite of the point."""
        def width(k, n):
            h = bi.rate_cell(k, n)
            return float(h.split('width:')[1].split('%')[0])
        lo, hi = bi.wilson(8, 10)
        self.assertAlmostEqual(width(8, 10), (hi - lo) * 100, places=1)
        self.assertLess(width(80, 100), width(8, 10))

    def test_the_bar_is_optional(self):
        """The effect table carries a CI on the difference; a third hairline is noise."""
        self.assertNotIn('class="ci"', bi.rate_cell(8, 10, bar=False))
        self.assertIn('class="ci"', bi.rate_cell(8, 10))

    def test_tooltip_states_the_interval(self):
        """The exact bounds must be reachable; a bar alone is not a number."""
        h = bi.rate_cell(0, 10)
        self.assertIn("0 of 10 passed", h)
        self.assertIn("95% CI", h)


class ExpectedReps(unittest.TestCase):
    def test_modal_n(self):
        s = summary({"a|env-only": {"n": 10, "passes": 1},
                     "a|env+skill": {"n": 10, "passes": 2},
                     "b|env-only": {"n": 4, "passes": 0}})
        self.assertEqual(bi.expected_reps(s), 10)

    def test_no_cells(self):
        self.assertEqual(bi.expected_reps(summary({})), 0)


class FmtPP(unittest.TestCase):
    def test_zero_has_no_sign(self):
        self.assertEqual(bi.fmt_pp(0.0), "0")

    def test_whole_numbers_lose_the_decimal(self):
        self.assertEqual(bi.fmt_pp(50.0), "+50")
        self.assertEqual(bi.fmt_pp(-3.0), "-3")

    def test_a_bound_near_zero_keeps_its_decimal(self):
        """+0.4 must not print as "+0" and imply the interval excludes no effect."""
        self.assertEqual(bi.fmt_pp(0.4), "+0.4")
        self.assertEqual(bi.fmt_pp(-0.4), "-0.4")


class FmtCI(unittest.TestCase):
    def test_bounds_share_a_precision(self):
        """-11.2, +51 reads as two different measurements. It is one interval."""
        self.assertEqual(bi.fmt_ci(-11.2, 51.0), "-11.2, +51.0")

    def test_whole_bounds_stay_terse(self):
        self.assertEqual(bi.fmt_ci(-11.0, 51.0), "-11, +51")

    def test_a_bound_near_zero_survives(self):
        self.assertEqual(bi.fmt_ci(-0.2, 67.6), "-0.2, +67.6")


class Effects(unittest.TestCase):
    def test_bare_model_key(self):
        """Older summaries keyed skill_effect by model alone."""
        s = summary({}, {"glm-5.2": {"delta_pp": 20.0}})
        self.assertEqual(bi.effect_rows(s), [("glm-5.2", "env+skill", {"delta_pp": 20.0})])

    def test_model_pipe_arm_key(self):
        """Multi-skill-arm sweeps key it model|arm."""
        s = summary({}, {"glm-5.2|env+skillB": {"delta_pp": 5.0}})
        self.assertEqual(bi.effect_rows(s)[0][1], "env+skillB")

    def test_head_to_head_is_not_a_baseline_effect(self):
        """skillA_vs_skillB entries answer a different question; keep them out."""
        s = summary({}, {"glm|env+skillA_vs_env+skillB": {"delta_pp": -3.0}})
        self.assertEqual(bi.effect_rows(s), [])

    def test_ranked_by_effect_size(self):
        s = summary({"m|env-only": {"n": 10, "passes": 1}},
                    {"small": {"delta_pp": 10.0, "ci95_pp": [-5, 25], "env_only_n": 10,
                               "env_skill_n": 10},
                     "big": {"delta_pp": 50.0, "ci95_pp": [12, 76], "env_only_n": 10,
                             "env_skill_n": 10}})
        h = effects_html([("t", s, "")])
        self.assertLess(h.index("big"), h.index("small"))

    def test_ci_crossing_zero_is_greyed(self):
        s = summary({}, {"m": {"delta_pp": 20.0, "ci95_pp": [-11, 51],
                               "env_only_n": 10, "env_skill_n": 10}})
        self.assertIn('<tr class="ns">', effects_html([("t", s, "")]))

    def test_ci_clear_of_zero_is_not_greyed(self):
        s = summary({}, {"m": {"delta_pp": 50.0, "ci95_pp": [12, 76],
                               "env_only_n": 10, "env_skill_n": 10}})
        self.assertNotIn('<tr class="ns">', effects_html([("t", s, "")]))

    def test_ci_touching_zero_counts_as_crossing(self):
        """[0, 40] does not exclude no effect. Rounding must not upgrade a result."""
        s = summary({}, {"m": {"delta_pp": 20.0, "ci95_pp": [0, 40],
                               "env_only_n": 10, "env_skill_n": 10}})
        self.assertIn('<tr class="ns">', effects_html([("t", s, "")]))

    def test_counts_are_reported_to_the_header(self):
        """The "N of M clear of zero" line must be counted, not asserted by hand."""
        s = summary({}, {"clear": {"delta_pp": 50.0, "ci95_pp": [12, 76],
                                   "env_only_n": 10, "env_skill_n": 10},
                         "muddy": {"delta_pp": 20.0, "ci95_pp": [-11, 51],
                                   "env_only_n": 10, "env_skill_n": 10}})
        _, n_sep, n_all = bi.build_effects([("t", s, "")])
        self.assertEqual((n_sep, n_all), (1, 2))

    def test_no_comparison_says_so(self):
        h = effects_html([("t", summary({"m|env-only": {"n": 10, "passes": 1}}), "")])
        self.assertIn("No task has both arms yet", h)

    def test_short_arm_is_marked(self):
        s = summary({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 4, "passes": 1}},
                    {"m": {"delta_pp": 15.0, "ci95_pp": [-20, 50], "env_only_pass": 1,
                           "env_only_n": 10, "env_skill_pass": 1, "env_skill_n": 4}})
        self.assertIn("†", effects_html([("t", s, "")]))

    def test_effect_rows_carry_one_interval_not_three(self):
        s = summary({}, {"m": {"delta_pp": 50.0, "ci95_pp": [12, 76], "env_only_pass": 5,
                               "env_only_n": 10, "env_skill_pass": 10, "env_skill_n": 10}})
        h = effects_html([("t", s, "")])
        self.assertEqual(h.count('class="ci"'), 0)
        self.assertEqual(h.count('class="whisk"'), 1)

    def test_rows_are_ranked_and_numbered(self):
        s = summary({}, {"a": {"delta_pp": 10.0, "ci95_pp": [-5, 25],
                               "env_only_n": 10, "env_skill_n": 10},
                         "b": {"delta_pp": 50.0, "ci95_pp": [12, 76],
                               "env_only_n": 10, "env_skill_n": 10}})
        h = effects_html([("t", s, "")])
        self.assertIn('<td class="rank">1</td>', h)
        self.assertIn('<td class="rank">2</td>', h)


class DeltaBar(unittest.TestCase):
    def test_zero_delta_sits_on_the_axis(self):
        self.assertIn("nil", bi.delta_bar(0, -30, 30))

    def test_sign_picks_the_colour(self):
        self.assertIn("fill pos", bi.delta_bar(20, 0, 40))
        self.assertIn("fill neg", bi.delta_bar(-20, -40, 0))

    def test_bounds_are_clamped(self):
        """A CI wider than the axis must not render outside the track."""
        h = bi.delta_bar(100, -200, 200)
        for tok in h.split("left:")[1:]:
            v = float(tok.split("%")[0])
            self.assertTrue(0 <= v <= 100, "left %s out of track" % v)


class Grid(unittest.TestCase):
    def test_baseline_arm_comes_first(self):
        s = summary({"m|env+skill": {"n": 10, "passes": 10},
                     "m|env-only": {"n": 10, "passes": 8}})
        h = bi.build_grid([("t", s, "")])
        self.assertLess(h.index("no skill"), h.index("with skill"))

    def test_missing_model_is_a_dash_not_a_zero(self):
        """An unrun cell and a 0% cell are different claims."""
        a = summary({"m1|env-only": {"n": 10, "passes": 5}}, task="a")
        b = summary({"m2|env-only": {"n": 10, "passes": 5}}, task="b")
        h = bi.build_grid([("a", a, ""), ("b", b, "")])
        self.assertIn("&mdash;", h)

    def test_missing_cell_sorts_below_zero_percent(self):
        """Sorting must not float an unrun cell up next to a 0% one."""
        a = summary({"m1|env-only": {"n": 10, "passes": 0}}, task="a")
        b = summary({"m2|env-only": {"n": 10, "passes": 5}}, task="b")
        self.assertIn('data-v="-1"', bi.build_grid([("a", a, ""), ("b", b, "")]))

    def test_names_are_escaped(self):
        """Task, model and arm names come from files, so they are not trusted markup."""
        s = summary({"<img>|env-only": {"n": 10, "passes": 1}})
        h = bi.build_grid([("a<b", s, "r.html")])
        self.assertNotIn("<img>", h)
        self.assertIn("&lt;img&gt;", h)
        self.assertIn("a&lt;b", h)

    def test_unknown_arm_keeps_its_own_name(self):
        s = summary({"m|env+skillB": {"n": 10, "passes": 3}})
        self.assertIn("env+skillB", bi.build_grid([("t", s, "")]))


class Discover(unittest.TestCase):
    def test_picks_up_a_report_dir(self):
        with tempfile.TemporaryDirectory() as d:
            for t in ("alpha", "beta"):
                with open(os.path.join(d, "summary_%s.json" % t), "w", encoding="utf-8") as fh:
                    json.dump(summary({"m|env-only": {"n": 10, "passes": 1}}, task=t), fh)
            with open(os.path.join(d, "report_alpha.html"), "w", encoding="utf-8") as fh:
                fh.write("<html></html>")
            got = bi.discover(d)
            self.assertEqual([n for n, _, _ in got], ["alpha", "beta"])
            self.assertEqual([h for _, _, h in got], ["report_alpha.html", ""])

    def test_absent_report_gives_no_dead_link(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "summary_x.json"), "w", encoding="utf-8") as fh:
                json.dump(summary({}, task="x"), fh)
            self.assertEqual(bi.discover(d)[0][2], "")

    def test_empty_report_is_not_linked(self):
        """build_report truncates before it writes, so a crash leaves 0 bytes behind.
        Linking that gives a row that looks clickable and opens nothing."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "summary_x.json"), "w", encoding="utf-8") as fh:
                json.dump(summary({}, task="x"), fh)
            open(os.path.join(d, "report_x.html"), "w").close()
            self.assertEqual(bi.discover(d)[0][2], "")


class Page(unittest.TestCase):
    def real(self):
        p = os.path.join(HERE, "examples", "brain-extraction-7t", "summary.json")
        if not os.path.exists(p):
            self.skipTest("example removed")
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_real_example_renders(self):
        h = bi.build([("Brain extraction - 7T", self.real(), "")])
        self.assertIn("<!doctype html>", h)
        self.assertIn("8/10", h)          # denominator visible
        self.assertIn("Skill effect", h)
        self.assertIn("Pass rate", h)

    def test_the_header_counts_the_sweep(self):
        h = bi.build([("Brain extraction - 7T", self.real(), "")])
        self.assertIn("5 models", h)
        self.assertIn("100</b> runs", h)
        self.assertIn("10 per cell", h)

    def test_sortable_headers_and_a_filter_are_wired(self):
        h = bi.build([("t", self.real(), "")])
        self.assertIn("data-s", h)
        self.assertIn('data-filter="fx"', h)
        self.assertIn('id="fx"', h)

    def test_no_external_hosts(self):
        """Self-contained means it renders with no network. Do not regress that."""
        s = summary({"m|env-only": {"n": 10, "passes": 5}})
        h = bi.build([("t", s, "")])
        for tok in ("http://", "https://", "//cdn", "src=", "@import"):
            self.assertNotIn(tok, h, "external reference: %s" % tok)

    def test_no_unfilled_placeholder(self):
        h = bi.build([("t", summary({"m|env-only": {"n": 10, "passes": 5}}), "")])
        self.assertNotIn("__", h)
        self.assertNotIn("{}", h)


if __name__ == "__main__":
    unittest.main(verbosity=2)
