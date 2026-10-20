"""Case review: every source, criterion and conflict behind a recommendation. Opened from app.py.

Layout: case header -> recommendation card -> detail tabs (criteria, gaps, sources, draft).
Only the recommendation is open by default; the evidence is one tab away.
"""

from __future__ import annotations

import json
import os
from html import escape

import streamlit as st

import engine
import kb
import ui

skill = engine.load_skill()
m = skill.manifest
ACTION_LABELS = {**engine.ACTION_LABELS, "NOT_APPLICABLE": "Decline · out of scope"}
has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
CRITERION_PILL = {"met": ("Met", "green"), "not_met": ("Not met", "red"), "unknown": ("Unknown", "amber")}

ui.inject()
ui.header("Case review", "Every source, criterion and conflict behind a recommendation")


def chips(ids: list[str]) -> str:
    return " ".join(f'<span class="pa-chip">{escape(i)}</span>' for i in ids)


# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.markdown('<div class="pa-label" style="margin-top:4px">Case queue</div>', unsafe_allow_html=True)

    def queue_label(cid: str) -> str:
        c = engine.case_summary(skill, cid)
        h = c["hours"]
        left = "n/a" if h is None else (f"{h:.0f}h" if h < 72 else f"{h / 24:.0f}d")
        return f"{cid} · {left}{' · at risk' if h is not None and h < m['deadline_risk_hours'] else ''}"

    case_id = st.radio("Case", engine.cases_by_urgency(skill), format_func=queue_label, label_visibility="collapsed", key="wb_case")
    with st.expander("About this skill"):
        st.markdown(f"**{m['name']}** · v{m['version']}\n\n**Use when:** {m['when_to_use']}\n\n**Don't use for:** {m['when_not_to_use']}\n\n*Owner: {m['owner']}*")
    if not has_key:
        st.warning("No ANTHROPIC_API_KEY set. Saved runs are viewable; new runs are disabled.")

case = engine.load_case(case_id)
g = engine.gate(skill, case)
info = engine.case_summary(skill, case_id)
tab_case, tab_evals = st.tabs(["Case", "Evals"])


# ------------------------------------------------------------------ case tab

