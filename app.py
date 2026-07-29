"""
Survey QA Agent — Streamlit UI.

Takes a questionnaire (.docx) + a live survey URL, runs the QA pipeline
(parse spec -> walk survey -> visual/textual checks -> triage), and shows evidence-backed
issues grouped by confidence tier, each with its screenshot and accept/reject controls.

Run:  streamlit run app.py
"""
import json
import os
import re
import subprocess
import sys
import time

import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)
os.chdir(ROOT)

DEFAULT_URL = "https://singapore.decipherinc.com/survey/selfserve/9c7/2510702"
# bundled in the repo so the default works in a container; falls back to a local path in dev
DEFAULT_DOCX = os.path.join(ROOT, "spec", "Survey_2.docx")
if not os.path.exists(DEFAULT_DOCX):
    DEFAULT_DOCX = "/home/nik/Downloads/Survey_2.docx"

st.set_page_config(page_title="Survey QA Agent", layout="wide")


def _password_gate():
    """Optional password wall for public deploys. Enabled only when APP_PASSWORD is set
    (so local/dev stays open). Guards the app since 'Run live' hits the real survey."""
    pw = os.environ.get("APP_PASSWORD")
    if not pw or st.session_state.get("authed"):
        return
    st.title("🔒 Survey QA Agent")
    entered = st.text_input("Password", type="password")
    if entered and entered == pw:
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Incorrect password")
    st.stop()


_password_gate()

st.title("🔎 Survey QA Agent")
st.caption("Reads a questionnaire, walks the live survey, and flags evidence-backed bugs "
           "(question ID · explanation · screenshot) with a confidence-based human-in-the-loop.")

c1, c2 = st.columns([3, 2])
url = c1.text_input("Survey URL", value=DEFAULT_URL)
spec_file = c2.file_uploader("Questionnaire (.docx)", type=["docx"])

c3, c4 = st.columns(2)
mode = c3.radio(
    "Run mode",
    ["⚡ Load last run (instant demo)", "🌐 Run live (walks the survey, ~5–8 min)"],
)
coverage_mode = c4.radio(
    "Path coverage",
    ["Single path", "Multi-path (auto-selects minimum profiles)"],
    help="Multi-path walks a candidate pool of profiles, then greedily selects the MINIMUM "
         "number that maximizes question coverage — the count is derived from the survey, not "
         "fixed. The result shows 'used N of M profiles'.",
)
include_routing = st.checkbox(
    "Also run routing checks with the full QA run (live, ~4 min)",
    value=False,
    help="Drives the survey down targeted answer paths: selects each [terminate] option and "
         "asserts screen-out (#1), and checks gated questions appear/skip per their ASK-IF "
         "condition (#2). Live-only and slower, so it's opt-in.",
)
bcol1, bcol2 = st.columns([1, 2])
go = bcol1.button("Run QA", type="primary")
go_routing = bcol2.button("🧭 Run routing checks only (live)")


