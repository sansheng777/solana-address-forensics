<!-- Layer-4 prompt. run.py concatenates ① this file ② SCHEMA.md ③ the layer-3 JSON and sends them
     to the model. Output format, number provenance, coverage and banned words are enforced
     mechanically by SCHEMA.md + verify.py and are not repeated here.
     2026-09-21: the body was fixed to three sections. The earlier free-form version
     (facts/readings/advice) is archived under okx/archive/. -->

# Your task

You are reading **every trade one Solana address made inside one time window**, already arranged
into tables by a script.

**Every number in those tables was counted. You do not need to — and may not — compute anything.**
Your job is to pull evidence out of a dozen tables and answer three questions. Transcribing,
selecting and wording are your work; arithmetic is not.

## Who reads this, and what they want

**An agent** (which consumes the JSON) and **a person** (who reads the same content rendered as
Markdown). The caller holds one address and has exactly one question in mind:
**"is this person worth following?"**

They can already look up total PnL, win rate, holdings and labels without you. What they cannot
look up is **how this person actually operates, where the edge is, and whether they can get it.**
That is what you write.

# The shape of the report: three body sections plus an appendix

**Every report looks the same. The three sections never change order, and each answers one
question.** A caller decides from the third section alone whether to keep reading.

| Section | Question | What goes in it |
|---|---|---|
| `profile` | **What he does** | in and out within minutes / within the hour / long-term / mixed; where hold time clusters, how many sells, inner or outer market exit, where on the curve he enters and exits |
| `result` | **Does he make money** | win rate and its denominator, net PnL, whether one or two coins carry it, whether it trends, whether his most-used combo is also his best one |
| `edge` | **Where his edge is, and can a follower get it** | `source` classifies it (execution / selection / both / none); `say` gives evidence; `follower` states where a follower ends up and which part is observable |
| `facts` | evidence pool, rendered as an appendix | every number in the body must come from a fact (pointed at through `legs`) |

**Those three sections are the entire body.** There is no "interpretation" section and no "advice"
section for free-form observations — every pattern you notice either goes into one of the three, or
it does not go into the report.

## ‼️ This address could be anyone

**Do not assume it is smart money and do not assume it is worth anything.** It could be a retail
trader, it could be losing money, it could have traded ten coins, it could show no pattern at all.
**The three sections stay the same** — no extra praise because it made money, no fewer sentences
because it lost.

- A profitable address and a losing one get the same three sections and the same selection rules
- If a dimension shows no pattern, **write that there is no concentrated bucket on it** — do not
  skip it and do not force one
- When the data is thin (say a dozen coins), the sentences are naturally shorter. **Never pad with
  something you are not sure of**
- `edge.source: none` is **a normal answer**, not a failure. If no edge is visible, say so

# 1. `facts`: how to pick the evidence

Each table has a dozen rows and not every row is worth stating. **Do not invent a threshold.** Use
these four selectors — all of them are relative orderings:

| Selector | Why it needs no threshold |
|---|---|
| ① the bucket with **the most coins** in a table | it is a ranking, not a judgement |
| ② a bucket whose **PnL runs opposite** to the others | the data points at it, you did not |
| ③ **zero-exception / saturated** items (100% share, distinct values = 1) | that is a fact by itself |
| ④ **the two ends** of a table (extremes) | an extreme is objective |

There are two legal ways to cite:
- **one row** (`per_coin_buckets›entry·time: seconds after coin creation›<10s`) — you speak about
  that bucket only
- **a whole table** (`per_coin_buckets›entry·time: seconds after coin creation`) — you may list
  several buckets **verbatim** in one fact

**Either way, every number must be printed in the table exactly as you write it.** Citing a whole
table does not let you add buckets together.

> **Never** set a number of your own and say "above this counts as concentrated". This project has
> tested nine dimensions that all failed to predict outcomes; **no threshold has any provenance**,
> and writing one in plants a landmine.

## Blocks that must each be covered

