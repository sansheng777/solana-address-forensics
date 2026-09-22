#!/usr/bin/env python3
"""Cross-check layer 3 against the raw store output.

    docker run --rm -v /root/meme:/work -w /work node:22 python3 okx/tests/xcheck.py

Seven conservation groups. This is not a unit test of features.py, it is an independent recount of
its output against store.py's raw tokens[] / trades[]: if a bucket table silently drops a row, or a
share stops adding to 100, or a PnL total stops matching, it shows up here.
"""
import json, glob, subprocess, sys

ADDR = ["4vw54B", "3Ycnj9", "DyNiyD", "6NsgAU", "4MPwyF"]
ran = 0
BUILD = ("import json,sys;sys.path.insert(0,'okx');from engine import features;"
         "print(json.dumps(features.build(json.load(open(sys.argv[1])),compact=False),"
         "ensure_ascii=False))")
fail = 0

for a in ADDR:
    fs = glob.glob("okx/data/_fin2_%s*.json" % a)
    if not fs: continue
    ran += 1
    S = json.load(open(fs[0], encoding="utf-8"))
    # compact=False on purpose: once flattened the tables are strings and conservation can no
    # longer be checked field by field.
    F = json.loads(subprocess.run(["python3", "-c", BUILD, fs[0]],
                                  capture_output=True, text=True).stdout)
    tk = [t for t in S["tokens"] if t.get("pnl_usd") is not None]
    n, bad = len(tk), []

    # 1 total coin count
    if (F.get("based_on") or {}).get("coins") != n:
        bad.append("coin count does not match")

    # 2 every bucket table: coins conserved + shares sum to 100
    for name, rows in F["per_coin_buckets"].items():
        s = sum(r["coins"] for r in rows); p = sum(r["pct"] for r in rows)
        if s != n: bad.append("bucket table [%s]: coins %d != %d" % (name, s, n))
        if abs(p - 100) > 1.5: bad.append("bucket table [%s]: shares sum to %.1f" % (name, p))

    # 3 PnL conserved (each table's total PnL sums to the overall total)
    tot = round(sum(t["pnl_usd"] for t in tk))
    for name, rows in F["per_coin_buckets"].items():
        s = sum(r["total_pnl"] for r in rows)
        if abs(s - tot) > max(3, abs(tot) * 0.002):
            bad.append("bucket table [%s]: pnl %d != %d" % (name, s, tot))

    # 4 action combos
    ac = (F.get("action_combos") or {}).get("rows") or []
    if ac:
        if sum(r["coins"] for r in ac) != n: bad.append("action_combos: coins not conserved")
        if abs(sum(r["total_pnl"] for r in ac) - tot) > max(3, abs(tot) * 0.002):
            bad.append("action_combos: pnl not conserved")

    # 5 ladder: coins included <= total coins
    dj = F.get("buy_sell_ladder") or {}
    if dj.get("coins_included", 0) > n:
        bad.append("buy_sell_ladder: coins_included > total coins")

    # 6 per-fill distribution sample sizes
    ntr = len(S.get("trades") or [])
    for item in (F.get("per_fill_distribution") or {}).values():
        if isinstance(item, dict) and item.get("sample", 0) > ntr:
            bad.append("per_fill_distribution: sample %d > fills %d" % (item["sample"], ntr))

    # 7 every representative sample must exist verbatim in store's token list
    ids = {t.get("token") or t.get("address") for t in tk}
    for smp in (F.get("sample_coins") or {}).get("samples") or []:
        if isinstance(smp, dict):
            k = (smp.get("coin") or {}).get("token")
            if k and k not in ids:
                bad.append("sample coin %s is not in store's token list" % k)

    print("%-9s %s" % (a, "OK  all groups passed" if not bad else "FAIL " + " | ".join(bad)))
    fail += len(bad)

if not ran:
    print("no okx/data/_fin2_*.json present — nothing was cross-checked "
          "(run okx/engine/store.py to produce one)")
    sys.exit(1)
sys.exit(1 if fail else 0)
