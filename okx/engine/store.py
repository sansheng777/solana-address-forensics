#!/usr/bin/env python3
"""okx/engine/store.py — the fetch layer: one address in, **the facts of how it traded** out.

It only fetches and aligns definitions; it makes **no judgements** (those belong to the prompt and
the LLM). Standard library only; Dune calls reuse dune/lib/dune_client.py.

★ Product scope (set by the user 2026-09-19): **trading only — buys, sells, profit and loss.**
  No relationship graphs, no crews, no "who signed for whom", no plain transfers or airdrops.
  "Solving 80% of the problem is enough; the rest is diminishing returns."
  Earlier versions (counterparty roster / crew co-occurrence / layered batch screening) are under
  okx/archive/.

Usage (must run inside a container):
    ./bin/dune-py /work/okx/engine/store.py <address> [--days 2] [--end YYYY-MM-DD]
                                            [--json out.json] [--cache dir]
    ⚠️ Without --end the window rolls with T-1, so **every cache entry expires at midnight**
       (this burned 103 credits for nothing on 2026-09-19)

★ The three-query chain (the order is fixed; each step depends on the one before)
    01 fills     his fills, looked up by signer. DBC / LaunchLab return only a pool, no coin address
    03 receipts  the movement of **goods and money** inside each of his transactions. ⚠️ The name says
                 "receipts" but this is not "transfers in/out", and it cannot be dropped:
                 · it backfills coin addresses — 02 maps only 53% of the pools; the other 47%
                   (45.4% of address 3's fills) come from here
                 · it rebuilds outer-market fills — 41.6% of address 2's fills are rebuilt from it
                 · it gives the USD unit price — meme coins have no price feed, so the money leg's
                   amount_usd ÷ quantity is the price at that fill (at zero extra cost)
    02 metadata  ★ only for coins that **actually have fills**. Feeding it every coin in receipts
                 would query 604 coins for address 3, of which 445 (74%) are airdrops it never traded

Key definitions (every one has a source; the SQL headers under okx/sql/ carry the evidence)
    · a fill counts only when evt_tx_signer is the address itself. Delegated signing contributed
      0 fills in practice (911 / 1307 / 2731 fills, all self-signed)
    · position = 1 − curve remaining after the fill ÷ the curve's size at that coin's birth (both are
      ready-made Dune fields)
    · graduated = the coin appears in the pool-creation table
    · PnL is converted to USD and **native amounts are never summed across quote tokens** (the three
      launchpads use 543 different quote tokens between them)
    · a coin whose sold amount exceeds its bought amount gets a null PnL — the excess came in as a
      transfer and its cost is unknown
    · mayhem gets no special handling; it is passed through as a flag
"""
import hashlib
import collections, json, os, re, sys, threading, time

sys.path.insert(0, "/work/dune/lib")
import dune_client as dc

SQL_DIR     = os.environ.get("OKX_SQL_DIR", "/work/okx/sql")
SCRATCH_QID = int(os.environ.get("DUNE_SCRATCH_QID", "8376415"))
# ★ This string of 32 ones serves two purposes. **The two meanings differ but the address is the
#   same**, so it is defined once:
#   ① the System Program's address — native SOL transfers are executed by it (needed to decide
#      "is this a plain transfer")
#   ② how pump's fill events write native SOL in the `quote_mint` field
#   Consolidated 2026-09-19: it used to appear as a literal five times in this file, plus a local
#   SOL_PLACEHOLDER holding the same value — one value stored in several places is the same old
#   disease that bit twice that day (ENTRY_FIELDS, n=25).
SOL_MINT    = "11111111111111111111111111111111"

ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58, no 0OIl


class DuneError(RuntimeError):
    pass


# The coin was created before 02's lookback window (d0 − 7 days). Measured: one address had 6 pump
# outer-market coins created between 06-01 and 09-01, and extending the lookback to 180 days would
# recover 5 of 6 for 0.077 credits — **deliberately not done** (user's call ②, 2026-09-18): they are
# 2% of that address's 300 coins, while the dominant cause is LaunchLab having no creation decoder
# (92%), so recovering them would not move coverage. To change this, widen the lookback in ③ rather
# than adding a second query.
DEV_TOO_OLD = ("the coin was created before the query lookback window (02 looks back 7 days); "
               "no extra lookup is made")

DEV_NO_DECODER = ("this LaunchLab pool is not in the pool-creation event table (created before the "
                  "lookback window, or not created by the launchpad program). "
                  "⚠️ Corrected 2026-09-19: Dune decodes 26 raydium_solana tables, one of which — "
                  "raydium_launchpad_evt_poolcreateevent — carries the creator and the creation "
                  "time. The old claim that LaunchLab has no creation decoder came from an "
                  "incomplete snapshot of the table list and was wrong.")


# ★ Entry / exit and pacing fields. **Both coin-row branches attach this same set**, so it is a
#   constant — hit on 2026-09-19: the long list used to be written out in two places, and when
#   first_trade_is_sell was added only one of them was updated, so the field went missing silently
#   (no error, the value simply was not there). **A list written twice always drifts.**
ENTRY_FIELDS = (
    "first_buy_at", "last_buy_at", "first_sell_at", "last_sell_at",
    "entry_progress", "entry_venue", "entry_basis", "entry_progress_reason",
    "exit_progress", "exit_venue", "exit_progress_reason", "progress_min", "progress_max",
    "hold_seconds", "hold_reason", "n_trades",
    "entry_coin_age_sec", "entry_coin_age_reason",
    "bought_pre_graduation", "entry_vs_graduation_reason",
    "first_trade_is_sell",                      # ★ is the first fill a sell → the hard test for window_edge
)


def _chunks(values, n):
    """Split a literal list into batches of n.
    ⚠️ Dune's SQL has a 500,000-character limit (hit 2026-09-18): one mint is 46 characters and
       {mints} appears three times in 02, so a few thousand coins overflow it. Query in batches and
       merge."""
    v = [x for x in dict.fromkeys(values) if x]
    return [v[i:i + n] for i in range(0, len(v), n)] or [[]]


def _lit(values):
    """Join addresses/hashes into an SQL literal list. An empty set yields a placeholder that cannot
    match, so `in ()` never appears."""
    vals = [v for v in dict.fromkeys(values) if v]           # de-duplicate, keep order
    for v in vals:
        if "'" in v or "\\" in v:                            # these come from on-chain base58/base64;
            raise ValueError("illegal literal: %r" % v)     # a quote means something is wrong
    return ",".join("'%s'" % v for v in vals) if vals else "'-'"


CACHE_DIR = None        # enabled by --cache; only Dune responses are cached, local logic always reruns

# ‼️ **One scratch query serves every call**: run() rewrites SCRATCH_QID's SQL and then executes it.
#    Two threads doing that at once means one of them executes the other's SQL and **silently gets
#    the wrong address's data** — no error, plausible numbers, wrong answer. The service runs jobs
#    on background threads (okx/service/app.py), so this is reachable in production, not theory.
#    → Every Dune round trip is serialised here. It costs nothing: Dune is the slow, paid, external
#      step and we never wanted two of them in flight anyway.
#    ⚠️ This lock is per process. Should the service ever run more than one worker process, the
#      scratch query id must become per worker (DUNE_SCRATCH_QID) or the race returns.
_DUNE_LOCK = threading.Lock()
# An execution that stays PENDING for ever would hold that lock for ever, so waiting is bounded.
DUNE_DEADLINE = int(os.environ.get("DUNE_WAIT_DEADLINE") or 900)


def run(name, cost, **params):
    """Run okx/sql/<name>.sql, return its rows and accumulate the cost into the cost dict.

    ★ --cache: the response for one SQL text plus one set of parameters is stored and reused.
      Why: reviewing this layer almost always means changing **local** logic (backfilling, pairing,
      USD conversion), and burning 40 credits per edit is pure waste. The cache key includes the
      full SQL text, so **editing the SQL invalidates it automatically** and stale results can never
      be mistaken for fresh ones.
    """
    path = os.path.join(SQL_DIR, name if name.endswith(".sql") else name + ".sql")
    raw = open(path, encoding="utf-8").read()
    try:
        sql = raw.format(**params)
    except KeyError as e:
        # ⚠️ Hit 2026-09-19: **a {xxx} inside a comment is also a .format() placeholder**. Writing
        #   the removed {txs} into a comment as documentation broke the whole query, and the error
        #   looked like a missing argument.
        #   → Never mention an old placeholder in SQL with its braces intact; the error below says so.
        raise DuneError("%s is missing placeholder %s (given: %s). ⚠️ A {...} inside a comment counts "
                        "as a placeholder too — check the comments"
                        % (name, e, list(params)))

    ck = None
    if CACHE_DIR:
        ck = os.path.join(CACHE_DIR, "%s.%s.json" % (name, hashlib.sha1(sql.encode()).hexdigest()[:16]))
        if os.path.exists(ck):
            rows = json.load(open(ck, encoding="utf-8"))
            cost["credits"] = cost.get("credits", 0.0)      # the key must exist even on a full cache hit
            cost.setdefault("rows", {})[name] = len(rows)
            cost.setdefault("steps", []).append(dict(step=name, rows=len(rows), credits=0.0,
                                                     seconds=0.0, cached=True))
            return rows

    t0 = time.time()
    with _DUNE_LOCK:                         # see the comment on _DUNE_LOCK: the scratch query is shared
        dc.update_query(SCRATCH_QID, sql, name="okx store · %s" % name)
        st = dc.wait(dc.execute(SCRATCH_QID), poll=5, log=False, deadline=DUNE_DEADLINE)
        if st.get("state") != "QUERY_STATE_COMPLETED":
            raise DuneError("%s failed: %s" % (name, json.dumps(st.get("error"), ensure_ascii=False)[:300]))
        res  = dc.results(st["execution_id"])
    rows = (res.get("result") or {}).get("rows") or []
    meta = (res.get("result") or {}).get("metadata") or {}
    # ★ Credits come in two halves and are recorded separately (2026-09-19) — deciding whether
    #   "cutting rows or columns saves money" needs that ratio. Expensive exec = driven by the scan,
    #   so only a narrower time window helps; expensive export = driven by response size, where
    #   cutting rows or columns does work.
    c_exec = st.get("execution_cost_credits") or 0.0
    c_out  = (meta.get("result_set_bytes") or 0) / 100_000.0
    c = c_exec + c_out
    cost["credits"] = cost.get("credits", 0.0) + c
    cost.setdefault("rows", {})[name] = len(rows)
    cost.setdefault("steps", []).append(dict(step=name, rows=len(rows), credits=round(c, 4),
                                             exec_credits=round(c_exec, 4),
                                             export_credits=round(c_out, 4),
                                             bytes=meta.get("result_set_bytes"),
                                             seconds=round(time.time() - t0, 1)))
    if ck:
        os.makedirs(CACHE_DIR, exist_ok=True)
        json.dump(rows, open(ck, "w", encoding="utf-8"), ensure_ascii=False, default=str)
    return rows


def rebuild_trades(receipts, addr, money):
    """**Rebuild** fills we did not decode, out of the transfer records (the user's idea, 2026-09-19).

    Why it works: almost everything a fill record holds is also in the transfer table — direction
      (in/out), token amount, money paid, the counterparty pool (the other end of the transfer) and
      the time. The only missing field is `progress`, which is **inner-market only**, and the inner
      market is already covered 100%.

    Algorithm: within one transaction, group by **counterparty**. One counterparty with "one in, one
      out, exactly two token types" = one swap in that pool. A multi-hop route naturally splits into
      several such pairs.

    Validation (against 01's already-decoded outer-market fills as ground truth):
      direction matches **100%** (32/32, 27/27); the few mismatches in amount/pool are all **known
      definition differences** — LaunchLab's fill table gives raw values while the transfer table
      gives scaled ones (a factor of 10^6 / 10^9, see 8.66), and one calls the pool `pool_state`
      while the other names the vault account. **The rebuilt values are the scaled ones, which are
      actually more useful.**

    ⚠️ Intermediate legs must be dropped: a coin a multi-hop route passes through is not one he meant
      to buy. The test is that the coin's net change within that transaction is ≈0 (in cancels out).
      Measured on address 1: **86% of the apparent gap was intermediate legs**; only 6 coins were
      genuinely missing.
    """
    out, bytx = [], {}
    net, vol = {}, {}
    for r in receipts:
        mine = r.get("direction") in ("IN", "OUT")     # direction is already computed per address in SQL
        if not mine or r.get("amt_token") is None: continue
        tk = r.get("token")
        if tk and tk not in money:
            k = (r.get("tx_id"), tk)
            net[k] = net.get(k, 0.0) + (r["amt_token"] if r["direction"] == "IN" else -r["amt_token"])
            vol[k] = vol.get(k, 0.0) + abs(r["amt_token"])
        peer = r.get("peer")                            # composed in SQL: the payer when he receives, and vice versa
        if peer and peer != addr:
            bytx.setdefault(r.get("tx_id"), {}).setdefault(peer, []).append(r)

    for tx, peers in bytx.items():
        for peer, legs in peers.items():
            ins  = [x for x in legs if x["direction"] == "IN"]
            outs = [x for x in legs if x["direction"] == "OUT"]
            if not ins or not outs: continue
            it = {x["token"] for x in ins}; ot = {x["token"] for x in outs}
            if len(it) != 1 or len(ot) != 1: continue
            gi, go = it.pop(), ot.pop()
            if gi == go or gi is None or go is None: continue
            ai = sum(x["amt_token"] for x in ins); ao = sum(x["amt_token"] for x in outs)
            if   gi not in money and go in money: tok, buy, at, aq, q = gi, True,  ai, ao, go
            elif go not in money and gi in money: tok, buy, at, aq, q = go, False, ao, ai, gi
            else: continue                                   # coin-for-coin: neither side is money,
            #    so no price can be derived and nothing is rebuilt
            # ⚠️ Decoded fills are **not** skipped here — a decoded LaunchLab outer-market fill has
            #    amt_token permanently 0, which is exactly what the rebuilt value fills in
            #    (2026-09-19: skipping them kept filled_missing at 0 forever).
            #    De-duplication happens where the caller merges them.
            k = (tx, tok)
            if vol.get(k, 0) > 0 and abs(net.get(k, 0)) <= vol[k] * 0.01:
                continue                                     # ★ intermediate leg: net change ≈0, he only passed through
            ref = ins[0] if buy else outs[0]
            out.append(dict(token=tok, is_buy=buy, amt_token=at, amt_quote=aq,
                            quote_mint=q, quote_symbol=None, pool=peer,
                            road=None, venue="outer",        # a rebuilt fill is always outer market (inner goes through 01)
                            progress=None, progress_basis=None, curve_left=None, is_mayhem=None,
                            # ⚠️ A rebuilt fill has no signer (03 no longer returns the tx_signer
                            # column; see that file's header). We do not substitute `addr` — that
                            # would be an assumption, not a fact, and the product no longer uses the
                            # signer for any judgement.
                            signer=None, signer_note="reconstructed fill: query 03 does not return the signer",
                            router=ref.get("router"),
                            evt_tx_id=tx, evt_block_time=ref.get("block_time"),
                            evt_block_slot=ref.get("block_slot"), evt_tx_index=ref.get("tx_index"),
                            evt_outer_instruction_index=None,
                            source="reconstructed"))         # ★ kept distinct from decoded, never conflated
    out.sort(key=lambda x: (x.get("evt_block_slot") or 0, x.get("evt_tx_index") or 0))
    return out


