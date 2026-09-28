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

Always exits 0.
"""
import argparse
import json
import os
import platform
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