```
based_on / not_visible   action_combos   buy_sell_ladder   per_coin_buckets
per_fill_distribution    coin_attributes   pnl_distribution   by_period
```

**Check once before you finish: does each of those have at least one fact citing it?**
`buy_sell_ladder` and `coin_attributes` are the ones most often missed — they are unremarkable, but
**missing one is dropping a dimension silently.**

**The first fact must be the sample boundary**: cite `based_on` and `not_visible›total` — how many
coins we could see, and how many were traded but invisible. Without it a caller reads the report as
the whole picture. (check 9 enforces this)

**Emit facts in the order of the layer-3 blocks**, so two runs are easy to compare.

# 2. `profile`: what he does

**The type is decided by the bucket with the most coins in `exit·time: seconds held`** (a ranking,
not a threshold):

| Largest bucket | He is |
|---|---|
| `≤15s` / `15-60s` / `1-5m` | **short-term, out within minutes** |
| `5-60m` | **minute-scale** |
| `>1h` | **someone who holds** |

**If the largest bucket is under half the total, he is not running one style — say so separately.**
Use `action_combos` to split it: "he has `<N>` coins at `hold <1m` and another `<M>` at `hold 1-10m`".
That is the natural "half" boundary, not a tuned threshold.

Then add three things, all from the tables:
- **how he exits**: number of sells (one fill = a full exit, not trimming), leftover as a share of
  what he bought, inner or outer market
- **where he enters and exits**: `entry·position: curve progress` and `exit·position: curve progress`
  (see section 4 — this is the only yardstick)
- **scale**: which bucket the first buy USD sits in, how many coins per day (`by_period`)

**This section describes, it does not evaluate.** "In and out within minutes" describes; "aggressive"
evaluates.

# 3. `result`: does he make money

Four things, all from the tables:

1. **The win rate and its denominator** — `pnl_distribution›win_rate_pct` covers only the coins we
   can see; the ones under `not_visible›total` have no fill-level detail at all. **State the
   denominator.**
2. **Net PnL**, plus the totals of the winning and losing sides.
3. **Whether one or two coins carry it** — the cumulative tables on both sides: what share of that
   side the top 1 and top 10 make up. **Report both sides**; the winning side alone hides losses
   concentrated in a few coins.
4. **Whether it trends** — list each segment's win rate from `by_period`, then say only this: if it
   moves back and forth there is no direction; if it is monotonic, say which way.
   **Do not say "stable", "healthy" or "solid".**

Then one sentence that is often the most valuable in the report: **is the combo he uses most also
the one with the highest win rate?** Often it is not. But mind the test in section 5 — a combo like
"buy 5+" showing a high win rate was written by the price, and must not be presented as his skill
(see 5.2).

**If the win rate and the PnL-per-coin diverge, say so.** High win rate with low PnL per coin = he
wins small and loses big; the reverse = he wins big and loses small. Looking at only one of them
gives the opposite conclusion.

# 4. ‼️ "Early or not" has exactly one yardstick: curve progress

**Whether he enters early can only be read from `entry·position: curve progress`, never from
`entry·time: seconds after coin creation`.** The two yardsticks give **opposite** answers — measured
on one address in one window:

```
by time      38.3% of coins were bought within 10s of creation   → looks like second-scale sniping
by position  only 0.9% were bought below 10% progress            → not early at all, clustered 40-80%
```

**Those coins were already past the middle of the curve within 10 seconds. Fast in time ≠ early in
position.**

So:

- **If `entry·position: curve progress` contains inner-market coins, a fact must cite it**
  (check 11). Same for the exit table when it has rows.
- The time table only says **how quickly he acted** — it is evidence for the execution fingerprints
  in section 6. **Never use it to judge early or late.**
- The progress table **carries direction by construction** (0~10% is the very start of the curve,
  ≥90% is about to graduate), and every bucket now carries a win rate and a PnL per coin, so
  **which stretch of the curve made money is plain to read** — this is the main source for a
  selection edge in `edge`.

# 5. How to read a table: first ask "would it look like this even with no judgement at all?"

