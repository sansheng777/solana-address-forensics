#!/usr/bin/env python3
"""okx/engine/features.py — layer 3: turn store.py's raw JSON into a compact set of tables.

    ./bin/dune-py /work/okx/engine/features.py <store.py output.json> [--out f.json]
    (pure local computation, not a single Dune query)

═══ The one rule of this layer: **plain script, no judgement** ════════════

    Fixed rules → fixed tables. The same input always yields the same tables, and
    anyone can recompute them.

    The output must NOT contain:
      · thresholds or binary verdicts  ("holds/does not hold", "concentrated/spread")
      · adjectives                     (early/late/high/low/fast/slow/stable/clear)
      · explanations or inferences     ("he does not select on this dimension")
      · suggestive naming              ("playbook" implies a script — renamed to "action combo")

    It emits only: buckets · counts · shares · summed PnL · raw values.
    "Is that concentrated?", "is it stable?", "is it a bot?" are all layer-4 questions.

    ⚠️ 2026-09-19: the first version mixed judgement in (a 40% verdict threshold, a 30%
       shape threshold). The same wallet flipped from "holds" to "does not hold" just by
       moving the window — because what flipped was my threshold, not the data. Deleting
       the thresholds made the problem disappear.

═══ Why this layer exists (measured 2026-09-19) ═══════════════════════════

    store.py output   5.42 MB ≈ 2.17M tokens   ← fits in no context window
    this layer        5-11K tokens

    And an LLM cannot do this layer's work anyway: exact counting, spotting "identical to
    seven decimal places", weighting by token amount, grouping across 4,007 rows.
    **Code computes the tables → the LLM reads them. That is the only workable split.**

═══ Two selection rules, both learned the hard way ════════════════════════

    ① **No medians, no averages.** The distribution itself is the data; a median collapses
       different batches of coins into one number (we once described one wallet with
       "median progress 59%" + "median coin age 14s" — those two numbers **did not come
       from the same coins**).
    ② **A representative sample is the coin that contributed most to that group's PnL**,
       plus the group's spread — not "the middle one". In a long-tailed distribution the
       middle coin represents nothing (we once picked a middle coin that had been rugged).
"""
import collections, json, sys, calendar, time

MONEY = {"So11111111111111111111111111111111111111112",
         "So11111111111111111111111111111111111111111",
         "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
         "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
         "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"}

# The 12 ladder buckets (price move relative to this coin's first buy price)
BANDS = [(-1e18, -50, "down>50%"), (-50, -30, "down30~50"), (-30, -20, "down20~30"),
         (-20, -10, "down10~20"), (-10, 0, "down0~10"), (0, 10, "up0~10"),
         (10, 20, "up10~20"), (20, 30, "up20~30"), (30, 50, "up30~50"),
         (50, 100, "up50~100"), (100, 200, "up100~200"), (200, 1e18, "up>200%")]


