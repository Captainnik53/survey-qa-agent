"""
Routing checks (Logical tier): #1 termination and #2 base/show-skip conditions.

Routing bugs can't be seen on a single page — you must DRIVE a specific answer path and check
the outcome. Each check derives a test case from the spec, walks the qualifying path to the
decision point, applies the decision, and asserts the resulting flow:

  #1 Termination      — at a [terminate] option, select it and assert the survey SCREENS OUT.
  #2 Base condition   — a gated question must APPEAR when its base is satisfied and be ABSENT
                        when it isn't (e.g. S_ProcType shows only if S_Role = Procurement).

A Decipher screen-out = no question div + no continue button (body: "Survey Completed …").
Findings are deterministic (ended vs advanced / present vs absent) -> High confidence.
"""
import json
import os
import re

from playwright.sync_api import sync_playwright

import runner
from checks import base_qid

SHOTS = "output/routing"
END_MARKERS = ("survey completed", "thank you for taking", "do not qualify",
               "does not qualify", "screened out", "no longer qualify")


def is_ended(page):
    """True if the survey has ended (terminated or completed) — no question and no continue."""
    if page.locator("div.question").count() == 0 and page.locator("#btn_continue").count() == 0:
        return True
    body = page.locator("body").inner_text().lower()
    return any(m in body for m in END_MARKERS)


def _select_option(qdiv, label, qtype):
    labels = qdiv.locator(".element label")
    texts = [(labels.nth(i).text_content() or "").strip() for i in range(labels.count())]
    idx = next((i for i, t in enumerate(texts) if label.lower() in t.lower()), None)
    if idx is None:
        return False
    sel = "input[type=checkbox]" if qtype == "multi" else "input[type=radio]"
    qdiv.evaluate(
        """(el, {idx, sel}) => {
            const t = el.querySelectorAll(sel)[idx];
            if (t) { t.checked = true;
                t.dispatchEvent(new Event('click',  {bubbles: true}));
                t.dispatchEvent(new Event('change', {bubbles: true})); }
        }""", {"idx": idx, "sel": sel})
    return True


def _continue(page):
    if page.locator("#btn_continue").count():
        page.locator("#btn_continue").first.click()
        page.wait_for_load_state("networkidle", timeout=60000)
        page.wait_for_timeout(400)


def _walk_to(page, target_base, qualify, max_pages=40):
    """Answer qualifying until the target question appears; return its observation or None."""
    for _ in range(max_pages):
        page.wait_for_timeout(250)
        if is_ended(page):
            return None
        page_obs = runner.observe(page)
        for o in page_obs:
            if base_qid(o["qid"]) == target_base:
                return o
        if not page_obs:
            if page.locator("#btn_continue").count():
                _continue(page)
                continue
            return None
        for o in page_obs:
            try:
                runner.answer_question(page.locator(f"#question_{o['qid']}"), o["qid"], o["type"], qualify)
            except Exception:
                pass
        if not page.locator("#btn_continue").count():
            return None
        _continue(page)
    return None


def _shot(name):
    os.makedirs(SHOTS, exist_ok=True)
    return f"{SHOTS}/{re.sub(r'[^A-Za-z0-9]+', '_', name)[:40]}.png"


def test_termination(term_qid, term_label, qualify=None, headless=True):
    qualify = qualify or runner.QUALIFY
    with sync_playwright() as p:
        b = p.chromium.launch(headless=headless)
        pg = b.new_page(viewport={"width": 1280, "height": 1300})
        pg.goto(runner.URL, wait_until="networkidle", timeout=60000)
        o = _walk_to(pg, term_qid, qualify)
        if not o:
            b.close()
            return {"qid": term_qid, "option": term_label, "reached": False}
        qdiv = pg.locator(f"#question_{o['qid']}")
        _select_option(qdiv, term_label, o["type"])
        _continue(pg)
        shot = _shot(f"term_{term_qid}_{term_label}")
        pg.screenshot(path=shot, full_page=True)
        ended = is_ended(pg)
        b.close()
    issue = None
    if not ended:
        issue = {
            "qid": term_qid, "check": "termination", "issue_type": "Logical",
            "explanation": f"Selecting '{term_label}' at {term_qid} should terminate the "
                           f"survey (it is a [terminate] option), but the survey advanced instead.",
            "expected": "survey screens out (terminates)",
            "observed": "survey continued to the next question",
            "spec_clause": f"{term_qid}: option '{term_label}' is [terminate]",
            "screenshot": shot, "confidence": "High",
            "confidence_factors": {"spec_parse": "high ([terminate] flag parsed)",
                                   "observation": "high (ended-vs-advanced directly observed)",
                                   "determinism": "high (active path test)"}}
    return {"qid": term_qid, "option": term_label, "reached": True,
            "terminated": ended, "issue": issue, "screenshot": shot}


