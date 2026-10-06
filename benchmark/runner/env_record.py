#!/usr/bin/env python3
"""Record the software environment of one run into its run record.

    env_record.py --record run.json

Written into the record (each null when it cannot be read):

  cvmfs_revision   revision of the CVMFS repository ($CVMFS_ROOT, default
                   /cvmfs/neurodesk.ardc.edu.au), from its `user.revision` attribute.
                   Fixes every module and container the agent can load.
  python_version   the Python on PATH as `python3`
  python_packages  `python3 -m pip freeze` of that Python
  node_version     `node --version`
  os_release       PRETTY_NAME from /etc/os-release
  kernel           `uname -r`
  harness_sha      $HARNESS_SHA, the harness commit (CI removes .git before the run)
  ci_run           $GITHUB_RUN_ID/$GITHUB_RUN_ATTEMPT/$GITHUB_JOB, when in CI
  session_id_sent  $BENCH_SESSION_ID, sent to the gateway on every call as
                   x-litellm-session-id
  measured         what the pod has, read from the kernel (each null when unreadable):
                   cpus (cgroup cpu.max, else nproc), memory_limit_gib (cgroup
                   memory.max, else MemTotal), work_volume_gib (size of the filesystem
                   holding $BENCH_HOME), tmp_on_work_volume (/tmp is on that same
                   filesystem), tmp_free_gib. wave_check.py compares them with the
                   wave's `measured` declaration before the agent starts.

Always exits 0.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys


def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def cvmfs_revision(root):
    try:
        return os.getxattr(root, "user.revision").decode().strip()
    except (OSError, AttributeError, UnicodeDecodeError):
        return None


def os_release(path="/etc/os-release"):
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def _read(path):
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return None


def _fs_id(path):
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def measured(cgroup="/sys/fs/cgroup", meminfo="/proc/meminfo", work=None, tmp="/tmp"):
    """The pod's resources as the kernel reports them. Values are integers or booleans;
    None when a source cannot be read."""
    work = work or os.environ.get("BENCH_HOME") or os.getcwd()
    cpus = None
    cm = _read(os.path.join(cgroup, "cpu.max"))
    if cm and cm.split()[0] != "max":
        q, p = cm.split()[:2]
        cpus = -(-int(q) // int(p))
    if cpus is None:
        cpus = os.cpu_count()
    mem = None
    mm = _read(os.path.join(cgroup, "memory.max"))
    if mm and mm != "max":
        mem = (int(mm) + 2**29) // 2**30
    if mem is None:
        mi = _read(meminfo) or ""
        for line in mi.splitlines():
            if line.startswith("MemTotal:"):
                mem = (int(line.split()[1]) + 2**19) // 2**20
    try:
        total, _, free_tmp = shutil.disk_usage(tmp)
    except OSError:
        total = free_tmp = None
    try:
        work_total = shutil.disk_usage(work).total
    except OSError:
        work_total = None
    wf, tf = _fs_id(work), _fs_id(tmp)
    return {
        "cpus": cpus,
        "memory_limit_gib": mem,
        "work_volume_gib": work_total // 2**30 if work_total is not None else None,
        "tmp_on_work_volume": (wf == tf) if wf is not None and tf is not None else None,
        "tmp_free_gib": free_tmp // 2**30 if free_tmp is not None else None,
    }


def collect():
    py = run(["python3", "-c", "import sys; print(sys.version.split()[0])"])
    freeze = run(["python3", "-m", "pip", "freeze", "--all"])
    ci = None
    if os.environ.get("GITHUB_RUN_ID"):
        ci = "%s/%s/%s" % (os.environ.get("GITHUB_RUN_ID"),
                           os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                           os.environ.get("GITHUB_JOB", ""))
    return {
        "cvmfs_revision": cvmfs_revision(
            os.environ.get("CVMFS_ROOT", "/cvmfs/neurodesk.ardc.edu.au")),
        "python_version": py,
        "python_packages": sorted(freeze.splitlines()) if freeze is not None else None,
        "node_version": run(["node", "--version"]),
        "os_release": os_release(),
        "kernel": platform.release() or None,
        "harness_sha": os.environ.get("HARNESS_SHA") or None,
        "ci_run": ci,
        "session_id_sent": os.environ.get("BENCH_SESSION_ID") or None,
        "measured": measured(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", required=True)
    a = ap.parse_args()
    out = collect()
    try:
        rec = json.load(open(a.record, encoding="utf-8"))
        rec.update(out)
        with open(a.record, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
            fh.write("\n")
    except (OSError, ValueError) as e:
        print("env_record: could not update %s (%s)" % (a.record, e))
    print("cvmfs_revision=%s python=%s packages=%s harness=%s"
          % (out["cvmfs_revision"], out["python_version"],
             len(out["python_packages"] or []), out["harness_sha"]))
    print("measured: " + " ".join("%s=%s" % kv for kv in sorted(out["measured"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