def selfcheck(out):
    """★ Invariants checked on every call. Returns the list of violations (empty = all passed).

    Why built in rather than an external script: this is **an agent's fetch layer**, so an arbitrary
    address can arrive and it has to check and report on itself. An external script can only vouch
    for "the two addresses I tested", never for the third.

    Each one corresponds to **a hole actually fallen into**, with the documentation reference in
    parentheses.
    ⚠️ Simplified 2026-09-19: the old check ⑦ (the three roster classes not overlapping) was removed
    together with the relationship graph, and the numbering shifted up.
    """
    bad, tks, trs = [], out.get("tokens") or [], out.get("trades") or []

    seen = {}                                              # ① a coin belongs to exactly one launchpad (8.3)
    for t in tks:
        tk = t.get("token")
        if not tk: continue
        if tk in seen and seen[tk] != t.get("road"):
            bad.append("coin %s is attached to two launchpads, %s and %s" % (tk[:12], seen[tk], t.get("road")))
        seen[tk] = t.get("road")

    pools = {}                                             # ② inner pool <-> coin is one-to-one (not
    #                                                     applicable to the outer market, 8.7)
    for t in trs:
        if t.get("venue") != "inner" or not t.get("pool") or not t.get("token"): continue
        pools.setdefault(t["pool"], set()).add(t["token"])
    for p_, v in pools.items():
        if len(v) > 1: bad.append("inner pool %s maps to %d coins" % (p_[:12], len(v)))

    # ③ the USD sign and the token-denominated multiple must point the same way (8.72)
    # ⚠️ The comparison must use **gross** profit — the ratio is on a raw-amount basis and excludes
    #    fees. After pnl_usd became net on 2026-09-19, a marginally profitable coin turns negative
    #    once fees come off, and comparing against that raises a pile of false violations.
    # ⚠️⚠️ **The quote token's own price move must be accounted for too**, or it is another false
    #    violation: the two bases have different denominators (one SOL, one USD). Measured on ZMINE:
    #    he lost 0.44% in SOL terms, but in the 47 seconds between buy and sell SOL rose from
    #    $110.16 to $111.11 (+0.86%), so in USD terms it turns positive at +0.41% —
    #    **that is a real phenomenon, not an error**.
    #    → Derive the quote token's average buy/sell price from the fills, compute the multiple the
    #    USD basis should show, and compare against that.
    qpx = {}
    for x in trs:
        tk_, q, u = x.get("token"), x.get("amt_quote"), x.get("usd")
        if not tk_ or not q or u is None: continue
        e = qpx.setdefault(tk_, [0.0, 0.0, 0.0, 0.0])   # buy: usd, quote; sell: usd, quote
        if x.get("is_buy"): e[0] += u; e[1] += q
        else:               e[2] += u; e[3] += q
    for t in tks:
        r, u = t.get("ratio"), t.get("pnl_usd_gross")
        if r is None or u is None or t.get("prior_inventory"): continue
        e = qpx.get(t.get("token"))
        drift = 1.0
        if e and e[1] > 0 and e[3] > 0:
            pb, ps = e[0] / e[1], e[2] / e[3]           # the quote token's average USD price on each side
            if pb > 0: drift = ps / pb
        if (r * drift - 1) * u < 0:
            bad.append("coin %s multiple %.3f (%.3f after the quote token's %+.2f%% move) points "
                       "the opposite way to the USD pnl %+.2f"
                       % ((t.get("token") or "?")[:12], r, 100 * (drift - 1), r * drift, u))

    vol = {}                                               # ④ sold > bought must not yield a PnL (8.72⑤)
    for t in trs:
        if t.get("amt_display") and t.get("token"):
            e = vol.setdefault(t["token"], [0.0, 0.0])
            e[0 if t.get("is_buy") else 1] += (t.get("amt_token") or 0.0)
    for t in tks:
        if t.get("pnl_usd") is None or not t.get("token"): continue
        vb, vs = vol.get(t["token"], [0.0, 0.0])
        if vs > vb * 1.05:
            bad.append("coin %s sold more than it bought yet has a PnL" % t["token"][:12])

    for t in trs:                                          # ⑤ an excluded fill must state its reason (8.72①)
        if not t.get("amt_display") and not t.get("amt_note"):
            bad.append("fill %s has unscaled amounts but no reason" % str(t.get("evt_tx_id"))[:10])

    for t in tks:                                          # ⑥ every gap must carry a reason (8.6)
        if not (t.get("dev_create") or t.get("dev_pool") or t.get("dev")) and not t.get("dev_missing_reason"):
            bad.append("coin %s has no dev and no reason" % (t.get("token") or t.get("pool") or "?")[:12])
        if t.get("pnl_usd") is None and not t.get("pnl_blocked") and not t.get("n_no_usd"):
            bad.append("coin %s has a null PnL and no reason" % (t.get("token") or t.get("pool") or "?")[:12])

    n = ((out.get("cost") or {}).get("rows") or {}).get("03_address_receipts")   # ⑦ possibly truncated (8.1)
    if n in (1000, 2000, 5000, 10000, 100000):
        bad.append("receipts returned exactly %d rows, a common cap — possibly truncated" % n)

    # ⑧ An inner-market fill must have progress. **The only legitimate exception** is an older pump
    #   coin with no curve0 (the initial curve supply = the denominator). pump's position is
    #   1 − curve remaining after the fill ÷ initial curve supply, and that denominator exists only
    #   in the creation event table, which is partitioned by date — an older coin falls outside the
    #   lookback window and cannot be found. That is not a bug.
    #   Measured 2026-09-19: a new address had 5 inner-market fills without a position, on two coins
    #   created on 05-12 and 03-08.
    #   DBC and LaunchLab carry the denominator on every fill, so a missing progress there is a real
    #   violation.
    no_curve0 = {t.get("token") for t in tks if t.get("road") == "pumpfun" and not t.get("curve0")}
    miss = [t for t in trs if t.get("venue") == "inner" and t.get("source") == "decoded"
            and t.get("progress") is None
            and not (t.get("road") == "pumpfun" and t.get("token") in no_curve0)]
    if miss: bad.append("%d inner-market fill(s) have no progress (and are not the pump-without-curve0 "
                        "case)" % len(miss))

    for t in tks:                                          # ⑬ if both timestamps exist on a graduated
    #                                                     coin, sec_to_graduate must be computable
        if (t.get("graduated") and t.get("created_at") and t.get("graduated_at")
                and t.get("sec_to_graduate") is None and not t.get("sec_to_graduate_reason")):
            bad.append("coin %s graduated and has both timestamps, yet sec_to_graduate is missing"
                       % (t.get("token") or "?")[:12])

    _r = {}                                                # ⑭ a coin row's launchpad must match its fills
    for x in trs:
        if x.get("token") and x.get("road"): _r.setdefault(x["token"], set()).add(x["road"])
    for t in tks:
        k, rd = t.get("token"), t.get("road")
        if k and rd and _r.get(k) and rd not in _r[k] and not t.get("road_conflict"):
            bad.append("coin %s: metadata says %s but the fills are on %s, and road_conflict is not set"
                       % (k[:12], rd, "/".join(sorted(_r[k]))))

    for t in tks:                                          # ⑫ a main-table coin must have an address
    #                                                     (unidentifiable ones are already excluded)
        if not t.get("token"):
            bad.append("the main table has a row with no coin address (pool=%s); it should have been excluded"
                       % str(t.get("pool"))[:12])

    for t in tks:                                          # ⑪ a missing creation time needs a reason
    #                                                     (the coin itself is not excluded)
        if not t.get("created_at") and not t.get("created_at_missing_reason"):
            bad.append("coin %s has no creation time and no reason"
                       % (t.get("token") or t.get("pool") or "?")[:12])

    for t in tks:                                          # ⑩ hold time must not be negative (sell before
    #                                                     buy = a window-edge coin)
        if (t.get("hold_seconds") or 0) < 0:
            bad.append("coin %s has a negative hold time (sold before bought; it belongs in window_edge)"
                       % (t.get("token") or "?")[:12])

    for t in tks:                                          # ⑨ a coin marked "no fills in the window" must
    #                                                     not carry buy/sell counts
        if t.get("no_trade_in_window") and (t.get("n_buy") or t.get("n_sell")):
            bad.append("coin %s is marked 'no fills in the window' yet has buy/sell counts"
                       % (t.get("token") or "?")[:12])

    return bad


