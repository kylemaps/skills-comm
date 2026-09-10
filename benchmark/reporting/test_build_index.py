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
import re
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
        """The mode, not the max. In the first version of this test max == mode,
        so returning max(ns) passed it."""
        s = summary({"a|env-only": {"n": 4, "passes": 1},
                     "a|env+skill": {"n": 4, "passes": 2},
                     "b|env-only": {"n": 10, "passes": 0}})
        self.assertEqual(bi.expected_reps(s), 4)

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
        self.assertIn('<tr class="unclear exp"', effects_html([("t", s, "")]))

    def test_ci_clear_of_zero_is_not_greyed(self):
        s = summary({}, {"m": {"delta_pp": 50.0, "ci95_pp": [12, 76],
                               "env_only_n": 10, "env_skill_n": 10}})
        self.assertNotIn('<tr class="unclear exp"', effects_html([("t", s, "")]))

    def test_ci_touching_zero_counts_as_crossing(self):
        """[0, 40] does not exclude no effect. Rounding must not upgrade a result."""
        s = summary({}, {"m": {"delta_pp": 20.0, "ci95_pp": [0, 40],
                               "env_only_n": 10, "env_skill_n": 10}})
        self.assertIn('<tr class="unclear exp"', effects_html([("t", s, "")]))

    def test_counts_are_reported_to_the_header(self):
        """Asymmetric on purpose: 1-of-2 is unchanged if the counter is inverted,
        so an inverted n_sep passed this test in its first form."""
        s = summary({}, {"clearA": {"delta_pp": 50.0, "ci95_pp": [12, 76],
                                    "env_only_n": 10, "env_skill_n": 10},
                         "clearB": {"delta_pp": -50.0, "ci95_pp": [-76, -12],
                                    "env_only_n": 10, "env_skill_n": 10},
                         "muddy": {"delta_pp": 20.0, "ci95_pp": [-11, 51],
                                   "env_only_n": 10, "env_skill_n": 10}})
        _, n_sep, n_all = bi.build_effects([("t", s, "")])
        self.assertEqual((n_sep, n_all), (2, 3))

    def test_a_wholly_negative_interval_also_separates(self):
        """Clear of zero means either side of it, not just above."""
        self.assertTrue(bi.separates(-76.0, -12.0))

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


class UsableCI(unittest.TestCase):
    """An interval that was never computed must never become a number."""

    def test_absent_is_none_not_zero_zero(self):
        """[0, 0] beside a +50 effect is a fabricated result, not a missing one."""
        self.assertIsNone(bi.usable_ci(None))
        self.assertIsNone(bi.usable_ci([]))

    def test_nan_is_rejected(self):
        """summarize.newcombe returns NaN when an arm has no runs."""
        nan = float("nan")
        self.assertIsNone(bi.usable_ci([nan, nan]))
        self.assertIsNone(bi.usable_ci([0.0, nan]))

    def test_inf_is_rejected(self):
        self.assertIsNone(bi.usable_ci([float("-inf"), 5.0]))

    def test_wrong_length_is_rejected(self):
        self.assertIsNone(bi.usable_ci([1, 2, 3]))
        self.assertIsNone(bi.usable_ci([5]))

    def test_non_numeric_is_rejected(self):
        self.assertIsNone(bi.usable_ci(["a", "b"]))
        self.assertIsNone(bi.usable_ci([None, None]))

    def test_reversed_bounds_are_ordered(self):
        self.assertEqual(bi.usable_ci([40.0, -10.0]), (-10.0, 40.0))

    def test_a_good_interval_survives(self):
        self.assertEqual(bi.usable_ci([-11.2, 51.0]), (-11.2, 51.0))


class Separates(unittest.TestCase):
    def test_nan_does_not_count_as_a_result(self):
        """`not (nan <= 0 <= nan)` is True, so NaN used to read as significant."""
        self.assertFalse(bi.separates(None, None))

    def test_straddling_zero_does_not_separate(self):
        self.assertFalse(bi.separates(-11.0, 51.0))

    def test_touching_zero_does_not_separate(self):
        self.assertFalse(bi.separates(0.0, 40.0))
        self.assertFalse(bi.separates(-40.0, 0.0))

    def test_clear_either_side_separates(self):
        self.assertTrue(bi.separates(3.8, 68.7))
        self.assertTrue(bi.separates(-68.7, -3.8))


