"""
FR2 — Playwright survey runner.

Walks Survey 2 as one qualifying respondent, page by page. Each Decipher question div
carries its QID in the element id (#question_<QID>), so we align live pages to the spec
deterministically. For every question we record an *observation* (QID, live text, live
option labels in DOM order, input type) and a screenshot, then answer with a qualifying
choice and Continue. Terminations / dead pages are detected and reported, never silently
passed.

The qualifying path is curated for the screener (some terminate rules are instruction-level,
not per-option, so "pick any non-terminate option" is not safe) and deliberately routes
through the Procurement branch so we reach S_ProcType (a target bug). Downstream questions
use a generic qualifying heuristic.
"""
import json
import os
import re
import time

from playwright.sync_api import sync_playwright

URL = os.environ.get("QA_SURVEY_URL",
                     "https://singapore.decipherinc.com/survey/selfserve/9c7/2510702")
SHOTS = "output/shots"

# Curated qualifying answers by QID. Value is a substring matched against option labels
# (single/multi) or a literal to type (numeric/text). Routes via Procurement to hit S_ProcType.
QUALIFY = {
    "S_Geography": "United States",
    "S_FullTimeEmployment": "full-time",
    "S_DecisionMakingAuthority": "final decision maker",
    "S_JobSeniority": "C-Level",
    "S_CoSize": "5,000 to 9,999",
    "S_CompanyInd": "Information Technology",
    "S_Role": "Procurement",          # -> triggers S_ProcType
    "S_ProcType": "IT Software",       # qualifies past the ProcType terminate
    "S_Assets": "Laptop computers",
    "S_Usage": "Cyber Asset Attack Surface",
    "S_Intend": "Cyber Asset Attack Surface",
}

TERMINATION_MARKERS = (
    "does not qualify", "do not qualify", "no longer qualify", "screened out",
    "thank you for your interest", "you have been terminated", "unfortunately",
)


def qid_of(qdiv):
    el_id = qdiv.get_attribute("id") or ""
    return el_id.replace("question_", "", 1) if el_id.startswith("question_") else el_id


def radio_group_names(qdiv):
    """Distinct `name` attributes among radio inputs. Single-select shares one name across
    all options; a grid has one group (name) per row, so >1 distinct name => grid."""
    names = []
    for inp in qdiv.locator("input[type=radio]").all():
        nm = inp.get_attribute("name")
        if nm and nm not in names:
            names.append(nm)
    return names


def question_type(qdiv, page):
    cls = qdiv.get_attribute("class") or ""
    rnames = radio_group_names(qdiv)
    if len(rnames) > 1:
        return "grid"
    if qdiv.locator("input[type=checkbox]").count():
        return "multi"
    if rnames:
        return "single"
    if qdiv.locator("select").count():
        return "select"
    if qdiv.locator("textarea").count():
        return "text"
    if qdiv.locator("input[type=number], input[type=text]").count():
        return "numeric" if "number" in cls or qdiv.locator("input[type=number]").count() else "text"
    return "unknown"


def read_options(qdiv):
    """Live option labels in DOM order (radio/checkbox questions)."""
    labels = []
    for lab in qdiv.locator(".element label").all():
        t = (lab.inner_text() or "").strip()
        if t:
            labels.append(t)
    return labels


def observe(page):
    """Return list of observations for all question divs on the current page."""
    obs = []
    for qdiv in page.locator("div.question").all():
        qid = qid_of(qdiv)
        text_el = qdiv.locator(".question-text")
        text = (text_el.inner_text().strip() if text_el.count() else "")
        obs.append({
            "qid": qid,
            "text": text,
            "type": question_type(qdiv, page),
            "options": read_options(qdiv),
        })
    return obs


