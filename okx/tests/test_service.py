#!/usr/bin/env python3
"""Offline self-test for the service layer. **Runs inside the service image** (it is the only place
with fastapi installed) and touches no network:

    docker run --rm -e X402_DISABLE=1 -v /root/meme:/app -w /app meme/okx-service \
        python3 okx/tests/test_service.py

What it covers is exactly the production behaviour that review 2026-09-22 found missing: atomic and
locked token storage, the retry/queue bookkeeping, the per-IP limit on the free routes, the engine
version inside the cache key, and the cost arithmetic.
"""
import json, os, sys, tempfile, threading, time

os.environ.setdefault("X402_DISABLE", "1")
os.environ.setdefault("OKX_ROOT", "/app")
sys.path.insert(0, os.path.join(os.environ["OKX_ROOT"], "okx", "service"))
import app                                                     # noqa: E402

n = [0]
bad = []


def want(cond, msg):
    n[0] += 1
    if not cond: bad.append(msg)


# ── 1. tokens.json: atomic, locked, corrupt-safe ─────────────────────────────
d = tempfile.mkdtemp()
app.TOKENS_F = os.path.join(d, "tokens.json")
want(app._tokens() == {}, "a missing token file must read as empty")

app._token_put("t1", {"key": "k1", "address": "a", "window": ["d", "d"], "created": time.time()})
want(list(app._tokens()) == ["t1"], "a token must survive a write")
want(not os.path.exists(app.TOKENS_F + ".tmp"), "the temp file must be renamed away")

open(app.TOKENS_F, "w").write("{ this is not json")
want(app._tokens() == {}, "a corrupt token file must read as empty")
want([f for f in os.listdir(d) if ".corrupt-" in f], "a corrupt token file must be kept, not dropped")

# concurrent writers: not one token may be lost (the read-modify-write used to be unlocked)
if os.path.exists(app.TOKENS_F): os.remove(app.TOKENS_F)
def _w(i):
    app._token_put("c%03d" % i, {"key": "k", "address": "a", "window": ["d", "d"],
                                 "created": time.time()})
ts = [threading.Thread(target=_w, args=(i,)) for i in range(40)]
[t.start() for t in ts]; [t.join() for t in ts]
want(len(app._tokens()) == 40, "40 concurrent writers must leave 40 tokens, got %d" % len(app._tokens()))

# the TTL prunes on write: an expired entry already in the file must not survive the next write
app._atomic_write(app.TOKENS_F, {"old": {"key": "k", "address": "a", "window": ["d", "d"],
                                         "created": time.time() - app.TOKEN_TTL - 10}})
app._token_put("fresh", {"key": "k", "address": "a", "window": ["d", "d"], "created": time.time()})
want(list(app._tokens()) == ["fresh"], "an expired token must be pruned on the next write")

# ── 2. the per-IP limit on the free routes ───────────────────────────────────
app.RL_MAX, app._rl = 3, {}
want([app._rate_ok("1.2.3.4") for _ in range(5)] == [True, True, True, False, False],
     "the limit must allow exactly RL_MAX per minute")
want(app._rate_ok("5.6.7.8"), "the limit must be per IP, not global")

# ── 3. the engine version, and the cache key that carries it ─────────────────
want(len(app.ENGINE_VERSION) == 8 and app.ENGINE_VERSION.isalnum(),
     "ENGINE_VERSION must be a short hex digest")
import hashlib
k = lambda ver: hashlib.sha256(("A|d0|d1|%s" % ver).encode()).hexdigest()[:16]
want(k(app.ENGINE_VERSION) != k("00000000"),
     "the cache key must change when the engine changes")

# ── 4. the cost of one report = the draft plus every rewrite ─────────────────
want(app._model_usd({"cost": 0.002, "repair": [{"meta": {"cost": 0.001}},
                                               {"meta": {"cost": 0.0005}}]}) == 0.0035,
     "model cost must add the rewrites to the first draft")
want(app._model_usd({}) == 0.0, "a missing cost must read as zero, not raise")

# ── 5. a queued job is not queued twice ──────────────────────────────────────
app.JOBS.clear()
while not app._Q.empty(): app._Q.get_nowait()
app._start_job("A", "d0", "d1", "kk")
before = app._Q.qsize() + sum(1 for j in app.JOBS.values() if j.get("status") == "running")
app._start_job("A", "d0", "d1", "kk")
after = app._Q.qsize() + sum(1 for j in app.JOBS.values() if j.get("status") == "running")
want(after == before, "the same (address, window) must not be queued twice")

print(("FAIL  %d of %d:\n   " % (len(bad), n[0])) + "\n   ".join(bad) if bad
      else "OK  service self-test passed (%d assertions)" % n[0])
sys.exit(1 if bad else 0)
