"""A run id: one task and, for a task graded per subject, one subject.

  <task>             a task with one reference: structural-mni-registration
  <task>@<subject>   a task graded per subject, run for one subject of its list:
                     structural-brain-extraction@sub-0004

The run id stands wherever a task id did: run.yml's task input, sweep.json's wave
tasks, run directories (<run id>__<model>__<arm>__r<n>), artifact names, the runs
CSV's task column, assemble and dispatch. Its two parts are taken only where they
are needed: mkprompt.py renders the task's prompt for the subject, the output path
goes one level deeper, and the grader is called with --subject (fetch_reference.py,
grade_wrapper.py). run_bench.sh and collect_results.sh split the same way in shell,
at the first '@'.

The output path follows the task pack's contract: submissions/<task>/output.nii.gz,
and submissions/<task>/<subject>/output.nii.gz for a subject run.

    python run_ids.py <run id>      prints task=, subject= and output=
"""
import sys

SEP = "@"
OUTPUT = "output.nii.gz"


def split(run_id):
    """(task, subject); the subject is None when the id names none. Half an id
    ('@sub-01', 'task@', 'a@b@c') names nothing and is refused, and so is an id
    with whitespace, '/' or '__': the first two would reach the agent and name a
    path nothing reads back, and '__' is the run directory's own separator."""
    rid = run_id or ""
    if any(c.isspace() for c in rid) or "/" in rid or "__" in rid:
        raise ValueError("run id %r holds whitespace, '/' or '__'" % rid)
    if SEP not in rid:
        return rid, None
    task, subject = rid.split(SEP, 1)
    if not task or not subject or SEP in subject:
        raise ValueError("run id %r is not <task> or <task>@<subject>" % rid)
    return task, subject


def join(task, subject=None):
    return task + SEP + subject if subject else task


def output_rel(run_id):
    """The run's contracted output, relative to its directory, with '/'."""
    task, subject = split(run_id)
    return "/".join(["submissions", task] + ([subject] if subject else []) + [OUTPUT])


def main(argv):
    if len(argv) != 1:
        print("usage: run_ids.py <run id>", file=sys.stderr)
        return 2
    try:
        task, subject = split(argv[0])
    except ValueError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 2
    print("task=%s\nsubject=%s\noutput=%s" % (task, subject or "", output_rel(argv[0])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