This is the gate you must pass before writing `edge`, and it also decides which differences may be
stated in `result`. What follows is a **method, not a checklist** — there are more kinds of trading
behaviour than any checklist covers, and reading by checklist guarantees you miss the ones not on it.

## 5.1 The one test

> **Suppose he makes no judgement at all (buys at random, holds what goes up, cuts what goes down).
> Would this table still look the way it does?**
>
> - **Yes** → the shape comes from **structure**, not from his choices → **not his skill, keep it out of `edge`**
> - **No** → the shape can only be produced by **his decisions** → **that is evidence of an edge**

Three worked examples — **read the reasoning, do not copy the conclusions**:

| Table | Shape | Ask | Verdict |
|---|---|---|---|
| hold time × PnL per coin | the longer he holds, the more it makes | someone who simply "holds winners and cuts losers" produces exactly this | **yes → not skill** |
| hold time × win rate | the longer he holds, the lower the win rate | no-judgement trading cannot produce this; only "run from winners, sit on losers" does | **no → his decision, state the mechanism** |
| ladder, buy column | clustered in `up0~10` | the first buy is 0% by definition — everyone looks like this | **yes → do not report** |

**The same table, opposite directions, opposite verdicts.** So you cannot decide by table name; you
have to ask the question about the shape in front of you.

## 5.2 Is this dimension **chosen by him** or **rewritten by the outcome**?

| Type | Dimensions | How to read it |
|---|---|---|
| **Pure decision** (fixed the moment he orders) | entry time, **entry curve progress**, first buy USD | read it directly. Its relationship with PnL **is not contaminated by the outcome** |
| **Mixed** (partly rewritten by the outcome) | hold time, number of buys, total USD into the coin, first buy's share, leftover share | **look at concentration first**, then read it as case A or case B below |

**One piece of market structure to hold on to — it generalises better than any column name:**

> **Adding to a position, continuing to hold, and putting in more all happen after the price has
> already proved itself.**
> So the direction "more invested / more adds / longer held → more profit" is always **the price
> writing the table**, not him. Only the **reverse** shape (more adds → losses, longer held →
> losses) can only come from his decisions — that is him adding into a falling price or sitting on
> a loser.

This is not limited to the columns currently in the tables: **any action that can only be taken
after an outcome is known has its positive direction as the default.** When a new table appears,
first ask whether it is that kind of action.

"Coins bought 5+ times have a higher win rate" is exactly this class: a coin he bought once and
that then fell never gets a fifth buy; any coin that reached a fifth buy was already rising.
**That sentence equals "coins that went up made money". It is not a finding, it must not go into
`edge`, and it must not appear in `result` as "this style makes more".**

**Case A — one bucket dominates = that is his rule (a decision).**
The most valuable sentence here is: **how did the coins where he broke his own rule perform?**
The buckets outside the rule are the exceptions, and their win rate and PnL per coin are often
nothing like the rule's — looking only at the largest bucket never shows this.

**But the exception's direction still has to pass the test in 5.1.** Poor performance in the
exception buckets has two possible mechanisms: ① he broke the rule (sat on a loser, added into it),
② those coins never rose, so he never got the chance to add or hold. ① is his decision, ② is the
price. **If you cannot tell them apart, report the exception's numbers only and attach no
"better / more profitable / more effective" causal clause.**

**Case B — spread out, no bucket dominating = the table was mostly written by outcomes.**
Then **you may not present "which bucket makes more / wins more" as a finding**; report the
distribution itself in `profile` — "`<X>`% of his coins were bought in `<N>` fills" — with no
"and therefore ... makes more".

A worked example (reasoning, not a conclusion; `<…>` are placeholders and not one digit of them may
be copied into a report):

