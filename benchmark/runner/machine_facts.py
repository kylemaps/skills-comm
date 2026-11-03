#!/usr/bin/env python3
"""Render the machine facts the agent is told, from what the pod has.

    machine_facts.py TEMPLATE [--timeout S] [--data-limit-kb KB] [--memwatch-pct N]
                     [--layer-bytes N] [--cgroup DIR] [--work DIR] [--shm DIR]

The agent is told the machine it runs on, never how to do the task, and the same text
in every arm. `free`, `top` and `nproc` in a pod show the node, not the pod's limits, so
an agent that checks its resources plans for a machine it does not have. The values
come from the pod and from the run's own settings, which a wave fixes for every arm:

  cpus         the cgroup's CPU limit (cpu.max), else every CPU (no limit)
  memory_gib   the cgroup's memory limit (memory.max), to the nearest GiB
  command_gib  the per-process data limit (--data-limit-kb), when one is set
  watch        set when memwatch stops the largest command (--memwatch-pct above 0)
  disk_gib     the size of the filesystem holding --work, in whole GiB
  shm_gib      the size of --shm (/dev/shm), when it is a filesystem of its own of at
               least 1 GiB (the runtime's 64 MiB default is not worth a sentence)
  layer_gib    the pod's budget for writes outside the work volume (--layer-bytes: the
               container's ephemeral-storage limit, which the pod cannot read itself)
  minutes      the run's time limit (--timeout seconds)

TEMPLATE holds one sentence per line with {name} placeholders. A line starting with
"?name " is kept only when name has a value. The kept lines print as one paragraph.
Exit 1, printing nothing, when a kept line needs a value that cannot be read: a pod
with no memory limit would otherwise be described by the node's memory.
"""
import argparse
import os
import re
import shutil
import sys


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _gib(n):
    g = n / 2**30
    return ("%d" % g) if g == int(g) else ("%.1f" % g)


def values(cgroup, work, timeout, data_limit_kb, memwatch_pct, layer_bytes=None, shm=None):
    v = {}
    cm = (_read(os.path.join(cgroup, "cpu.max")) or "").split()
    if len(cm) == 2 and cm[0].isdigit() and cm[1].isdigit():
        v["cpus"] = -(-int(cm[0]) // int(cm[1]))
    elif cm[:1] == ["max"]:
        v["cpus"] = os.cpu_count()
    mm = _read(os.path.join(cgroup, "memory.max")) or ""
    if mm.isdigit():
        v["memory_gib"] = (int(mm) + 2**29) // 2**30
    if data_limit_kb:
        v["command_gib"] = _gib(data_limit_kb * 1024)
    if memwatch_pct and memwatch_pct > 0:
        v["watch"] = ""
    if layer_bytes:
        v["layer_gib"] = _gib(layer_bytes)
    try:
        v["disk_gib"] = shutil.disk_usage(work).total // 2**30
    except OSError:
        pass
    if shm:
        try:
            total = shutil.disk_usage(shm).total
            if total >= 2**30 and os.stat(shm).st_dev != os.stat(work).st_dev:
                v["shm_gib"] = _gib(total)
        except OSError:
            pass
    if timeout:
        v["minutes"] = timeout // 60
    return {k: x for k, x in v.items() if x is not None}


def render(template, v):
    """The paragraph, or None when a kept line needs a value v lacks."""
    out = []
    for line in template.splitlines():
        line = line.strip()
        if not line:
            continue
        cond = re.match(r"\?(\w+)\s+", line)
        if cond:
            if cond.group(1) not in v:
                continue
            line = line[cond.end():]
        need = re.findall(r"\{(\w+)\}", line)
        if any(n not in v for n in need):
            return None
        out.append(line.format(**v))
    return " ".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("template")
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--data-limit-kb", type=int)
    ap.add_argument("--memwatch-pct", type=float)
    ap.add_argument("--layer-bytes", type=int)
    ap.add_argument("--cgroup", default="/sys/fs/cgroup")
    ap.add_argument("--work", default=os.getcwd())
    ap.add_argument("--shm", default="/dev/shm")
    a = ap.parse_args()
    text = render(open(a.template, encoding="utf-8").read(),
                  values(a.cgroup, a.work, a.timeout, a.data_limit_kb, a.memwatch_pct,
                         a.layer_bytes, a.shm))
    if text is None:
        print("machine_facts: a value the template needs cannot be read", file=sys.stderr)
        return 1
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