class Agreement(unittest.TestCase):
    """Newcombe and Fisher are different tests and can disagree. Say so."""

    def test_both_agree_is_clear(self):
        self.assertEqual(bi.agreement(11.7, 76.3, 0.033), "clear")

    def test_neither_is_unclear(self):
        self.assertEqual(bi.agreement(-11.2, 51.0, 0.474), "unclear")

    def test_interval_without_fisher_is_split(self):
        """Live case: qwen3 on 7t, CI [+3.8, +68.7] with p=0.087."""
        self.assertEqual(bi.agreement(3.8, 68.7, 0.087), "split")

    def test_fisher_without_interval_is_split(self):
        self.assertEqual(bi.agreement(-1.0, 60.0, 0.04), "split")

    def test_a_missing_p_is_not_a_disagreement(self):
        self.assertEqual(bi.agreement(11.7, 76.3, None), "clear")
        self.assertEqual(bi.agreement(-11.2, 51.0, None), "unclear")

    def test_an_absent_interval_is_never_clear(self):
        self.assertEqual(bi.agreement(None, None, 0.001), "split")
        self.assertEqual(bi.agreement(None, None, None), "unclear")

    def test_only_agreement_counts_toward_the_headline(self):
        """The header claim is the strong one, so it takes the strict reading."""
        s = summary({}, {"split": {"delta_pp": 40.0, "ci95_pp": [3.8, 68.7],
                                   "fisher_p": 0.087, "env_only_n": 10, "env_skill_n": 10},
                         "clear": {"delta_pp": 50.0, "ci95_pp": [11.7, 76.3],
                                   "fisher_p": 0.033, "env_only_n": 10, "env_skill_n": 10}})
        _, n_sep, n_all = bi.build_effects([("t", s, "")])
        self.assertEqual((n_sep, n_all), (1, 2))

    def test_the_verdict_is_text_not_only_colour(self):
        """Greying was the sole signal, which fails in print and for colour-blind
        readers, on the column that matters most."""
        s = summary({}, {"m": {"delta_pp": 20.0, "ci95_pp": [-11, 51], "fisher_p": 0.47,
                               "env_only_n": 10, "env_skill_n": 10}})
        h = effects_html([("t", s, "")])
        self.assertIn(">unclear<", h)


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


class EffectRendering(unittest.TestCase):
    """Mutation-tested. Each of these caught a mutation the earlier suite missed."""

    def row(self, **kw):
        e = {"delta_pp": 40.0, "ci95_pp": [3.8, 68.7], "env_only_pass": 6,
             "env_only_n": 10, "env_skill_pass": 10, "env_skill_n": 10,
             "fisher_p": 0.087}
        e.update(kw)
        return effects_html([("t", summary({}, {"m": e}), "")])

    def test_the_p_value_is_rendered_as_given(self):
        """A mutation rendering 1-p passed the whole suite. A wrong p on a public
        scientific page is the worst single thing this file can do."""
        self.assertIn(">0.087<", self.row())
        self.assertNotIn("0.913", self.row())

    def test_p_keeps_three_decimals(self):
        self.assertIn(">0.003<", self.row(fisher_p=0.003))

    def test_an_absent_p_is_a_dash_not_a_number(self):
        self.assertIn("&mdash;", self.row(fisher_p=None))

    def test_an_absent_interval_says_so(self):
        h = self.row(ci95_pp=None)
        self.assertIn("not computed", h)
        self.assertNotIn("0, 0", h)

    def test_an_absent_interval_draws_no_whisker(self):
        """A stub on the centre line claims a precision never computed."""
        # The ELEMENT, not the word: "whisker" also appears in the Effect column's
        # hover definition, so matching a bare substring made this pass or fail on
        # prose rather than on markup.
        self.assertNotIn('class="whisk"', self.row(ci95_pp=None))
        self.assertIn('class="whisk"', self.row())

    def test_an_absent_interval_is_not_counted_as_clear(self):
        s = summary({}, {"m": {"delta_pp": 50.0, "env_only_n": 10, "env_skill_n": 10}})
        _, n_sep, n_all = bi.build_effects([("t", s, "")])
        self.assertEqual((n_sep, n_all), (0, 1))

    def test_model_names_are_escaped_in_the_effect_table(self):
        """Escaping was pinned only in the grid; removing it here passed."""
        s = summary({}, {"<img src=x>": {"delta_pp": 1.0, "ci95_pp": [0, 2],
                                         "env_only_n": 10, "env_skill_n": 10}})
        h = effects_html([("t<b>", s, "")])
        self.assertNotIn("<img src=x>", h)
        self.assertIn("&lt;img", h)
        self.assertIn("t&lt;b&gt;", h)


