# Claims ledger

Every number we are willing to publish, the report file that produces it, and
the caveat that has to travel with it.

**The rule this exists to enforce:** no figure goes into a message, a poster or
a paper unless `make_report.sh` produced it. An audit of the first results pack
found nine wrong numbers, two of them headline figures, and every single one
came from an ad-hoc one-liner rather than a committed script.

## Regenerating everything

```bash
bash benchmark/runner/make_report.sh ~/bench/runs
```

Writes `~/bench/report/<date>/`. Output is deterministic, so auditing a later
report is `diff -r` against an earlier one: anything that moved while the inputs
did not is a bug. That check has already earned itself once, catching an
unstable sort that reordered a table between identical runs.

File prefixes: `00` manifest, `10` summaries, `20` gates, `30` mechanism,
`40/41` matrix and tokens, `50/51` agent self-QC.

---

## 1. Headline pass rates (intent-to-treat)

Source: `10_summary_<task>.txt`, `40_matrix.txt`

| task | models | no skill | skill A | skill B | A vs baseline |
|---|---|---|---|---|---|
| 7T MP2RAGE | 5 | 21/50 = 42% | 35/50 = 70% | 25/50 = 50% | +28 pp, p=0.0085 |
| motion (3T) | 5 | 30/50 = 60% | 43/50 = 86% | not run | +26 pp, p=0.0063 |
| stroke | 3 | 15/30 = 50% | 22/30 = 73% | 20/30 = 67% | +23 pp, p=0.11 |
| 7t-nodura | 5 | 20/50 = 40% | 23/50 = 46% | not run | +6 pp, p=0.69 |

**Arms are intent-to-treat**, defined by skill *availability*, not uptake. Runs
that never opened the skill still count in the skill arm.

**Quote the pass rate, never the mean.** Cells are bimodal: runs score near 0 or
near 100 and a mean of 70 describes no run that happened.

**The two skills are not distinguishable.** On the three models present in both
head-to-head tasks: 7T -17 pp p=0.28, stroke -7 pp p=0.78. A -20 pp gap appears
only on the full five-model 7T panel (p=0.066) and comes from qwen3 and qwen3.5.
**Never report skill A as outperforming skill B.**

**Every skill B number needs this caveat:** skill B is built around a human
choosing the tool when QC fails. `patch_unattended.py` removed that gate so it
could run unattended, so the numbers measure it without its central interaction.

---

## 2. Mechanism: selection versus execution

Source: `30_mechanism_<task>.txt`

| task | reached a panel tool | passed given it reached one |
|---|---|---|
| 7T | 62% -> 94%, p=0.0002 | 68% -> 74%, p=0.61 |
| motion | 32% -> 74%, p=0.0000 | 56% -> 86%, p=0.029 |
| stroke | 57% -> 73%, p=0.28 | 88% -> 100%, p=0.18 |
| nodura | no tool separates | n/a |

Skill B on 7T: selection 88% (p=0.0050), execution 57% (p=0.47). Skill A vs
skill B on selection: 94% vs 88%, **p=0.49** -- both route, equally well.

**BET is not a bad tool.** BET-only runs pass **0/25** on 7T, **0/25** on
stroke, **29/44** on motion. It is fine on ordinary 3T and collapses on 7T
MP2RAGE and on stroke. The skill supplies knowing when the default will not
work.

**The mechanism is not uniform.** Selection is the consistent effect. Motion
also moves execution, but p=0.029 after many tests is soft, and `methods`
records an *invocation* -- an agent that called SynthStrip, hit an error and
fell back to BET still counts as having reached it. Stroke is underpowered to
decompose; neither mediator separates and neither did its ITT effect.

**nodura is the control, not a disappointment.** No tool separates (bet 47%,
hd-bet 55%, synthstrip 44%) because nothing removes dura by default, so there is
nothing to route to. That is why the skill gave +6 pp, and it is what makes the
mechanism credible rather than fitted.

