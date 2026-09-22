#!/usr/bin/env python3
"""Build two **test fixtures** from a real layer-3 output.

⚠️ These are not agent reports, they are test data for verify.py — `good` must pass everything and
   `bad` must trip every class of check. Values like the address are read out of the data, never
   typed by hand (completing a base58 suffix from memory has silently produced wrong data twice).
"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import verify

L3 = json.load(open(sys.argv[1], encoding="utf-8"))
dg, S = verify.digest(L3), "›"
addr, win = L3["address"], L3["window"]

# Real values read out of the data, never typed
row_hold = next(r for r in L3["per_coin_buckets"]["exit·time: seconds held"] if r.startswith("15-60s"))
combo    = L3["action_combos"]["rows"][0]
net      = L3["pnl_distribution"]["net"]
basis    = L3["based_on"]
gap      = L3["not_visible"]
winrate  = L3["pnl_distribution"]["win_rate_pct"]
seg0     = L3["by_period"]["rows"][0]

good = dict(
    address=addr, window=win, layer3_digest=dg,
    facts=[
        dict(id="f0", say="this profile is based on %d visible coins and %d fills"
                          % (basis["coins"], basis["fills"]),
             cite=S.join(["based_on"])),
        dict(id="f0b", say="another %d coins were traded but have no visible detail" % gap["total"],
             cite=S.join(["not_visible", "total"])),
        dict(id="f1", say="the most common buy size is listed in the source",
             cite=S.join(["per_fill_distribution", "buy_size"])),
        dict(id="f2", say="41 coins were bought within 10s of creation, 42.3% of the total",
             cite=S.join(["per_coin_buckets", "entry·time: seconds after coin creation", "<10s"])),
        dict(id="f3", say="43 coins were held 15-60s, 44.3% of the total",
             cite=S.join(["per_coin_buckets", "exit·time: seconds held", "15-60s"])),
        dict(id="f4", say="71 coins were sold in one fill, 73.2% of the total",
             cite=S.join(["per_coin_buckets", "exit·style: number of sells", "1"])),
        dict(id="f5", say="the most common action combo covers 38 coins, 39.2% of the total",
             cite=S.join(["action_combos", "rows", combo.split("|")[0].strip()])),
        dict(id="f6", say="89.3% of the bought volume sits in the up0~10 band relative to the first buy",
             cite=S.join(["buy_sell_ladder", "rows", "up0~10"])),
        dict(id="f7", say="the coins come from pumpfun and launchlab",
             cite=S.join(["coin_attributes", "by_launchpad"])),
        dict(id="f8", say="net PnL over the window is %d USD" % net,
             cite=S.join(["pnl_distribution", "net"])),
        dict(id="f8b", say="the win rate over the window is %s%%" % winrate,
             cite=S.join(["pnl_distribution", "win_rate_pct"])),
        dict(id="fw", say="entry curve progress is listed in the source",
             cite=S.join(["per_coin_buckets", "entry·position: curve progress"])),
        dict(id="fw2", say="exit curve progress is listed in the source",
             cite=S.join(["per_coin_buckets", "exit·position: curve progress"])),
        dict(id="f9", say="the first segment's win rate is listed in the source",
             cite=S.join(["by_period", "rows", seg0.split("|")[0].strip()])),
    ],
    profile=dict(say="hold time clusters at 15-60s (43 coins, 44.3%) and 71 coins are sold in one fill",
                 legs=["f3", "f4"]),
    result=dict(say="net PnL over the window is %d USD at a %s%% win rate" % (net, winrate),
                legs=["f8", "f8b"]),
    edge=dict(source="execution",
              say="41 coins were bought within 10s of creation (42.3%) and buy sizes repeat on the "
                  "same values — the edge is in execution",
              follower="that one beat is his edge and a follower cannot get it; he is out within "
                       "minutes, so a follower takes the other side of his sells",
              legs=["f1", "f2", "f3"]))

bad = json.loads(json.dumps(good))
bad["layer3_digest"] = "deadbeef1234"                                   # (1) fingerprint
# ⚠️ Locate by id, never by index — hit 2026-09-20: after f0/f0b were inserted at the front,
#    indices 0/1 pointed at those two, the (2)/(3) breakages landed on them, and they were then
#    removed by the (9) line — so (2) and (3) both silently stopped firing.
#    **Positions move, ids do not.**
_by = {f["id"]: f for f in bad["facts"]}
_by["f1"]["cite"] = S.join(["per_fill_distribution", "no such key"])    # (2) cite does not resolve
_by["f2"]["say"] = "45.0% of coins were bought within 10s of creation"  # (3) a computed number
bad["profile"]["legs"] = ["f99"]                                        # (4) leg does not exist
bad["profile"]["say"] = "the order entry is Axiom, covering 693 of 694 fills"  # (5) new numbers
bad["facts"] = [f for f in bad["facts"] if f["id"] != "f9"]             # (6) a block uncovered
bad["edge"]["say"] = "this is a bot"                                    # (7) banned word
bad["profile"]["say"] += ", and most coins are closed in one fill"      # (8) vague quantifier
bad["facts"] = [f for f in bad["facts"] if not f["id"].startswith("f0")]      # (9) no sample boundary
bad["edge"]["follower"] = "a follower has to get the first buy in within 10 seconds"
#    ^ (10) copy-this checklist; it also drops "cannot get it" → (13)
del bad["result"]                                                       # (14) a section missing

for name, obj in (("good", good), ("bad", bad)):
    p = os.path.join(HERE, "fixtures", name + ".report.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("wrote", p)
