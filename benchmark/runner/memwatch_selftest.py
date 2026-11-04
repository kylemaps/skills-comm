#!/usr/bin/env python3
"""memwatch stops what it should: run from a pod of the pool, no agent, no model.

    memwatch_selftest.py [--memwatch PATH] [--data-limit-kb N] [--pct N] [--cgroup DIR]
    memwatch_selftest.py --judge FILE    # the checks alone, on a result this printed

Two cases, with the run's settings (the per-command limit of the agent's BASH_ENV,
memwatch's threshold):

single  under the per-command limit (ulimit -d), one allocation larger than the limit
        fails at once with MemoryError; one of 1 GiB succeeds.
pair    two commands of an agent shell grow together past memwatch's threshold, each
        alone well under it: memwatch stops exactly one (SIGKILL; the shell sees 137),
        the other finishes, and the kernel kills nothing (memory.events oom and
        oom_kill unchanged). The process tree is a run's: --root, the agent shell
        (its direct child, never stopped), the agent's commands.

Each command's size is taken from the pod: half of what is left below the threshold,
plus a fifth of the gap between the threshold and the limit. Together they end above
the threshold and below the limit, so a memwatch that does not fire fails this test
instead of the kernel ending the pod; alone, either stays under the threshold.

Not covered here: a command found by its working directory after it left the agent's
tree (nohup, setsid), the agent as a child of `timeout`, the key proxy's memory.

Prints each check, then the result as one JSON line (`RESULT {...}`; memwatch's record
holds no command lines). Exit 1 when an expectation misses, 2 when the cgroup has no
memory limit.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time

MIB = 1024 * 1024
GIB = 1024 * MIB

ALLOC = r"""
import sys, time
target, hold, chunk = int(sys.argv[1]), float(sys.argv[2]), 64 << 20
held = []
while len(held) * chunk < target:
    held.append(b"\x01" * chunk)     # written, so the pages are real
    time.sleep(0.05)
