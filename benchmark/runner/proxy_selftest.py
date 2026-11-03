#!/usr/bin/env python3
"""The key proxy refuses what it should: run from a pod of the pool, no agent.

    proxy_selftest.py --case seal      # a fresh proxy: a chat before any lock seals it
    proxy_selftest.py --case locked    # a fresh proxy: lock, then every refusal

The proxy's lock is one-shot, so the two cases need two fresh pods (preflight.yml,
proxy_tests). Each check prints what it sent and what came back; the exit is 1 when
any expectation misses, 2 when the pod has no proxy (LLM_PROXY_CONTROL_URL and the
provider base URLs unset).

seal    a chat call before any lock is refused (403) and seals the proxy: a later lock
        is refused (409, locked to nothing), and the locked model is refused after it.
locked  the run's lock (provider/model ids); a model query string is dropped, so the
        locked model answers whatever ?model= says (one 8-token call per provider);
        routing fields (fallbacks, api_base) are refused; another model is refused,
        and so is the locked model on the other provider's port; only the two model
        endpoints pass; a second lock is refused; after /end every chat is refused;
        the summary counts exactly that. Then, refused before any call leaves the pod:
        a body declared over the size limit (a chat call over 8 MB, a lock over 4 KB),
        and a burst of chat calls past the rate limit (some 429, no 5xx).

Only the proxy's own error messages are printed: a provider's error body can carry
a digest of the key.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

LOCKED_NRP, LOCKED_EGI, OTHER = "deepseek-v4-flash", "kimi-k3", "qwen3-vl"
PROBE = [{"role": "user", "content": "Reply with the word ok."}]


def call(url, body=None, timeout=120):
    """(status, parsed JSON or None) of one request; status 0 when unreachable."""
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer llm-proxy"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception:
        return 0, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def chat(base, model, query="", extra=None, retry_5xx=False):
    body = {"model": model, "messages": PROBE, "max_tokens": 8, "temperature": 0}
    body.update(extra or {})
    for attempt in range(3 if retry_5xx else 1):
        status, ans = call(base.rstrip("/") + "/chat/completions" + query, body)
        if not (retry_5xx and (status == 0 or status >= 500)):
            break
        time.sleep(5)
    return status, ans


def declared(url, length):
    """The status for a POST that declares `length` bytes and sends none: a body over
    the server's limit is answered from the headers alone. 0 when nothing came back."""
    u = urllib.parse.urlsplit(url)
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=10) as s:
            s.sendall(("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
                       "Content-Length: %d\r\nConnection: close\r\n\r\n"
                       % (u.path, u.netloc, length)).encode())
            line = s.recv(64).split(b"\r\n", 1)[0].split()
    except OSError:
        return 0
    return int(line[1]) if len(line) > 1 and line[1].isdigit() else 0


def kind(ans):
    """The error type and, for the proxy's own errors only, its message."""
    err = (ans or {}).get("error") if isinstance(ans, dict) else None
    if not isinstance(err, dict):
        return ""
    t = str(err.get("type") or "")
    return t + (": " + str(err.get("message"))[:90] if t.startswith("llm_proxy") else "")