def profile(addr, d0, d1):
    """Everything one address did between [d0, d1]. Returns a plain dict; makes no judgements."""
    if not ADDR_RE.match(addr or ""):
        raise ValueError("this does not look like a Solana address: %r" % addr)
    cost = {}

    # ① fill-level detail, looked up by signer.
    # ★ Simplified 2026-09-19: transactions where someone else signed and he received are no longer
    #   pulled back in. Measured on three addresses, that class contributed **0 fills** (911 / 1307 /
    #   2731 fills were all in self-signed transactions); the delegated receipts were all airdrops
    #   and transfers.
    trades = run("01_address_trades", cost, addr=addr, d0=d0, d1=d1)

    # ② the movement of **goods and money** inside each of his transactions.
    #    ⚠️ This table is called "receipts" but it is not "transfers in/out", and **it cannot be
    #       dropped** — it carries three jobs:
    #      · backfilling coin addresses: DBC / LaunchLab fill tables give only a pool, and 02 maps
    #        just 53% of pools; the other 47% (= 45.4% of address 3's fills) can only be matched
    #        through this table by transaction, or we do not know which coin it was
    #      · rebuilding outer-market fills: LaunchLab's outer call table has amt_token permanently 0
    #        — 41.6% of address 2's fills are rebuilt from here
    #      · the USD unit price: meme coins have no price feed, but the money leg carries amount_usd,
    #        and dividing by the real quantity gives the price at that fill (free, versus the
    #        12.5-36.8 credits a prices.minute lookup costs)
    receipts = run("03_address_receipts", cost, addr=addr, d0=d0, d1=d1)
    # ⚠️ Self-check: the row cap was removed from the query, but Dune may still truncate implicitly.
    #    A row count landing exactly on a round number deserves suspicion — on 2026-09-18 a
    #    limit of 2000 silently discarded two thirds of the position data.
    if len(receipts) in (1000, 2000, 5000, 10000, 100000):
        cost.setdefault("warn", []).append("receipts returned exactly %d rows, a common cap — possibly "
                                           "truncated" % len(receipts))
    if not trades and not receipts:
        return dict(address=addr, window=[d0, d1], tokens=[], trades=[], cost=cost,
                    note="no launchpad fill and no token movement observed for this address inside the window")

    # ③ Rebuilding comes first — it surfaces coins the decoded tables do not have, and those coins
    #    need metadata too.
    rebuilt = rebuild_trades(receipts, addr, MONEY)
    for t in rebuilt:
        t["amt_display"] = True                            # from the transfer table, already scaled

    # ④ ★ Metadata only for coins that **actually have fills** (simplified 2026-09-19).
    #    It used to query every coin that appeared in receipts — 604 coins for address 3, of which
    #    **445 (74%) were airdrops or transfers he never traded**. Pure waste.
    #    The current definition: coins carried by the fill rows + coins produced by the rebuild +
    #    non-quote tokens that moved inside **the transactions that had fills**.
    quotes = _quote_mints()
    trade_txs = {t.get("evt_tx_id") for t in trades} | {t.get("evt_tx_id") for t in rebuilt}
    trade_txs.discard(None)
    mints = sorted({t["token"] for t in trades if t.get("token")}
                   | {t["token"] for t in rebuilt if t.get("token")}
                   | {rc["token"] for rc in receipts
                      if rc.get("tx_id") in trade_txs and rc.get("token")
                      and rc["token"] not in quotes})
    pools = sorted({t["pool"] for t in trades if t.get("pool")})
    cost["scoped_to_traded"] = dict(
        mints_queried=len(mints),
        mints_if_all_receipts=len({rc["token"] for rc in receipts if rc.get("token")}),
        note="metadata is fetched only for coins with fills; the difference is airdropped or plainly "
             "transferred coins, which this product does not look at")
    if not mints and not pools:
        return dict(address=addr, window=[d0, d1], tokens=[], trades=[], cost=cost,
                    note="tokens moved inside the window but there was no fill at all (only transfers/airdrops)")

    # Metadata (literal lists). A coin may have been created before the fill window → look back
    #    7 days; the "shop" template (DBC's config) may have been created months earlier → that one
    #    looks back 120 days.
    # ⚠️ Fixed 2026-09-19: the old `for i, mb in enumerate(_chunks(mints,400))` used i to index the
    #    pool batches, so **whenever there were more pool batches than coin batches, the extra pools
    #    were never queried** (metadata lost silently). Adding the LaunchLab pool-creation event made
    #    this worse — that one is queried by pool. Loop over the longer side.
    facts = []
    mb_list, pb_list = _chunks(mints, 400), _chunks(pools, 400)
    for i in range(max(len(mb_list), len(pb_list), 1)):
        facts += run("02_token_facts", cost,
                     mints=_lit(mb_list[i] if i < len(mb_list) else []),
                     pools=_lit(pb_list[i] if i < len(pb_list) else []),
                     d0=_shift(d0, -7), d1=d1, cfg0=_shift(d0, -120))
    by_token = {f["token"]: f for f in facts if f.get("token")}
    # LaunchLab / DBC fills identify only a **pool**, while the metadata carries the coin address —
    # the two are matched through the pool
    by_pool  = {f["pool"]: f for f in facts if f.get("pool")}

    # ── ★ Authoritative decimals (2026-09-19) ──
    # This used to be inferred as "raw fill amount ÷ the scaled transfer of the same coin in the same
    # transaction = 10^d", and for address 2 that failed on 181 of 1307 fills (13.8%), whose amounts
    # then had to be marked amt_display=False and excluded from PnL.
    # The root cause was ignoring the project's first rule: **Dune already has
    # tokens_solana.fungible, with decimals in it**.
    # Now the table is queried first and inference is only the fallback. Quote tokens are fetched in
    # the same pass — their decimals were being inferred too.
    DEC, TOKMETA = {}, {}
    dec_need = sorted(set(mints) | {t["quote_mint"] for t in trades if t.get("quote_mint")}
                      | {f["quote_mint"] for f in facts if f.get("quote_mint")})
    for nb in _chunks(dec_need, 400):
        for row in run("09_token_decimals", cost, mints=_lit(nb)):
            if row.get("mint") is None: continue
            if row.get("decimals") is not None:
                DEC[row["mint"]] = int(row["decimals"])
            TOKMETA[row["mint"]] = row
    # LaunchLab's pool-creation event also carries decimals (inside base_mint_param); recorded per
    # pool as a second source
    DEC_BY_POOL = {f["pool"]: int(f["base_dec"]) for f in facts
                   if f.get("pool") and f.get("base_dec") is not None}
    # ★ Fill created_at / symbol / name from 09 into the metadata we already have (2026-09-19)
    #
    #   Why: an older coin (created before 02's lookback window) is not in 02 at all, so created_at
    #   and symbol are empty. A separate creation-table query without date pruning was once run for
    #   this and **cost 6.74 credits** — pure waste: `tokens_solana.fungible` is **a dimension table,
    #   not a partitioned event stream**, so it can be queried by mint with no date condition, and 09
    #   was already pulling it — **0.3 credits covers all of it**.
    #
    #   ✅ Equivalence verified (657 coins that have a value on both sides):
    #      `fungible.created_at` − `creation event.evt_block_time` = **0 seconds, 657 of 657 exactly
    #      equal**. Which makes sense: pump creates the mint and the bonding curve in one transaction.
    #
    #   ⚠️ It **does not give `curve0`** (pump's denominator for the entry position, which exists only
    #      in the creation event table) → older pump coins still have no entry_progress.
    #      DBC and LaunchLab carry the denominator on every fill and are unaffected.
    #
    #   ⚠️ **Fill in fields only; never create a metadata entry out of nothing.** The first version on
    #      2026-09-19 created by_token entries for coins missing from 02, which pulled LaunchLab coins
    #      out of the "look up metadata by pool" branch into the "by coin address" one — the dev field
    #      vanished wholesale and the self-check reported 123 violations in one run.
    #      Coins absent from 02 now fall back to _meta() when the coin row is assembled.
    def _meta(tok):
        """fungible metadata from 09, used as the fallback when 02 has nothing. It carries only
        created_at / symbol / name."""
        return TOKMETA.get(tok) or {}

    n_filled = 0
    for f in facts:
        tok = f.get("token")
        meta = TOKMETA.get(tok) if tok else None
        if not meta: continue
        for k in ("created_at", "symbol", "name"):
            if not f.get(k) and meta.get(k):
                f[k] = meta[k]; n_filled += 1

    cost["decimals"] = dict(asked=len(dec_need), from_dune=len(DEC), facts_filled=n_filled,
                            from_launchlab_create=len(DEC_BY_POOL),
                            note="anything unavailable falls back to the old ratio inference; only when both fail is "
                                 "amt_display set to False")

    # ── Alignment: backfill pools into coin addresses and compute the position ──
    #    position = 1 − curve remaining after the fill ÷ the curve's size at birth (pump);
    #    DBC / LaunchLab positions are already computed in 01 (they carry the denominator per fill)
    #    and are not overwritten here.
    #    The outer market has no curve → None.
    #    ⚠️ mayhem can produce a negative value: the AI mints into the curve, so what remains can
    #       exceed the birth supply. It is not clamped to 0 — the negative number is itself
    #       information, flagged as curve_inflated for the LLM to read.
    # LaunchLab / DBC fill tables carry only a pool address, and 02 maps only graduated coins, which
    # covers few of them → backfill through 03's transfer table by transaction id: the coin moved in
    # that same transaction is the coin of that fill. Data we already have; no extra query.
    # ⚠️ Two lists, two purposes, **never interchangeable** (hit twice on 2026-09-18):
    #   · quotes (a 543-entry registry): decides "is this leg the payment". Used only to exclude the
    #     payment leg during the tx2tok backfill.
    #   · MONEY (5 entries): decides "is this money or goods". Every "what did he trade or hold"
    #     definition uses this one.
    #   Mixing them: the registry is built from graduated coins' quote tokens and contains coins like
    #   STONK / ZEC / WBTC / USELESS that are **used as money and traded as goods**; filtering "his
    #   coins" through it deletes swathes of what he actually traded (measured on address 2: 13 coins
    #   and 435 receipt rows removed, worth −610.63 USD in the PnL table).
    # (quotes were already fetched in ④, above)
    # LaunchLab's outer market (Raydium cpswap) call table only says "paid A, received B" without
    # saying which side is money.
    # → If what was received is a quote token, the fill is really a **sell**: flip the direction and
    #   swap the goods and money legs.
    # ⚠️ A coin can be **both goods and money** (STONK / USELESS are often used as quote tokens while
    #    also being traded themselves). So "is what was received in the quote list" is not enough —
    #    it would turn "bought STONK with WSOL" into "sold STONK".
    #    Only **received is money AND paid is not money** counts as a sell. When both sides are money
    #    (or neither is), nothing is changed.
    # ★★ Detecting and dropping the **intermediate legs** of a multi-hop route (2026-09-19, general form)
    #
    #   The test: **within one transaction, if fill A's output coin == fill B's input (quote_mint),
    #   then A is an intermediate leg** — his money only passed through that coin; he did not mean to
    #   buy it.
    #
    #   An example (address notdecu, same instruction index):
    #     pump outer  buy  token=?             paying 2.94 SOL ($296)
    #     pump inner  buy  token=H26vBmR3Mpek  paying 133,188 of "some coin" ($307)
    #     → two hops: SOL →(outer)→ X →(inner)→ H26vBmR3. X is sitting in the second hop's quote_mint.
    #
    #   **All three rules are needed; they catch different things** (keeping only the chained rule on
    #   2026-09-19 took the fill count from 3,947 to 4,140 — it went up, not down):
    #     ① the output coin is known and equals another fill's quote_mint in the same transaction —
    #        chained, with both hops inside what we decode
    #     ② the output coin is unidentifiable, but exactly one non-quote quote_mint exists in that
    #        transaction — that is its output
    #     ③ **a LaunchLab outer fill where neither side is money** — it comes from the call table with
    #        no direction, and the rest of the route often runs through DEXes we do not decode
    #        (507 of 596 such transactions contain only one cpswap), so **neither ① nor ② catches it**
    #        and it needs its own rule
    #
    #   Measured: of notdecu's 452 fills with an unidentifiable coin address, 307 fall under ②;
    #        of 979 LaunchLab outer fills, 825 fall under ① (which had produced a 22:1 imbalance of
    #        287 buys to 7 sells).
    #   ⚠️ Dropping them changes the outcome of the "exclusive claim" step — which is how a real
    #      $18,486 trade was uncovered once (see DATA.md section 12).
    #   ⚠️ **Counts are kept; nothing is dropped silently.**
    bytx = {}
    for t in trades:
        if t.get("evt_tx_id"): bytx.setdefault(t["evt_tx_id"], []).append(t)
    n_route = collections.Counter()
    drop = set()
    for tx, v in bytx.items():
        # ⚠️ A transaction with a single fill must be checked too: rule ③ looks only at that fill's
        #    own two sides
        Q = {t.get("quote_mint") for t in v if t.get("quote_mint") and t.get("quote_mint") not in MONEY}
        for t in v:
            tok = t.get("token")
            if (t.get("road") == "launchlab" and t.get("venue") == "outer"
                    and tok and tok not in MONEY and t.get("quote_mint")
                    and t.get("quote_mint") not in MONEY):
                drop.add(id(t)); n_route["LaunchLab outer fill with money on neither side"] += 1
            elif tok and tok in Q:
                drop.add(id(t)); n_route["output coin is another fill's input in the same transaction"] += 1
            elif not tok and (t.get("quote_mint") in MONEY
                              or (t.get("quote_mint") is None and t.get("quote_symbol") == "SOL")):
                # ⚠️ At this point a pump outer fill still has a NULL quote_mint (it is null in SQL and
                #   only resolved later by _qmint), but its SQL hard-codes quote_symbol='SOL' — which
                #   serves as the second signal for "the payment side is money".
                #   DBC has both fields NULL, so it is never misjudged (that is what saved the RANSOM
                #   fill).
                # ⚠️ **The rule must require that this fill itself paid money** — that it is the
                #   `money → X` hop. Missing that restriction on 2026-09-19 wrongly deleted the $18,486
                #   RANSOM trade: its 4 dbc fills shared one non-money quote token (wXMR), so each took
                #   the others' quote_mint for "its own output" and they marked each other as
                #   intermediate legs.
                #   A genuine intermediate coin is **both somebody's output and somebody's input**;
                #   wXMR there was only ever an input (the pool's quote token), never an output.
                q_other = {y.get("quote_mint") for y in v
                           if y is not t and y.get("quote_mint") and y.get("quote_mint") not in MONEY}
                if len(q_other) == 1:
                    drop.add(id(t)); n_route["paid in money, received an unidentifiable coin that is another fill's input"] += 1
    keep = []
    for t in trades:
        if id(t) in drop: continue
        # A LaunchLab outer fill comes from the call table, so its direction is inferred from "is what
        # was received money": money received and non-money paid → flip it to a sell
        if t.get("road") == "launchlab" and t.get("venue") == "outer":
            got, paid = t.get("token"), t.get("quote_mint")
            if got in MONEY and paid not in MONEY:
                t["is_buy"] = False
                t["token"], t["quote_mint"] = paid, got
                t["amt_token"], t["amt_quote"] = t.get("amt_quote"), t.get("amt_token")
        keep.append(t)
    if drop:
        cost["routing_legs_dropped"] = dict(
            n=len(drop), by_rule=dict(n_route),
            note="a leg whose output coin equals another fill's input coin in the same transaction is an "
             "intermediate leg of a multi-hop route, not a trade of his; dropped")
    trades = keep
    tx2toks = {}
    for rc in receipts:
        # ⚠️ Quote tokens must be excluded: a transaction holds both the meme coin and the SOL/USDC
        #    paid out, and without excluding them SOL is taken for "a coin he traded" (hit
        #    2026-09-18: SOL showed up as 20 buys and 5 sells)
        if rc.get("tx_id") and rc.get("token") and rc["token"] not in quotes:
            v = tx2toks.setdefault(rc["tx_id"], [])
            if rc["token"] not in v: v.append(rc["token"])

    # ★ Before backfilling, look at which coins in this transaction **already have an owner**.
    #   Hit 2026-09-18: one transaction contained both a LaunchLab inner fill and a pump outer fill
    #   while the transfer table offered only one meme coin, so both fills were assigned the same
    #   coin → the same coin appeared twice in tokens (one row per launchpad, with identical PnL).
    #   **A coin already claimed cannot be used for another backfill**; after removing those, a claim
    #   is accepted only if exactly one candidate remains, otherwise the field stays empty with a
    #   reason.
    claimed, blanks = {}, {}
    for t in trades:
        tx = t.get("evt_tx_id")
        if t.get("token"): claimed.setdefault(tx, set()).add(t["token"])
        else:              blanks[tx] = blanks.get(tx, 0) + 1
    for t in trades:
        f = by_token.get(t.get("token")) or by_pool.get(t.get("pool")) or {}
        if not t.get("token"):
            tx = t.get("evt_tx_id")
            tok = f.get("token")
            if not tok:
                cand = [m for m in tx2toks.get(tx, []) if m not in claimed.get(tx, ())]
                if len(cand) == 1 and blanks.get(tx, 0) == 1:
                    tok = cand[0]
                else:
                    t["token_missing_reason"] = (
                        "this transaction has %d fills waiting for a coin and %d unclaimed coins; "
                        "which belongs to which cannot be determined"
                        % (blanks.get(tx, 0), len(cand)))
            t["token"] = tok
            if tok: claimed.setdefault(tx, set()).add(tok)
            f = by_token.get(tok) or f
        if t.get("progress") is None:                    # pump only; the other two are given by 01
            c0, left = f.get("curve0"), t.get("curve_left")
            if c0 and left is not None:
                t["progress"] = round(1 - left / c0, 6)
                t["curve_inflated"] = t["progress"] < 0

    # ④-b PnL in USD — the only basis comparable across quote tokens (user's call 2026-09-18: USD
    #     only, no token-denominated figures)
    #
    # ⚠️ The transfer table's amount_usd cannot serve as the money leg: **the SOL received on a sell
    #    has no transfer record** (the program credits the wallet's lamport balance directly, which is
    #    a balance change, not a transfer instruction, and the transfer table records instructions).
    #    Measured on one sell: only two fee rows were visible and the 0.161825 SOL received was absent
    #    entirely, which made every USD PnL negative and contradicted the token-denominated multiple.
    # → The right way: **take the amount from the fill event** (exact, already returned by 01) and
    #   **take the price from prices.minute** (per minute).
    SOL_PLACEHOLDER = SOL_MINT          # how pump events write SOL (see the top of this module)
    WSOL = "So11111111111111111111111111111111111111112"

    # LaunchLab's quote token appears only in the migration table (graduated coins), which covers few
    # of them.
    # The fix: **the quote token he paid on a buy does have a transfer record** (what he received on a
    # sell does not — see the note below), but an arbitrary OUT row will not do, because it may be a
    # fee (measured 2026-09-18: taking the first row produced a unit price of $183 while SOL was $99).
    # → Use **exact amount matching**: the fill event's raw value ÷ the transfer table's real quantity
    #   must be exactly a power of ten, and that exponent is the quote token's decimals. Only an exact
    #   match is accepted.
    # ⚠️ Candidates must **not** be restricted to the quote-token registry — that table is built from
    #    graduated coins, and most quote tokens used by non-graduated LaunchLab coins are absent from
    #    it (hit 2026-09-18: coverage stuck at 24/78).
    #    **An exact amount match is proof by itself**; being "on the list" is not required.
    #    A buy looks at OUT (what he paid), a sell at IN (what he received) —
    #    ⚠️ native SOL received on a sell is a balance change with no transfer record, so that class
    #       can only be matched from the buy side.
    by_tx_mv = {}
    for rc in receipts:
        if rc.get("amt_token"):
            by_tx_mv.setdefault(rc["tx_id"], []).append(rc)

    def _match_quote(t):
        """Identify this fill's quote token by **exact amount matching**. Returns
        (quote token, decimals), or (None, None) when nothing matches.

        The fill event gives the raw on-chain value and the transfer table gives the real quantity;
        the two must differ by exactly a power of ten, and that exponent is the quote token's
        decimals. No match means it was not identified — we do not guess.
        """
        raw = t.get("amt_quote")
        if not raw: return (None, None)
        want = "OUT" if t.get("is_buy") else "IN"
        tok = t.get("token")
        for rc in by_tx_mv.get(t.get("evt_tx_id"), []):
            if rc.get("direction") != want: continue
            if rc.get("token") == tok: continue             # that is the goods leg, not the money leg
            disp = float(rc["amt_token"] or 0)
            if disp <= 0: continue
            k = raw / disp
            for d in range(0, 13):
                if abs(k - 10 ** d) / (10 ** d) < 1e-6:
                    return (rc["token"], d)
        return (None, None)

    key_quote = {}
    for t in trades:
        k = t.get("pool") or t.get("token")
        if not k or k in key_quote: continue
        m, d = _match_quote(t)
        # ★ The quote token's decimals also prefer Dune's authoritative value (2026-09-19)
        if m and DEC.get(m) is not None: d = DEC[m]
        if m: key_quote[k] = (m, d)
    # Fills that carry their own quote_mint (pump / LaunchLab outer) get the authoritative decimals too
    for t in trades:
        qm = t.get("quote_mint")
        k = t.get("pool") or t.get("token")
        if qm and k and k not in key_quote and DEC.get(qm) is not None:
            key_quote[k] = (qm, DEC[qm])

    # ═══ Put amounts on one basis → fill the gaps → merge the rebuilt fills (2026-09-19) ═══
    #
    # Why this must come first: in 01, **pump amounts are already scaled by decimals while DBC and
    #   LaunchLab are raw on-chain values** (their fill tables carry no quote_mint, so no scaling was
    #   possible at that point). The rebuilt values come from the transfer table and are scaled by
    #   construction.
    #   Merging without unifying them means one coin's buys use raw values and its sells use scaled
    #   ones — measured 2026-09-19, the PnL went from −5,731 to +4,937: even the sign flipped.
    #
    # How the decimals are obtained: the same trick as _match_quote — **the fill event's raw value ÷
    #   the scaled transfer of the same coin in the same transaction = a power of ten**, and that
    #   exponent is the decimals. No match means it was not identified: **do not guess, do not scale,
    #   flag it**.
    _dec_cache = {}

    def _base_dec(t):
        """The decimals of the **goods** side of this fill. Returns an int or None.

        ★ Since 2026-09-19 there are three sources, ordered by trustworthiness:
          ① Dune's tokens_solana.fungible (authoritative — it is literally the decimals field)
          ② base_mint_param.decimals from LaunchLab's pool-creation event (per pool)
          ③ ratio inference (the original single source): raw fill value ÷ the scaled transfer of the
             same coin in the same transaction = 10^d
        Only when all three fail does it return None, and that fill is marked amt_display=False and
        excluded from PnL.
        """
        tok, raw = t.get("token"), t.get("amt_token")
        if not tok or not raw: return None
        if tok in DEC:
            t["decimals_from"] = "dune"
            return DEC[tok]
        if t.get("pool") in DEC_BY_POOL:
            t["decimals_from"] = "launchlab_create"
            return DEC_BY_POOL[t["pool"]]
        t["decimals_from"] = "inferred"
        if tok in _dec_cache: return _dec_cache[tok]
        for rc in by_tx_mv.get(t.get("evt_tx_id"), []):
            if rc.get("token") != tok: continue
            disp = float(rc.get("amt_token") or 0)
            if disp <= 0: continue
            k = raw / disp
            for d in range(0, 13):
                if abs(k - 10 ** d) / (10 ** d) < 1e-6:
                    _dec_cache[tok] = d
                    return d
        return None

    RAW_ROADS = ("dbc", "launchlab")          # only these two return raw values; pump is already scaled
    n_scaled = n_unscaled = 0
    for t in trades:
        t.setdefault("source", "decoded")
        t["amt_display"] = t.get("road") not in RAW_ROADS      # pump is scaled to begin with
        if t.get("road") not in RAW_ROADS: continue
        d = _base_dec(t)
        q = key_quote.get(t.get("pool") or t.get("token"))
        if d is not None and t.get("amt_token"):
            t["amt_token"] = t["amt_token"] / (10 ** d); t["token_decimals"] = d
        if q and q[1] is not None and t.get("amt_quote"):
            t["amt_quote"] = t["amt_quote"] / (10 ** q[1]); t["quote_decimals"] = q[1]
        # ★ Scaling a fee depends on which side it is charged on (fee_side); **it cannot always be
        #   treated as the quote token**. DBC (inner and outer alike) charges the fee on the **output**
        #   coin: quote on a sell, base on a buy.
        #   Treating it as quote throughout on 2026-09-19 inflated DBC's buy-side fee rate to 433% and
        #   over-deducted ten thousand dollars from the net PnL.
        if t.get("fee_quote"):
            if t.get("fee_side") == "base":
                if d is not None:
                    t["fee_base"] = t["fee_quote"] / (10 ** d)      # first convert to a real token amount
                    t["fee_quote"] = None                           # converting to the quote token happens
                                                                    # below, at the fill price
                else:
                    t["fee_quote"] = None
                    t["fee_scale_failed"] = "the fee is charged on the base coin, whose decimals were not identified"
            elif q and q[1] is not None:
                t["fee_quote"] = t["fee_quote"] / (10 ** q[1])
        # Only when both legs are scaled does the fill count as display; otherwise it is flagged
        # honestly — **a half-scaled, half-raw amount never enters the PnL**
        if (d is not None or not t.get("amt_token")) and (q or not t.get("amt_quote")):
            t["amt_display"] = True; n_scaled += 1
        else:
            t["amt_display"] = False; n_unscaled += 1
            t["amt_note"] = ("decimals were not identified, so the amount is still the raw on-chain "
                             "value and cannot be summed with other fills")

    # ── Rebuild: add the outer-market fills we did not decode ──
    decoded_keys = {(t.get("evt_tx_id"), t.get("token")) for t in trades if t.get("token")}
    # (the rebuild already ran in ③ — its coins have to be part of the 02 metadata query, so it must
    #  come before 02)

    # ── Fill the gaps: LaunchLab's outer call table has `amt_token` permanently 0 (it is the slippage
    #    bound, not the filled amount), and `amt_quote` is often missing too. When the same (tx, coin)
    #    was rebuilt, the rebuilt value fills it in, with the source recorded.
    by_key = {}
    for t in rebuilt: by_key[(t["evt_tx_id"], t["token"])] = t
    n_filled = 0
    for t in trades:
        k = (t.get("evt_tx_id"), t.get("token"))
        src = by_key.get(k)
        if not src: continue
        if not t.get("amt_token"):
            t["amt_token"] = src["amt_token"]; t["amt_display"] = True; t["amt_from"] = "rebuilt"
        if not t.get("amt_quote"):
            t["amt_quote"] = src["amt_quote"]; t["quote_mint"] = t.get("quote_mint") or src["quote_mint"]
            t["amt_display"] = True; t["amt_from"] = "rebuilt"
        if t.get("amt_from") == "rebuilt": n_filled += 1

    # ── Merge: only (tx, coin) pairs absent from the decoded set, and only on the scaled basis ──
    add = [t for t in rebuilt if (t["evt_tx_id"], t["token"]) not in decoded_keys]
    # ★ Inherit the coin's road — PnL is grouped by (road, coin), so a null road puts the rebuilt
    #   fills in a group of their own and produces "sells counted, buys not" (which is exactly how it
    #   went wrong on 2026-09-19).
    road_of = {t["token"]: t["road"] for t in trades if t.get("token") and t.get("road")}
    for t in add:
        t["road"] = road_of.get(t["token"]) or (by_token.get(t["token"]) or {}).get("road")
    if add:
        trades += add
        trades.sort(key=lambda x: (x.get("evt_block_slot") or 0, x.get("evt_tx_index") or 0,
                                   x.get("evt_outer_instruction_index") or 0))
    cost.setdefault("rebuilt", {}).update(
        candidates=len(rebuilt), merged=len(add), filled_missing=n_filled,
        scaled_to_display=n_scaled, still_raw=n_unscaled,
        note="merged/filled fills carry source=reconstructed or amt_from=rebuilt; anything under "
             "still_raw is a raw amount and is not summed")

    def _qmint(t):
        """Which token this fill is quoted in. pump's fill table carries it; DBC reads it from the
        shop template; LaunchLab has no source, so it is derived from the paid/received token of fills
        in the same pool (identified by exact amount matching)."""
        m = t.get("quote_mint")
        if not m:
            f = by_token.get(t.get("token")) or by_pool.get(t.get("pool")) or {}
            m = f.get("quote_mint")
        if not m:
            kq = key_quote.get(t.get("pool")) or key_quote.get(t.get("token"))
            m = kq[0] if kq else None
        if t.get("road") == "pumpfun" and m in (SOL_PLACEHOLDER, None):
            m = WSOL
        return m

    # ★ The primary price source costs **no extra query** — among the transfers 03 already returned,
    #   any row carrying amount_usd divided by its real quantity is the unit price at that moment.
    #   Free, and measured to cover 18 tokens, more than the 12 a dedicated prices.minute lookup gave.
    #   (prices.minute was tried: 12.5 credits for a whole day, 36.8 for a per-minute IN list —
    #   neither is worth it.)
    px_pts = {}
    for rc in receipts:
        a, d = rc.get("amt_usd"), rc.get("amt_token")
        if a and d and float(d) > 0:
            px_pts.setdefault((rc["token"], str(rc["block_time"])[:13]), []).append(float(a) / float(d))
    px = {k: sum(v) / len(v) for k, v in px_pts.items()}
    px_any = {}
    for (tok, _h), v in px_pts.items():
        px_any.setdefault(tok, []).extend(v)
    px_any = {k: sum(v) / len(v) for k, v in px_any.items()}

    # The second source: quote tokens still missing a price are derived through SOL as a bridge — they
    # trade against SOL on DEXes themselves, and Dune computes amount_usd for a fill as long as one
    # side has a price. Measured: 23 of 24 missing tokens were recovered, at prices matching reality
    # (AAVE $149 / UNI $7.75 / STONK $0.22).
    # ⚠️ The list must be sorted: a list coming out of a set has a different order every run → a
    #    different SQL text → --cache never hits
    need = sorted({m for m in (_qmint(t) for t in trades) if m and m not in px_any})
    if need:
        for nb in _chunks(need, 400):
            for row in run("07_derived_prices", cost, mints=_lit(nb), d0=d0, d1=d1):
                if row.get("price") is None: continue
                px[(row["mint"], str(row["hour"])[:13])] = float(row["price"])
                px_any.setdefault(row["mint"], float(row["price"]))

    def _usd(t):
        """This fill converted to USD. If the quote token, the decimals or that minute's price is
        unavailable it returns None — better missing than wrong.

        ⚠️ **Scaling happens once, in the "put amounts on one basis" section above; it must never be
           divided again here.** Hit 2026-09-19: after that section shipped, the old `/10**d` was still
           here, so **the same quantity was divided twice** — address 2's PnL went from −5,731 to −66,
           off by a factor of 87.
           → This function only reads `amt_display`: True means the quantity is real and is multiplied
             by the price; False means the decimals were never identified, and it returns None.
        """
        if not t.get("amt_display"):                       # not on a common basis, so no conversion
            return None
        m = _qmint(t)
        amt = t.get("amt_quote")
        if not m or amt is None:
            return None
        h = str(t.get("evt_block_time"))[:13]
        pr = px.get((m, h)) or px_any.get(m)                # by hour first, else this token's window average
        return pr * amt if pr else None

    # ★ 2026-09-19: the USD value, quote token and fill price are **written back into trades[]**.
    #   All three were already computed per fill, but only aggregated into the coin-level
    #   usd_spent/usd_got — nothing was left on the fill itself, so a caller holding one fill could
    #   not see what it was worth, what it was quoted in, or at what price.
    #   The user's brief was "export everything about how the trader traded one coin", and these three
    #   are basic columns of that ledger.
    for t in trades:
        qm = _qmint(t)
        if qm and not t.get("quote_mint"):
            t["quote_mint"] = qm
            t["quote_mint_from"] = "resolved"      # absent from the fill table; resolved via metadata or
                                                   # exact amount matching
        t["usd"] = _usd(t)
        if t["usd"] is None:
            t["usd_missing_reason"] = ("amounts are not on a consistent basis (decimals unresolved)" if not t.get("amt_display")
                                       else "quote token unavailable" if not qm
                                       else "no USD price for this quote token at that moment")
        # ★ A fee charged on the base coin is converted to the quote token at **this fill's own
        #   price**: one base token is worth amt_quote/amt_token of the quote. That is the price of
        #   this very trade, which is the closest available.
        if t.get("fee_base") is not None and t.get("amt_token") and t.get("amt_quote"):
            try:
                t["fee_quote"] = t["fee_base"] * (t["amt_quote"] / t["amt_token"])
            except ZeroDivisionError:
                t["fee_quote"] = None

        # ★ Fees converted to USD, at the same price as `usd` so the basis matches.
        #   ⚠️ fee_quote being None is not 0 — LaunchLab's outer market (the call table) has no fee
        #     field at all.
        if t.get("fee_quote") is None:
            t["fee_usd"] = None
            t["fee_missing_reason"] = ("LaunchLab's outer market goes through Raydium cpswap's call "
                                       "table, which holds decoded instruction arguments rather than "
                                       "settlement results, so it carries no fee field"
                                       if t.get("road") == "launchlab" and t.get("venue") == "outer"
                                       else t.get("fee_scale_failed")
                                       or "this fill has no fee field")
        elif not t.get("amt_display"):
            t["fee_usd"] = None
            t["fee_missing_reason"] = "amounts are not on a consistent basis (decimals unresolved), so fees cannot be converted either"
        else:
            m2, a2 = _qmint(t), t.get("amt_quote")
            h2 = str(t.get("evt_block_time"))[:13]
            pr2 = px.get((m2, h2)) or px_any.get(m2) if m2 else None
            t["fee_usd"] = pr2 * t["fee_quote"] if pr2 else None
            if t["fee_usd"] is None:
                t["fee_missing_reason"] = "no USD price for this quote token at that moment"
            # ★ A sanity gate (2026-09-19): an obviously implausible rate is **not used**, rather than
            #   forced into a number. Measured real rates: DBC 0.0801-0.2005% · pump 1.25-3.95% ·
            #   LaunchLab 1.25-1.2658%. The band is 0.01%-10%, which only stops the absurd.
            #   Why: one LaunchLab-heavy smart-money address (3Ycnj9...) had 94 of 3300 fills (2.8%)
            #   compute to a ~0% rate, including 22 of 32 dbc outer buys at exactly 0 — **the cause
            #   was never established** (possibly a different fee arrangement in that pool's config).
            #   The other four addresses had not one anomaly. **An unexplained number is not used**;
            #   it is flagged so the caller knows.
            elif t.get("usd"):
                _rate = abs(t["fee_usd"]) / abs(t["usd"]) if t["usd"] else 0
                if _rate < 1e-4 or _rate > 0.10:
                    t["fee_rate_suspect"] = round(100 * _rate, 6)
                    t["fee_missing_reason"] = (
                        "the computed fee rate %.6f%% is outside the plausible band (real rates: "
                        "DBC 0.08-0.20%% / pump 1.25-3.95%% / LaunchLab 1.25%%); this pool's fee field "
                        "probably uses a different basis, so it is not counted"
                        % (100 * _rate))
                    t["fee_usd"] = None

        # Fill price = money paid or received ÷ tokens filled. Both sides must be real quantities
        # (amt_display)
        if t.get("amt_display") and t.get("amt_token") and t.get("amt_quote"):
            try:
                t["price_quote"] = t["amt_quote"] / t["amt_token"]      # unit price in the quote token
                t["price_usd"] = (t["usd"] / t["amt_token"]) if t["usd"] is not None else None
            except ZeroDivisionError:
                t["price_quote"] = t["price_usd"] = None

    usd = {}
    for t in trades:
        tok = t.get("token")
        if not tok: continue
        a = usd.setdefault(tok, dict(usd_in=0.0, usd_out=0.0, n_no_usd=0,
                                     fees_usd=0.0, fees_in_amount_usd=0.0,
                                     n_no_fee=0, n_fee_suspect=0))
        # ★ Fees are accumulated separately, and anything unavailable is **counted, not treated as
        #   0** — otherwise the net PnL is systematically overstated.
        # ⚠️ **Only fee_basis='on_top' is accumulated** (the fee sits outside amt_quote and must be
        #   subtracted). The `in_amount` ones (pump outer's user_quote_amount_in/out, LaunchLab's
        #   amount_in/out) already are what the user actually paid or received, fee included —
        #   **subtracting again double-counts it**.
        #   Caught 2026-09-19 while cross-checking against a GMGN smart-money address: we computed
        #   $1,670 of fees over 5 days where GMGN showed $873 over 7. The way to tell them apart is in
        #   01's header (look at whether the buy and sell rates are symmetric).
        if t.get("fee_usd") is None:
            a["n_no_fee"] += 1
            if t.get("fee_rate_suspect") is not None: a["n_fee_suspect"] = a.get("n_fee_suspect", 0) + 1
        elif t.get("fee_basis") == "on_top": a["fees_usd"] += t["fee_usd"]
        else: a["fees_in_amount_usd"] = a.get("fees_in_amount_usd", 0.0) + t["fee_usd"]
        v = t.get("usd")
        if v is None: a["n_no_usd"] += 1; continue
        if t.get("is_buy"): a["usd_out"] += v              # a buy spends USD
        else:               a["usd_in"]  += v              # a sell returns USD
    # ★ A coin that was only sold, never bought, **came in as a transfer**: its cost is not 0 but
    #   **unknown**, so no PnL can be computed.
    #   The project's iron rule ① (learned on the BSC line): "anything with buy_cost_usd == 0 must be
    #   excluded, or the whole amount is counted as profit."
    #   It repeated on Solana on 2026-09-19: address 2 had 24 coins that were only sold, whose sale
    #   proceeds were all counted as profit → +3,290.89, while the 276 coins with both sides were
    #   actually −398.74. **Reporting the sum as +2,892 was false.**
    # ⚠️ The test is **token amount**, not fill count. Written by fill count first on 2026-09-19, it
    #   missed this case: a decoded LaunchLab outer buy has `amt_token = 0` (the call table gives the
    #   slippage bound), so "1 buy, 27 sells" slipped past an nb==0 check while he had actually
    #   received 0 tokens and sold 0.75 — those tokens were transferred in. That one coin alone
    #   inflated the result by $1,407.
    vol_t = {}
    for t in trades:
        if t.get("amt_display") and t.get("token"):
            e = vol_t.setdefault(t["token"], [0.0, 0.0])
            e[0 if t.get("is_buy") else 1] += (t.get("amt_token") or 0.0)
    # ★★ The user's call, 2026-09-19: **pnl_usd becomes net profit** (on-chain fees deducted).
    #   Why: a cross-check against GMGN showed we deducted nothing at all — a single fill's dex_usd of
    #   0.747 was 3.9% of its buy cost. Address 1 had 237 closed coins, and round-trip fees are enough
    #   to turn "−93 USD in total" into "over −400": **the conclusion flips**.
    #   The definitions:
    #     pnl_usd_gross = received − spent      (the old pnl_usd, kept for reconciliation)
    #     fees_usd      = the sum of per-fill on-chain fees (pump/DBC/LaunchLab curve fees, see 01)
    #     pnl_usd       = gross − fees          ★ the default basis
    #   ⚠️ **On-chain fees only.** The cut taken by the router or trading bot he uses (FLASHX8..., for
    #     example) is in no Dune table — it is an ordinary transfer — and is **not included**; see
    #     fee_scope.
    #   ⚠️ When some fills have no fee available (n_no_fee > 0) the net profit is optimistic; that is
    #     flagged in the field rather than silently filled with 0.
    for tok, a in usd.items():
        # ⚠️ If not one fill could be converted, return None rather than pretending it is 0 —
        #   "computed to be zero" and "could not be computed" must stay distinct
        if a["usd_in"] == 0 and a["usd_out"] == 0 and a["n_no_usd"]:
            a["pnl_usd"] = a["pnl_usd_gross"] = None
            a["usd_in"] = a["usd_out"] = None
            a["fees_usd"] = round(a["fees_usd"], 4) or None
            continue
        a["fees_usd"] = round(a["fees_usd"], 4)
        a["fees_in_amount_usd"] = round(a.get("fees_in_amount_usd", 0.0), 4)
        vb, vs = vol_t.get(tok, [0.0, 0.0])
        if vs > vb * 1.05:                       # sold more tokens than bought → the excess came in as a transfer
            a["pnl_usd"] = a["pnl_usd_gross"] = None
            a["pnl_blocked"] = ("sold %.6g > bought %.6g: the excess came in as a transfer, its cost "
                                "is unknown, so no PnL can be computed"
                                % (vs, vb))
            a["usd_in"] = round(a["usd_in"], 2); a["usd_out"] = round(a["usd_out"], 2)
            continue
        a["pnl_usd_gross"] = round(a["usd_in"] - a["usd_out"], 2)
        a["pnl_usd"] = round(a["pnl_usd_gross"] - a["fees_usd"], 2)      # ★ net profit
        if a["n_no_fee"]:
            a["fee_incomplete"] = ("%d fill(s) have no fee available (not counted), so the net profit "
                                   "is optimistic" % a["n_no_fee"])
        a["usd_in"] = round(a["usd_in"], 2); a["usd_out"] = round(a["usd_out"], 2)

    # ④ PnL — the one thing that really has to be computed, and it is **computed locally, with no
    #    further Dune query**: 01 already returned the goods and money of every fill, so what remains
    #    is addition and subtraction.
    #    ⚠️ Grouped by **(launchpad, coin/pool, quote token)** and **never summed across quote
    #       tokens** — the three launchpads use 543 of them between them, and adding SOL to USDC is
    #       simply wrong (hit 2026-09-18).
    #    ⚠️ pump amounts are already scaled by decimals; DBC / LaunchLab are raw (their fill tables
    #       carry no quote_mint). So alongside the raw values there is a **multiple = received ÷
    #       spent** — dimensionless, unaffected by decimals, and comparable across launchpads.
    pnl = {}
    # ★ Only fills on a **common basis** take part in the amount totals. A fill whose decimals were
    #   never identified is still a raw on-chain value, and mixing it in makes buys and sells differ by
    #   a factor of 10^n (hit 2026-09-19). Those fills remain in trades, they just carry no amount.
    priced = [t for t in trades if t.get("amt_display")]
    skipped = len(trades) - len(priced)
    if skipped:
        cost.setdefault("warn", []).append(
            "%d fill(s) had unidentified decimals, so their amounts are still raw and were "
            "excluded from the PnL (see amt_note)" % skipped)
    # ★ When one coin was traded against several quote tokens, the raw spent / got / multiple are
    #   **meaningless** — measured 2026-09-19, dividing a USDC sale by a SOL purchase produced a 90.2x
    #   multiple. The comment had said "grouped by (launchpad, coin, quote token)" all along, but the
    #   key never actually contained the quote token. It does now, and when a coin shows more than one
    #   the raw figures are nulled (the USD figures are unaffected — they convert per fill).
    qmints = {}
    for t in priced:
        tk = t.get("token") or t.get("pool")
        if tk: qmints.setdefault(tk, set()).add(_qmint(t))
    for t in priced:
        key = (t.get("road"), t.get("token") or t.get("pool"))
        a = pnl.setdefault(key, dict(road=key[0], token=t.get("token"), pool=t.get("pool"),
                                     n_buy=0, n_sell=0, spent=0.0, got=0.0,
                                     quote_symbol=t.get("quote_symbol"),
                                     quote_scaled=(t.get("road") == "pumpfun"),
                                     venues=set()))
        a["venues"].add(t.get("venue"))
        q = t.get("amt_quote") or 0.0
        if t.get("is_buy"): a["n_buy"] += 1; a["spent"] += q
        else:               a["n_sell"] += 1; a["got"] += q
    # Window edge: selling far more tokens than were bought means the position existed before the
    # window opened (not a bug). Measured on one coin: bought 2.86e12, sold 3.16e13 (11x), giving a
    # 9.65x multiple — the money figure is correct, but it means "how many times the money spent
    # inside this window came back", and without flagging it, it reads like uncanny coin selection.
    tokq = {}
    for t in priced:
        k = (t.get("road"), t.get("token") or t.get("pool"))
        a = tokq.setdefault(k, [0.0, 0.0])
        if t.get("amt_token"):
            a[0 if t.get("is_buy") else 1] += t["amt_token"]
    for a in pnl.values():
        tq = tokq.get((a["road"], a.get("token") or a.get("pool")), [0.0, 0.0])
        a["tok_bought"], a["tok_sold"] = tq[0], tq[1]
        a["prior_inventory"] = bool(tq[1] > tq[0] * 1.05)   # sold more than bought → held before the
        #                                                 window, or received as a transfer
        mixed = len(qmints.get(a.get("token") or a.get("pool"), ())) > 1
        if mixed:
            # Several quote tokens: raw values cannot be summed, so they are nulled and explained
            # (the USD figures remain valid)
            a["pnl_raw"] = a["ratio"] = None
            a["quote_mixed"] = ("this coin was traded against several quote tokens, so the raw values "
                                    "and the multiple are not comparable; use pnl_usd")
        else:
            a["pnl_raw"] = round(a["got"] - a["spent"], 9)
            a["ratio"]   = round(a["got"] / a["spent"], 4) if a["spent"] else None   # received ÷ spent,
        #                                                                        dimensionless
        a["venues"]  = sorted(v for v in a["venues"] if v)

    # ═══ ★ Entry / exit positions and pacing (added 2026-09-19) ═══
    #
    # The user's point: **the entry position is a first-class fact and belongs in the coin-level
    # output**, rather than making a caller dig through trades[]. PnL answers "did it make money";
    # this block answers "**how was it traded**" — and the second is what the product is for.
    #
    # All of it is plain aggregation with **no judgement** (never "early" or "late", only the number
    # and its basis).
    # ⚠️ progress exists only on the inner market — the outer market has no curve, so **None is not
    #    missing data**, and a *_reason says so.
    # ⚠️ progress_basis travels with it: supply (pump / launchlab) and quote (dbc) are two different
    #    bases and cannot be compared for "speed" directly; check this field before comparing across
    #    launchpads (DATA.md section 7).
    def _ts(x):
        return (str(x.get("evt_block_time")) if x and x.get("evt_block_time") else None)

    entry, key_pool = {}, {}
    for t in trades:
        key = (t.get("road"), t.get("token") or t.get("pool"))
        if not key[1]: continue
        # ★ Record which pool this key belongs to — LaunchLab metadata is **stored by pool only**
        #   (a non-graduated coin has no coin address) while the key uses the coin address. Without
        #   remembering the pool the metadata cannot be found and every creation time comes back empty
        #   (hit 2026-09-19).
        if t.get("pool"): key_pool.setdefault(key, t["pool"])
        e = entry.setdefault(key, dict(buys=[], sells=[]))
        e["sells" if t.get("is_buy") is False else "buys"].append(t)

    for key, e in entry.items():
        bs, ss = e.pop("buys"), e.pop("sells")
        fb = bs[0] if bs else None          # trades are already sorted by (slot, tx_index, instr_index)
        lb = bs[-1] if bs else None
        ls = ss[-1] if ss else None
        fs = ss[0] if ss else None
        e["first_buy_at"], e["last_buy_at"]   = _ts(fb), _ts(lb)
        e["first_sell_at"], e["last_sell_at"] = _ts(fs), _ts(ls)
        # ★ 2026-09-19: is the first fill a buy or a sell? **A sell implies the coin was already held
        #   before the window opened**, which is a far harder test than "sold more than bought" —
        #   measured on three dbc coins that sold before buying while selling *less* than they bought,
        #   the quantity test missed them entirely and hold_seconds came out as −20 / −25 / −4 seconds,
        #   while their PnL (−9.66 / −9.80 / −29.90) was selling pre-window inventory whose cost is
        #   outside the window and therefore meaningless.
        both = bs + ss
        both.sort(key=lambda x: (x.get("evt_block_slot") or 0, x.get("evt_tx_index") or 0,
                                 x.get("evt_outer_instruction_index") or 0))
        e["first_trade_is_sell"] = bool(both and both[0].get("is_buy") is False)
        # ★ The entry position is the curve position at the moment of the first buy
        e["entry_progress"] = fb.get("progress") if fb else None
        e["entry_venue"]    = fb.get("venue") if fb else None
        e["entry_basis"]    = fb.get("progress_basis") if fb else None
        e["exit_progress"]  = ls.get("progress") if ls else None
        e["exit_venue"]     = ls.get("venue") if ls else None
        if fb is not None and e["entry_progress"] is None:
            e["entry_progress_reason"] = ("bought on the outer market, which has no curve - not missing data"
                                          if fb.get("venue") == "outer" else
                                          "initial curve supply unavailable (the coin was created before the lookback window), so the denominator is missing and the position cannot be computed")
        if not bs:
            e["entry_progress_reason"] = "no buy inside the window (sells only: already held before it, or the coin was transferred in)"
        # ★ Added 2026-09-20: the exit needs a reason field of its own. Layer 3's exit-progress table
        #   used to read entry_progress_reason, which does not fit inner→outer coins at all (bought on
        #   the inner market, sold on the outer one after graduation), so 9 coins printed
        #   "(no reason recorded)". What was missing was not the reason but this field.
        if ls is not None and e["exit_progress"] is None:
            e["exit_progress_reason"] = ("sold on the outer market, which has no curve - not missing data"
                                         if ls.get("venue") == "outer" else
                                         "initial curve supply unavailable (the coin was created before the lookback window), so the denominator is missing and the position cannot be computed")
        if not ss:
            e["exit_progress_reason"] = "no sell inside the window"
        # The highest and lowest curve position he saw on this coin (inner market only)
        pg = [t["progress"] for t in bs + ss if t.get("progress") is not None]
        e["progress_min"], e["progress_max"] = (min(pg), max(pg)) if pg else (None, None)
        # Hold time: first buy → last sell. If either end is missing it returns None with a reason
        if e["first_buy_at"] and e["last_sell_at"]:
            try:
                import calendar
                f = calendar.timegm(time.strptime(e["first_buy_at"][:19], "%Y-%m-%d %H:%M:%S"))
                l = calendar.timegm(time.strptime(e["last_sell_at"][:19], "%Y-%m-%d %H:%M:%S"))
                e["hold_seconds"] = l - f
                if e["hold_seconds"] < 0:
                    # Sold before bought — this is not "held for −20 seconds", the coin was already
                    # held before the window. Return None with a reason.
                    e["hold_seconds"] = None
                    e["hold_reason"] = "the first fill inside the window is a sell (the coin was already held), so hold time cannot be derived"
            except Exception:
                e["hold_seconds"] = None
        else:
            e["hold_seconds"] = None
            e["hold_reason"] = ("not sold inside the window (still held, or sold outside it)" if e["first_buy_at"]
                                else "no buy inside the window")
        e["n_trades"] = len(bs) + len(ss)

    # How old the coin was at the buy, and whether it had graduated — both need the coin metadata, so
    # they live here rather than in the loop above.
    # ⚠️ The project's iron rule ③ (learned on the BSC line): **buying after graduation is following,
    #    not predicting**. Failing to exclude that class once inflated a graduation rate of 10.6% into
    #    19.7%. Here the fact is simply recorded.
    for key, e in entry.items():
        f = (by_token.get(key[1]) or by_pool.get(key[1])
             or by_pool.get(key_pool.get(key)) or {})
        if not f.get("created_at") and TOKMETA.get(key[1], {}).get("created_at"):
            f = dict(f, created_at=TOKMETA[key[1]]["created_at"])   # fallback creation time for older coins
        born, fb_at, grad_at = f.get("created_at"), e.get("first_buy_at"), f.get("graduated_at")
        import calendar
        def _sec(x):
            try: return calendar.timegm(time.strptime(str(x)[:19], "%Y-%m-%d %H:%M:%S"))
            except Exception: return None
        b, q, g = _sec(born), _sec(fb_at), _sec(grad_at)
        # ⚠️ The three ways this can be uncomputable must be stated separately rather than all left
        #    as one empty value (caught during validation 2026-09-19: the "no buy inside the window"
        #    class left both fields empty with no reason — 23 coins on address 2, 2 on address 3)
        NO_BUY = "no buy inside the window (sells only: already held before it, or the coin was transferred in)"
        e["entry_coin_age_sec"] = (q - b) if (b is not None and q is not None) else None
        if e["entry_coin_age_sec"] is None:
            e["entry_coin_age_reason"] = (NO_BUY if q is None else
                                          "creation time unavailable (the pool is absent from the pool-creation event table: created before the lookback window, or not covered by the pump/DBC creation tables)")
        # Had the coin graduated at the time of the buy? **No graduation timestamp is not the same as
        # not graduated**, hence three states: True / False / None
        e["bought_pre_graduation"] = None if (q is None) else (True if g is None and not f.get("graduated")
                                                               else (q < g if g is not None else None))
        if e["bought_pre_graduation"] is None:
            e["entry_vs_graduation_reason"] = (NO_BUY if q is None else
                                               "graduated, but with no graduation timestamp there is "
                                               "no way to tell whether the buy came before or after")

    tokens = []
    for m, f in by_token.items():
        p = (pnl.get(("pumpfun", m)) or pnl.get(("dbc", m)) or pnl.get(("launchlab", m))
             or pnl.get(("dbc", f.get("pool"))) or pnl.get(("launchlab", f.get("pool"))) or {})
        # ★ Entry position and the other pacing fields are looked up by the same key (pnl and entry
        #   build their keys identically)
        ee = (entry.get(("pumpfun", m)) or entry.get(("dbc", m)) or entry.get(("launchlab", m))
              or entry.get(("dbc", f.get("pool"))) or entry.get(("launchlab", f.get("pool"))) or {})
        tokens.append(dict(
            token=m, symbol=f.get("symbol") or _meta(m).get("symbol"),
            name=f.get("name") or _meta(m).get("name"),
            created_at=f.get("created_at") or _meta(m).get("created_at"), graduated=bool(f.get("graduated")),
            graduated_at=f.get("graduated_at"), sec_to_graduate=f.get("sec_to_graduate"),
            is_mayhem=bool(f.get("is_mayhem_mode")),
            # ⚠️ These names must match 02's output columns: the creation table gives dev, the pool
            #    table gives dev2. Hit 2026-09-18: after 02 renamed a column this was not updated and
            #    the developer field silently became None everywhere (with no error).
            dev_create=f.get("dev"), dev_pool=f.get("dev2"),
            road=f.get("road"), platform=f.get("platform"),      # platform = the shop (DBC's config /
            #                                                  LaunchLab's platform_config)
            # ⚠️ Unavailable is not the same as unobserved. When a field is missing, say it is a data
            #    source boundary rather than "not observed" (the user's correction, 2026-09-18)
            dev_missing_reason=(DEV_NO_DECODER
                                if (f.get("road") == "launchlab" and not f.get("dev")) else None),
            dev_conflict=bool(f.get("dev") and f.get("dev2") and f["dev"] != f["dev2"]),  # sources disagree →
            #                                    flag it and call neither of them "the developer"
            inner_addr=f.get("inner_addr"), outer_addr=f.get("outer_addr"),
            curve0=f.get("curve0"), uri=f.get("uri"),
            n_buy=p.get("n_buy", 0), n_sell=p.get("n_sell", 0),
            no_trade_in_window=not p,                    # transfers exist but no fill was caught inside the
            #                                          window (the fills may be outside it)
            spent=p.get("spent"), got=p.get("got"),
            # ★ Stated plainly: how many tokens were bought, sold and left (the user's call
            #   2026-09-19: "write it out, do not aggregate")
            tok_bought=p.get("tok_bought"), tok_sold=p.get("tok_sold"),
            tok_left=(None if p.get("tok_bought") is None
                      else round(p["tok_bought"] - (p.get("tok_sold") or 0.0), 9)),
            pnl_raw=p.get("pnl_raw"), ratio=p.get("ratio"),      # raw values mean something only within
            #                                                      a single quote token
            prior_inventory=p.get("prior_inventory"),            # held before the window → the multiple runs
            #                                    high; do not read it as coin-picking skill
            usd_spent=(usd.get(m) or {}).get("usd_out"),         # USD spent
            usd_got=(usd.get(m) or {}).get("usd_in"),            # USD received
            pnl_usd=(usd.get(m) or {}).get("pnl_usd"),           # ★ net profit, the only figure comparable
            #                                                      across quote tokens
            pnl_usd_gross=(usd.get(m) or {}).get("pnl_usd_gross"),   # before the separately charged fees
            fees_usd=(usd.get(m) or {}).get("fees_usd"),             # ★ curve fees charged on top (already
            #                                                          deducted from the net profit)
            fees_in_amount_usd=(usd.get(m) or {}).get("fees_in_amount_usd"),  # fees already inside the fill
            #                                            amount (informational; not deducted again)
            n_no_fee=(usd.get(m) or {}).get("n_no_fee"),
            n_fee_suspect=(usd.get(m) or {}).get("n_fee_suspect"),
            fee_incomplete=(usd.get(m) or {}).get("fee_incomplete"),
            n_no_usd=(usd.get(m) or {}).get("n_no_usd"),         # how many fills on this coin had no USD price
            # Why no PnL could be computed. ★ A coin that has metadata but no fill inside the window
            #   needs an explanation too — measured on one DBC address 2026-09-19: 445 of 604 coins
            #   were exactly that, and the self-check flagged it immediately.
            pnl_blocked=((usd.get(m) or {}).get("pnl_blocked")
                         or (None if p else "no fill inside the window (only transfers, or the fills "
                                            "fall outside it)")),
            quote_symbol=p.get("quote_symbol"), quote_scaled=p.get("quote_scaled"),
            quote_mixed=p.get("quote_mixed"),
            venues=p.get("venues"),
            **{k: ee.get(k) for k in ENTRY_FIELDS},
        ))
    # Anything whose decimals were never scaled cannot be sorted by raw value, so everything is sorted
    # by the multiple instead.
    # Real currencies are not "coins he traded" and are removed (swapping one currency for another is
    # not meme trading)
    tokens = [t for t in tokens if t.get("token") not in MONEY]
    tokens.sort(key=lambda x: -(x.get("pnl_usd") or 0))

    # For fills whose coin address could not be identified, carry "why" onto the coin row — otherwise
    # that row looks like data missing for no reason
    why = {}
    for t in trades:
        if not t.get("token") and t.get("token_missing_reason"):
            why.setdefault((t.get("road"), t.get("pool")), t["token_missing_reason"])

    seen = {t["token"] for t in tokens}
    seen_pool = {t.get("pool") for t in tokens if t.get("pool")}
    for (road, key), a in pnl.items():                 # coins with no metadata keyed by coin address
        #                                              (most of LaunchLab) are listed from their fills
        if key in seen or key in seen_pool: continue
        # ★ Fixed 2026-09-19: this branch used to **read no metadata at all**, hard-coding dev,
        #   created_at and symbol to None. And LaunchLab's non-graduated coins have a NULL token in 02
        #   and are **stored by pool only**, so they all come through here — which left 146 launchlab
        #   coins with no dev and no creation time. It looked like "Dune has not decoded it", while the
        #   data was sitting in by_pool all along (134 of 141 rows had values).
        f = by_pool.get(a.get("pool")) or by_token.get(a.get("token")) or {}
        tokens.append(dict(token=a.get("token"), pool=a.get("pool"), road=road,
                           symbol=f.get("symbol") or _meta(a.get("token")).get("symbol"),
                           name=f.get("name") or _meta(a.get("token")).get("name"),
                           created_at=f.get("created_at") or _meta(a.get("token")).get("created_at"),
                           platform=f.get("platform"),
                           graduated_at=f.get("graduated_at"),
                           inner_addr=f.get("inner_addr"), outer_addr=f.get("outer_addr"),
                           token_missing_reason=why.get((road, a.get("pool"))),
                           dev=f.get("dev"), dev_create=f.get("dev"), dev_pool=f.get("dev2"),
                           # The three kinds of "not found" are stated separately rather than merged
                           # into one empty value (the user's call 2026-09-18: say so honestly)
                           dev_missing_reason=(None if f.get("dev") else
                                               "the coin address could not be identified (see token_missing_reason), so no creation record can be looked up"
                                               if not a.get("token") else
                                               DEV_NO_DECODER if road == "launchlab" else
                                               DEV_TOO_OLD),
                           n_buy=a["n_buy"], n_sell=a["n_sell"], spent=a["spent"], got=a["got"],
                           tok_bought=a.get("tok_bought"), tok_sold=a.get("tok_sold"),
                           tok_left=(None if a.get("tok_bought") is None
                                     else round(a["tok_bought"] - (a.get("tok_sold") or 0.0), 9)),
                           # ⚠️ This used to be hard-coded False; now that metadata is available it
                           #    follows the metadata (graduation is a fact, not an assumption)
                           graduated=bool(f.get("graduated")),
                           pnl_raw=a["pnl_raw"], ratio=a["ratio"],
                           quote_scaled=a["quote_scaled"], venues=a["venues"],
                           prior_inventory=a.get("prior_inventory"),
                           # ⚠️ This branch (coins listed from fills, with no metadata) used to omit
                           #    the USD fields, so LaunchLab's USD PnL covered only 1 coin in 76 —
                           #    another silently missed assignment
                           usd_spent=(usd.get(key) or {}).get("usd_out"),
                           usd_got=(usd.get(key) or {}).get("usd_in"),
                           pnl_usd=(usd.get(key) or {}).get("pnl_usd"),
                           pnl_usd_gross=(usd.get(key) or {}).get("pnl_usd_gross"),
                           fees_usd=(usd.get(key) or {}).get("fees_usd"),
                           fees_in_amount_usd=(usd.get(key) or {}).get("fees_in_amount_usd"),
                           n_no_fee=(usd.get(key) or {}).get("n_no_fee"),
                           fee_incomplete=(usd.get(key) or {}).get("fee_incomplete"),
                           n_no_usd=(usd.get(key) or {}).get("n_no_usd"),
                           # For a row with no coin address, the USD figures aggregate by coin and
                           # therefore cannot exist → say why
                           pnl_blocked=((usd.get(key) or {}).get("pnl_blocked")
                                        or (None if a.get("token") else
                                            "the coin address could not be identified (see token_missing_reason), so it cannot be priced")),
                           # ★ This branch must carry the entry position too — omitting it makes
                           #   "coins without metadata" look like they have no entry data, and most
                           #   LaunchLab coins come through here (the USD fields were missed here once
                           #   already, on 2026-09-18)
                           **{k: (entry.get((road, key)) or {}).get(k) for k in ENTRY_FIELDS}))
    # ═══ ★ Position state → PnL and ROI are given only for coins that were **closed** (2026-09-19) ═══
    #
    # ⚠️ This is the kind of error that bends the whole conclusion, caught 2026-09-19:
    #    `pnl_usd = received − spent`, so a coin bought and not sold has received = 0 and reports
    #    **−98 USD**, which reads as "wiped out" when **the tokens are still held**. One pass over
    #    address 2 therefore reported "profitable on 8 of 153 = 5%".
    # → Unrealised PnL needs a market price at the end of the window, and **we do not query market
    #    prices** (that is a different data chain).
    #    So for a coin that was not closed, `pnl_usd` and `roi_pct` are None with a reason, while
    #    `usd_spent` / `usd_got` / `tok_bought` / `tok_sold` **are still reported as they are**.
    #
    # ⚠️ There is also a "looks closed but is unknown" case: LaunchLab's outer call table has
    #    `amt_token` permanently 0, so tok_bought = 0 → tok_left = 0 → it is misread as closed.
    #    It needs a state of its own.
    for t in tokens:
        tb, ts_, tl = t.get("tok_bought"), t.get("tok_sold"), t.get("tok_left")
        nb, ns_ = t.get("n_buy") or 0, t.get("n_sell") or 0
        t["tok_left_pct"] = (round(100.0 * tl / tb, 2) if (tb and tl is not None) else None)
        if ns_ == 0 and nb > 0:
            st, why_ = "open", ("bought but not sold inside the window, so the tokens are still held — "
                                "unrealised PnL needs a market price at the end of the window, which "
                                "we do not query")
        elif nb == 0:
            st, why_ = "sell_only", "sold but not bought inside the window, so the cost is unknown"
        elif not tb:
            st, why_ = "volume_unknown", ("the filled token amount is unavailable (LaunchLab's outer call "
                                          "table has amt_token permanently 0), so whether the position "
                                          "was closed cannot be determined")
        elif tl is not None and tl > tb * 0.10:
            st, why_ = "partial", None      # more than a tenth left; flagged, but the PnL is still given (below)
        else:
            st, why_ = "closed", None
        t["position"] = st                       # closed / partial / open / sell_only / volume_unknown
        t["unrealized_left"] = st in ("partial", "open")
        # ⚠️ **Only the two genuinely misleading classes are suppressed** (tightened and then loosened
        #   again on 2026-09-19, both times with measurements behind it):
        #   · open       bought and not sold → pnl = 0 − spent = **the whole amount negative**, which
        #                reads as "wiped out" while the tokens are still held. Must be suppressed.
        #   · sell_only / volume_unknown  the cost or the token amount is unknown, so the number means
        #                nothing. Must be suppressed.
        #   partial is **not** suppressed — it gives the **net cash flow inside the window**, which is
        #   a real and useful number, merely conservative (the unsold part is not valued). Suppressing
        #   it is worse: measured on address 2, 135 of 153 coins were partial, and the leftover share
        #   was **systematically 5.9%** (PURRPETUAL and PLUMBERS matched to the decimal), which looks
        #   like a fee or a structural residue rather than a deliberately held position (cause not
        #   verified).
        #   A blanket rule would leave 88% of coins with no readable PnL — that is treating a property
        #   of the data source as an error.
        if st in ("open", "sell_only", "volume_unknown"):
            t["pnl_usd"] = None
            t["pnl_blocked"] = t.get("pnl_blocked") or why_
            t["roi_pct"] = None
            t["roi_reason"] = why_
        else:
            sp, pl = t.get("usd_spent"), t.get("pnl_usd")
            t["roi_pct"] = (round(100.0 * pl / sp, 2) if (sp and pl is not None and sp > 0) else None)
            if t["roi_pct"] is None:
                t["roi_reason"] = t.get("pnl_blocked") or "the USD amount could not be computed (see n_no_usd)"
            t["pnl_basis"] = ("net cash flow inside the window (received − spent). %.1f%% is still "
                              "unsold and unvalued, so this is conservative"
                              % t["tok_left_pct"]) if st == "partial" else "closed: received − spent"

    # ── Coins with no fill-level detail ──
    # ★ Simplified 2026-09-19: the product only looks at trading, so **plainly transferred coins are
    #   dropped outright** (address 3's 445 airdropped coins already stopped being queried in ④). But
    #   another class must be kept and labelled honestly:
    #
    #     traded elsewhere   the router is Jupiter or another aggregator → **he is trading, but on a
    #                        DEX we do not decode** (only 8 tables are wired up = the inner and outer
    #                        markets of three launchpads; Orca / Raydium AMM v4 / Meteora DLMM /
    #                        Phoenix and the rest are invisible)
    #
    #   Measured on address 2: of 92 coins with no fill detail, **75 (82%) were in fact being traded**.
    #   Dropping them silently would make a caller believe "he never traded this coin" — that is
    #   fabrication, not simplification.
    traded_toks = {t.get("token") for t in trades if t.get("token")}
    elsewhere = {}
    for rc in receipts:
        tk = rc.get("token")
        if not tk or tk in traded_toks or tk in MONEY: continue      # ★ MONEY, not quotes — see below
        if (rc.get("router") or "") in TOKEN_PROGRAMS: continue       # a plain transfer or airdrop — skipped
        e = elsewhere.setdefault(tk, dict(token=tk, n_in=0, n_out=0,
                                          routers=set(), first=rc.get("block_time")))
        e["n_in" if rc.get("direction") == "IN" else "n_out"] += 1
        if rc.get("router"): e["routers"].add(rc["router"])
    for e in elsewhere.values():
        e["routers"] = sorted(e["routers"])
        e["why"] = ("he is trading this coin, but the fills happened on a DEX we do not decode "
                    "(only 8 tables are wired up: the inner and outer markets of pump/DBC/LaunchLab)")

    # ★ MONEY (5 entries) and quotes (a 543-entry registry) are not interchangeable; this was hit
    #   twice:
    #   · quotes: decides "is this leg the payment", used only to exclude the payment leg in the
    #     tx2tok backfill
    #   · MONEY : decides "is this money or goods", used by every "what did he trade or hold" figure
    #   The registry is built from graduated coins' quote tokens and contains coins like STONK / ZEC /
    #   WBTC / USELESS that are **used as money and traded as goods**; filtering "his coins" through it
    #   deletes swathes of what he actually traded (measured on address 2: 13 coins and 435 receipt
    #   rows, worth −610.63 USD).
    mv_meme = [rc for rc in receipts if rc.get("token") and rc["token"] not in MONEY]

    # ★ Invariant: a coin belongs to exactly one launchpad. Appearing twice means the backfill
    #   assigned someone else's fill to it (hit 2026-09-18; the root cause is in the "exclusive claim"
    #   section above). Better to raise than to emit wrong data silently.
    _seen_road = {}
    for t in tokens:
        tk = t.get("token")
        if not tk: continue
        if tk in _seen_road and _seen_road[tk] != t.get("road"):
            cost.setdefault("warn", []).append(
                "coin %s is attached to both %s and %s, so its PnL would be counted twice"
                % (tk[:12], _seen_road[tk], t.get("road")))
        _seen_road[tk] = t.get("road")

    # ★ Self-declared coverage — ⚠️ the unit must be **the (tx, coin) pair**, not the transaction
    #   alone. Hit 2026-09-19: one transaction can both trade coin A and transfer coin B (and with
    #   multi-hop routes that is the norm), so counting by transaction treats B's transfer as
    #   "detailed". Measured, that overstated coverage by 21-36 percentage points:
    #     address 1 invisible 1.0% → **22.2%**; address 2 26.2% → **61.7%**
    #   An example: one FLASHX8 transaction hops SOL→USDC→PTNz→Hxmv, and we decode only the last hop —
    #   he genuinely bought and sold PTNz on the way, with no detail for it.
    tx_trades = {}
    for t in trades:
        if t.get("evt_tx_id"): tx_trades.setdefault(t["evt_tx_id"], []).append(t)
    tx_tok = {(t["evt_tx_id"], t["token"]) for t in trades if t.get("evt_tx_id") and t.get("token")}
    n_full = n_leg = n_pure = n_blind = 0
    for rc in mv_meme:
        if (rc.get("tx_id"), rc.get("token")) in tx_tok:      n_full += 1
        elif rc.get("tx_id") in tx_trades:                    n_leg += 1   # an intermediate leg of a route
        elif (rc.get("router") or "") in TOKEN_PROGRAMS:      n_pure += 1
        else:                                                 n_blind += 1
    tot = max(n_full + n_leg + n_pure + n_blind, 1)
    # Which coins **already listed in tokens** are missing fills → their PnL is incomplete
    incomplete = sorted({rc["token"] for rc in mv_meme
                         if (rc.get("tx_id"), rc.get("token")) not in tx_tok
                         and rc["token"] in {t.get("token") for t in tokens if t.get("token")}})
    # ★ 2026-09-20: mark "missing fills" on the coin row itself.
    #   `tokens_incomplete` is computed **before the main table is split up**, while window_edge and
    #   unclosed are still in it. Measured on 6NsgAU: the main table had 23 coins with 0 missing fills
    #   while that number was 296 (275 of them in window_edge). Layer 4 printed "this profile is based
    #   on 23 coins ... coins missing fills 296", which reads as nonsense.
    #   → Marking it per row lets each downstream table count its own, so the number always matches
    #   its denominator.
    _inc = set(incomplete)
    for _t in tokens:
        if _t.get("token") in _inc:
            _t["legs_missing"] = True
    coverage = dict(meme_transfers=tot,
                    with_trade_detail=n_full,        # this transfer matches a fill on this coin
                    routed_leg=n_leg,                # the transaction has fills, but not of this coin = a routed leg
                    pure_transfer=n_pure,            # a genuine peer-to-peer transfer
                    traded_but_blind=n_blind,        # no fills in the transaction + not a token program = a DEX we
                    #                      do not decode
                    detail_pct=round(100.0 * n_full / tot, 1),
                    tokens_incomplete=len(incomplete),   # ⚠️ counted across everything **before the split**, not the
                    #    main table; for the main table read legs_missing on the coin row
                    incomplete_mints=incomplete[:50],
                    note="routed_leg and traded_but_blind both mean **he is trading and we have no "
                         "detail**; the coins under incomplete_mints are listed in tokens, but their "
                         "PnL is missing legs and runs small")

    # ⚠️ No separate trades_reconstructed list — rebuilt fills are already merged into trades with
    #   source="reconstructed", and a second copy would let a caller add both (double counting).
    #   To see how many were rebuilt, read cost.rebuilt.
    # ═══ ★ Was the coin **transferred in** or **bought**? (found during review 2026-09-19) ═══
    #
    # This is not a bug, it is a layer of fact we had simply never looked at. All 275 of address 3's
    # (dbc) window_edge coins follow **the same pattern**: a plain transfer arrives with a token
    # program as its router, and selling starts seconds later.
    #   8ToGERQAyG  13:45:10 received 233,673 tokens → started selling 13:45:16
    #   4WQRZ2WRba  23:55:11 received 233,673 tokens → started selling 23:55:18   ← identical amounts
    #   9RMN3YueF3  08:32:18 received 333,673 tokens → started selling 08:32:23
    # → He is mostly not "buying then selling" but **being sent coins and selling them**. The fixed
    #   incoming amount is the fingerprint.
    #
    # This changes how the address must be read: treating him as a trader and reading his PnL produces
    # meaningless numbers (the cost is neither inside the window nor his). So it is emitted as a
    # first-class fact, **with no judgement attached**.
    xfer_in = {}
    for rc in receipts:
        tk_ = rc.get("token")
        if (not tk_ or tk_ in MONEY or rc.get("direction") != "IN"
                or (rc.get("router") or "") not in TOKEN_PROGRAMS):
            continue
        e = xfer_in.setdefault(tk_, dict(n=0, amt=0.0, first=None, peers=set()))
        e["n"] += 1
        e["amt"] += float(rc.get("amt_token") or 0.0)
        ts_ = str(rc.get("block_time") or "")
        if ts_ and (e["first"] is None or ts_ < e["first"]): e["first"] = ts_
        if rc.get("peer"): e["peers"].add(rc["peer"])
    # ★ Window-edge coins are split out (the user's call 2026-09-19: "just exclude the edges")
    #
    #   The test looks only at **the opening end**: sold more than bought → the coin was already held
    #   when the window opened (or was transferred in). We see the selling and not the buying, so the
    #   cost is unknown and the PnL, the ROI and the entry position all fail to hold.
    #   Measured on address 3 (a 2-day window): **86 of 159 = 54%** fall into this class.
    #
    #   ⚠️ **The closing end is not excluded.** Still holding at the end of the window (tok_left > 0)
    #      is **a genuinely open position**, not a data defect — those coins are marked
    #      unrealized_left and stay in the main table. Excluding them too would count only coins that
    #      completed a full round, which biases systematically towards short-term trading and deletes
    #      every large position still being held.
    # ⛔ 2026-09-19: older coins were once excluded wholesale by **creation time** (out_of_window[]),
    #    which **was wrong**. The user's correction: "we are collecting the coins this trader traded",
    #    "a buy and a sell inside one window is enough".
    #    → **The time window constrains the fills, not when the coin was created.** If he bought and
    #    sold it, it belongs in the main table.
    #    The only thing an older coin loses is pump's entry_progress (the initial curve supply
    #    denominator); its creation time, age and name come back from 09's fungible, and every other
    #    field is unaffected.
    # ★ How many seconds from creation to graduation (fixed 2026-09-19)
    #
    #   ⚠️ This field used to be **permanently None**: store.py read `f.get("sec_to_graduate")` while
    #      `02_token_facts` **does not produce that column at all** — the names did not match, so it
    #      was silently empty with no error. Across six addresses it appeared 1,060 times with 0
    #      values, and nobody noticed.
    #      That was the fourth instance of the same disease in one day (ENTRY_FIELDS written twice,
    #      n=25 hard-coded, SOL_MINT stored five times).
    #   → Computed locally instead: both timestamps are already in hand, so it should never have been
    #     a lookup.
    import calendar as _cal
    def _epoch(x):
        try: return _cal.timegm(time.strptime(str(x)[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception: return None
    for t in tokens:
        b, g = _epoch(t.get("created_at")), _epoch(t.get("graduated_at"))
        t["sec_to_graduate"] = (g - b) if (b is not None and g is not None and g >= b) else None
        if t["sec_to_graduate"] is None and t.get("graduated"):
            t["sec_to_graduate_reason"] = ("no creation time available" if b is None else
                                           "no graduation time available" if g is None else
                                           "the graduation time precedes the creation time, which is "
                                           "anomalous")

    # ★ When a coin row's launchpad disagrees with its fills', the **fills** win (2026-09-19)
    #
    #   A fill is a direct observation (Dune's decoded event states the launchpad), while the metadata
    #   is joined on (matched by coin or by pool). When they disagree, the metadata is the likelier
    #   error.
    #   Measured: 1 coin in 1,242 — `RANSOM`'s metadata said launchlab while all four of its fills were
    #   dbc outer market.
    #   ⚠️ Never changed silently: the row is marked `road_conflict` so a caller knows its metadata may
    #   belong to a different coin.
    _tr_road = {}
    for x in trades:
        if x.get("token") and x.get("road"): _tr_road.setdefault(x["token"], set()).add(x["road"])
    for t in tokens:
        k, rd = t.get("token"), t.get("road")
        seen = _tr_road.get(k)
        if k and rd and seen and rd not in seen:
            t["road_conflict"] = ("the metadata says %s while every fill of this coin is on %s — the "
                                  "fills win; this row's creation time, dev and other metadata fields "
                                  "may belong to a different coin"
                                  % (rd, "/".join(sorted(seen))))
            t["road_from_facts"] = rd
            t["road"] = sorted(seen)[0]

    # ★ Rows with an unidentifiable coin address are excluded outright (the user's call 2026-09-19:
    #   "that one is of no use at all")
    #
    #   Such a row has a pool address but no coin address — the cause is several fills in one
    #   transaction all waiting for a coin, where the "exclusive claim" step cannot tell which is which
    #   (see 8.3), so the field is left empty.
    #   It is useless to a caller too: without knowing the coin there is no metadata to look up,
    #   nothing to match against external data, and nothing to write into a profile.
    #   ⚠️ **Never dropped silently**: the count and the pool addresses go into cost.excluded.
    no_id = [t for t in tokens if not t.get("token")]
    tokens = [t for t in tokens if t.get("token")]

    for t in tokens:
        if not t.get("created_at"):
            # By this point the coin address is certainly known, so only one possibility remains
            t["created_at_missing_reason"] = ("this coin is in neither the creation event table nor "
                                              "tokens_solana.fungible")

    edge, clean = [], []
    for t in tokens:
        xi = xfer_in.get(t.get("token")) or {}
        t["transfer_in_n"]   = xi.get("n") or 0
        t["transfer_in_amt"] = round(xi.get("amt"), 9) if xi.get("amt") else None
        t["transfer_in_at"]  = xi.get("first")
        t["transfer_in_from"] = sorted(xi.get("peers") or ())[:5] or None
        # ★ Two different questions that **must not be merged** (the first version during the
        #   2026-09-19 review merged them):
        #   a. was the opening position transferred in — the transfer happens **before** the first
        #      fill. This decides whether the earliest sells have a cost at all. Measured on address 3:
        #      275 of 275.
        #   b. which dominates by volume — transferred in versus bought.
        #      Most of address 3's coins satisfy a but not b (233k arrive first, then more is bought).
        #   Testing only b misses 250 coins (the first version recognised just 25 of 275).
        tbv = t.get("tok_bought") or 0.0
        ft = t.get("first_buy_at") or t.get("first_sell_at")
        t["opened_by_transfer"] = bool(
            t["transfer_in_at"] and ft and str(t["transfer_in_at"])[:19] <= str(ft)[:19])
        t["acquired_mostly_by_transfer"] = bool(
            t["transfer_in_amt"] and t["transfer_in_amt"] > max(tbv, 0.0))
        tb, ts_ = t.get("tok_bought") or 0.0, t.get("tok_sold") or 0.0
        # ★ Three tests; any one of them makes it a window-edge coin (the third added 2026-09-19):
        #   a. sold, never bought      b. sold more than bought
        #   c. **the first fill is a sell** — the hardest one, which both a and b can miss
        is_edge = (not t.get("first_buy_at")) or (ts_ > tb * 1.05) \
                  or bool(t.get("prior_inventory")) or bool(t.get("first_trade_is_sell"))
        if is_edge:
            # ⚠️ The reason must distinguish **transferred in** from **bought before the window** —
            #    they mean entirely different things:
            #    transferred in = the cost is not his at all (a distribution leg of a multi-wallet setup)
            #    bought earlier  = the cost is his, just outside this window (a longer window shows it)
            if t.get("opened_by_transfer"):
                t["window_edge_reason"] = (
                    "★ the opening position was **transferred in**, not bought: %s received %.6g "
                    "tokens as a plain transfer (before his first fill) against %.6g bought inside the "
                    "window — that part's cost is not his, so no PnL can be derived%s"
                    % (str(t.get("transfer_in_at"))[:19], t.get("transfer_in_amt") or 0, tb,
                       "; and the transferred amount exceeds the bought amount"
                       if t.get("acquired_mostly_by_transfer") else ""))
            elif not t.get("first_buy_at"):
                t["window_edge_reason"] = "no buy inside the window; only selling is visible"
            elif t.get("first_trade_is_sell"):
                t["window_edge_reason"] = ("the first fill inside the window is a sell — the coin was "
                                           "already held, and that cost lies outside the window")
            else:
                t["window_edge_reason"] = ("sold %.6g > bought %.6g: the coin was already held when "
                                           "the window opened, so its cost is unknown"
                                           % (ts_, tb))
            edge.append(t)
        else:
            clean.append(t)

    # ═══ ★ Coins still open at the end of the window are split out too (user's call 2026-09-19) ═══
    #
    # The user's words: "**we only look at coins bought and sold inside the window; bought earlier or
    # sold later can be excluded**".
    # The first half is handled by window_edge (first fill is a sell / sold > bought / opening position
    # transferred in); this handles the second: **bought but not fully sold inside the window** — that
    # leg sells after the window and we cannot see it.
    #
    #   position=open   bought and not sold → the sell is after the window
    #   partial         bought and sold, with more than a tenth left → **stays in the main table**
    #                   (it completed a round inside the window)
    #   volume_unknown  the filled amount is unavailable → **stays in the main table**; that is a data
    #                   gap, not a window problem, and it is already flagged
    #
    # Measured shares: a random new address **47/129 = 36%**, launchlab 4, dbc 3, pump 0.
    # ⚠️ A longer window (the user wants one to two months) lowers this naturally — it is an edge
    #    effect, not a data problem.
    unclosed = [t for t in clean if t.get("position") in ("open", "sell_only")]
    for t in unclosed:
        t["unclosed_reason"] = ("bought but not sold inside the window — the sell happens after it, "
                                "so this round is incomplete"
                                if t.get("position") == "open" else
                                "sold but not bought inside the window — the buy happened before it")
    clean = [t for t in clean if t.get("position") not in ("open", "sell_only")]

    # ⚠️ receipts is **an intermediate product** and is not emitted (2026-09-19) — it starts at ten
    #   thousand rows and accounts for nine tenths of the JSON, while a caller wants tokens / trades.
    #   Its three jobs (backfilling coin addresses, rebuilding fills, USD unit prices) are done above
    #   and have already landed on trades.
    # ⚠️ traded_elsewhere is no longer emitted either (the user's call 2026-09-19: "coins with no
    #    detail at all can be excluded; the three launchpads are enough") — only the count remains,
    #    **nothing is dropped silently**.
    # ★ The definition of the main tokens[] table (after the three gates):
    #   **bought inside the window** × **sold inside the window** — one complete round
    out = dict(address=addr, window=[d0, d1], tokens=clean,
               window_edge=edge,        # bought before the window
               unclosed=unclosed,       # sold after the window
               trades=trades, coverage=coverage, cost=cost)
    n_xfer = sum(1 for t in edge if t.get("opened_by_transfer"))
    n_nofact = sum(1 for t in clean if not t.get("created_at"))
    cost["excluded"] = dict(
        unidentified_token=len(no_id),
        unidentified_token_pools=sorted({t.get("pool") for t in no_id if t.get("pool")})[:20],
        unidentified_token_note="only a pool address, with no way to tell which coin it is (several "
                                "fills in one transaction, which the exclusive claim cannot separate). "
                                "Useless to a caller, so excluded; the fills remain in trades[]",
        unclosed=len(unclosed),
        unclosed_note="bought but not sold inside the window (sold after it) → the round is incomplete "
                      "and has moved to unclosed[]; a longer window reduces this naturally",
        no_creation_record=n_nofact,
        no_creation_record_note="coins without a creation record **stay in the main table** — the time "
                                "window constrains the fills, not the creation. The only field they "
                                "lack is pump's entry_progress (whose denominator is the initial curve "
                                "supply); their creation time and name come back from 09's "
                                "tokens_solana.fungible",
        window_edge=len(edge),
        window_edge_opened_by_transfer=n_xfer,
        acquired_by_transfer_note=("the opening position of these %d coins was **transferred in**, not "
                                   "bought — the cost is not his. They are %.0f%% of window_edge and "
                                   "are the key to reading this address" %
                                   (n_xfer, 100.0 * n_xfer / max(len(edge), 1))) if n_xfer else None,
        traded_elsewhere=len(elsewhere),
        traded_elsewhere_note="coins he traded on DEXes we do not decode (Orca / Raydium AMM v4 / "
                              "Meteora DLMM / Phoenix and others), with no fill detail at all; excluded",
        window_edge_note="coins already held when the window opened: the cost is unknown, so the PnL "
                         "and the entry position do not hold; moved to window_edge[]")

    # ★ Invariant self-check — runs on every call, with the result written into cost.selfcheck
    v = selfcheck(out)
    cost["selfcheck"] = dict(passed=not v, violations=len(v))
    if v: cost.setdefault("warn", []).extend(v[:20])
    return out


# ★ Real currencies ("money") — a different thing from the "quote token registry", hit 2026-09-18:
#   the registry records "what has been used as a quote token", and STONK / USELESS are in it even
#   though they are meme coins that get traded themselves.
#   Using the registry to decide "is this money" turns "bought STONK with WSOL" into "sold STONK", and
#   counts USDC as a coin he traded (measured: a fabricated −79,850 USD loss).
#   → Only this short list decides "is this money"; the registry is used solely for matching which
#   token a payment was made in.
# Solana's token programs. **A transfer whose `router` (outer_executing_account) is one of these is a
# genuine peer-to-peer transfer**; any other program (Jupiter / LaunchLab / FLASHX / routeUGWgW...)
# means it was a trade, and the other end is a pool rather than a person.
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # the classic SPL Token program
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
    SOL_MINT,                                        # the System Program (native SOL transfers)
}

MONEY = {
    SOL_MINT,                                        # how pump events write SOL
    "So11111111111111111111111111111111111111111",   # native SOL (as used by sol_transfers)
    "So11111111111111111111111111111111111111112",   # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

_QUOTES_CACHE = [None]


def _quote_mints():
    """The quote-token list ("money", not "goods"). Produced once by 05_quote_mints.sql and read
    locally thereafter. The three launchpads use 543 of them between them (measured 2026-09-18) — they
    must be excluded when backfilling "which coin was this fill about"."""
    if _QUOTES_CACHE[0] is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "quote_mints.json")
        try:
            rows = json.load(open(path, encoding="utf-8"))
            _QUOTES_CACHE[0] = {r["mint"] for r in rows if r.get("mint")}
        except Exception:
            # When the list is missing, fall back to the most common few, visibly so in the result
            # (better to exclude too little than to exclude the wrong thing)
            _QUOTES_CACHE[0] = set()
        # ⚠️ Native SOL has three spellings, one per table, and all three must be recognised (hit
        #    2026-09-18: missing the one ending in 111 turned every USD PnL into 0). Whatever the list
        #    comes from, these three are always added.
        _QUOTES_CACHE[0] |= {SOL_MINT,                                        # the pump placeholder
                             "So11111111111111111111111111111111111111111",   # used by sol_transfers
                             "So11111111111111111111111111111111111111112"}   # WSOL
    return _QUOTES_CACHE[0]


def _shift(day, delta):
    t = time.strptime(day, "%Y-%m-%d")
    return time.strftime("%Y-%m-%d", time.gmtime(time.mktime(t) + delta * 86400))


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    addr = args[0]
    days, out, end = 2, None, None
    i = 1
    while i < len(args):
        if args[i] == "--days": days = int(args[i + 1]); i += 2
        elif args[i] == "--json": out = args[i + 1]; i += 2
        elif args[i] == "--cache":
            globals()["CACHE_DIR"] = args[i + 1]; i += 2
        elif args[i] == "--end":
            end = args[i + 1]; i += 2                  # pin the last day of the window; see the d1 note below
        else: sys.exit("unrecognised argument: %s" % args[i])

    # ⚠️ The default window **rolls with T-1**. After midnight both d0 and d1 move a day → the SQL
    #   changes → **every --cache entry misses and everything is re-queried**.
    #   Hit 2026-09-19: what looked like "re-validating for 0 credits" actually burned 103.81 credits
    #   across two addresses, and an outer grep matching only "total 0" printed not a character of the
    #   real "total 45.72" — two layers of concealment stacked on each other.
    #   → To re-validate yesterday's result, pin the window with `--end 2026-09-17`.
    d1 = end or time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))   # T-1: this is retrospective
    #                                                    forensics (the user's call, 2026-09-18)
    d0 = _shift(d1, -(days - 1))
    r = profile(addr, d0, d1)

    if r["cost"].get("credits", 0) > 0.001:
        print("Paid Dune %.4f credits on this run (cache miss; to reuse yesterday's cache pass "
              "--end %s)"
              % (r["cost"]["credits"], d1))
    print("address %s   window %s ~ %s" % (addr, d0, d1))
    print("%d coins traded / %d fills" % (len(r["tokens"]), len(r["trades"])))
    sc = r["cost"].get("scoped_to_traded") or {}
    if sc: print("  (metadata fetched for the %d coins with fills; another %d coins in receipts are "
                 "plain transfers or airdrops, which this product ignores)"
                 % (sc.get("mints_queried", 0), sc.get("mints_if_all_receipts", 0) - sc.get("mints_queried", 0)))
    # ⚠️ Raw amounts are never summed across quote tokens; wins and losses are counted by multiple
    # ★ Only **closed** coins are counted — an open one has no PnL to speak of (see the position field)
    closed = [t for t in r["tokens"] if t.get("position") in ("closed", "partial")
              and t.get("pnl_usd") is not None]
    n = len(closed)
    st = collections.Counter(t.get("position") for t in r["tokens"])
    print("position states: " + "  ".join("%s %d" % kv for kv in st.most_common()))
    if r.get("window_edge"):
        print("  another %d coin(s) in window_edge[] (already held when the window opened, cost "
              "unknown, split out)" % len(r["window_edge"]))
    uc = r.get("unclosed") or []
    if uc:
        print("  another %d coin(s) in unclosed[] (bought inside the window, sold after it)" % len(uc))
    ni = (r["cost"].get("excluded") or {}).get("unidentified_token") or 0
    if ni:
        print("  excluded %d row(s) with an unidentifiable coin address (pool only, useless to a "
              "caller)" % ni)
    nf = (r["cost"].get("excluded") or {}).get("no_creation_record") or 0
    if nf:
        print("  %d of them have no creation record (still in the main table; only pump's entry "
              "position is missing)" % nf)
    if n:
        win = sum(1 for t in closed if t["pnl_usd"] > 0)
        tot = sum(t["pnl_usd"] for t in closed)
        print("%d coin(s) with a computable PnL (closed + partial): %+.2f USD total, %d profitable "
              "(%.0f%%)"
              % (n, tot, win, 100 * win / n))
        print("\n%-12s %-9s %4s %4s %8s %10s %8s %6s"
              % ("coin", "pad", "buy", "sell", "entry%", "pnlUSD", "ROI%", "grad"))
        for t in sorted(closed, key=lambda x: -(x["pnl_usd"]))[:12]:
            k = t.get("symbol") or (t.get("token") or t.get("pool") or "?")[:10]
            print("%-12s %-9s %4s %4s %8s %10s %8s %6s"
                  % (k[:12], t.get("road") or "", t.get("n_buy"), t.get("n_sell"),
                     ("%.1f%%" % (100 * t["entry_progress"])) if t.get("entry_progress") is not None
                     else "outer",
                     "%+.2f" % t["pnl_usd"],
                     ("%+.0f%%" % t["roi_pct"]) if t.get("roi_pct") is not None else "—",
                     "yes" if t.get("graduated") else ""))
    cv = r.get("coverage") or {}
    if cv:
        print("\ncoverage: %d meme transfers" % cv["meme_transfers"])
        print("   OK   fill detail for this coin   %5d  %5.1f%%"
              % (cv["with_trade_detail"], 100.0 * cv["with_trade_detail"] / cv["meme_transfers"]))
        print("   -    plain transfer/airdrop     %5d  %5.1f%%"
              % (cv["pure_transfer"], 100.0 * cv["pure_transfer"] / cv["meme_transfers"]))
        print("   !    routed intermediate leg    %5d  %5.1f%%   trading, no detail"
              % (cv["routed_leg"], 100.0 * cv["routed_leg"] / cv["meme_transfers"]))
        print("   !    trade on an undecoded DEX  %5d  %5.1f%%   same"
              % (cv["traded_but_blind"], 100.0 * cv["traded_but_blind"] / cv["meme_transfers"]))
        te = r.get("traded_elsewhere") or []
        if te: print("   -> %d coin(s) have no fill detail at all (absent from tokens)" % len(te))
        if cv.get("tokens_incomplete"):
            print("   -> ★ %d coin(s) in tokens are missing fills, so their PnL runs small"
                  % cv["tokens_incomplete"])
    for w in (r["cost"].get("warn") or []):
        print("  ⚠️ %s" % w)
    for s in r["cost"]["steps"]:
        det = ("  (scan %.2f + export %.2f)" % (s["exec_credits"], s["export_credits"])
               if s.get("export_credits") is not None else "")
        print("  [%s] %d rows  %.4f credits%s  %.1fs"
              % (s["step"], s["rows"], s["credits"], det, s["seconds"]))
    print("  total %.4f credits" % r["cost"]["credits"])
    if out:
        json.dump(r, open(out, "w"), ensure_ascii=False, default=str)
        print("wrote %s" % out)


if __name__ == "__main__":
    main()
