"""
Multi-path coverage orchestrator.

Runs a set of profiles (paths), unions their observations, and measures QUESTION COVERAGE
against the spec: which answerable spec questions were reached by at least one path, and
which remain uncovered (an honest gap list). Writes the union to run_observations.json (what
the checks read) and coverage.json.

Reuses a cached per-profile walk (obs_<P>.json) if present, so a demo doesn't re-walk.
"""
import json
import os

from checks import base_qid
from profiles import PROFILES

UNION_PATH = "output/run_observations.json"
COVERAGE_PATH = "output/coverage.json"


def _tag(obs, profile):
    for o in obs:
        o["profile"] = profile
    return obs


def spec_targets(spec_path="spec/survey2_spec.json"):
    """Answerable spec questions to cover: exclude hidden HQ variables and empty pseudo-Qs."""
    spec = json.load(open(spec_path))["questions"]
    seen, target = set(), []
    for q in spec:
        qid = q["id"]
        if qid.startswith("HQ") or qid in seen or not q.get("text"):
            continue
        seen.add(qid)
        target.append(qid)
    return target


def compute_coverage(all_obs, spec_path="spec/survey2_spec.json"):
    target = spec_targets(spec_path)
    reached = {base_qid(o["qid"]) for o in all_obs if o.get("qid")}
    covered = [q for q in target if q in reached]
    uncovered = [q for q in target if q not in reached]
    per_profile = {}
    for o in all_obs:
        if o.get("qid"):
            per_profile.setdefault(o.get("profile", "?"), set()).add(base_qid(o["qid"]))
    return {
        "n_target": len(target), "n_covered": len(covered),
        "coverage_pct": round(100 * len(covered) / max(len(target), 1)),
        "n_paths": len({o.get("profile") for o in all_obs if o.get("profile")}),
        "covered": covered, "uncovered": uncovered,
        "per_profile": {k: sorted(v) for k, v in per_profile.items()},
    }


def run_profiles(candidates=None, reuse=True, headless=True, on_walk=None):
    """Walk a candidate pool of profiles, then GREEDILY select the minimum subset that
    maximizes question coverage (set-cover), union their observations, and report coverage.
    Not a hardcoded count — the number of profiles used is derived from coverage."""
    candidates = list(candidates) if candidates else list(PROFILES.keys())
    target = set(spec_targets())
    reach, obs_by = {}, {}
    for idx, name in enumerate(candidates):
        if on_walk:
            on_walk(name, idx, len(candidates))
        p = PROFILES[name]
        if reuse and os.path.exists(p["out"]):
            obs = json.load(open(p["out"]))
        else:
            from runner import run
            obs = run(qualify=p["qualify"], out_path=p["out"], shots=p["shots"],
                      profile_name=name, headless=headless)
        _tag(obs, name)
        obs_by[name] = obs
        reach[name] = {base_qid(o["qid"]) for o in obs if o.get("qid")} & target

    # greedy set cover: repeatedly add the profile that covers the most still-uncovered Qs
    selected, covered, pool = [], set(), list(candidates)
    while pool:
        best = max(pool, key=lambda n: len(reach[n] - covered))
        if not (reach[best] - covered):
            break
        selected.append(best)
        covered |= reach[best]
        pool.remove(best)

    union = [o for name in selected for o in obs_by[name]]
    with open(UNION_PATH, "w") as f:
        json.dump(union, f, indent=2, ensure_ascii=False)
    cov = compute_coverage(union)
    cov["selected_profiles"] = [f"{n} ({PROFILES[n]['label']})" for n in selected]
    cov["candidate_pool"] = candidates
    cov["per_candidate_reach"] = {n: len(reach[n]) for n in candidates}
    with open(COVERAGE_PATH, "w") as f:
        json.dump(cov, f, indent=2, ensure_ascii=False)
    return union, cov


if __name__ == "__main__":
    import sys
    cands = sys.argv[1:] or None
    _, cov = run_profiles(cands)
    print(f"\nCoverage: {cov['n_covered']}/{cov['n_target']} ({cov['coverage_pct']}%) "
          f"using {len(cov['selected_profiles'])} of {len(cov['candidate_pool'])} candidate "
          f"profiles (minimum set)")
    print("selected:", cov["selected_profiles"])
    print("per-candidate reach:", cov["per_candidate_reach"])
    print("uncovered:", cov["uncovered"])
