#!/usr/bin/env python3
"""How a model's answer speed changes with the number of requests in flight at once.

    load_probe.py --model nrp/minimax-m2 [--model ...] --providers sweep.json
        --worker I --workers N --start 2026-11-14T02:00:00Z [--period 40]
        [--levels 1,4,8,12,24,48] [--rounds 2] [--passes 2] [--max-tokens 200]
        [--context-words 2000] [--out worker_I.json]
    load_probe.py --merge worker_*.json [--out probe.json]

The probe is a fixed list of rounds, the same in every worker: for each pass, each level
(passes alternate ascending and descending, so a drift in the provider's speed over the
probe's minutes does not line up with the level), each model, `rounds` rounds. Round k
starts at --start plus k periods on the clock. In a round of level L, the L requests are
spread over the N workers (worker i sends L // N, plus one when i < L % N) and each worker
starts its share at that moment. One worker is one pod with its own key proxy, which is how
a wave's agents reach a provider; a proxy allows 8 calls in flight per provider, so a level
above 8 needs more than one worker.

A worker that reaches a round more than --grace seconds late does not send and records the
round as late, so the merge can tell a level that was not reached from one that was.

Every request carries the same fixed text after a line that differs per request, so no
request is answered from another's cached prefix. Recorded per request: the round, the
seconds to the full answer, the prompt and completion token counts the provider reports,
and the HTTP status or the error's type. Never a response body or a header: an error body
can carry a key's digest.

--merge prints, per model and level: rounds, rounds in which every request was sent,
requests, how many succeeded, the statuses seen, median and 90th percentile seconds and
median completion tokens per second over the rounds that reached their level.
"""
import argparse
import calendar
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from model_probe import resolve  # noqa: E402

SENTENCE = "The quick brown fox jumps over the lazy dog near the quiet river bank at dawn."
QUESTION = ("Describe in plain words, in about 150 words, how a kettle boils water. "
            "Do not use lists.")
PER_WORKER_MAX = 8   # the key proxy's calls in flight per provider


def prompt(tag, context_words):
    """One request's text: a line unique to it first, then the same filler and question."""
    words = SENTENCE.split()
    filler = " ".join(words[i % len(words)] for i in range(context_words))
    return "Request %s.\n\n%s\n\n%s" % (tag, filler, QUESTION)


