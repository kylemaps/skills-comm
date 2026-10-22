#!/usr/bin/env python
"""Append post-run facts to a run's provenance record.

    python finalize_run.py <run_dir> [exit_code]

`run.json` is written *before* the agent starts, so it cannot know which tools the
agent chose. This reads them back out of the transcript once the run is over:

  tools_loaded   every `<tool>/<version>` loaded with `module load` or `ml` in an
                 executed command or a script the agent wrote
  methods_used   which brain-extraction method it actually RAN. Not the same thing as
                 the module it loaded: an agent can `module load fsl` and then call
                 `bet`, and one model loaded hd-bet and still fell back to BET.
  not_found_claims  times the agent claimed a tool/module was unavailable
  tokens_*       token accounting, read out of opencode's own SQLite database. The
                 gateway returns a `usage` block on every call and opencode totals it
                 per session, so this works retroactively on runs already on disk --
                 nothing has to be re-run to get it.
  dataset_pin    dataset version the agent pinned itself (e.g. `git checkout 1.1.0`)
  skill_loads    successful skill tool calls
  skills_seen    which skills those were
  skill_load_failures  skill tool calls that failed
  skills_available     skills opencode listed in a failed call's error; None if
                       it never listed them
  skill_files_read     SKILL.md / references/*.md files opened directly
  answer_key_reads     reads of grading material and fetches of where it is
                       published; [] is clean, None means no transcript
  arm_seen             transcript lines naming an arm label; [] is clean
  opencode_errors      opencode's own error messages, excluding tool-call errors
  off_spec       why this run left the benchmark environment: "sudo",
                 "package-install", "external-image", "external-download". [] is
                 clean; None means no commands could be read, which is NOT clean.
  used_sudo / installed_packages / external_images / external_downloads
                 the evidence behind it
  tool_routes    how tools were obtained, any of "lmod", "cvmfs-direct",
                 "external-image", "pip", "conda", "download". Descriptive, not
                 an exclusion: a clean run can still show "cvmfs-direct". None
                 under the same condition as off_spec.
  commands_from  where the executed commands were read: "opencode-db" (every
                 bash tool call, in full) or "transcript" (first line of each)
  bash_calls     number of bash tool calls (None when read from the transcript)
  web_fetches    WebFetch tool calls (the database when readable, else the
                 transcript's `% WebFetch` lines)
  web_searches   web search calls (the transcript's `◈` lines)
  web_hosts      hosts named in executed commands, fetches and the scripts the
                 agent wrote. Descriptive, never an exclusion: the task's data
                 comes over the network too. All three None when neither the
                 transcript nor the database could be read.
  exit_code / output_present / end
  tokens_output_per_s  output tokens per second of generation, from the database's
                       per-message timings; generation_s the seconds summed
  agent_outcome        completed / timeout / gateway_cut / no_model_call

Safe to re-run: it only adds keys. Also usable to backfill older runs.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

if len(sys.argv) < 2:
    sys.exit("usage: finalize_run.py <run_dir> [exit_code]")

run_dir = sys.argv[1]
rc = sys.argv[2] if len(sys.argv) > 2 else None

rj = os.path.join(run_dir, "run.json")
if not os.path.exists(rj):
    sys.exit(0)

try:
    rec = json.load(open(rj, encoding="utf-8"))
except Exception:
    sys.exit(0)

tr = os.path.join(run_dir, "transcript.txt")
txt = ""
if os.path.exists(tr):
    with open(tr, encoding="utf-8", errors="replace") as fh:
        txt = fh.read()
ANSI = re.compile(r"\x1b\[[0-9;]*m")
plain = ANSI.sub("", txt)

# Method detection lives in summarize.py so the two never drift apart. Guarded
# because this runs in the hot path of every run: a missing sibling must not cost
# us the rest of the provenance record.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from summarize import detect_methods, NOT_FOUND_RE
    rec["methods_used"] = detect_methods(txt)
    rec["not_found_claims"] = len(NOT_FOUND_RE.findall(txt))
    # Ordered by first appearance, and counted. The set tells you WHICH tools a
    # run touched; the order tells you what it fell back FROM, and the count is a
    # proxy for how many times it re-extracted after a failed QC. A run that went
    # BET -> SynthStrip is a different story from one that only ever ran
    # SynthStrip, and the set cannot tell them apart.
    from summarize import METHOD_PATTERNS
    seen = []
    total = 0
    for name, pat in METHOD_PATTERNS:
        hits = list(re.finditer(pat, txt, re.I))
        total += len(hits)
        if hits:
            seen.append((hits[0].start(), name))
    rec["method_sequence"] = [n for _, n in sorted(seen)]
    rec["method_invocations"] = total
except Exception:
    pass
# --- effort and behaviour, read back out of the transcript -------------------
# Deliberately format-agnostic. These match on CONTENT (paths, error text, tool
# names) rather than on how opencode frames a tool call, so they survive an agent
# runtime upgrade. Where a signal is a heuristic it is named as one: error_lines
# counts lines that look like failures, which is not the same as counting commands
# that returned non-zero, and pretending otherwise would put a wrong number in a
# table nobody could check.
rec["error_lines"] = len(re.findall(
    "command not found|No such file or directory|Permission denied|Traceback"
    "|is not recognized|cannot access|Segmentation fault", txt, re.I))

# Progressive disclosure is the central design claim of a skill: a lean SKILL.md
# that pulls detail from references/ on demand. Nothing measured whether agents
# ever open those files.
rec["references_opened"] = sorted(set(
    re.findall("references/([a-z0-9_-]+[.]md)", txt, re.I)))

# Every kimi run touched the scheduler, in every arm including no-skill, in a
# container where slurmd cannot start. That is effort spent on a dead end and it
# should be a column, not something we grep for by hand each time we wonder.
rec["scheduler_mentions"] = len(re.findall("slurm|sbatch|squeue|scontrol", txt, re.I))

# Did the agent QC its own output, as both skills instruct?
rec["qc_ran"] = bool(re.search(
    "qc_metrics|afni_outline_qc|qc_mosaic|ai_eval|brain-extraction-qc", txt, re.I))

# --- off_spec: did the run leave the benchmark environment? -------------------
# The benchmark environment provides Neurodesk tools through Lmod and nothing else.
# The runner allows sudo, so leaving the environment is detected, not prevented:
#   sudo               a command run with sudo
#   package-install    an apt/pip/conda/npm install
#   external-image     a container image fetched from outside CVMFS
#   external-download  a tool, its source, or model weights fetched from outside
#                      the environment: curl/wget, `git clone`, or `pip download`.
#                      NOT the task's DATA: anything naming OpenNeuro, a Hugging
#                      Face *dataset* path (.../datasets/...), or fetched with
#                      datalad/the openneuro CLI (different verbs, so they never
#                      match the patterns below anyway). NOT `uvx astra-tools`
#                      (the one external CLI this environment provides on
#                      purpose) and NOT a bare `pip install` (already
#                      package-install). curl/wget only count against a
#                      conservative allowlist of hosts that serve software
#                      (github/githubusercontent, the micromamba installer,
#                      conda/pytorch/pypi mirrors, huggingface) or a
#                      software-shaped extension (.whl/.pt/.pth/.ckpt/
#                      .safetensors); an unlisted host is read as innocent, not
#                      guilty, same as everything else this file flags.
#
# tool_routes is a separate, non-exclusionary field (see near `tools_loaded`
# below): which of lmod / cvmfs-direct / external-image / pip / conda / download
# a run actually used to obtain its tools, so "ran the CVMFS container directly
# instead of `module load`" -- in-spec, but a different route -- is visible
# without being off_spec.
#
# Only executed commands are read. Preferred source: the bash tool calls in
# opencode's database, which hold each command in full. Fallback: transcript lines
# opencode prefixes with `$ `, which show only the first line of a multi-line
# command (the rest is indistinguishable from output). Plus the lines of shell
# scripts the agent wrote (it runs them by name, so their content is not in the
# transcript). Text the agent writes is not a command.
def _open_db(run_dir):
    """(connection, path) of the opencode database this run used, or None."""
    candidates = [
        os.environ.get("OPENCODE_DB"),
        os.path.join(run_dir, ".xdg-data", "opencode", "opencode.db"),
        os.path.expanduser("~/.local/share/opencode/opencode.db"),
    ]
    db = next((c for c in candidates if c and os.path.exists(c)), None)
    if db is None:
        return None
    try:
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con, db
    except Exception:
        return None


def _session_dirs(run_dir):
    # The agent may have run in a working directory that was renamed to run_dir
    # afterwards (`workdir` in the record). Each as given and resolved.
    wd = rec.get("workdir")
    return sorted({d for d in (wd, wd and os.path.realpath(wd),
                               os.path.abspath(run_dir), os.path.realpath(run_dir))
                   if d})


def _db_tool_calls(run_dir):
    """(tool, input) of this run's session's tool calls, in order; None if unavailable."""
    opened = _open_db(run_dir)
    if opened is None:
        return None
    con, _ = opened
    try:
        dirs = _session_dirs(run_dir)
        sid = con.execute(
            "select id from session where directory in (%s) order by time_created "
            "desc limit 1" % ",".join("?" * len(dirs)), dirs).fetchone()
        if sid is None:
            return None
        out = []
        for row in con.execute("select data from part where session_id = ? "
                               "order by time_created", (sid[0],)):
            try:
                d = json.loads(row["data"])
            except Exception:
                continue
            if d.get("type") == "tool":
                inp = (d.get("state") or {}).get("input")
                if isinstance(inp, dict):
                    out.append((d.get("tool"), inp))
        return out
    except Exception:
        return None
    finally:
        con.close()


_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")


def _shell_lines(text):
    """The lines of a shell text that are commands: a here-document's body is data
    (`cat <<EOF` ... `EOF`), so those lines are left out."""
    out, terms = [], []
    for line in text.splitlines():
        if terms:
            if line.strip() == terms[0]:
                terms.pop(0)
            continue
        if line.strip():
            out.append(line)
        terms += [m.group(2) for m in _HEREDOC_RE.finditer(line)]
    return out


_tr_cmds = [l[2:] for l in plain.splitlines() if l.startswith("$ ")]
_db_calls = _db_tool_calls(run_dir)
_db_cmds = None if _db_calls is None else [
    inp["command"] for tool, inp in _db_calls
    if tool == "bash" and isinstance(inp.get("command"), str)]
if _db_cmds is not None:
    rec["bash_calls"] = len(_db_cmds)
    rec["commands_from"] = "opencode-db"
    _cmds = [l for c in _db_cmds for l in _shell_lines(c)]
else:
    rec["bash_calls"] = None
    rec["commands_from"] = "transcript"
    _cmds = list(_tr_cmds)
# Non-comment lines of the .sh and .py files the agent wrote, by extension.
_script_lines = {".sh": [], ".py": []}
for _root, _dirs, _files in os.walk(run_dir):
    # Skip the fetched dataset, scratch, opencode's state, the graded outputs, and
    # any cloned repository or dataset (it has .git or .datalad); those carry their
    # own scripts.
    # Installed packages and virtual environments are not the agent's scripts either.
    _dirs[:] = [d for d in _dirs
                if d not in ("data", "tmp", ".xdg-data", "submissions", "site-packages",
                             "node_modules", "__pycache__")
                and not os.path.exists(os.path.join(_root, d, ".git"))
                and not os.path.exists(os.path.join(_root, d, ".datalad"))
                and not os.path.exists(os.path.join(_root, d, "pyvenv.cfg"))]
    for _f in _files:
        _ext = os.path.splitext(_f)[1]
        if _ext in _script_lines:
            try:
                with open(os.path.join(_root, _f), encoding="utf-8", errors="replace") as fh:
                    _lines = fh.read().splitlines()
                if _ext == ".sh":
                    _lines = _shell_lines("\n".join(_lines))
                _script_lines[_ext] += [l for l in _lines
                                        if l.strip() and not l.lstrip().startswith("#")]
            except OSError:
                pass
_cmds += _script_lines[".sh"]

# Command position: start of a line, after a shell separator, after then/do/else,
# `{` or xargs, or inside the quoted argument of `bash -c` / `eval`. A quote
# anywhere else is an argument: `echo "sudo ..."` runs echo. Optional `VAR=x`,
# env, nohup, time, exec, command, nice, ionice and `timeout [flags] DURATION`
# in front.
_AT = (r"(?:^|[;&|(`]|\$\(|\b(?:then|do|else)\s|\{\s|(?:ba)?sh\s+-c\s+['\"]"
       r"|\beval\s+['\"]?|\bxargs\s+(?:-\S+\s+)*)\s*(?:\w+=\S*\s+)*"
       r"(?:(?:env|nohup|time|exec|command"
       r"|(?:nice|ionice)(?:\s+-\S+(?:\s+\d+)?)*"
       r"|timeout(?:\s+-\S+(?:\s+[\w.]+)?)*\s+\d+(?:\.\d+)?[smhd]?)\s+(?:\w+=\S*\s+)*)*")
_SUDO = r"(?:sudo(?:\s+-u\s+\S+)?(?:\s+-\S+)*\s+)?"
SUDO_RE = re.compile(_AT + r"sudo\b")
INSTALL_RE = re.compile(
    _AT + _SUDO + r"(?:\w+=\S*\s+)*"
    r"((?:apt-get|apt|dnf|yum|apk)\s+(?:-\S+\s+)*install\b[^;&|<>]*"
    r"|(?:\S*/)?(?:pip(?:3(?:\.\d+)?)?|uv\s+pip|python3?(?:\.\d+)?\s+-m\s+pip)"
    r"\s+install\b[^;&|<>]*"
    r"|pipx\s+install\b[^;&|<>]*"
    r"|(?:conda|mamba|micromamba)\s+(?:env\s+)?(?:install|create)\b[^;&|<>]*"
    r"|npm\s+(?:i|install)\b[^;&|<>]*)")
# An image from outside CVMFS: `docker pull`, `apptainer pull|build` from a remote
# URI, or `apptainer exec|run|shell` whose image argument (first positional after
# the flags) is a remote URI. A URL later on the line is a download inside the
# container, not an image.
_REMOTE = r"['\"]?(?:docker|oras|library|shub)://"
# docker run flags that take a value, so the value is not read as the image.
_DOCKER_VAL = r"(?:-v|--volume|-e|--env|-w|--workdir|--name|-u|--user|--entrypoint|--network|-p|--mount|--platform)"
IMAGE_RE = re.compile(
    _AT + _SUDO + r"(?:"
    r"(?:docker|podman)\s+pull\s+(?:-\S+\s+)*['\"]?([\w./:@+-]+)"
    r"|(?:docker|podman)\s+run\s+(?:" + _DOCKER_VAL + r"\s+\S+\s+|-\S+\s+)*['\"]?([\w./:@+-]+)"
    r"|(?:apptainer|singularity)\s+(?:pull|build)\b[^;&|]*?"
    r"(?:" + _REMOTE + r"|['\"]?https?://)([\w./:@+-]+)"
    r"|(?:apptainer|singularity)\s+(?:exec|run|shell)\s+"
    r"(?:-\S+(?:\s+(?!" + _REMOTE + r")[^-\s]\S*)?\s+)*" + _REMOTE + r"([\w./:@+-]+))")

# external-download: curl/wget, `git clone`, or `pip download`. git/pip are
# judged by VERB alone (the environment never hands out tools that way, so any
# use is suspect) and then cleared by _DATA_SRC; curl/wget additionally need a
# host/extension hit, since they are used for too many innocent things (health
# checks, polling a job's status) to flag on the verb alone.
_DATA_SRC = re.compile(r"openneuro|/datasets/", re.I)
_DOWNLOAD_HOST = re.compile(
    r"(?:[\w-]+\.)?github(?:usercontent)?\.com"
    r"|micro\.mamba\.pm|repo\.anaconda\.com|(?:conda\.)?anaconda\.org"
    r"|download\.pytorch\.org|files\.pythonhosted\.org"
    r"|huggingface\.co|hf\.co", re.I)
_DOWNLOAD_EXT = re.compile(r"\.(?:whl|pt|pth|ckpt|safetensors)(?:[?#'\"]|\s|$)", re.I)
DOWNLOAD_RE = re.compile(
    _AT + _SUDO + r"(?:\w+=\S*\s+)*((?:curl|wget)\s[^;&|<>]*)"
    r"|" + _AT + _SUDO + r"(?:\w+=\S*\s+)*(git\s+clone\s[^;&|<>]*)"
    r"|" + _AT + _SUDO + r"(?:\w+=\S*\s+)*"
    r"((?:(?:\S*/)?pip(?:3(?:\.\d+)?)?|uv\s+pip|python3?(?:\.\d+)?\s+-m\s+pip)"
    r"\s+download\b[^;&|<>]*)")


def _download_hits(cmds):
    """Evidence for external-download: matched text, cleared of data sources and
    (for curl/wget only) of hosts not on the software allowlist."""
    hits = []
    for c in cmds:
        for m in DOWNLOAD_RE.finditer(c):
            text = m.group(1) or m.group(2) or m.group(3)
            if _DATA_SRC.search(text):
                continue
            if m.group(1) and not (_DOWNLOAD_HOST.search(text) or _DOWNLOAD_EXT.search(text)):
                continue
            hits.append(re.sub(r"\s+", " ", text).strip()[:120])
    return sorted(set(hits))


# cvmfs-direct: apptainer/singularity exec|run of a path under /cvmfs/ -- the
# provided containers run BY PATH instead of through `module load`. In-spec (it
# is still the environment's own container) but a different route, so this
# feeds tool_routes only and is never added to off_spec.
CVMFS_RE = re.compile(
    _AT + _SUDO + r"(?:apptainer|singularity)\s+(?:exec|run)\s+"
    r"(?:-\S+(?:\s+(?!/cvmfs/)[^-\s]\S*)?\s+)*['\"]?/cvmfs/")
# pip vs conda/mamba/micromamba, read back out of installed_packages (below) so
# this can never disagree with what off_spec itself counted as a package-install.
_PIP_EVIDENCE = re.compile(
    r"^(?:\S*/)?pip(?:3(?:\.\d+)?)?\b|^uv\s+pip\b|^pipx\b"
    r"|^python3?(?:\.\d+)?\s+-m\s+pip\b", re.I)
_CONDA_EVIDENCE = re.compile(r"^(?:conda|mamba|micromamba)\b", re.I)

_cvmfs_direct = _pip_seen = _conda_seen = False
if not _tr_cmds and _db_cmds is None:
    # No command lines anywhere: unreadable, not clean. Scripts alone are not
    # enough, since most commands never reach a script.
    rec["off_spec"] = None
else:
    rec["used_sudo"] = sum(1 for c in _cmds if SUDO_RE.search(c))
    # The trailing fd of a redirect (`... 2>&1`) is not part of the install.
    rec["installed_packages"] = sorted(set(
        re.sub(r"\s+\d$", "", " ".join(m.group(1).split()))[:120]
        for c in _cmds for m in INSTALL_RE.finditer(c)))
    rec["external_images"] = sorted(set(
        next(g for g in m.groups() if g) for c in _cmds for m in IMAGE_RE.finditer(c)))
    rec["external_downloads"] = _download_hits(_cmds)
    _cvmfs_direct = any(CVMFS_RE.search(c) for c in _cmds)
    _pip_seen = any(_PIP_EVIDENCE.match(p) for p in rec["installed_packages"])
    _conda_seen = any(_CONDA_EVIDENCE.match(p) for p in rec["installed_packages"])
    rec["off_spec"] = (["sudo"] * bool(rec["used_sudo"])
                       + ["package-install"] * bool(rec["installed_packages"])
                       + ["external-image"] * bool(rec["external_images"])
                       + ["external-download"] * bool(rec["external_downloads"]))

# Modules loaded, from the same executed commands and scripts. A module named in
# the agent's text, a diff it printed, or a script comment was not loaded.
_MODLOAD = re.compile(_AT + r"(?:module\s+load|ml)\s+((?:[\w.+-]+/[\w.+-]+\s*)+)")
rec["tools_loaded"] = sorted({m for c in _cmds for g in _MODLOAD.findall(c) for m in g.split()})

# tool_routes: see the comment above off_spec. "pip"/"conda" mean the run used
# that manager to INSTALL something (off_spec already has it as package-install);
# a bare fetch with no manager is "download" instead, whether that was curl/wget,
# `git clone`, or `pip download`.
if rec["off_spec"] is None:
    rec["tool_routes"] = None
else:
    rec["tool_routes"] = sorted(set(
        (["lmod"] if rec["tools_loaded"] else [])
        + (["cvmfs-direct"] if _cvmfs_direct else [])
        + (["external-image"] if rec["external_images"] else [])
        + (["pip"] if _pip_seen else [])
        + (["conda"] if _conda_seen else [])
        + (["download"] if rec["external_downloads"] else [])))

# --- web use ------------------------------------------------------------------------
# Descriptive, never an exclusion: the environment leaves the internet open and the
# task's data comes over it. Fetches are the WebFetch tool's calls (the database when
# readable, else the transcript's `% WebFetch` lines); searches are the transcript's
# `◈` lines; hosts are every host named in an executed command, a fetch or a script
# the agent wrote, so a run that only fetched its data still names its data host.
_URL_HOST = re.compile(r"https?://([\w.-]+)")
_tr_fetches = [l[len("% WebFetch "):] for l in plain.splitlines() if l.startswith("% WebFetch ")]
_db_fetches = None if _db_calls is None else [
    inp["url"] for tool, inp in _db_calls if tool == "webfetch" and isinstance(inp.get("url"), str)]
if not txt and _db_calls is None:
    rec["web_fetches"] = rec["web_searches"] = rec["web_hosts"] = None
else:
    _fetches = _db_fetches if _db_fetches is not None else _tr_fetches
    rec["web_fetches"] = len(_fetches)
    rec["web_searches"] = sum(1 for l in plain.splitlines() if l.startswith("◈ "))
    rec["web_hosts"] = sorted({m.group(1).lower()
                               for text in _cmds + _fetches + _script_lines[".sh"] + _script_lines[".py"]
                               for m in _URL_HOST.finditer(text)})

rec["dataset_pin"] = sorted(set(
    re.findall(r"checkout\s+([0-9]+\.[0-9]+(?:\.[0-9]+)?)", txt)))
# --- skill loads -----------------------------------------------------------------
# opencode prints one status line per skill tool call: `→ Skill "<name>"` on
# success, `✗ Skill "<name>" failed` on failure. A failure is followed by
# `Error: Skill "<name>" not found. Available skills: a, b`, which is read only for
# the list of skills opencode offered. Any other line naming a skill is text.
_loads, _fails, _avail = [], [], None
for _l in plain.splitlines():
    _a = re.match(r"Error: .*Available skills:\s*(.*)", _l)
    if _a:
        _avail = (_avail or set()) | {s.strip(" .") for s in _a.group(1).split(",")
                                      if s.strip(" .")}
    _m = re.match(r'([→✗])\s*Skill "([^"]+)"', _l)
    if _m:
        (_loads if _m.group(1) == "→" else _fails).append(_m.group(2))
rec["skill_loads"] = len(_loads)
rec["skills_seen"] = sorted(set(_loads))
rec["skill_load_failures"] = len(_fails)
# None when opencode never printed its list, which it does only on a failed call.
rec["skills_available"] = sorted(_avail) if _avail is not None else None

# Skill files opened directly (the Read tool, or a command that prints a file)
# rather than through the skill tool. Each pipeline segment is judged on its own:
# in `find / -name SKILL.md | head` the path belongs to find, which only lists.
# Paths must contain a directory; `cat SKILL.md` after a `cd` is not detected.
_READ_CMD = re.compile(r"\s*(?:sudo\s+(?:-\S+\s+)*)?(?:cat|head|tail|less|more|sed|awk|grep|bat|nl"
                       r"|git\s+(?:-C\s+\S+\s+)?(?:show|cat-file))\b")
_SKILL_FILE = re.compile(r"[\w./~-]*(?:/SKILL\.md|/references/[\w.-]+\.md)")
_reads = set()
for _l in plain.splitlines():
    if _l.startswith("→ Read "):
        _reads.update(_SKILL_FILE.findall(_l))
    elif _l.startswith("$ "):
        for _seg in re.split(r"\|\|?|;|&&", _l[2:]):
            if _READ_CMD.match(_seg):
                _reads.update(_SKILL_FILE.findall(_seg))
rec["skill_files_read"] = sorted(_reads)

# --- answer key --------------------------------------------------------------------
# Grading material and where it is published. None of it is on disk during a CI run;
# all of it is reachable over the network.
#   files    tasks.json (solution blocks), rubric.json, a task's PROVENANCE.md or
#            README.md in a graders/ or benchmark/ tree, anything under graders/,
#            CLAIMS.md, INFRA_LEDGER.md, the grading scripts
#   sources  the skills-comm and skills-benchmark repositories and site, and the
#            reference dataset on Hugging Face (gated: files need a logged-in account)
# Files count when read: the Read and Grep tools, or a command segment that is not
# only listing or searching (find, ls, ...). Sources count when fetched or searched
# for: WebFetch, web search, or any executed command. Scripts the agent wrote are
# read for both. Each hit is recorded as the line it was found on.
# A path segment named graders/ or benchmark/ must be the whole segment, so a path
# through a directory named skills-benchmark does not match. A bare repository id
# (owner/skills-comm) must not itself be a path segment.
_KEY_FILE = re.compile(
    r"(?:^|[\s/'\"=:(])(?:tasks\.json|rubric\.json|CLAIMS\.md|INFRA_LEDGER\.md"
    r"|grade_wrapper\.py|fetch_reference\.py|run_manifest\.json)\b"
    r"|(?<![\w.-])(?:graders|benchmark)/\S*(?:PROVENANCE|README)\.md"
    r"|(?<![\w.-])graders/\S")
# The OSF projects that hold reference outputs (the task pack's datasets_catalog names
# them as ground-truth sources: zjqey main, y2dq4 MRtrix diffusion, q7v8c QSIPrep)
# count as sources too. ru43c holds a task's DATA on OSF and is not one of them.
_OSF_REFS = r"(?<![\w])(?:zjqey|y2dq4|q7v8c)(?![\w])"
_KEY_SRC = re.compile(
    r"(?:github\.com|githubusercontent\.com|github\.io|huggingface\.co|hf\.co)"
    r"[^\s'\"]*skills[-_](?:comm|benchmark)"
    r"|skills-comm-ground-truth|(?<![/\w.-])[\w.-]+/skills-(?:comm|benchmark)\b(?!/)"
    r"|" + _OSF_REFS, re.I)
_KEY_QUERY = re.compile(r"skills[-_ ]?(?:comm|benchmark)|" + _OSF_REFS, re.I)
_LISTS = ("find", "ls", "locate", "which", "echo", "printf", "mkdir", "cd", "stat")


def _key_hits():
    hits = []
    for l in plain.splitlines():
        if l.startswith(("→ Read ", "✱ Grep ")):
            if _KEY_FILE.search(l):
                hits.append(l)
        elif l.startswith("% WebFetch "):
            if _KEY_SRC.search(l):
                hits.append(l)
        elif l.startswith("◈ "):
            if _KEY_QUERY.search(l):
                hits.append(l)
        elif l.startswith("$ "):
            if _KEY_SRC.search(l):
                hits.append(l)
                continue
            for seg in re.split(r"\|\|?|;|&&", l[2:]):
                words = seg.split()
                if words and words[0] not in _LISTS and _KEY_FILE.search(seg):
                    hits.append(l)
                    break
    for l in _script_lines[".sh"] + _script_lines[".py"]:
        if _KEY_SRC.search(l) or _KEY_FILE.search(l):
            hits.append("script: " + l.strip())
    return sorted({h[:160] for h in hits})


rec["answer_key_reads"] = _key_hits() if txt else None

# --- the arm label seen ---------------------------------------------------------------
# The prompt never names the arm, and the agent's working directory does not either.
# A transcript line containing an arm label (env-only, env+skill...) means the agent
# came across it: in the process list, the environment, the runner's event file, or
# the harness's own files. Recorded as those lines; [] is clean.
_ARM_RE = re.compile(r"(?<![\w.])env[-+](?:only|skill)")
rec["arm_seen"] = ([l[:160] for l in plain.splitlines() if _ARM_RE.search(l)][:10]
                   if txt else None)

# --- opencode's own errors ----------------------------------------------------------
# opencode prints its errors as a bold red `Error: `. One that follows a `✗ ... failed`
# line belongs to a tool call the agent made; the rest are opencode's own (the
# gateway, the session, the runtime). Read from the raw transcript: the colour code
# is what separates them from `Error:` lines in command output. Stored without the
# `Error: ` prefix.
_OC_ERR = "\x1b[91m\x1b[1mError: "
_errs, _prev = [], ""
for _raw in txt.splitlines():
    _p = ANSI.sub("", _raw).strip()
    if _raw.startswith(_OC_ERR) and not _prev.startswith("✗"):
        _errs.append(_p[len("Error: "):][:200])
    if _p:
        _prev = _p
rec["opencode_errors"] = _errs if txt else None
rec["transcript_lines"] = txt.count("\n")

# --- token accounting from opencode's own database ---------------------------
# opencode records per-session token totals in SQLite, keyed by the working
# directory run_bench.sh passed as --dir: the run directory for older runs, the
# recorded `workdir` for runs renamed afterwards. Read-only and best-effort: a
# locked or missing DB must never cost us the rest of the provenance record.
#
# `cost` is 0.0 on this gateway (self-hosted vLLM with no pricing configured), so
# tokens are the currency, not dollars.
def _session_row(run_dir):
    # With OPENCODE_ISOLATE=1 each run has its own database under the run
    # directory; otherwise every run shares the one in $HOME. Prefer the local
    # copy so isolated runs still get token accounting.
    opened = _open_db(run_dir)
    if opened is None:
        return None
    con, _ = opened
    try:
        # A directory can be reused across sweeps, so take the newest session.
        dirs = _session_dirs(run_dir)
        cur = con.execute(
            "select * from session where directory in (%s) order by time_created desc "
            "limit 1" % ",".join("?" * len(dirs)), dirs)
        row = cur.fetchone()
        con.close()
        return row
    except Exception:
        return None


def _generation_speed(run_dir):
    """(output tokens per second of generation, generation seconds) over the run's
    assistant messages, from the database's per-message timings; None without it."""
    opened = _open_db(run_dir)
    if opened is None:
        return None
    con, _ = opened
    tokens = secs = 0.0
    try:
        for (data,) in con.execute("select data from message"):
            m = json.loads(data)
            t = m.get("time") or {}
            if m.get("role") == "assistant" and t.get("created") and t.get("completed"):
                secs += (t["completed"] - t["created"]) / 1000
                tokens += (m.get("tokens") or {}).get("output") or 0
    except Exception:
        return None
    finally:
        con.close()
    return (round(tokens / secs, 2), int(secs)) if secs > 0 else None


