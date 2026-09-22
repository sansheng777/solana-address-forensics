#!/usr/bin/env python3
"""okx/agent/run.py — layer 4: hand the layer-3 JSON to an **external model**, take the report back
and verify it on the spot.

    ./bin/agent-py /work/okx/agent/run.py <layer3.json> [--model X] [--tag name]

★ The report must be produced by an external model actually reading the layer-3 JSON.
  **We never hand-write one** — a hand-written report proves a person can write it, not that the
  prompt works.

Three parts are concatenated and sent:
    ① okx/agent/prompt.md    the prompt
    ② okx/agent/SCHEMA.md    the output contract (enforced by verify.py)
    ③ the layer-3 JSON       features.py's output, with its digest, which the model copies back

Credentials live in okx/.env (chmod 600, gitignored):
    ANTHROPIC_API_KEY=...        or        OPENAI_API_KEY=...   or   OPENROUTER_API_KEY=...
    OKX_AGENT_MODEL=deepseek/deepseek-v4-flash   (optional; falls back to the defaults below)

Standard library only (urllib) — the same discipline as the rest of the engine.
"""
import http.client, json, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
DEFAULT = {"anthropic": "claude-haiku-4-5-20251001", "openai": "gpt-4o-mini"}   # cheapest tier;
# to change model, edit OKX_AGENT_MODEL in okx/.env
# ⚠️ Newer models enable extended thinking by default, and **thinking counts against max_tokens**.
#    Measured on claude-sonnet-5: a budget of 8000 → thinking consumed all 8000 and not one word of
#    the answer came out; 24000 → thinking took 20667 and the answer was still truncated.
#    → **Give thinking its own explicit budget**, so what remains always belongs to the answer.
MAX_TOKENS = int(os.environ.get("OKX_AGENT_MAX_TOKENS") or 32000)
THINK_TOKENS = int(os.environ.get("OKX_AGENT_THINK_TOKENS") or 10000)