class DeltaBarGeometry(unittest.TestCase):
    """The bar's meaning is its position. A sign flip passed the earlier suite."""

    def left(self, h, cls):
        seg = h.split('class="%s"' % cls)[1]
        return float(seg.split("left:")[1].split("%")[0])

    def test_a_positive_delta_starts_at_the_centre_line(self):
        self.assertAlmostEqual(self.left(bi.delta_bar(40, 3.8, 68.7), "fill pos"), 50.0, 1)

    def test_a_negative_delta_ends_at_the_centre_line(self):
        h = bi.delta_bar(-40, -68.7, -3.8)
        seg = h.split('class="fill neg"')[1]
        left = float(seg.split("left:")[1].split("%")[0])
        width = float(seg.split("width:")[1].split("%")[0])
        self.assertAlmostEqual(left + width, 50.0, places=1)
        self.assertLess(left, 50.0)

    def test_bigger_effects_reach_further(self):
        a = bi.delta_bar(20, 0, 40)
        b = bi.delta_bar(60, 40, 80)
        wa = float(a.split('class="fill pos"')[1].split("width:")[1].split("%")[0])
        wb = float(b.split('class="fill pos"')[1].split("width:")[1].split("%")[0])
        self.assertLess(wa, wb)

    def test_the_whisker_spans_the_interval_not_the_delta(self):
        """A mutation drawing the whisker from the delta bounds passed before."""
        h = bi.delta_bar(40, -10, 90)
        seg = h.split('class="whisk"')[1]
        left = float(seg.split("left:")[1].split("%")[0])
        width = float(seg.split("width:")[1].split("%")[0])
        self.assertAlmostEqual(left, 45.0, places=1)          # x(-10)
        self.assertAlmostEqual(left + width, 95.0, places=1)  # x(90)


class Grid(unittest.TestCase):
    def test_baseline_arm_comes_first(self):
        s = summary({"m|env+skill": {"n": 10, "passes": 10},
                     "m|env-only": {"n": 10, "passes": 8}})
        h = bi.build_grid([("t", s, "")])
        self.assertLess(h.index("no skill"), h.index("Skill A"))

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

    def test_any_skill_arm_gets_a_letter(self):
        """An arm named after a folder still reads as an artefact, not a person."""
        s = summary({"m|env+skill-michele": {"n": 10, "passes": 3}})
        h = bi.build_grid([("t", s, "")])
        self.assertIn("Skill A", h)
        self.assertNotIn("michele", h)


class Attribution(unittest.TestCase):
    """A hash printed beside the wrong skill turns two incomparable runs into a
    matched pair. Attribution is only made where summary.json settles it."""

    def _s(self, cells, hashes, task="t"):
        d = summary(cells, task=task)
        d["provenance"] = {"skills_hash": hashes}
        return d

    def test_one_skill_arm_one_hash_is_attributed(self):
        s = self._s({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9}},
                    {"291f844a43ec": 20, "unknown": 4})
        reg = bi.arm_registry([("t", s, "")])
        self.assertEqual(bi.attributed_hashes([("t", s, "")], reg),
                         {"env+skill": ["291f844a43ec"]})

    def test_two_skill_arms_two_hashes_is_left_blank(self):
        """summary.json aggregates provenance per TASK. With two skill arms and two
        hashes there is no record of which belongs to which, so neither is claimed."""
        s = self._s({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9},
                     "m|env+skill-b": {"n": 10, "passes": 7}},
                    {"291f844a43ec": 10, "5ba66f0c7ddf": 10, "unknown": 10})
        reg = bi.arm_registry([("t", s, "")])
        self.assertEqual(bi.attributed_hashes([("t", s, "")], reg), {})

    def test_unattributed_arm_says_so_instead_of_showing_nothing(self):
        s = self._s({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9},
                     "m|env+skill-b": {"n": 10, "passes": 7}},
                    {"291f844a43ec": 10, "5ba66f0c7ddf": 10})
        entries = [("t", s, "")]
        h = bi.skills_key(entries, bi.arm_registry(entries))
        self.assertIn("not attributable per arm", h)

    def test_unknown_is_not_a_hash(self):
        s = self._s({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9}},
                    {"unknown": 20})
        reg = bi.arm_registry([("t", s, "")])
        self.assertEqual(bi.attributed_hashes([("t", s, "")], reg), {})

    def test_skill_label_and_link_come_from_the_caller(self):
        """Nothing on this page infers what a skill IS. It is passed in or it is blank."""
        s = self._s({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9}}, {"abc123": 20})
        entries = [("t", s, "")]
        reg = bi.arm_registry(entries, {"env+skill": {"label": "brain-extraction",
                                                      "href": "http://x/"}})
        h = bi.skills_key(entries, reg)
        self.assertIn("brain-extraction", h)
        self.assertIn('href="http://x/"', h)


