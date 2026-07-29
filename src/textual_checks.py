"""
Textual bug checks  (taxonomy top-level: TEXTUAL).

Two tiers:
  DETERMINISTIC (no LLM, runs without an API key) — high precision on concrete defects:
    T2  struck_text_present   -> deleted/struck questionnaire text still shown  (Q_Aware)
    T5  unresolved_placeholder-> a pipe token / unchosen alternate shows literally live
    T1  wording_diff (gated)  -> normalized spec-vs-live diff, ONLY on static (non-dynamic)
                                 text; dynamic text (pipes/alternates/inserts) is deferred.

  SEMANTIC (optional LLM tier) — for spec text containing pipes / <INSERT> / [A / B]
    alternates, judge semantic equivalence modulo legitimate substitution. Enabled only when
    ANTHROPIC_API_KEY is set; its findings default to Med (human review). See llm_wording().

Every issue carries decomposed confidence.
"""
import difflib
import os
import re

from checks import _norm, load


def _sim(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()

# spec text is "dynamic" (legitimately differs live) if it has pipe alternates, inserts, pipes
DYNAMIC = re.compile(r"\[[^\]]*/[^\]]*\]|<[^>]*>|\binsert\b|\bpipe\b", re.I)
# an unresolved token that should never be visible to a respondent
UNRESOLVED = re.compile(r"<\s*insert[^>]*>|\[\s*insert[^\]]*\]|\bpipe\s+[A-Z_]+", re.I)


def _mk(qid, subtype, explanation, expected, observed, clause, shot, conf, factors):
    return {"qid": qid, "check": subtype, "issue_type": "Textual",
            "explanation": explanation, "expected": expected, "observed": observed,
            "spec_clause": clause, "screenshot": shot, "confidence": conf,
            "confidence_factors": factors}


def t2_struck_text_present(spec, obs):
    """A struck-through (deleted) fragment in the spec must NOT appear in the live text."""
    issues = []
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not q.get("text_struck"):
            continue
        live = _norm(o.get("text"))
        for frag in q["text_struck"]:
            f = _norm(frag).strip(" ,")
            if f and f in live:
                issues.append(_mk(
                    qid, "struck_text_present",
                    f"Text {frag!r} is struck-through (deleted) in the questionnaire but "
                    f"still appears in the live survey.",
                    f"'{frag.strip()}' removed from the question",
                    o.get("text"),
                    f"{qid}: struck run {frag!r}",
                    o.get("screenshot"), "High",
                    {"spec_parse": "high (explicit strike-through run)",
                     "observation": "high (exact substring present)",
                     "determinism": "high (substring match)"}))
    return issues


def t5_unresolved_placeholder(spec, obs):
    """A pipe/insert token that leaked through to the respondent-facing text."""
    issues = []
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not o.get("text"):
            continue
        m = UNRESOLVED.search(o["text"])
        if m:
            issues.append(_mk(
                qid, "unresolved_placeholder",
                f"Live text contains an unresolved piping token {m.group(0)!r} — it should "
                f"have been replaced with a real value.",
                "token replaced with piped value",
                o.get("text"),
                f"{qid}: piping/insert",
                o.get("screenshot"), "High",
                {"spec_parse": "n/a (observed on live text)",
                 "observation": "high (token literally visible)",
                 "determinism": "high (pattern match)"}))
    return issues


def t1_wording_diff(spec, obs):
    """Normalized spec-vs-live wording diff, ONLY for static text. Dynamic text (pipes /
    alternates / inserts) is deferred to the semantic tier to avoid false positives."""
    issues = []
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not o.get("text") or not q.get("text"):
            continue
        if DYNAMIC.search(q["text"]):
            continue  # -> semantic tier
        s, l = _norm(q["text"]), _norm(o["text"])
        # normalize smart quotes / dashes for a fair comparison
        trans = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                               "–": "-", "—": "-"})
        s, l = s.translate(trans), l.translate(trans)
        if s and l and s != l:
            issues.append(_mk(
                qid, "wording_diff",
                "Live question wording does not match the questionnaire (static text).",
                q.get("text"), o.get("text"),
                f"{qid}: question text",
                o.get("screenshot"), "Med",
                {"spec_parse": "med (text boundary heuristics)",
                 "observation": "high (live text captured)",
                 "determinism": "med (normalized string compare)"}))
    return issues