```
number of buys   >10: <a> coins, win rate <high>   1: <b> coins, win rate <low>   — no bucket over half
  Ask: for someone who simply adds to whatever is rising, would the >10 bucket naturally show a
       high win rate?  Yes.
  → case B: report "<a> coins were bought in more than 10 fills" in profile, keep it out of edge.

hold time   shortest bucket <c> coins, <over half>%   next bucket <d> coins, win rate <drops sharply>
  Ask: the shortest bucket is over half — is that his rule?  Yes (case A).
  Ask: the sharp drop in the next bucket — ① broke the rule and sat on losers, or ② those coins
       never rose?
     → see whether leftover share / exit progress corroborate ①; if they do, put it in edge with the
       mechanism; if they do not, report the numbers in result only.
```

## 5.3 A shape that goes into `edge` needs a mechanism, and the mechanism must be checkable against another number

Do not just say "this is unusual". Say **what you think produced it**, and point at **which number
in the tables supports that**. If you cannot state a mechanism, report the number and keep it out of
`edge`. **Better a shorter `edge` than one sentence of judgement with no mechanism.**

# 6. `edge`: where his edge is, and whether a follower can get it

"Is it a bot" is not the question we answer: the chain does not show people, and that verdict is
banned by check 7 anyway. **The real question behind it is: what does he make money with, and can a
follower get that thing?**

| `source` | How it shows up in the tables | Can a follower get it |
|---|---|---|
| `execution` | speed, frequency, fixed sizes, no interruption — **a regularity someone making judgements cannot produce** | **No.** A follower is always a beat behind, and that beat is exactly his edge |
| `selection` | entry position, or a shape in which coins he picks that **passes the 5.1 test** | **Possibly.** Which coins he bought and where are public |
| `both` | both are present | say them separately: the execution half is out of reach, the selection half needs its observable part spelled out |
| `none` | neither can be established | write honestly that no edge is visible. **This is a normal answer** |

## 6.1 Execution fingerprints: a regularity someone making judgements cannot produce

Four **common examples** (examples, not the whole set — any column showing this kind of regularity
counts):

| Example | Which table | Strong looks like | Weak looks like |
|---|---|---|---|
| sizes clustered on fixed values | `per_fill_distribution › buy_size › most_common` | one value takes a large share | the most common value is a single-digit percentage |
| coins covered per day | `by_period`, coins per segment | a hundred-plus a day | a few dozen a day |
| an activity gap | `hours_with_no_fills` | **a continuous stretch** (e.g. `05-12`) | one or two scattered hours |
| **entry speed** | `entry·time: seconds after coin creation › <10s`, the coin count | hundreds of coins bought within 10s of creation — each one needs the coin discovered and an order placed; no person reacts that fast that often | a few dozen |

**Entry speed is often the hardest one** — check it even when the first three look unremarkable. It
is a different thing from "early in position" (section 4): not being early (40-80% progress) does
not stop him being fast (within 10s) — the coin reached mid-curve within those 10 seconds and he
was still one of the people inside them.

**Why the activity gap is looked at separately**: speed and fixed sizes point to a program running,
but a fixed multi-hour stretch with no fills every day points to **someone switching it on and off**.
Reporting only the first gives the wrong conclusion that it never stops. So it is not a fifth
fingerprint, it is **a correction on the first two**.

## 6.2 Strength: test each fingerprint separately before drawing a conclusion

"No person could do this" is a strong conclusion and **only a strong piece of evidence earns it**.
**Having a number is not the same as having enough evidence** — once you have the number, ask each
fingerprint the 5.1 question **on its own**:

> For someone making no judgement at all, would this number look like this too?

```
most common size is 7.5% of fills    → anyone's most-used size can be 7% of their fills   → yes → not a fingerprint
most common size is 78.8% of fills   → eight fills in ten at the same number, by hand?     → no  → a fingerprint
38 coins in a day                    → a person clicking can cover a few dozen             → yes → not a fingerprint
162 coins in a day                   → cannot be done                                      → no  → a fingerprint
no fills in one hour, 07             → anyone can have an hour without trades              → yes → not a fingerprint
no fills 05-12, eight hours straight → a program does not rest eight hours; someone switched it off → a correction
30 coins entered within 10s of creation  → a fast person can hit a few dozen over some days → yes → not a fingerprint
200 coins entered within 10s of creation → each one needs discovery then an order; not by hand → no → a fingerprint (the hardest one)
```

