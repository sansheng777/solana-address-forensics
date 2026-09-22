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
import hashlib, json, os, re, secrets, sys, threading, time, calendar

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



@app.get("/health")
async def health():
    """Free. ★ It really pings the facilitator — a credential mismatch has to be visible here, not
    at the moment a user pays."""
    fac = "disabled" if X402_OFF else "unknown"
    if not X402_OFF:
        try:
            facilitator.get_supported()
            fac = "ok"
        except Exception as e:
            fac = "FAIL: %s" % str(e)[:200]
    return {"status": "ok" if fac in ("ok", "disabled") else "degraded",
            "network": NETWORK, "price": PRICE, "chain": "solana",
            "facilitator": fac, "pay_to": PAY_TO}


@app.get("/spec")
async def spec():
    """The machine-readable form of the parameter schema; the [Parameter Spec] section required at
    registration is its prose version."""
    return {"service": SERVICE_DESC, "method": "POST", "path": "/profile",
            "parameters": PARAMS, "price_per_call": PRICE, "network": NETWORK,
            "typical_seconds": [90, 450],
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



# ═══ Background jobs: the official buyer CLI reads for 30s, one call of ours takes 90-450s ═══
# Measured 2026-09-21: on the paid replay Caddy logged `status 0 / dur 30.0s` (the client hung up),
# the payment never settled and no report came back. **This is not a knob we control** — the CLI has
# no timeout switch.
# → So `POST /profile` must answer within seconds: a cache hit returns the report, a miss **starts
#   the work in the background and hands back a pickup token**, which the caller redeems at the free
#   `GET /report/{token}`. The payment bought the computation; picking it up is not charged again.
JOBS = {}                      # key → {"status","started","result"}
TOKENS_F = os.path.join(OUT, "tokens.json")
_jlock = threading.Lock()


def _tokens():
    try:
        return json.load(open(TOKENS_F, encoding="utf-8"))
    except Exception:
        return {}


def _run_job(addr, d0, d1, key):
    """The part that actually works (fetch → tables → model → verify → render). Synchronous and
    blocking; it runs on a background thread."""
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
    except Exception as e:
        return {"status": "failed", "error": "upstream data query failed", "detail": str(e)[:400]}
    tables = features.build(raw)                              # ② tables (pure local)
    if not (tables.get("based_on") or {}).get("coins"):
        return {"address": addr, "window": [d0, d1], "status": "no_closed_trades",
                "note": "This address has no complete buy→sell round inside the window. "
                        "Try a longer `days`.", "tables": tables}
    try:
        rep, viol, meta = agent.generate(tables, stem=os.path.join(OUT, key), log=lambda *a: None)
    except Exception as e:                                    # ③ model (incl. targeted rewrites)
        return {"status": "failed", "error": "report generation failed",
                "detail": str(e)[:400], "tables": tables}

    out = {
        "address": addr, "window": [d0, d1], "chain": "solana",
        "report": rep,                                        # structured JSON for the calling agent
        "markdown": render.render(rep, tables),               # for a human, sources as endnotes
        "tables": tables,                                     # the layer-3 tables, so a caller can recheck
        "verification": {
            "passed": not viol, "violations": viol,
            "checks": verify.CHECK_COUNT,
            "what_it_guarantees":
                "Every number in the report resolves to a row in `tables`; no arithmetic, "
                "no vague quantifiers, no trading advice. It does NOT guarantee the "
                "conclusions are the right ones to draw.",
        },
        "cached": False, "seconds": round(time.time() - t0, 1),
    }
    json.dump(out, open(os.path.join(OUT, key + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
    return out


def _start_job(addr, d0, d1, key):
    """Start the work in the background. The same (address, window) is never started twice."""
    def _go():
        try:
            r = _run_job(addr, d0, d1, key)
        except Exception as e:
            r = {"status": "failed", "error": type(e).__name__, "detail": str(e)[:400]}
        with _jlock:
            JOBS[key] = {"status": "done", "started": JOBS.get(key, {}).get("started", time.time()),
                         "result": r}
    with _jlock:
        if JOBS.get(key, {}).get("status") == "running":
            return
        JOBS[key] = {"status": "running", "started": time.time(), "result": None}
    threading.Thread(target=_go, daemon=True).start()


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
    if job.get("status") == "running":
        return {"status": "computing", "elapsed_seconds": round(time.time() - job["started"], 1),
                "retry_after_seconds": 60,
                "note": "Still working. A cold address takes 90-450s (Dune query + model + "
                        "up to 3 verification rewrites)."}
    # The service restarted and the job was lost → restart it under the same token, still free
    _start_job(t["address"], t["window"][0], t["window"][1], key)
    return {"status": "computing", "elapsed_seconds": 0.0, "retry_after_seconds": 60,
            "note": "Restarted the job (service had been restarted). No extra charge."}


@app.post("/profile")
async def profile(req: Request):
    # ★ Reaching here means **payment already happened**. So: check the cache first, report every
    #   failure honestly, and never swallow one silently.
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return _bad("body must be a JSON object")
    # ★ Fill anything missing from the body out of the query string — this keeps working when a
    #   buyer puts the parameters in the URL (the body wins).
    q = dict(req.query_params)
    body = {k: (body.get(k) if body.get(k) not in (None, "") else q.get(k))
            for k in set(body) | set(q)} or body

    addr = str(body.get("address") or "").strip()
    if not B58.match(addr):
        return _bad("address must be a base58 Solana address (32-44 chars)", got=addr[:64])
    chain = str(body.get("chain") or "solana").lower()
    if chain != "solana":
        return _bad("only chain='solana' is supported today", got=chain)
    try:
        days = int(body.get("days") or 2)
    except (TypeError, ValueError):
        return _bad("days must be an integer")
    if not 1 <= days <= MAX_DAYS:
        return _bad("days must be between 1 and %d" % MAX_DAYS)
    end = str(body.get("end") or "").strip() or time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    try:
        time.strptime(end, "%Y-%m-%d")
    except ValueError:
        return _bad("end must be YYYY-MM-DD (UTC)")
    d0, d1 = _shift(end, -(days - 1)), end

    t0 = time.time()
    os.makedirs(OUT, exist_ok=True)
    key = hashlib.sha256(("%s|%s|%s" % (addr, d0, d1)).encode()).hexdigest()[:16]
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

