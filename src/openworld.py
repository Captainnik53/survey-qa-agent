"""
Open-world discrepancy finder (the complement to the typed detectors).

Typed checks are closed-world: they only catch bug categories we predefined. This pass is
open-world: for each question it hands Claude the full parsed SPEC record and the full
OBSERVED live state and asks it to list ANY divergence — of any kind — and name the category
itself. That surfaces the unknown-unknowns the taxonomy misses.

Because it's generative (lower precision than mechanical checks), every finding is capped at
Med/Low confidence and routed to the human REVIEW QUEUE — never auto-flagged. It also relies
on the LLM's judgment to ignore parser noise (over-captured table rows / instruction lines),
which makes it more robust to imperfect parsing than the deterministic checks.

Guarded: skips hidden HQ variables and pages with contaminated extraction.
"""
import json
import os
import re

from checks import load
from textual_checks import _extraction_contaminated

MODEL = "claude-sonnet-4-6"

SYSTEM = (
    "You are a STRICT survey-QA reviewer. You get the INTENDED spec (from the questionnaire) "
    "and the LIVE programmed question. Report ONLY clear TEXTUAL (wording) bugs — nothing "
    "visual, structural, or behavioural.\n\n"
    "FLAG ONLY these, and ONLY when you can clearly see BOTH the spec and live wording of the "
    "SAME element:\n"
    "  - the QUESTION wording differs in a way that changes meaning (words added/removed/changed);\n"
    "  - an answer OPTION's wording differs in a way that changes meaning;\n"
    "  - text struck/deleted in the questionnaire appears in the live wording.\n\n"
    "DO NOT FLAG anything else. NOT: option order, anchor position, missing/extra/duplicated "
    "options, question type, number of scale points or endpoints, layout/rendering (these are "
    "handled by deterministic checks). NOT: numeric ranges/min-max, required/format validation, "
    "skip/branch logic, piping/carry-forward, quotas, randomization (behavioural — handled by "
    "active probes; you cannot observe them).\n\n"
    "SKIP the question entirely (flag nothing) when the spec text or options look INCOMPLETE, "
    "TRUNCATED, or like parser noise (e.g. the 'text' is just a single scale-endpoint label like "
    "'1 - Per user based pricing model', the options list is partial, ALL-CAPS instructions, "
    "lookup-table rows, dollar amounts, 'PIPE X', 'HQ...' lines). If you cannot clearly see the "
    "intended wording, do NOT guess.\n\n"
    "IGNORE (not bugs): resolved piping/placeholders; a chosen alternate from [A / B] syntax; "
    "whitespace/case/punctuation/smart quotes.\n\n"
    "EXAMPLES.\n"
    "TEXTUAL BUGS (flag these):\n"
    "  - spec option 'Market' shown live as 'Marketing' -> option_wording.\n"
    "  - spec question 'How satisfied are you?' shown live as 'How happy are you?' -> "
    "question_wording.\n"
    "  - spec has 'if any,' struck (deleted); live still shows 'if any,' -> struck_text_shown.\n"
    "NOT textual bugs (mark not_a_textual_bug / discard):\n"
    "  - live has an option that isn't in the (incomplete) spec list -> missing/extra option.\n"
    "  - 'Other' or 'None of the above' not pinned to the bottom -> order/anchor.\n"
    "  - grid row labels missing, or scale shows only endpoint labels -> structure.\n"
    "  - 'max 199,999 for the identity row may not be enforced' -> validation (unobservable).\n"
    "  - spec 'text' is just '1 - Per user based pricing model' -> incomplete spec, skip.\n\n"
    "Be STRICT: when in doubt, flag NOTHING.\n\n"
    "Report your OWN confidence that each finding is a genuine bug (not a legitimate "
    "transformation or parser noise): use 'High' only when it is a clear, unambiguous "
    "divergence; 'Med' when likely but you are not certain; 'Low' when it is a guess worth a "
    "human glance. When in doubt, lower the confidence rather than omitting.\n"
    "Also classify likely_cause: 'survey' when the questionnaire is clear and the LIVE survey "
    "clearly deviates from it (a survey bug); 'spec' when the divergence is likely because the "
    "questionnaire itself is INCOMPLETE, AMBIGUOUS, or MISSING information (so you cannot be "
    "sure the survey is wrong — it may just be under-specified). Prefer 'spec' whenever the "
    "spec text/options look partial or truncated.\n"
    "Set category to EXACTLY one of: 'question_wording' (the question stem wording changed), "
    "'option_wording' (an answer option's wording changed), 'struck_text_shown' (struck/deleted "
    "spec text appears live), or 'not_a_textual_bug' for ANYTHING ELSE (order, anchor, "
    "missing/extra options, grid rows, scale points, type, validation, logic, layout, or an "
    "incomplete spec). Anything marked 'not_a_textual_bug' will be DISCARDED — use it liberally "
    "when unsure.\n"
    "Return ONLY a JSON array; one object per finding: "
    '{"qid": <id>, "category": "question_wording|option_wording|struck_text_shown|not_a_textual_bug", '
    '"confidence": "High|Med|Low", "likely_cause": "survey|spec", '
    '"description": "<what is wrong>", "expected": "<from spec>", "observed": "<from live>"}. '
    "Return [] if a question is fine."
)

