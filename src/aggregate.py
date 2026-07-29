"""
FR5-7 — Aggregate, triage, score, report.

Runs the in-scope checks (Visual + Textual), applies confidence-based triage
(High -> auto-flagged; Med/Low -> human-review queue), scores against the ground-truth
bug list, and writes a unified report (JSON + self-contained HTML with screenshots).

Logical checks (probes / carry-forward) are deferred per the current scope but plug in here
the same way when re-enabled.
"""
import base64
import json
import os

from visual_checks import run_visual_checks
from textual_checks import run_textual_checks

# Ground truth for the current (Visual + Textual) scope. Full 5-bug list kept for reference.
GROUND_TRUTH = [
    {"qid": "S_ProcType", "type": "Visual", "desc": "'Other' not anchored at the end"},
    {"qid": "Q_Aware", "type": "Textual", "desc": "struck 'if any' still shown"},
    {"qid": "S_Role", "type": "Textual", "desc": "option 'Market' (spec) shown as 'Marketing' (live)"},
]


def triage(issue):
    """Three buckets:
      flagged     - deterministic High-confidence checks (trusted survey bugs, auto-flag)
      spec_issue  - LLM thinks the divergence is a SPEC gap (incomplete/ambiguous), optional
      review_queue- everything else (generative findings, Med/Low) a human should confirm
    The LLM's self-reported confidence is kept on the issue but never lets a generative
    finding into the trusted auto-flag tier."""
    if issue.get("likely_cause") == "spec":
        return "spec_issue"
    if issue.get("source") == "open-world":
        return "review_queue"
    return "flagged" if issue["confidence"] == "High" else "review_queue"


def aggregate(spec_path="spec/survey2_spec.json", obs_path="output/run_observations.json",
              open_world=True):
    # typed detectors (closed-world, high precision)
    issues = []
    issues += run_visual_checks(spec_path, obs_path)
    issues += run_textual_checks(spec_path, obs_path)

    # open-world discovery pass (generative, self-reported confidence) — merged into the same
    # list, deduped so it doesn't re-report a QID the typed detectors already caught.
    if open_world and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from openworld import find_discrepancies
            typed_qids = {i["qid"] for i in issues}
            for it in find_discrepancies(spec_path, obs_path):
                if it["qid"] not in typed_qids:
                    issues.append(it)
        except Exception as e:
            print(f"[open-world skipped: {e}]")

    # routing checks (Logical) are live/slow, so we read their cached results if present
    if os.path.exists("output/routing_issues.json"):
        issues += json.load(open("output/routing_issues.json")).get("issues", [])

    for it in issues:
        it["bucket"] = triage(it)

    # score vs ground truth (a GT bug counts as caught if flagged OR queued)
    caught, missed = [], []
    for gt in GROUND_TRUTH:
        hit = next((i for i in issues if i["qid"] == gt["qid"]), None)
        (caught if hit else missed).append({**gt, "confidence": hit["confidence"] if hit else None})
    flagged = [i for i in issues if i["bucket"] == "flagged"]
    n_spec_issue = len([i for i in issues if i["bucket"] == "spec_issue"])
    # precision on auto-flagged = flagged issues that are in ground truth
    gt_ids = {g["qid"] for g in GROUND_TRUTH}
    tp = [i for i in flagged if i["qid"] in gt_ids]
    precision = (len(tp) / len(flagged)) if flagged else None

    summary = {
        "n_issues": len(issues),
        "n_flagged_high": len(flagged),
        "n_spec_issue": n_spec_issue,
        "n_review_queue": len(issues) - len(flagged) - n_spec_issue,
        "recall": f"{100 * len(caught) / max(len(GROUND_TRUTH), 1):.0f}%",
        "precision_flagged": (f"{precision:.0%}" if precision is not None else "n/a"),
        "caught": caught, "missed": missed,
    }
    # fold in question coverage from the multi-path run, if present
    if os.path.exists("output/coverage.json"):
        cov = json.load(open("output/coverage.json"))
        summary["coverage"] = {
            "pct": cov["coverage_pct"], "covered": cov["n_covered"],
            "target": cov["n_target"], "paths": cov["n_paths"],
            "uncovered": cov["uncovered"],
            "selected_profiles": cov.get("selected_profiles", []),
            "candidate_pool": cov.get("candidate_pool", []),
            "per_candidate_reach": cov.get("per_candidate_reach", {}),
        }
    report = {"summary": summary, "issues": issues}
    with open("output/report.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    write_html(report)
    return report


def _img_tag(path):
    if path and os.path.exists(path):
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        return f'<img src="data:image/png;base64,{b64}" style="max-width:640px;border:1px solid #ccc"/>'
    return "<em>no screenshot</em>"


def write_html(report):
    s = report["summary"]
    rows = []
    order = {"High": 0, "Med": 1, "Low": 2}
    for it in sorted(report["issues"], key=lambda x: order.get(x["confidence"], 9)):
        badge = {"High": "#1a7f37", "Med": "#9a6700", "Low": "#8250df"}.get(it["confidence"], "#555")
        bucket = "AUTO-FLAGGED" if it["bucket"] == "flagged" else "REVIEW QUEUE"
        rows.append(f"""
        <div style="border:1px solid #ddd;border-radius:8px;padding:14px;margin:12px 0">
          <div>
            <span style="background:{badge};color:#fff;padding:2px 8px;border-radius:10px;font-size:12px">
              {it['confidence']}</span>
            <span style="background:#eee;padding:2px 8px;border-radius:10px;font-size:12px;margin-left:6px">{bucket}</span>
            <b style="margin-left:8px">{it['qid']}</b>
            <span style="color:#666">· {it['issue_type']} · {it['check']}</span>
          </div>
          <p>{it['explanation']}</p>
          <div style="font-size:13px;color:#444">
            <b>Expected:</b> {str(it.get('expected'))[:300]}<br>
            <b>Observed:</b> {str(it.get('observed'))[:300]}<br>
            <b>Spec:</b> {it.get('spec_clause','')}
          </div>
          <div style="margin-top:8px">{_img_tag(it.get('screenshot'))}</div>
        </div>""")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Survey QA Report</title>
    <style>body{{font-family:-apple-system,Segoe UI,Arial;margin:32px;max-width:820px}}</style></head>
    <body>
    <h1>Survey QA Report — Survey 2</h1>
    <div style="background:#f6f8fa;border-radius:8px;padding:14px">
      <b>Issues:</b> {s['n_issues']} &nbsp;·&nbsp; <b>Auto-flagged (High):</b> {s['n_flagged_high']}
      &nbsp;·&nbsp; <b>Review queue:</b> {s['n_review_queue']}<br>
      <b>Recall (ground truth):</b> {s['recall']} &nbsp;·&nbsp;
      <b>Precision (auto-flagged):</b> {s['precision_flagged']}<br>
      <b>Caught:</b> {', '.join(c['qid'] for c in s['caught']) or '—'} &nbsp;·&nbsp;
      <b>Missed:</b> {', '.join(c['qid'] for c in s['missed']) or '—'}
    </div>
    {''.join(rows)}
    </body></html>"""
    with open("output/report.html", "w") as f:
        f.write(html)


if __name__ == "__main__":
    rep = aggregate()
    s = rep["summary"]
    print(json.dumps(s, indent=2, ensure_ascii=False))
    print("\n-> output/report.json and output/report.html")