def _extraction_contaminated(spec_text, live_text):
    """Skip pages where the runner grabbed a product-description block instead of the
    question text (known loop-page extraction issue) — else the LLM just judges garbage."""
    if not live_text:
        return True
    if re.search(r"thinking about the product shown below|trusted system of record",
                 live_text, re.I):
        return True
    # live text far longer than intended => description contamination
    return len(live_text) > max(240, 3 * len(spec_text or ""))


def _tokens(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def t1_question_wording(spec, obs):
    """Deterministic WORD-LEVEL diff of the question stem: reports words removed (in spec,
    not live) and added (in live, not spec) — covers wording changes and added/missing text.
    Gated to avoid false positives: skips dynamic text (pipes/alternates -> LLM tier),
    contaminated extractions, and parser-mis-assigned stems (where an option was captured as
    the text). Struck words are left to T2 to avoid double-reporting."""
    issues = []
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not o.get("text") or not q.get("text"):
            continue
        if DYNAMIC.search(q["text"]):
            continue
        if _extraction_contaminated(q["text"], o["text"]):
            continue
        opt_norms = {_norm(op["label"]) for op in q.get("options", [])}
        if _norm(q["text"]) in opt_norms:        # stem is actually a leaked option
            continue
        # skip parser-mis-assigned stems: too short to be a real question, or a scale point
        if len(_tokens(q["text"])) < 5 or re.match(r"^\s*\d+\s*[-–]", q["text"]):
            continue
        # strip a known live UI preamble that isn't part of the question wording
        live_text = re.sub(r"if you would like to see the product description again[^\n]*",
                           "", o["text"], flags=re.I).strip()
        s_toks, l_toks = _tokens(q["text"]), _tokens(live_text)
        struck = set()
        for fr in q.get("text_struck", []):
            struck |= set(_tokens(fr))
        removed, added = [], []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, s_toks, l_toks).get_opcodes():
            if tag in ("replace", "delete"):
                removed += s_toks[i1:i2]
            if tag in ("replace", "insert"):
                added += l_toks[j1:j2]
        added = [w for w in added if w not in struck]   # T2 owns struck-text-present
        if not added and not removed:
            continue
        issues.append(_mk(
            qid, "question_wording",
            f"Question wording differs from the questionnaire — "
            f"removed (spec→not live): {removed or '—'}; added (live, not in spec): {added or '—'}.",
            q.get("text"), o.get("text"), f"{qid}: question text",
            o.get("screenshot"), "High",
            {"spec_parse": "high (static question text)",
             "observation": "high (live text captured)",
             "determinism": "high (word-level diff)"}))
    return issues