**`--robust` is never inferred from results.** It is each grader pack's kept
reference panel from its `PROVENANCE.md` (HD-BET, AFNI, SynthStrip). The split
is insensitive to that boundary anyway: adding or removing AFNI changes 7T and
stroke not at all, motion by one run, because every run outside the panel used
BET alone.

**This section is per-protocol, not ITT.** Only runs that produced a mask have a
tool to classify, and skill arms produce masks more often. It explains section 1
rather than replacing it.

---

## 3. Agent self-QC versus the grader

Source: `50_qcval_<set>.txt`, `51_qcsim_<set>.txt`

210 masks scored with Michele's `qc_metrics.py` (128 7T + 82 nodura), same
subject, against the OpenNeuro anat.

| claim | number |
|---|---|
| battery verdict | **FAIL on 210 of 210** |
| hard blockers (reject every accepted mask) | `bright_rim_frac_pct`, `brain_to_head_ratio_low`, `rim_brightness_high` |
| 7T with those three relaxed | 79/81 accepted, **0/47 false accepts** |
| ...over distinct masks | 12/13 and **0/19** |
| nodura with those three relaxed | 40/43 accepted, **23 false accepts** |
| what those 23 tripped | `no_dura_inclusion`, 23 of 23, nothing else |
| ...distinct masks | 7 |
| suggested ratio bound | 0.224 (window 0.197 to 0.251) |

**The battery is correct everywhere it claims to measure something.** Its only
false accepts are dura inclusion, which the script itself reports as
`UNVERIFIED` because it cannot measure it.

**Her scale is three-valued** (PASS < BORDERLINE < FAIL, worst wins), verified
against **1260 observed ratings, zero disagreements** by `qcval_simulate.py`. A
boolean model was rejected on 84 of 640 before it could print anything.

**The 7T AUCs are confounded and must not be quoted.** All 47 7T failures
tripped volume gates, and every mask is one subject, so brain/head ratio is
brain volume predicting a volume threshold. nodura broke the confound (23 of 39
failures tripped no volume gate) and there the intensity and shape metrics do
separate -- but on **7 distinct** failure masks, so quote the direction and not
the AUC.

**Threshold recommendations are a starting point, not a calibration.** See
section 5.

---

## 3b. Cost per usable result

Source: `31_economics_<task>.txt`

Only two comparisons survive both token accountings, and only those two are
quotable.

| claim | billed | processed |
|---|---|---|
| 7T skill A: a usable mask costs **half** as much | 0.50x | 0.63x |
| motion skill A | 0.68x | 0.79x |

An attempt costs 0.84x on 7T and 0.98x on motion, so on these two tasks the
skill is cheaper per attempt *and* markedly cheaper per result.

**Four of six comparisons flip sign between the two accountings and must not be
given a direction at all:** 7T skill B (0.92x / 1.08x), stroke skill A
(1.24x / 0.76x), stroke skill B (0.88x / 1.09x), nodura skill A (1.30x / 0.89x).
Cache reads are usually discounted but not free, and the gateway publishes no
prices, so both readings are defensible from the same runs. `economics.py`
refuses to state a direction for these rather than printing whichever was
computed first.

**This is the strongest practical argument for asking Steffen to configure
per-model pricing in LiteLLM.** The gateway currently returns `cost: 0.0`. With
prices, four ambiguous comparisons resolve and the cost story covers all four
tasks instead of two.

`min/PASS` is in the same files but is inflated wherever runs hit the 45-minute
timeout, worst in `nodura / kimi / skill A`. Tokens are the reliable half.

---

## 3c. Do skills help weak models most? No.

Source: `capability.py runs_<task>.csv --outcome pass|tool`

The claim is attractive and it is **not supported**. It does not fail by being
noisy, it fails by reversing sign between tasks.

| task, outcome | absolute favours | RFR favours | verdict |
|---|---|---|---|
| 7T, pass | stronger (+37 vs +15 pp) | stronger (100% vs 17%) | **stronger models gain more** |
| motion, pass | weaker (+55 vs +7 pp) | weaker (85% vs 29%) | **weaker models gain more** |
| 7T, tool | weaker | stronger | ceiling, undecidable |
| motion, tool | weaker | stronger | ceiling, undecidable |

