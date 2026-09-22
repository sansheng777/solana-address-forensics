# okx/service — the public A2MCP endpoint (paid through x402)

One HTTP service: `POST /profile` takes a Solana address and returns a three-section report (what he
does / does he make money / where his edge is and whether a follower can get it), plus the
verification result and the raw layer-3 tables. Unpaid requests get a 402; the replayed, signed
request gets a 200.

| Route | Paid | What it does |
|---|---|---|
| `GET /` | free | What this service is, its routes and its price — the bare domain used to answer `{"detail":"Not Found"}` |
| `GET /health` | free | Is it alive? **It really pings the facilitator** (cached 60s), reports how Dune and the model behaved on the last real job, the worker queue and the engine version. `?deep=1` probes Dune and the model directly |
| `GET /spec` | free | Parameter schema + `report_shape`, for registration and for callers |
| `POST /profile` | **x402** | `{"address", "days" (1-7, default 2), "end" (YYYY-MM-DD, default yesterday)}` |
| `GET /report/{token}` | free | Collect a report a paid call started (see "Why a paid call returns a token") |

## Local smoke test (no payment involved)

```bash
docker build -f okx/service/Dockerfile -t meme/okx-service .
docker run -d --name okx-svc --env-file okx/.env --env-file dune/.env \
    -e X402_DISABLE=1 -e PAY_TO_ADDRESS= \
    -v /root/meme/okx/data:/app/okx/data -p 127.0.0.1:4021:4021 meme/okx-service
curl -s localhost:4021/health
curl -s -X POST localhost:4021/profile -H 'content-type: application/json' \
    -d '{"address":"<addr>","days":5,"end":"2026-09-18"}'
```

With `X402_DISABLE=1` no facilitator is constructed, no payee address is needed, and `/profile`
returns the report directly. **Local use only.**

## Deploying (three steps)

Prerequisites: Docker and the compose plugin on the server, an A record pointing at it, and ports
80/443 open. **Do not use a Hong Kong node** — the AI vendors this service calls refuse connections
from there.

```bash
# 1. Pack (locally) — only engine/agent/sql/service/dune/lib, with a credential scan afterwards
sh okx/service/pack.sh                      # → okx-service-<date>.tar.gz
scp okx-service-*.tar.gz user@server:~/

# 2. Unpack and fill in credentials (on the server)
mkdir -p ~/okx-service && cd ~/okx-service && tar -xzf ~/okx-service-<date>.tar.gz
cp okx/service/.env.example okx/service/.env && chmod 600 okx/service/.env
vi okx/service/.env                         # see "What to fill in" below

# 3. Start
DOMAIN=your.domain docker compose -f okx/service/docker-compose.yml up -d --build
curl https://your.domain/health             # facilitator must read "ok"
```

### What to fill in (`okx/service/.env`)

| Variable | Where it comes from |
|---|---|
| `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE` | the OKX Developer Portal (x402 seller credentials) |
| `PAY_TO_ADDRESS` | an EVM address on X Layer to receive payment |
| `X402_NETWORK` | **must be `eip155:196` (mainnet)** — testnet is unsupported, see below |
| `PUBLIC_URL` | the public https address. **Without it the 402 challenge advertises `http://`** (the protocol the container sees), which breaks both the buyer's replay and marketplace validation |
| `X402_PRICE` | price per call, e.g. `$0.10` |
| `DUNE_API_KEY` | Dune (data) |
| `OPENROUTER_API_KEY` + `OKX_AGENT_MODEL` + `OKX_AGENT_EFFORT` | the model (`deepseek/deepseek-v4-flash` / `high`) |

### ‼️ Mainnet only — testnet does not work (measured 2026-09-21)

Converting a fiat price like `$0.10` requires the payment SDK to know that network's default
stablecoin, and `x402/mechanisms/evm/constants.py` **registers one for mainnet only**:

```
eip155:1952  FAIL  ValueError: No default stablecoin configured for network eip155:1952
eip155:196   OK    asset=0x779ded... (USD₮0), amount=100000 ($0.10 at 6 decimals)
```

On testnet the middleware raises at that point, so `POST /profile` answers **500 instead of 402**.
Staying on testnet would mean supplying a test token address through `register_money_parser`, which
the official docs do not provide. **Use mainnet.**

### Why a paid call returns a pickup token

The official buyer CLI gives up on the HTTP read after **30 seconds** (measured 2026-09-21: Caddy
logged `status 0 / dur 30.0s` with the PAYMENT-SIGNATURE header present — the payment never settled
and the caller got nothing). A cold address needs 90-450 seconds, and the client has no timeout
switch.

So `POST /profile` answers within a second:

```
cache hit  → the full report
cache miss → {"status":"computing", "token":"...", "report_url":"https://.../report/<token>"}
             and the work starts in the background

GET /report/{token}   free; returns the report when ready, poll roughly every 60s
```

The payment bought the computation; collecting it is not charged again. If the service restarts and
loses the job, the same token restarts it, still free.

### Verifying payment

Use the official buyer CLI rather than assembling headers by hand (`quote` is free and moves no
money):

```bash
onchainos payment quote https://your.domain/profile --method POST \
    --param address=<addr> --param days=2      # → paymentId + "Will pay 0.1 USDT (exact, X Layer)"
onchainos payment pay --payment-id <id> --param address=<addr> --param days=2 --yes
```