def _sec(x):
    try: return calendar.timegm(time.strptime(str(x)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception: return None


def _legs_by_token(trades):
    """Fills per coin, ordered as they happened on chain. Only fills whose amounts are on a
    consistent basis (amt_display=True) are kept."""
    by = collections.defaultdict(list)
    for x in trades:
        if x.get("token") and x.get("amt_display"): by[x["token"]].append(x)
    for k in by:
        by[k].sort(key=lambda z: (z.get("evt_block_slot") or 0, z.get("evt_tx_index") or 0,
                                  z.get("evt_outer_instruction_index") or 0))
    return by


def _combo(t):
    """The action combo on this coin. All four dimensions are categorical — no degree judgements."""
    ev, xv = t.get("entry_venue"), t.get("exit_venue")
    nb, ns = t.get("n_buy") or 0, t.get("n_sell") or 0
    h = t.get("hold_seconds")
    venue = ("inner→inner" if ev == "inner" and xv == "inner" else
             "inner→outer" if ev == "inner" and xv == "outer" else
             "outer→inner" if ev == "outer" and xv == "inner" else
             "outer→outer" if ev == "outer" and xv == "outer" else "other")
    hold = ("unknown" if h is None else "<1m" if h < 60 else "1-10m" if h < 600 else
            "10-60m" if h < 3600 else ">1h")
    # ⚠️ Fill counts are **bucketed**, not exact. Bucketing is a fixed partition (like the 12
    #   ladder bands), not a judgement; using the exact count as a categorical value explodes
    #   the combo space — measured 2026-09-19: 23 combos → 138, and the top-10 coverage fell
    #   from 90% to 64%. Exact counts get their own tables under per_coin_buckets.
    def _n(x): return "1" if x == 1 else "2-4" if x <= 4 else "5+"
    return "%s · buy %s · sell %s · hold %s" % (venue, _n(nb), _n(ns), hold)


def _row(lab, v, n):
    """One row of a bucket table: bucket / coins / share / total PnL / PnL per coin / win rate.

    ★ 2026-09-20 added **PnL per coin** and **win rate**. The user's words: "it is not only the
      win rate, it is also how much it makes." You need both to describe a bucket:
        · total_pnl     — how much this bucket contributed (scales with coin count)
        · pnl_per_coin  — comparable across buckets (100 coins making 1000 ≠ 5 coins making 1000)
        · win_rate      — what share of coins made money (can diverge from the PnL direction:
                          a high win rate with a net loss means a few large losses dominate)
      **All three, because they diverge from each other — giving only one is misleading.**
      The division happens here; layer 4 is not allowed to do arithmetic.
    """
    p = sum(t.get("pnl_usd") or 0 for t in v)
    w = sum(1 for t in v if (t.get("pnl_usd") or 0) > 0)
    return dict(bucket=lab, coins=len(v), pct=round(100 * len(v) / n, 1),
                total_pnl=round(p), pnl_per_coin=round(p / len(v), 1),
                win_rate=round(100 * w / len(v), 1))


def _table(rows, field, buckets, reason_field=None):

    """A bucket table for one dimension: bucket / coins / share / total PnL.
    **No medians, no shape verdicts.**

    ★ Conservation: **the coin counts of every table sum to exactly len(rows)**. Buckets are
    half-open intervals lo <= v < hi, and a value that falls outside every bucket **must get
    its own row** — it is neither None (so it cannot land in the "(no value)" row) nor inside
    any bucket, so without a catch-all it disappears silently. Hit in practice 2026-09-20:
    entry_progress exactly == 1.0, exit_progress negative (pool holds more tokens than the
    total supply), tok_left_pct negative (sold more than bought) — three addresses whose
    tables came up 1 / 2 / 7 and 42 coins short."""
    # ★ Tripwire: reason_field must be **this field's own** reason column. Hit 2026-09-20:
    #   the exit-progress table was wired to entry_progress_reason, and the 9 inner→outer coins
    #   printed "(no reason recorded)" — because the wrong column was read. A mis-wire does not
    #   raise, the values are legal, and the result looks plausible.
    if reason_field:
        stem = reason_field.rsplit("_reason", 1)[0]
        assert field.startswith(stem), "bucket table [%s] wired to the wrong reason column: %s" % (
            field, reason_field)
    out, n, seen = [], len(rows), set()
    for lo, hi, lab in buckets:
        v = [t for t in rows if t.get(field) is not None and lo <= t[field] < hi]
        for t in v:
            seen.add(id(t))
        if v:
            out.append(_row(lab, v, n))
    spill = [t for t in rows if t.get(field) is not None and id(t) not in seen]
    if spill:
        vs = sorted(t[field] for t in spill)
        out.append(_row("(outside every bucket: %s ~ %s)" % (round(vs[0], 4), round(vs[-1], 4)),
                        spill, n))
    miss = [t for t in rows if t.get(field) is None]
    if miss:
        row = _row("(no value)", miss, n)
        if reason_field:
            row["missing_reason"] = dict(collections.Counter(
                str(t.get(reason_field) or "(no reason recorded)")[:48] for t in miss))
        out.append(row)
    return out


def _freq(values, label, top_n=6):
    """Pour a set of values together and count frequencies. Only the most common few and their
    shares — **no verdicts**."""
    if not values: return dict(item=label, sample=0, most_common=[])
    c = collections.Counter(values); tot = len(values)
    topk = c.most_common(top_n)
    return dict(item=label, sample=tot, distinct_values=len(c),
                top_n_share_pct=round(100 * sum(v for _, v in topk) / tot, 1),
                most_common=[dict(value=str(k), count=v, pct=round(100 * v / tot, 2))
                             for k, v in topk])


def _m(v):
    """Signed PnL, easier to scan.

    ⚠️ Counts elsewhere are prefixed with '×' (U+00D7), never an ASCII 'x' — verify.NUM ignores a
    digit glued to a letter (so that f10 is an identifier, not a number), and "x760" would hide the
    760, making a correct fact look unsourced."""
    return ("+%d" % v) if v >= 0 else ("%d" % v)


def _compact(out):
    """★ Flatten each table to one string per row. **Pure formatting — no change of basis and
    no number lost.** Called only after every self-check has run (the checks read the
    structured dicts). 2026-09-20, after the user read the full JSON: one bucket took five
    lines, so 13 tables ran to several hundred lines — unreadable."""
    out["per_coin_buckets"] = {
        k: ["%s | %d coins %.1f%% | pnl %s | per coin %s | win %.1f%%%s" % (
                d["bucket"], d["coins"], d["pct"], _m(d["total_pnl"]), _m(d["pnl_per_coin"]),
                d["win_rate"],
                ("" if not d.get("missing_reason") else
                 " | " + "; ".join("%s ×%d" % (a, b) for a, b in d["missing_reason"].items())))
            for d in v]
        for k, v in out.get("per_coin_buckets", {}).items()}
    if "action_combos" in out:
        out["action_combos"]["rows"] = ["%s | %d coins %.1f%% | pnl %s | won %d lost %d" % (
            d["combo"], d["coins"], d["pct"], _m(d["total_pnl"]), d["winners"], d["losers"])
            + " win %.1f%%" % d["win_rate"]
            for d in out["action_combos"]["rows"]]
    if "by_period" in out:
        out["by_period"]["rows"] = ["%s | %d coins | winners %d | win %.1f%% | net pnl %s" % (
            d["period"], d["coins"], d["winners"], d["win_rate"], _m(d["net_pnl"]))
            for d in out["by_period"]["rows"]]
    if "buy_sell_ladder" in out:
        out["buy_sell_ladder"]["rows"] = ["%s | buy %.1f%% | sell %.1f%%" % (
            d["bucket"], d["buy"], d["sell"]) for d in out["buy_sell_ladder"]["rows"]]
    for side in ("winners_side", "losers_side"):
        blk = (out.get("pnl_distribution") or {}).get(side)
        if blk:
            blk["cumulative"] = ["top%d | %s | %.1f%% of this side" % (
                d["top_n"], _m(d["total"]), d["pct_of_side"])
                for d in blk["cumulative"] if d["pct_of_side"] is not None]
    fd = out.get("per_fill_distribution") or {}
    for k, v in list(fd.items()):
        if isinstance(v, dict) and "most_common" in v:
            fd[k] = dict(sample=v["sample"], distinct_values=v["distinct_values"],
                         top_share_pct=v["top_n_share_pct"],
                         most_common=["%s ×%d (%.1f%%)" % (z["value"], z["count"], z["pct"])
                                      for z in v["most_common"]])
    h = fd.get("fills_by_utc_hour")
    if h:
        fd["fills_by_utc_hour"] = dict(
            total_fills=h["total_fills"],
            hours_with_fills=" ".join("%02dh:%d" % (i, n)
                                      for i, n in enumerate(h["by_hour"]) if n),
            hours_with_no_fills=_ranges(h["hours_with_no_fills"]))
    for smp in (out.get("sample_coins") or {}).get("samples") or []:
        smp["fills"] = ["%s %s %s progress %s | %s (%s) | vs first buy %s" % (
            x["time"], x["side"], ("inner" if x["venue"] == "inner" else "outer"),
            ("%.0f%%" % (100 * x["progress"])) if x["progress"] is not None else "—",
            ("%.6g" % x["quote_amount"]) if x["quote_amount"] is not None else "—",
            ("$%.2f" % x["usd"]),
            ("%+.1f%%" % x["vs_first_buy_pct"]) if x["vs_first_buy_pct"] is not None else "—")
            for x in smp["fills"]]
    return out


def _ranges(xs):
    """[0,1,4,5,6,23] → "00-01, 04-06, 23" """
    if not xs: return ""
    o, a, b = [], xs[0], xs[0]
    for v in xs[1:]:
        if v == b + 1: b = v
        else: o.append((a, b)); a = b = v
    o.append((a, b))
    return ", ".join("%02d" % x if x == y else "%02d-%02d" % (x, y) for x, y in o)


def build(r, compact=True):
    tk = [t for t in r.get("tokens", []) if t.get("pnl_usd") is not None]
    all_tk = r.get("tokens", [])
    tr = r.get("trades", [])
    cv, cost = r.get("coverage") or {}, r.get("cost") or {}
    ex = cost.get("excluded") or {}
    out = dict(address=r.get("address"), window=r.get("window"))

    # ── ① Data completeness (counts only — never "is that enough to trust") ──
    # ★ 2026-09-20, user's call: undecoded DEXes, intermediate legs of multi-hop routes and
    #   plain transfers are **no longer reported**. They are intermediate states of the data
    #   pipeline and say nothing about how this address trades; they only made this layer
    #   heavier. The boundary is still stated honestly — but only as **how much we could see**
    #   and **how many coins we could not, and why**.
    # ★ 2026-09-20, two fixes:
    #   ① `no_creation_record` does not belong under "could not see" — store's own comment is
    #      explicit: **those coins are still in the main table**, they are just missing the
    #      creation record (the denominator for pump's entry_progress). Listing them here turns
    #      "saw it but one field is missing" into "did not see it". Measured: 1 such coin in
    #      4vw54B's five-day window, wrongly listed. → moved into `based_on`.
    #   ② `window_edge_opened_by_transfer` is a **subset** of `window_edge` (measured 8 ⊂ 26).
    #      Listed side by side, a reader will add them up (267 vs the true 258). → folded into
    #      the parent, and **the total is given directly** so nothing downstream has to sum
    #      anything — same rule as "the model may not do arithmetic".
    _why = [("traded_elsewhere", "traded on a DEX we do not decode, not a single fill visible"),
            ("unclosed",         "bought inside the window, not fully sold (sold after it)"),
            ("window_edge",      "already held before the window opened, cost unknown"),
            ("unidentified_token", "could not identify which coin it is")]
    _sub = {"window_edge": ("window_edge_opened_by_transfer",
                            "opening position was transferred in, not bought")}
    rows, tot = [], 0
    for k, lab in _why:
        v = ex.get(k)
        if not isinstance(v, int) or not v: continue
        tot += v
        extra = ""
        if k in _sub:
            sk, slab = _sub[k]
            sv = ex.get(sk)
            if isinstance(sv, int) and sv: extra = " (of which %d: %s)" % (sv, slab)
        rows.append("%s: %d%s" % (lab, v, extra))
    # ★ 2026-09-20: turn "does his holding run past the window" into a number.
    #   It is the first signal for routing by trader type, and it is this product's hardest
    #   limitation: **a long-term holder's coins land in `unclosed` in bulk (bought inside the
    #   window, not sold) and are excluded from the main table**, leaving only his short-term
    #   sliver — take that sliver for the whole picture and every conclusion is wrong.
    #   The denominator is **coins bought inside the window** = main table + unclosed, not all
    #   observed coins.
    _unc = (ex.get("unclosed") or 0)
    _bought = len(tk) + _unc
    out["based_on"] = dict(
        coins=len(tk), fills=len(tr),
        bought_not_fully_sold=dict(
            coins=_unc,
            pct_of_bought_in_window=round(100.0 * _unc / _bought, 1) if _bought else 0.0,
            meaning="his holding on these coins **ran past the window**. The higher this share, "
                    "the shorter the window is relative to him and the more exits are invisible "
                    "— so the main table represents only his short-term slice"),
        # ★ Count only coins **in the main table** that are missing fills. coverage.tokens_incomplete
        #   counts everything before the split and, for a thin address, can exceed the main table
        #   itself (measured on 6NsgAU: main table 23 coins, that field 296).
        coins_missing_fills=sum(1 for t in tk if t.get("legs_missing")),
        coins_without_creation_record=ex.get("no_creation_record") or 0,
        selfcheck=("passed" if (cost.get("selfcheck") or {}).get("passed") else
                   (cost.get("selfcheck") or {}).get("violations")))
    out["not_visible"] = dict(
        total=tot,
        basis="the rows below do not overlap; the total is their sum, subsets are folded into "
              "their parent. **Do not add them up yourself**",
        rows=rows)

    if not tk:
        out["note"] = "no coin in the window has a computable PnL"; return out

    by = _legs_by_token(tr)

    # ── ② Action-combo table (four categorical dimensions bound together) ──
    cb = collections.Counter(_combo(t) for t in tk)
    rows, cum = [], 0
    for k, v in cb.most_common(10):
        cum += v
        g = [t for t in tk if _combo(t) == k]
        p = sum(t["pnl_usd"] for t in g)
        _w = sum(1 for t in g if t["pnl_usd"] > 0)
        rows.append(dict(combo=k, coins=v, pct=round(100 * v / len(tk), 1),
                         total_pnl=round(p), pnl_per_coin=round(p / v, 1),
                         winners=_w, losers=v - _w,
                         win_rate=round(100 * _w / v, 1)))
    # Only the top 10 are listed, but the remainder is collapsed into one row so that
    # **the table's coin count and PnL add up to the totals on their own** — nothing downstream
    # (the layer-4 LLM) has to derive the remainder.
    if cum < len(tk):
        rest = [t for t in tk if _combo(t) not in {r["combo"] for r in rows}]
        rp = sum(t["pnl_usd"] for t in rest)
        _w = sum(1 for t in rest if t["pnl_usd"] > 0)
        rows.append(dict(combo="(other %d combos, none of them in the top 10)" % (len(cb) - len(rows)),
                         coins=len(rest), pct=round(100 * len(rest) / len(tk), 1),
                         total_pnl=round(rp), pnl_per_coin=round(rp / len(rest), 1),
                         winners=_w, losers=len(rest) - _w,
                         win_rate=round(100 * _w / len(rest), 1)))
    out["action_combos"] = dict(distinct_combos=len(cb),
                                top10_coverage_pct=round(100 * cum / len(tk), 1), rows=rows)

    # ── ③ Buy/sell ladder: weighted by token amount, each coin's total buy volume normalised to 1 ──
    # ★ Main-table coins only. The ladder's baseline is "this coin's first buy price" and its
    #   denominator is "this coin's total bought amount"; both require a complete round inside the
    #   window. A window_edge coin's first buy is not really its first (it was already held), and
    #   an unclosed coin's sell volume is truncated by the window. Mixing them in makes both the
    #   baseline and "sold as a share of bought" wrong. Measured 2026-09-20: 619 coins included
    #   vs 588 in the main table — the extra ones were exactly those two classes.
    main = {t.get("token") for t in tk}
    bw, sw, n, tot_sell = collections.Counter(), collections.Counter(), 0, 0.0
    for k, v in by.items():
        if k not in main: continue
        b = [x for x in v if x.get("is_buy")]
        if not b: continue
        p0 = b[0].get("price_usd"); tb = sum(x.get("amt_token") or 0 for x in b)
        if not p0 or not tb: continue
        n += 1
        tot_sell += sum(x.get("amt_token") or 0 for x in v if not x.get("is_buy")) / tb
        for x in v:
            px = x.get("price_usd")
            if not px: continue
            g = 100 * (px / p0 - 1); w = (x.get("amt_token") or 0) / tb
            for lo, hi, lab in BANDS:
                if lo <= g < hi:
                    (bw if x.get("is_buy") else sw)[lab] += w; break
    if n:
        out["buy_sell_ladder"] = dict(
            basis="weighted by token amount; each coin's total buy volume is normalised to 1; "
                  "the bucket is that fill's price move relative to **this coin's first buy price**",
            coins_included=n, main_table_coins=len(tk),
            exclusion_note="included are main-table coins for which a first buy price and a total "
                           "bought amount are available; the difference is coins missing one of the two",
            sold_pct_of_bought=round(100 * tot_sell / n, 1),
            rows=[dict(bucket=lab, buy=round(100 * bw[lab] / n, 1), sell=round(100 * sw[lab] / n, 1))
                  for _, _, lab in BANDS if bw[lab] or sw[lab]])

    # ── ④ Per-coin bucket tables ──
    first_usd = {k: (v[0].get("usd") if v and v[0].get("is_buy") else None) for k, v in by.items()}
    first_q = {k: (v[0].get("amt_quote") if v and v[0].get("is_buy") else None) for k, v in by.items()}
    for t in tk:
        f0, tt = first_usd.get(t["token"]), t.get("usd_spent")
        t["_first_buy_share"] = round(100 * f0 / tt, 1) if f0 and tt else None
        t["_first_buy_usd"] = round(f0, 2) if f0 else None
        a, b = _sec(t.get("created_at")), _sec(t.get("last_sell_at"))
        t["_exit_age_sec"] = (b - a) if (a is not None and b is not None and b >= a) else None
    # Measured progress range [-0.38, 1.0]: negative = the pool holds more tokens than the total
    # supply (the curve was minted into); the last bucket closes at ≥90% so it catches a coin at
    # exactly == 1.0 (the moment of graduation).
    P10 = ([(-1e18, 0, "<0% (pool holds more than total supply)")]
           + [(i / 10, (i + 1) / 10, "%d~%d%%" % (i * 10, i * 10 + 10)) for i in range(9)]
           + [(0.9, 1e18, "≥90%")])
    NB = [(1, 2, "1"), (2, 3, "2"), (3, 5, "3-4"), (5, 11, "5-10"), (11, 1e18, ">10")]
    cells = [
        ("entry·time: seconds after coin creation", "entry_coin_age_sec",
         [(0, 10, "<10s"), (10, 30, "10-30s"), (30, 120, "30-120s"), (120, 1e18, ">120s")],
         "entry_coin_age_reason"),
        ("entry·position: curve progress", "entry_progress", P10, "entry_progress_reason"),
        ("entry·size: first buy USD", "_first_buy_usd",
         [(0, 50, "<50"), (50, 150, "50-150"), (150, 350, "150-350"), (350, 800, "350-800"),
          (800, 1e18, ">800")], None),
        ("entry·size: total USD into this coin", "usd_spent",
         [(0, 100, "<100"), (100, 250, "100-250"), (250, 500, "250-500"), (500, 1000, "500-1k"),
          (1000, 1e18, ">1k")], None),
        ("entry·style: number of buys", "n_buy", NB, None),
        ("entry·style: first buy as share of total", "_first_buy_share",
         [(0, 40, "<40%"), (40, 70, "40-70%"), (70, 90, "70-90%"), (90, 1e18, ">90%")], None),
        ("exit·time: seconds held", "hold_seconds",
         [(0, 15, "≤15s"), (15, 60, "15-60s"), (60, 300, "1-5m"), (300, 3600, "5-60m"),
          (3600, 1e18, ">1h")], "hold_reason"),
        ("exit·time: seconds after coin creation", "_exit_age_sec",
         [(0, 60, "<1m"), (60, 600, "1-10m"), (600, 3600, "10-60m"), (3600, 1e18, ">1h")], None),
        ("exit·position: curve progress", "exit_progress", P10, "exit_progress_reason"),
        ("exit·size: leftover as share of bought", "tok_left_pct",
         [(-1e18, 0, "<0% (sold more than bought, held before the window)"),
          (0, 1, "<1%"), (1, 5, "1-5%"), (5, 10, "5-10%"), (10, 1e18, ">10%")], None),
        ("exit·style: number of sells", "n_sell", NB, None),
        ("fills on this coin", "n_trades",
         [(2, 3, "2"), (3, 5, "3-4"), (5, 11, "5-10"), (11, 1e18, ">10")], None),
    ]
    out["per_coin_buckets"] = {name: _table(tk, fld, bk, rf) for name, fld, bk, rf in cells}
    # Venue is categorical, so it gets its own listing
    vv = collections.Counter("%s→%s" % (t.get("entry_venue"), t.get("exit_venue")) for t in tk)
    out["per_coin_buckets"]["entry→exit venue"] = [
        _row(k, [t for t in tk if "%s→%s" % (t.get("entry_venue"), t.get("exit_venue")) == k], len(tk))
        for k, _ in vv.most_common()]

    # ★ Conservation check: every bucket table's coin count must equal the total. One short means
    #   a row was dropped silently (a value outside every bucket) — better to crash here than to
    #   emit a table that is quietly missing rows.
    for _name, _rows in out["per_coin_buckets"].items():
        _sum = sum(r["coins"] for r in _rows)
        assert _sum == len(tk), "bucket table [%s] coins sum to %d ≠ total %d (a row was dropped)" % (
            _name, _sum, len(tk))

    # ── ⑤ Per-fill distribution (all fills poured together across coins, counted by frequency) ──
    buys = [x for x in tr if x.get("is_buy") and x.get("amt_quote") and x.get("amt_display")]
    sells = [x for x in tr if x.get("is_buy") is False and x.get("amt_quote") and x.get("amt_display")]
    gaps = []
    for k, v in by.items():
        b = [x for x in v if x.get("is_buy")]
        for i in range(len(b) - 1):
            a1, a2 = _sec(b[i].get("evt_block_time")), _sec(b[i + 1].get("evt_block_time"))
            if a1 is not None and a2 is not None and a2 >= a1: gaps.append(a2 - a1)
    hours = collections.Counter(int(str(x.get("evt_block_time"))[11:13])
                                for x in tr if x.get("evt_block_time"))
    out["per_fill_distribution"] = dict(
        basis="every fill of this address poured together and counted. Sizes are in the **quote "
              "token's own units**, not USD (USD would smear repeated values by price drift)",
        buy_size=_freq(["%.6g" % x["amt_quote"] for x in buys], "buy size (quote token units)"),
        sell_size=_freq(["%.6g" % x["amt_quote"] for x in sells], "sell size (quote token units)"),
        gap_between_buys_sec=_freq(gaps, "gap between two consecutive buys (seconds)"),
        # "router used" was removed 2026-09-21, the user's call: "we should not report tooling,
        #   it means nothing". It is not trading behaviour, it only consumed a fact slot and kept
        #   leaking into advice ("same program"). The execution fingerprints (fixed size / coins
        #   per day / activity gap) do not depend on it. The raw router is still in store's trades[].
        fills_by_utc_hour=dict(by_hour=[hours.get(h, 0) for h in range(24)],
                               hours_with_no_fills=[h for h in range(24) if hours.get(h, 0) == 0],
                               total_fills=sum(hours.values())))

    # ── ⑥ Coin attributes (what he picked, not what he did) ──
    _g = collections.Counter(
        ("<60s" if t["sec_to_graduate"] < 60 else "1-10m" if t["sec_to_graduate"] < 600 else ">10m")
        for t in tk if t.get("sec_to_graduate") is not None)
    out["coin_attributes"] = dict(
        # ★ road can be None (the launchpad could not be identified). **Never let None be a dict
        #   key** — verify.digest uses json.dumps(sort_keys=True), None and str cannot be ordered
        #   against each other, and the whole pipeline dies on a TypeError (this blew up on a
        #   real paid call, 2026-09-21).
        by_launchpad=dict(collections.Counter(t.get("road") or "unknown" for t in tk)),
        graduated=sum(1 for t in tk if t.get("graduated")),
        bought_pre_graduation=sum(1 for t in tk if t.get("bought_pre_graduation") is True),
        bought_post_graduation=sum(1 for t in tk if t.get("bought_pre_graduation") is False),
        time_to_graduate=[("%s: %d" % (k, _g[k])) for k in ("<60s", "1-10m", ">10m") if _g.get(k)],
        coins_with_incoming_transfer=sum(1 for t in tk if t.get("transfer_in_n")),
        coins_opened_by_transfer=sum(1 for t in tk if t.get("opened_by_transfer")))

    # ── ⑦ PnL distribution (**both tails reported**) ──
    s = sorted(tk, key=lambda x: -x["pnl_usd"]); tot = sum(t["pnl_usd"] for t in tk)
    # ★ Fixed 2026-09-20: both sides used to be fed the sorted list of **all** coins, which hit
    #   two mines —
    #   ① base = the grand total (= net PnL), so the denominator was not "this side" at all and
    #      shares printed as impossible values like 100.8% / 134.9%;
    #   ② seq[:k] crossed over: with only 34 losing coins, top-50 took 34 losers + the 16 smallest
    #      winners, and the cumulative walked back from −2768 to −2654 — **non-monotonic**.
    #   Sorting each side separately makes base that side's total, and k <= len(seq) blocks the
    #   crossover.
    w = sorted((t for t in tk if t["pnl_usd"] > 0), key=lambda x: -x["pnl_usd"])
    l = sorted((t for t in tk if t["pnl_usd"] <= 0), key=lambda x: x["pnl_usd"])
    def _side(seq):
        base = sum(x["pnl_usd"] for x in seq)
        return [dict(top_n=k, total=round(sum(x["pnl_usd"] for x in seq[:k])),
                     pct_of_side=round(100 * sum(x["pnl_usd"] for x in seq[:k]) / base, 1)
                     if base else None)
                for k in (1, 3, 5, 10, 20, 50) if k <= len(seq)]
    out["pnl_distribution"] = dict(
        basis="per-coin PnL sorted and accumulated. **Bookkeeping only, and both tails are listed**: "
              "looking at the winning tail alone hides losses concentrated in a few coins",
        winners_side=dict(coins=len(w), total=round(sum(x["pnl_usd"] for x in w)), cumulative=_side(w)),
        losers_side=dict(coins=len(l), total=round(sum(x["pnl_usd"] for x in l)), cumulative=_side(l)),
        win_rate_pct=round(100 * len(w) / len(tk), 1),
        win_rate_denominator="these %d coins we can see. He also trades on DEXes we do not decode, "
                             "with no fill-level detail at all; those are not in the denominator" % len(tk),
        net=round(tot),
        best_coin=dict(symbol=s[0].get("symbol"), token=s[0].get("token"), pnl=round(s[0]["pnl_usd"])),
        worst_coin=dict(symbol=s[-1].get("symbol"), token=s[-1].get("token"), pnl=round(s[-1]["pnl_usd"])))

    # ★ Tripwire: the cumulative is a sum of the top N within one side — the winners side must be
    #   non-decreasing, the losers side non-increasing, and |pct_of_side| never above 100. Breaking
    #   either means the denominator or the slice crossed over to the other side again.
    for _side_name, _dir in (("winners_side", 1), ("losers_side", -1)):
        _rows = out["pnl_distribution"][_side_name]["cumulative"]
        for _i in range(1, len(_rows)):
            assert _dir * (_rows[_i]["total"] - _rows[_i - 1]["total"]) >= -0.5, \
                "pnl_distribution[%s] cumulative is not monotonic: top%d %s → top%d %s" % (
                    _side_name, _rows[_i - 1]["top_n"], _rows[_i - 1]["total"],
                    _rows[_i]["top_n"], _rows[_i]["total"])
        for _r in _rows:
            assert _r["pct_of_side"] is None or abs(_r["pct_of_side"]) <= 100.01, \
                "pnl_distribution[%s] top%d share %.1f%% — the denominator is not this side's total" % (
                    _side_name, _r["top_n"], _r["pct_of_side"])

    # ── ⑦b By period (★ a trend has to be visible) ──
    # CLAUDE.md hard rule 8: an aggregate across a time window cannot describe "now" — split it
    # into segments first. Only a stable series may be averaged; a trending one is reported by its
    # latest segment. **This layer only splits; it does not judge stability** — that is layer 4's job.
    seg = collections.defaultdict(list)
    for t in tk:
        d = str(t.get("first_buy_at") or "")[:10]
        if d: seg[d].append(t["pnl_usd"])
    days = sorted(seg)
    if len(days) > 14:
        # Past two weeks, collapse into three segments — otherwise a 90-day window prints 90 rows
        k, out_seg = (len(days) + 2) // 3, []
        for i in range(0, len(days), k):
            grp = days[i:i + k]
            out_seg.append(("%s ~ %s" % (grp[0], grp[-1]),
                            [x for d in grp for x in seg[d]]))
    else:
        out_seg = [(d, seg[d]) for d in days]
    if out_seg:
        out["by_period"] = dict(
            basis="segmented by the date of **this coin's first buy**. Per segment: coins / winners / "
                  "win rate / net PnL",
            periods=len(out_seg),
            rows=[dict(period=lab, coins=len(v), winners=sum(1 for x in v if x > 0),
                       win_rate=round(100 * sum(1 for x in v if x > 0) / len(v), 1),
                       net_pnl=round(sum(v))) for lab, v in out_seg])
        _n = sum(r["coins"] for r in out["by_period"]["rows"])
        assert _n == len(tk), "by_period coins sum to %d ≠ total %d" % (_n, len(tk))

    # ── ⑧ Representative samples ──
    by_cnt = out["action_combos"]["rows"][:3]
    by_pnl = sorted(out["action_combos"]["rows"], key=lambda z: -z["total_pnl"])[:2]
    by_loss = sorted(out["action_combos"]["rows"], key=lambda z: z["total_pnl"])[:1]
    seen, pick = set(), []
    for row in by_cnt + by_pnl + by_loss:
        if row["combo"] in seen: continue
        seen.add(row["combo"]); pick.append(row)
    out["sample_coins"] = dict(
        selection="covers the **3 combos with the most coins** + the **2 with the highest total PnL** "
                  "+ the **1 with the lowest** (deduplicated); for each combo the coin that "
                  "contributed most to that combo's PnL, plus the group's spread. Not 'the middle "
                  "one': in a long-tailed distribution the middle coin represents nothing",
        samples=[])
    for row in pick:
        g = sorted([t for t in tk if _combo(t) == row["combo"]], key=lambda x: -x["pnl_usd"])
        if not g: continue
        t = g[0]; legs = by.get(t["token"], [])
        p0 = next((x.get("price_usd") for x in legs if x.get("is_buy")), None)
        gs = sum(x["pnl_usd"] for x in g)
        out["sample_coins"]["samples"].append(dict(
            combo=row["combo"],
            group_spread=dict(coins=len(g), total_pnl=round(gs),
                              winners=row["winners"], losers=row["losers"],
                              best=round(g[0]["pnl_usd"]), worst=round(g[-1]["pnl_usd"]),
                              top3_pnl=round(sum(x["pnl_usd"] for x in g[:3]))),
            coin=dict(symbol=t.get("symbol"), token=t.get("token"), road=t.get("road"),
                      created_at=t.get("created_at"), graduated=t.get("graduated"),
                      coin_age_at_entry_sec=t.get("entry_coin_age_sec"),
                      hold_seconds=t.get("hold_seconds"),
                      entry_progress=(round(t["entry_progress"], 4)
                                      if t.get("entry_progress") is not None else None),
                      exit_progress=(round(t["exit_progress"], 4)
                                     if t.get("exit_progress") is not None else None),
                      usd_spent=t.get("usd_spent"), usd_got=t.get("usd_got"),
                      fees_usd=t.get("fees_usd"), pnl_usd=t.get("pnl_usd"), roi_pct=t.get("roi_pct")),
            fills=[dict(time=str(x.get("evt_block_time"))[11:19],
                        side="buy" if x.get("is_buy") else "sell", venue=x.get("venue"),
                        progress=(round(x["progress"], 4) if x.get("progress") is not None else None),
                        quote_amount=x.get("amt_quote"), usd=round(x.get("usd") or 0, 2),
                        vs_first_buy_pct=(round(100 * (x["price_usd"] / p0 - 1), 1)
                                          if p0 and x.get("price_usd") else None))
                   for x in legs[:40]]))

    # ── ⑨ What this layer does not contain (a boundary, not a judgement) ──
    out["not_in_this_layer"] = [
        "what he saw when he placed the order (other coins / social media / the market) — not on chain",
        "why he picked these coins — not on chain",
        "his trades on DEXes we do not decode — see `not_visible`",
        "what he did from other addresses — this layer reads one address only",
        "fills outside the window — see store.py's window_edge[] and unclosed[]"]
    # compact=False is kept for self-checks and regressions — once flattened the tables become
    # strings, and conservation can no longer be checked field by field
    return _compact(out) if compact else out


def main():
    a = sys.argv[1:]
    if not a: sys.exit("usage: features.py <store.py output.json> [--out f.json]")
    src = a[0]; dst = a[a.index("--out") + 1] if "--out" in a else None
    f = build(json.load(open(src, encoding="utf-8")))
    s = json.dumps(f, ensure_ascii=False, indent=1)
    if dst:
        open(dst, "w", encoding="utf-8").write(s)
        print("wrote %s   %d chars ≈ %d tokens" % (dst, len(s), len(s) / 2.5))
    else:
        print(s)


if __name__ == "__main__":
    main()
