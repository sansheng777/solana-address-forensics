#!/usr/bin/env python3
"""okx/service/app.py — the A2MCP endpoint: one Solana address in, one mechanically verified
trading-behaviour report out.

    POST /profile     ★ the paid route (x402). Unpaid → 402 + PAYMENT-REQUIRED; replayed with a
                        signature → 200 + the report
    GET  /health      free, outside the middleware. For ops and for the registration self-check
    GET  /spec        free. The parameter schema — the machine-readable form of the [Parameter Spec]
                        section required at registration
    GET  /report/{t}  free. Pick up a report a paid call started (see the async note below)

═══ Two properties of x402 that shape this file ═════════════════════════════

    ① **An unpaid probe never reaches the business code.** The middleware returns 402 itself, and
       only the replayed, signed request lands in the handler below. So there is no risk of burning
       a Dune query and a model call for free.
    ② **Payment happens first, work second.** Once the handler runs, the caller has already been
       charged — so every failure must be reported honestly, and **the cache is checked first** so
       the same address never burns Dune credits twice.

═══ Why a paid call hands back a pickup token ═══════════════════════════════

    The official buyer CLI gives up on the HTTP read after **30 seconds** (measured 2026-09-21:
    Caddy logged `status 0 / dur 30.0s` with the PAYMENT-SIGNATURE header present, the payment never
    settled and the caller got nothing). A cold address needs 90-450 seconds. There is no timeout
    switch in the client.
    So `POST /profile` returns within a second: a cache hit returns the whole report, a miss starts
    the work in the background and returns a pickup token. `GET /report/{token}` is **free** — the
    payment already bought that computation, and picking it up is not charged again.

═══ Dependencies ═══════════════════════════════════════════════════════════

    This file is the **only** place in the project allowed to have pip dependencies
    (fastapi / uvicorn / okxweb3-app-x402). They are installed inside the Dockerfile and never touch
    the host — see the environment rules in CLAUDE.md.
    The engine layers (store / features / agent) remain pure standard library.
"""
import hashlib, json, os, queue, re, secrets, sys, threading, time, calendar
import urllib.request, urllib.error

# The engine layers live in the repo, mounted at /app (see the Dockerfile)
ROOT = os.environ.get("OKX_ROOT", "/app")
for p in (os.path.join(ROOT, "okx", "engine"), os.path.join(ROOT, "okx", "agent"),
          os.path.join(ROOT, "dune", "lib")):
    if p not in sys.path: sys.path.insert(0, p)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from x402.http import (OKXAuthConfig, OKXFacilitatorClient, OKXFacilitatorConfig, PaymentOption)
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig, HTTPResponseBody
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.server import x402ResourceServer

NETWORK = os.environ.get("X402_NETWORK", "eip155:196")       # 196 = X Layer mainnet
# ⚠️ Testnet (eip155:1952) does not work: the payment SDK has no default stablecoin registered for
#    it, so a price like "$0.10" cannot be converted and the middleware raises — the route then
#    answers 500 instead of 402 (measured 2026-09-21). Mainnet is the only option.
PRICE = os.environ.get("X402_PRICE", "$0.10")
PAY_TO = os.environ.get("PAY_TO_ADDRESS", "")
# ★ resource.url inside the 402 challenge must be the **public https address**.
#   Without it the SDK uses the request's own URL — and behind Caddy the container sees http://...,
#   so the challenge advertises http:// (measured 2026-09-21). The buyer replays against that URL
#   and the marketplace validates it, so both break.
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
CACHE = os.path.join(ROOT, "okx", "data", "_cache")
OUT = os.path.join(ROOT, "okx", "data", "_svc")
# Solana address: base58, 32-44 chars. ★ Strict: the address is the only user input, and therefore
# the only injection surface
B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MAX_DAYS = int(os.environ.get("OKX_MAX_DAYS") or 7)

# ★ The report cache is keyed by (address, window) **and by the code that produced it**. Without the
#   second half, redeploying a changed prompt, verifier or layer-3 table silently kept serving the
#   old report — with `cached: true` and no way for the caller to tell which engine wrote it.
def _engine_version():
    h = hashlib.sha256()
    for rel in ("okx/engine/features.py", "okx/engine/store.py", "okx/agent/prompt.md",
                "okx/agent/verify.py", "okx/agent/SCHEMA.md", "okx/agent/repair.md",
                "okx/agent/forbidden.txt", "okx/agent/vague.txt", "okx/agent/follower-banned.txt"):
        try:
            h.update(open(os.path.join(ROOT, rel), "rb").read())
        except OSError:
            h.update(b"?" + rel.encode())        # a missing file must change the digest, not crash
    return h.hexdigest()[:8]


