---

# ‼️ This is a TARGETED REWRITE, not a new report

**You are not writing the report again.** Below is **the version you just wrote**, followed by the
violations the verifier found. Further down is the layer-3 JSON (the same one you read before, for
looking numbers up).

## What to do

**Change only the places named below. Return every other character exactly as it was.**

- Any fact or body section not named: **do not touch a single character** — wording, order, ids
- Do not add entries, do not remove entries that were not named, do not reorganise anything
- Return the **complete report JSON** (not a diff, not a fragment), in exactly the same shape

## How to fix each kind

| Violation | Fix |
|---|---|
| (3) a number or range is not in the passage it cites | Either **move the `cite` one level up** (to the level that covers this number) or **delete the number**. A range must be a bucket printed in the table (`10~20%`); three buckets may not be merged into `10~40%` |
| (5) a section used a number or range no fact wrote | The body may use only numbers a fact **spelled out** in its `say`. Either **write that number into a fact's say** (and add that fact to `legs`), or delete it. A range like `10~40%` that the table does not have: **split it back into the table's buckets** |
| (4) a leg does not exist / is empty | `legs` may only contain ids present in `facts`, and needs at least one |
| (7) a banned word | Delete the word. **What the chain cannot distinguish is not written at all** — do not rephrase it and keep it |
| (8) a vague quantifier or approximation | **Use the number exactly as layer 3 prints it**; if there is no such number, delete the sentence. "roughly", "about", "close to" always go |
| (7) prescriptive voice (should / ought to / must improve) | **That is telling this address how to trade, which is out of scope.** Either rewrite it as what a follower would face, or delete it |
| (7) an evaluative word (excellent / solid / decisive) | Delete the word and keep the number |
| (6) a block is not covered | Add one fact citing that block |
| (9) the sample boundary is missing | Add a fact citing `based_on` or `not_visible` |
| (10) copy-this-checklist content in `follower` | **Rewrite or delete the sentence.** `follower` answers where a follower ends up, and only the exit side decides that (how long he holds / how many sells / how much is left / where he exits). Buy sizes, tooling, his worst single-coin loss, "how fast you must buy" and activity hours are all out |
| (11) curve progress not reported | Add a fact citing `per_coin_buckets›entry·position: curve progress` (and/or the exit one). **How early an entry was can only be judged on progress; seconds after creation gives the opposite answer** |
| (13) source claims execution but follower does not carry it | Add to `follower`: his edge is in execution (speed / frequency / fixed sizes), a follower is always a beat behind, and **that part is out of reach** |
| (14) a section is missing / source illegal / follower empty | Add the missing section; `source` must be one of `execution` / `selection` / `both` / `none`; `follower` needs at least one sentence |
| (2) a cite does not resolve | A `cite` is **one** path (never `A + B` joined), and every segment must match the layer-3 key character for character |

## ⚠️ Do not fix anything else along the way

After you return it the verifier re-checks **the whole report**, not just the parts you touched.
**Fixing one thing and breaking another** is the most common failure here — keep your hands on the
named sentences.

<<<VIOLATIONS>>>

<<<PREVIOUS REPORT>>>

---

**Once more: change only the places named above, and return everything else verbatim.**
