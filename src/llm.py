"""
LLM helper — semantic wording-equivalence judge (Claude, Sonnet tier).

Used by the TEXTUAL semantic tier: for questions whose spec text is dynamic (pipes /
<INSERT> / [A / B] alternates), a raw string diff is too noisy, so we ask Claude whether the
live wording is EQUIVALENT to the intended wording modulo legitimate substitution. Findings
default to Med confidence -> human review (LLM judgment, not a mechanical fact).

Key is read from qa-agent/.env (gitignored). No key -> tier is skipped upstream.
"""
import json
import os
import re

MODEL = "claude-sonnet-4-6"  # cost-efficient judge per the plan's model tiering


def load_env(path=None):
    path = path or os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


SYSTEM = (
    "You are a survey-QA assistant. You compare the INTENDED question wording (from the "
    "questionnaire, with any struck/deleted text already removed) against the LIVE wording "
    "shown in the programmed survey.\n"
    "Treat these as EQUIVALENT (NOT issues): resolved piping/placeholders, a chosen option "
    "from [A / B] pipe-alternate syntax, whitespace/casing/punctuation, smart quotes, and a "
    "product name/price legitimately inserted.\n"
    "Treat as NOT equivalent (an ISSUE): words added or removed that change meaning; text "
    "that was deleted in the questionnaire but appears live; changed numbers, scales, or "
    "question intent.\n"
    "Return ONLY a JSON array, one object per item: "
    '{"id": <id>, "equivalent": true|false, "difference": "<short; empty if equivalent>"}.'
)


def judge_wording(pairs, model=MODEL, max_chars=600):
    """pairs: list of {id, spec_text, live_text} -> list of {id, equivalent, difference}."""
    if not pairs:
        return []
    load_env()
    import anthropic

    client = anthropic.Anthropic()
    items = [{"id": p["id"],
              "intended": (p["spec_text"] or "")[:max_chars],
              "live": (p["live_text"] or "")[:max_chars]} for p in pairs]
    user = "Compare each item.\n" + json.dumps(items, ensure_ascii=False, indent=2)
    resp = client.messages.create(
        model=model, max_tokens=1500, system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
