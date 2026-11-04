#!/usr/bin/env python3
"""Watch the pod's memory while the agent runs; stop the agent's largest command
before the container reaches its limit.

    memwatch.py --root PID --out FILE [--work DIR ...] [--pct N] [--interval S]
                [--min-rss-mib M] [--cgroup DIR] [--proc DIR] [--once] [--dry-run]

At the memory limit the kernel kills the whole container (cgroup v2, one OOM group
per container): the agent, the harness and the run record go with it, and the run
leaves nothing to grade. This keeps the container under the limit instead. Every
--interval seconds it reads the pod's memory cgroup; when the memory the kernel
cannot reclaim passes --pct of memory.max, it SIGKILLs the agent command with the
largest resident set, waits for it to go, and looks again. The agent sees the command
die ("Killed") and carries on.

Unreclaimable memory is memory.current less the page cache and the reclaimable slab
(memory.stat file, slab_reclaimable), plus shmem, which the page cache count includes
and which tmpfs and /dev/shm hold until deleted.

An agent command is a descendant of --root (the process the harness started, e.g.
`timeout`), or any process whose working directory is under a --work directory: a
command started with nohup or setsid is re-parented away from the agent when its shell
exits, and keeps working in the run's directories. Never chosen: --root, its direct
children (the agent itself), this process and its ancestors (the harness), zombies,
and processes below --min-rss-mib, whose death would not bring the memory back. Above
the threshold with nothing to choose, it records the pass and kills nothing.

--pct 0 kills nothing and only records. It writes --out as JSON when it starts, after
each kill, when the highest pressure has risen 256 MiB since the last write, and on
SIGTERM or after one pass with --once: the limit and threshold, the highest pressure
seen, each kill (time, process name, resident MiB, pressure MiB), passes above the
threshold with nothing to stop, kills whose process was not gone within 10 s
(slow_exits), errors, and the cgroup's memory.peak and memory.events
counters (oom, oom_kill, max), which the kernel keeps for the container's whole life.
No command lines: they can carry anything the agent typed. An error in one pass is
counted and the watch goes on.

A per-command limit (ulimit -d in the agent's BASH_ENV) stops one runaway process;
this covers several processes that together reach the limit, and processes the
agent starts outside bash. Exit 0; exit 2 when there is no memory limit to watch.
"""
import argparse
import json
import os
import signal
import sys
import time

MIB = 1024 * 1024


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _stat_kv(text):
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def pressure(cg):
    """(bytes the kernel cannot reclaim, memory.current) of the cgroup, or None."""
    cur = _read(os.path.join(cg, "memory.current"))
    if cur is None or not cur.isdigit():
        return None
    st = _stat_kv(_read(os.path.join(cg, "memory.stat")))
    cur = int(cur)
    free = st.get("file", 0) - st.get("shmem", 0) + st.get("slab_reclaimable", 0)
    return max(0, cur - free), cur


def _cwd(proc, pid):
    """A process's working directory: the /proc link, or for a test fixture without
    links, a regular file holding the path. None when unreadable."""
    p = os.path.join(proc, str(pid), "cwd")
    try:
        return os.readlink(p)
    except OSError:
        return _read(p) if os.path.isfile(p) else None


def processes(proc):
    """{pid: (ppid, name, state, rss bytes)} from proc's stat and statm files."""
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    out = {}
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        stat = _read(os.path.join(proc, d, "stat"))
        statm = _read(os.path.join(proc, d, "statm"))
        if not stat or not statm:
            continue
        # The name is in parentheses and can hold spaces and ')'.
        name = stat[stat.find("(") + 1:stat.rfind(")")]
        rest = stat[stat.rfind(")") + 2:].split()
        try:
            out[int(d)] = (int(rest[1]), name, rest[0], int(statm.split()[1]) * page)
        except (IndexError, ValueError):
            continue
    return out