print("done", len(held) * chunk >> 20, flush=True)
time.sleep(hold)
"""

# The agent shell: waits for the watch to start, then two commands at once, under the
# run's per-command limit.
AGENT = r"""
ulimit -d "$KB"
while [ ! -e "$D/go" ]; do sleep 0.1; done
SIZE=$(cat "$D/size")
"$PY" "$D/alloc.py" "$SIZE" 15 > "$D/a1.out" 2>&1 & p1=$!
"$PY" "$D/alloc.py" "$SIZE" 15 > "$D/a2.out" 2>&1 & p2=$!
wait $p1; e1=$?
wait $p2; e2=$?
echo "$e1 $e2" > "$D/rc"
"""

# One allocation of N bytes under the per-command limit: bash -c SCRIPT PY KB N.
SINGLE = 'ulimit -d "$1"; exec "$0" -c "import sys; b = bytes([1]) * int(sys.argv[1])" "$2"'


def events(cg):
    out = {}
    try:
        with open(os.path.join(cg, "memory.events")) as fh:
            for line in fh:
                k, _, v = line.partition(" ")
                if k in ("max", "oom", "oom_kill") and v.strip().isdigit():
                    out[k] = int(v)
    except OSError:
        pass
    return out


def single(py, kb):
    def alloc(n):
        return subprocess.run(["bash", "-c", SINGLE, py, str(kb), str(n)],
                              capture_output=True, text=True, timeout=120)
    over = alloc(kb * 1024 + GIB)
    under = alloc(GIB)
    return {"over_rc": over.returncode, "over_memoryerror": "MemoryError" in over.stderr,
            "under_rc": under.returncode}


def pair(py, memwatch, mw_mod, cg, kb, pct, limit, threshold):
    d = tempfile.mkdtemp(prefix="mwtest-")
    with open(os.path.join(d, "alloc.py"), "w") as fh:
        fh.write(ALLOC)
    env = dict(os.environ, D=d, PY=py, KB=str(kb), AGENT=AGENT)
    # `; true` keeps the outer shell from exec'ing the inner one: it stays --root. Its
    # own session, so a timeout stops the whole tree.
    root = subprocess.Popen(["bash", "-c", 'bash -c "$AGENT"; true'], env=env,
                            start_new_session=True)
    out = os.path.join(d, "mw.json")
    watch = subprocess.Popen([py, memwatch, "--root", str(root.pid), "--out", out,
                              "--pct", str(pct), "--interval", "0.2", "--cgroup", cg])
    t = time.time()
    while not os.path.exists(out) and time.time() - t < 15:
        time.sleep(0.1)
    ev0 = events(cg)
    # Sized from the pressure just before the commands start.
    before = mw_mod.pressure(cg)[0]
    size = int((threshold - before) / 2 + (limit - threshold) / 5)
    with open(os.path.join(d, "size"), "w") as fh:
        fh.write(str(size))
    open(os.path.join(d, "go"), "w").close()
    try:
        root.wait(timeout=300)
    except subprocess.TimeoutExpired:
        os.killpg(root.pid, signal.SIGKILL)
        root.wait()
    time.sleep(1)
    watch.send_signal(signal.SIGTERM)
    try:
        watch.wait(timeout=15)
    except subprocess.TimeoutExpired:
        watch.kill()
    ev1 = events(cg)

    def read(name):
        try:
            with open(os.path.join(d, name)) as fh:
                return fh.read()
        except OSError:
            return ""
    try:
        rec = json.loads(read("mw.json") or "null")
    except ValueError:
        rec = None
    rcs = [int(x) for x in read("rc").split() if x.lstrip("-").isdigit()]
    done = sum(read(a).startswith("done") for a in ("a1.out", "a2.out"))
    return {"pressure_before_mib": before // MIB, "size_each_mib": size // MIB, "rcs": rcs,
            "done": done, "memwatch": rec, "events_before": ev0, "events_after": ev1}


class Checks:
    def __init__(self):
        self.failed = 0

    def expect(self, name, ok, got):
        print("%s %s: %s" % ("ok  " if ok else "FAIL", name, got))
        self.failed += not ok


def judge(res):
    c = Checks()
    want = res.get("expect_limit_mib")
    c.expect("the pod's memory limit is the run's", want is None or res.get("limit_mib") == want,
             "%s MiB (expected %s)" % (res.get("limit_mib"), want))
    s = res.get("single") or {}
    c.expect("single: an allocation over the per-command limit fails with MemoryError",
             s.get("over_rc") == 1 and s.get("over_memoryerror") is True,
             "exit %s, MemoryError %s" % (s.get("over_rc"), s.get("over_memoryerror")))
    c.expect("single: an allocation of 1 GiB under it succeeds", s.get("under_rc") == 0,
             "exit %s" % s.get("under_rc"))
    p = res.get("pair") or {}
    m = p.get("memwatch") or {}
    kills = m.get("kills") or []
    thr = res.get("threshold_mib") or 0
    c.expect("pair: memwatch stopped exactly one command", len(kills) == 1,
             "%d kill(s) %s" % (len(kills), [(k.get("name"), k.get("rss_mib"), k.get("pressure_mib"))
                                             for k in kills]))
    k = kills[0] if kills else {}
    half = (thr - (p.get("pressure_before_mib") or 0)) / 2
    c.expect("pair: it stopped one of the agent's commands, above the threshold, at its size",
             str(k.get("name", "")).startswith("python") and (k.get("pressure_mib") or 0) >= thr
             and (k.get("rss_mib") or 0) >= 0.8 * half,
             "%s rss %s MiB at pressure %s MiB (threshold %s)"
             % (k.get("name"), k.get("rss_mib"), k.get("pressure_mib"), thr))
    c.expect("pair: the shell saw it killed (137) and the other command finished",
             sorted(p.get("rcs") or []) == [0, 137] and p.get("done") == 1,
             "exits %s, finished %s" % (p.get("rcs"), p.get("done")))
    b, a = p.get("events_before") or {}, p.get("events_after") or {}
    c.expect("pair: the kernel killed nothing (oom, oom_kill unchanged)",
             bool(a) and all(a.get(x) == b.get(x) for x in ("oom", "oom_kill")),
             "before %s, after %s" % (b, a))
    c.expect("pair: the highest pressure recorded is above the threshold",
             (m.get("max_pressure_mib") or 0) >= thr, "%s MiB" % m.get("max_pressure_mib"))
    c.expect("pair: every pass above the threshold had a command to stop, no errors",
             m.get("no_victim_passes") == 0 and m.get("errors") == 0,
             "no_victim_passes %s, errors %s" % (m.get("no_victim_passes"), m.get("errors")))
    c.expect("pair: the stopped command was gone within memwatch's wait",
             m.get("slow_exits") == 0, "slow_exits %s" % m.get("slow_exits"))
    return 1 if c.failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--memwatch", default=os.path.join(here, "memwatch.py"))
    ap.add_argument("--data-limit-kb", type=int, default=25165824)
    ap.add_argument("--pct", type=float, default=90.0)
    ap.add_argument("--cgroup", default="/sys/fs/cgroup")
    ap.add_argument("--expect-limit-mib", type=int, help="the pod's memory limit a run has")
    ap.add_argument("--judge", metavar="FILE")
    a = ap.parse_args()
    if a.judge:
        with open(a.judge) as fh:
            return judge(json.load(fh))
    try:
        with open(os.path.join(a.cgroup, "memory.max")) as fh:
            limit = int(fh.read().strip())
    except (OSError, ValueError):
        print("FAIL: no memory limit at %s; nothing to test" % a.cgroup)
        return 2
    sys.path.insert(0, os.path.dirname(os.path.abspath(a.memwatch)))
    import memwatch as mw_mod
    threshold = limit * a.pct / 100
    res = {"limit_mib": limit // MIB, "threshold_mib": int(threshold // MIB),
           "data_limit_kb": a.data_limit_kb, "pct": a.pct, "expect_limit_mib": a.expect_limit_mib}
    res["single"] = single(sys.executable, a.data_limit_kb)
    res["pair"] = pair(sys.executable, a.memwatch, mw_mod, a.cgroup, a.data_limit_kb, a.pct,
                       limit, threshold)
    rc = judge(res)
    print("RESULT " + json.dumps(res, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