ENGINE_VERSION = _engine_version()
# How many reports may be computed at once. Each one is a Dune query plus a model call, so this is
# about the quota and the upstream rate limits, not about CPU. Jobs above the limit queue.
MAX_WORKERS = max(1, int(os.environ.get("OKX_MAX_WORKERS") or 2))
# A paid call whose job failed is retried for free, this many times, before the failure is returned.
MAX_ATTEMPTS = max(1, int(os.environ.get("OKX_MAX_ATTEMPTS") or 2))
RETRY_DELAY = int(os.environ.get("OKX_RETRY_DELAY") or 15)   # seconds before a failed job is retried

# ★ X402_DISABLE=1 is for **local smoke tests only**: skip payment and exercise the business path.
#   Why it is needed: on its first request the payment middleware calls the OKX facilitator's
#   /supported, and wrong credentials take the whole thing down (measured: HTTP 401
#   "Invalid OK-ACCESS-KEY") — so **without credentials not one line of business code runs**, and
#   even a self-test is impossible.
#   ⚠️ Never enable it in production: that would give the service away for free, so startup shouts
#      about it.
#   ⚠️ And the facilitator must **not be constructed at all** when it is off — the SDK validates
#      non-empty credentials in its constructor, so a top-level construction stops the smoke mode
#      from even starting (measured 2026-09-20: ValueError).
X402_OFF = os.environ.get("X402_DISABLE") == "1"

if not PAY_TO and not X402_OFF:
    raise SystemExit("PAY_TO_ADDRESS is not set — a paid service cannot start without a payee address")

facilitator = server = None
if not X402_OFF:
    facilitator = OKXFacilitatorClient(OKXFacilitatorConfig(
        auth=OKXAuthConfig(api_key=os.environ.get("OKX_API_KEY", ""),
                           secret_key=os.environ.get("OKX_SECRET_KEY", ""),
                           passphrase=os.environ.get("OKX_PASSPHRASE", "")),
        base_url=os.environ.get("OKX_BASE_URL", "https://web3.okx.com"),
        sync_settle=True))
    server = x402ResourceServer(facilitator)
    server.register(NETWORK, ExactEvmScheme())

SERVICE_DESC = ("Solana address trading-behavior forensics: returns a fully cited, "
                "machine-verified profile of how one address trades")
PARAMS = {
    "address": {"type": "string", "required": True,
                "meaning": "Solana wallet address (base58, 32-44 chars)"},
    "days":    {"type": "integer", "required": False, "default": 2,
                "meaning": "window length in days, 1-%d" % MAX_DAYS},
    "end":     {"type": "string", "required": False, "default": "yesterday (UTC)",
                "meaning": "window end date, YYYY-MM-DD (UTC)"},
    "chain":   {"type": "string", "required": False, "default": "solana",
                "meaning": "only 'solana' is supported today"},
}


# ★ The 402 body must **declare the input parameters**, or the buyer CLI replays with an empty body
#   and we answer 400. Measured 2026-09-21: `payment quote --param address=...` put the parameters
#   only into knownParams, `paramPlan` was empty and `merchantBody` was "{}", so the paid replay
#   carried none of them.
#   The official rule (payments SKILL.md, "Source 2 — non-Bazaar"): without a Bazaar
#   outputSchema.input, **the CLI assembles parameters only if the response body lists
#   required / params / parameters / fields / inputSchema**; otherwise "ambiguous → add nothing,
#   replay unchanged". The Python SDK's PaymentRequirements has no outputSchema field, so the body
#   is the only route.
UNPAID_BODY = {
    "error": "Payment required",
    "method": "POST",
    "inputSchema": {
        "type": "object",
        "properties": {k: {"type": v["type"], "description": v["meaning"]}
                       for k, v in PARAMS.items()},
        "required": [k for k, v in PARAMS.items() if v.get("required")],
    },
    "parameters": PARAMS,
    "required": [k for k, v in PARAMS.items() if v.get("required")],
}
# ⚠️ The SDK wants Callable[[HTTPRequestContext], HTTPResponseBody]; both levels are easy to get
#    wrong (each hit once on 2026-09-21):
#      passing a dict       → TypeError: 'dict' object is not callable
#      returning a bare dict → AttributeError: 'dict' object has no attribute 'content_type'