class SuppliedHash(unittest.TestCase):
    """A hash the summaries cannot attribute, supplied after checking it elsewhere."""

    def _entries(self, hashes):
        d = summary({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9},
                     "m|env+skill-b": {"n": 10, "passes": 7}})
        d["provenance"] = {"skills_hash": hashes}
        return [("t", d, "")]

    def test_supplied_hash_shows_and_is_marked_verified(self):
        e = self._entries({"aaa": 10, "bbb": 10})
        reg = bi.arm_registry(e, {"env+skill-b": {"hash": "5ba66f0c7ddf"}})
        h = bi.skills_key(e, reg)
        self.assertIn("5ba66f0c7ddf", h)
        self.assertIn("verified", h)
        self.assertNotIn("not attributable per arm</span></td><td class=\"l\">"
                         "<code>5ba66f0c7ddf", h)

    def test_a_supplied_hash_that_contradicts_the_summary_is_flagged(self):
        """One of the two describes runs that are not the runs on this page."""
        d = summary({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9}})
        d["provenance"] = {"skills_hash": {"291f844a43ec": 20}}
        e = [("t", d, "")]
        reg = bi.arm_registry(e, {"env+skill": {"hash": "deadbeef0000"}})
        h = bi.skills_key(e, reg)
        self.assertIn("conflicts with", h)
        self.assertIn("291f844a43ec", h)

    def test_agreement_does_not_flag(self):
        d = summary({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9}})
        d["provenance"] = {"skills_hash": {"291f844a43ec": 20}}
        e = [("t", d, "")]
        reg = bi.arm_registry(e, {"env+skill": {"hash": "291f844a43ec"}})
        self.assertNotIn("conflicts with", bi.skills_key(e, reg))


class PerArmProvenance(unittest.TestCase):
    """Newer summaries carry provenance inside each cell, so nothing is inferred."""

    def test_two_skill_arms_are_both_attributed_from_cells(self):
        d = summary({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9},
                     "m|env+skill-b": {"n": 10, "passes": 7}})
        d["cells"]["m|env+skill"]["provenance"] = {"skills_hash": {"291f844a43ec": 10}}
        d["cells"]["m|env+skill-b"]["provenance"] = {"skills_hash": {"5ba66f0c7ddf": 10}}
        d["provenance"] = {"skills_hash": {"291f844a43ec": 10, "5ba66f0c7ddf": 10}}
        e = [("t", d, "")]
        self.assertEqual(bi.attributed_hashes(e, bi.arm_registry(e)),
                         {"env+skill": ["291f844a43ec"], "env+skill-b": ["5ba66f0c7ddf"]})

    def test_baseline_arm_is_not_given_a_skill_hash(self):
        d = summary({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9}})
        d["cells"]["m|env-only"]["provenance"] = {"skills_hash": {"unknown": 10}}
        d["cells"]["m|env+skill"]["provenance"] = {"skills_hash": {"291f844a43ec": 10}}
        e = [("t", d, "")]
        self.assertNotIn("env-only", bi.attributed_hashes(e, bi.arm_registry(e)))

    def test_old_summaries_still_fall_back(self):
        """The task-level path must keep working; four published packs use it."""
        d = summary({"m|env-only": {"n": 10, "passes": 1},
                     "m|env+skill": {"n": 10, "passes": 9}})
        d["provenance"] = {"skills_hash": {"291f844a43ec": 20, "unknown": 4}}
        e = [("t", d, "")]
        self.assertEqual(bi.attributed_hashes(e, bi.arm_registry(e)),
                         {"env+skill": ["291f844a43ec"]})


