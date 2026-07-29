"""
Condition interpreter — turns spec routing rules into concrete answers.

Used by the routing checks to (a) auto-generate base-condition test cases from `ASK IF`
strings and (b) derive qualifying / violating answers. Deterministic; handles the simple
`QID = value` conditions (value = 1-based option index OR a label). Complex conditions
(compound / range / hidden-variable) are left for a future extension and reported as
"too complex to interpret" rather than guessed.
"""
import re


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def resolve_option(source_q, value_str):
    """value_str '8' (1-based option index) or 'Procurement' (label) -> option label."""
    opts = source_q.get("options", [])
    value_str = value_str.strip()
    if value_str.isdigit():
        i = int(value_str) - 1
        return opts[i]["label"] if 0 <= i < len(opts) else None
    for o in opts:
        if _norm(value_str) and (_norm(value_str) in _norm(o["label"])
                                 or _norm(o["label"]) in _norm(value_str)):
            return o["label"]
    return None


def interpret_ask(cond, spec_by_id):
    """Parse a simple 'SOURCE = value' condition -> (source_qid, satisfy_label) or None."""
    m = re.search(r"([A-Za-z]_?[A-Za-z]\w*)\s*=\s*(\d+|[A-Za-z][\w ]*)", cond or "")
    if not m:
        return None
    raw_qid, value = m.group(1), m.group(2).strip()
    source_qid = next((qid for qid in spec_by_id if qid.lower() == raw_qid.lower()), None)
    if not source_qid:  # tolerate underscore/spacing differences
        key = raw_qid.lower().replace("_", "")
        source_qid = next((qid for qid in spec_by_id if qid.lower().replace("_", "") == key), None)
    if not source_qid:
        return None
    satisfy = resolve_option(spec_by_id[source_qid], value)
    return (source_qid, satisfy) if satisfy else None


def pick_violate(source_q, satisfy_label):
    """A different, non-terminating option that does NOT satisfy the base (so the gated
    question should be correctly skipped)."""
    for o in source_q.get("options", []):
        if o.get("terminate"):
            continue
        lab = _norm(o["label"])
        if lab == _norm(satisfy_label) or lab.startswith("none of the above"):
            continue
        return o["label"]
    return None


def qualifying_choice(q):
    """A safe, non-terminating answer for any single/multi question, from the spec."""
    for o in q.get("options", []):
        lab = _norm(o["label"])
        if o.get("terminate") or lab.startswith("none of the above"):
            continue
        return o["label"]
    opts = q.get("options", [])
    return opts[0]["label"] if opts else None
