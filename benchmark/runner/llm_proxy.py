#!/usr/bin/env python3
"""The run's side of the key proxy: lock the run's model, read back what was called.

    llm_proxy.py lock --model MODEL_ID [--model MODEL_ID ...] --record RUN_JSON
    llm_proxy.py summary --record RUN_JSON
    llm_proxy.py upstream --record RUN_JSON

With the key proxy, the provider keys live only in a sidecar container on the pod; the
job reaches each provider through it (LLM_PROXY_<PROVIDER>_BASE_URL) with a placeholder
key, and the proxy adds the real one. Its control endpoint (LLM_PROXY_CONTROL_URL) takes
one lock per job: after it, the proxy answers chat calls for the locked model only. A
pod without LLM_PROXY_CONTROL_URL has no proxy, and both commands do nothing.

lock      waits for the proxy (/healthz), locks the model ids opencode sends (each
          --model without its provider prefix; a run locks one, preflight every model
          it probes) and writes the answer into the record as llm_proxy_lock. Anything
          but a 200 whose `locked` is exactly those ids exits 1 and the run stops
          before the agent: a proxy that is down or has
          no key for the provider (/healthz 503), a lock this job already holds or a
          proxy sealed by a chat call before any lock (409). Never a direct call.
summary   writes the proxy's per-job summary into the record as llm_proxy: the lock,
          its mode, chat calls per provider/model, mismatches, denied calls, calls
          before the lock, relock attempts, whether it was sealed. On failure
          llm_proxy holds the error and the exit is 1: without the summary the run
          cannot show which models it called, and it is not valid. So is a summary
          from an impostor: the proxy runs in its own container, so a process in
          this one that holds the control port (found from /proc/net/tcp and the
          processes' socket fds) means the proxy died and something else answered.
upstream  after the model probe: exit 1 when the probe's chat call through the proxy
          failed in a way that is the pod's, not the model's: a key the provider
          rejects (401, as during a key swap), a refusal (403), a rate limit (429), a
          5xx, or no answer at all. A 400 or 404 (the probe or the model id is wrong)
          is left to the fingerprint check, which stops for a human.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_ids import opencode_id  # noqa: E402


def call(url, body=None, timeout=10):
    """(status, parsed JSON or text) of one request; status 0 when unreachable."""
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception as e:
        return 0, str(e)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")[:200]


def port_owner(port, proc="/proc"):
    """The pid of a process in this container that holds a listening socket on port,
    or None. The proxy's own process is in another container, invisible here."""
    inodes = set()
    for table in ("net/tcp", "net/tcp6"):
        try:
            lines = open(os.path.join(proc, table), encoding="utf-8").read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) > 9 and f[3] == "0A" and f[1].rsplit(":", 1)[-1].upper() == "%04X" % port:
                inodes.add("socket:[%s]" % f[9])
    if not inodes:
        return None
    for d in os.listdir(proc):
        if not d.isdigit():
            continue
        fds = os.path.join(proc, d, "fd")
        try:
            names = os.listdir(fds)
        except OSError:
            continue
        for n in names:
            p = os.path.join(fds, n)
            try:
                target = os.readlink(p)
            except OSError:      # a test fixture without links holds the target as text
                try:
                    target = open(p, encoding="utf-8").read().strip() if os.path.isfile(p) else ""
                except OSError:
                    target = ""
            if target in inodes:
                return int(d)
    return None


def update(record, **fields):
    rec = json.load(open(record, encoding="utf-8"))
    rec.update(fields)
    with open(record, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
        fh.write("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("lock", "summary", "upstream"))
    ap.add_argument("--model", action="append", default=[])
    ap.add_argument("--record", required=True)
    ap.add_argument("--tries", type=int, default=10)
    ap.add_argument("--proc", default="/proc")
    a = ap.parse_args()
    control = os.environ.get("LLM_PROXY_CONTROL_URL", "").rstrip("/")
    if not control:
        return 0

    if a.command == "upstream":
        err = str((json.load(open(a.record, encoding="utf-8")).get("model_probe_error") or ""))
        if not err:
            return 0
        if err.startswith("HTTP "):
            code = int(err.split()[1]) if err.split()[1].isdigit() else 0
            return 1 if code in (401, 403, 429) or code >= 500 else 0
        return 1                        # no HTTP answer at all after a lock that worked

    if a.command == "summary":
        port = urllib.parse.urlsplit(control).port or 80
        owner = port_owner(port, a.proc)
        if owner is not None:
            update(a.record, llm_proxy={"error": "impostor: a process in the job's container "
                                                 "holds the proxy's port", "port": port})
            return 1
        status, body = call(control + "/summary")
        ok = status == 200 and isinstance(body, dict)
        update(a.record, llm_proxy=body if ok else {"error": "summary unavailable", "status": status})
        return 0 if ok else 1

    if not a.model:
        sys.exit("llm_proxy.py lock needs --model")
    for i in range(a.tries):
        status, body = call(control + "/healthz", timeout=5)
        if status == 200:
            break
        if status == 503 and isinstance(body, dict) and body.get("error"):
            break                       # up, and says what is missing: no use waiting
        time.sleep(1)
    if status != 200:
        update(a.record, llm_proxy_lock={"error": "proxy not ready", "status": status,
                                         "answer": body})
        print("llm_proxy: the key proxy at %s is not ready (HTTP %s)" % (control, status),
              file=sys.stderr)
        return 1
    models = [opencode_id(m).split("/", 1)[1] for m in a.model]
    status, body = call(control + "/lock", {"models": models})
    if status == 200 and isinstance(body, dict) and body.get("locked") == models:
        update(a.record, llm_proxy_lock=body)
        return 0
    update(a.record, llm_proxy_lock={"error": "lock refused", "status": status, "answer": body})
    print("llm_proxy: lock of %s refused (HTTP %s)" % (", ".join(models), status), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
