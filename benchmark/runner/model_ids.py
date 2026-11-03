"""A model id in its three forms, and the one rule between them.

  opencode id  <provider>/<model>, what `opencode run -m` takes and run.yml's
               matrix carries: neurodesk/EGI.kimi-k3, nrp/glm-5.
  wave id      the opencode id with a leading `neurodesk/` removed: EGI.kimi-k3,
               nrp/glm-5. What sweep.json's waves declare, assemble takes, and the
               runs CSV and summaries write. An id with no '/' names the neurodesk
               gateway (the gateway's own names already carry the upstream, as in
               EGI.kimi-k3); any other provider keeps its prefix.
  segment      the opencode id with '/' turned into '-': neurodesk-EGI.kimi-k3,
               nrp-glm-5. The model part of a run directory and an artifact name
               (run_bench.sh's NAME=), since neither can hold a '/'.

Provider names contain no '-', so a segment splits at its first '-'. That split is
only made for a provider sweep.json declares: a segment that starts with anything
else is returned unchanged rather than guessed at.
"""
import json
import os

DEFAULT = "neurodesk"
SWEEP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ci", "sweep.json")


def wave_id(model):
    m = (model or "").strip()
    return m[len(DEFAULT) + 1:] if m.startswith(DEFAULT + "/") else m


def opencode_id(model):
    m = wave_id(model)
    return m if "/" in m else "%s/%s" % (DEFAULT, m)


def segment(model):
    return opencode_id(model).replace("/", "-")


def provider_names(sweep_path=None):
    """The providers sweep.json declares ("_comment" keys left out), plus the default.
    A sweep that cannot be read declares none beyond the default."""
    try:
        with open(sweep_path or SWEEP, encoding="utf-8") as fh:
            providers = json.load(fh).get("providers") or {}
    except (OSError, ValueError):
        providers = {}
    return {k for k in providers if not k.startswith("_")} | {DEFAULT}


def from_segment(seg, providers=None):
    """The wave id a run directory's model segment names: 'neurodesk-X' -> 'X',
    '<p>-X' -> 'p/X' for a declared provider p, anything else unchanged."""
    if seg.startswith(DEFAULT + "-"):
        return seg[len(DEFAULT) + 1:]
    p, sep, rest = seg.partition("-")
    if sep and rest and p in (provider_names() if providers is None else providers):
        return p + "/" + rest
    return seg


def provider_base_url(provider, base_url):
    """The base URL a run uses for a provider: the key proxy's when the pod has one,
    else sweep.json's base_url. A pod with the proxy sets LLM_PROXY_CONTROL_URL and one
    LLM_PROXY_<PROVIDER>_BASE_URL per provider it serves; the job then holds no provider
    key, so a provider the proxy does not serve is a ValueError, never a direct call."""
    if not os.environ.get("LLM_PROXY_CONTROL_URL"):
        return base_url
    name = "LLM_PROXY_%s_BASE_URL" % provider.upper()
    url = os.environ.get(name)
    if not url:
        raise ValueError("the key proxy serves no provider %s (%s is unset)" % (provider, name))
    return url
