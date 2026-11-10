#!/usr/bin/env python3
"""When each passing run last put its output in place, from the agent's own tool calls.

seconds_to_output (finalize_run.py) is the output file's mtime less the run's start. Some
ways of putting a file in place keep an older mtime: mv, ln, cp -p/-a, rsync -t/-a,
install -p, gzip of an uncompressed file, extracting an archive, and Python's shutil.move,
shutil.copy2/copytree, os.rename and os.replace. A run that wrote its mask at minute 20 and
moved it into place at minute 50 records 20.

This reads each passing run's opencode.db (every tool call, with opencode's own millisecond
timestamps) from the archived artifacts and finds the last such placement of the output.
permutation_effect.py --placements then counts the later of the two times.

    python3 -I output_time.py --results results/wave10 --sweep ../ci/sweep.json --wave 10 \\
        --artifacts <archive: <run id>/cell-*.zip> --work <new empty dir> --json placements.json

What counts as a placement: a shell command whose destination is the output (or a
directory, with the output's name as the source), with a shell variable holding the output
path resolved within the same call; a Python call above in the same call; or a script that
the agent wrote with such a call and then ran. A plain cp, a redirect or a write by a tool
gives the file a fresh mtime and is not a placement. A placement through any other route is
not seen.

The archive is untrusted: each opencode.db is copied out of its zip under a fixed name into
its own new directory below --work, read there, and deleted. A run whose artifact or
database is missing is listed and marked unchecked.
"""
import argparse
import glob
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wave_report import load, segment, wave_config  # noqa: E402
from permutation_effect import run_key  # noqa: E402

OUTPUT = "output.nii.gz"
DB = "/.xdg-data/opencode/opencode.db"

SEG = re.compile(r"&&|\|\||;|\n|\|")
ASSIGN = re.compile(r"(?:^|[\s;&(])(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)="
                    r"(\"[^\"]*\"|'[^']*'|[^\s;&|]+)")
PREFIX = {"sudo", "time", "command", "nohup", "env", "exec"}
PY_PLACE = re.compile(r"shutil\.(?:move|copy2|copytree)\s*\(|os\.(?:rename|replace)\s*\(")


def _expand(tok, env):
    for k, v in env.items():
        tok = tok.replace("${%s}" % k, v).replace("$%s" % k, v)
    return tok


def places_output(seg, env=None):
    """True when one shell command puts the output in place keeping an older mtime."""
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        return False
    toks = [_expand(t, env or {}) for t in toks]
    while toks and (toks[0] in PREFIX or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0])):
        toks = toks[1:]
    if not toks:
        return False
    cmd = os.path.basename(toks[0])
    opts = [t for t in toks[1:] if t.startswith("-")]
    args = [t for t in toks[1:] if not t.startswith("-")]
    short = "".join(o[1:] for o in opts if not o.startswith("--"))
    longs = {o.split("=")[0] for o in opts if o.startswith("--")}
    if cmd in ("mv", "ln", "cp", "rsync", "install"):
        if cmd == "cp" and not ("p" in short or "a" in short or longs & {"--preserve", "--archive"}):
            return False
        if cmd == "rsync" and not ("t" in short or "a" in short or longs & {"--times", "--archive"}):
            return False
        if cmd == "install" and not ("p" in short or "--preserve-timestamps" in longs):
            return False
        if len(args) < 2:
            return False
        dest, srcs = args[-1], args[:-1]
        if dest.rstrip("/").endswith(OUTPUT):
            return True
        return (not dest.endswith(".gz")
                and any(os.path.basename(s.rstrip("/")) == OUTPUT for s in srcs))
    if cmd in ("gzip", "pigz"):
        if set(short) & set("tlcd") or longs & {"--test", "--list", "--stdout", "--decompress"}:
            return False
        return any(a.endswith(OUTPUT[:-len(".gz")]) for a in args)
    if cmd == "tar":
        return OUTPUT in seg and ("x" in short or "--extract" in longs or bool(args and "x" in args[0]))
    if cmd == "unzip":
        return OUTPUT in seg and not set(short) & set("lt")
    return False


def last_placement(calls, t0):
    """(seconds from t0 of the last placement of the output, its kind) over tool calls given
    as (tool, input, epoch seconds), or (None, None)."""
    scripts = set()
    last = (None, None)
    for tool, inp, t in calls:
        if tool == "write":
            content = str(inp.get("content") or "")
            if OUTPUT in content and PY_PLACE.search(content):
                scripts.add(os.path.basename(str(inp.get("filePath") or "")))
            continue
        if tool != "bash":
            continue
        cmd = str(inp.get("command") or "")
        env = {name: val.strip("\"'") for name, val in ASSIGN.findall(cmd) if OUTPUT in val}
        kind = None
        if OUTPUT in cmd or env:
            if any(places_output(s, env) for s in SEG.split(cmd)):
                kind = "shell"
            elif OUTPUT in cmd and PY_PLACE.search(cmd):
                kind = "python"
        if not kind and any(s and s in cmd for s in scripts):
            kind = "script"
        if kind:
            last = (t - t0, kind)
    return last