ROUTES = {"POST /profile": RouteConfig(
    # ⚠️ Give the timeout room. A full call (Dune cache miss + first draft + 2 rewrites) has been
    #    measured at 455 seconds; 300 would expire the payment challenge before the work finishes.
    accepts=[PaymentOption(scheme="exact", price=PRICE, network=NETWORK, pay_to=PAY_TO,
                           max_timeout_seconds=int(os.environ.get("X402_TIMEOUT") or 900))],
    resource=(PUBLIC_URL + "/profile") if PUBLIC_URL else None,
    unpaid_response_body=lambda _ctx: HTTPResponseBody(
        content_type="application/json", body=UNPAID_BODY),
    description=SERVICE_DESC, mime_type="application/json")}

app = FastAPI(title="Onchain Address Forensics", description=SERVICE_DESC)

if X402_OFF:
    print("!!! X402_DISABLE=1 — payment middleware is OFF. Local smoke tests only, never production !!!",
          flush=True)
else:
    app.add_middleware(PaymentMiddlewareASGI, routes=ROUTES, server=server)


# ═══ Rate limit, free routes only ════════════════════════════════════════════
# /, /health, /spec and /report are unauthenticated and free. /health in particular used to reach an
# upstream service on every request. POST /profile is deliberately exempt: it is already gated by
# payment, and throttling someone who has paid would be the wrong kind of protection.
RL_MAX = int(os.environ.get("OKX_RL_PER_MIN") or 60)
_rl = {}
_rllock = threading.Lock()


def _rate_ok(ip):
    now = int(time.time() // 60)
    with _rllock:
        if len(_rl) > 10000:                       # a flood from many IPs must not grow without bound
            for k, v in list(_rl.items()):
                if v[0] != now: _rl.pop(k, None)
        minute, n = _rl.get(ip, (now, 0))
        if minute != now:
            minute, n = now, 0
        n += 1
        _rl[ip] = (minute, n)
        return n <= RL_MAX


@app.middleware("http")
async def _throttle(request: Request, call_next):
    if request.method == "POST" and request.url.path.rstrip("/") == "/profile":
        # ★ Validate **before** the payment middleware issues the 402 challenge (OKX listing review,
        #   2026-09-22). The body has to be read here and replayed downstream, because the payment
        #   middleware is an ASGI app reading the raw receive channel — consuming it without putting
        #   it back would hang the request.
        raw = await request.body()
        async def _replay():
            return {"type": "http.request", "body": raw, "more_body": False}
        request._receive = _replay
        try:
            parsed = json.loads(raw or b"{}")
        except Exception:
            return _bad("body must be valid JSON")
        if not isinstance(parsed, dict):
            return _bad("body must be a JSON object")
        merged = _merge(parsed, dict(request.query_params))
        print("[probe] POST /profile paid=%s params=%s"
              % (bool(request.headers.get("payment-signature") or request.headers.get("x-payment")),
                 json.dumps(merged, ensure_ascii=False)[:200]), flush=True)
        vals, err = _parse(merged)
        if err:
            return err               # 400 before any challenge — nobody pays for a doomed request
    if request.method != "POST":
        # Behind Caddy the peer is the proxy, so the client is the first hop in X-Forwarded-For.
        ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
              or (request.client.host if request.client else "-"))
        if not _rate_ok(ip):
            return JSONResponse(status_code=429, content={
                "error": "too many requests",
                "limit_per_minute": RL_MAX,
                "hint": "Poll a report_url about once a minute; it is not ready sooner."})
    return await call_next(request)


@app.exception_handler(Exception)
async def _on_error(request: Request, exc: Exception):
    """★ Report facilitator credential/network problems separately from real server bugs.
    Both used to surface as a bare 500, so ops could not tell a misconfigured key from a crash."""
    name = type(exc).__name__
    if "Facilitator" in name or "OKX API error" in str(exc):
        return JSONResponse(status_code=503, content={
            "error": "payment facilitator unavailable",
            "detail": str(exc)[:300],
            "hint": "check OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE and OKX_BASE_URL"})
    return JSONResponse(status_code=500, content={"error": name, "detail": str(exc)[:300]})



