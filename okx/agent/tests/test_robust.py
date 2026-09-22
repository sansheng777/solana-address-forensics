#!/usr/bin/env python3
"""okx/agent/tests/test_robust.py — layer-4 network robustness self-test. No network: urlopen is replaced.

    docker run --rm -v /root/meme:/work -w /work node:22 python3 okx/agent/tests/test_robust.py

★ Why these need their own test: these paths are **only reached when something actually goes
  wrong**, so a hundred normal runs never touch them. Not staging the failure is not fixing it.
"""
import io, json, os, socket, sys, urllib.error, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
os.environ.setdefault("OPENROUTER_API_KEY", "test-key-not-used")
import run

run.time.sleep = lambda *_: None          # do not let the back-off stretch the test to 35s
n = 0


def ok(cond, msg):
    global n
    n += 1
    assert cond, "✗ " + msg


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def fake(seq):
    """Each item in seq is either an exception to raise or a response body to return. One call
    consumes one item."""
    box = list(seq)
    calls = []

    def _open(req, timeout=None):
        calls.append(json.loads(req.data.decode()))
        x = box.pop(0)
        if isinstance(x, Exception): raise x
        return _Resp(json.dumps(x).encode())
    _open.calls = calls
    return _open


def _http(code, body):
    return urllib.error.HTTPError("u", code, "e", {}, io.BytesIO(body.encode()))


# ── ① a timeout must be caught and retried (before 2026-09-20 it escaped the retry logic) ──
urllib.request.urlopen = fake([socket.timeout("timed out"),
                               TimeoutError("timed out"),
                               {"ok": 1}])
ok(run._post("https://x.test/v1", {"a": 1}, {}) == {"ok": 1}, "a timeout was not caught, or the retry did not succeed")

# ── ② HTTPError must be caught before OSError (HTTPError ⊂ URLError ⊂ OSError) ──
#    a 401 must not be retried; it should raise ModelError straight away
urllib.request.urlopen = f = fake([_http(401, "no auth"), {"ok": 1}])
try:
    run._post("https://x.test/v1", {"a": 1}, {}); ok(False, "a 401 must not be retried as a network error")
except run.ModelError as e:
    ok("401" in str(e), "the 401 error should carry the status code, got: %s" % e)
ok(len(f.calls) == 1, "a 401 should be called once, got %d" % len(f.calls))

# ── ③ 429 / 5xx must be retried ──
urllib.request.urlopen = fake([_http(429, "slow down"), _http(503, "busy"), {"ok": 1}])
ok(run._post("https://x.test/v1", {"a": 1}, {}) == {"ok": 1}, "the 429/503 retry did not succeed")

# ── ④ when the error names a parameter → drop it and resend, and **verify it was dropped** ──
urllib.request.urlopen = f = fake([_http(400, "`temperature` is deprecated"), {"ok": 1}])
ok(run._post("https://x.test/v1", {"model": "m", "temperature": 0}, {}) == {"ok": 1}, "temperature was not dropped before resending")
ok("temperature" not in f.calls[1], "the second request still carries temperature")
ok(f.calls[1]["model"] == "m", "dropping one parameter lost other keys as well")

# ── ⑤ exhausting the retries must raise ModelError, not a raw traceback ──
urllib.request.urlopen = fake([socket.timeout()] * 4)
try:
    run._post("https://x.test/v1", {}, {}); ok(False, "repeated timeouts should raise ModelError")
except run.ModelError as e:
    ok("network layer" in str(e), "wrong message for repeated timeouts: %s" % e)

# ── ⑥ a non-JSON response → ModelError, without retrying (retrying would not help) ──
class _Bad(_Resp):
    def read(self): return b"<html>502 Bad Gateway</html>"
urllib.request.urlopen = lambda req, timeout=None: _Bad()
try:
    run._post("https://x.test/v1", {}, {}); ok(False, "a non-JSON response should raise ModelError")
except run.ModelError as e:
    ok("not JSON" in str(e), "wrong message for a non-JSON response: %s" % e)

# ── ⑦ truncation must be recognised (truncated=True), not treated as a normal response ──
urllib.request.urlopen = fake([{"choices": [{"message": {"content": "{half a re"},
                                             "finish_reason": "length"}],
                                "usage": {"cost": 0.001}}])
txt, meta = run.call_model("sys", "usr", "vendor/model", budget=1000)
ok(meta["truncated"] is True, "finish_reason=length was not recognised as truncation")
ok(meta["budget"] == 1000, "meta did not record the budget used for this call")

# ── ⑧ a normal response must have truncated=False ──
urllib.request.urlopen = fake([{"choices": [{"message": {"content": "{}"},
                                             "finish_reason": "stop"}], "usage": {}}])
txt, meta = run.call_model("sys", "usr", "vendor/model")
ok(meta["truncated"] is False, "a normal response was misread as truncated")
ok(meta["provider"] == "openrouter", "a model name with a slash did not route to openrouter")

# ── ⑨ an error field must be surfaced, not passed on as empty content ──
urllib.request.urlopen = fake([{"error": {"message": "rate limited"}}])
try:
    run.call_model("sys", "usr", "vendor/model"); ok(False, "the error field in the body was ignored")
except run.ModelError as e:
    ok("rate limited" in str(e), "the error content was not carried into the message: %s" % e)

print("OK  network robustness self-test passed (%d assertions)" % n)
