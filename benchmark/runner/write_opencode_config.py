#!/usr/bin/env python3
"""Write the `neurodesk` provider into opencode's config. Nothing else creates it.

    python3 write_opencode_config.py                 # ~/.config/opencode/opencode.json
    python3 write_opencode_config.py --out PATH
    python3 write_opencode_config.py --check         # report, change nothing

WHY THIS EXISTS
On the Play server this file was written once, interactively, by the neurodesktop
image's own opencode wrapper on first run. Nothing in our harness wrote it --
`setup.sh` creates `~/.config/opencode/skills/` and stops, and the runner README's
claim that setup writes the model list is wrong. That was survivable while the only
machine was one long-lived VM. It is not survivable in CI, where every pod is fresh
and there is no interactive first run.

It is also the fault that cost the most for its size. The 2026-09-01 image upgrade
reset this file, leaving the `neurodesk` provider declaring a single placeholder model
called `neurodesk`. `-m neurodesk/glm-5.2` then resolved to nothing and the gateway
answered `Model '' was not found` -- an error naming an empty string, which points at
the server rather than at the client. Every run for a day died in seconds and was
classified as our harness failing.

THE ONE RULE THAT MATTERS
Refuse to write a provider whose model list is empty or is the placeholder. A config
that exists and is wrong is worse than one that is missing: the missing one fails at
startup, the wrong one fails per-run, in seconds, in a way that reads as the model.
Same reasoning as preflight's snapshot step, which used to save a broken config as
"known-good" because the gateway was answering 200.

WHAT IT PRESERVES
The server's config also declares `ollama`, `jetstream` and `jetstream-deepseek`,
which are nothing to do with us. Merging rather than overwriting means this is safe to
run on the VM, not only on a fresh pod.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

GATEWAY = os.environ.get("NEURODESK_GATEWAY", "https://llm.neurodesk.org/openai")
PROVIDER = "neurodesk"

# Defaults, not discoveries. Every model on this gateway carried these in the
# known-good config; /openai/models does not report them reliably, and a wrong
# context limit truncates a prompt silently rather than failing. Override per model
# with --limit id:context:output if that ever stops being true.
DEFAULT_CONTEXT = 131000
DEFAULT_OUTPUT = 8192

# The exact shape of the 2026-09-01 breakage: a provider declaring one model whose id
# is the provider's own name. Named explicitly so the refusal below cites the incident
# rather than a generic "looks wrong".
PLACEHOLDER = {PROVIDER}


def discover(key, timeout=30):
    """Model ids the gateway will actually serve, from its own roster."""
    req = urllib.request.Request(GATEWAY.rstrip("/") + "/models")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        raw = fh.read()
    # The oauth2-proxy incident: /models answered 200 with a sign-in PAGE, so
    # anything checking only the status code saw a healthy gateway. Parse, then
    # complain about the shape, never about the code -- and say "proxy", because
    # the first hour of that outage went on the API key.
    try:
        body = json.loads(raw)
    except ValueError:
        raise SystemExit(
            "FAIL: %s/models answered, but with something that is not JSON.\n"
            "      First bytes: %r\n"
            "      A sign-in page arrives as a 200. This is what an oauth2 proxy in\n"
            "      front of the gateway looks like, and it is not a bad API key."
            % (GATEWAY, raw[:80]))
    if not isinstance(body, dict) or "data" not in body:
        raise SystemExit(
            "FAIL: %s/models returned JSON with no `data` list.\n"
            "      Got %s." % (GATEWAY, type(body).__name__))
    ids = sorted({m.get("id") for m in body["data"] if m.get("id")})
    return ids


def build(ids, limits):
    models = {}
    for i in ids:
        ctx, out = limits.get(i, (DEFAULT_CONTEXT, DEFAULT_OUTPUT))
        models[i] = {"name": i, "limit": {"context": ctx, "output": out}}
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Neurodesk LiteLLM",
        # The key is referenced, never inlined. This file is world-readable on a
        # shared image and gets copied into bug reports.
        "options": {"baseURL": GATEWAY, "apiKey": "{env:NEURODESK_API_KEY}"},
        "models": models,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    default_out = os.path.join(os.path.expanduser("~"), ".config", "opencode",
                               "opencode.json")
    ap.add_argument("--out", default=default_out)
    ap.add_argument("--check", action="store_true",
                    help="report what is there and what would change; write nothing")
    ap.add_argument("--default-model", default="neurodesk/glm-5.2")
    ap.add_argument("--limit", action="append", default=[], metavar="ID:CTX:OUT",
                    help="override the context/output limit for one model")
    a = ap.parse_args()

    limits = {}
    for spec in a.limit:
        try:
            i, c, o = spec.rsplit(":", 2)
            limits[i] = (int(c), int(o))
        except ValueError:
            raise SystemExit("FAIL: --limit wants ID:CONTEXT:OUTPUT, got %r" % spec)

    key = os.environ.get("NEURODESK_API_KEY", "")
    if not key:
        print("WARN: NEURODESK_API_KEY is unset. Asking the gateway anyway; if it is\n"
              "      behind a proxy this will return a sign-in page, not a roster.",
              file=sys.stderr)
    try:
        ids = discover(key)
    except urllib.error.HTTPError as e:
        raise SystemExit("FAIL: %s/models returned HTTP %s" % (GATEWAY, e.code))
    except Exception as e:
        raise SystemExit("FAIL: could not reach %s/models: %s" % (GATEWAY, e))

    # The refusal. Both branches describe the incident, because the whole cost of
    # that day was that the failure did not look like this.
    if not ids:
        raise SystemExit(
            "FAIL: the gateway listed no models. Refusing to write a provider with\n"
            "      an empty model list -- every run would then resolve to nothing and\n"
            "      the gateway would answer `Model '' was not found`, which reads as a\n"
            "      server fault and is not one. Nothing written.")
    if set(ids) <= PLACEHOLDER:
        raise SystemExit(
            "FAIL: the gateway listed only %r, which is the provider's own name and\n"
            "      is exactly the placeholder the 2026-09-01 image upgrade left behind.\n"
            "      Nothing written." % sorted(ids))

    print("gateway: %s" % GATEWAY)
    print("models : %s" % ", ".join(ids))

    cfg = {}
    if os.path.exists(a.out):
        try:
            cfg = json.load(open(a.out, encoding="utf-8"))
        except Exception as e:
            raise SystemExit(
                "FAIL: %s exists and is not valid JSON (%s).\n"
                "      Refusing to overwrite it -- move it aside yourself, so whatever\n"
                "      state it is in survives to be looked at." % (a.out, e))
        if not isinstance(cfg, dict):
            raise SystemExit("FAIL: %s is not a JSON object" % a.out)

    before = (cfg.get("provider") or {}).get(PROVIDER, {})
    old = sorted((before.get("models") or {}).keys())
    if old:
        gone = [m for m in old if m not in ids]
        added = [m for m in ids if m not in old]
        if gone:
            # qwen3.5-122b went this way. Nine runs can never be re-run because of it,
            # so a model leaving the roster is worth a line rather than a silent diff.
            print("  retired since last write: %s" % ", ".join(gone))
        if added:
            print("  new since last write    : %s" % ", ".join(added))
        if not gone and not added:
            print("  no change to the model list")

    cfg.setdefault("$schema", "https://opencode.ai/config.json")
    cfg.setdefault("autoupdate", False)
    # Runs must not be shareable. The transcripts carry the task prompts.
    cfg["share"] = "disabled"
    cfg.setdefault("model", a.default_model)
    cfg.setdefault("provider", {})
    # Only our provider. ollama and jetstream on the VM are not ours to touch.
    cfg["provider"][PROVIDER] = build(ids, limits)

    if a.check:
        print("\n--check: nothing written. %s would %s."
              % (a.out, "be updated" if os.path.exists(a.out) else "be created"))
        return

    d = os.path.dirname(a.out)
    if d:
        os.makedirs(d, exist_ok=True)
    # Write via a temp file in the same directory and rename. A process killed
    # partway through a direct write leaves truncated JSON, which is the one state
    # this script refuses to repair.
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, a.out)
    print("\nwrote %s (%d models under %r)" % (a.out, len(ids), PROVIDER))


if __name__ == "__main__":
    main()
