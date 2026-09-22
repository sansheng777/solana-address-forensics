#!/usr/bin/env python3
"""okx/agent/verify.py — the layer-4 verifier: mechanically check the report the model wrote.

    python3 okx/agent/verify.py <report.json> <layer3.json>
    exit 0 = everything passed, 1 = violations (each one printed)

★ This check is **not an LLM**, it is a script. Verifying an LLM with an LLM is self-deception —
  both can be wrong in the same place.

The thirteen checks are listed in okx/agent/SCHEMA.md. The whole idea is one sentence:
**the model may only transcribe, never compute** — so every number it writes must be findable
in the very row it cites.

⚠️ One known slack: numbers are matched by **absolute value** (a -382 in the cite serves both
   "382" and "-382"). Otherwise a normal phrasing like "lost 382" would be flagged. The price is
   that **a sign error goes undetected** (calling a loss a gain). Closing that hole means making
   the model carry the sign in `say`, which is a prompt matter, not this layer's.
"""
import hashlib, json, re, sys

# Analysis blocks that facts must cover. ★ `by_period` is on the list because it is the only
# source for **whether the money-making is trending** — without forcing a citation the model
# skips the whole block, and that is exactly what a caller wants to know.
ANALYTIC_BLOCKS = ["action_combos", "buy_sell_ladder", "per_coin_buckets",
                   "per_fill_distribution", "coin_attributes", "pnl_distribution", "by_period"]
