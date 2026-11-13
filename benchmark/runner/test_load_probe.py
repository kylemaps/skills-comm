"""load_probe.py: the plan every worker shares, how a level is spread over workers, rounds
on the clock, the merge, and that a failed call keeps its status and nothing else."""
import http.server
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import load_probe as lp


class Plan(unittest.TestCase):
    def test_passes_alternate_direction_and_every_model_gets_every_level(self):
        p = lp.plan(["m1", "m2"], [12, 1, 4], rounds=2, passes=2)
        self.assertEqual(len(p), 2 * 3 * 2 * 2)
        self.assertEqual([r["k"] for r in p], list(range(len(p))))
        self.assertEqual([r["level"] for r in p if r["pass"] == 0][::4], [1, 4, 12])
        self.assertEqual([r["level"] for r in p if r["pass"] == 1][::4], [12, 4, 1])
        self.assertEqual({(r["model"], r["level"]) for r in p}, {(m, l) for m in ("m1", "m2") for l in (1, 4, 12)})

    def test_a_level_is_spread_over_the_workers(self):
        for level, workers in ((1, 24), (4, 24), (12, 24), (24, 24), (48, 24), (7, 3)):
            shares = [lp.share(level, workers, i) for i in range(workers)]
            self.assertEqual(sum(shares), level)
            self.assertLessEqual(max(shares) - min(shares), 1)
        self.assertEqual([lp.share(4, 24, i) for i in range(6)], [1, 1, 1, 1, 0, 0])
        self.assertEqual(lp.share(48, 24, 23), 2)

    def test_prompts_differ_only_in_their_first_line(self):
        a, b = lp.prompt("0-1-0", 50), lp.prompt("0-1-1", 50)
        self.assertNotEqual(a.split("\n")[0], b.split("\n")[0])
        self.assertEqual(a.split("\n")[1:], b.split("\n")[1:])
        self.assertEqual(len(a.split("\n\n")[1].split()), 50)


class Clock:
    def __init__(self, t=1000.0):
        self.t, self.slept = t, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Work(unittest.TestCase):
    def call(self, model, tag):
        return 2.0, 200, 100, 50

    def test_a_worker_sends_its_share_at_each_round_time(self):
        rounds = lp.plan(["m"], [1, 3], rounds=1, passes=1)       # k0 level 1, k1 level 3
        clock = Clock()
        rows0 = lp.work(rounds, 0, 2, start=1010.0, period=40.0, call=self.call, now=clock.now, sleep=clock.sleep)
        self.assertEqual([(r["k"], r["status"]) for r in rows0], [(0, 200), (1, 200), (1, 200)])
        self.assertEqual(clock.slept, [10.0, 40.0])
        clock = Clock()
        rows1 = lp.work(rounds, 1, 2, start=1010.0, period=40.0, call=self.call, now=clock.now, sleep=clock.sleep)
        self.assertEqual([(r["k"], r["status"]) for r in rows1], [(1, 200)])   # no share in the level 1 round
        self.assertEqual(clock.slept, [50.0])

    def test_a_round_reached_late_is_recorded_and_not_sent(self):
        rounds = lp.plan(["m"], [2], rounds=2, passes=1)
        sent = []
        clock = Clock(t=1020.0)                                   # 20 s after round 0 was due
        rows = lp.work(rounds, 0, 2, start=1000.0, period=40.0,
                       call=lambda m, tag: sent.append(tag) or (1.0, 200, 1, 1), now=clock.now, sleep=clock.sleep)
        self.assertEqual([r["status"] for r in rows], ["late", 200])
        self.assertEqual(len(sent), 1)

    def test_calls_of_a_round_start_together(self):
        gate = threading.Barrier(4, timeout=5)
        self.assertEqual(lp.together(4, lambda i: gate.wait() and i or i), [0, 1, 2, 3])


class Merge(unittest.TestCase):
    def row(self, k, status=200, secs=2.0, level=2, worker=0):
        return {"k": k, "model": "m", "level": level, "pass": 0, "round": 0, "worker": worker,
                "seconds": secs if status != "late" else None, "status": status,
                "prompt_tokens": 10 if status == 200 else None, "completion_tokens": 100 if status == 200 else None}

    def test_speed_comes_from_rounds_that_reached_their_level(self):
        rows = [self.row(0, secs=2.0), self.row(0, secs=4.0, worker=1),         # reached: 2 of 2 sent
                self.row(1, secs=1.0), self.row(1, status="late", worker=1),    # not reached
                self.row(2, status=429, secs=0.1), self.row(2, status="TimeoutError", secs=9.0, worker=1)]
        (s,) = lp.summarize(rows)
        self.assertEqual((s["rounds"], s["rounds_reached"], s["requests"], s["ok"]), (3, 2, 5, 3))
        self.assertEqual(s["statuses"], {"200": 3, "late": 1, "429": 1, "TimeoutError": 1})
        self.assertEqual(s["median_seconds"], 3.0)             # round 1's lone fast call is left out
        self.assertEqual(s["median_tokens_per_s"], 37.5)


class Send(unittest.TestCase):
    """Against a local server: a success returns the usage counts; a refusal returns its
    status and none of its body."""

    def setUp(self):
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode())
                if body["model"] == "refused":
                    self.send_response(429)
                    self.end_headers()
                    self.wfile.write(b'{"error": "Rate limit exceeded for api_key: 0123abcd"}')
                    return
                out = json.dumps({"model": body["model"], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_success_and_refusal(self):
        secs, status, ptok, ctok = lp.send(self.base, "placeholder", "served", "hi", 8, 10)
        self.assertEqual((status, ptok, ctok), (200, 7, 3))
        self.assertGreater(secs, 0)
        got = lp.send(self.base, "placeholder", "refused", "hi", 8, 10)
        self.assertEqual(got[1:], (429, None, None))
        self.assertNotIn("0123abcd", repr(got))


if __name__ == "__main__":
    unittest.main()
