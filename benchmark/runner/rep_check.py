#!/usr/bin/env python3
"""The stop-and-look over one rep of a wave: the gate a wave's README declares, read from
the run records. It prints gate fields only, never a verdict, a score or a time, so the
decision to continue cannot be informed by the outcome.

    python -I rep_check.py --sweep SWEEP --wave N --order ORDER --since ISO --work EMPTY_DIR
        [--rep 1] [--harness SHA] [--repo R] [--no-download]

Downloads every artifact of the run.yml runs created at or after --since into --work (a
new, empty directory: the files are untrusted and only read as data) and reads each
run.json of the wave and rep.

  G1 every declared (task, model, arm) has one record whose agent started
  G2 no run stopped by the key proxy itself (proxy-lock-failed), and every started run has
     its proxy summary. A run stopped before the agent because its provider refused the
     probe (proxy-upstream-failed with 429, 5xx or no answer) is a harness fault, re-run.
  G3 each summary: locked to the run's model, calls for that model only, none before the
     lock, not sealed without a lock
  G4 model_fingerprint and model_fingerprint_end are the declared fingerprint, and
     model_changed is not true
  G5 one prompt_hash per run id (task and case)
  G6 a run id that names a case: every record carries its input record, with the same file
     digests in every record of that run id
  G7 no job stopped by the blind check (the "Fetch the input of a blinded run" step failed)
  G8 pins and measured values as the wave declares (tasks_sha, image_version, the harness
     commit when --harness is given, measured resources)
  G9 the panel can still answer the primary question: every declared model has a started
     record in every cell of the wave's primary family (waves.<n>.primary_family, when set)

Reported, not part of the gate: subject_named per arm (true, false, unchecked), answer-key
reads per arm, memory watch kills and errors, kernel OOM events, proxy flags, model limits,
and key-shaped strings in the downloaded files (counts per file only).
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys

# "sk-" only at a word start: "task-structural-..." holds "sk-" too.
KEY_SHAPES = [re.compile(rb"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
              re.compile(rb"Bearer\s+[A-Za-z0-9._~+/=-]{24,}"),
              re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")]
UPSTREAM_RERUN = re.compile(r"HTTP (429|5\d\d)\b|timed out|no answer|URLError|Connection", re.I)
BLIND_STEP = "Fetch the input of a blinded run"
GATES = ("G1 no started record", "G2 stopped by the proxy", "G2 no proxy summary", "G3 proxy summary",
         "G4 fingerprint", "G5 prompt_hash", "G6 input record", "G7 stopped by the blind check",
         "G8 pins and measured", "G9 panel")


def gh_json(args):
    out = subprocess.run(["gh"] + args, capture_output=True, text=True, check=True).stdout
    return json.loads(out or "null")


def download(repo, since, work):
    """(runs seen, ids not completed, artifacts downloaded, jobs the blind check stopped)."""
    runs = gh_json(["run", "list", "-R", repo, "--workflow", "run.yml", "--limit", "400",
                    "--json", "databaseId,createdAt,status"])
    runs = [r for r in runs if r["createdAt"] >= since]
    pending = [r["databaseId"] for r in runs if r["status"] != "completed"]
    n, blind = 0, []
    for r in runs:
        rid = r["databaseId"]
        jobs = gh_json(["api", "repos/%s/actions/runs/%d/jobs?per_page=100" % (repo, rid)])
        for j in (jobs or {}).get("jobs", []):
            if any(s.get("name") == BLIND_STEP and s.get("conclusion") == "failure"
                   for s in j.get("steps") or []):
                blind.append("%s (run %d)" % (j.get("name"), rid))
        arts = gh_json(["api", "repos/%s/actions/runs/%d/artifacts?per_page=100" % (repo, rid)])
        for a in arts.get("artifacts", []):
            if a.get("expired"):
                continue
            dest = os.path.join(work, "%d-%d" % (rid, a["id"]))
            if os.path.isdir(dest):
                continue
            subprocess.run(["gh", "run", "download", str(rid), "-R", repo, "-n", a["name"], "-D", dest],
                           capture_output=True, check=True)
            n += 1
    return len(runs), pending, n, blind


def records(work):
    for root, _dirs, files in os.walk(work):
        if "run.json" in files and os.path.basename(os.path.dirname(root)) == "runs":
            try:
                with open(os.path.join(root, "run.json"), encoding="utf-8") as fh:
                    yield root, json.load(fh)
            except (OSError, ValueError) as e:
                yield root, {"_unreadable": str(e)}


def key_shapes(work):
    hits = {}
    for root, _dirs, files in os.walk(work):
        for f in files:
            p = os.path.join(root, f)
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            c = sum(len(rx.findall(data)) for rx in KEY_SHAPES)
            if c:
                hits[os.path.relpath(p, work)] = c
    return hits


def read_order(path):
    order = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 4 and parts[0].isdigit():
                order[(parts[1], parts[2], parts[3])] = int(parts[0])
    return order


def check(wave, wave_id, order, recs, rep, harness=None, blind_stops=()):
    """(fail, note, started, reruns, outcomes) for the records of one wave and rep. `fail`
    maps a gate to its problems; `note` holds descriptive findings."""
    fps = {k: v for k, v in (wave.get("model_fingerprints") or {}).items() if not k.startswith("_")}
    incomplete = {k for k in (wave.get("incomplete") or {}) if not k.startswith("_")}
    pins, measured = wave.get("pins") or {}, wave.get("measured") or {}
    fail, note = collections.defaultdict(list), collections.defaultdict(list)
    started, reruns = collections.defaultdict(list), []
    prompts, inputs = collections.defaultdict(set), collections.defaultdict(set)
    named = collections.defaultdict(collections.Counter)
    outcomes = collections.Counter()
    for where, r in recs:
        if "_unreadable" in r:
            note["unreadable"].append(where)
            continue
        if r.get("repeat") != rep or str(r.get("wave")) != str(wave_id):
            continue
        pair = (r.get("task_id"), r.get("model"), r.get("condition"))
        rid = str(r.get("ci_run", "")).split("/")[0]
        tag = "%s %s %s r%s (run %s)" % (pair + (r.get("repeat"), rid))
        if pair not in order:
            note["not declared"].append(tag)
        m = r.get("model")
        if m in incomplete:
            note["incomplete model, exploratory (%s)" % m].append(tag)
            continue
        if r.get("agent_outcome") == "not_started":
            why, err = r.get("stopped_before_agent"), str(r.get("model_probe_error") or "")
            if why == "proxy-upstream-failed" and UPSTREAM_RERUN.search(err):
                reruns.append("%s: %s, probe %s" % (tag, why, err))
            elif why == "proxy-lock-failed":
                fail["G2 stopped by the proxy"].append("%s: %s" % (tag, why))
            else:
                note["stopped before the agent, other"].append("%s: %s, probe %s" % (tag, why, err))
            continue
        started[pair].append(tag)
        outcomes[r.get("agent_outcome")] += 1
        px = r.get("llm_proxy")
        if not isinstance(px, dict) or "error" in px or "calls" not in px:
            fail["G2 no proxy summary"].append("%s: %s" % (tag, px))
        else:
            calls = px.get("calls") or {}
            if px.get("locked") != [m] or set(calls) != {m} or (px.get("unlocked_calls") or 0) \
                    or px.get("sealed_without_lock"):
                fail["G3 proxy summary"].append("%s: locked %s calls %s unlocked %s sealed %s" % (
                    tag, px.get("locked"), calls, px.get("unlocked_calls"), px.get("sealed_without_lock")))
            for k in ("denied", "mismatches", "calls_after_end", "relock_attempts"):
                if px.get(k):
                    note["proxy flag " + k].append("%s: %s" % (tag, px.get(k)))
        if not (r.get("model_fingerprint") == r.get("model_fingerprint_end") == fps.get(m)) \
                or r.get("model_changed"):
            fail["G4 fingerprint"].append("%s: %s / %s, changed %s, declared %s" % (
                tag, r.get("model_fingerprint"), r.get("model_fingerprint_end"),
                r.get("model_changed"), fps.get(m)))
        prompts[pair[0]].add(r.get("prompt_hash"))
        if re.search(r"@case-\d{3}$", str(pair[0])):
            inp = r.get("input")
            files = (inp or {}).get("files") if isinstance(inp, dict) else None
            if not files or (inp.get("case") != str(pair[0]).split("@")[1]):
                fail["G6 input record"].append("%s: no input record for its case" % tag)
            else:
                inputs[pair[0]].add(tuple(sorted((f.get("as"), f.get("sha256")) for f in files)))
            sn = r.get("subject_named")
            named[pair[2]]["true" if sn is True else "false" if sn is False else "unchecked"] += 1
        if r.get("answer_key_reads"):
            note["answer-key reads, arm %s" % pair[2]].append(tag)
        ms = r.get("measured") or {}
        for k, v in measured.items():
            got = ms.get(k[:-4] if k.endswith("_min") else k)
            ok = (got is not None and float(got) >= float(v)) if k.endswith("_min") else got == v
            if not ok:
                fail["G8 pins and measured"].append("%s: measured %s is %s (declared %s)" % (tag, k, got, v))
        for k, want in sorted(pins.items()):
            if r.get(k) != want:
                fail["G8 pins and measured"].append("%s: %s is %s (declared %s)" % (tag, k, r.get(k), want))
        if harness and not str(r.get("harness_sha", "")).startswith(harness):
            fail["G8 pins and measured"].append("%s: harness_sha %s (declared %s)" % (
                tag, str(r.get("harness_sha", ""))[:12], harness))
        if r.get("model_limit") != {"context": 131000, "output": 32000}:
            note["model_limit"].append("%s: %s" % (tag, r.get("model_limit")))
        mw = r.get("memwatch") or {}
        for k in ("errors", "slow_exits", "no_victim_passes"):
            if mw.get(k):
                note["memwatch " + k].append("%s: %s" % (tag, mw.get(k)))
        for kill in mw.get("kills") or []:
            note["memwatch kill"].append("%s: %s rss %s MiB at %s MiB" % (
                tag, kill.get("name"), kill.get("rss_mib"), kill.get("pressure_mib")))
        ev = mw.get("memory_events") or {}
        if ev.get("oom") or ev.get("oom_kill"):
            note["kernel oom"].append("%s: %s" % (tag, ev))

    for (task, model, arm), cell in sorted(order.items(), key=lambda kv: kv[1]):
        if model in incomplete:
            continue
        n = len(started.get((task, model, arm), []))
        if n == 0:
            fail["G1 no started record"].append("cell %d %s %s %s" % (cell, task, model, arm))
        elif n > 1:
            note["more than one started record"].append("cell %d: %s" % (cell, started[(task, model, arm)]))
    for task, hashes in sorted(prompts.items()):
        if len(hashes) != 1:
            fail["G5 prompt_hash"].append("%s: %s" % (task, sorted(str(h) for h in hashes)))
    for task, digests in sorted(inputs.items()):
        if len(digests) != 1:
            fail["G6 input record"].append("%s: %d different inputs across its records" % (task, len(digests)))
    for x in blind_stops:
        fail["G7 stopped by the blind check"].append(x)
    fam = wave.get("primary_family")
    if fam:
        tasks = wave.get("tasks") or {}
        for (task, model, arm) in sorted(order):
            if (tasks.get(task) or {}).get("family") == fam and (
                    model in incomplete or not started.get((task, model, arm))):
                fail["G9 panel"].append("%s %s %s: no started record in the primary family" % (task, model, arm))
    for arm, c in sorted(named.items()):
        note["subject_named, arm %s" % arm].append(
            "true %d, false %d, unchecked %d" % (c["true"], c["false"], c["unchecked"]))
    return fail, note, started, reruns, outcomes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--wave", required=True)
    ap.add_argument("--order", required=True)
    ap.add_argument("--since")
    ap.add_argument("--work", required=True, help="a new, empty directory for the downloads")
    ap.add_argument("--repo", default="neurodesk/skills-benchmark")
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--harness", help="the harness commit the wave pins (a prefix is enough)")
    ap.add_argument("--no-download", action="store_true", help="re-check what --work already holds")
    a = ap.parse_args()

    with open(a.sweep, encoding="utf-8") as fh:
        wave = json.load(fh)["waves"][str(a.wave)]
    order = read_order(a.order)
    os.makedirs(a.work, exist_ok=True)
    blind = []
    if not a.no_download:
        if not a.since:
            sys.exit("--since is required to download")
        if os.listdir(a.work):
            sys.exit("--work must be empty (or use --no-download)")
        nruns, pending, nart, blind = download(a.repo, a.since, a.work)
        print("downloaded %d artifacts from %d runs since %s; %d runs not completed: %s"
              % (nart, nruns, a.since, len(pending), pending))
    else:
        print("no download: G7 (jobs stopped by the blind check) is not checked")

    fail, note, started, reruns, outcomes = check(wave, a.wave, order, records(a.work), a.rep, a.harness, blind)
    incomplete = {k for k in (wave.get("incomplete") or {}) if not k.startswith("_")}
    gate_pairs = [p for p in order if p[1] not in incomplete]
    hits = key_shapes(a.work)
    print("\nwave %s rep %d; incomplete (not in the gate): %s" % (a.wave, a.rep, sorted(incomplete) or "none"))
    print("started records: %d of %d declared pairs; agent outcomes %s" % (
        sum(1 for p in gate_pairs if started.get(p)), len(gate_pairs), dict(outcomes)))
    print("re-runs, provider refused the probe (not counted against the gate): %d" % len(reruns))
    for x in reruns:
        print("  " + x)
    for k in sorted(note):
        print("NOTE %s (%d)" % (k, len(note[k])))
        for x in note[k][:12]:
            print("  " + x)
    print("key-shaped strings: %s" % ("none" if not hits else "%d files (counts only): %s" % (len(hits), hits)))
    for g in GATES:
        if g in fail:
            print("FAIL %s (%d)" % (g, len(fail[g])))
            for x in fail[g][:12]:
                print("  " + x)
    ok = not fail and not hits
    print("\nGATE: %s" % ("PASS" if ok else "NOT PASSED (see FAIL lines%s)"
                         % (" and key-shaped strings" if hits else "")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
