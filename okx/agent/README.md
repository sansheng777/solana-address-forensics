# Layer 4 — the report agent

**A layer-3 JSON goes in → an external model reads it → a mechanically verified report comes out.**
It is not a chat agent; it only produces a result.

## Files

| File | What it is | Needs credentials |
|---|---|---|
| `prompt.md` | The prompt: three-section skeleton plus the method for reading a table | — |
| `SCHEMA.md` | The output contract: `facts` as an evidence pool + `profile` / `result` / `edge` (enforced by `verify.py`) | — |
| `forbidden.txt` | Banned words, one per line, editable directly | — |
| `vague.txt` | Vague quantifiers ("most", "roughly N"), one per line | — |
| `follower-banned.txt` | Bans applied **only inside `edge.follower`** (copy-this-checklist content) | — |
| `run.py` | Calls the external model → stores artefacts → verifies and renders on the spot | **yes** |
| `verify.py` | **Thirteen** mechanical checks (see SCHEMA.md; number 12 was retired, the ids are not renumbered) | no |
| `render.py` | Report JSON → Markdown for a human (sources as endnotes) | no |
| `tests/test_agent.py` | Offline: twelve violation classes + the skeleton + ranges + digest robustness + rendering, 106 assertions | no |
| `tests/test_robust.py` | Offline: the **network layer** (timeout / retry / truncation / dropping parameters), 14 assertions | no |
| `out/` | Artefacts of each run (`.report.json` / `.md` / `.raw.txt` / **`.meta.json`** with cost) | — |

**Only `run.py` needs model credentials.** The verifier, the renderer and both self-tests run
offline — which is why the framework could be validated before any key existed.

## The report skeleton (fixed 2026-09-21)

A caller holding an address asks exactly one thing: **is this person worth following?** Split into
three questions, which are the report's three sections, in a fixed order:

| Section | Question | Source |
|---|---|---|
| `profile` | What he does | the largest hold-time bucket sets the type; number of sells, venue, curve progress |
| `result` | Does he make money | win rate + denominator, net PnL, concentration on both sides, trend |
| `edge` | Where his edge is, can a follower get it | `source` ∈ execution / selection / both / none; `say` the evidence; `follower` the position they end up in and what is observable |

`facts` moved into an appendix of sources. **There is no free-form "interpretation" or "advice"
section** — in the earlier four-section version the model picked different tables each run,
mentioning a selection edge once and not the next time, and presenting "coins bought 5+ times have a
higher win rate" as a finding. With three fixed sections it can only answer the three questions.
The method for reading a table (the single test, decision vs outcome-driven variables, progress as
the only yardstick, execution vs selection) is all still there, as the derivation manual for the
third section.

## Running it

```bash
# Offline: both self-tests, the verifier fixtures, the renderer
docker run --rm -v /root/meme:/work -w /work node:22 python3 okx/agent/tests/test_agent.py
docker run --rm -v /root/meme:/work -w /work node:22 python3 okx/agent/tests/test_robust.py
docker run --rm -v /root/meme:/work -w /work node:22 python3 okx/agent/verify.py \
    okx/agent/tests/fixtures/good.report.json okx/data/_f4_d1_4vw54B.json

# With a model (copy okx/.env.example to okx/.env, fill in the key, chmod 600)
./bin/agent-py /work/okx/agent/run.py /work/okx/data/_f4_4vw54B.json --tag round1
```

## The development loop

```
edit prompt.md  →  run.py calls the external model  →  verify.py checks it  →  a human reads it  →  edit again
                          ↓
                    out/ keeps every round, so "what changed after this prompt edit" is comparable
```

> **★ The report must be produced by an external model actually reading the layer-3 JSON. We never
> hand-write one** — a hand-written report proves a person can write it, not that the prompt works.
> The two files under `tests/fixtures/` are **test data for the verifier**, not agent output.

## Known slack (stated, not hidden)

- Numbers are compared by **absolute value**, so **a sign error is invisible** (calling a loss a
  gain). Closing it means making the model carry the sign in `say`, which is a prompt matter.
- The checks enforce **that every sentence has a source**; they cannot enforce **that the sentence is
  worth making** — that is what a human reviewer is for.
- Change layer 3 and the `tests/fixtures/` digests go stale; rerun `tests/make_fixtures.py`
  (the suite's first assertion checks exactly this).

## Four robustness fixes in the network layer (2026-09-20, each covered by `tests/test_robust.py`)

| # | Fixed | What used to happen |
|---|---|---|
| ① | **timeouts caught separately, with exponential back-off** (5→10→20s) | only `HTTPError` was caught, while a timeout raises `URLError`/`TimeoutError` and **escaped the retry logic entirely**. DeepSeek takes 120-210s per call, not far from the 300s timeout |
| ② | **`HTTPError` must be caught before `OSError`** | `HTTPError` ⊂ `URLError` ⊂ `OSError`; in the wrong order a 4xx is retried as a network error |
| ③ | **truncation doubles the budget and retries** (ceiling 64K) | reasoning length floats (measured 5.5K-20.7K on one model), so **"pick a big enough number" is not robustness, only a number that happens to fit today** |
| ④ | **three failure modes reported separately** | endpoint down / truncated / model wrote it wrong all printed as "no parsable JSON", which hides which one it was |

Every run also writes `.meta.json` (model / duration / budget / truncated / reasoning tokens /
cost), so **cost is traceable**.

## Language

Everything the model reads and writes is **English**: the layer-3 table keys and row labels, the
prompt, the contract, the three word lists, and the report. Before 2026-09-22 the whole pipeline was
Chinese; the switch was made because the marketplace, the listing and the callers are international.
A side effect worth knowing: English has word boundaries, so the four word-boundary bugs the Chinese
word lists needed regex work-arounds for simply do not exist here.

## Model choice (measured 2026-09-20, compared by violation count from `verify.py`)

| Model | Violations | Cost per run | Duration |
|---|---|---|---|
| Haiku 4.5 | 7 | $0.04 | 37s |
| Sonnet 5 | **0** (both addresses) | $0.26 | 144-152s |
| **deepseek/deepseek-v4-flash** (in use) | **0** after targeted rewrites | **$0.01** | 90-260s |

## Reasoning control (verified against the official docs 2026-09-20 — the earlier conclusion was wrong)

OpenRouter takes reasoning through the unified object `reasoning: {...}`, **not a top-level
`reasoning_effort`** (that is the OpenAI-native style). Getting this wrong silently does nothing,
which is how a wrong conclusion was drawn from it once.

```
deepseek/deepseek-v4-flash → mandatory:false, supported_efforts:[xhigh, high], default_effort:high
```

It has **no low/medium/minimal** — passing them changes nothing; but `mandatory:false` means it can
be switched off entirely. `OKX_AGENT_EFFORT=off` disables it, any of the supported levels selects
one, and leaving it unset uses the model's default.

### Production settings (fixed 2026-09-20 by measuring violation counts)

```
OKX_AGENT_MODEL=deepseek/deepseek-v4-flash
OKX_AGENT_EFFORT=high
OKX_AGENT_UPSTREAM=Alibaba      # pinned: the only upstream that supports `seed`, and the two
                                # malformed-JSON responses ever seen both came from another one
```
