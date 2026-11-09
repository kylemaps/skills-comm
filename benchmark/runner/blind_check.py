#!/usr/bin/env python3
"""Nothing the agent can read names the subject or the dataset of a blinded run.

    BLIND_SUBJECT=<subject> BLIND_DATASET=<dataset id> \\
    python3 blind_check.py --prompt FILE [--scan PATH ...] [--event] [--environment]

The two names come from this process's own environment, set by the step that resolved
the case (never from the command line, which the job log shows). Every file under each
--scan path is checked by name and by content (gzip members decompressed, so a NIfTI
header inside .nii.gz counts), as is the prompt, the workflow event payload (--event,
GITHUB_EVENT_PATH) and the process environment (--environment, the two variables
themselves excepted). Exit 1 names the offending file or variable, never the string.
Exit 0 when nothing was found.
"""
import argparse
import gzip
import json
import os
import sys

CHUNK = 1 << 20


def needles():
    out = []
    for var in ("BLIND_SUBJECT", "BLIND_DATASET"):
        v = os.environ.get(var, "")
        if not v:
            sys.exit("FAIL: %s is not set" % var)
        out.append(v.encode())
    return out


def content_hit(path, ns):
    """True when any needle appears in the file's bytes (decompressed when gzip)."""
    longest = max(len(n) for n in ns)
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rb") as f:
            tail = b""
            while True:
                block = f.read(CHUNK)
                if not block:
                    return False
                buf = tail + block
                if any(n in buf for n in ns):
                    return True
                tail = buf[-(longest - 1):] if longest > 1 else b""
    except (OSError, EOFError):
        # An unreadable or truncated file is checked by name only.
        return False


def scan_tree(root, ns, hits):
    if os.path.isfile(root):
        files = [root]
    else:
        files = [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs]
    for p in files:
        rel = p.encode()
        if any(n in rel for n in ns):
            hits.append("name: " + p)
        elif content_hit(p, ns):
            hits.append("content: " + p)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompt", help="the rendered prompt")
    ap.add_argument("--scan", action="append", default=[], help="a file or directory to check")
    ap.add_argument("--event", action="store_true", help="also check GITHUB_EVENT_PATH")
    ap.add_argument("--environment", action="store_true", help="also check this process's environment")
    a = ap.parse_args(argv)
    ns = needles()
    hits = []
    if a.prompt:
        scan_tree(a.prompt, ns, hits)
    for p in a.scan:
        if os.path.exists(p):
            scan_tree(p, ns, hits)
    if a.event:
        ev = os.environ.get("GITHUB_EVENT_PATH")
        if ev and os.path.isfile(ev):
            try:
                blob = json.dumps(json.load(open(ev, encoding="utf-8"))).encode()
            except (OSError, ValueError):
                blob = open(ev, "rb").read()
            if any(n in blob for n in ns):
                hits.append("event: " + ev)
    if a.environment:
        for k, v in os.environ.items():
            if k in ("BLIND_SUBJECT", "BLIND_DATASET"):
                continue
            if any(n in (k + "=" + v).encode() for n in ns):
                hits.append("environment: " + k)
    if hits:
        for h in hits:
            print("FAIL: a blinded name appears in", h)
        return 1
    print("ok   no blinded name in %d scanned path(s)%s%s" % (
        len(a.scan) + (1 if a.prompt else 0), ", the event" if a.event else "",
        ", the environment" if a.environment else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
