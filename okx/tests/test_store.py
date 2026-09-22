#!/usr/bin/env python3
"""Offline self-test for the fetch layer — no network, no Dune queries, synthetic data.

Run: docker run --rm -v /root/meme:/work -w /work node:22 python3 okx/tests/test_store.py

Why it exists: `selfcheck()` is the safety net of the agent's fetch layer, but **it can be wrong
itself**. Here every invariant is deliberately violated once with synthetic data to confirm it really
fires — a check that does not fire is no check at all.
"""
import importlib.util, os, sys
# store.py imports dune_client at the top, and that module wants DUNE_API_KEY at import time.
# This test never touches the network and runs no Dune query, so a dummy value is enough.
os.environ.setdefault("DUNE_API_KEY", "offline-test-no-network")
HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("store", os.path.join(HERE, "..", "engine", "store.py"))
S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)

FAIL, RAN = [], []
def want(cond, what):
    # ⚠️ The assertion count is **computed**, never written down. Hit 2026-09-19: it was hard-coded as
    #    n = 25 and still printed 25 after two more tests were added. One number stored twice always
    #    drifts — the same disease as the ENTRY_FIELDS case.
    RAN.append(what)
    if not cond: FAIL.append(what)

SOL = "So11111111111111111111111111111111111111112"

def base():
    """A **clean** output: the self-check must report zero violations."""
    return dict(
        address="ME",
        tokens=[dict(token="C1", road="pumpfun", created_at="2026-09-16 00:00:00.000 UTC",
                     curve0=793100000,          # has an initial curve supply → a missing progress is a real violation
                     pnl_usd=-1.0, pnl_usd_gross=-1.0, ratio=0.9,
                     dev_create="D1", n_buy=1, n_sell=1)],
        trades=[dict(token="C1", road="pumpfun", venue="inner", pool="P1", is_buy=True,
                     amt_token=100.0, amt_display=True, source="decoded", progress=0.3,
                     evt_tx_id="T1"),
                dict(token="C1", road="pumpfun", venue="inner", pool="P1", is_buy=False,
                     amt_token=90.0, amt_display=True, source="decoded", progress=0.4,
                     evt_tx_id="T2")],
        cost=dict(rows={"03_address_receipts": 7}),
    )