# ★ Total number of checks. Anywhere else that needs it (app.py's response, the README) must
#   reference this constant and **never hard-code its own number** — found 2026-09-21: app.py
#   said "checks": 9 while there were already 12.
CHECK_COUNT = 13
SEP = "›"
# ★ A digit glued to a letter is not a number (f10 / r2 are identifiers).
#   Hit 2026-09-21: the model wrote fact ids into the body, "(f10)" yielded a 10 that counted as
#   an unsourced number, and a correct report took 8 false violations that two rewrites could not
#   fix.
#   ⚠️ The look-behind must exclude letters **and digits**: excluding only letters leaves a "0"
#      behind in f10 (measured 2026-09-21).
#   ⚠️ Thousands separators are only "1-3 digits + (comma + exactly 3 digits)+". The old [\d,]*
#      swallowed the "02,07" inside "01-02, 07-15" into 0207 (measured 2026-09-21: a correct
#      report stuck on 3 false violations that two rewrites could not clear).
NUM = re.compile(r"(?<![A-Za-z0-9])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
# ★ Range tokens (10~20% / 15-60s / 150-350). Caught 2026-09-21: "the 62 coins at 10~40% progress"
#   — three buckets merged into one range, and the count 62 came from a different table. Every
#   digit was individually "legal"; together they were fabricated. So a range must match as a
#   whole: an A~B in the body must appear verbatim in the cite / in a leg's fact.
#   Years (4 leading digits) are skipped so 2026-09-14 is not read as a range.
RANGE = re.compile(r"(?<![\d.\-~～])(\d+(?:\.\d+)?)\s*%?\s*[~～\-]\s*(\d+(?:\.\d+)?)(?![\d.\-~～])")


def _strkeys(o):
    """Normalise every dict key to a string.

    ⚠️ This blew up on a real paid call, 2026-09-21: `coin_attributes›by_launchpad` contained a
    None key (that coin's launchpad could not be identified), and the `sort_keys=True` below tried
    to order it → `'<' not supported between 'str' and 'NoneType'`. **The whole pipeline died
    before the model even ran; the buyer paid and got nothing.**
    The root cause is fixed in features.py (None → "unknown"); this is the backstop: no nullable
    field used as a key should ever be able to take the service down."""
    if isinstance(o, dict):
        return {(k if isinstance(k, str) else json.dumps(k, ensure_ascii=False)): _strkeys(v)
                for k, v in o.items()}
    if isinstance(o, list):
        return [_strkeys(x) for x in o]
    return o


def digest(layer3):
    """Fingerprint of the layer-3 JSON. The report must copy it back verbatim, proving it read
    this version of the data."""
    return hashlib.sha256(
        json.dumps(_strkeys(layer3), ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]


def _text(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def resolve(layer3, path):
    """Walk into the layer-3 JSON along A›B›C. Inside a list, match on the **row's leading label**
    exactly, falling back to a substring match. Returns (ok?, the text it resolved to)."""
    cur, segs = layer3, [s.strip() for s in str(path).split(SEP) if s.strip()]
    if not segs: return False, ""
    for i, seg in enumerate(segs):
        if isinstance(cur, dict):
            if seg not in cur: return False, ""
            cur = cur[seg]
        elif isinstance(cur, list):
            hit = [x for x in cur if isinstance(x, str) and x.split("|")[0].strip() == seg]
            if not hit:
                hit = [x for x in cur if seg in _text(x)]
            if not hit: return False, ""
            cur = hit[0]
        else:
            return False, ""
    return True, _text(cur)


def nums(text):
    """Pull every number out of the text, normalised by absolute value (see the slack noted at the
    top of this file)."""
    o = set()
    for m in NUM.findall(_text(text)):
        s = m.replace(",", "").lstrip("-")
        o.add(s)
        if s.endswith(".0"): o.add(s[:-2])
        o.add(s.rstrip("0").rstrip(".") if "." in s else s)
    return {x for x in o if x}


def ranges(text):
    """Pull range tokens out of the text, normalised to 'A~B' (whitespace and % stripped, the
    hyphen form unified to ~)."""
    o = set()
    for m in RANGE.finditer(_text(text)):
        a, b = m.group(1), m.group(2)
        if len(a.split(".")[0]) == 4: continue          # a year
        o.add("%s~%s" % (a, b))
    return o


def _dedupe(missing):
    """45 and 45.0 are the same number, report it once — normalisation put both forms in the set."""
    seen, out = set(), []
    for n in sorted(missing):
        try: k = float(n)
        except ValueError: k = n
        if k in seen: continue
        seen.add(k); out.append(n)
    return out


SECTIONS = ("profile", "result", "edge")          # the three body sections, in a fixed order
EDGE_SOURCES = ("execution", "selection", "both", "none")


def _body_texts(report):
    """Every sentence in the three body sections: (section, text). `edge` has two."""
    out = []
    for k in SECTIONS:
        sec = report.get(k)
        if isinstance(sec, dict):
            out.append((k, _text(sec.get("say", ""))))
            if k == "edge":
                out.append(("edge.follower", _text(sec.get("follower", ""))))
    return out


def check(report, layer3):
    v = []
    g = lambda k, d=None: report.get(k, d)

    # ── 1 fingerprint ──
    want = digest(layer3)
    if g("layer3_digest") != want:
        v.append("(1) digest mismatch: the report says %r, the actual layer 3 is %r — this report "
                 "was not written from this version of the data" % (g("layer3_digest"), want))

    facts = g("facts") or []
    if not facts: v.append("(1) facts is empty")
    for k in ("address", "window"):
        if k not in report: v.append("(1) missing top-level key %s" % k)

    # Global constants: the report's denominators (coin count, fill count...) and the window dates,
    # which any line may cite.
    # ★ `window` must be included: the first sentence often says "window 2026-09-14 ~ 2026-09-18",
    #   and window is a separate top-level key, not part of `based_on`. Without this a single date
    #   would split into four "unsourced numbers" (2026/09/14/18).
    base = (nums(_text(layer3.get("based_on") or {}))
            | nums(_text(layer3.get("window") or [])))

    by_id, fact_nums = {}, {}
    for f in facts:
        fid = f.get("id") or "?"
        by_id[fid] = f
        ok, txt = resolve(layer3, f.get("cite"))
        # ── 2 does the citation resolve ──
        if not ok:
            v.append("(2) facts[%s] cite does not resolve: %r" % (fid, f.get("cite")))
            continue
        # ★ Numbers inside a `value` field are not accepted. `value` was dropped from the contract
        #   on 2026-09-20; even if a model still emits it we ignore it — letting the model write a
        #   field that "proves" its own numbers legal is **a back door into check (3)**. Legality
        #   comes only from the layer-3 text and the global constants.
        allowed = nums(txt) | base
        # ★ What the body may use is only what a fact spelled out in its `say` (not the whole block
        #   the fact cites). 2026-09-21 v2b: one section listed 8 legs, the pool was the union of
        #   8 blocks, and almost any small number passed — "the 62 coins at 10~40% progress" got in
        #   because 62 was a winner count in `by_period`. Tightened, this means "a number used in
        #   the body must first be written out in some fact".
        fact_nums[fid] = nums(f.get("say"))
        # ── 3 numbers in `say` must exist in the row it cites ──
        for n in _dedupe(nums(f.get("say")) - allowed):
            v.append("(3) facts[%s] number %s is not in the cited %s — it was computed or comes "
                     "from another table" % (fid, n, f.get("cite")))
        for r in sorted(ranges(f.get("say")) - ranges(txt) - ranges(_text(layer3.get("window") or []))):
            v.append("(3) facts[%s] range '%s' does not exist in the cited %s — the table has "
                     "separate buckets, they may not be merged into one range" % (fid, r, f.get("cite")))

    # ── (14) skeleton: all three sections present, edge.source legal, follower non-empty ──
    #   The core of the 2026-09-21 redesign: a caller decides from the third section alone, so a
    #   missing section leaves them unable to assemble an answer.
    for k in SECTIONS:
        sec = g(k)
        if not isinstance(sec, dict) or not _text(sec.get("say", "")).strip():
            v.append("(14) section '%s' is missing (or its say is empty) — none of the three may "
                     "be dropped" % k)
    edge = g("edge") if isinstance(g("edge"), dict) else {}
    if edge and edge.get("source") not in EDGE_SOURCES:
        v.append("(14) edge.source is %r, only %s are allowed"
                 % (edge.get("source"), " / ".join(EDGE_SOURCES)))
    if edge and not _text(edge.get("follower", "")).strip():
        v.append("(14) edge.follower is empty — 'where a follower would end up' is the whole point "
                 "of the report and cannot be blank")

    # ── 4 / 5 the three sections: legs must exist, and no new numbers ──
    for k in SECTIONS:
        sec = g(k)
        if not isinstance(sec, dict): continue
        legs = sec.get("legs") or []
        if not legs:
            v.append("(4) %s has no supporting legs" % k); continue
        pool, rpool = set(base), ranges(_text(layer3.get("window") or []))
        for lid in legs:
            if lid in by_id:
                pool |= nums(by_id[lid].get("say")); rpool |= ranges(by_id[lid].get("say"))
            else:
                v.append("(4) %s leg %r does not exist" % (k, lid))
        texts = [("say", _text(sec.get("say", "")))]
        if k == "edge": texts.append(("follower", _text(sec.get("follower", ""))))
        for name, t in texts:
            for n in _dedupe(nums(t) - pool):
                v.append("(5) %s.%s uses the number %s, which no fact in its legs wrote out — put "
                         "it into a fact's say first" % (k, name, n))
            for r in sorted(ranges(t) - rpool):
                v.append("(5) %s.%s uses the range '%s', which no leg's fact wrote verbatim — the "
                         "table has separate buckets, they may not be merged" % (k, name, r))

    # ── 6 coverage: at least one fact per analysis block ──
    cited = {str(f.get("cite", "")).split(SEP)[0].strip() for f in facts}
    for b in ANALYTIC_BLOCKS:
        if b in layer3 and b not in cited:
            v.append("(6) analysis block '%s' has no fact at all — a dimension dropped silently" % b)

    # ── 7 banned words ──
    body = " ".join([_text(f.get("say")) for f in facts if isinstance(f, dict)]
                    + [t for _, t in _body_texts(report)])
    v += _scan(body, "forbidden.txt", "(7)", "a banned word",
               "what the chain cannot distinguish is not written at all — delete the sentence")
    v += _scan(body, "vague.txt", "(8)", "a vague quantifier",
               "either use the number exactly as layer 3 prints it, or delete the sentence")

    # ── (9) the sample boundary must have a fact: without it a caller reads the report as the
    #      whole picture ──
    if not any(str(f.get("cite", "")).split(SEP)[0].strip() in ("based_on", "not_visible")
               for f in facts):
        v.append("(9) no fact cites `based_on` or `not_visible` — the sample boundary is not stated")

    # ── (10) follower-only bans: scans edge.follower, not facts / profile ──
    #   "first buy sizes cluster at 150-350" is a normal fact; put the same words in `follower` and
    #   it becomes a copy-this checklist. **The same word means different things in the two places,
    #   so they must be scanned separately.**
    fol = _text(edge.get("follower", "")) if edge else ""
    v += _scan(fol, "follower-banned.txt", "(10)", "copy-this-checklist content",
               "follower answers **where a follower would end up**, and only the exit side "
               "determines that")

    # ((12) was removed 2026-09-21 when the whole not_determined section was dropped — the two
    #  informative lines already live in facts / follower and the rest were template sentences like
    #  "whether his next trade wins". The number is left unused rather than renumbering, so the
    #  (13)/(14) referred to in the history still mean the same checks.)

    # ── (13) cross-sentence consistency: if source says execution, follower must carry "cannot get it" ──
    #   Measured 2026-09-21: the model correctly wrote "38.3% bought within 10s ... the edge is in
    #   execution", yet the caller-facing sentence never mentioned it — and that sentence is the
    #   direct answer to "is this worth following", so dropping it wastes the judgement.
    _cannot_get = re.compile(
        r"\b(cannot|can ?not|can't|will not|won't|unable to|no way to)\b[^.;]{0,48}"
        r"\b(get|match|reach|obtain|replicate|copy|capture|access)\b"
        r"|\b(out of reach|a (step|beat) behind|always behind|too slow to)\b", re.I)
    if edge and edge.get("source") in ("execution", "both") and not _cannot_get.search(fol):
        v.append("(13) edge.source claims an execution edge, but follower never says a follower "
                 "**cannot get it / is a step behind** — what an execution edge means for the "
                 "follower is the answer they came for, so it must appear in follower")

    # ── (11) if there are inner-market coins, curve progress must be reported ──
    #   "Early or not" has exactly one yardstick: curve progress. Using time (seconds after
    #   creation) gives the opposite answer: measured, 38.3% of coins were bought within 10s
    #   (looks like sniping) while only 0.9% were bought below 10% progress.
    for tbl in ("entry·position: curve progress", "exit·position: curve progress"):
        rows = ((layer3.get("per_coin_buckets") or {}).get(tbl)) or []
        real = [r for r in rows if isinstance(r, str) and not r.startswith("(")]
        if real and not any(tbl in str(f.get("cite", "")) for f in facts):
            v.append("(11) '%s' has %d buckets and not one fact — how early an entry or exit was "
                     "can only be judged on progress; time gives the opposite answer"
                     % (tbl, len(real)))
    return v


def _scan(body, listname, tag, what, howto):
    """Scan the body against a word list and return violations. Shared by (7) and (8).

    Three rules, all learned the hard way:
    1. **`re:` prefixed regexes are supported** — needed for phrases where a bare substring would
       hit an unintended word.
    2. **Plain words are matched before regexes** — otherwise a longer regex eats the match first
       and the reported fragment is unreadable.
    3. **Every occurrence of the same rule is reported separately** — reporting only the first one
       means the model fixes half of them and the rewrite round is wasted.
    """
    plain = [w for w in _wordlist(listname) if not w.startswith("re:")]
    regex = [w for w in _wordlist(listname) if w.startswith("re:")]
    out, rest = [], body
    for w in sorted(plain, key=len, reverse=True) + regex:
        pat = (re.compile(w[3:], re.I) if w.startswith("re:")
               else re.compile(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z])", re.I))
        seen = set()
        for mt in pat.finditer(rest):
            g = mt.group(0)
            i, j = mt.span()
            ctx = rest[max(0, i - 24):j + 24]
            # ★ De-duplicate on the **context**, not on the matched word. In Chinese the two hits of
            #   one rule were different strings (two distinct prescriptive phrases), so de-duplicating on
    #   the match itself was
            #   enough; in English both are literally "should" and the second one would be dropped —
            #   the model then fixes one of them and the rewrite round is wasted (2026-09-22).
            if ctx in seen: continue
            seen.add(ctx)
            out.append("%s %s: '%s' — %s (context: ...%s...)"
                       % (tag, what, g, howto, ctx))
        if seen:
            rest = pat.sub(lambda x: " " * len(x.group(0)), rest)
    return out


def _wordlist(name):
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    try:
        out = []
        for l in open(p, encoding="utf-8"):
            l = l.split("#")[0].strip() if not l.lstrip().startswith("#") else ""
            if l: out.append(l)
        return out
    except OSError:
        return []


def main():
    a = sys.argv[1:]
    if len(a) != 2: sys.exit(__doc__)
    rep, l3 = json.load(open(a[0], encoding="utf-8")), json.load(open(a[1], encoding="utf-8"))
    v = check(rep, l3)
    if not v:
        print("OK  report passed every check (%d facts / three body sections)"
              % len(rep.get("facts") or []))
        return 0
    print("FAIL  %d violation(s):" % len(v))
    for x in v: print("   " + x)
    return 1


if __name__ == "__main__":
    sys.exit(main())