@app.get("/")
async def index():
    """Free. ★ Added 2026-09-22: the bare domain used to answer FastAPI's default
    {"detail":"Not Found"}, which is what a judge, a marketplace crawler or a curious caller sees
    first. A service that cannot say what it is at its own root looks broken."""
    return {"service": SERVICE_DESC,
            "docs": "https://github.com/sansheng777/solana-address-forensics",
            "price_per_call": PRICE, "network": NETWORK, "chain": "solana",
            "routes": {
                "GET /": "this page",
                "GET /health": "liveness: the x402 facilitator, and how Dune and the model "
                               "behaved on the last real call (?deep=1 probes them directly)",
                "GET /spec": "parameter schema and the shape of the report",
                "POST /profile": "the paid call (x402); see /spec for parameters",
                "GET /report/{token}": "free pickup for a report a paid call started"},
            "example": ("onchainos payment quote %s/profile --method POST "
                        "--param address=<solana address> --param days=2"
                        % (PUBLIC_URL or "https://<this host>")),
            "note": "Read-only. This service never trades and never signs anything."}


_FAC = {"at": 0.0, "value": "unknown"}
_FAC_TTL = 60


def _facilitator_status():
    """Ping the facilitator, but **at most once a minute**. /health is free and unauthenticated, so
    one ping per request means anyone with a loop is hammering OKX's endpoint through us — a good
    way to get rate limited on the path that takes the money."""
    if X402_OFF:
        return "disabled"
    if time.time() - _FAC["at"] < _FAC_TTL:
        return _FAC["value"]
    try:
        facilitator.get_supported()
        _FAC["value"] = "ok"
    except Exception as e:
        _FAC["value"] = "FAIL: %s" % str(e)[:200]
    _FAC["at"] = time.time()
    return _FAC["value"]


def _probe(url, headers, name):
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return {"ok": 200 <= r.status < 300, "http": r.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "error": e.read()[:120].decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120])}


def _deep():
    """Really call Dune and the model vendor. Both endpoints are metadata reads: Dune's query
    definition costs no credits (credits are charged for executions and result reads) and
    OpenRouter's /key returns the account's own limits. Kept behind ?deep=1 so the cheap /health
    stays cheap."""
    out = {}
    dk = os.environ.get("DUNE_API_KEY", "")
    out["dune"] = ({"ok": False, "error": "DUNE_API_KEY is not set"} if not dk else
                   _probe("https://api.dune.com/api/v1/query/%s"
                          % os.environ.get("DUNE_SCRATCH_QID", "8376415"),
                          {"X-Dune-API-Key": dk}, "dune"))
    ok_ = os.environ.get("OPENROUTER_API_KEY", "")
    out["model"] = ({"ok": False, "error": "OPENROUTER_API_KEY is not set"} if not ok_ else
                    _probe("https://openrouter.ai/api/v1/key",
                           {"Authorization": "Bearer " + ok_}, "model"))
    return out


@app.get("/health")
async def health(deep: int = 0):
    """Free. ★ It really pings the facilitator (cached for 60s) — a credential mismatch has to be
    visible here, not at the moment a user pays.
    ★ 2026-09-22: the facilitator was the *only* thing checked, while the two dependencies that
    actually make a paid call fail are Dune and the model. `last_call` reports how each of them
    behaved on the last real job (free, no probing), and `?deep=1` probes them for real."""
    fac = _facilitator_status()
    body = {"status": "ok" if fac in ("ok", "disabled") else "degraded",
            "network": NETWORK, "price": PRICE, "chain": "solana",
            "facilitator": fac, "pay_to": PAY_TO,
            "engine_version": ENGINE_VERSION,
            "workers": {"max": MAX_WORKERS, "queued": _Q.qsize(),
                        "running": sum(1 for j in JOBS.values() if j.get("status") == "running")},
            "credentials_present": {k: bool(os.environ.get(k)) for k in
                                    ("DUNE_API_KEY", "OPENROUTER_API_KEY", "OKX_API_KEY")},
            "last_call": {k: (v and {**v, "ago_seconds": round(time.time() - v["at"])})
                          for k, v in LAST.items()}}
    if deep:
        d = _deep()
        body["deep"] = d
        if not all(x.get("ok") for x in d.values()):
            body["status"] = "degraded"
    return body