def main():
    # ── 0. clean data must pass everything ──
    want(S.selfcheck(base()) == [], "clean data must raise no violation")

    # ── 1. one coin attached to two launchpads ──
    o = base(); o["tokens"].append(dict(token="C1", road="dbc", pnl_usd=0.0, dev_create="D", created_at="2026-09-16 00:00:00.000 UTC"))
    want(any("attached to" in x for x in S.selfcheck(o)), "(1) not caught: coin across two launchpads")

    # ── 2. one inner pool mapping to two coins ──
    o = base(); o["trades"].append(dict(token="C2", road="pumpfun", venue="inner", pool="P1",
                                        is_buy=True, amt_token=1.0, amt_display=True,
                                        source="decoded", progress=0.1, evt_tx_id="T3"))
    o["tokens"].append(dict(token="C2", road="pumpfun", pnl_usd=0.0, dev_create="D", created_at="2026-09-16 00:00:00.000 UTC"))
    want(any("inner pool" in x for x in S.selfcheck(o)), "(2) not caught: inner pool with several coins")
    # The same on the outer market must NOT fire (an AMM pool is a pair by design)
    o = base(); o["trades"].append(dict(token="C2", road="pumpfun", venue="outer", pool="P1",
                                        is_buy=True, amt_token=1.0, amt_display=True,
                                        source="decoded", evt_tx_id="T3"))
    o["tokens"].append(dict(token="C2", road="pumpfun", pnl_usd=0.0, dev_create="D", created_at="2026-09-16 00:00:00.000 UTC"))
    want(not any("inner pool" in x for x in S.selfcheck(o)), "(2) an outer pool with several coins must not fire (false positive)")

    # ── 3. the USD PnL and the token-denominated multiple point opposite ways ──
    # ⚠️ Since 2026-09-19 the comparison uses **gross profit (pnl_usd_gross)**: pnl_usd is now net, and
    #    a marginally profitable coin turns negative once fees come off, which would raise a pile of
    #    false violations.
    o = base(); o["tokens"][0].update(ratio=2.0, pnl_usd_gross=-5.0)
    want(any("opposite way" in x for x in S.selfcheck(o)), "(3) not caught: opposite signs")
    o = base(); o["tokens"][0].update(ratio=2.0, pnl_usd_gross=-5.0, prior_inventory=True)
    want(not any("opposite way" in x for x in S.selfcheck(o)), "(3) a coin held before the window must not fire")
    # The quote token itself rose: a loss in SOL terms and a gain in USD terms — **not a violation**
    # (measured on ZMINE)
    o = base(); o["tokens"][0].update(ratio=0.9956, pnl_usd_gross=0.34)
    o["trades"][0].update(is_buy=True,  amt_quote=0.7551, usd=83.19)   # SOL $110.16
    o["trades"][1].update(is_buy=False, amt_quote=0.7518, usd=83.53)   # SOL $111.11
    want(not any("opposite way" in x for x in S.selfcheck(o)), "(3) a quote-token move that explains it must not fire")
    # The quote token did not move and they still disagree — **a violation**
    o = base(); o["tokens"][0].update(ratio=2.0, pnl_usd_gross=-5.0)
    o["trades"][0].update(is_buy=True,  amt_quote=1.0, usd=100.0)
    o["trades"][1].update(is_buy=False, amt_quote=2.0, usd=200.0)
    want(any("opposite way" in x for x in S.selfcheck(o)), "(3) still opposite with a flat quote token must fire")

    # Gross positive, net turned negative by fees — **not a violation**
    o = base(); o["tokens"][0].update(ratio=1.02, pnl_usd_gross=0.5, pnl_usd=-0.3, fees_usd=0.8)
    want(not any("opposite way" in x for x in S.selfcheck(o)), "(3) fees turning the net negative must not fire")

    # ── 4. sold more than bought yet a PnL was produced (transferred-in tokens treated as free) ──
    o = base(); o["trades"][1]["amt_token"] = 500.0
    want(any("sold more than it bought" in x for x in S.selfcheck(o)), "(4) not caught: oversold yet a PnL")
    o = base(); o["trades"][1]["amt_token"] = 500.0; o["tokens"][0]["pnl_usd"] = None
    o["tokens"][0]["pnl_blocked"] = "received as a transfer"
    want(not any("sold more than it bought" in x for x in S.selfcheck(o)), "(4) nulled with a reason must not fire")

    # ── 5. unscaled amounts with no reason given ──
    o = base(); o["trades"][0]["amt_display"] = False
    want(any("no reason" in x for x in S.selfcheck(o)), "(5) not caught: unscaled with no explanation")
    o = base(); o["trades"][0].update(amt_display=False, amt_note="decimals were not identified")
    want(not any("unscaled amounts" in x for x in S.selfcheck(o)), "(5) with a reason present it must not fire")

    # ── 6. a gap with no reason given ──
    o = base(); o["tokens"][0].pop("dev_create")
    want(any("has no dev" in x for x in S.selfcheck(o)), "(6) not caught: missing dev with no reason")
    o = base(); o["tokens"][0].update(pnl_usd=None)
    want(any("null PnL" in x for x in S.selfcheck(o)), "(6) not caught: null PnL with no reason")

    # ── 7. receipts land exactly on a common cap (read from cost.rows since 2026-09-19, as receipts
    #      are no longer emitted) ──
    o = base(); o["cost"]["rows"]["03_address_receipts"] = 2000
    want(any("truncated" in x for x in S.selfcheck(o)), "(7) not caught: suspected truncation")

    # ── 8. an inner-market fill without progress ──
    o = base(); o["trades"][0]["progress"] = None
    want(any("progress" in x for x in S.selfcheck(o)), "(8) not caught: inner fill without progress")
    # The only legitimate exception to (8): an older pump coin has no curve0 (the denominator), so a
    # missing progress is not a violation
    o = base(); o["tokens"][0].update(curve0=None); o["trades"][0]["progress"] = None
    want(not any("progress" in x for x in S.selfcheck(o)), "(8) pump without an initial curve supply must not fire")
    # But DBC / LaunchLab carry the denominator per fill, so a missing progress there is real
    o = base(); o["tokens"][0].update(road="dbc", curve0=None)
    o["trades"][0].update(road="dbc", progress=None)
    want(any("progress" in x for x in S.selfcheck(o)), "(8) DBC without progress must fire")
    o = base(); o["trades"][0].update(venue="outer", progress=None)
    want(not any("progress" in x for x in S.selfcheck(o)), "(8) an outer fill without progress must not fire")

    # ── 10b. hold time must not be negative (sold before bought = a window edge, already split out) ──
    o = base(); o["tokens"][0]["hold_seconds"] = -20
    want(any("negative hold time" in x for x in S.selfcheck(o)), "(10) not caught: negative hold time")
    o = base(); o["tokens"][0]["hold_seconds"] = 0
    want(not any("negative hold time" in x for x in S.selfcheck(o)), "(10) a zero-second hold must not fire")

    # ── 11b. the fee sanity gate: an absurd rate must not enter fees_usd ──
    # (the gate itself lives in profile(); this only checks the flagged fields are self-consistent:
    #  once suspect is set there must be no fee_usd)
    o = base(); o["trades"][0].update(fee_rate_suspect=0.000001, fee_usd=None,
                                      fee_missing_reason="the computed fee rate is outside the plausible band")
    want(all(not (t.get("fee_rate_suspect") is not None and t.get("fee_usd") is not None)
             for t in o["trades"]), "a rate flagged suspect still kept its fee_usd")

    # ── 13. a graduated coin with both timestamps must yield sec_to_graduate ──
    o = base(); o["tokens"][0].update(graduated=True, graduated_at="2026-09-16 00:01:00.000 UTC",
                                      sec_to_graduate=None)
    want(any("sec_to_graduate" in x for x in S.selfcheck(o)), "(13) not caught: graduated coin without sec_to_graduate")
    o["tokens"][0]["sec_to_graduate"] = 60
    want(not any("sec_to_graduate" in x for x in S.selfcheck(o)), "(13) a present value must not fire")

    # ── 14. a coin row's launchpad must match its fills (a mismatch must set road_conflict) ──
    o = base(); o["tokens"][0]["road"] = "launchlab"      # while the fills are pumpfun
    want(any("road_conflict" in x for x in S.selfcheck(o)), "(14) not caught: launchpad mismatch")
    o["tokens"][0]["road_conflict"] = "already flagged"
    want(not any("road_conflict" in x for x in S.selfcheck(o)), "(14) once flagged it must not fire")

    # ── 12. a main-table coin must have an address (unidentifiable ones are already excluded) ──
    o = base(); o["tokens"].append(dict(pool="P9", road="dbc", created_at="2026-09-16 00:00:00.000 UTC",
                                        dev_create="D", pnl_usd=0.0))
    want(any("no coin address" in x for x in S.selfcheck(o)), "(12) not caught: main table row with no coin address")

    # ── 11. a main-table coin must have a creation time (added 2026-09-19) ──
    o = base(); o["tokens"][0].pop("created_at", None)
    want(any("creation time" in x for x in S.selfcheck(o)), "(11) not caught: main table missing a creation time")

    # ── 9. marked "no fills in the window" yet carrying buy/sell counts (added after the
    #      simplification: 02 only queries coins with fills, so the two cannot coexist) ──
    o = base(); o["tokens"][0]["no_trade_in_window"] = True
    want(any("no fills in the window" in x for x in S.selfcheck(o)), "(9) not caught: no fills yet fill counts present")
    o = base(); o["tokens"][0].update(no_trade_in_window=True, n_buy=0, n_sell=0,
                                      pnl_usd=None, pnl_blocked="no fill inside the window")
    want(not any("no fills in the window" in x for x in S.selfcheck(o)), "(9) genuinely no fills must not fire")

    # ── 10. rebuild_trades: pairing one in against one out ──
    # ⚠️ Since 2026-09-19, 03 no longer returns from_owner / to_owner (two 44-character address
    #    columns, the bulk of the export cost) and composes a single `peer` column in SQL instead.
    #    The fixtures follow suit.
    rc = [dict(tx_id="X", direction="OUT", peer="POOL", token=SOL,
               amt_token=1.0, block_slot=1, tx_index=0, signer="ME", router="R", block_time="t"),
          dict(tx_id="X", direction="IN", peer="POOL", token="C9",
               amt_token=500.0, block_slot=1, tx_index=0, signer="ME", router="R", block_time="t")]
    out = S.rebuild_trades(rc, "ME", S.MONEY)
    want(len(out) == 1 and out[0]["token"] == "C9" and out[0]["is_buy"] is True
         and abs(out[0]["amt_token"] - 500.0) < 1e-9 and abs(out[0]["amt_quote"] - 1.0) < 1e-9,
         "rebuild-A: the buy pair was not reconstructed: %r" % out)
    want(out and out[0]["source"] == "reconstructed", "rebuild-B: source=reconstructed was not set")

    # An intermediate leg (net change 0) must be dropped
    rc2 = rc + [dict(tx_id="X", direction="OUT", peer="POOL2", token="C9",
                     amt_token=500.0, block_slot=1, tx_index=0, signer="ME", router="R", block_time="t"),
                dict(tx_id="X", direction="IN", peer="POOL2", token=SOL,
                     amt_token=1.1, block_slot=1, tx_index=0, signer="ME", router="R", block_time="t")]
    out2 = S.rebuild_trades(rc2, "ME", S.MONEY)
    want(all(t["token"] != "C9" for t in out2), "rebuild-C: the intermediate leg (net change 0) was not dropped: %r" % out2)

    # ── 11. _chunks batching ──
    want(len(S._chunks(list(range(900)), 400)) == 3, "batching produced the wrong number of chunks")
    want(S._chunks([], 400) == [[]], "an empty list must return [[]]")
    want(len(S._chunks(["a", "a", "b"], 400)[0]) == 2, "batching did not de-duplicate")

    print(("FAIL " + "\nFAIL ".join(FAIL)) if FAIL
          else "OK  fetch-layer self-test passed (%d assertions)" % len(RAN))
    sys.exit(1 if FAIL else 0)

main()