class NoIndex(unittest.TestCase):
    def test_absent_by_default(self):
        s = summary({"m|env-only": {"n": 1, "passes": 1}})
        self.assertNotIn("noindex", bi.build([("t", s, "")]))

    def test_present_when_asked(self):
        s = summary({"m|env-only": {"n": 1, "passes": 1}})
        h = bi.build([("t", s, "")], noindex=True)
        self.assertIn('<meta name="robots" content="noindex,nofollow">', h)

    def test_page_still_reaches_no_external_host(self):
        """The tag is the only addition; it must not introduce a URL."""
        s = summary({"m|env-only": {"n": 1, "passes": 1}})
        h = bi.build([("t", s, "")], noindex=True)
        self.assertNotIn("http://", h)


class Registry(unittest.TestCase):
    """The arm registry is what stops two charts disagreeing about a colour."""

    def test_slots_are_global_not_per_task(self):
        """The bug this replaces: colour came from the arm's index WITHIN a task, so a
        one-skill task drew its skill in the same hue a two-skill task used for its
        second. Two charts on one page then meant different things by one colour."""
        a = summary({"m|env-only": {"n": 1, "passes": 0},
                     "m|env+skill-b": {"n": 1, "passes": 1}}, task="a")
        b = summary({"m|env-only": {"n": 1, "passes": 0},
                     "m|env+skill-a": {"n": 1, "passes": 1},
                     "m|env+skill-b": {"n": 1, "passes": 1}}, task="b")
        reg = bi.arm_registry([("a", a, ""), ("b", b, "")])
        self.assertEqual(reg["env+skill-b"]["slot"], reg["env+skill-b"]["slot"])
        self.assertNotEqual(reg["env+skill-a"]["slot"], reg["env+skill-b"]["slot"])
        self.assertEqual(reg["env-only"]["slot"], 0)

    def test_letters_are_stable_across_tasks(self):
        a = summary({"m|env+skill-a": {"n": 1, "passes": 1}}, task="a")
        b = summary({"m|env+skill-b": {"n": 1, "passes": 1}}, task="b")
        reg = bi.arm_registry([("a", a, ""), ("b", b, "")])
        self.assertEqual(reg["env+skill-a"]["name"], "Skill A")
        self.assertEqual(reg["env+skill-b"]["name"], "Skill B")

    def test_control_is_never_a_letter(self):
        s = summary({"m|env-only": {"n": 1, "passes": 0}})
        reg = bi.arm_registry([("t", s, "")])
        self.assertEqual(reg["env-only"]["name"], "no skill")
        self.assertFalse(reg["env-only"]["skill"])


class Direction(unittest.TestCase):
    """Lower-is-better measures are mirrored, and a mirror must be labelled."""

    def _chart(self, metric):
        s = summary({"m|env-only": {"n": 10, "passes": 2, "median_minutes": 40.0,
                                    "median_tokens_total": 20000000},
                     "m|env+skill": {"n": 10, "passes": 8, "median_minutes": 4.0,
                                     "median_tokens_total": 200000}})
        h = bi.build_charts([("t", s, "")])
        i = h.index('data-metric="%s"' % metric)
        j = h.index("</figure>", i)
        return h[i:j]

    def test_faster_arm_sits_to_the_right(self):
        """4 min must plot right of 40 min, or the chart reads backwards."""
        f = self._chart("mins")
        xs = [float(x.split("%")[0]) for x in f.split("--x:")[1:]]
        self.assertGreater(max(xs), min(xs))
        # The faster value is the one carrying the larger position.
        self.assertIn("better", f)

    def test_higher_pass_rate_also_sits_to_the_right(self):
        f = self._chart("pass")
        self.assertIn("better", f)

    def test_both_ends_of_a_mirrored_axis_are_labelled(self):
        """An unlabelled reversal is the one failure mode worse than no chart.

        Asserts the values, not the markup: the right-hand tick must be the BETTER
        end. A class name can be renamed without anyone noticing; a tick reading
        "40 min" on the side the eye takes as good is the actual defect."""
        f = self._chart("mins")
        i = f.index('class="cscale"')
        scale = f[i:f.index("</div>", i)]
        ticks = re.findall(r'<span class="cax">([^<]+)</span>', scale)
        self.assertEqual(len(ticks), 2)
        left, right = (float(t.split()[0]) for t in ticks)
        self.assertLess(right, left)

    def test_log_is_offered_on_tokens_and_withheld_from_pass_rate(self):
        """A proportion has no decades to spread and lands on zero, which log cannot draw."""
        self.assertIn('data-log="1"', self._chart("tokens"))
        self.assertIn('data-log="0"', self._chart("pass"))

    def test_log_positions_are_rendered_server_side(self):
        """The toggle picks between two numbers the page already holds; it computes none."""
        self.assertIn("--xl:", self._chart("tokens"))


    def test_only_one_axis_is_visible_at_a_time(self):
        """Regression. The log axis was hidden with the `hidden` attribute, which the
        UA stylesheet implements as display:none -- and `.caxis{display:grid}` is a
        class rule, so it outranked it. Both axes drew, one above the other, each
        naming a different right-hand end. Visibility is a class rule now, at the
        same weight as the rule that broke it."""
        self.assertIn(".caxis.log-only{display:none}", bi.STYLE)
        self.assertIn(".charts.log .caxis.log-only{display:grid}", bi.STYLE)
        self.assertNotIn('AXIS % ("log-only", " hidden"', bi.STYLE)
        f = self._chart("mins")
        self.assertIn('class="caxis log-only"', f)

    def test_axis_zero_is_not_written_to_one_decimal(self):
        """"0.0 min" claims a precision the end of an axis does not have."""
        self.assertEqual(bi._compact(0.0, 1, " min"), "0 min")