def _load_env():
    """Read okx/.env (run.py may also run inside a container where the variables are already set)."""
    p = os.path.join(os.path.dirname(HERE), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _pick(model):
    """Pick the model first, then the provider — **a slash in the model name means it is an
    OpenRouter slug** (vendor/model). Switching provider then needs no code change and no second
    PROVIDER variable to keep in sync with the model name (two places always drift apart)."""
    model = model or os.environ.get("OKX_AGENT_MODEL")
    if model and "/" in model:
        if not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("the model name %r has a slash (an OpenRouter slug), but okx/.env has no "
                     "OPENROUTER_API_KEY" % model)
        return "openrouter", model
    for env, prov in (("ANTHROPIC_API_KEY", "anthropic"), ("OPENAI_API_KEY", "openai")):
        if os.environ.get(env):
            return prov, (model or DEFAULT[prov])
    sys.exit("okx/.env has no model credential at all (ANTHROPIC_API_KEY / OPENAI_API_KEY / "
             "OPENROUTER_API_KEY).\n   (The rest of the framework needs no key: verify.py and "
             "render.py run offline.)")


class ModelError(Exception):
    """A failure on the model's side. **Report the three kinds separately** instead of printing
    "no parsable JSON" for all of them — when debugging you need to see at a glance whether the
    endpoint is down, the answer was truncated, or the model wrote it wrong."""


def _post(url, body, headers, tries=4, timeout=300):
    """Retry on failure. **No concurrency, no batching** — the same discipline this project applies
    to GMGN and RPC.

    ⚠️ Two ordering traps:
    1. `HTTPError` ⊂ `URLError` ⊂ `OSError`, so **HTTPError must be caught first**; otherwise a
       4xx/5xx is retried as if it were a network error.
    2. **A timeout does not raise HTTPError.** Before 2026-09-20 only HTTPError was caught, so a
       timeout escaped the whole retry path and raised — and DeepSeek takes 160s per call, not far
       from the 300s timeout. The network layer is now caught separately, with back-off.

    ⚠️ Do not hard-code which models reject which parameter: the error text names the parameter, so
    drop it and resend."""
    delay = 5
    for i in range(tries):
        last = i + 1 >= tries
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise ModelError("the endpoint returned something that is not JSON (first 200 chars): %s" % raw[:200])
        except urllib.error.HTTPError as e:                     # ← must come before OSError
            msg = e.read().decode()[:400]
            drop = next((k for k in ("temperature", "thinking", "top_p",
                                     "response_format", "seed", "reasoning",
                                     "provider")
                         if k in body and k in msg), None)
            if drop:
                print("   this model rejects %s, dropping it and resending" % drop)
                body = {k: v for k, v in body.items() if k != drop}
                continue
            if e.code in (408, 429, 500, 502, 503, 504, 529) and not last:
                print("   HTTP %d, retrying in %ds: %s" % (e.code, delay, msg))
                time.sleep(delay); delay *= 2; continue
            raise ModelError("endpoint error HTTP %d: %s" % (e.code, msg))
        except (OSError, http.client.HTTPException) as e:       # timeout / dropped / half a response
            if not last:
                print("   network layer failed (%r), retrying in %ds" % (e, delay))
                time.sleep(delay); delay *= 2; continue
            raise ModelError("network layer failed %d times in a row: %r" % (tries, e))
    raise ModelError("no response after %d retries" % tries)


def call_model(system, user, model=None, budget=None):
    """Returns (text, meta). **meta carries `truncated`** — what to do about truncation is the
    caller's decision, and truncation is never conflated with "the model wrote it wrong"."""
    prov, model = _pick(model)
    budget = budget or MAX_TOKENS
    t0 = time.time()
    if prov == "anthropic":
        r = _post("https://api.anthropic.com/v1/messages",
                  dict(model=model, max_tokens=budget, temperature=0,
                       thinking=dict(type="enabled", budget_tokens=THINK_TOKENS),
                       system=system, messages=[dict(role="user", content=user)]),
                  {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                   "anthropic-version": "2023-06-01", "content-type": "application/json"})
        txt = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
        usage = r.get("usage") or {}
        trunc = r.get("stop_reason") == "max_tokens"
        think = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
        cost, upstream = None, None
    else:
        # OpenRouter is OpenAI-compatible; only the URL and the auth header differ
        url, key = (("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY")
                    if prov == "openrouter" else
                    ("https://api.openai.com/v1/chat/completions", "OPENAI_API_KEY"))
        # ★ Three free wins on OpenRouter (an unsupported parameter is dropped by _post based on
        #   the error text):
        #   response_format → forces valid JSON, which removes the "no parsable JSON" failure mode
        #   seed            → two calls on the same address agree; determinism is part of the pitch
        #   reasoning       → thinking control, see the OKX_AGENT_EFFORT note below.
        body = dict(model=model, temperature=0, max_tokens=budget,
                    messages=[dict(role="system", content=system),
                              dict(role="user", content=user)])
        if prov == "openrouter":
            body["response_format"] = dict(type="json_object")
            body["seed"] = int(os.environ.get("OKX_AGENT_SEED") or 20260920)
            # ★ Reasoning control goes through OpenRouter's unified object `reasoning: {...}`,
            #   **not a top-level reasoning_effort** (that is the OpenAI-native style). Passing the
            #   wrong one silently does nothing, which is how a wrong conclusion was drawn once.
            #   Source: openrouter.ai/docs/use-cases/reasoning-tokens
            #
            #   ⚠️ Supported levels differ per model; check the `reasoning` object in
            #      GET /api/v1/models:
            #     deepseek/deepseek-v4-flash → mandatory:false, supported_efforts:[xhigh,high],
            #                                  default_effort:high
            #     → it has **no low/medium/minimal** (passing them does nothing), but
            #       mandatory:false means it **can be switched off entirely**.
            #   OKX_AGENT_EFFORT=off disables it; xhigh/high selects a level; unset uses the default.
            eff = os.environ.get("OKX_AGENT_EFFORT")
            if eff == "off":
                body["reasoning"] = dict(enabled=False)
            elif eff:
                body["reasoning"] = dict(effort=eff)
            # ★ OpenRouter routes the same model to different upstream providers, whose speeds
            #   differ a lot; measured 63-77 tok/s on the default route, and nearly all the time is
            #   spent generating tokens. sort=throughput picks a fast one;
            #   OKX_AGENT_ROUTE=default restores the default.
            # ★ Prefer pinning one upstream. OKX_AGENT_UPSTREAM takes comma-separated names; empty
            #   falls back to sorting. allow_fallbacks=True means a dead first choice still yields a
            #   result rather than failing the whole call.
            #
            #   Why Alibaba is pinned (observed 2026-09-20 — **not a controlled experiment, and the
            #   samples are uneven**):
            #       Alibaba    30 runs  mean reasoning 6342  mean final violations 0.0 (n=23)  72.9s
            #       Baidu      11 runs  mean reasoning 3805  mean final violations 0.4 (n=5)   47.2s
            #       StreamLake  3 runs  mean reasoning 9046  mean final violations 0.0 (n=3)  231.9s
            #   Baidu is faster and cheaper, but reasons about 60% as much and shows more
            #   violations; **and both malformed-JSON responses ever seen came from Baidu**
            #   (zero in 30 Alibaba runs). The second point is a hard signal, the first only an
            #   observation.
            #   An independent reason: **Alibaba supports `seed` and Baidu does not** — determinism
            #   is part of this product's pitch, which is enough on its own.
            up = os.environ.get("OKX_AGENT_UPSTREAM", "Alibaba")
            route = os.environ.get("OKX_AGENT_ROUTE", "throughput")
            if up:
                body["provider"] = dict(order=[x.strip() for x in up.split(",") if x.strip()],
                                        allow_fallbacks=True)
            elif route != "default":
                body["provider"] = dict(sort=route)
        r = _post(url, body,
                  {"Authorization": "Bearer " + os.environ[key],
                   "content-type": "application/json",
                   "X-Title": "okx-address-profile"})
        if r.get("error"):
            raise ModelError("endpoint returned an error: %s" % json.dumps(r["error"], ensure_ascii=False)[:400])
        ch = (r.get("choices") or [{}])[0]
        txt = (ch.get("message") or {}).get("content") or ""
        usage = r.get("usage") or {}
        trunc = ch.get("finish_reason") == "length"
        think = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        cost = usage.get("cost")
        # ★ OpenRouter reports **which upstream actually served the call** at the top of the
        #   response. Without recording it there is no way to tell whether "the reasoning length
        #   keeps changing" is the model's own behaviour or a different provider — a conclusion was
        #   once drawn without that evidence (2026-09-20).
        upstream = r.get("provider")
    return txt, dict(provider=prov, upstream=upstream if prov == "openrouter" else None,
                     model=model, seconds=round(time.time() - t0, 1),
                     budget=budget, truncated=trunc, reasoning_tokens=think, cost=cost,
                     usage=usage)


def _parse(txt):
    """The model may still wrap the JSON in a ``` fence — strip it. If it cannot be stripped, report
    it as it is; never guess."""
    s = txt.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < 0: return None
    try:
        return json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None


MAX_BUDGET = int(os.environ.get("OKX_AGENT_MAX_BUDGET") or 64000)


def generate(layer3, model=None, stem=None, log=print):
    """★ One layer-3 JSON in → (report, violations, meta) out. **The CLI and the service share this
    one path.**

    Two implementations always drift — this project has paid for that before (ENTRY_FIELDS was
    written out in two places, a field was added to one of them, and it went missing silently). So
    generation, the truncation retry and the targeted rewrites all live here; `main()` and the
    service are thin shells.

    With `stem`, artefacts are written to disk (.raw.txt / .report.json / .md / .meta.json); without
    it everything stays in memory.
    With `log`, progress is printed; the service passes `lambda *a: None` (its stdout is a protocol
    channel and must stay clean).
    """
    sys.path.insert(0, HERE)
    import verify, render
    dg = verify.digest(layer3)
    system = (open(os.path.join(HERE, "prompt.md"), encoding="utf-8").read()
              + "\n\n" + open(os.path.join(HERE, "SCHEMA.md"), encoding="utf-8").read())
    user = ("Layer-3 JSON (digest=%s — copy this digest verbatim into layer3_digest):\n\n%s"
            % (dg, json.dumps(layer3, ensure_ascii=False, indent=1)))

    def _save(ext, text):
        if stem: open(stem + ext, "w", encoding="utf-8").write(text)

    # ── If truncated, double the budget and retry (with a ceiling) ──
    # Reasoning length floats (measured 5.5K-20.7K on one model), so **"pick a big enough number" is
    # not robustness, only a number that happens to fit today**.
    budget, txt, meta = MAX_TOKENS, None, None
    while True:
        txt, meta = call_model(system, user, model, budget)     # ModelError is the caller's to handle
        log("<- %s / %s  upstream %s  %.1fs  budget %d  reasoning %s  cost %s"
            % (meta["provider"], meta["model"], meta.get("upstream"), meta["seconds"],
               meta["budget"], meta["reasoning_tokens"], meta["cost"]))
        if not meta["truncated"]:
            break
        nxt = min(budget * 2, MAX_BUDGET)
        if nxt <= budget:
            _save(".raw.txt", txt or "")
            raise ModelError("truncated, and the budget is already at its ceiling %d (raise OKX_AGENT_MAX_BUDGET)" % MAX_BUDGET)
        log("   truncated; budget %d -> %d, retrying" % (budget, nxt))
        budget = nxt

    _save(".raw.txt", txt)
    rep = _parse(txt)
    if rep is None:
        # ★ The endpoint answered and nothing was truncated → the model wrote it wrong.
        #   **Reported separately from the other two failure modes**
        raise ModelError("the endpoint answered normally and nothing was truncated, but the content is not valid JSON — the model wrote it wrong")

    v = verify.check(rep, layer3)
    for x in v: log("   " + x)

    # ── Targeted rewrite: hand the violations back and let it change only those places ──
    # Why not "generate it again": regeneration is another roll of the dice and can introduce new
    # errors
    # (measured: 3 violations, and after a prompt edit a rerun bounced to 14). A targeted rewrite
    # carries the previous version in its input, so the blast radius is limited to the named
    # sentences — **it is patching, not rethinking.**
    # ★ With the "adopt only if strictly better" guard, adding a round is **monotonically not
    #   worse**; but a ceiling is mandatory — without one it burns money forever and the model can
    #   oscillate between two errors.
    rounds = []
    # Ceiling of 3 rounds (raised from 2 on 2026-09-20): one address went X -> 3 -> 1, still falling
    # monotonically when the old ceiling of 2 cut it off. The guards are "adopt only if strictly
    # better" and "stop when it stops improving", so extra rounds can only help or do nothing; the
    # cost is roughly 20-60s and $0.002 per round.
    for rnd in range(1, int(os.environ.get("OKX_AGENT_MAX_REPAIR") or 3) + 1):
        if not v or os.environ.get("OKX_AGENT_REPAIR", "1") == "0":
            break
        log("-> targeted rewrite, round %d: handing back %d violation(s) to fix in place" % (rnd, len(v)))
        # ⚠️ Use replace, not str.format — the report JSON is full of braces and format would treat
        #    them as placeholders and blow up. This project has hit that before (a {txs} inside an
        #    SQL comment was substituted and took the whole query down).
        rp = (open(os.path.join(HERE, "repair.md"), encoding="utf-8").read()
              .replace("<<<PREVIOUS REPORT>>>", "## Your previous report\n\n```json\n%s\n```"
                       % json.dumps(rep, ensure_ascii=False, indent=1))
              .replace("<<<VIOLATIONS>>>", "## Violations found by the verifier (%d in total)\n\n%s"
                       % (len(v), "\n".join("- " + x for x in v))))
        try:
            # ★ Task first, data second. The other way round, the model reads 11K tokens of data
            #   and is already in "write the report" mode, so the later "change only these" cannot
            #   hold it — measured: a rewrite with **0 reasoning tokens** that echoed the input back.
            txt2, meta2 = call_model(system, rp + "\n\n" + user, model, budget)
        except ModelError as e:
            log("   rewrite call failed (%s) — keeping the previous version" % e)
            break
        log("<-(rewrite) upstream %s  %.1fs  reasoning %s  cost %s"
            % (meta2.get("upstream"), meta2["seconds"], meta2["reasoning_tokens"], meta2["cost"]))
        rep2 = None if meta2["truncated"] else _parse(txt2)
        rounds.append(dict(round=rnd, meta=meta2))
        if rep2 is None:
            log("   the rewrite produced no valid JSON (or was truncated) — keeping the previous version")
            break
        v2 = verify.check(rep2, layer3)
        log("   %d violation(s) after the rewrite" % len(v2))
        if len(v2) >= len(v):
            # Stop when it stops improving: the input has not changed, so another round spends the
            # same money on the same input
            log("   no improvement (%d -> %d) — reverting and stopping the rewrites" % (len(v), len(v2)))
            break
        if stem:
            json.dump(rep, open(stem + ".r%d.report.json" % rnd, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        rep, v = rep2, v2
        for x in v: log("   " + x)

    meta["repair"] = rounds
    meta["violations"] = v
    if stem:
        json.dump(rep, open(stem + ".report.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        _save(".md", render.render(rep, layer3))
        json.dump(meta, open(stem + ".meta.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return rep, v, meta


def main():
    _load_env()
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    l3path, model, tag = a[0], None, ""
    for i, x in enumerate(a):
        if x == "--model": model = a[i + 1]
        if x == "--tag":   tag = "_" + a[i + 1]
    layer3 = json.load(open(l3path, encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)
    stem = os.path.join(OUT, "%s_%s%s" % (
        str(layer3.get("address", "?"))[:6], time.strftime("%m%d-%H%M%S"), tag))
    print("-> layer 3 %s  about %d coins" % (l3path, (layer3.get("based_on") or {}).get("coins", 0)))
    try:
        rep, v, meta = generate(layer3, model, stem)
    except ModelError as e:
        sys.exit("FAILED: %s\n   artefacts (if any): %s.*" % (e, stem))
    print("OK  report passed every check" if not v else "FAILED  %d violation(s) remain" % len(v))
    print("artefacts: %s.*" % stem)
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main())