def t4_option_wording(spec, obs):
    """Deterministic per-option wording check: the spec is ground truth, so an answer option
    whose live label differs from the spec label is a real bug (e.g. spec 'Market' vs live
    'Marketing'). Conservatively gated — only runs when option lists align cleanly (equal
    count, all but <=2 options match exactly) so it can't fire on parser-over-captured lists.
    A differing pair that is still clearly the SAME option (similar text) is auto-flagged."""
    issues = []
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not o.get("options") or not q.get("options"):
            continue
        spec_labels = [op["label"] for op in q["options"]]
        live_labels = list(o["options"])
        if len(spec_labels) != len(live_labels):
            continue
        # compare on ALPHANUMERIC content only, so punctuation/spacing artifacts
        # ('etc' vs 'etc.', '$10,,000' vs '$10,000') don't count as differences.
        alnum = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())
        sset = {alnum(x) for x in spec_labels}
        lset = {alnum(x) for x in live_labels}
        exact = sset & lset
        if len(exact) < len(spec_labels) - 2:   # require a clean alignment
            continue
        leftover_spec = [x for x in spec_labels if alnum(x) not in lset]
        leftover_live = [x for x in live_labels if alnum(x) not in sset]
        if not leftover_spec or len(leftover_spec) != len(leftover_live):
            continue
        used = set()
        for sp in leftover_spec:
            best, bestr = None, 0.0
            for i, lv in enumerate(leftover_live):
                if i in used:
                    continue
                r = _sim(alnum(sp), alnum(lv))
                if r > bestr:
                    bestr, best = r, (i, lv)
            if best and bestr >= 0.6:            # same option, wording changed
                used.add(best[0])
                lv = best[1]
                issues.append(_mk(
                    qid, "option_wording",
                    f"Answer option wording differs from the questionnaire: "
                    f"spec {sp!r} vs live {lv!r}.",
                    sp, lv, f"{qid}: answer option label",
                    o.get("screenshot"), "High",
                    {"spec_parse": "high (option label parsed)",
                     "observation": "high (live option label captured from DOM)",
                     "determinism": "high (same option, text provably differs)"}))
    return issues


def llm_wording(spec, obs):
    """Semantic tier for DYNAMIC spec text (pipes / <INSERT> / [A / B] alternates), where a
    raw diff is too noisy. Only runs if ANTHROPIC_API_KEY is set. LLM-judged -> Med (review)."""
    import llm
    llm.load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []  # deterministic-only run

    pairs, meta = [], {}
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not o.get("text") or not q.get("text"):
            continue
        if not DYNAMIC.search(q["text"]):
            continue  # static text is handled deterministically
        if _extraction_contaminated(q["text"], o["text"]):
            continue
        pairs.append({"id": qid, "spec_text": q["text"], "live_text": o["text"]})
        meta[qid] = o

    verdicts = llm.judge_wording(pairs)
    issues = []
    for v in verdicts:
        if v.get("equivalent") is False:
            qid = v.get("id")
            o = meta.get(qid, {})
            q = spec.get(qid, {})
            issues.append(_mk(
                qid, "wording_semantic",
                f"Live wording differs from the questionnaire (LLM-judged): "
                f"{v.get('difference', '').strip()}",
                q.get("text"), o.get("text"),
                f"{qid}: dynamic question text (semantic compare)",
                o.get("screenshot"), "Med",
                {"spec_parse": "med (dynamic text; pipes resolved)",
                 "observation": "high (live text captured)",
                 "determinism": "low (LLM semantic judgment -> human review)"}))
    return issues


def run_textual_checks(spec_path="spec/survey2_spec.json",
                       obs_path="output/run_observations.json",
                       include_wording_diff=False):
    """Ships the two high-precision deterministic checks by default. The naive wording-diff
    is OFF by default: it empirically over-fires on our own text-extraction edge cases
    (looped/scale/description pages), so wording EQUIVALENCE is deferred to the semantic
    LLM tier (llm_wording) rather than shipped as noisy Med flags."""
    spec, obs = load(spec_path, obs_path)
    issues = []
    issues += t2_struck_text_present(spec, obs)
    issues += t1_question_wording(spec, obs)   # gated word-level stem diff
    issues += t4_option_wording(spec, obs)
    issues += t5_unresolved_placeholder(spec, obs)
    if include_wording_diff:
        issues += t1_wording_diff(spec, obs)   # legacy boolean diff (off by default)
    issues += llm_wording(spec, obs)
    return issues


if __name__ == "__main__":
    issues = run_textual_checks()
    print(f"TEXTUAL checks found {len(issues)} issue(s) "
          f"(LLM tier: {'on' if os.environ.get('ANTHROPIC_API_KEY') else 'off'}):\n")
    for it in issues:
        print(f"  [{it['confidence']}] {it['qid']} ({it['check']}): {it['explanation']}")
        print(f"        screenshot: {it['screenshot']}\n")
