#!/usr/bin/env python
"""Project what re-running a specific set of runs will cost.

    printf '%s\n' <run dirs...> | python price_runs.py <runs_dir> <task>

Prints one integer to stdout: projected in+out tokens. Nothing else, so a shell
script can compare it against a budget.

Why this exists
---------------
estimate_cost.py prices a sweep you are about to launch deliberately. This
prices spend that happens on its own -- specifically run_sweep.sh's automatic
retry of harness failures, which chooses its own workload at runtime.

That path spent nothing knowingly and could spend a great deal: the retry
filtered failures by arm but not by model, so a sweep of two cheap models was
about to re-run a third model's ten failures at ~1.5 M tokens each. Caught by
eye, an hour before it fired. A number printed before the spend, and a ceiling
it has to clear, is the difference between catching that and paying for it.

Unknown cells are priced at the model's median in any arm, then at the global
median. Guessing high is the safe direction here: the number gates a spend.
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from estimate_cost import load  # noqa: E402


def main():
    if len(sys.argv) < 3:
        sys.exit("usage: price_runs.py <runs_dir> <task> <run dir> [...]")
    runs_dir, task = sys.argv[1], sys.argv[2]
    dirs = [d for d in sys.argv[3:] if d.strip()]
    if not dirs:
        print(0)
        return

    hist = load(runs_dir, task)
    if not hist:
        # No history means no basis to project. Print a sentinel the caller reads
        # as "unknown" rather than 0, which would read as "free".
        print(-1)
        return
    by_model = {}
    for (m, _a), v in hist.items():
        by_model.setdefault(m, []).append(v)
    overall = statistics.median(list(hist.values()))

    total = 0
    for d in dirs:
        parts = os.path.basename(d.rstrip("/")).split("__")
        if len(parts) < 3:
            total += overall
            continue
        model, arm = parts[1].replace("neurodesk-", ""), parts[2]
        med = hist.get((model, arm))
        if med is None:
            med = (statistics.median(by_model[model])
                   if model in by_model else overall)
        total += med
    print(int(total))


if __name__ == "__main__":
    main()
