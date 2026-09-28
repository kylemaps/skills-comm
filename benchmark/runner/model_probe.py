#!/usr/bin/env python3
"""Record the gateway's roster and what it serves for a model into a run record.

    model_probe.py --model neurodesk/qwen3 --record run.json [--gateway URL] [--phase end]
    model_probe.py --model m --record r.json --offline ROSTER.json RESPONSE.json HEADERS.json

GET <gateway>/models gives the roster. One minimal chat completion to the model
gives what the gateway reports serving. Written into the record:

  gateway_roster     sorted model ids the gateway offers
  model_served       the chat response's `model` field
  model_fingerprint  first 12 hex chars of sha256 over model_served,
                     system_fingerprint and the identity headers
  model_probe        the values the fingerprint was computed from; header values
                     naming a host, URL or API base are stored as hashes. `routing`
                     holds the gateway's fallback and retry headers for the probe
                     call (not part of the fingerprint)

Identity headers are response headers whose name contains "model", "version" or
"fingerprint", excluding per-request values (ids, durations, costs, keys, limits).
On any failure the fields are null and the exit code is 0: the run's own provider
check decides whether the model is usable.

--phase end, run after the agent, writes the same fields with an `_end` suffix and
`model_changed`: true if the fingerprint differs from the one recorded before the
agent started, null if either is missing.

The gateway defaults to $NEURODESK_GATEWAY or https://llm.neurodesk.org/openai; the
key is $NEURODESK_API_KEY. $BENCH_SESSION_ID, if set, is sent as
x-litellm-session-id, as the agent's calls do.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request

IDENTITY = ("model", "version", "fingerprint")
PER_REQUEST = ("call-id", "request-id", "duration", "cost", "spend", "key",
               "ratelimit", "limit", "remaining", "latency", "timing", "trace")
HASHED = ("api-base", "api_base", "url", "host")
ROUTING = ("fallback", "retries")


def h(s, n=12):
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def identity_headers(headers):
    out = {}
    for k, v in headers.items():
        name = k.lower()
        if not any(x in name for x in IDENTITY) or any(x in name for x in PER_REQUEST):
            continue
        out[name] = ("sha256:" + h(str(v))) if any(x in name for x in HASHED) else str(v)
    return dict(sorted(out.items()))


def summarize(roster_json, response_json, headers):
    roster = None
    if isinstance(roster_json, dict) and isinstance(roster_json.get("data"), list):
        roster = sorted(m.get("id", "") for m in roster_json["data"] if isinstance(m, dict))
    served = fp_raw = None
    probe = None
    if isinstance(response_json, dict):
        served = response_json.get("model")
        fp_raw = response_json.get("system_fingerprint")
        probe = {"model": served, "system_fingerprint": fp_raw,
                 "headers": identity_headers(headers or {})}
    fingerprint = None
    if probe is not None and served:
        fingerprint = h(json.dumps(probe, sort_keys=True))
    if probe is not None:
        probe["routing"] = dict(sorted(
            (k.lower(), str(v)) for k, v in (headers or {}).items()
            if any(x in k.lower() for x in ROUTING)))
    return {"gateway_roster": roster, "model_served": served,
            "model_fingerprint": fingerprint, "model_probe": probe}


def fetch(url, key, body=None, timeout=60):
    hdrs = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if os.environ.get("BENCH_SESSION_ID"):
        hdrs["x-litellm-session-id"] = os.environ["BENCH_SESSION_ID"]
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()), dict(r.headers.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--record", required=True)
    ap.add_argument("--gateway", default=os.environ.get(
        "NEURODESK_GATEWAY", "https://llm.neurodesk.org/openai"))
    ap.add_argument("--offline", nargs=3, metavar=("ROSTER", "RESPONSE", "HEADERS"))
    ap.add_argument("--phase", choices=("start", "end"), default="start")
    a = ap.parse_args()
    model = a.model.split("/", 1)[-1]

    roster_json = response_json = headers = None
    if a.offline:
        loaded = []
        for p in a.offline:
            try:
                loaded.append(json.load(open(p, encoding="utf-8")))
            except (OSError, ValueError):
                loaded.append(None)
        roster_json, response_json, headers = loaded
    else:
        key = os.environ.get("NEURODESK_API_KEY", "")
        base = a.gateway.rstrip("/")
        try:
            roster_json, _ = fetch(base + "/models", key)
        except Exception as e:
            print("model_probe: roster unavailable (%s)" % e)
        try:
            response_json, headers = fetch(base + "/chat/completions", key, {
                "model": model, "max_tokens": 8, "temperature": 0,
                "messages": [{"role": "user", "content": "Reply with the word ok."}]})
        except Exception as e:
            print("model_probe: chat probe failed (%s)" % e)

    out = summarize(roster_json, response_json, headers)
    try:
        rec = json.load(open(a.record, encoding="utf-8"))
        if a.phase == "end":
            before, after = rec.get("model_fingerprint"), out["model_fingerprint"]
            out = {k + "_end": v for k, v in out.items()}
            out["model_changed"] = (before != after) if before and after else None
        rec.update(out)
        with open(a.record, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
            fh.write("\n")
    except (OSError, ValueError) as e:
        print("model_probe: could not update %s (%s)" % (a.record, e))
    print(" ".join("%s=%s" % (k, v) for k, v in sorted(out.items())
                   if not k.startswith("model_probe")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
