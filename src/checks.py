"""
FR3 (passive checks) + FR5 (confidence).

Passive checks run over the spec (spec/survey2_spec.json) aligned to the live-walk
observations (output/run_observations.json) by QID. Each returns issue dicts carrying a
decomposed confidence (High/Med/Low) per the plan:
    confidence = f(spec-parse certainty, survey-observation certainty, check determinism)

Covered here:
  - wording_check      -> Q_Aware   (struck-through text still shown live)
  - option_order_check -> S_ProcType (anchor option not at the bottom)
  - carry_forward_check-> Q_Current  (piped question shows more than the source selection)

Active validation probes (Q_VW_TC, S_Contact_Info) live in probe.py because they must
drive the browser, not just read observations.
"""
import json
import re


# Live QIDs carry a product-loop suffix (Q_VW_TC_Cyber) and/or a sample-variant suffix
# (Q_Aware_vis). Strip either to align to the spec's base QID.
PRODUCT_SUFFIXES = ("_Cyber", "_SaaS", "_Identity", "_Exposures", "_Software",
                    "_SaaSApps", "_vis", "_hid", "_pan")


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def base_qid(qid):
    """Strip a product-loop suffix (Q_VW_TC_Cyber -> Q_VW_TC) so live QIDs align to spec."""
    for s in PRODUCT_SUFFIXES:
        if qid and qid.endswith(s):
            return qid[: -len(s)]
    return qid


def load(spec_path="spec/survey2_spec.json", obs_path="output/run_observations.json"):
    spec = {q["id"]: q for q in json.load(open(spec_path))["questions"]}
    obs = [o for o in json.load(open(obs_path)) if o.get("qid")]
    # align by base QID (dropping product suffix); keep first observation per QID.
    seen = {}
    for o in obs:
        for key in {o["qid"], base_qid(o["qid"])}:
            seen.setdefault(key, o)
    return spec, seen


def _match_live_to_spec(live_labels, spec_options):
    """Map each live option label to its spec option (or None). Normalized containment."""
    spec_norm = [( _norm(o["label"]), o) for o in spec_options]
    out = []
    for ll in live_labels:
        nll = _norm(ll)
        hit = None
        for sn, so in spec_norm:
            if sn and (sn == nll or sn in nll or nll in sn):
                hit = so
                break
        out.append((ll, hit))
    return out


def wording_check(spec, obs):
    """A struck-through (deleted) fragment from the spec must NOT appear in the live text."""
    issues = []
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not q.get("text_struck"):
            continue
        live = _norm(o.get("text"))
        for frag in q["text_struck"]:
            f = _norm(frag)
            if f and f.strip(" ,") and f.strip(" ,") in live:
                issues.append({
                    "qid": qid, "check": "wording", "issue_type": "Textual",
                    "explanation": f"Text {frag!r} is struck-through (deleted) in the "
                                   f"questionnaire but still appears in the live survey.",
                    "expected": f"'{frag.strip()}' removed from question text",
                    "observed": o.get("text"),
                    "spec_clause": f"{qid}: struck run {frag!r}",
                    "screenshot": o.get("screenshot"),
                    "confidence": "High",
                    "confidence_factors": {
                        "spec_parse": "high (explicit strike-through run)",
                        "observation": "high (exact substring present in live text)",
                        "determinism": "high (mechanical substring match)",
                    },
                })
    return issues


def option_order_check(spec, obs):
    """Anchor options (Other / None of the above) must sit at the bottom of the list."""
    issues = []
    for qid, q in spec.items():
        o = obs.get(qid)
        if not o or not o.get("options") or not q.get("options"):
            continue
        mapped = _match_live_to_spec(o["options"], q["options"])
        # position (in live order) of anchor vs non-anchor options
        anchor_pos, nonanchor_pos = [], []
        for idx, (ll, so) in enumerate(mapped):
            if so is None:
                continue
            (anchor_pos if so.get("anchor") else nonanchor_pos).append((idx, ll))
        if not anchor_pos or not nonanchor_pos:
            continue
        for a_idx, a_label in anchor_pos:
            after = [lbl for i, lbl in nonanchor_pos if i > a_idx]
            if after:
                issues.append({
                    "qid": qid, "check": "option_order", "issue_type": "Visual",
                    "explanation": f"Anchor option '{a_label}' should be pinned to the "
                                   f"bottom but appears before {len(after)} other option(s) "
                                   f"live: {after}.",
                    "expected": f"'{a_label}' last in the option list",
                    "observed": f"live order: {o['options']}",
                    "spec_clause": f"{qid}: option '{a_label}' is an anchor",
                    "screenshot": o.get("screenshot"),
                    "confidence": "High",
                    "confidence_factors": {
                        "spec_parse": "high (Other/None convention or [Anchor])",
                        "observation": "high (live DOM order captured directly)",
                        "determinism": "high (positional comparison)",
                    },
                })
    return issues


def carry_forward_check(spec, obs):
    """A piped question's non-anchor options must be a subset of the source question's
    options. If the piped question shows the full list (nothing filtered), flag it."""
    issues = []
    for qid, q in spec.items():
        src = q.get("carry_forward")
        o = obs.get(qid)
        if not src or not o or not o.get("options"):
            continue
        src_obs = obs.get(src)
        if not src_obs or not src_obs.get("options"):
            # can't verify without having seen the source page
            continue
        live = [l for l in o["options"] if not re.match(r"(other|none of the above)", _norm(l))]
        src_set = {_norm(l) for l in src_obs["options"]}
        extra = [l for l in live if _norm(l) not in src_set]
        # bug manifests as the piped question showing options that were NOT in the source
        if extra:
            issues.append({
                "qid": qid, "check": "carry_forward", "issue_type": "Logical",
                "explanation": f"{qid} pipes from {src} but shows option(s) not carried "
                               f"forward from {src}: {extra}.",
                "expected": f"options ⊆ selections at {src}",
                "observed": f"live options: {o['options']}",
                "spec_clause": f"{qid}: PIPE {src}",
                "screenshot": o.get("screenshot"),
                "confidence": "Med",
                "confidence_factors": {
                    "spec_parse": "high (explicit PIPE marker)",
                    "observation": "med (compares against source options, not exact selection)",
                    "determinism": "med (subset heuristic)",
                },
            })
    return issues


def run_passive_checks(spec_path="spec/survey2_spec.json",
                       obs_path="output/run_observations.json"):
    spec, obs = load(spec_path, obs_path)
    issues = []
    issues += wording_check(spec, obs)
    issues += option_order_check(spec, obs)
    issues += carry_forward_check(spec, obs)
    return issues


if __name__ == "__main__":
    issues = run_passive_checks()
    print(f"Passive checks found {len(issues)} issue(s):\n")
    for it in issues:
        print(f"  [{it['confidence']}] {it['qid']} ({it['check']}/{it['issue_type']}): "
              f"{it['explanation']}")
        print(f"        screenshot: {it['screenshot']}\n")