⚠️ `--param` must be passed to **`pay` as well**. The 402 body declares the input schema so the CLI
can assemble the parameters, but without repeating them on `pay` the replay carries an empty body
(measured 2026-09-21).

## Cost and quota per call

| Item | Amount | Notes |
|---|---|---|
| Dune | 2-61 credits (varies by launchpad and by how active the address is; dbc is the most expensive). **Measured in production 2026-09-22: 60.87 credits** for a 2-day cold window on a busy pump.fun address | cached per (address, window) in the `okx_data` volume; the second call costs nothing |
| Model | ≈ $0.01 (first draft + 2-3 targeted rewrites) | the report is cached at `okx/data/_svc/<key>.json`; the second call returns `cached: true` |
| Time | 90-450 s | longest on a Dune cache miss plus three rewrites; the proxy and the x402 timeout are both set to 900 s |

Every paid call appends one line to `okx/data/_svc/calls.jsonl` — address, window, outcome, Dune
credits, model dollars, seconds, whether verification passed. Each report also carries its own
`cost` block. **Without this there is no answer to "what did today cost"**: store.py accumulates the
credits and the agent returns the model spend, and until 2026-09-22 both were dropped when the
report was assembled.

## Production behaviour

| Concern | What the service does | Knob |
|---|---|---|
| **Concurrency** | A fixed worker pool. A job above the limit queues rather than starting — each job is a Dune query plus a model call, so the limit is about the monthly quota and upstream rate limits, not CPU. Nothing is refused: the caller has already paid | `OKX_MAX_WORKERS` (2) |
| **Wrong-data race** | ‼️ One Dune scratch query serves every call, so `store.run` rewrites its SQL and executes it. Two jobs doing that at once meant one executed the other's SQL and **silently got the wrong address's data**. Every Dune round trip is now serialised behind a process-wide lock | `DUNE_SCRATCH_QID` |
| **A stuck Dune execution** | Waiting for an execution is bounded; a query that never finishes releases the lock instead of blocking every other caller for ever | `DUNE_WAIT_DEADLINE` (900s) |
| **A failed paid call** | Retried for free after a delay, then the failure is returned with its cause. Failures are never written to the report cache | `OKX_MAX_ATTEMPTS` (2), `OKX_RETRY_DELAY` (15s) |
| **Pickup tokens** | Written under a lock, through a temp file and a rename. A half-written `tokens.json` would turn every outstanding token into "unknown token" for callers who already paid; an unreadable one is moved aside, not silently replaced. Tokens older than the TTL are pruned on write | `OKX_TOKEN_TTL_DAYS` (30) |
| **Stale reports after a deploy** | The cache key includes `ENGINE_VERSION`, a digest of the layer-3 builder, the fetch layer, the prompt, the verifier and the three word lists. Change any of them and the next call recomputes instead of returning yesterday's answer with `cached: true` | — |
| **Free endpoints abused** | `/`, `/health`, `/spec` and `/report` are rate limited per IP (the client IP is taken from `X-Forwarded-For`). `POST /profile` is exempt — it is already gated by payment | `OKX_RL_PER_MIN` (60) |
| **Paying for a doomed request** | ‼️ The OKX listing review rejected the service on 2026-09-22 because a missing or malformed parameter was only reported **after** the buyer had signed and been charged. Validation now runs in the outer middleware, **before the 402 challenge is issued**: a bad request gets a 400 carrying the full parameter spec and no payment is ever requested. Measured the same day: the buyer CLI's discovery probe **does carry the parameters** (`[probe] POST /profile paid=False params={"days":"2","address":"4vw54Bm…"}`), so validating that early costs nothing — a valid probe still gets its 402 | — |
| **Data-layer failures** | The fetch layer's 14 invariants (`store.selfcheck`) are reported in the response as `verification.data_selfcheck`, and `verification.passed` is the conjunction of that and the 13 report checks. Before 2026-09-22 a caller could read `passed: true` while the fetch layer's own invariants had been violated | — |

Not solved, stated: there is no refund path — x402 settles before the work runs, so a call that fails
after `MAX_ATTEMPTS` has still been charged. The failure and its cause are returned in full.

## Files

| File | What it is |
|---|---|
| `app.py` | FastAPI + the x402 seller side; `/profile` chains store → features → agent.generate → verify → render |
| `Dockerfile` | python:3.12-slim, the only image with pip dependencies (the two traps are documented in requirements.txt) |
| `docker-compose.yml` + `Caddyfile` | for the server: the service listens on the container network only, Caddy terminates HTTPS |
| `pack.sh` | builds the deployment tarball and scans it for credentials |
| `.env.example` | the credential template |

## ⚠️ A deployment trap worth knowing (2026-09-21)

On one provider the service came up fine but **Let's Encrypt could not issue a certificate**, failing
three rounds with `Timeout during connect (likely firewall problem)`.

The server itself was clean — `ufw` inactive, `iptables` policy ACCEPT, docker-proxy listening on
both ports — and `tcpdump` over 100 seconds caught **not one inbound SYN**, while outbound traffic to
Let's Encrypt was fine. Three external parties (Let's Encrypt, a crawler, a proxy service) all timed
out; a high port was equally unreachable. Inbound was being filtered upstream.

**Why it looked fine at first**: the machine used to test it happened to sit in the same provider's
network, so those connections never left it.

→ **Verify public reachability from a third party on a different network.** Your own curl proving it
works does not count.

Do not work around this with a CDN proxy or a tunnel: those paths impose a ~125-second timeout, and
one call here takes 90-450 seconds.
