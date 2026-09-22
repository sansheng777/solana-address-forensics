#!/usr/bin/env python3
"""okx/agent/tests/test_agent.py — layer-4 offline self-test. No network, no model credentials.

    docker run --rm -v /root/meme:/work -w /work node:22 python3 okx/agent/tests/test_agent.py

It tests two things: ① the `good` fixture must pass everything ② **every class of violation in the
`bad` fixture must fire**. A tripwire that does not trip is not a tripwire — a rule adopted after
the four tripwires in layer 3 were each verified this way.
"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)
import verify, render

L3 = json.load(open(os.path.join(os.path.dirname(AGENT), "data", "_f4_d1_4vw54B.json"),
                    encoding="utf-8"))
G = json.load(open(os.path.join(HERE, "fixtures", "good.report.json"), encoding="utf-8"))
B = json.load(open(os.path.join(HERE, "fixtures", "bad.report.json"), encoding="utf-8"))
n = 0


def ok(cond, msg):
    global n
    n += 1
    assert cond, "FAIL " + msg


# ── the fixtures must match the current layer 3 (regenerate them whenever layer 3 changes) ──
ok(G["layer3_digest"] == verify.digest(L3),
   "the good fixture's digest does not match the current layer 3 — rerun make_fixtures.py")

# ── ① good passes everything ──
v = verify.check(G, L3)
ok(not v, "the good fixture should pass; it produced %d violation(s): %s" % (len(v), v))

# ── ② every class in bad must fire ──
v = verify.check(B, L3)
for tag, what in (("(1)", "digest mismatch"), ("(2)", "cite does not resolve"),
                  ("(3)", "number not in the cited row"), ("(4)", "leg does not exist"),
                  ("(5)", "new number introduced"), ("(6)", "block not covered"),
                  ("(7)", "banned word"), ("(8)", "vague quantifier"),
                  ("(9)", "sample boundary missing"), ("(10)", "checklist content in follower"),
                  ("(13)", "execution edge not carried into follower"), ("(14)", "section missing")):
    ok(any(x.startswith(tag) for x in v), "class %s (%s) did not fire: %s" % (tag, what, v))
ok(len(v) == 12, "the bad fixture should produce exactly 12 violations (one per class), got %d: %s"
                 % (len(v), v))

# ── the resolver itself ──
ok(verify.resolve(L3, "pnl_distribution›net")[0], "resolve cannot walk into pnl_distribution›net")
ok(not verify.resolve(L3, "pnl_distribution›no such key")[0],
   "resolve should fail on a key that does not exist")
ok(verify.resolve(L3, "per_coin_buckets›entry·time: seconds after coin creation›<10s")[1]
   .startswith("<10s"), "resolve failed to match a row by its leading label")
ok("694" in verify.nums("0.0244444 ×694 (100.0%)"),
   "nums must extract a count written as ×694 (an ASCII x would hide it — see features._m)")
ok("382" in verify.nums("pnl -382"), "nums normalises by absolute value, so -382 must match 382")
# 2026-09-21: an identifier is not a number. The model wrote (f10) into the body and the 10 that
# came out produced 8 false violations that two rewrites could not clear.
ok(verify.nums("sources (f10) and (r2)") == set(), "f10 / r2 are identifiers, not numbers")
ok(verify.nums("f10") == set(), "excluding letters alone is not enough: f10 leaves a 0 behind")
ok(verify.nums("42 coins within 15-60s") == {"42", "15", "60"}, "normal numbers must not be lost")
ok(verify.nums("pnl +1,234") == {"1234"}, "thousands separators must be absorbed")
ok(verify.nums("no fills 01-02,07-15,17-18") == {"01", "02", "07", "15", "17", "18"},
   "a comma not followed by exactly 3 digits is not a separator: 02,07 must not become 0207")
ok(verify.nums("total 12,345,678 USD") == {"12345678"}, "multi-group separators must be absorbed")

# ── a minimal report builder: all three sections, change one thing at a time ──
_L = {"based_on": {"coins": 1}, "not_visible": {"total": 0}, "window": ["2026-09-14", "2026-09-18"]}


def _rep(fact_say="1 coin", profile="1 coin", result="1 coin", edge_say="1 coin",
         follower="a follower is a beat behind", source="none"):
    return dict(address="a", window=["2026-09-14", "2026-09-18"], layer3_digest=verify.digest(_L),
                facts=[dict(id="f1", say=fact_say, cite="based_on")],
                profile=dict(say=profile, legs=["f1"]),
                result=dict(say=result, legs=["f1"]),
                edge=dict(source=source, say=edge_say, follower=follower, legs=["f1"]))


def _tags(rep, tags, l3=None):
    return [x for x in verify.check(rep, l3 if l3 is not None else _L)
            if any(x.startswith(t) for t in tags)]


# ── (8) vague quantifiers ──
_v8 = lambda say: _tags(_rep(fact_say=say), ["(8)"])
ok(_v8("most coins are closed in one fill"), "'most' must be caught")
ok(_v8("the majority of coins graduated"), "'majority' must be caught")
ok(_v8("roughly 50% of coins"), "'roughly 50' must be caught")
ok(_v8("about 200 coins"), "'about 200' must be caught")
ok(_v8("over 200 coins"), "'over 200' must be caught")
ok(_v8("a handful of fixed values"), "'a handful of' must be caught")
ok("context" in (_v8("most coins") or [""])[0], "a violation must carry surrounding context")
# ★ 2026-09-22: 'most common' is the layer-3 field name — a bare substring match flags a correct fact
ok(not _v8("the most common buy size is 0.0244444"),
   "'the most common' is a layer-3 field name and must not be flagged")
ok(not _v8("the combo with the most coins covers 207 coins"),
   "'the most X' is a superlative ranking, not a vague quantifier (measured 2026-09-22)")
ok(not _v8("89.3% of volume sits in the up0~10 band"),
   "a '0~10' bucket label is not an approximation — merged ranges are check 5's job")
ok(not _v8("the win rate is 61.4% across 585 coins"), "plain numbers must not be flagged")

# ── (7) banned words ──
_v7 = lambda say: _tags(_rep(edge_say=say), ["(7)"])
for bad_say, why in (("this is a bot", "identity claim"),
                     ("a script is running this", "identity claim"),
                     ("someone is watching the screen manually", "identity claim"),
                     ("a very high win rate", "degree without a baseline"),
                     ("significantly better than the rest", "strength word without a baseline"),
                     ("an impressive, disciplined approach", "evaluative words"),
                     ("he should tighten his stops", "prescription"),
                     ("he needs to improve his loss ratio", "prescription")):
    ok(_v7(bad_say), "%s must be caught: %s" % (why, bad_say))
ok(not _v7("no human could place that many orders that fast"),
   "'no human could' describes the data, not the operator — it must not be flagged")
ok(not _v7("the 48 coins at 30~40% progress had an 85.4% win rate, higher than the overall 61.4%"),
   "a comparison carrying numbers is not an evaluative word")
ok(len(_v7("he should tighten stops and he should widen entries")) == 2,
   "every occurrence of one rule must be reported separately (otherwise only half gets fixed)")

# ── (10) follower-only bans: the four families the user identified on 2026-09-20 ──
def _v10(follower, fact_say="1 coin"):
    return _tags(_rep(fact_say=fact_say, follower=follower), ["(10)"])


for bad_say, why in (
        ("a follower has to get the first buy in within 10 seconds",
         "④ treats following as a speed problem; the problem is the exit"),
        ("keep the first buy size at 150-350 USD", "① size has no causal link to PnL"),
        ("the router has to handle that throughput", "② tooling says nothing about following"),
        ("a follower must be able to absorb a 505 dollar loss", "③ that is his number, not theirs"),
        ("watch during his active hours", "⑤ activity hours belong in edge.say")):
    ok(_v10(bad_say), "%s — must be caught by (10): %s" % (why, bad_say))
# The same words inside facts / profile are normal and must not be flagged
ok(not _v10("a follower may arrive after he has already left",
            fact_say="first buy sizes cluster at 150-350 USD and one router covers every fill"),
   "(10) scans follower only; sizes and tooling are normal facts")
ok(not _tags(_rep(profile="first buy sizes cluster at 150-350 USD"), ["(10)"]),
   "(10) does not scan profile")
for good_say in ("451 coins are sold in one fill, so a follower needs time to see it and act",
                 "84.4% enter and exit on the inner market, so the sell lands straight on the curve",
                 "leftover is under 1% on 66 coins: exits are complete, not partial",
                 "the 48 coins he bought at 30~40% progress had an 85.4% win rate, and that is visible"):
    ok(not _v10(good_say), "a normal exit-side or what-to-watch sentence must not be flagged: %s"
                           % good_say)

# ── (13) / (14) skeleton and cross-sentence consistency ──
ok(_tags(_rep(source="execution", follower="a follower takes the other side of his sells"), ["(13)"]),
   "source=execution with no 'cannot get it' in follower must fire (13)")
ok(_tags(_rep(source="both", follower="a follower takes the other side of his sells"), ["(13)"]),
   "source=both must fire it too")
ok(not _tags(_rep(source="execution",
                  follower="a follower is always a beat behind and cannot get that part"), ["(13)"]),
   "once it is carried through, (13) must not fire")
ok(not _tags(_rep(source="execution",
                  follower="that edge is out of reach for a follower"), ["(13)"]),
   "'out of reach' also satisfies (13)")
ok(not _tags(_rep(source="selection", follower="the coins he bought early are visible"), ["(13)"]),
   "selection does not require a 'cannot get it' sentence")
ok(_tags(_rep(source="robot"), ["(14)"]), "an illegal source must fire")
ok(_tags(_rep(follower=""), ["(14)"]), "an empty follower must fire")
_r = _rep(); del _r["result"]
ok(_tags(_r, ["(14)"]), "a missing result section must fire")
_r = _rep(); _r["profile"]["legs"] = []
ok(_tags(_r, ["(4)"]), "an empty legs list must fire (4)")
ok(_tags(_rep(profile="999 coins in total"), ["(5)"]), "a number outside the legs must fire (5)")
ok(_tags(_rep(follower="a follower is a beat behind; he sells 88% of coins in one fill"), ["(5)"]),
   "follower is checked for new numbers too")
ok(not _tags(_rep(profile="1 coin in total"), ["(5)"]), "a number the legs contain must not fire")

# ── (5) tightened + ranges (2026-09-21: "the 62 coins at 10~40% progress") ──
_L2 = {"based_on": {"coins": 1}, "not_visible": {"total": 0},
       "window": ["2026-09-14", "2026-09-18"],
       "per_coin_buckets": {"entry·position: curve progress":
                            ["10~20% | 21 coins 3.6% | win 100.0%",
                             "20~30% | 42 coins 7.2% | win 81.0%",
                             "30~40% | 56 coins 9.6% | win 83.9%"]},
       "by_period": {"rows": ["2026-09-18 | 96 coins | winners 62 | win 64.6%"]}}


def _rep2(fact_say, sec_say, cite="per_coin_buckets›entry·position: curve progress", extra_fact=None):
    facts = [dict(id="f1", say=fact_say, cite=cite)]
    if extra_fact: facts.append(dict(id="f2", **extra_fact))
    legs = [f["id"] for f in facts]
    return dict(address="a", window=["2026-09-14", "2026-09-18"], layer3_digest=verify.digest(_L2),
                facts=facts, profile=dict(say=sec_say, legs=legs),
                result=dict(say="1", legs=legs),
                edge=dict(source="none", say="1", follower="a follower is a beat behind", legs=legs))


ok(_tags(_rep2("10~20% has 21 coins", "he bought 62 coins at 10~40% progress",
               extra_fact=dict(say="win rates per day are listed", cite="by_period")), ["(5)"], _L2),
   "★ 62 lives only in a leg's cited block, not in any fact's say → (5) must fire (the v2b leak)")
ok(not _tags(_rep2("10~20% has 21 coins", "he bought 21 coins at 10~20% progress"), ["(5)"], _L2),
   "a number a fact wrote out must not fire")
ok(_tags(_rep2("10~20% has 21 coins", "he bought coins at 10~40% progress"), ["(5)"], _L2),
   "a range merged in the body must fire")
ok(_tags(_rep2("10~40% progress has 21 coins", "1"), ["(3)"], _L2),
   "a range merged inside a fact must fire (3)")
ok(not _tags(_rep2("10~20% has 21 coins, 20~30% has 42", "10~20% has 21 coins"), ["(3)", "(5)"], _L2),
   "ranges printed in the table must not fire")
ok(not _tags(_rep2("1", "the window is 2026-09-14 ~ 2026-09-18"), ["(5)"], _L2),
   "a date is not a range")
ok("15~60" in verify.ranges("15-60s, 226 coins") and "150~350" in verify.ranges("150-350 USD"),
   "ranges must pick up the hyphen form")
ok(verify.ranges("2026-09-14") == set(), "a leading year must not be read as a range")

# ── digest must survive non-string keys (this crashed a real paid call on 2026-09-21) ──
ok(verify.digest({"a": {None: 3, "pumpfun": 4}}) == verify.digest({"a": {None: 3, "pumpfun": 4}}),
   "★ a None dict key must not crash digest — sort_keys would compare None against str")
ok(verify.digest({"a": {None: 3}}) != verify.digest({"a": {"x": 3}}),
   "normalising keys must still tell different data apart")
_l3n = {"based_on": {"coins": 1}, "not_visible": {"total": 0},
        "coin_attributes": {"by_launchpad": {None: 2}}}
_rn = dict(address="a", window=["d", "d"], layer3_digest=verify.digest(_l3n),
           facts=[dict(id="f1", say="1 coin", cite="based_on")],
           profile=dict(say="x", legs=["f1"]), result=dict(say="x", legs=["f1"]),
           edge=dict(source="none", say="x", follower="a follower is a beat behind", legs=["f1"]))
ok(not [x for x in verify.check(_rn, _l3n) if x.startswith("(1)")],
   "layer 3 carrying a None key must still pass the fingerprint check")

# ── rendering: one source for both outputs, endnote numbers must line up ──
md = render.render(G, L3)
ok(md.startswith("# Address behaviour report"), "the rendered heading is wrong")
ok(G["address"] in md, "render did not include the address")
for h in ("## What he does", "## Does he make money",
          "## Where his edge is, and whether a follower can get it",
          "### Appendix — facts and sources"):
    ok(h in md, "render is missing '%s'" % h)
ok("Where a follower would end up:" in md, "render did not output follower")
ok("execution (speed)" in md, "render did not translate the source label")
for i in range(1, len(G["facts"]) + 1):
    ok(("[%d]" % i) in md, "appendix marker [%d] is missing" % i)
ok("[%d]" % (len(G["facts"]) + 1) not in md, "appendix numbering must not exceed the fact count")
ok("(sources [" in md, "body sections must carry endnote pointers")

print("OK  layer-4 self-test passed (%d assertions)" % n)