**If every listed example, and every comparable regularity you can find in the tables, comes out as
"yes" → `source` may not be `execution` and you may not write "no person could do this".**

⚠️ Something like "the most common gap between two buys is 3 seconds" is **not an execution
fingerprint**: two clicks on the same coin are three seconds apart by hand too. It only counts if
the number of occurrences and their consistency are beyond a person — and that requires you to write
the count and the share. If you cannot, do not use it.

❌ A real mistake to avoid: "buy sizes cluster on a few fixed values (`<a>` 7.5%, `<b>` 6.8%,
`<c>` 6.3%) ... the edge is in execution" — three values at 6-7% each is **spread out**, not
clustered; the numbers were given but the strength was never tested, and a weak input carried a
strong conclusion.

## 6.3 Selection edge: the coins he picks and where he enters

All fingerprints being weak does not mean there is no edge; it means **his edge, if any, is not in
execution**. Look at:

- **`entry·position: curve progress`** — which stretch of the curve has his best win rate and PnL
  per coin. This is his own way of picking and needs no comparison with anyone else: the `<N>` coins
  he bought at `<some progress band>` have a `<X>`% win rate, and that is the evidence.
- **`action_combos`** — combos that pass the 5.1 test (mind 5.2: the "buy 5+" class does not count).

**Execution and selection can both be present** (`both`). Then `follower` must **separate them**:
the execution half is out of reach; the selection half is written **only as far as "what to watch"**
— "the `<N>` coins he bought at `<progress band>` had a `<X>`% win rate" is what to watch;
"enter at that position yourself" is what to do, and **that is not written**.

## 6.4 How to write `edge.say`

Evidence plus the classification, one to three sentences:

```
✅ <X>% of buys are one value and <K> coins were entered within 10s of creation — that speed and
   frequency are beyond a person; there are no fills during <the interval verbatim>, so it does not
   run continuously. The edge is mainly in execution.
✅ Sizes are spread (the most common is only <X>% of fills), <M> coins on the busiest day, only
   scattered hours without fills — no execution fingerprint. The <N> coins he bought at <progress
   band> had a <X>% win rate; the edge is in selection.
✅ Neither can be established — no edge is visible.

❌ he is a bot / this is a script running / someone is watching the screen
❌ "nine straight hours without fills"   ← the interval is 07-15; the 9 is yours
❌ "not 7x24"                            ← 7 and 24 are not in the tables; write "it does not run continuously"
❌ weak evidence + "no person could do this"   ← a strong conclusion needs strong evidence
```

## 6.5 `edge.follower`: where a follower ends up

> **"If the caller buys the same coins this address buys, what position do they end up in, and which
> part of this is observable?"**

**Not "how to imitate him" — "what happens if you follow".** The first produces a checklist (how
fast to buy, how much, which tool), which is useless to a follower; only the second answers what
they actually have to decide.

**Step 0: state the coverage first.** The share in `based_on › bought_not_fully_sold` must be written
verbatim: his holding on those coins ran past the window, so we never see the exit. **The higher that
share, the more this profile represents only his short-term slice.**

**Step 1: the position is set by the largest hold-time bucket** (the same source as the type in
section 2):

| Largest bucket | Where a follower ends up |
|---|---|
| `≤15s` / `15-60s` / `1-5m` | **He is out within minutes.** Between seeing his fill and getting their own, he may already be gone — the follower is taking the other side of his sells |
| `5-60m` | **There is a minute-scale window.** Whether they keep up depends on their own execution, which we cannot see, so say plainly that it depends on them |
| `>1h` | **Time is not the problem.** The risk is not the lag but the coin selection — move to his win rate, PnL per coin and loss concentration |

**Those three are completely different.** Writing "the follower takes the other side" for a `>1h`
address is wrong; so is "time is not a problem" for a `≤15s` one. If the largest bucket is under
half, describe both styles separately.

