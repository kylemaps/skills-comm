#!/usr/bin/env python3
"""Record the gateway's roster and what it serves for a model into a run record.

    model_probe.py --model neurodesk/qwen3 --record run.json [--gateway URL] [--phase end]
    model_probe.py --model m --record r.json --offline ROSTER.json RESPONSE.json HEADERS.json

GET <gateway>/models gives the roster. One minimal chat completion to the model
gives what the gateway reports serving. Written into the record:

  provider           the model id's provider prefix (before the first '/'), or
                     null when it has none
  gateway_roster     sorted model ids the gateway offers
  model_served       the chat response's `model` field
  model_fingerprint  first 12 hex chars of sha256 over model_served,
                     system_fingerprint and the identity headers
  model_fingerprint_strength  "strong" when an identity value beyond the model id
                     itself was present (system_fingerprint or an identity header);
                     "weak" when only the model id was. A declared fingerprint gates
                     the run the same way regardless of strength -- this only says
                     how much the match is worth.
  model_probe        the values the fingerprint was computed from; header values
                     naming a host, URL or API base are stored as hashes. `basis`
                     lists which identity fields were present (not part of the
                     fingerprint); `routing` holds the gateway's fallback and retry
                     headers for the probe call, and `proxy` the proxy headers
                     below (neither part of the fingerprint)

Identity headers are response headers whose name contains "model", "version" or
"fingerprint", excluding per-request values (ids, durations, costs, keys, limits)
and proxy headers: a LiteLLM proxy's own software version and the alias the request
named (`*-litellm-version`, `*-model-group`). Neither says which model or engine
answered, so a proxy upgrade is not read as a model change. The deployment's id,
model name and API base, at every proxy layer, stay in.
On any failure the fields are null and the exit code is 0: the run's own provider
check decides whether the model is usable.

--phase end, run after the agent, writes the same fields with an `_end` suffix and
`model_changed`: true if the fingerprint differs from the one recorded before the
agent started, null if either is missing.

--providers FILE: sweep.json, to resolve the gateway URL and the key's environment
variable from the model id's provider prefix (sweep.json's top-level `providers`).
Without it, for a model id with no prefix, or for the `neurodesk` prefix itself,
falls back to $NEURODESK_GATEWAY / https://llm.neurodesk.org/openai and
$NEURODESK_API_KEY -- today's one gateway, which keeps its own env override whether
or not sweep.json also declares it. $BENCH_SESSION_ID, if set, is sent as
x-litellm-session-id, as the agent's calls do.
"""
import argparse
import hashlib
import json
import os
import sys
import urllib.request

PROVIDER = "neurodesk"  # the one gateway every model went through before dedicated keys

IDENTITY = ("model", "version", "fingerprint")
PER_REQUEST = ("call-id", "request-id", "duration", "cost", "spend", "key",
               "ratelimit", "limit", "remaining", "latency", "timing", "trace")
HASHED = ("api-base", "api_base", "url", "host")
ROUTING = ("fallback", "retries")
PROXY = ("litellm-version", "model-group")


def h(s, n=12):
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def identity_headers(headers, proxy=False):
    """Identity headers (see the module docstring); with proxy=True, only the proxy
    headers that the fingerprint leaves out."""
    out = {}
    for k, v in headers.items():
        name = k.lower()
        if not any(x in name for x in IDENTITY) or any(x in name for x in PER_REQUEST):
            continue
        if any(x in name for x in PROXY) != proxy:
            continue
        out[name] = ("sha256:" + h(str(v))) if any(x in name for x in HASHED) else str(v)
    return dict(sorted(out.items()))


def summarize(roster_json, response_json, headers):
    roster = None
    if isinstance(roster_json, dict) and isinstance(roster_json.get("data"), list):
        roster = sorted(m.get("id", "") for m in roster_json["data"] if isinstance(m, dict))
    served = fp_raw = None
    probe = None
    basis = []
    if isinstance(response_json, dict):
        served = response_json.get("model")
        fp_raw = response_json.get("system_fingerprint")
        idheaders = identity_headers(headers or {})
        probe = {"model": served, "system_fingerprint": fp_raw, "headers": idheaders}
        if served:
            basis.append("model")
        if fp_raw:
            basis.append("system_fingerprint")
        basis += sorted(idheaders)
    fingerprint = strength = None
    if probe is not None and served:
        fingerprint = h(json.dumps(probe, sort_keys=True))
        strength = "strong" if len(basis) > 1 else "weak"
    if probe is not None:
        probe["routing"] = dict(sorted(
            (k.lower(), str(v)) for k, v in (headers or {}).items()
            if any(x in k.lower() for x in ROUTING)))
        probe["proxy"] = identity_headers(headers or {}, proxy=True)
        probe["basis"] = basis
    return {"gateway_roster": roster, "model_served": served,
            "model_fingerprint": fingerprint, "model_fingerprint_strength": strength,
            "model_probe": probe}


def load_providers(path):
    """sweep.json's top-level `providers`, "_comment" keys left out."""
    sweep = json.load(open(path, encoding="utf-8"))
    return {k: v for k, v in (sweep.get("providers") or {}).items()
            if not k.startswith("_")}


def resolve(model_id, providers_path, default_gateway):
    """(base_url, key_env, provider) for a model id.

    provider is the text before the first '/' in model_id, or None when it has
    none. For any OTHER provider, when providers_path names it, its base_url and
    key_env are used. The PROVIDER ("neurodesk") itself, and a model id with no
    prefix at all, always resolve to default_gateway and $NEURODESK_API_KEY --
    $NEURODESK_GATEWAY/--gateway is this one gateway's own override, named for it,
    and must keep working whether or not sweep.json also declares it.
    """
    provider = model_id.split("/", 1)[0] if "/" in model_id else None
    if providers_path and provider and provider != PROVIDER:
        pdef = load_providers(providers_path).get(provider)
        if pdef:
            return pdef["base_url"], pdef["key_env"], provider
    return default_gateway, "NEURODESK_API_KEY", provider


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
    ap.add_argument("--providers", help="sweep.json, to resolve the gateway and key "
                    "from the model id's provider prefix")
    ap.add_argument("--offline", nargs=3, metavar=("ROSTER", "RESPONSE", "HEADERS"))
    ap.add_argument("--phase", choices=("start", "end"), default="start")
    a = ap.parse_args()
    base_url, key_env, provider = resolve(a.model, a.providers, a.gateway)
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
        key = os.environ.get(key_env, "")
        base = base_url.rstrip("/")
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
    out["provider"] = provider
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