def render_case_tab() -> None:
    pa = case["pa"]
    submission = f"Submission #{pa['submission_count']}"
    st.markdown(
        f'<div class="pa-case-title">{case_id}<span>{escape(info["drug"])}</span></div>'
        f'<div class="pa-pills">{ui.pill(info["status"])} {ui.pill(info["payer"])} {ui.pill(submission)} {ui.deadline_pill(info["hours"])}</div>'
        f'<div class="pa-sub" style="margin:10px 0 18px">Payer said: {escape(pa["reason_text"])}</div>',
        unsafe_allow_html=True,
    )

    if not g.applies:
        st.markdown(ui.manual_card(g.notes, margin_top=0), unsafe_allow_html=True)
        return

    if "runs" not in st.session_state:
        st.session_state.runs = {}
    run = st.session_state.runs.get(case_id) or engine.last_run(case_id)

    if not run or run.get("status") != "ok":
        with st.container(key="pa-card-empty"):
            st.markdown("No recommendation yet for this case.")
            if st.button("Get next step", type="primary", disabled=not has_key):
                if ui.run_with_log(lambda log: st.session_state.runs.__setitem__(case_id, engine.run_case(case_id, skill, on_step=log))):
                    st.rerun()
        return

    r = run["result"]
    sources = engine.build_sources(skill, case)
    cited = {sid for sec in ("criteria", "conflicts", "triggers_fired") for item in r[sec] for sid in item["source_ids"]}
    prov = engine.provenance(case)

    if not engine.is_current(run, skill, case):
        st.markdown('<div class="pa-warn" style="margin-bottom:10px">New documents or messages arrived since this analysis. '
                    'Re-run to include them.</div>', unsafe_allow_html=True)

    # ---- recommendation card
    with st.container(key="pa-card-rec"):
        action = ACTION_LABELS[r["primary_action"]]
        if r["escalate"]:
            owners = sorted({o.strip() for t in r["triggers_fired"] for o in m["triggers"][t["id"]]["owner"].split("+")})
            pills = " ".join(ui.pill(f"{t['id']} · {m['triggers'][t['id']]['name']}", "amber") for t in r["triggers_fired"])
            banner = (f'<div class="pa-step escalate" style="margin-top:0"><div class="pa-kicker">Escalate</div><div class="pa-headline">{action}</div>'
                      f'<div class="pa-sendto"><span>Escalate to</span>{escape(", ".join(owners))}</div>'
                      f'<div class="pa-pills" style="margin-top:10px">{pills}</div></div>')
        else:
            banner = (f'<div class="pa-step go" style="margin-top:0"><div class="pa-kicker">Ready to act</div><div class="pa-headline">{action}</div>'
                      f'<div class="pa-sendto"><span>Escalation</span>none needed</div></div>')
        summary = (r.get("operator_summary") or {}).get("why") or []
        st.markdown(banner + (ui.label("Why") + ui.bullet_list(summary, "why") if summary else ""), unsafe_allow_html=True)
        for note in run["guardrail_notes"]:
            st.markdown(f'<div class="pa-warn">Guardrail: {escape(note)}</div>', unsafe_allow_html=True)
        meta, btn = st.columns([4, 1], vertical_alignment="center")
        u = run["usage"]
        changed = f" · model's own pick was {run['model_action']}" if run["model_action"] != r["primary_action"] else ""
        meta.markdown(f'<div class="pa-sub">Confidence {r["confidence"]} · {u["model"]} · {u["seconds"]}s · analyzed {run["ran_at"].replace("T", " ")}{changed}</div>',
                      unsafe_allow_html=True)
        rerun = btn.button("Re-run", disabled=not has_key, use_container_width=True)
        if rerun:
            if ui.run_with_log(lambda log: st.session_state.runs.__setitem__(case_id, engine.run_case(case_id, skill, on_step=log))):
                st.rerun()
        elif run.get("log"):
            with st.expander(f"Action log · {len(run['log'])} steps"):
                st.markdown("".join(ui.log_line(x["text"], x["t"]) for x in run["log"]), unsafe_allow_html=True)

    # ---- details
    met = sum(c["status"] == "met" for c in r["criteria"])
    gaps = len(r["needs_human_judgment"]) + len(r["could_not_determine"]) + len(r["conflicts"]) + len(r["missing_information"])
    t_crit, t_gaps, t_src, t_draft = st.tabs([f"Criteria · {met}/{len(r['criteria'])} met", f"Gaps & judgment · {gaps}",
                                              f"Sources · {len(sources)}", "Draft & decision"])

    with t_crit:
        rows = "".join(
            f'<tr><td>{ui.pill(*CRITERION_PILL[c["status"]])}</td><td class="crit">{escape(c["criterion"])}</td>'
            f'<td>{escape(c["evidence"])}</td><td>{chips(c["source_ids"])}</td></tr>'
            for c in r["criteria"])
        st.markdown(f'<table class="pa-table"><thead><tr><th>Status</th><th>Criterion</th><th>Evidence</th><th>Sources</th></tr></thead><tbody>{rows}</tbody></table>',
                    unsafe_allow_html=True)

    with t_gaps:
        html = ""
        if r["needs_human_judgment"]:
            html += ui.label("Needs your judgment") + ui.bullet_list(r["needs_human_judgment"], "why")
        if r["could_not_determine"]:
            html += ui.label("Could not determine") + ui.bullet_list(r["could_not_determine"], "why")
        if r["conflicts"]:
            html += ui.label("Conflicts between sources") + '<ul class="pa-list why">' + "".join(
                f'<li>{escape(c["description"])}<div class="pa-sub" style="margin-top:2px">→ {escape(c["resolution"])} {chips(c["source_ids"])}</div></li>'
                for c in r["conflicts"]) + "</ul>"
        if r["missing_information"]:
            html += ui.label("Missing information") + '<ul class="pa-list why">' + "".join(
                f'<li><b>{escape(x["item"])}</b><div class="pa-sub" style="margin-top:2px">Get it by: {escape(x["how_to_get"])}</div></li>'
                for x in r["missing_information"]) + "</ul>"
        if r["parallel_actions"]:
            html += ui.label("Can happen in parallel") + ui.bullet_list(r["parallel_actions"], "todo")
        st.markdown(html or '<div class="pa-sub">No open questions on this case.</div>', unsafe_allow_html=True)
        st.markdown(ui.label("Full reasoning") + f'<div class="pa-sub" style="font-size:14px;line-height:1.6">{escape(r["action_rationale"])}</div>',
                    unsafe_allow_html=True)

    with t_src:
        st.markdown('<div class="pa-sub" style="margin-bottom:6px">Three separate sources, kept apart on purpose. Inside each, the higher trust tier wins '
                    'when sources disagree. \u201cCited\u201d = used in the recommendation.</div>', unsafe_allow_html=True)
        kb_meta = {e["id"]: e for e in kb.entries()}
        for key, (name, about, tiers) in engine.SECTIONS.items():
            group = [s for s in sources if s["tier"] in tiers]
            st.markdown(f'<div class="pa-section-head"><b>{name}</b><span class="pa-sub">{about}</span></div>', unsafe_allow_html=True)
            if not group:
                st.markdown('<div class="pa-sub" style="margin:0 0 10px 2px">Nothing relevant to this case.</div>', unsafe_allow_html=True)
            for s in group:
                tag = "  ·  cited" if s["id"] in cited else ""
                src = prov.get(s["id"], {})
                with st.expander(f"{s['id']}  —  {s['label']}{tag}"):
                    if s["id"] in kb_meta:
                        e = kb_meta[s["id"]]
                        st.markdown(f'<div class="pa-pills" style="margin-bottom:8px">{ui.pill("Team knowledge base")} {ui.pill("Slack thread " + e["slack_thread"])} '
                                    f'{ui.pill("about this case" if e["case_id"] else "general know-how", "accent")}</div>', unsafe_allow_html=True)
                    elif src:
                        how = src["linked_by"]
                        st.markdown(f'<div class="pa-pills" style="margin-bottom:8px">{ui.pill(src["system"])} {ui.pill(src["ref"])} '
                                    f'{ui.pill("matched by AI" if how.startswith("AI:") else "exact match", "accent" if how.startswith("AI:") else "green")}'
                                    f'{" " + ui.pill("simulated", "amber") if src.get("simulated") else ""}'
                                    f'{" " + ui.pill(src["intake"]["format"] + " · read by " + src["intake"]["read_by"]) if src.get("intake") else ""}</div>'
                                    f'<div class="pa-sub" style="margin-bottom:8px">{escape(how.removeprefix("AI: "))}</div>', unsafe_allow_html=True)
                    with st.container(key=f"pa-doc-{s['id']}"):
                        if s["id"] == "CASE":
                            st.code(s["text"], language="json")
                        else:
                            st.markdown(s["text"])

    with t_draft:
        d = r["draft"]
        st.markdown(f'<div class="pa-pills" style="margin-bottom:10px">{ui.pill(d["kind"].replace("_", " "), "accent")} '
                    f'<span class="pa-sub">To</span> <b style="font-size:14px">{escape(d["to"])}</b></div>', unsafe_allow_html=True)
        edited = st.text_area("Draft message", f"Subject: {d['subject']}\n\n{d['body']}", height=260, key=f"draft-{case_id}-{run['ran_at']}")
        a, b, _ = st.columns([1, 1, 2])
        if a.button("Mark as sent", type="primary", use_container_width=True, key=f"accept-{case_id}"):
            engine.log_decision(case_id, run, "accept", edited_draft=edited, view="case_review")
            st.toast("Logged as sent. See the Decision log.")
        with b.popover("Override", use_container_width=True):
            new_action = st.selectbox("Correct action", m["actions"], index=m["actions"].index(r["primary_action"]), format_func=ACTION_LABELS.get)
            reason = st.text_area("Why? (becomes a new eval case)")
            if st.button("Log override", type="primary", disabled=not reason.strip()):
                engine.log_decision(case_id, run, "override", reason=reason, final_action=new_action, edited_draft=edited, view="case_review")
                st.toast("Override logged. Review it in the Decision log.")