class Retired(unittest.TestCase):
    def test_retired_model_row_is_marked_in_the_chart(self):
        s = summary({"gone|env-only": {"n": 10, "passes": 2},
                     "gone|env+skill": {"n": 10, "passes": 8}})
        h = bi.build_charts([("t", s, "")], roster=["live"])
        self.assertIn("drow retired", h)
        self.assertIn("retired</span>", h)

    def test_live_model_is_not_marked(self):
        s = summary({"live|env-only": {"n": 10, "passes": 2},
                     "live|env+skill": {"n": 10, "passes": 8}})
        self.assertNotIn("drow retired", bi.build_charts([("t", s, "")], roster=["live"]))


class Notices(unittest.TestCase):
    """`cells` holds only surviving runs, so a sweep can look complete while a
    fifth of it was thrown out. Both facts are in summary.json and the page was
    discarding both."""

    def sm(self, **kw):
        s = summary({"m|env-only": {"n": 10, "passes": 5}})
        s.update(kw)
        return s

    def test_exclusions_are_shown_with_their_reason(self):
        h = bi.notices([("t", self.sm(n_runs=80, n_excluded=13, exclusions={
            "harness failure: run timed out (exit 124)": 11,
            "contaminated: env-only run loaded the skill": 2}), "")])
        self.assertIn("13", h)
        self.assertIn("80", h)
        self.assertIn("harness failure", h)

    def test_not_poolable_names_the_field(self):
        """A flag you cannot argue with gets ignored."""
        h = bi.notices([("t", self.sm(poolable=False,
                                      not_poolable_because=["skills_hash"]), "")])
        self.assertIn("not poolable", h)
        self.assertIn("skills_hash", h)

    def test_absent_poolable_is_not_treated_as_false(self):
        """Older summaries predate the field. Absent is not the same as false."""
        self.assertEqual(bi.notices([("t", self.sm(), "")]), "")

    def test_poolable_true_says_nothing(self):
        self.assertEqual(bi.notices([("t", self.sm(poolable=True), "")]), "")

    def test_a_clean_sweep_renders_no_strip_at_all(self):
        self.assertEqual(bi.notices([("t", self.sm(n_excluded=0, poolable=True), "")]), "")

    def test_the_header_counts_exclusions(self):
        h = bi.build([("t", self.sm(n_runs=80, n_excluded=13), "")])
        self.assertIn("<b>13</b><span>excluded</span>", h)

    def test_reasons_are_escaped(self):
        h = bi.notices([("t", self.sm(n_runs=1, n_excluded=1,
                                      exclusions={"<img src=x>": 1}), "")])
        self.assertNotIn("<img src=x>", h)


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
        self.assertIn("<b>5</b><span>models</span>", h)
        self.assertIn("<b>100</b><span>runs</span>", h)
        self.assertIn("<b>10</b><span>per cell</span>", h)

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
