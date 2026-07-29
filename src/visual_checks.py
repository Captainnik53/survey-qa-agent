"""
Visual bug checks  (taxonomy top-level: VISUAL).

DOM/JSON-based, NO LLM. A survey's rendered option order and page grouping are fully present
in the DOM we captured during the walk, so we compare structure deterministically and keep
the screenshot purely as human-verifiable evidence. LLM-vision is reserved for true pixel
rendering defects (overlap/truncation/broken image) — a separate, optional layer, not needed
for the bugs in these surveys.

Types implemented:
  V1  anchor-at-bottom      -> Other / None-of-the-above must be last   (catches S_ProcType)
  V2  fixed-order mismatch  -> for [as_listed] questions, live order must match spec order
  V3  alphabetical-broken   -> for [alphabetical] questions, non-anchor options must be sorted

Each issue carries decomposed confidence (all High: mechanical, directly observed).
"""
import json

from checks import _norm, _match_live_to_spec, load  # reuse spec/obs loader + matcher


def _mk(qid, subtype, explanation, expected, observed, clause, shot, conf="High", factors=None):
    return {
        "qid": qid, "check": subtype, "issue_type": "Visual",
        "explanation": explanation, "expected": expected, "observed": observed,
        "spec_clause": clause, "screenshot": shot,
        "confidence": conf,
        "confidence_factors": factors or {
            "spec_parse": "high (option list + anchor/order markers)",
            "observation": "high (live DOM order captured directly)",
            "determinism": "high (positional comparison)",
        },
    }


def v1_anchor_at_bottom(spec, obs):
    """Anchor options (Other / None of the above / [Anchor]) must sit below all others."""
    issues = []
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not o.get("options") or not q.get("options"):
            continue
        mapped = _match_live_to_spec(o["options"], q["options"])
        anchor_pos = [(i, ll) for i, (ll, so) in enumerate(mapped) if so and so.get("anchor")]
        nonanchor_pos = [(i, ll) for i, (ll, so) in enumerate(mapped) if so and not so.get("anchor")]
        if not anchor_pos or not nonanchor_pos:
            continue
        for a_idx, a_label in anchor_pos:
            after = [lbl for i, lbl in nonanchor_pos if i > a_idx]
            if after:
                issues.append(_mk(
                    qid, "anchor_position",
                    f"Anchor option '{a_label}' should be pinned to the bottom but renders "
                    f"above {len(after)} other option(s): {after}.",
                    f"'{a_label}' last in the option list",
                    f"live order: {o['options']}",
                    f"{qid}: '{a_label}' is an anchor option",
                    o.get("screenshot"),
                ))
    return issues


def v2_fixed_order(spec, obs):
    """For [as_listed] questions the live order must equal the spec order (ignoring anchors,
    which V1 owns). Skips randomize/alphabetical where a differing order is legitimate."""
    issues = []
    for qid, q in spec.items():
        if q.get("order") != "as_listed":
            continue
        o = obs.get(qid)
        if not o or not o.get("options") or len(q.get("options", [])) < 2:
            continue
        spec_seq = [_norm(op["label"]) for op in q["options"] if not op.get("anchor")]
        mapped = _match_live_to_spec(o["options"], q["options"])
        live_seq = [_norm(so["label"]) for (ll, so) in mapped if so and not so.get("anchor")]
        # PRECISION GUARD: only judge ORDER when the option SETS match (same composition).
        # If the sets differ, that's a composition/parse problem — not an order bug — so we
        # skip rather than emit a false positive (e.g. a spec whose option list over-captured
        # trailing tables/instructions).
        if set(spec_seq) != set(live_seq) or len(spec_seq) != len(live_seq) or len(spec_seq) < 2:
            continue
        if spec_seq != live_seq:
            issues.append(_mk(
                qid, "option_order",
                "Fixed-order question shows options in a different order than the questionnaire.",
                f"spec order: {[op['label'] for op in q['options'] if not op.get('anchor')]}",
                f"live order: {o['options']}",
                f"{qid}: [as_listed] fixed order",
                o.get("screenshot"),
                conf="Med",
                factors={"spec_parse": "med (option list boundary can over-capture)",
                         "observation": "high (live DOM order captured)",
                         "determinism": "high (sequence equality on matched set)"},
            ))
    return issues


def v3_alphabetical_broken(spec, obs):
    """For [alphabetical] questions, non-anchor options must be in alphabetical order."""
    issues = []
    for qid, q in spec.items():
        if q.get("order") != "alphabetical":
            continue
        o = obs.get(qid)
        if not o or not o.get("options"):
            continue
        mapped = _match_live_to_spec(o["options"], q["options"])
        live_nonanchor = [ll for (ll, so) in mapped if so and not so.get("anchor")]
        if len(live_nonanchor) < 2:
            continue
        if [_norm(x) for x in live_nonanchor] != sorted(_norm(x) for x in live_nonanchor):
            issues.append(_mk(
                qid, "alphabetical_order",
                "Question marked [alphabetical] but non-anchor options are not sorted "
                "alphabetically in the live survey.",
                f"alphabetical: {sorted(live_nonanchor)}",
                f"live order: {live_nonanchor}",
                f"{qid}: [alphabetical]",
                o.get("screenshot"),
                conf="Med",
                factors={"spec_parse": "high ([alphabetical] marker)",
                         "observation": "high (live order captured)",
                         "determinism": "med (locale/tie-break ambiguity in sorting)"},
            ))
    return issues


def run_visual_checks(spec_path="spec/survey2_spec.json",
                      obs_path="output/run_observations.json"):
    spec, obs = load(spec_path, obs_path)
    issues = []
    issues += v1_anchor_at_bottom(spec, obs)
    issues += v2_fixed_order(spec, obs)
    issues += v3_alphabetical_broken(spec, obs)
    return issues


if __name__ == "__main__":
    issues = run_visual_checks()
    print(f"VISUAL checks found {len(issues)} issue(s):\n")
    for it in issues:
        print(f"  [{it['confidence']}] {it['qid']} ({it['check']}): {it['explanation']}")
        print(f"        expected: {it['expected']}")
        print(f"        screenshot: {it['screenshot']}\n")