with tab_case:
    render_case_tab()


# ----------------------------------------------------------------- evals tab

with tab_evals:
    st.markdown('<div class="pa-sub" style="margin-bottom:14px">Backtest against labeled cases in the skill pack (evals/expected.json). '
                'Re-score before any SOP, prompt or model change ships.</div>', unsafe_allow_html=True)
    latest = skill.path / "evals" / "results" / "latest.json"
    labeled = json.loads((skill.path / m["eval_file"]).read_text())["cases"]
    unrun = [c for c in labeled if engine.last_run(c) is None and labeled[c].get("expected_status") != "not_applicable"]
    col1, col2, _ = st.columns([1.3, 1.3, 1.4])
    run_reuse = col1.button(f"Run new cases ({len(unrun)})", type="primary", disabled=not has_key, use_container_width=True,
                            help="Runs the AI engine only on cases without a saved analysis (about $0.10 each), reuses the rest, then grades all of them.")
    run_fresh = col2.button(f"Re-run all {len(labeled)} cases", disabled=not has_key, use_container_width=True,
                            help="Fresh AI-engine run on every case (about $0.10 each), then grades all of them.")
    if run_fresh or run_reuse:
        import run_evals
        with st.spinner("Running cases and grading them… about a minute"):
            run_evals.run_all(reuse=run_reuse)

    if not latest.exists():
        st.info("No eval results yet.")
    else:
        report = json.loads(latest.read_text())
        s = report["summary"]
        k = st.columns(5)
        k[0].metric("Cases passed", f"{s['passed']}/{s['cases']}")
        k[1].metric("Action", f"{s['action_accuracy']:.0%}")
        k[2].metric("Escalation", f"{s['escalation_accuracy']:.0%}")
        k[3].metric("Must-flag", f"{s['must_flag_hit_rate']:.0%}")
        k[4].metric("Violations", s["must_not_violations"])
        st.markdown(f'<div class="pa-sub" style="margin:8px 0 12px">{report["skill"]} · {report["model"]} · {report["ran_at"].replace("T", " ")}</div>',
                    unsafe_allow_html=True)
        rows = "".join(
            f'<tr><td><b>{x["case_id"]}</b></td><td>{x["label"]}</td><td>{ACTION_LABELS.get(x.get("expected_action"), "")}</td>'
            f'<td>{ACTION_LABELS.get(x.get("got_action"), "")}</td><td>{x.get("flags_hit")}/{x.get("flags_total")}</td>'
            f'<td>{ui.pill("Pass", "green") if x["passed"] else ui.pill("Fail", "red")}</td></tr>'
            for x in report["rows"])
        st.markdown(f'<table class="pa-table"><thead><tr><th>Case</th><th>Type</th><th>Expected</th><th>Got</th><th>Flags</th><th>Result</th></tr></thead>'
                    f'<tbody>{rows}</tbody></table>', unsafe_allow_html=True)
        for x in report["rows"]:
            failed = [c for c in x.get("checks", []) if not c["passed"]]
            if failed or x.get("missed_triggers") or x.get("extra_triggers"):
                with st.expander(f"{x['case_id']}: what failed"):
                    for c in failed:
                        st.markdown(f"- **{c['kind']}:** {c['check']}  \n  judge: {c['reason']}")
                    if x.get("missed_triggers") or x.get("extra_triggers"):
                        st.markdown(f"- triggers missed `{x['missed_triggers']}` · extra `{x['extra_triggers']}`")