@app.get("/spec")
async def spec():
    """The machine-readable form of the parameter schema; the [Parameter Spec] section required at
    registration is its prose version."""
    return {"service": SERVICE_DESC, "method": "POST", "path": "/profile",
            "parameters": PARAMS, "price_per_call": PRICE, "network": NETWORK,
            # The report cache is keyed by this too, so a caller can tell which engine wrote a report
            "engine_version": ENGINE_VERSION,
            # measured in production 2026-09-22: 378 s for a 2-day cold window, 653 s for 5 days
            "typical_seconds": [380, 660],
            # ★ The official buyer CLI reads for 30 seconds, so a paid call hands back a pickup
            #   token instead of waiting (see how_it_works)
            "how_it_works": {
                "cached": "POST /profile returns the full report immediately (~0.02s).",
                "cold": "POST /profile returns {status:'computing', token, report_url} in <1s "
                        "and computes in the background.",
                "pickup": "GET /report/{token} is FREE and returns the report when ready; "
                          "poll every ~60s. The payment already covers the computation.",
            },
            # 2026-09-21: the body is three fixed sections; a caller decides from `edge` alone
            "report_shape": {
                "profile": "what this address does (hold time, exit style, curve position)",
                "result": "whether it makes money (win rate + denominator, net PnL, concentration, trend)",
                "edge": {"source": "execution | selection | both | none",
                         "say": "evidence for the source",
                         "follower": "where a follower would end up; which part is observable"},
                "facts": "evidence pool; every number in the three sections resolves here"},
            "note": "Read-only. This service never trades and never signs anything."}


def _shift(d, n):
    t = calendar.timegm(time.strptime(d, "%Y-%m-%d")) + n * 86400
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def _bad(msg, **extra):
    return JSONResponse(status_code=400, content=dict(error=msg, parameters=PARAMS, **extra))


def _merge(body, q):
    """Body wins, query string fills the gaps — a buyer may put parameters in either."""
    if not isinstance(body, dict): body = {}
    return {k: (body.get(k) if body.get(k) not in (None, "") else q.get(k))
            for k in set(body) | set(q)}


def _parse(p):
    """Validate the parameters. Returns (values, None) or (None, a 400 response).

    ‼️ This runs **before the 402 challenge is issued** (see the middleware). The OKX listing review
       rejected the service on 2026-09-22 for exactly this: "参数缺失或错误" was only reported after
       the buyer had signed and been charged. Nobody should pay for a request that cannot succeed,
       so the same function now guards both sides of the payment.
    """
    addr = str(p.get("address") or "").strip()
    if not addr:
        return None, _bad("address is required: a base58 Solana address (32-44 chars)")
    if not B58.match(addr):
        return None, _bad("address must be a base58 Solana address (32-44 chars)", got=addr[:64])
    chain = str(p.get("chain") or "solana").lower()
    if chain != "solana":
        return None, _bad("only chain='solana' is supported today", got=chain)
    try:
        days = int(p.get("days") or 2)
    except (TypeError, ValueError):
        return None, _bad("days must be an integer", got=str(p.get("days"))[:32])
    if not 1 <= days <= MAX_DAYS:
        return None, _bad("days must be between 1 and %d" % MAX_DAYS, got=days)
    end = str(p.get("end") or "").strip() or time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    try:
        time.strptime(end, "%Y-%m-%d")
    except ValueError:
        return None, _bad("end must be YYYY-MM-DD (UTC)", got=end[:32])
    return {"address": addr, "days": days, "end": end, "chain": chain}, None