def answer_question(qdiv, qid, qtype, qualify=None):
    """Answer with a qualifying choice. Returns a short string describing what we did.
    `qualify` is the profile's answer map (defaults to the module QUALIFY = profile A)."""
    qualify = QUALIFY if qualify is None else qualify
    want = qualify.get(qid)
    if qtype == "grid":
        # One cell per row. Decipher hides the real <input> (fir-hidden, off-viewport) behind
        # an SVG, so .check() fails; set the first radio of each row-group via JS and fire the
        # change/click events Decipher listens for. First column is qualifying here
        # (S_Familiar CAASM row must not be "Not familiar at all"; col 1 "Very familiar" passes).
        n = qdiv.evaluate(
            """(el) => {
                const seen = new Set(); let count = 0;
                el.querySelectorAll('input[type=radio]').forEach(i => {
                    if (!seen.has(i.name)) {
                        seen.add(i.name); count++;
                        i.checked = true;
                        i.dispatchEvent(new Event('click',  {bubbles: true}));
                        i.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                });
                return count;
            }"""
        )
        return f"grid: selected first column for {n} rows (via JS)"
    if qtype in ("single", "multi"):
        # Match option labels by text (use text_content so hidden scale labels still read),
        # then SELECT via JS + dispatch events — Decipher hides many inputs (scales, grids)
        # behind SVGs so clicking the visible label fails.
        labels = qdiv.locator(".element label")
        n = labels.count()
        texts = [(labels.nth(i).text_content() or "").strip() for i in range(n)]
        target = None
        for i, t in enumerate(texts):
            if want and want.lower() in t.lower():
                target = i
                break
        if target is None:
            for i, t in enumerate(texts):
                if "none of the above" not in t.lower():
                    target = i
                    break
            target = 0 if target is None else target
        sel = "input[type=checkbox]" if qtype == "multi" else "input[type=radio]"
        qdiv.evaluate(
            """(el, {idx, sel}) => {
                const inputs = el.querySelectorAll(sel);
                const t = inputs[idx];
                if (t) { t.checked = true;
                    t.dispatchEvent(new Event('click',  {bubbles: true}));
                    t.dispatchEvent(new Event('change', {bubbles: true})); }
            }""",
            {"idx": target, "sel": sel},
        )
        return f"selected option '{texts[target] if target < len(texts) else target}'"
    if qtype in ("numeric", "text"):
        val = want if want else ("100" if qtype == "numeric" else "test")
        # Van Westendorp price ladder must be strictly increasing: TC < RP < EX < TE.
        for key, v in (("Q_VW_TC", "1000"), ("Q_VW_RP", "2000"),
                       ("Q_VW_EX", "3000"), ("Q_VW_TE", "4000")):
            if qid.startswith(key):
                val = v
        inps = qdiv.locator("input[type=number], input[type=text], textarea")
        k = inps.count()
        for i in range(k):
            inps.nth(i).fill(str(val))
        return f"filled '{val}' into {k} field(s)"
    if qtype == "select":
        sel = qdiv.locator("select").first
        sel.select_option(index=1)
        return "selected index 1"
    return "no-op (unknown type)"


def run(qualify=None, out_path="output/run_observations.json", shots=SHOTS,
        profile_name="A", max_pages=120, headless=True):
    """Walk one qualifying path (one profile). Writes observations to out_path (each tagged
    with the profile) and screenshots to `shots`."""
    qualify = QUALIFY if qualify is None else qualify
    os.makedirs(shots, exist_ok=True)
    observations = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(URL, wait_until="networkidle", timeout=60000)

        prev_qids, stall = None, 0
        for step in range(max_pages):
            page.wait_for_timeout(400)
            body = page.locator("body").inner_text().lower()
            if any(m in body for m in TERMINATION_MARKERS):
                shot = f"{shots}/step{step:02d}_TERMINATED.png"
                page.screenshot(path=shot, full_page=True)
                observations.append({"step": step, "event": "TERMINATED",
                                     "profile": profile_name, "screenshot": shot})
                print(f"[{step}] TERMINATED")
                break

            page_obs = observe(page)
            if not page_obs:
                if page.locator("#btn_continue").count():
                    print(f"[{step}] intro/no-question page -> continue")
                    page.locator("#btn_continue").first.click()
                    page.wait_for_load_state("networkidle", timeout=60000)
                    continue
                print(f"[{step}] no questions and no continue -> stop")
                break

            qids = [o["qid"] for o in page_obs]
            if qids == prev_qids:
                stall += 1
                if stall >= 2:
                    shot = f"{shots}/step{step:02d}_STALLED_{'_'.join(qids)[:30]}.png"
                    page.screenshot(path=shot, full_page=True)
                    observations.append({"step": step, "event": "STALLED", "qids": qids,
                                         "profile": profile_name, "screenshot": shot})
                    print(f"[{step}] STALLED on {qids} (page not advancing) -> stop")
                    break
            else:
                stall = 0
            prev_qids = qids

            shot = f"{shots}/step{step:02d}_{'_'.join(qids)[:40]}.png"
            page.screenshot(path=shot, full_page=True)

            actions = []
            for o in page_obs:
                qdiv = page.locator(f"#question_{o['qid']}")
                try:
                    act = answer_question(qdiv, o["qid"], o["type"], qualify)
                except Exception as e:
                    act = f"ERROR: {e}"
                actions.append(act)
                o["step"] = step
                o["profile"] = profile_name
                o["screenshot"] = shot
                o["action"] = act
                observations.append(o)
            print(f"[{step}] {qids} -> {actions}")

            if not page.locator("#btn_continue").count():
                print(f"[{step}] no continue button -> stop")
                break
            page.locator("#btn_continue").first.click()
            page.wait_for_load_state("networkidle", timeout=60000)

        browser.close()

    with open(out_path, "w") as f:
        json.dump(observations, f, indent=2, ensure_ascii=False)
    seen = [o.get("qid") for o in observations if o.get("qid")]
    print(f"\n[profile {profile_name}] Walked {len(seen)} questions -> {out_path}")
    return observations


if __name__ == "__main__":
    import sys
    prof = sys.argv[1] if len(sys.argv) > 1 else "A"
    from profiles import PROFILES
    p = PROFILES[prof]
    run(qualify=p["qualify"], out_path=p["out"], shots=p["shots"], profile_name=prof)
