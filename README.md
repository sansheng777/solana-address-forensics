# Solana address forensics — a paid agent-to-agent endpoint

**One Solana address in, one machine-verified report out: what this trader does, whether he makes
money, where his edge is, and whether someone copying him could get it.**

It is a tool other agents buy, not a chatbot. The endpoint speaks [x402](https://x402.org) over
OKX's X Layer, so an agent that holds an address can pay for one report and read it without a human
in the loop.

```
POST /profile  {"address": "<base58>", "days": 5, "end": "2026-09-18"}   $0.10, X Layer mainnet
```

Live: **https://agent.stochedge.com** (`GET /health` and `GET /spec` are free and unauthenticated).

---

## What comes back

Three fixed sections, in this order, because a caller holding an address asks exactly one question —
*is this person worth watching?* — and it splits into exactly three:

| Section | Question |
|---|---|
| `profile` | what does he do |
| `result` | does he make money |
| `edge` | where is his edge, and can a follower get it |

An excerpt from a real report (address `4vw54B…`, 5 days, 585 coins):

> He holds between 15 seconds and 5 minutes: 226 coins (38.6%) held 15-60s and 182 (31.1%) held
> 1-5m. He exits in a single sell on 77.1% of coins. […] 544 coins (93.0%) have first buy USD
> 150-350. *(sources [5][6][7][8][9][3])*
>
> Win rate 61.4% on 585 coins; net +54,273. Winners total +73,183, losers -18,911. […] Win rates per
> day fluctuate (65.0%, 60.0%, 61.0%, 57.8%, 64.6%) with no monotonic trend. *(sources [14][15][3])*
>
> **Edge comes from: selection (which coins)** — coins bought at 10-20%, 20-30% and 30-40% bonding
> curve progress have win rates of 100% (21 coins), 81.0% (42) and 83.9% (56). *(sources
> [11][12][6][7][1][5])*
>
> **Where a follower would end up:** he is out within minutes; a follower enters after he has left
> and may take the other side of his sells. What is observable are the coins he buys at 10-40%
> progress — but a follower's entry will be later.

Every bracketed number is a footnote to a fact, and every fact carries the exact table row it was
read from. The appendix prints them.

## Why it is unusual: the report is mechanically verified, not trusted

A model that reads a table will, sooner or later, write a number the table does not contain. So the
model's output is checked by a program before anything is returned —
**[13 checks](okx/agent/SCHEMA.md)**, and a failed check triggers a targeted rewrite of only the
offending sentence.

A few of them, each written because of a specific failure that got through:

| Check | What it prevents |
|---|---|
| every number in a fact must appear in the row that fact cites | arithmetic, crossed tables, merged buckets |
| every number in the body must have been written out by one of the facts it cites | "the 62 coins at 10~40% progress" — 62 came from a different table and 10~40 merged three buckets; every digit was individually legal, the sentence was not |
| no vague quantifier anywhere | rewriting "207 coins" as "most coins" leaves nothing to check — the front door around the check above |
| a `layer3_digest` that must equal the hash of the data actually passed in | a report that belongs to different data |
| when the edge is execution, the follower section must say a follower cannot get it | making the judgement and then not passing it to the caller |

There is no advice section, no score, no traffic lights. The report says what happened, with
sources, and stops.

## Architecture

```
Dune (Solana raw tables)
   │  layer 0-1   okx/sql/*.sql        fills, transfers, coin metadata, decimals, prices
   ▼
okx/engine/store.py                    attribution: rebuild every fill, reconcile the money leg,
   │                                   convert to USD, mark what could not be seen
   ▼
okx/engine/features.py                 layer 3: 13 blocks, 29 tables — hold-time buckets, entry/exit curve
   │                                   progress, buy/sell ladder, action combos, PnL concentration.
   │                                   A pure script: no judgement, no model
   ▼
okx/agent/{prompt.md, run.py}          layer 4: an external model reads layer 3 and writes the report
   │
   ├─ okx/agent/verify.py              13 mechanical checks
   ├─ targeted rewrite of failures     (up to 3 rounds)
   └─ okx/agent/render.py              JSON + Markdown
```

The split matters: **layers 0-3 contain no judgement and no model**, so every number in a report is
reproducible from the data by running a script. The model's only job is to choose what is worth
saying and to say it — and even that is bounded by the checks.

### Coverage: three launchpads, not one

Measured over 165,123 Solana launchpad traders: covering only pump.fun gives a complete picture for
68.8% of them, misses fills for 16.6%, and **answers "no activity found" for 14.5%** — so
pump.fun, Meteora DBC and Raydium LaunchLab are all decoded, inner market and post-graduation venue
alike. The SQL headers in [okx/sql/](okx/sql/) document every trap found along the way: DBC's
`trade_direction` is 0=sell 1=buy; DBC charges its fee on the *output* token, so a buy's fee is in
the meme coin; pump's outer venue reports user-side amounts that already have the fee removed, so
subtracting it again inflated five days of PnL from 873 to 1,670.

## Running it yourself

Everything runs in a container; nothing is installed on the host.

```bash
# The offline self-tests — no credentials, no network
docker run --rm -v "$PWD":/work -w /work python:3.12-slim python3 okx/agent/tests/test_agent.py    # 106 assertions
docker run --rm -v "$PWD":/work -w /work python:3.12-slim python3 okx/agent/tests/test_robust.py   #  14 assertions
docker run --rm -v "$PWD":/work -w /work python:3.12-slim python3 okx/tests/test_store.py          #  37 assertions
docker run --rm -v "$PWD":/work -w /work python:3.12-slim python3 okx/tests/xcheck.py              # conservation check
docker run --rm -v "$PWD":/work -w /work python:3.12-slim python3 okx/agent/tests/doccheck.py      # docs vs code

# The service layer's own self-test — needs the service image (fastapi lives only there)
docker build -f okx/service/Dockerfile -t okx-service .
docker run --rm -e X402_DISABLE=1 -e OKX_ROOT=/app -v "$PWD":/app -w /app okx-service \
    python3 okx/tests/test_service.py                                                             #  14 assertions

# The verifier, against the shipped sample layer-3 data
docker run --rm -v "$PWD":/work -w /work python:3.12-slim python3 okx/agent/verify.py \
    okx/agent/tests/fixtures/good.report.json okx/data/_f4_d1_4vw54B.json

# The service, locally, with payment switched off
docker build -f okx/service/Dockerfile -t okx-service .
docker run --rm -e X402_DISABLE=1 -p 127.0.0.1:4021:4021 okx-service
curl -s localhost:4021/spec
```

`okx/tests/xcheck.py` is worth a look: it recounts layer 3 against the raw fills in seven
conservation groups (coin counts, shares summing to 100, PnL totals, sample coins existing), so a
bucket table that silently drops a row fails the build rather than the report.

A full run needs a [Dune](https://dune.com) key and an [OpenRouter](https://openrouter.ai) key;
deployment additionally needs OKX x402 seller credentials. See
[okx/service/README.md](okx/service/README.md).

## Cost and latency per call

| | |
|---|---|
| Dune | 2-61 credits for a cold address (measured 60.87 in production on a busy address, 2 days), cached per (address, window) |
| Model | ≈ $0.01 — `deepseek/deepseek-v4-flash`, one draft plus targeted rewrites |
| Time | 90-450 s cold, ~0.02 s cached |

The service is built for being left running: a fixed worker pool (jobs above the limit queue rather
than starting), every Dune round trip serialised because one scratch query serves them all, a bounded
wait so a stuck query cannot block everyone, free retries for a paid call that failed, pickup tokens
written atomically under a lock, a cache key that includes a digest of the engine that produced the
report, and a per-IP rate limit on the free routes. Each of those is one line in
[okx/service/README.md](okx/service/README.md) with the failure it prevents. Every call appends its
Dune credits and model dollars to `calls.jsonl`.

Because the official x402 buyer client gives up on an HTTP read after 30 seconds, a paid call that
cannot be served from cache returns a pickup token in under a second and computes in the background;
`GET /report/{token}` collects it, free, and the same token restarts the job if the service was
restarted. That design and the reason for it are in
[okx/service/README.md](okx/service/README.md).

## Layout

| Path | What it is |
|---|---|
| [okx/sql/](okx/sql/) | the Dune queries, one file each, every header documenting what it scans and what it cost |
| [okx/engine/store.py](okx/engine/store.py) | attribution — fills, money legs, USD, what could not be seen |
| [okx/engine/features.py](okx/engine/features.py) | layer 3 — the tables the model reads |
| [okx/agent/](okx/agent/) | the prompt, the output contract, the 13 checks, the renderer ([README](okx/agent/README.md)) |
| [okx/service/](okx/service/) | the FastAPI + x402 endpoint and its deployment ([README](okx/service/README.md)) |
| [okx/tests/](okx/tests/), [okx/agent/tests/](okx/agent/tests/) | the offline self-tests, including the service layer's |
| [dune/lib/dune_client.py](dune/lib/dune_client.py) | a minimal Dune client, standard library only |
| `okx/data/` | three sample layer-3 / attribution files so the tests and the verifier run out of the box |

Standard library only, apart from the service image, which needs FastAPI and the x402 SDK.

## Known limits, stated rather than hidden

- **The window is 1-7 days.** Positions opened before it have unknown cost and are excluded from PnL;
  the report says how many.
- **Not every fill is visible.** Coins traded on a DEX that is not decoded, or not fully sold inside
  the window, are counted and reported as such — a report always states what it could not see.
- Numbers are checked by absolute value, so **a sign error would pass**. Making the model carry the
  sign is a prompt change, not a checker change.
- The checks enforce that every sentence has a source. They cannot enforce that a sentence is worth
  making.
- Dune is minutes behind the chain. This is forensics, not a real-time trigger.

## License

MIT — see [LICENSE](LICENSE).
