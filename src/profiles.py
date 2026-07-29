"""
Respondent profiles for multi-path coverage.

Each profile is one qualifying answer map (a path). Their union of reached questions is what
gives question coverage. Profiles are chosen to unlock DIFFERENT branches:

  A  IT-DM via Procurement -> reaches S_ProcType + the Cyber/Identities/SaaS product loops
  B  Security-DM (Cybersecurity role) -> reaches the Cyber/Exposures/Software product loops
     (and deliberately skips S_ProcType, which only shows for Procurement)

Add more profiles here to cover more branches — the runner/checks don't change.
"""
from runner import QUALIFY as _BASE

PROFILE_A = dict(_BASE)  # Procurement -> IT decision-maker

PROFILE_B = dict(_BASE)
PROFILE_B["S_Role"] = "Cyber security"   # -> Security decision-maker (HQCOTYPE=1)
PROFILE_B.pop("S_ProcType", None)         # not shown on this branch

PROFILES = {
    "A": {"label": "IT-DM · Procurement→IT Software",
          "qualify": PROFILE_A, "out": "output/obs_A.json", "shots": "output/shots"},
    "B": {"label": "Security-DM · Cybersecurity role",
          "qualify": PROFILE_B, "out": "output/obs_B.json", "shots": "output/shots_B"},
}