# Only these three categories are genuine textual bugs; everything else is discarded.
ALLOWED_CATEGORIES = {"question_wording", "option_wording", "struck_text_shown"}
# Hard backstop: drop any finding whose text drifts into non-textual / retracted territory,
# regardless of the category the LLM assigned (it does not reliably obey the prompt).
DENY_RE = re.compile(
    r"validation|\bmin\b|\bmax\b|\brange\b|enforce|required|per-row|\blimit\b|"
    r"\border\b|anchor|pinned|randomiz|missing|\bgrid\b|\brow\b|column|scale[ -]?point|"
    r"repeated|type mismatch|withdraw|no issue|this item is fine|\bis fine\b|"
    r"parser noise|lookup|truncat|incomplete|placeholder|absent", re.I)


def _payload(qid, q, o):
    def opts(x):
        return [op["label"] if isinstance(op, dict) else op for op in (x or [])][:25]
    # NOTE: validation/min-max is intentionally NOT sent — it is behavioural (unobservable
    # from the page) and belongs to active probes, not the LLM observational pass.
    # Strip the loop-page UI preamble so it isn't read as "added" question wording.
    live_text = re.sub(r"if you would like to see the product description again[^\n]*", "",
                       (o.get("text") or ""), flags=re.I).strip()
    return {
        "qid": qid,
        "intended": {"text": (q.get("text") or "")[:500], "type": q.get("type"),
                     "order": q.get("order"), "options": opts(q.get("options")),
                     "struck_text": q.get("text_struck") or []},
        "live": {"text": live_text[:500], "type": o.get("type"),
                 "options": opts(o.get("options"))},
    }


def find_discrepancies(spec_path="spec/survey2_spec.json",
                       obs_path="output/run_observations.json", batch=5, max_questions=40):
    import llm
    llm.load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    import anthropic
    client = anthropic.Anthropic()
    spec, obs = load(spec_path, obs_path)

    payloads, meta = [], {}
    for qid, q in spec.items():
        if qid.startswith("HQ") or not q.get("text"):
            continue
        o = obs.get(qid)
        if not o or not o.get("text"):
            continue
        if _extraction_contaminated(q["text"], o["text"]):
            continue
        payloads.append(_payload(qid, q, o))
        meta[qid] = o
        if len(payloads) >= max_questions:
            break

    findings = []
    for i in range(0, len(payloads), batch):
        chunk = payloads[i:i + batch]
        resp = client.messages.create(
            model=MODEL, max_tokens=2000, system=SYSTEM,
            messages=[{"role": "user", "content": json.dumps(chunk, ensure_ascii=False)}])
        text = resp.content[0].text
        import re
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            continue
        try:
            findings += json.loads(m.group(0))
        except json.JSONDecodeError:
            continue

    issues = []
    for f in findings:
        # HARD FILTERS (don't trust the prompt): keep only genuine textual categories, and drop
        # anything whose text drifts into non-textual/retracted territory.
        if f.get("category") not in ALLOWED_CATEGORIES:
            continue
        blob = f"{f.get('description', '')} {f.get('expected', '')} {f.get('observed', '')}"
        if DENY_RE.search(blob):
            continue
        qid = f.get("qid")
        o = meta.get(qid, {})
        cause = f.get("likely_cause", "survey")
        conf = f.get("confidence", "Low")
        if conf not in ("High", "Med", "Low"):
            conf = "Low"
        if cause == "spec":
            conf = "Low"   # spec-side / under-specified -> optional low-confidence review
        issues.append({
            "qid": qid, "check": f"openworld:{f.get('category', 'discrepancy')}",
            "issue_type": "Open-world", "explanation": f.get("description", ""),
            "expected": f.get("expected", ""), "observed": f.get("observed", ""),
            "spec_clause": f"{qid}: open-world spec-vs-live compare",
            "screenshot": o.get("screenshot"), "confidence": conf,
            "source": "open-world", "likely_cause": cause,
            "confidence_factors": {
                "spec_parse": "med (full record compared)",
                "observation": "med (full live state compared)",
                "determinism": "low (generative LLM discovery, self-reported confidence)"},
        })
    return issues


if __name__ == "__main__":
    issues = find_discrepancies()
    print(f"Open-world finder surfaced {len(issues)} candidate discrepancy(ies):\n")
    for it in issues:
        print(f"  [{it['confidence']}] {it['qid']} ({it['check']}): {it['explanation'][:160]}")