def victim(procs, root, work=(), proc="/proc", me=None, min_rss=0):
    """(pid, name, rss) of the agent command with the largest resident set (see the
    module docstring), or None."""
    kids = {}
    for pid, (ppid, _, _, _) in procs.items():
        kids.setdefault(ppid, []).append(pid)
    never = {root} | set(kids.get(root, []))
    pid = os.getpid() if me is None else me
    while pid in procs and pid not in never:          # this process and the harness
        never.add(pid)
        pid = procs[pid][0]
    cands, todo = set(), list(kids.get(root, []))
    while todo:
        pid = todo.pop()
        if pid not in cands:
            cands.add(pid)
            todo.extend(kids.get(pid, []))
    roots = [w.rstrip("/") + "/" for w in work if w]
    if roots:
        for pid in procs:
            if pid not in cands and pid not in never:
                c = _cwd(proc, pid)
                if c and any((c.rstrip("/") + "/").startswith(r) for r in roots):
                    cands.add(pid)
    best = None
    for pid in cands - never:
        _, name, state, rss = procs[pid]
        if state == "Z" or rss < min_rss:
            continue
        if best is None or rss > best[2]:
            best = (pid, name, rss)
    return best


def _gone(proc, pid, timeout):
    """Wait until pid has exited (gone or a zombie, its memory released)."""
    end = time.time() + timeout
    while time.time() < end:
        stat = _read(os.path.join(proc, str(pid), "stat"))
        if not stat or stat[stat.rfind(")") + 2:].startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", action="append", default=[])
    ap.add_argument("--pct", type=float, default=90.0)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--min-rss-mib", type=int, default=256)
    ap.add_argument("--cgroup", default="/sys/fs/cgroup")
    ap.add_argument("--proc", default="/proc")
    ap.add_argument("--once", action="store_true", help="one pass, then write --out")
    ap.add_argument("--dry-run", action="store_true", help="choose, record, kill nothing")
    a = ap.parse_args()

    mx = _read(os.path.join(a.cgroup, "memory.max"))
    if not mx or not mx.isdigit():
        print("memwatch: no memory limit at %s; nothing to watch" % a.cgroup, file=sys.stderr)
        return 2
    limit = int(mx)
    threshold = limit * a.pct / 100 if a.pct > 0 else None
    rec = {"pct": a.pct, "interval_s": a.interval, "limit_mib": limit // MIB,
           "threshold_mib": int(threshold // MIB) if threshold else None,
           "max_pressure_mib": 0, "kills": [], "no_victim_passes": 0, "errors": 0,
           "slow_exits": 0}
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))

    def write():
        try:
            peak = _read(os.path.join(a.cgroup, "memory.peak")) or ""
            rec["memory_peak_mib"] = int(peak) // MIB if peak.isdigit() else None
            ev = _stat_kv(_read(os.path.join(a.cgroup, "memory.events")))
            rec["memory_events"] = {k: ev[k] for k in ("max", "oom", "oom_kill") if k in ev}
            tmp = a.out + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(rec, fh)
            os.replace(tmp, a.out)
        except OSError:
            rec["errors"] += 1

    write()
    written = 0
    while not stop:
        try:
            p = pressure(a.cgroup)
            if p is not None:
                rec["max_pressure_mib"] = max(rec["max_pressure_mib"], p[0] // MIB)
                if rec["max_pressure_mib"] - written >= 256:
                    written = rec["max_pressure_mib"]
                    write()
                if threshold and p[0] > threshold:
                    v = victim(processes(a.proc), a.root, a.work, a.proc,
                               min_rss=a.min_rss_mib * MIB)
                    if v is None:
                        rec["no_victim_passes"] += 1
                        if not a.once:
                            time.sleep(1.0)
                    else:
                        pid, name, rss = v
                        killed = a.dry_run
                        if not a.dry_run:
                            try:
                                os.kill(pid, signal.SIGKILL)
                                killed = True
                            except OSError:
                                pass
                        if killed:
                            rec["kills"].append({
                                "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "name": name, "rss_mib": rss // MIB, "pressure_mib": p[0] // MIB})
                            write()
                            # Let its memory come back before the next reading counts it.
                            if not a.dry_run and not _gone(a.proc, pid, 10.0):
                                rec["slow_exits"] += 1
        except Exception as e:  # one bad pass must not end the watch
            rec["errors"] += 1
            rec["last_error"] = ("%s: %s" % (type(e).__name__, e))[:200]
        if a.once:
            break
        time.sleep(a.interval)
    write()
    return 0


if __name__ == "__main__":
    sys.exit(main())
