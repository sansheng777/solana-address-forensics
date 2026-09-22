#!/usr/bin/env python3
"""okx/agent/render.py — render the report JSON into Markdown for a human (sources as endnotes).

    python3 okx/agent/render.py <report.json> [layer3.json] [--out f.md]

★ Both outputs come from **one source**: the JSON for the calling agent and the Markdown for a
  human are rendered from the same report object. They are not generated twice, so they can never
  drift — the human copy can never say one extra thing.

2026-09-21: the body is three sections (what he does / does he make money / where his edge is and
whether a follower can get it); `facts` moved into an appendix of sources.
"""
import json, sys

SEP = "›"
SOURCE_LABEL = {"execution": "execution (speed)", "selection": "selection (which coins)",
                "both": "execution + selection", "none": "no edge identified"}


def _relevant(txt, want, cap=260):
    """When a fact cites a whole block, keep only the rows **related to the numbers in that
    sentence**.

    2026-09-20: once endnotes were resolved from the source data, a block-level cite dumped the
    entire block (measured: one `pnl_distribution` endnote ran past 900 characters, and two of them
    were identical) — unscannable. The machine check already happened in verify; **an endnote's job
    is to let a human spot-check**, so only the relevant rows are kept. If none can be picked out,
    truncate — better short than falling back on a `value` the model wrote itself."""
    import verify
    try:
        v = json.loads(txt)
    except (json.JSONDecodeError, TypeError):
        v = txt
    if isinstance(v, dict):
        v = [x for x in v.values() if not isinstance(x, str) or "|" in x or len(x) < 40] or list(v.values())
    if isinstance(v, list):
        hit = [x for x in v if verify.nums(x) & want]
        if hit:
            out = "; ".join(_flat(x) for x in hit)
            return out if len(out) <= cap * 2 else out[:cap * 2] + " …"
    t = txt if isinstance(txt, str) else json.dumps(txt, ensure_ascii=False)
    return t if len(t) <= cap else t[:cap] + " …"


def _flat(x):
    if isinstance(x, str): return x
    if isinstance(x, list): return "; ".join(_flat(i) for i in x)
    if isinstance(x, dict): return "; ".join("%s %s" % (k, _flat(y)) for k, y in x.items())
    return str(x)


def render(rep, layer3=None):
    """★ Endnote sources are **resolved from the layer-3 data**, never from a `value` the model
    wrote.

    Measured 2026-09-20: the model rearranged the rows it cited and wrote them into `value`, so the
    endnote became "it says the data looks like this" instead of "the data is this".
    **Evidence cannot be supplied by the party being checked.** If layer3 is passed, every cite is
    resolved against it; only without it do we fall back to `value` (and then the endnote is a
    pointer, not evidence)."""
    L, notes, num = [], [], {}
    addr = rep.get("address", "")
    w = rep.get("window") or ["", ""]
    L += ["# Address behaviour report", "",
          "`%s`" % addr,
          "",
          "| | |", "|---|---|",
          "| Window | %s ~ %s |" % (w[0], w[-1]),
          "| Data fingerprint | `%s` |" % rep.get("layer3_digest", ""),
          ""]

    # Number every fact first (appendix order = facts order); the body cites them via legs
    facts = rep.get("facts") or []
    for f in facts:
        num[f.get("id")] = len(notes) + 1
        src = f.get("value", "")
        if layer3 is not None:
            import verify
            ok, txt = verify.resolve(layer3, f.get("cite"))
            src = _relevant(txt, verify.nums(f.get("say"))) if ok \
                else "WARNING: cite does not resolve: %s" % f.get("cite")
        notes.append("%s — `%s`" % (str(f.get("cite", "")).replace(SEP, " › "), src))

    def _legs(ids):
        """Legs point at facts only. Test the value, not the key — this crashed once on
        2026-09-20 (the key was present, the value was None)."""
        return "".join("[%d]" % num[i] for i in ids or [] if num.get(i) is not None)

    def _sec(title, sec, extra=None):
        if not isinstance(sec, dict): return
        L.append("## " + title); L.append("")
        if extra: L.append(extra); L.append("")
        tail = _legs(sec.get("legs"))
        L.append("%s %s" % (sec.get("say", ""), ("(sources %s)" % tail) if tail else ""))
        L.append("")

    _sec("What he does", rep.get("profile"))
    _sec("Does he make money", rep.get("result"))
    edge = rep.get("edge")
    if isinstance(edge, dict):
        src = SOURCE_LABEL.get(edge.get("source"), str(edge.get("source")))
        _sec("Where his edge is, and whether a follower can get it", edge,
             extra="**Edge comes from: %s**" % src)
        tail = _legs(edge.get("legs"))
        if edge.get("follower"):
            L.append("**Where a follower would end up:** %s %s"
                     % (edge["follower"], ("(sources %s)" % tail) if tail else ""))
            L.append("")

    if facts:
        L += ["---", "", "### Appendix — facts and sources", ""]
        for f in facts:
            i = num[f.get("id")]
            L.append("[%d] %s" % (i, f.get("say", "")))
            L.append("    ↳ %s" % notes[i - 1])
    return "\n".join(L) + "\n"


def main():
    a = sys.argv[1:]
    if not a: sys.exit(__doc__)
    l3 = json.load(open(a[1], encoding="utf-8")) if len(a) > 1 and not a[1].startswith("--") else None
    md = render(json.load(open(a[0], encoding="utf-8")), l3)
    if "--out" in a:
        p = a[a.index("--out") + 1]
        open(p, "w", encoding="utf-8").write(md); print("wrote %s" % p)
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