class Checks:
    def __init__(self):
        self.failed = 0

    def expect(self, name, ok, got):
        print("%s %s: %s" % ("ok  " if ok else "FAIL", name, got))
        self.failed += not ok

    def refused(self, name, status, ans):
        t = kind(ans)
        self.expect(name, status == 403 and t.startswith("llm_proxy_denied"),
                    "HTTP %s %s" % (status, t))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", choices=("seal", "locked"), required=True)
    a = ap.parse_args()
    control = os.environ.get("LLM_PROXY_CONTROL_URL", "").rstrip("/")
    nrp = os.environ.get("LLM_PROXY_NRP_BASE_URL", "")
    egi = os.environ.get("LLM_PROXY_EGI_BASE_URL", "")
    if not (control and nrp and egi):
        print("FAIL: this pod has no key proxy (LLM_PROXY_CONTROL_URL and the provider base URLs)")
        return 2
    c = Checks()
    status, ans = call(control + "/healthz")
    c.expect("the proxy is up and holds both keys", status == 200, "HTTP %s" % status)

    if a.case == "seal":
        status, ans = chat(nrp, LOCKED_NRP)
        c.refused("a chat before any lock is refused", status, ans)
        status, ans = call(control + "/lock", {"models": ["nrp/" + LOCKED_NRP]})
        models = ans.get("models") if isinstance(ans, dict) else None
        c.expect("  and seals the proxy: a lock after it is refused, locked to nothing",
                 status == 409 and models == [], "HTTP %s models %s" % (status, models))
        status, ans = chat(nrp, LOCKED_NRP)
        c.refused("  and the model is still refused", status, ans)
        status, s = call(control + "/summary")
        s = s if isinstance(s, dict) else {}
        # The first chat is a call before the lock (and seals it); the second is a
        # model the empty lock does not hold.
        want = {"sealed_without_lock": True, "locked": [], "unlocked_calls": 1, "denied": 2,
                "mismatches": 1}
        off = {k: s.get(k) for k, v in want.items() if s.get(k) != v}
        c.expect("the summary shows the seal", status == 200 and not off,
                 "sealed_without_lock %s, locked %s, unlocked_calls %s, denied %s, mismatches %s%s"
                 % (s.get("sealed_without_lock"), s.get("locked"), s.get("unlocked_calls"),
                    s.get("denied"), s.get("mismatches"), "; off: %s" % off if off else ""))
        return 1 if c.failed else 0

    lock = ["nrp/" + LOCKED_NRP, "egi/" + LOCKED_EGI]
    status, ans = call(control + "/lock", {"models": lock})
    c.expect("the lock is taken, as provider/model ids",
             status == 200 and isinstance(ans, dict) and ans.get("locked") == lock,
             "HTTP %s locked %s" % (status, (ans or {}).get("locked") if isinstance(ans, dict) else None))
    status, ans = chat(nrp, LOCKED_NRP, query="?model=" + OTHER, retry_5xx=True)
    served = str((ans or {}).get("model") or "") if isinstance(ans, dict) else ""
    c.expect("NRP: ?model=%s is dropped, the locked model answers" % OTHER,
             status == 200 and "deepseek" in served.lower() and "qwen" not in served.lower(),
             "HTTP %s served %s" % (status, served or "-"))
    status, ans = chat(egi, LOCKED_EGI, query="?model=deepseek", retry_5xx=True)
    served = str((ans or {}).get("model") or "") if isinstance(ans, dict) else ""
    c.expect("EGI: ?model=deepseek is dropped, the locked model answers",
             status == 200 and "kimi" in served.lower(),
             "HTTP %s served %s" % (status, served or "-"))
    status, ans = chat(nrp, LOCKED_NRP, extra={"fallbacks": [OTHER]})
    c.refused("a fallbacks field is refused", status, ans)
    status, ans = chat(nrp, LOCKED_NRP, extra={"api_base": "https://example.invalid/v1"})
    c.refused("an api_base field is refused", status, ans)
    status, ans = chat(nrp, OTHER)
    c.refused("another model (%s) is refused" % OTHER, status, ans)
    status, ans = chat(egi, LOCKED_NRP)
    c.refused("the locked NRP model on the EGI port is refused (the lock is per provider)", status, ans)
    status, ans = call(nrp.rstrip("/") + "/embeddings", {"model": LOCKED_NRP, "input": "x"})
    c.expect("an endpoint other than models and chat is not served", status == 404,
             "HTTP %s" % status)
    status, ans = call(control + "/lock", {"models": ["nrp/" + OTHER]})
    c.expect("a second lock is refused", status == 409, "HTTP %s" % status)
    status, ans = call(control + "/end", {})
    c.expect("the end of the job's calls is confirmed",
             status == 200 and isinstance(ans, dict) and ans.get("ended") is True, "HTTP %s" % status)
    status, ans = call(control + "/end", {})
    c.expect("  and ending twice is fine", status == 200, "HTTP %s" % status)
    status, ans = chat(nrp, LOCKED_NRP)
    c.refused("after the end the locked model is refused too", status, ans)
    status, s = call(control + "/summary")
    s = s if isinstance(s, dict) else {}
    # Refused: two fields and two models (4); the two models (2); one second lock; one
    # chat after the end, which counts there and nowhere else.
    want = {"locked": lock, "ended": True, "sealed_without_lock": False, "unlocked_calls": 0,
            "denied": 4, "mismatches": 2, "relock_attempts": 1, "calls_after_end": 1}
    off = {k: s.get(k) for k, v in want.items() if s.get(k) != v}
    c.expect("the summary counts all of it", status == 200 and not off,
             "calls %s, denied %s, mismatches %s, relocks %s, after end %s%s"
             % (s.get("calls"), s.get("denied"), s.get("mismatches"), s.get("relock_attempts"),
                s.get("calls_after_end"), "; off: %s" % off if off else ""))

    # After the summary: these are counted nowhere, or would change its counts.
    status = declared(nrp.rstrip("/") + "/chat/completions", 9 * 2**20)
    c.expect("a chat body over 8 MB is refused before it is read", status == 413, "HTTP %s" % status)
    status = declared(control + "/lock", 5 * 2**10)
    c.expect("a lock body over 4 KB is refused before it is read", status == 413, "HTTP %s" % status)
    body = {"model": LOCKED_NRP, "messages": PROBE, "fallbacks": [OTHER]}
    url = nrp.rstrip("/") + "/chat/completions"
    with ThreadPoolExecutor(max_workers=6) as ex:     # under the 8 connections a port allows
        codes = list(ex.map(lambda _: call(url, body, timeout=30)[0], range(40)))
    seen = {k: codes.count(k) for k in sorted(set(codes))}
    c.expect("a burst of 40 chat calls is rate-limited (some 429), with no 5xx",
             seen.get(429, 0) > 0 and not any(k == 0 or k >= 500 for k in seen),
             "statuses %s" % seen)
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
