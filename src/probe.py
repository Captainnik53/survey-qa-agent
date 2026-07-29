"""
FR3 (active validation probes).

Some bugs can't be seen by observation alone — you have to *try* a bad answer and see if
the survey wrongly lets you proceed. This drives the browser with the same qualifying path
as the runner, stops at a target question, enters an invalid value, and clicks Continue:

  - Q_VW_TC        -> enter 19,000,000 (no sane max; breaks the < next-answer rule)
  - S_Contact_Info -> enter junk text in the Phone Number field (no format validation)

Outcome logic: if the target question is no longer shown after Continue, the survey ACCEPTED
the invalid input -> BUG. If it stays / shows an error, validation is working -> no issue.
"""
import json
import os

from playwright.sync_api import sync_playwright

import runner  # reuse URL, QUALIFY, observe, question_type, answer_question, TERMINATION_MARKERS

SHOTS = "output/probes"
PRODUCT_SUFFIXES = ("_Cyber", "_SaaS", "_Identity", "_Exposures", "_Software",
                    "_SaaSApps", "_vis", "_hid", "_pan")


def base_qid(qid):
    for s in PRODUCT_SUFFIXES:
        if qid.endswith(s):
            return qid[: -len(s)]
    return qid


def _fill_invalid(qdiv, target_base, value, field_hint=None):
    """Type an invalid value into the target's input(s). If field_hint given (e.g. 'phone'),
    only fill the matching field."""
    inps = qdiv.locator("input[type=number], input[type=text], textarea")
    k = inps.count()
    filled = []
    for i in range(k):
        inp = inps.nth(i)
        if field_hint:
            # match by nearby label / aria / name
            aria = (inp.get_attribute("aria-labelledby") or "") + (inp.get_attribute("name") or "")
            lab = ""
            try:
                lab = qdiv.locator(f"label[for='{inp.get_attribute('id')}']").inner_text()
            except Exception:
                pass
            if field_hint.lower() not in (aria + lab).lower():
                continue
        inp.fill(str(value))
        filled.append(i)
    return filled


def probe(target_base, invalid_value, field_hint=None, max_pages=140, headless=True):
    os.makedirs(SHOTS, exist_ok=True)
    result = {"target": target_base, "invalid_value": invalid_value,
              "reached": False, "issue": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(runner.URL, wait_until="networkidle", timeout=60000)

        for step in range(max_pages):
            page.wait_for_timeout(300)
            body = page.locator("body").inner_text().lower()
            if any(m in body for m in runner.TERMINATION_MARKERS):
                result["error"] = f"terminated before reaching {target_base}"
                break

            qdivs = page.locator("div.question")
            page_obs = runner.observe(page)
            # is the target on this page?
            target_idx = next((i for i, o in enumerate(page_obs)
                               if base_qid(o["qid"]) == target_base), None)

            if target_idx is not None:
                o = page_obs[target_idx]
                qdiv = page.locator(f"#question_{o['qid']}")
                before = f"{SHOTS}/{target_base}_before.png"
                page.screenshot(path=before, full_page=True)
                filled = _fill_invalid(qdiv, target_base, invalid_value, field_hint)
                result["reached"] = True
                result["live_qid"] = o["qid"]
                if not filled:
                    result["error"] = "no input field matched to fill"
                    page.screenshot(path=f"{SHOTS}/{target_base}_nofield.png", full_page=True)
                    break
                # try to proceed
                if page.locator("#btn_continue").count():
                    page.locator("#btn_continue").first.click()
                    page.wait_for_load_state("networkidle", timeout=60000)
                    page.wait_for_timeout(500)
                after = f"{SHOTS}/{target_base}_after.png"
                page.screenshot(path=after, full_page=True)
                # did we advance past the target?
                still = page.locator(f"#question_{o['qid']}").count() > 0
                accepted = not still
                if accepted:
                    result["issue"] = {
                        "qid": target_base, "check": "validation_probe", "issue_type": "Logical",
                        "explanation": f"Entered invalid value {invalid_value!r} at {o['qid']} "
                                       f"and the survey ACCEPTED it and advanced — the field is "
                                       f"missing proper validation.",
                        "expected": "survey blocks the invalid value / shows an error",
                        "observed": f"advanced past {o['qid']} with value {invalid_value!r}",
                        "spec_clause": f"{target_base}: input validation rule",
                        "screenshot": after, "screenshot_before": before,
                        "confidence": "High",
                        "confidence_factors": {
                            "spec_parse": "high (validation rule / ordering constraint in spec)",
                            "observation": "high (page advanced = accepted, directly observed)",
                            "determinism": "high (active input/response test)",
                        },
                    }
                else:
                    result["blocked"] = True
                break

            # not the target: answer normally and continue
            if not page_obs:
                if page.locator("#btn_continue").count():
                    page.locator("#btn_continue").first.click()
                    page.wait_for_load_state("networkidle", timeout=60000)
                    continue
                result["error"] = f"dead page before reaching {target_base}"
                break
            for o in page_obs:
                qdiv = page.locator(f"#question_{o['qid']}")
                try:
                    runner.answer_question(qdiv, o["qid"], o["type"])
                except Exception:
                    pass
            if not page.locator("#btn_continue").count():
                result["error"] = f"no continue before reaching {target_base}"
                break
            page.locator("#btn_continue").first.click()
            page.wait_for_load_state("networkidle", timeout=60000)

        browser.close()
    return result


PROBES = [
    {"target": "Q_VW_TC", "invalid_value": "19000000", "field_hint": None},
    {"target": "S_Contact_Info", "invalid_value": "not-a-phone-xyz", "field_hint": "phone"},
]


def run_probes():
    results = []
    for spec in PROBES:
        print(f"Probing {spec['target']} with {spec['invalid_value']!r} ...")
        r = probe(spec["target"], spec["invalid_value"], spec.get("field_hint"))
        status = ("BUG" if r.get("issue") else "blocked (ok)" if r.get("blocked")
                  else r.get("error", "not reached"))
        print(f"  -> reached={r['reached']} outcome={status}")
        results.append(r)
    with open("output/probe_results.json", "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    return results


if __name__ == "__main__":
    run_probes()