srow = _session_row(run_dir)
if srow is not None:
    keys = srow.keys()
    for k in ("tokens_input", "tokens_output", "tokens_reasoning",
              "tokens_cache_read", "tokens_cache_write", "cost"):
        if k in keys and srow[k] is not None:
            rec[k] = srow[k]
    if "id" in keys:
        rec["session_id"] = srow["id"]
    if "model" in keys and srow["model"]:
        try:
            rec["session_model"] = json.loads(srow["model"]).get("id")
        except Exception:
            rec["session_model"] = str(srow["model"])
    # opencode's own millisecond timestamps beat our transcript-mtime estimate.
    if {"time_created", "time_updated"} <= set(keys):
        try:
            rec["session_duration_s"] = int(
                (srow["time_updated"] - srow["time_created"]) / 1000)
        except Exception:
            pass
    rec["tokens_total"] = sum(
        rec.get(k, 0) or 0 for k in ("tokens_input", "tokens_output"))
_speed = _generation_speed(run_dir)
rec["tokens_output_per_s"], rec["generation_s"] = _speed if _speed else (None, None)

task = rec.get("task_id", "")
out = os.path.join(run_dir, "submissions", task, "output.nii.gz")
rec["output_present"] = os.path.exists(out)

# Time to a usable result, which is not the same as run duration. A run that
# produced its mask in 5 minutes and then spent 40 more trying to repair a batch
# scheduler took 45 minutes and was useful after 5. Total runtime reports that as
# slow; this reports it as fast-then-distracted, which is what actually happened
# and what a user would feel.
rec["seconds_to_output"] = None
if rec["output_present"] and rec.get("start"):
    try:
        t0 = datetime.strptime(rec["start"], "%Y-%m-%dT%H:%M:%SZ")
        t0 = t0.replace(tzinfo=timezone.utc).timestamp()
        rec["seconds_to_output"] = max(0, int(os.path.getmtime(out) - t0))
    except Exception:
        pass