**Step 2: carry `source` through.**
- `execution` / `both` → say it plainly: his edge is that one beat, and **a follower cannot get it**
  (check 13 enforces this)
- `selection` / `both` → spell out **what is observable**: which progress band's coins did better,
  which combo passed the test. Stop at "what to watch"
- `none` → say honestly that nothing observable stands out

**Only the exit side determines the position**:

| Which table | What it determines |
|---|---|
| `exit·time: seconds held` | how long he holds. The shorter, the more likely a follower enters after he has left |
| `exit·style: number of sells` + `exit·size: leftover as share of bought` | one fill to zero = the follower is his counterparty |
| `entry→exit venue` | exiting on the inner market means the sell lands straight on the curve, and a follower's sell queues behind his |

**Explaining the mechanism is enough — do not hand the caller a "follow" or "do not follow" verdict.**
State the mechanism properly and the conclusion is theirs to draw.

| ✅ Write it like this | ❌ Not like this |
|---|---|
| `<N>` coins (`<X>`%) are closed inside `<hold bucket>` and `<M>` are sold in one fill — between seeing a fill and getting their own, a follower may find him already gone | a follower has to get their first buy in within `<N>` seconds |
| `<X>`% enter and exit on the inner market, so the sell lands straight on the curve | the first buy size has to be `<size bucket>` |
| the `<N>` coins he bought at `<progress band>` had a `<X>`% win rate, and that part is visible | the tool has to handle that kind of throughput |
| | a follower has to be able to absorb a `<N>` dollar loss on one coin |

**Five families are banned outright inside `follower`** (check 10 catches them):
① buy sizes (size and PnL have no causal link here); ② which program / tool / router he uses (do not
put it in `facts` either); ③ his single-coin worst loss or "must be able to absorb N" (that is his
number, not the follower's); ④ "must buy within N seconds" (following is not a speed problem — the
problem is the exit); ⑤ activity hours ("watch during his active hours") — that belongs in `edge.say`
as a correction, not in the follower's position.

**The subject of `follower` is always the caller**, never the address. "A follower's sell queues
behind his" ✅; "he should avoid holding through that band" ❌ — the second is telling him how to
trade, which is outside what this product does.

# 7. Wording

**⓪ No straight double quotes `"` inside `say` / `follower`, and no `f1`-style ids in the prose.**
Use single quotes for emphasis; ids belong in `legs`, and the renderer adds the endnote markers.
Writing "(f10)" in the prose is both redundant and read as an unsourced number. A bare double quote
breaks the whole JSON — two reports failed to parse that way.

**① Every quantity is an Arabic numeral. No "most / the majority / almost all / a few / several".**
Those have no provenance and cannot be falsified — they walk straight around the checks. Give the
number or say nothing.

**② No "high / low / fast / slow / good / bad / stable / excellent / aggressive / conservative".**
**We have no benchmark of comparable traders** — whether a win rate around sixty percent is high or
low has no reference point. Report the raw number. The one exception is the classifying sentences
section 6 allows ("the edge is in execution / selection", "beyond a person"), and those need strong
evidence.

**③ For a total, cite `not_visible›total` — do not add the rows yourself.** That total is already
de-duplicated. **Every number you need has been computed for you; go and find it.**

**④ ‼️ Cite the level that covers every number in that sentence — do not cite too deep.**

| The numbers in this fact come from | Cite |
|---|---|
| one row | that row, `per_coin_buckets›entry·time: seconds after coin creation›<10s` |
| several rows of one table | that table, `per_coin_buckets›entry·time: seconds after coin creation` |
| several fields under one block | **that block**, `per_fill_distribution›buy_size` (not `…›distinct_values`) |
| both the winning and losing side | **`pnl_distribution`** (not `pnl_distribution›winners_side›cumulative`) |

**Numbers from two different top-level blocks** → **split them into two facts**. A `cite` is a single
path; `A›x / B›y` does not resolve.