def tool_calls(db):
    """(tool, input, epoch seconds) for every tool call in an opencode database, in order;
    the time is the call's end, else its start, else the part's creation."""
    con = sqlite3.connect(db)
    try:
        for created, data in con.execute("select time_created, data from part order by time_created"):
            try:
                d = json.loads(data)
            except (TypeError, ValueError):
                continue
            if not isinstance(d, dict) or d.get("type") != "tool":
                continue
            st = d.get("state") if isinstance(d.get("state"), dict) else {}
            tm = st.get("time") if isinstance(st.get("time"), dict) else {}
            t = tm.get("end") or tm.get("start") or created
            inp = st.get("input") if isinstance(st.get("input"), dict) else {}
            if isinstance(t, (int, float)):
                yield str(d.get("tool") or ""), inp, t / 1000.0
    finally:
        con.close()


def find_artifact(archive, ci_run, model, arm, rep):
    """The archived artifact zip of one job, or None (none, or more than one)."""
    hits = glob.glob(os.path.join(glob.escape(os.path.join(archive, ci_run)),
                                  glob.escape("cell-") + "*" +
                                  glob.escape("-%s-%s-r%s-%s.zip" % (segment(model), arm, rep, ci_run))))
    return hits[0] if len(hits) == 1 else None


def extract_db(zpath, dest):
    """Copy the run's opencode database (and its -wal/-shm) out of an artifact zip into dest
    under fixed names; the path of the copy, or None when the zip holds no single one."""
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        dbs = [n for n in names if n.startswith("runs/") and n.endswith(DB)]
        if len(dbs) != 1:
            return None
        for suffix in ("", "-wal", "-shm"):
            if dbs[0] + suffix in names:
                with z.open(dbs[0] + suffix) as src, \
                        open(os.path.join(dest, "opencode.db" + suffix), "wb") as dst:
                    shutil.copyfileobj(src, dst)
    return os.path.join(dest, "opencode.db")


def _epoch(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def check(rows, archive, work):
    """{run_key: {"checked", "placed_s", "kind", "recorded_s"}} for every passing run."""
    out = {}
    n = 0
    for r in rows:
        run = r.get("_run") or {}
        if r.get("passed") != "True" or r.get("valid") != "True" or r.get("lost"):
            continue
        rec = {"checked": False, "placed_s": None, "kind": None,
               "recorded_s": run.get("seconds_to_output")}
        out[run_key(r)] = rec
        z = find_artifact(archive, r.get("_ci_run") or "", r["model"], r["arm"], r["rep"])
        if not z or not run.get("start"):
            continue
        n += 1
        d = os.path.join(work, "r%04d" % n)
        os.mkdir(d)
        try:
            db = extract_db(z, d)
            if db:
                p, kind = last_placement(tool_calls(db), _epoch(run["start"]))
                rec.update(checked=True, placed_s=None if p is None else int(p), kind=kind)
        except (sqlite3.Error, zipfile.BadZipFile, OSError):
            pass
        finally:
            shutil.rmtree(d, ignore_errors=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="results/wave<N> directory")
    ap.add_argument("--sweep", required=True)
    ap.add_argument("--wave", required=True)
    ap.add_argument("--artifacts", required=True, help="archive: <run id>/cell-*.zip")
    ap.add_argument("--work", required=True, help="a new empty directory")
    ap.add_argument("--json", required=True)
    a = ap.parse_args()
    if not os.path.isdir(a.work) or os.listdir(a.work):
        print("ERROR: --work must be an existing empty directory", file=sys.stderr)
        return 2
    sweep = json.load(open(a.sweep, encoding="utf-8"))
    tasks, _reps, _pilot, _speed = wave_config(sweep, a.wave)
    rows = [r for rs in load(a.results, tasks).values() for r in rs]
    res = check(rows, a.artifacts, a.work)
    with open(a.json, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1, sort_keys=True)
        fh.write("\n")
    later = [k for k, v in res.items() if v["placed_s"] is not None and v["recorded_s"] is not None
             and v["placed_s"] > v["recorded_s"]]
    print("passing runs %d: checked %d, not checked %d; placed later than recorded %d"
          % (len(res), sum(v["checked"] for v in res.values()),
             sum(not v["checked"] for v in res.values()), len(later)))
    for k in sorted(later):
        v = res[k]
        print("  %s: recorded %.1f min, placed %.1f min (%s)"
              % (k, v["recorded_s"] / 60.0, v["placed_s"] / 60.0, v["kind"]))
    for k in sorted(k for k, v in res.items() if not v["checked"]):
        print("  not checked: %s" % k)
    return 0


if __name__ == "__main__":
    sys.exit(main())
