# okx/service — the public A2MCP endpoint (paid through x402)

One HTTP service: `POST /profile` takes a Solana address and returns a three-section report (what he
does / does he make money / where his edge is and whether a follower can get it), plus the
verification result and the raw layer-3 tables. Unpaid requests get a 402; the replayed, signed
request gets a 200.

| Route | Paid | What it does |
|---|---|---|
| `GET /health` | free | Is it alive? **It really pings the facilitator**, so a credential mismatch shows up here |
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
| Dune | 2-50 credits (varies by launchpad; dbc is the most expensive) | cached per (address, window) in the `okx_data` volume; the second call costs nothing |
| Model | ≈ $0.01 (first draft + 2-3 targeted rewrites) | the report is cached at `okx/data/_svc/<key>.json`; the second call returns `cached: true` |
| Time | 90-450 s | longest on a Dune cache miss plus three rewrites; the proxy and the x402 timeout are both set to 900 s |

One worker. Before running concurrently, deal with the Dune monthly quota (4,000 credits).

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