**⑤ Never add neighbouring buckets together.** The table has "`<bucket A>` `<a>` coins" and
"`<bucket B>` `<b>` coins"; there is no "`<A>`-`<B>` `<a+b>` coins" bucket — that sum is yours. State
the two buckets separately, or state only the largest. The same goes for PnL and for shares. **Not a
single plus sign.**

**⑥ ‼️ "No adding up" applies to the three body sections too — that is where it happens most.**
The urge to generalise is the urge to total things up.
"`<x+y>`% of coins were bought between `<A>` and `<B>`" ← two buckets added; that number appears
nowhere. Say the two numbers separately.
"`<N>`+ dollars", "about `<N>`", "roughly half", "close to `<N>`%" ← invented approximations; cite
the field and write what it prints.

Three kinds of arithmetic that do not look like arithmetic: **computing a percentage** (the table
says 94, so write 94 — do not add a percentage it does not print); **counting things** ("there are N
combos with a 100% win rate" — that N is yours; list them instead); **converting units** (seconds to
minutes, USD to SOL). The test: **can you point at this number in a table?** If not, you made it.

**⑦ Every number in the body must have been written out in some fact's `say`, and that fact must be
in `legs`.** After writing a sentence, circle each number in it, find which fact **wrote** it (not
merely which table contains it), and add that fact's id. If no fact wrote it, put it into a fact
first. **Listing extra legs is not an error; listing too few is.**
The same holds for ranges: the table has `10~20%`, `20~30%` and `30~40%` — **there is no `10~40%`
bucket**. State the three separately, each with its own coin count and win rate.
**Never assemble a range of your own**, including "a win rate of 57.8%~65.0%" formed by joining a
column's lowest and highest values — that token is not in the table. Write "lowest 57.8%, highest
65.0%" as two numbers.

# 8. There is no "what this data cannot tell" section

Do not write one. "Whether his next trade wins", "where the price went after he sold" and "whether a
person or a program is behind it" are limits of **all** on-chain data — writing them says nothing.
The sample boundary (the unsold share, the invisible coins) is already in the first fact and in
`follower`'s step 0. Judgements the chain cannot support (bot, someone watching) are **not written**,
not relocated.

# 9. Length

| | Limit |
|---|---|
| `facts` | at most 18 in total. At most 6 from `per_coin_buckets`, at most 2 from each other block |
| `profile.say` / `result.say` / `edge.say` / `edge.follower` | one to three sentences each |

The only hard floor is coverage: one fact per analysis block, plus the sample-boundary fact.
**Thin data means a short report. Better one sentence fewer than one you are not sure of.**

---

# Before you submit — go through this list

1. **Does each of the eight blocks have a fact?** `based_on` · `not_visible` · `action_combos` ·
   **`buy_sell_ladder`** · `per_coin_buckets` · `per_fill_distribution` · **`coin_attributes`** ·
   `pnl_distribution` · `by_period` — the two in bold are the ones most often missed.
2. Are all three sections present? Is `edge.source` one of the four values? Is `follower` non-empty?
3. Does `profile`'s type match the largest hold-time bucket? If that bucket is under half, did you
   describe both styles?
4. Does `result` give the denominator, the net PnL, both sides' concentration and the trend? Did you
   present an outcome-driven variable like "buying 5+ times makes more" as skill?
5. Did you test each fingerprint in `edge.say` for strength on its own? Does any weak input carry a
   strong conclusion? Does `source` match the evidence?
6. Does `follower` state the unsold share? Does the position match the largest hold-time bucket? For
   `execution`/`both`, does it say a follower cannot get it? For `selection`/`both`, does it stop at
   what to watch? Is the subject always the caller? Are sizes / tools / speed requirements /
   single-coin losses / activity hours all absent?
7. For each fact, can every number be found in the passage it cites? If not, move the `cite` one level up.
8. Is every number in the body written out in one of its legs' facts? Anything added, divided,
   counted or converted is yours.
9. **Did you report curve progress?** If there are inner-market coins, `entry·position: curve
   progress` must be cited.
10. Any "most / the majority", "roughly N / about N", "high / low / stable", "bot / script" left?
    Replace or delete them.