if rc is not None:
    try:
        rec["exit_code"] = int(rc)
    except ValueError:
        pass
# What became of the agent, as one word a dispatcher can read without the log. The
# job's own conclusion cannot carry it: an agent the gateway cut off exits non-zero
# and the job still ends "success". summarize.py applies the wave's rules to the
# same facts; this field only names the case.
#   completed      the agent stopped on its own (its output may still fail grading)
#   timeout        stopped at the wall (exit 124)
#   gateway_cut    an opencode error the harness classes as infrastructure, no output
#   no_model_call  no output and not one token spent (the database reported the tokens)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from summarize import INFRA_ERROR_RES, RUN_TIMEOUT_EXIT  # noqa: E402
_infra = "\n".join("Error: " + m for m in rec.get("opencode_errors") or [])
if rec.get("exit_code") == RUN_TIMEOUT_EXIT:
    rec["agent_outcome"] = "timeout"
elif not rec["output_present"] and any(rx.search(_infra) for rx, _ in INFRA_ERROR_RES):
    rec["agent_outcome"] = "gateway_cut"
elif not rec["output_present"] and "tokens_input" in rec and not rec.get("tokens_total"):
    rec["agent_outcome"] = "no_model_call"      # only when the database reported the tokens
else:
    rec["agent_outcome"] = "completed"
# The transcript is opencode's stdout redirect, so its last write is the moment the
# agent stopped. Use that rather than "now": this script is re-run during collection
# to backfill fields, and stamping now() there measured time-from-start-to-GRADING,
# not run duration. That produced runs "lasting" 12 hours inside a 4.5 hour sweep,
# and -- because the arms are graded in order -- a fake 25% speedup for the second
# arm. Deriving it from the file makes the value idempotent and repairs old runs.
if os.path.exists(tr):
    rec["end"] = datetime.fromtimestamp(
        os.path.getmtime(tr), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
else:
    rec.setdefault("end", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

with open(rj, "w", encoding="utf-8") as fh:
    json.dump(rec, fh, indent=None)
    fh.write("\n")