def _walk_live(url, names):
    """Walk each profile live (subprocess), streaming a log + progress bar per profile,
    with an overall 'profile X of N' indicator."""
    env = dict(os.environ, QA_SURVEY_URL=url)
    n = len(names)
    overall = st.progress(0, text=f"0 / {n} profiles walked")
    for idx, name in enumerate(names):
        st.caption(f"Profile {name} ({idx + 1} of {n}): walking the live survey…")
        log_box = st.empty()
        bar = st.progress(5, text=f"Profile {name}: walking…")
        proc = subprocess.Popen([sys.executable, "-u", "src/runner.py", name], env=env,
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        lines = []
        for line in proc.stdout:
            lines.append(line.rstrip())
            m = re.match(r"\[(\d+)\]", line)
            if m:
                bar.progress(min(5 + int(m.group(1)), 95),
                             text=f"Profile {name}: {line.strip()[:60]}")
            log_box.code("\n".join(lines[-16:]))
        proc.wait()
        bar.progress(100, text=f"Profile {name}: done")
        overall.progress(int(100 * (idx + 1) / n), text=f"{idx + 1} / {n} profiles walked")


def _run_routing_live(url, docx_path):
    """Parse spec, then run the routing checks as a streamed subprocess; return the report."""
    subprocess.run([sys.executable, "src/parse_spec.py", docx_path], cwd=ROOT)
    env = dict(os.environ, QA_SURVEY_URL=url)
    log_box = st.empty()
    bar = st.progress(0, text="Running routing checks…")
    proc = subprocess.Popen([sys.executable, "-u", "src/routing.py", "quick"], env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines, done = [], 0
    for line in proc.stdout:
        lines.append(line.rstrip())
        if line.lstrip().startswith(("[term]", "[base]")):
            done += 1
            bar.progress(min(done * 8, 95), text=f"Routing: {line.strip()[:70]}")
        log_box.code("\n".join(lines[-16:]))
    proc.wait()
    bar.progress(100, text="Routing done")
    return json.load(open("output/routing_issues.json"))


def render_routing(routing):
    st.subheader("🧭 Routing checks")
    n_bugs = routing.get("n_issues", 0)
    c = st.columns(4)
    c[0].metric("Terminate tests", routing.get("n_termination_tests", 0))
    c[1].metric("Reached", routing.get("n_reached", 0))
    c[2].metric("Routing bugs", n_bugs)
    c[3].metric("Runtime", f"{routing.get('runtime_min', '—')} min")
    if n_bugs == 0:
        st.success("No routing bugs — all terminations screen out and gated questions route correctly.")
    tr = routing.get("termination_results", [])
    if tr:
        st.markdown("**#1 Termination** — each `[terminate]` option must screen the respondent out:")
        st.dataframe([{"Question": r["qid"], "Terminate option": r["option"],
                       "Reached": r["reached"], "Terminated": r["terminated"],
                       "Result": "✅ OK" if r["ok"] else "❌ BUG"} for r in tr],
                     use_container_width=True, hide_index=True)
    br = routing.get("base_results", [])
    if br:
        st.markdown("**#2 Gated questions** — must appear iff their ASK-IF base is satisfied:")
        st.dataframe([{"Gated question": b["gated"], "Base": f"{b['source']} = {b['satisfy']}",
                       "Result": "✅ OK" if b["ok"] else "❌ BUG"} for b in br],
                     use_container_width=True, hide_index=True)
    for it in routing.get("issues", []):
        with st.expander(f"❌ {it['qid']} · {it['check']} · {it['confidence']}", expanded=True):
            st.markdown(f"**Issue:** {it['explanation']}")
            st.markdown(f"**Expected:** {it.get('expected')}  \n**Observed:** {it.get('observed')}")
            shot = it.get("screenshot")
            if shot and os.path.exists(shot):
                st.image(shot, width=680)


def run_pipeline(url, docx_path, live, multipath, routing=False):
    # 1) parse the questionnaire -> spec JSON
    subprocess.run([sys.executable, "src/parse_spec.py", docx_path], cwd=ROOT)
    import importlib
    import runner
    runner.URL = url

    # 2) build the observations to check on
    if multipath:
        # MERGED: generate candidate paths from the spec -> greedy set-cover picks the minimum
        # -> walk (only) those -> union. Reach is measured (cached walks reused).
        import coverage_greedy as cg
        importlib.reload(cg)
        status = st.empty()

        def _on_walk(name, i):
            status.info(f"Walking candidate #{i}: {name}")

        with st.spinner("Multi-path: greedy set-cover over spec-generated candidate paths…"):
            cg.build_and_select(live=live, reuse=True, on_walk=_on_walk if live else None)
    else:
        import multipath as mp
        importlib.reload(mp)
        if live:
            _walk_live(url, ["A"])
            st.success("Walk complete — running checks…")
        mp.run_profiles(["A"], reuse=True)
    # 2b) routing checks (live path-driving; writes routing_issues.json read by the aggregator)
    if routing:
        with st.spinner("Running routing checks (terminations + gated questions, live)…"):
            import routing as rt
            importlib.reload(rt)
            rt.run_routing_checks(one_per_question=True)
    elif os.path.exists("output/routing_issues.json") and not multipath:
        pass  # keep last routing results in view unless a fresh run overwrites them
    # 3) checks + triage + score  (this phase includes the LLM discovery pass, ~30–90s)
    with st.spinner("Running checks (incl. LLM discovery, ~30–90s)…"):
        import aggregate
        importlib.reload(aggregate)
        return aggregate.aggregate()


if go:
    docx_path = DEFAULT_DOCX
    if spec_file is not None:
        docx_path = os.path.join(ROOT, "spec", "uploaded.docx")
        with open(docx_path, "wb") as f:
            f.write(spec_file.getbuffer())
    live = mode.startswith("🌐")
    multipath = coverage_mode.startswith("Multi")
    t0 = time.time()
    try:
        rep = run_pipeline(url, docx_path, live, multipath, include_routing)  # manages its own spinners
        rep["summary"]["runtime_min"] = round((time.time() - t0) / 60, 1)
        with open("output/report.json", "w") as f:           # persist so reloads show it
            json.dump(rep, f, indent=2, ensure_ascii=False)
        st.session_state.report = rep
    except Exception as e:
        st.error(f"Run failed: {e}")

if go_routing:
    docx_path = DEFAULT_DOCX
    if spec_file is not None:
        docx_path = os.path.join(ROOT, "spec", "uploaded.docx")
        with open(docx_path, "wb") as f:
            f.write(spec_file.getbuffer())
    t0 = time.time()
    try:
        routing_rep = _run_routing_live(url, docx_path)
        routing_rep["runtime_min"] = round((time.time() - t0) / 60, 1)
        st.session_state.routing = routing_rep
    except Exception as e:
        st.error(f"Routing run failed: {e}")

# routing report (from this session's run, else the last cached routing run)
routing_data = st.session_state.get("routing")
if not routing_data and os.path.exists("output/routing_issues.json"):
    routing_data = json.load(open("output/routing_issues.json"))
if routing_data:
    render_routing(routing_data)
    st.divider()

report = st.session_state.get("report")
if not report and os.path.exists("output/report.json"):
    report = json.load(open("output/report.json"))

if report:
    s = report["summary"]
    st.subheader("Summary")
    m = st.columns(7)
    m[0].metric("Issues", s["n_issues"])
    m[1].metric("Auto-flagged (High)", s["n_flagged_high"])
    m[2].metric("Review queue", s["n_review_queue"])
    m[3].metric("Spec issues?", s.get("n_spec_issue", 0))
    m[4].metric("Recall", s["recall"])
    m[5].metric("Precision", s["precision_flagged"])
    rt = s.get("runtime_min")
    m[6].metric("Runtime", f"{rt} min" if rt is not None else "—")

    cov = s.get("coverage")
    if cov:
        sel = cov.get("selected_profiles", [])
        pool = cov.get("candidate_pool", [])
        measured = len(cov.get("per_candidate_reach", {}))
        cc = st.columns([1, 3])
        cc[0].metric("Paths to run", len(sel))
        cc[1].caption(
            f"📐 Greedy set-cover selected **{len(sel)} path(s)** to run "
            f"(from **{len(pool)}** spec-generated candidates, {measured} measured) → "
            f"**{cov['pct']}%** coverage ({cov['covered']}/{cov['target']} questions).  \n"
            f"Paths: {', '.join(sel) or '—'}")
        if cov.get("uncovered"):
            with st.expander(f"Uncovered questions ({len(cov['uncovered'])})"):
                st.write(cov["uncovered"])

    tabs = st.tabs([f"🚩 Auto-flagged ({s['n_flagged_high']})",
                    f"🕵️ Review queue ({s['n_review_queue']})",
                    f"🧩 Could be a spec issue? ({s.get('n_spec_issue', 0)})"])
    buckets = {"flagged": [], "review_queue": [], "spec_issue": []}
    for it in report["issues"]:
        buckets.get(it["bucket"], buckets["review_queue"]).append(it)

    for tab, key in zip(tabs, ["flagged", "review_queue", "spec_issue"]):
        with tab:
            if key == "spec_issue":
                st.caption("The LLM thinks these diffs may be because the *questionnaire* is "
                           "incomplete/ambiguous — not survey bugs. Optional to review.")
            if not buckets[key]:
                st.info("Nothing here.")
            for i, it in enumerate(buckets[key]):
                color = {"Visual": "🟦", "Textual": "🟨", "Logical": "🟥",
                         "Open-world": "🟪"}.get(it["issue_type"], "⬜")
                with st.expander(
                    f"{color} **{it['qid']}** · {it['issue_type']} · {it['check']} "
                    f"· confidence: {it['confidence']}", expanded=(key == "flagged")):
                    st.markdown(f"**Issue:** {it['explanation']}")
                    st.markdown(f"**Expected:** {str(it.get('expected'))[:400]}")
                    st.markdown(f"**Observed:** {str(it.get('observed'))[:400]}")
                    st.caption(f"Spec: {it.get('spec_clause','')}")
                    if it.get("confidence_factors"):
                        st.caption("Confidence: " + " · ".join(
                            f"{k}={v}" for k, v in it["confidence_factors"].items()))
                    shot = it.get("screenshot")
                    if shot and os.path.exists(shot):
                        st.image(shot, caption=shot, width=680)
                    a, r, _ = st.columns([1, 1, 6])
                    a.button("✅ Accept", key=f"acc_{key}_{i}")
                    r.button("❌ Reject", key=f"rej_{key}_{i}")
else:
    st.info("Upload a questionnaire and choose a run mode, then click **Run QA**. "
            "Or click **Run QA** in demo mode to view the last run.")