def test_base_condition(gated_qid, source_qid, satisfy, violate, qualify=None, headless=True):
    """Assert gated_qid APPEARS when source=satisfy and is ABSENT when source=violate."""
    qualify = dict(qualify or runner.QUALIFY)
    issues = []
    for kind, ans, expect_present in (("satisfy", satisfy, True), ("violate", violate, False)):
        q = dict(qualify)
        q[source_qid] = ans
        with sync_playwright() as p:
            b = p.chromium.launch(headless=headless)
            pg = b.new_page(viewport={"width": 1280, "height": 1300})
            pg.goto(runner.URL, wait_until="networkidle", timeout=60000)
            src = _walk_to(pg, source_qid, q)
            present = None
            shot = _shot(f"base_{gated_qid}_{kind}")
            if src:
                _select_option(pg.locator(f"#question_{src['qid']}"), ans, src["type"])
                _continue(pg)
                # look a couple pages ahead for the gated question
                present = False
                for _ in range(3):
                    if any(base_qid(o["qid"]) == gated_qid for o in runner.observe(pg)):
                        present = True
                        break
                    if is_ended(pg) or not pg.locator("#btn_continue").count():
                        break
                    # answer + advance to check subsequent page
                    for o in runner.observe(pg):
                        try:
                            runner.answer_question(pg.locator(f"#question_{o['qid']}"), o["qid"], o["type"], q)
                        except Exception:
                            pass
                    _continue(pg)
                pg.screenshot(path=shot, full_page=True)
            b.close()
        if src and present is not None and present != expect_present:
            issues.append({
                "qid": gated_qid, "check": "base_condition", "issue_type": "Logical",
                "explanation": (f"{gated_qid} should {'appear' if expect_present else 'be skipped'} "
                                f"when {source_qid}='{ans}', but it was "
                                f"{'absent' if expect_present else 'shown'}."),
                "expected": f"{gated_qid} {'shown' if expect_present else 'hidden'} for {source_qid}='{ans}'",
                "observed": f"{gated_qid} {'not found' if expect_present else 'found'}",
                "spec_clause": f"{gated_qid}: base condition on {source_qid}",
                "screenshot": shot, "confidence": "High",
                "confidence_factors": {"spec_parse": "high (ASK-IF parsed)",
                                       "observation": "high (present-vs-absent observed)",
                                       "determinism": "high (active path test)"}})
    return issues


def auto_base_conditions(spec_list):
    """Auto-generate #2 test cases from every question's ASK-IF condition."""
    from spec_logic import interpret_ask, pick_violate
    spec_by_id = {q["id"]: q for q in spec_list}
    bcs = []
    for q in spec_list:
        cond = q.get("ask_condition")
        if not cond:
            continue
        parsed = interpret_ask(cond, spec_by_id)
        if not parsed:
            print(f"  [base] {q['id']}: condition {cond!r} too complex to interpret -> skipped")
            continue
        source_qid, satisfy = parsed
        violate = pick_violate(spec_by_id[source_qid], satisfy)
        if not violate:
            continue
        bcs.append({"gated": q["id"], "source": source_qid,
                    "satisfy": satisfy, "violate": violate})
    return bcs


def run_routing_checks(spec_path="spec/survey2_spec.json", one_per_question=False, headless=True):
    spec = json.load(open(spec_path))["questions"]
    issues, results = [], []
    # #1 termination — every [terminate] option (or one representative per question)
    for q in spec:
        terms = [o["label"] for o in q.get("options", []) if o.get("terminate")]
        for lbl in (terms[:1] if one_per_question else terms):
            r = test_termination(q["id"], lbl, headless=headless)
            results.append(r)
            if r.get("issue"):
                issues.append(r["issue"])
            print(f"  [term] {q['id']} / '{lbl[:28]}': reached={r.get('reached')} "
                  f"terminated={r.get('terminated')}")
    # #2 base conditions — auto-generated from the spec's ASK-IF rules
    base_results = []
    for bc in auto_base_conditions(spec):
        bi = test_base_condition(bc["gated"], bc["source"], bc["satisfy"], bc["violate"],
                                 headless=headless)
        issues += bi
        base_results.append({**bc, "ok": not bi})
        print(f"  [base] {bc['gated']} (if {bc['source']}={bc['satisfy']}, else {bc['violate']}): "
              f"{'OK' if not bi else 'ISSUE'}")
    # cache for the aggregator/UI (routing is live/slow, so we don't re-run it every aggregate)
    term_results = [{"qid": r["qid"], "option": r["option"], "reached": r.get("reached"),
                     "terminated": r.get("terminated"), "ok": r.get("terminated") is True,
                     "screenshot": r.get("screenshot")} for r in results]
    summary = {"n_termination_tests": len(results),
               "n_reached": sum(1 for r in results if r.get("reached")),
               "n_issues": len(issues), "issues": issues,
               "termination_results": term_results, "base_results": base_results}
    with open("output/routing_issues.json", "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return issues, results


if __name__ == "__main__":
    import sys
    quick = "quick" in sys.argv        # one terminate option per question (faster)
    issues, results = run_routing_checks(one_per_question=quick)
    print(f"\nRouting: {len(results)} termination tests, {len(issues)} issue(s)")
    for it in issues:
        print(f"  [{it['confidence']}] {it['qid']} ({it['check']}): {it['explanation'][:90]}")
