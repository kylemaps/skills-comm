#!/usr/bin/env python
"""Every host the agents actually contacted, for building a firewall allowlist.

    python egress_report.py [runs_dir] [--min 1] [--csv out.csv]

An egress allowlist that is missing a host does not fail loudly. The agent gets
a connection error, writes no mask, and the run is scored as a model failure.
Six separate infrastructure faults on this benchmark have already presented that
way, so guessing at the allowlist is the expensive option.

This reads the transcripts we already have and reports what was reached, how
often, and by what: package installs look different from dataset fetches, and
both look different from the LLM gateway.

WHAT IT CANNOT SEE
------------------
Only what appears as a URL or a host-like string in a transcript. A tool that
resolves a host internally without printing it will be invisible here, so treat
the output as a floor. `datalad`, `git-annex` and `aws` are counted separately
for that reason: they reach endpoints their own way, and if they show up at all
their endpoints need adding by hand.

The right order is: allowlist from this, then run one cell, then read
INFRA_ERROR_RES for connection failures before scoring anything.
"""
import argparse
import csv
import glob
import os
import re
from collections import Counter, defaultdict

URL_RE = re.compile(r'\b(?:https?|ftp)://([A-Za-z0-9._-]+(?::\d+)?)', re.I)
BARE_HOST_RE = re.compile(
    r'\b([a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)+\.(?:org|com|net|edu|au|io|dev|ai))\b',
    re.I)

# Tools that reach the network on their own terms. If these appear, their
# endpoints are not necessarily in the URLs above and must be asked about
# rather than inferred.
FETCHERS = {
    "datalad": r"\bdatalad\s+(?:get|install|clone|download)",
    "git-annex": r"\bgit[- ]annex\b",
    "git clone": r"\bgit\s+clone\b",
    "aws cli": r"\baws\s+s3\b",
    "curl": r"\bcurl\s+[-a-zA-Z]*\s*https?://",
    "wget": r"\bwget\s+",
    "pip": r"\bpip\s+install\b",
    "conda/mamba": r"\b(?:conda|mamba)\s+install\b",
    "datalad-osf": r"\bdatalad[- ]osf\b",
    "templateflow": r"\btemplateflow\b",
}

# Rough grouping so the allowlist request reads as a request and not a dump.
GROUPS = [
    ("LLM gateway", r"llm\.|openwebui|litellm|api\.openai|anthropic"),
    ("neuroimaging data", r"openneuro|s3\.amazonaws|datasets\.datalad|"
                         r"templateflow|osf\.io|figshare|nitrc"),
    ("package registries", r"pypi|files\.pythonhosted|anaconda|conda\.|"
                           r"registry\.npmjs|deb\.|archive\.ubuntu|security\.ubuntu"),
    ("code hosting", r"github|gitlab|githubusercontent|raw\."),
    ("containers", r"docker\.io|registry-1|ghcr\.io|quay\.io|vnmd"),
    ("neurodesk", r"neurodesk|cvmfs|ardc\.edu\.au"),
    ("DOI resolution", r"doi\.org|dx\.doi|crossref"),
]


def group_of(host):
    for name, pat in GROUPS:
        if re.search(pat, host, re.I):
            return name
    return "other"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs_dir", nargs="?",
                    default=os.path.expanduser("~/bench/runs"))
    ap.add_argument("--min", type=int, default=1,
                    help="hide hosts seen in fewer than this many runs")
    ap.add_argument("--csv", default=None)
    a = ap.parse_args()

    hosts = Counter()          # host -> runs that contacted it
    host_hits = Counter()      # host -> total mentions
    fetchers = Counter()
    n = 0

    for d in sorted(glob.glob(os.path.join(a.runs_dir, "*__*"))):
        tr = glob.glob(os.path.join(d, "transcript*"))
        if not tr:
            continue
        n += 1
        try:
            with open(tr[0], errors="ignore", encoding="utf-8") as fh:
                txt = fh.read()
        except Exception:
            continue
        seen = set()
        for m in URL_RE.findall(txt):
            h = m.split(":")[0].lower().rstrip(".")
            seen.add(h)
            host_hits[h] += 1
        for m in BARE_HOST_RE.findall(txt):
            h = m.lower().rstrip(".")
            if h not in seen:
                seen.add(h)
                host_hits[h] += 1
        for h in seen:
            hosts[h] += 1
        for name, pat in FETCHERS.items():
            if re.search(pat, txt, re.I):
                fetchers[name] += 1

    if not n:
        raise SystemExit("no transcripts under %s" % a.runs_dir)

    print("=== %d transcripts scanned %s" % (n, "=" * 30))

    by_group = defaultdict(list)
    for h, c in hosts.items():
        if c >= a.min:
            by_group[group_of(h)].append((h, c, host_hits[h]))

    order = [g for g, _ in GROUPS] + ["other"]
    for g in order:
        rows = sorted(by_group.get(g, []), key=lambda r: (-r[1], r[0]))
        if not rows:
            continue
        print("")
        print("--- %s ---" % g)
        for h, c, hits in rows:
            print("  %-46s %4d runs  %6d mentions" % (h, c, hits))

    print("")
    print("--- tools that fetch on their own terms ---")
    if not fetchers:
        print("  none detected")
    for name in sorted(fetchers, key=lambda k: -fetchers[k]):
        print("  %-16s %4d runs" % (name, fetchers[name]))
    print("")
    print("  These reach endpoints their own way. If any appear above, ask for")
    print("  their endpoints explicitly rather than inferring from URLs.")

    print("")
    print("--- allowlist candidate, one per line ---")
    for h, c in sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0])):
        if c >= a.min:
            print("  %s" % h)
    print("")
    print("  A missing host does not fail loudly: the agent gets a connection")
    print("  error, writes no mask, and is scored as a model failure. Treat this")
    print("  as a floor, not a complete list.")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["host", "group", "runs", "mentions"])
            for h, c in sorted(hosts.items(), key=lambda kv: (-kv[1], kv[0])):
                w.writerow([h, group_of(h), c, host_hits[h]])
        print("")
        print("  wrote %s" % a.csv)


if __name__ == "__main__":
    main()
