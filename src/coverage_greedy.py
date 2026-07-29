"""
Greedy coverage — spec-driven candidate generation + greedy set-cover.

Goal: reach every answerable question with the FEWEST walks. Two parts:

  1) generate_candidate_profiles(spec): auto-build candidate answer-profiles from the spec's
     branch structure — a base qualifying profile, one profile per role/branch option (these
     unlock different downstream branches), and one per resolvable gated condition (pin the
     ASK-IF so the gated question is reached). No LLM; all from the parsed spec + interpreter.

  2) greedy_min_cover(reach): the greedy set-cover — repeatedly add the profile that covers
     the most still-uncovered questions, until none adds anything. This is the minimum-path
     idea: one path covers a whole chain, so a few branch-profiles cover everything.

Each candidate's reached-question set comes from a walk (cached per profile, or live).
"""
import json
import os

import runner
from checks import base_qid
from spec_logic import interpret_ask, qualifying_choice

SHOTS_DIR = "output/shots_cov"


def question_universe(spec_list):
    """Answerable questions to cover — exclude hidden HQ variables, intro/display screens
    (…INTRO), and empty pseudo-questions."""
    seen, uni = set(), []
    for q in spec_list:
        qid = q["id"]
        if (qid.startswith("HQ") or qid.upper().endswith("INTRO")
                or qid in seen or not q.get("text")):
            continue
        seen.add(qid)
        uni.append(qid)
    return uni


def generate_candidate_profiles(spec_list):
    """Return {name: qualify_override_dict} candidate profiles derived from the spec.
    Base is the curated qualifying map; branch/gated variants override specific answers."""
    spec_by_id = {q["id"]: q for q in spec_list}
    base = dict(runner.QUALIFY)
    candidates = {"base": base}

    # (a) Role/branch variants: the role question drives the biggest downstream branch
    # (HQCOTYPE -> different product loops). One candidate per non-terminate role option.
    role_q = spec_by_id.get("S_Role")
    if role_q:
        for o in role_q.get("options", []):
            if o.get("terminate") or o["label"].lower().startswith("other"):
                continue
            prof = dict(base)
            prof["S_Role"] = o["label"]
            # a Procurement respondent must also pass the ProcType gate
            if "procurement" not in o["label"].lower():
                prof.pop("S_ProcType", None)
            candidates[f"role::{o['label'][:18]}"] = prof

    # (b) Gated-question variants: pin each resolvable ASK-IF so its question is reached
    for q in spec_list:
        parsed = interpret_ask(q.get("ask_condition"), spec_by_id) if q.get("ask_condition") else None
        if not parsed:
            continue
        source_qid, satisfy = parsed
        prof = dict(base)
        prof[source_qid] = satisfy
        candidates[f"gated::{q['id']}"] = prof

    return candidates


def greedy_min_cover(reach, universe):
    """Pure greedy set-cover. reach: {name: set(qids)}; universe: set(qids).
    Returns (selected_names, covered_qids)."""
    universe = set(universe)
    covered, selected, pool = set(), [], list(reach.keys())
    while pool:
        best = max(pool, key=lambda n: len((reach[n] & universe) - covered))
        gain = (reach[best] & universe) - covered
        if not gain:
            break
        selected.append(best)
        covered |= reach[best] & universe
        pool.remove(best)
    return selected, covered & universe


def _reach_from_obs(obs):
    return {base_qid(o["qid"]) for o in obs if o.get("qid")}


def build_coverage(spec_path="spec/survey2_spec.json", live=False, headless=True, max_walk=6):
    """Generate candidates, get each one's reached set (cached obs_<name>.json or a live walk),
    greedy-select the minimum, and report coverage."""
    spec = json.load(open(spec_path))["questions"]
    universe = question_universe(spec)
    candidates = generate_candidate_profiles(spec)

    reach, walked = {}, 0
    for name, qualify in candidates.items():
        cache = f"output/cov_{name.replace('::', '_').replace(' ', '_')}.json"
        if os.path.exists(cache):
            reach[name] = _reach_from_obs(json.load(open(cache)))
        elif live and walked < max_walk:
            walked += 1
            obs = runner.run(qualify=qualify, out_path=cache,
                             shots=f"{SHOTS_DIR}/{name}".replace("::", "_"),
                             profile_name=name, headless=headless)
            reach[name] = _reach_from_obs(obs)
        # else: unknown reach -> skip (can't select what we haven't measured)

    selected, covered = greedy_min_cover(reach, universe)
    result = {
        "universe": len(universe),
        "covered": len(covered),
        "coverage_pct": round(100 * len(covered) / max(len(universe), 1)),
        "n_candidates": len(candidates),
        "n_measured": len(reach),
        "selected_profiles": selected,
        "uncovered": sorted(set(universe) - covered),
    }
    with open("output/coverage_greedy.json", "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result


def _safe(name):
    return name.replace("::", "_").replace(" ", "_").replace("/", "_")


def build_and_select(spec_path="spec/survey2_spec.json", live=False, reuse=True,
                     headless=True, max_walk=8, on_walk=None):
    """MERGED multi-path: generate candidates from the spec, MEASURE each one's reach (cached
    walk if available, else a live walk — capped), GREEDY-select the minimum, then union the
    SELECTED walks into run_observations.json (+ coverage.json) for the checks to run on.

    Reach isn't reliably predictable from this spec statically, so we measure by walking and
    reuse the per-candidate cache to avoid redundant work."""
    spec = json.load(open(spec_path))["questions"]
    universe = question_universe(spec)
    candidates = generate_candidate_profiles(spec)

    reach, obs_by, walked = {}, {}, 0
    for name, qualify in candidates.items():
        cache = f"output/cov_{_safe(name)}.json"
        obs = None
        if reuse and os.path.exists(cache):
            obs = json.load(open(cache))
        elif live and walked < max_walk:
            walked += 1
            if on_walk:
                on_walk(name, walked)
            obs = runner.run(qualify=qualify, out_path=cache,
                             shots=f"{SHOTS_DIR}/{_safe(name)}", profile_name=name,
                             headless=headless)
        if obs is None:
            continue
        for o in obs:
            o["profile"] = name
        obs_by[name] = obs
        reach[name] = {base_qid(o["qid"]) for o in obs if o.get("qid")} & set(universe)

    selected, covered = greedy_min_cover(reach, universe)
    union = [o for n in selected for o in obs_by[n]]
    with open("output/run_observations.json", "w") as f:
        json.dump(union, f, indent=2, ensure_ascii=False)
    cov = {
        "coverage_pct": round(100 * len(covered) / max(len(universe), 1)),
        "n_covered": len(covered), "n_target": len(universe), "n_paths": len(selected),
        "selected_profiles": selected, "candidate_pool": list(candidates.keys()),
        "per_candidate_reach": {n: sorted(reach[n]) for n in reach},
        "per_profile": {n: sorted(reach[n]) for n in selected},
        "uncovered": sorted(set(universe) - covered),
    }
    with open("output/coverage.json", "w") as f:
        json.dump(cov, f, indent=2, ensure_ascii=False)
    return union, cov


if __name__ == "__main__":
    import sys
    live = "live" in sys.argv
    r = build_coverage(live=live)
    print(f"Candidates generated: {r['n_candidates']} (measured: {r['n_measured']})")
    print(f"Coverage: {r['covered']}/{r['universe']} ({r['coverage_pct']}%) "
          f"using {len(r['selected_profiles'])} profiles: {r['selected_profiles']}")
    print(f"Uncovered: {r['uncovered']}")