# ═══ Background jobs: the official buyer CLI reads for 30s, one call of ours takes 90-450s ═══
# Measured 2026-09-21: on the paid replay Caddy logged `status 0 / dur 30.0s` (the client hung up),
# the payment never settled and no report came back. **This is not a knob we control** — the CLI has
# no timeout switch.
# → So `POST /profile` must answer within seconds: a cache hit returns the report, a miss **starts
#   the work in the background and hands back a pickup token**, which the caller redeems at the free
#   `GET /report/{token}`. The payment bought the computation; picking it up is not charged again.
JOBS = {}                      # key → {"status","started","result","attempts"}
TOKENS_F = os.path.join(OUT, "tokens.json")
CALLS_F = os.path.join(OUT, "calls.jsonl")      # one line per paid call: what it cost and how it ended
_jlock = threading.Lock()      # guards JOBS
_tlock = threading.Lock()      # guards tokens.json: read-modify-write has to be one operation
TOKEN_TTL = int(os.environ.get("OKX_TOKEN_TTL_DAYS") or 30) * 86400
# The outcome of the **last real call** to each dependency. /health reports it, which is how a dead
# Dune key or an empty model account becomes visible without probing (and without costing anything).
LAST = {"dune": None, "model": None}


def _atomic_write(path, obj):
    """Write via a temp file and rename. ‼️ `json.dump(open(path,"w"))` leaves a truncated file if the
    process dies mid-write, and this file is the only record of which tokens were handed out — a
    corrupt one turns every outstanding token into "unknown token" for callers who already paid."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _tokens():
    """Read the token table. A corrupt file is **moved aside, not silently ignored** — otherwise the
    next write would overwrite it with {} and the loss would leave no trace."""
    try:
        return json.load(open(TOKENS_F, encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        try:
            os.replace(TOKENS_F, TOKENS_F + ".corrupt-%d" % int(time.time()))
            print("!!! tokens.json was unreadable (%s), moved aside" % e, flush=True)
        except OSError:
            pass
        return {}


def _token_put(token, rec):
    """Add one token. Under the lock, because read-modify-write from two paid calls at once would
    drop one of them — and that caller has already been charged."""
    with _tlock:
        tk = _tokens()
        now = time.time()
        tk = {k: v for k, v in tk.items() if now - (v.get("created") or 0) < TOKEN_TTL}
        tk[token] = rec
        _atomic_write(TOKENS_F, tk)


def _record_call(**kw):
    """Append one line per paid call. Without this there is no answer to "what did today cost" —
    store.py accumulates the Dune credits and the agent returns the model spend, and both used to be
    dropped on the floor when the report was assembled."""
    kw["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        os.makedirs(os.path.dirname(CALLS_F), exist_ok=True)
        with open(CALLS_F, "a", encoding="utf-8") as f:      # O_APPEND: one short line is atomic
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _model_usd(meta):
    """The model spend of one report = the first draft plus every targeted rewrite."""
    tot = float(meta.get("cost") or 0)
    for r in meta.get("repair") or []:
        tot += float(((r or {}).get("meta") or {}).get("cost") or 0)
    return round(tot, 6)


def _run_job(addr, d0, d1, key):
    """The part that actually works (fetch → tables → model → verify → render). Synchronous and
    blocking; it runs on a worker thread."""
    t0 = time.time()
    import store, features, run as agent, verify, render
    agent._load_env()
    # ★ The cache directory must be handed to store explicitly — it is a module global that only
    #   the CLI's --cache sets. Found by review 2026-09-21: without this line **every call really
    #   queried Dune**, about 25 credits and 270 seconds each. A 4,000/month quota would be gone in
    #   160 calls.
    store.CACHE_DIR = CACHE
    try:
        raw = store.profile(addr, d0, d1)                     # ① fetch (Dune)
        LAST["dune"] = {"ok": True, "at": time.time()}
    except Exception as e:
        LAST["dune"] = {"ok": False, "at": time.time(), "error": str(e)[:200]}
        _record_call(key=key, address=addr, window=[d0, d1], outcome="dune_failed",
                     seconds=round(time.time() - t0, 1), error=str(e)[:200])
        return {"status": "failed", "error": "upstream data query failed", "detail": str(e)[:400]}
    dune_credits = round(float((raw.get("cost") or {}).get("credits") or 0.0), 4)
    tables = features.build(raw)                              # ② tables (pure local)
    if not (tables.get("based_on") or {}).get("coins"):
        _record_call(key=key, address=addr, window=[d0, d1], outcome="no_closed_trades",
                     dune_credits=dune_credits, seconds=round(time.time() - t0, 1))
        return {"address": addr, "window": [d0, d1], "status": "no_closed_trades",
                "note": "This address has no complete buy→sell round inside the window. "
                        "Try a longer `days`.", "tables": tables,
                "cost": {"dune_credits": dune_credits, "model_usd": 0.0}}
    try:
        rep, viol, meta = agent.generate(tables, stem=os.path.join(OUT, key), log=lambda *a: None)
        LAST["model"] = {"ok": True, "at": time.time()}
    except Exception as e:                                    # ③ model (incl. targeted rewrites)
        LAST["model"] = {"ok": False, "at": time.time(), "error": str(e)[:200]}
        _record_call(key=key, address=addr, window=[d0, d1], outcome="model_failed",
                     dune_credits=dune_credits, seconds=round(time.time() - t0, 1),
                     error=str(e)[:200])
        return {"status": "failed", "error": "report generation failed",
                "detail": str(e)[:400], "tables": tables,
                "cost": {"dune_credits": dune_credits, "model_usd": 0.0}}

    # ★ Two independent verdicts, both reported. `passed` is the conjunction, because a caller who
    #   reads one boolean must not get "true" while the fetch layer's own invariants were violated
    #   (before 2026-09-22 the layer-0-3 selfcheck only sat inside tables.based_on, where nobody
    #   looked).
    sc = (tables.get("based_on") or {}).get("selfcheck")
    data_ok = (sc == "passed")
    out = {
        "address": addr, "window": [d0, d1], "chain": "solana",
        "report": rep,                                        # structured JSON for the calling agent
        "markdown": render.render(rep, tables),               # for a human, sources as endnotes
        "tables": tables,                                     # the layer-3 tables, so a caller can recheck
        "verification": {
            "passed": bool(not viol and data_ok),
            "report_checks": {"passed": not viol, "count": verify.CHECK_COUNT, "violations": viol},
            "data_selfcheck": {"passed": data_ok,
                               "violations": [] if data_ok else (sc if isinstance(sc, list) else [sc])},
            "engine_version": ENGINE_VERSION,
            "what_it_guarantees":
                "Every number in the report resolves to a row in `tables`; no arithmetic, "
                "no vague quantifiers, no trading advice. It does NOT guarantee the "
                "conclusions are the right ones to draw.",
        },
        "cost": {"dune_credits": dune_credits, "model_usd": _model_usd(meta)},
        "cached": False, "seconds": round(time.time() - t0, 1),
    }
    _atomic_write(os.path.join(OUT, key + ".json"), out)
    _record_call(key=key, address=addr, window=[d0, d1], outcome="ok",
                 dune_credits=dune_credits, model_usd=out["cost"]["model_usd"],
                 seconds=out["seconds"], verified=out["verification"]["passed"])
    return out


# ═══ A fixed worker pool, not a thread per job ═══════════════════════════════
# One job is a Dune query plus a model call, so running many at once burns the Dune monthly quota and
# trips upstream rate limits — and, before the lock in store.py, made the shared scratch query race
# a certainty. Jobs above MAX_WORKERS wait in a queue; nothing is refused, because the caller has
# already paid.
_Q = queue.Queue()


def _worker():
    while True:
        addr, d0, d1, key = _Q.get()
        with _jlock:
            j = JOBS.setdefault(key, {"status": "queued", "started": time.time(), "attempts": 0})
            j["status"] = "running"
            j["attempts"] = j.get("attempts", 0) + 1
            j["running_since"] = time.time()
        try:
            r = _run_job(addr, d0, d1, key)
        except Exception as e:
            r = {"status": "failed", "error": type(e).__name__, "detail": str(e)[:400]}
        failed = isinstance(r, dict) and r.get("status") == "failed"
        with _jlock:
            j = JOBS.get(key) or {}
            attempts = j.get("attempts", 1)
            retry = failed and attempts < MAX_ATTEMPTS
            JOBS[key] = {"status": "queued" if retry else "done",
                         "started": j.get("started", time.time()),
                         "attempts": attempts, "result": None if retry else r}
        if retry:
            # ★ The caller already paid, so a failure is retried for free rather than returned.
            #   The delay matters: most failures here are a busy or rate-limited upstream, and an
            #   immediate retry just fails again. The timer keeps the worker free while it waits.
            print("job %s failed (attempt %d/%d), retrying in %ds: %s"
                  % (key, attempts, MAX_ATTEMPTS, RETRY_DELAY,
                     str(r.get("detail") or r.get("error"))[:120]), flush=True)
            threading.Timer(RETRY_DELAY, _Q.put, args=((addr, d0, d1, key),)).start()
        _Q.task_done()


for _i in range(MAX_WORKERS):
    threading.Thread(target=_worker, daemon=True, name="okx-worker-%d" % _i).start()


def _start_job(addr, d0, d1, key):
    """Queue the work. The same (address, window) is never queued twice."""
    with _jlock:
        st = (JOBS.get(key) or {}).get("status")
        if st in ("queued", "running"):
            return
        JOBS[key] = {"status": "queued", "started": time.time(), "attempts": 0, "result": None}
    _Q.put((addr, d0, d1, key))


@app.get("/report/{token}")
async def report(token: str):
    """Free pickup. The paid call already bought this computation; collecting it is not charged."""
    t = _tokens().get(token)
    if not t:
        return JSONResponse(status_code=404, content={
            "error": "unknown token",
            "hint": "Tokens come from a paid POST /profile response."})
    key = t["key"]
    done = os.path.join(OUT, key + ".json")
    if os.path.exists(done):
        out = json.load(open(done, encoding="utf-8"))
        out["cached"] = True
        return out
    with _jlock:
        job = dict(JOBS.get(key) or {})
    if job.get("status") == "done":
        return job["result"]
    if job.get("status") in ("queued", "running"):
        return {"status": "computing",
                "queue_position": _Q.qsize() if job["status"] == "queued" else 0,
                "attempt": job.get("attempts", 1),
                "elapsed_seconds": round(time.time() - job["started"], 1),
                "retry_after_seconds": 60,
                "note": "Still working. A cold address takes 90-450s (Dune query + model + "
                        "up to 3 verification rewrites); a queued job is waiting for a free worker."}
    # The service restarted and the job was lost → restart it under the same token, still free
    _start_job(t["address"], t["window"][0], t["window"][1], key)
    return {"status": "computing", "elapsed_seconds": 0.0, "retry_after_seconds": 60,
            "note": "Restarted the job (service had been restarted). No extra charge."}


@app.post("/profile")
async def profile(req: Request):
    # ★ Reaching here means **payment already happened**. So: check the cache first, report every
    #   failure honestly, and never swallow one silently.
    try:
        raw = await req.json()
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        return _bad("body must be a JSON object")
    # The middleware already validated this before taking payment; re-run it here so the handler is
    # still correct on its own (and for X402_DISABLE=1 local runs).
    vals, err = _parse(_merge(raw, dict(req.query_params)))
    if err: return err
    addr, days, end = vals["address"], vals["days"], vals["end"]
    d0, d1 = _shift(end, -(days - 1)), end

    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    # ★ ENGINE_VERSION is part of the key: after a deploy that changes the prompt, the verifier or
    #   a layer-3 table, the old report is no longer "the answer to this question" and must not
    #   come back with `cached: true`.
    key = hashlib.sha256(("%s|%s|%s|%s" % (addr, d0, d1, ENGINE_VERSION)).encode()).hexdigest()[:16]
    done = os.path.join(OUT, key + ".json")
    if os.path.exists(done):
        # This (address, window) was computed before — return it and burn neither Dune
        # credits nor model spend again
        out = json.load(open(done, encoding="utf-8"))
        out["cached"] = True
        out["seconds"] = round(time.time() - t0, 1)
        return out

    # Cache miss → start in the background and hand back the pickup token within seconds
    # (the CLI waits only 30)
    _start_job(addr, d0, d1, key)
    token = secrets.token_hex(16)
    tk = _tokens(); tk[token] = {"key": key, "address": addr, "window": [d0, d1],
                                 "created": time.time()}
    json.dump(tk, open(TOKENS_F, "w", encoding="utf-8"), ensure_ascii=False)
    base = PUBLIC_URL or ""
    return JSONResponse(status_code=200, content={
        "address": addr, "window": [d0, d1], "chain": "solana",
        "status": "computing",
        "token": token,
        "report_url": (base + "/report/" + token) if base else ("/report/" + token),
        "retry_after_seconds": 60,
        "note": "A cold address takes 90-450s (Dune query + model + up to 3 verification "
                "rewrites), which is longer than most HTTP clients wait. Poll `report_url` "
                "— it is free, the payment already covers this computation.",
        "seconds": round(time.time() - t0, 1),
    })

