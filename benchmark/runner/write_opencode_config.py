#!/usr/bin/env python3
"""Write the `neurodesk` provider into opencode's config. Nothing else creates it.

    python3 write_opencode_config.py                 # ~/.config/opencode/opencode.json
    python3 write_opencode_config.py --out PATH
    python3 write_opencode_config.py --check         # report, change nothing

    # Every provider sweep.json declares, instead of just `neurodesk`:
    python3 write_opencode_config.py --providers ../ci/sweep.json
    # A provider with models_endpoint false must have its models given explicitly:
    python3 write_opencode_config.py --providers ../ci/sweep.json --models openai/gpt-5

    # Only the provider one model id names (model_ids.py), so a run's config does
    # not depend on any other provider's roster answering:
    python3 write_opencode_config.py --providers ../ci/sweep.json --for-model nrp/glm-5

    # Every key_env --providers would read, for a workflow step to pass to redact.py
    # (so the masked-secret list grows with sweep.json's `providers`, never by hand);
    # with --for-model, only that model's:
    python3 write_opencode_config.py --providers ../ci/sweep.json --list-key-envs

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

from model_ids import opencode_id

GATEWAY =os.environ.get("NEURODESK_GATEWAY", "https://llm.neurodesk.org/openai")
PROVIDER = "neurodesk"

# Defaults, not discoveries. Every model on this gateway carried these in the
# known-good config; /openai/models does not report them reliably, and a wrong
# context limit truncates a prompt silently rather than failing. Override per model
# with --limit id:context:output if that ever stops being true.
DEFAULT_CONTEXT = 131000
DEFAULT_OUTPUT = 8192

# The exact shape of the 2026-09-01 breakage: a provider declaring one model whose id
# is the provider's own name. refuse_if_bad names it explicitly so the refusal cites
# the incident rather than a generic "looks wrong".


def discover(base_url, key, timeout=30):
    """Model ids a provider will actually serve, from its own roster at <base_url>/models."""
    req = urllib.request.Request(base_url.rstrip("/") + "/models")
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
            % (base_url, raw[:80]))
    if not isinstance(body, dict) or "data" not in body:
        raise SystemExit(
            "FAIL: %s/models returned JSON with no `data` list.\n"
            "      Got %s." % (base_url, type(body).__name__))
    ids = sorted({m.get("id") for m in body["data"] if m.get("id")})
    return ids


def refuse_if_bad(name, ids, source):
    """The one rule that matters (see the module docstring), generalized to any
    provider: refuse an empty or placeholder model list rather than write it."""
    if not ids:
        raise SystemExit(
            "FAIL: %s listed no models. Refusing to write a provider with\n"
            "      an empty model list -- every run would then resolve to nothing and\n"
            "      the gateway would answer `Model '' was not found`, which reads as a\n"
            "      server fault and is not one. Nothing written." % source)
    if set(ids) <= {name}:
        raise SystemExit(
            "FAIL: %s listed only %r, which is the provider's own name and\n"
            "      is exactly the placeholder the 2026-09-01 image upgrade left behind.\n"
            "      Nothing written." % (source, sorted(ids)))


def build_provider(name, base_url, key_env, ids, limits, extra_headers=None):
    """One opencode provider block: an OpenAI-compatible endpoint plus its models."""
    models = {}
    for i in ids:
        ctx, out = limits.get(i, (DEFAULT_CONTEXT, DEFAULT_OUTPUT))
        models[i] = {"name": i, "limit": {"context": ctx, "output": out}}
    # The key is referenced, never inlined. This file is world-readable on a shared
    # image and gets copied into bug reports.
    options = {"baseURL": base_url, "apiKey": "{env:%s}" % key_env}
    if extra_headers:
        options["headers"] = extra_headers
    return {"npm": "@ai-sdk/openai-compatible", "name": name,
            "options": options, "models": models}


def build(ids, limits):
    """The default, single-provider block: the neurodesk gateway. x-litellm-session-id
    groups a run's calls in the gateway's logs; run_bench.sh sets BENCH_SESSION_ID per
    run and records it, and the header is empty when it is unset."""
    return build_provider(PROVIDER, GATEWAY, "NEURODESK_API_KEY", ids, limits,
                          {"x-litellm-session-id": "{env:BENCH_SESSION_ID}"})


def load_providers(path, for_model=None):
    """sweep.json's top-level `providers`, "_comment" keys left out; with for_model,
    only the provider that model id names."""
    sweep = json.load(open(path, encoding="utf-8"))
    providers = sweep.get("providers") or {}
    if not providers:
        raise SystemExit("FAIL: %s declares no providers" % path)
    providers = {k: v for k, v in providers.items() if not k.startswith("_")}
    if for_model:
        name = opencode_id(for_model).split("/", 1)[0]
        if name not in providers:
            raise SystemExit("FAIL: %s names provider %s, which %s does not declare"
                             % (for_model, name, path))
        providers = {name: providers[name]}
    return providers


def parse_explicit_models(specs):
    """--models provider/id,provider/id,... -> {provider: [id, ...]}."""
    out = {}
    for spec in specs:
        for item in spec.split(","):
            item = item.strip()
            if not item:
                continue
            if "/" not in item:
                raise SystemExit("FAIL: --models wants provider/id, got %r" % item)
            prov, mid = item.split("/", 1)
            out.setdefault(prov, []).append(mid)
    return out


def report_diff(name, old_ids, ids):
    """Print what changed in one provider's model list since the last write."""
    if not old_ids:
        return
    gone = [m for m in old_ids if m not in ids]
    added = [m for m in ids if m not in old_ids]
    if gone:
        # qwen3.5-122b went this way. Nine runs can never be re-run because of it,
        # so a model leaving the roster is worth a line rather than a silent diff.
        print("  %s retired since last write: %s" % (name, ", ".join(gone)))
    if added:
        print("  %s new since last write    : %s" % (name, ", ".join(added)))
    if not gone and not added:
        print("  %s: no change to the model list" % name)


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
    ap.add_argument("--providers", help="sweep.json: write every provider it declares, "
                    "instead of just neurodesk")
    ap.add_argument("--models", action="append", default=[], metavar="PROVIDER/ID,...",
                    help="explicit roster for a provider whose models_endpoint is false")
    ap.add_argument("--list-key-envs", action="store_true",
                    help="print --providers' key_env names, space-separated, and exit "
                         "-- for a workflow step to pass them all to redact.py")
    ap.add_argument("--for-model", metavar="MODEL_ID",
                    help="with --providers: only the provider this model id names")
    a = ap.parse_args()
    if a.for_model and not a.providers:
        raise SystemExit("FAIL: --for-model needs --providers FILE")

    if a.list_key_envs:
        if not a.providers:
            raise SystemExit("FAIL: --list-key-envs needs --providers FILE")
        envs = sorted({p["key_env"] for p in
                       load_providers(a.providers, a.for_model).values()})
        print(" ".join(envs))
        return

    limits = {}
    for spec in a.limit:
        try:
            i, c, o = spec.rsplit(":", 2)
            limits[i] = (int(c), int(o))
        except ValueError:
            raise SystemExit("FAIL: --limit wants ID:CONTEXT:OUTPUT, got %r" % spec)

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

    written_models = 0
    if a.providers:
        # Every provider sweep.json declares, so a new one drops in as data: nothing
        # here names "neurodesk" or any other provider specifically.
        providers = load_providers(a.providers, a.for_model)
        explicit = parse_explicit_models(a.models)
        cfg.setdefault("provider", {})
        for name, pdef in sorted(providers.items()):
            base_url, key_env = pdef["base_url"], pdef["key_env"]
            if pdef.get("models_endpoint"):
                key = os.environ.get(key_env, "")
                if not key:
                    print("WARN: %s is unset for provider %s. Asking its roster anyway;\n"
                          "      if it is behind a proxy this will return a sign-in page,\n"
                          "      not a roster." % (key_env, name), file=sys.stderr)
                try:
                    ids = discover(base_url, key)
                except urllib.error.HTTPError as e:
                    raise SystemExit("FAIL: %s/models returned HTTP %s" % (base_url, e.code))
                except Exception as e:
                    raise SystemExit("FAIL: could not reach %s/models: %s" % (base_url, e))
                refuse_if_bad(name, ids, "%s/models" % base_url)
            else:
                ids = sorted(set(explicit.get(name, [])))
                if not ids:
                    raise SystemExit(
                        "FAIL: provider %s has no models_endpoint; its models must be "
                        "given explicitly: --models %s/id,..." % (name, name))
                refuse_if_bad(name, ids, "--models for %s" % name)
            print("provider %s (%s): %s" % (name, base_url, ", ".join(ids)))
            old_ids = sorted((cfg["provider"].get(name, {}).get("models") or {}).keys())
            report_diff(name, old_ids, ids)
            extra_headers = ({"x-litellm-session-id": "{env:BENCH_SESSION_ID}"}
                             if name == PROVIDER else None)
            cfg["provider"][name] = build_provider(name, base_url, key_env, ids, limits,
                                                   extra_headers)
            written_models += len(ids)
    else:
        # Today's behaviour: one provider, the neurodesk gateway, discovered from its
        # own roster. Unchanged so every existing caller of this script keeps working.
        key = os.environ.get("NEURODESK_API_KEY", "")
        if not key:
            print("WARN: NEURODESK_API_KEY is unset. Asking the gateway anyway; if it is\n"
                  "      behind a proxy this will return a sign-in page, not a roster.",
                  file=sys.stderr)
        try:
            ids = discover(GATEWAY, key)
        except urllib.error.HTTPError as e:
            raise SystemExit("FAIL: %s/models returned HTTP %s" % (GATEWAY, e.code))
        except Exception as e:
            raise SystemExit("FAIL: could not reach %s/models: %s" % (GATEWAY, e))
        refuse_if_bad(PROVIDER, ids, "%s/models" % GATEWAY)

        print("gateway: %s" % GATEWAY)
        print("models : %s" % ", ".join(ids))
        old_ids = sorted((cfg.get("provider") or {}).get(PROVIDER, {}).get("models", {}).keys())
        report_diff(PROVIDER, old_ids, ids)

        cfg.setdefault("provider", {})
        # Only our provider. ollama and jetstream on the VM are not ours to touch.
        cfg["provider"][PROVIDER] = build(ids, limits)
        written_models = len(ids)

    cfg.setdefault("$schema", "https://opencode.ai/config.json")
    cfg.setdefault("autoupdate", False)
    # Runs must not be shareable. The transcripts carry the task prompts.
    cfg["share"] = "disabled"
    cfg.setdefault("model", a.default_model)

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
    print("\nwrote %s (%d providers, %d models)"
          % (a.out, len(cfg.get("provider") or {}), written_models))


if __name__ == "__main__":
    main()