def send(base_url, key, model, text, max_tokens, timeout):
    """(seconds, status, prompt tokens, completion tokens); status is an HTTP code or an
    error's type name, the token counts None unless the call succeeded."""
    body = json.dumps({"model": model, "max_tokens": max_tokens, "temperature": 0,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            usage = (json.loads(r.read().decode()).get("usage") or {})
            return (time.monotonic() - t0, r.status, usage.get("prompt_tokens"),
                    usage.get("completion_tokens"))
    except urllib.error.HTTPError as e:
        return time.monotonic() - t0, e.code, None, None
    except Exception as e:  # a timeout, a reset, an answer that is not JSON
        return time.monotonic() - t0, type(e).__name__, None, None


def together(n, call):
    """n calls started together; their results in start order."""
    results = [None] * n
    gate = threading.Barrier(n)

    def work(i):
        gate.wait()
        results[i] = call(i)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def plan(models, levels, rounds, passes):
    """The probe's rounds in order: [{"k", "pass", "level", "model", "round"}]."""
    up = sorted(levels)
    out = []
    for p in range(passes):
        for level in (up if p % 2 == 0 else up[::-1]):
            for model in models:
                for rnd in range(rounds):
                    out.append({"k": len(out), "pass": p, "level": level, "model": model, "round": rnd})
    return out


def share(level, workers, i):
    """How many of a round's `level` requests worker i of `workers` sends."""
    return level // workers + (1 if i < level % workers else 0)


def work(rounds, worker, workers, start, period, call, grace=5.0, now=time.time, sleep=time.sleep):
    """This worker's rows over the plan. call(model, tag) -> send()'s tuple. A round this
    worker has no share in adds nothing; one it reaches late adds a row with status "late"."""
    rows = []
    for r in rounds:
        n = share(r["level"], workers, worker)
        if n == 0:
            continue
        due = start + r["k"] * period
        wait = due - now()
        if wait > 0:
            sleep(wait)
        late = now() - due
        base = {"k": r["k"], "model": r["model"], "level": r["level"], "pass": r["pass"],
                "round": r["round"], "worker": worker}
        if late > grace:
            rows.append(dict(base, seconds=None, status="late", prompt_tokens=None, completion_tokens=None))
            continue
        got = together(n, lambda j: call(r["model"], "%d-%d-%d" % (r["k"], worker, j)))
        for secs, status, ptok, ctok in got:
            rows.append(dict(base, seconds=round(secs, 3), status=status,
                             prompt_tokens=ptok, completion_tokens=ctok))
    return rows


def summarize(rows):
    """Per (model, level): rounds, rounds that reached their level, requests, ok, statuses,
    and over the reached rounds median and p90 seconds and median tokens per second."""
    out = []
    for key in sorted({(r["model"], r["level"]) for r in rows}):
        rs = [r for r in rows if (r["model"], r["level"]) == key]
        by_round = {}
        for r in rs:
            by_round.setdefault(r["k"], []).append(r)
        reached = {k for k, v in by_round.items()
                   if sum(1 for r in v if r["status"] != "late") == key[1]}
        sent = [r for r in rs if r["status"] != "late"]
        ok = [r for r in sent if r["status"] == 200 and r["k"] in reached]
        secs = sorted(r["seconds"] for r in ok)
        speed = [r["completion_tokens"] / r["seconds"] for r in ok
                 if r["completion_tokens"] and r["seconds"] > 0]
        statuses = {}
        for r in rs:
            statuses[str(r["status"])] = statuses.get(str(r["status"]), 0) + 1
        out.append({"model": key[0], "level": key[1], "rounds": len(by_round),
                    "rounds_reached": len(reached), "requests": len(sent),
                    "ok": sum(1 for r in sent if r["status"] == 200), "statuses": statuses,
                    "median_seconds": round(statistics.median(secs), 2) if secs else None,
                    "p90_seconds": round(secs[int(0.9 * (len(secs) - 1))], 2) if secs else None,
                    "median_tokens_per_s": round(statistics.median(speed), 1) if speed else None})
    return out


def show(summary):
    print("%-26s %5s %7s %8s %8s %4s %9s %7s %7s  %s" % (
        "model", "level", "rounds", "reached", "requests", "ok", "median s", "p90 s", "tok/s", "statuses"))
    for s in summary:
        print("%-26s %5d %7d %8d %8d %4d %9s %7s %7s  %s" % (
            s["model"], s["level"], s["rounds"], s["rounds_reached"], s["requests"], s["ok"],
            s["median_seconds"], s["p90_seconds"], s["median_tokens_per_s"], s["statuses"]))


def epoch(iso):
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge", nargs="+", metavar="WORKER_JSON")
    ap.add_argument("--model", action="append")
    ap.add_argument("--providers", help="sweep.json, for each provider's address")
    ap.add_argument("--gateway", default=os.environ.get("NEURODESK_GATEWAY", "https://llm.neurodesk.org/openai"))
    ap.add_argument("--worker", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--start", help="the first round's time, UTC, as 2026-11-14T02:00:00Z (default: now + 5 s)")
    ap.add_argument("--period", type=float, default=40.0)
    ap.add_argument("--grace", type=float, default=5.0)
    ap.add_argument("--levels", default="1,4,8,12,24,48")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=200)
    ap.add_argument("--context-words", type=int, default=2000)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--out")
    a = ap.parse_args()

    if a.merge:
        rows, settings = [], None
        for p in a.merge:
            d = json.load(open(p, encoding="utf-8"))
            rows += d["rows"]
            settings = settings or d.get("settings")
        summary = summarize(rows)
        show(summary)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as fh:
                json.dump({"settings": settings, "workers_merged": len(a.merge), "summary": summary, "rows": rows}, fh, indent=1)
                fh.write("\n")
        return 0

    if not a.model or not a.providers:
        sys.exit("--model and --providers are required")
    levels = [int(x) for x in a.levels.split(",") if x.strip()]
    if not levels or min(levels) < 1:
        sys.exit("levels must be 1 or more")
    if not 0 <= a.worker < a.workers:
        sys.exit("--worker must be between 0 and --workers - 1")
    if max(share(max(levels), a.workers, i) for i in range(a.workers)) > PER_WORKER_MAX:
        sys.exit("level %d over %d workers puts more than %d requests on one worker: the key proxy "
                 "allows %d in flight per provider" % (max(levels), a.workers, PER_WORKER_MAX, PER_WORKER_MAX))
    where = {}
    for m in a.model:
        base, key_env, _provider = resolve(m, a.providers, a.gateway)
        where[m] = (base, os.environ.get(key_env, ""), m.split("/", 1)[-1])

    def call(model, tag):
        base, key, name = where[model]
        return send(base, key, name, prompt(tag, a.context_words), a.max_tokens, a.timeout)

    start = epoch(a.start) if a.start else time.time() + 5
    rounds = plan(a.model, levels, a.rounds, a.passes)
    print("worker %d of %d: %d rounds, one every %.0f s from %s" % (
        a.worker, a.workers, len(rounds), a.period, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))))
    rows = work(rounds, a.worker, a.workers, start, a.period, call, grace=a.grace)
    late = sum(1 for r in rows if r["status"] == "late")
    print("worker %d: %d requests sent, %d rounds reached late" % (a.worker, len(rows) - late, late))
    if a.workers == 1:
        show(summarize(rows))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump({"settings": {"levels": levels, "rounds": a.rounds, "passes": a.passes, "workers": a.workers,
                                    "period": a.period, "max_tokens": a.max_tokens, "context_words": a.context_words,
                                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))},
                       "worker": a.worker, "rows": rows}, fh, indent=1)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