On 7T the two weakest models are the two that barely improve: minimax 0/10 to
2/10 and qwen3.5 2/10 to 3/10, against RFR 100% for all three stronger models.

**Report both metrics or neither.** Absolute improvement is bounded by headroom,
so ranking models by it ranks them by how much room they had. Relative failure
reduction is scale-free. Where they disagree, the ceiling is doing the talking.

**Structural limit:** only one model has a low baseline on any given task, so
"weak models" is close to n=1 wearing a group label. More weak models in the
panel is a run-budget question, not an analysis one.

## 3d. The skill can make things worse, and did

Source: `31_economics_...`, `capability.py runs_...-motion.csv`

```
minimax-m2 on motion   pass rate   10/10 -> 7/10   (-30 pp, p=0.21)
minimax-m2 on motion   tool choice  1/10 -> 9/10   (+80 pp, p=0.001)
```

The skill routed minimax off BET onto panel tools almost perfectly, and its pass
rate fell. BET works on motion (BET-only passes 29/44 there, against 0/25 on 7T
and 0/25 on stroke), so on this task the routing moved a model that was already
at 10/10 to something that served it worse.

This is the falsification the mechanism claim needed and we did not have to
build it -- it was already in the runs. It says the mechanism is real *and* has
a cost: a skill that routes on "which tool is generally robust" will sometimes
route away from a tool that was working.

Not significant on its own (p=0.21, one model, one task). Quote it as the
direction to check next, not as an established harm.

---

## 4. Integrity checks

Source: recorded in `_local/SUCCESS.md`; re-runnable from the runs directory.

| check | result |
|---|---|
| prompts byte-identical across every arm | md5 of `prompt.txt`, all match |
| no copied masks | max Dice 0.964 / 0.973 / 0.975 / 0.986, none at or above 0.995 |
| ASTRA decisions name a tool actually run | 141 of 141 |
| cited DOIs resolve | 17 of 17 (5 return 403 only because Wiley blocks bots) |
| Fisher implementation | matches scipy to 1e-9 on all 10 reported tables, and 3 textbook cases with published values |
| Wilson implementation | matches 3 hand-computed references |

---

## 5. Standing caveats that must travel with any result

1. **Every number is one subject** -- `ds003642 sub-025 ses-003` for both 7T
   tasks. Nothing here speaks to generalisation across subjects. This is the
   benchmark's biggest weakness.
2. **Runs replicate outputs.** The 81 accepted 7T runs are **13 distinct masks**,
   one accounting for 51 runs. Deterministic tools converge. Run-level counts
   are not sample sizes for anything mask-level. Count distinct masks by
   **hashing the files** -- printed metric values round and gave 6.
3. **51 of 440 runs hit the 45-minute `RUN_TIMEOUT`** and were scored as agent
   failures. Excluding them flips nothing, but Methods must state the budget,
   and **`nodura / kimi / skill A` is 0/10 where all ten are timeouts** -- never
   quote that cell alone.
4. **Token counts compare within a model only.** Cross-model rankings invert
   once prompt-cache reads are counted; use `41_tokens.txt`, which reports both.
5. **No statistics below n=5.**

---

## 6. Checked and rejected

Kept so they are not re-derived, and because a discarded hypothesis is cheaper
to read than to re-test.

| hypothesis | verdict | evidence |
|---|---|---|
| The SLURM mandate is arm-specific and taxes one arm | **rejected**, twice | transcripts mentioning SLURM: 48/50, 37/50, 43/50 across arms |
| Her always-FAIL QC drives agents into the 45-min wall | **rejected** | 7 of the 9 non-delivering runs finish under 12 minutes (3,3,3,3,7,9,11,43,45) |
| `brain_to_head_ratio` predicts failure (AUC 1.00) | **confounded** | all 47 failures tripped volume gates; nothing left to test on |
| Skill A outperforms skill B | **rejected** | not distinguishable on any shared task |
| Skill B causes non-delivery | **not established** | 22% vs 6% p=0.041 against skill A, but neither differs from baseline |
