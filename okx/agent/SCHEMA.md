# Report output contract (mechanically enforced by `verify.py` — change this and you must change verify.py)

The model must output **one JSON object only**: no text before or after it, no ```json fence.

**The body is three sections in a fixed order, each answering one question.** `facts` is the
evidence pool, rendered as an appendix; every number in the body must be findable in the facts it
lists under `legs`.

```json
{
  "address": "<copy the input's address verbatim>",
  "window":  ["<copy window[0]>", "<copy window[1]>"],
  "layer3_digest": "<copy the digest given in the input, character for character>",

  "facts": [
    {"id": "f1",
     "say": "one sentence of fact — transcription only, no causality, no adjectives",
     "cite": "block›subkey›row label"}
  ],

  "profile":  {"say": "what he does: one to three sentences", "legs": ["f1", "f3"]},
  "result":   {"say": "does he make money: one to three sentences", "legs": ["f8", "f9"]},
  "edge":     {"source": "execution | selection | both | none",
               "say": "where his edge is and what the evidence is: one to three sentences",
               "follower": "if a caller buys the same coins, where they end up, and which part is "
                           "observable: one to three sentences",
               "legs": ["f2", "f6"]}
}
```

Each section answers one question and **they may not bleed into each other**:

| Section | Question | What the answer looks like |
|---|---|---|
| `profile` | What does he do | in and out within minutes / within the hour / long-term / mixed; where the hold time clusters, how many sells, where on the curve he enters and exits |
| `result` | Does he make money | win rate, net PnL, whether one or two coins carry it, whether it trends over the window |
| `edge` | Where his edge is, and can a follower get it | `source` classifies it; `say` gives the evidence; `follower` states the position a follower ends up in and what is observable |

## What verify.py checks

| # | Check | What it prevents |
|---|---|---|
| 1 | `layer3_digest` equals the hash of the layer-3 JSON actually passed in | a report that does not belong to this data |
| 2 | every `facts[].cite` resolves inside the layer-3 JSON | invented sources |
| 3 | **every number in `facts[].say` appears in the row it cites**; ranges (`10~20%`) must exist verbatim too | arithmetic / crossing tables / merged buckets |
| 4 | each section's `legs` is non-empty and every id exists in `facts` | claims with no support |
| 5 | **every number and range in the body (`say` / `follower`) was written out by one of its `legs` facts** (not merely present in the block that fact cites) | 2026-09-21: "the 62 coins at 10~40% progress" — 62 came from another table and 10~40 merged three buckets; every digit was individually legal, the sentence was not |
| 6 | `facts` covers every analysis block, at least one each | a dimension dropped silently |
| 7 | no banned word anywhere (`forbidden.txt`) | judgements that cross a semantic layer |
| 8 | no vague quantifier anywhere (`vague.txt`) | **rewriting "207 coins" as "most coins" leaves nothing to check — the front door around check 3** |
| 9 | at least one fact cites `based_on` or `not_visible` | a caller reading the report as the whole picture |
| 10 | **inside `edge.follower`**, no copy-this-checklist content (`follower-banned.txt`) | turning the section into "how to imitate him" |
| 11 | when inner-market coins exist, a fact must cite `per_coin_buckets›entry/exit·position: curve progress` | reporting only "bought within N seconds", **when time and progress give opposite answers** |
| 13 | when `edge.source` is `execution` or `both`, `follower` must say a follower **cannot get it / is a step behind** | making the judgement and then not passing it to the caller |
| 14 | all three sections present, `edge.source` one of the four legal values, `edge.follower` non-empty | a missing section leaves the caller unable to assemble an answer |

> Check 12 was removed on 2026-09-21 along with the `not_determined` section. The number is left
> unused rather than renumbering, so references to checks 13 and 14 in the history still point at
> the same checks.

> **⚠️ Do not output a `value` field.** Removed 2026-09-20 for three reasons stacked together:
> ① it asks the model to re-serialise cited JSON into a JSON string, so **escaping eventually
>    breaks** (one whole report failed to parse);
> ② once endnote sources were resolved from the layer-3 data, nothing read it;
> ③ verify used to count numbers inside `value` as legal — which lets the model write a field that
>    proves its own numbers, **a back door into check 3**. Legality now comes only from the layer-3
>    text and the global constants.

> **2026-09-21 redesign**: the body used to be `facts / readings / advice`, free-form. The model
> picked different tables each run — mentioning a selection edge once and not the next time, and
> presenting "coins bought 5+ times have a higher win rate" as a finding. With three fixed sections
> there is nowhere to put filler: it can only answer the three questions.

## How to write a `cite`

Separate levels with `›` (U+203A), starting from a top-level key of the layer-3 JSON. When the path
reaches a list, the last segment is the **row's leading label** (everything before `|`); the
resolver matches that exactly, then falls back to a substring match.

```
pnl_distribution›net
per_coin_buckets›entry·time: seconds after coin creation›<10s
action_combos›rows›inner→inner · buy 2-4 · sell 1 · hold <1m
```

## Where the wording line sits (this is the product's position, not a technical limit)

| Layer | Allowed | Example |
|---|---|---|
| `facts` | transcription only | "224 coins were bought within 10s of creation" |
| `profile` / `result` | generalisation, but every number must come from a leg | "hold time clusters between 15 seconds and 5 minutes; 77.1% are sold in one fill" |
| `edge.say` | **one step on the same semantic layer** | "25.6% of buys are the same size and 38.3% of coins are entered within 10s of creation — the edge is in execution" |
| `edge.follower` | subject is the caller; describes a position, not a procedure | "a follower is a step behind and takes the other side of his sells; what is observable is the coins he bought at 30~40% progress" |

> **Crossing a semantic layer is the red line.** "every fill went through one program" → "he uses
> one platform" stays on the layer; → "he is a bot" crosses it, and the chain cannot tell. Sentences
> like that are not written at all.
>
> **There is no `not_determined` section** (removed 2026-09-21): "whether his next trade wins" and
> "where the price went after he sold" are limits of *all* such data — writing them says nothing.
> The sample boundary (coins not fully sold, coins not visible) already lives in `facts` and
> `follower`.
