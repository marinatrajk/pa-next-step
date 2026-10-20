"""Data sources page: where case information lives, how the gatherer pulls it together, what it couldn't place,
and buttons to simulate new information arriving."""

from __future__ import annotations

import email
import json
import os
import re
from datetime import datetime
from email import policy
from html import escape
from pathlib import Path

import pymupdf
import streamlit as st

import engine
import gather
import ingest
import kb
import ui

skill = engine.load_skill()
has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

ui.inject()
ui.header("Data sources", "Where case information lives, and how it's gathered into one case record")

systems = gather.load_systems()
state = gather.load_state()
entries = kb.entries()
faxes = sum(1 for d in systems["inbox"] if d["channel"] == "fax")
intake = [d for d in systems["inbox"] if d.get("intake")]
processed = sorted(ingest.log()["files"].values(), key=lambda d: d["intake"]["processed_at"], reverse=True)  # incl. set-aside files


def card(kicker: str, title: str, body: str, feeds: str, tone: str = "") -> str:
    return (f'<div class="pa-src {tone}"><div class="pa-kicker">{kicker}</div><b>{title}</b><div class="pa-sub">{body}</div>'
            f'<div class="pa-feeds">→ {feeds}</div></div>')


st.markdown(
    '<div class="pa-three pa-five">'
    + card("PA tracker", f"{len(systems['tracker'])} cases", "Insurer, drug, status, deadline, patient name and DOB.", "case record")
    + card("Document inbox", f"{len(systems['inbox'])} documents", f"Faxes, PDFs, emails, photos, EHR exports ({len(intake)} read by intake). No case number on them.", "case record")
    + card("Case notes", f"{len(systems['notes'])} notes", "What operators wrote, filed on each case.", "case record")
    + card("Slack #access-ops", f"{len(systems['slack'])} messages", f"Only threads tagged @kb add become knowledge ({len(entries)} so far).", "team knowledge base", "accent")
    + card("Skill pack", f"SOP + {len(skill.policies)} policies", "Official rules, written by process owners.", "official rules")
    + "</div>",
    unsafe_allow_html=True,
)

# ------------------------------------------------------------------ gather

with st.container(key="pa-card-gather"):
    left, right = st.columns([3, 1.2], vertical_alignment="center")
    when = state.get("gathered_at", "")
    left.markdown(
        '<div class="pa-row-title">Gather</div>'
        f'<div class="pa-sub">Matches every document and note to its case, then rebuilds only the cases that changed. '
        f'Exact matches are free; the AI only judges items it hasn’t seen before. '
        f'{"Last run " + when.replace("T", " ") if when else "Not run yet."}</div>',
        unsafe_allow_html=True,
    )
    if right.button("Gather now", type="primary", use_container_width=True, disabled=not has_key):
        with st.spinner("Matching documents and notes to cases…"):
            st.session_state.last_gather = gather.gather(skill)["report"]
        st.rerun()

    report = st.session_state.get("last_gather")
    if report:
        changed = {cid: r for cid, r in report.items() if r["status"] != "unchanged"}
        if changed:
            rows = "".join(f'<div class="pa-req">{ui.pill(r["status"].title(), "accent")}<span class="pa-req-text"><b>{cid}</b> '
                           f'{escape(engine.case_summary(skill, cid)["drug"])}</span>'
                           f'{ui.pill("+" + str(len(r["new_items"])) + " new item" + ("s" if len(r["new_items"]) != 1 else ""), "green") if r["new_items"] else ""}</div>'
                           for cid, r in changed.items())
            st.markdown(ui.label(f"Changed in the last gather · {len(changed)}") + rows
                        + '<div class="pa-sub" style="margin-top:8px">Their next analysis runs again automatically, with the new information.</div>',
                        unsafe_allow_html=True)
        else:
            st.markdown('<div class="pa-sub" style="margin-top:10px">Last gather: nothing new. Every case is up to date.</div>', unsafe_allow_html=True)

# ------------------------------------------------------------------ details

if state.get("links"):
    sc = gather.score(state)
    k = st.columns(4)
    k[0].metric("Exact", sc["exact"], help="Case number on the item, patient name + DOB, or filed on the case")
    k[1].metric("By AI", sc["ai"] - len(state.get("unmatched", [])), help="Faxes with initials, typos or no DOB")
    k[2].metric("Unmatched", len(state.get("unmatched", [])), help="Belong to no case: a person needs to look")
    k[3].metric("Accuracy", f"{sc['accuracy']:.0%}", help="Checked against the known right answers (evals/gather_gold.json)")


# ------------------------------------------------------------------ intake


def show_original(path: Path, missing: str = "The original file is no longer in the raw inbox.") -> None:
    if not path.exists():
        st.markdown(f'<div class="pa-sub">{missing}</div>', unsafe_allow_html=True)
        return
    ext = path.suffix.lower()
    if ext in ingest.IMAGE_TYPES:
        st.image(str(path), width="stretch")
    elif ext == ".pdf":
        for page in pymupdf.open(path):
            st.image(page.get_pixmap(dpi=110).tobytes("png"), width="stretch")
    elif ext == ".eml":
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        head = "".join(f"<b>{h}:</b> {escape(str(msg[h] or ''))}<br>" for h in ("From", "To", "Date", "Subject"))
        st.markdown(f'<div class="pa-email">{head}<br>{escape(body.get_content() if body else "")}</div>', unsafe_allow_html=True)
    elif ext == ".json":
        st.code(json.dumps(json.loads(path.read_text()), indent=2)[:8000], language="json", height=460)
    else:
        st.code(path.read_text()[:8000], language=None, height=460)


def field_rows(pairs: list[tuple[str, str]]) -> str:
    cell = lambda v: escape(str(v)) if v not in ("", None, -1, -1.0) else '<span class="empty">not on the document</span>'
    return '<table class="pa-fields">' + "".join(f"<tr><td>{k}</td><td>{cell(v)}</td></tr>" for k, v in pairs) + "</table>"


def show_structured(item: dict, link: dict) -> None:
    r, i = item["schema"], item["intake"]
    conf = {"high": "green", "medium": "amber", "low": "red"}[r["confidence"]]
    st.markdown(f'<div class="pa-pills" style="margin-bottom:8px">{ui.pill(i["format"])} {ui.pill("read by " + i["read_by"], "accent")} '
                f'{ui.pill(r["confidence"] + " confidence", conf)}</div><div class="pa-answer" style="margin:0 0 10px">{escape(r["summary"])}</div>',
                unsafe_allow_html=True)
    who = ", ".join(x for x in (r["sender"]["person"], r["sender"]["organization"], r["sender"]["contact"]) if x)
    html = ui.label("Document") + field_rows([("Type", ingest.DOC_LABEL[r["doc_type"]]), ("Patient", r["patient"]["name"]),
                                             ("Date of birth", r["patient"]["dob"]), ("Member ID", r["patient"]["member_id"] or r["coverage"]["member_id"]),
                                             ("From", who), ("Dated", r["document_date"])])
    d = r["insurer_decision"]
    if d["status"]:
        html += ui.label("Insurer decision") + field_rows([("Status", d["status"].title()), ("Reason code", d["reason_code"]), ("Reason", d["reason"])])
    if r["coverage"]["insurer"]:
        html += ui.label("Coverage") + field_rows([("Insurer", r["coverage"]["insurer"]), ("Member ID", r["coverage"]["member_id"]),
                                                  ("Effective", r["coverage"]["effective_date"])])
    sev = [(k, v) for k, v in (("IGA", r["severity"]["iga"]), ("BSA %", r["severity"]["bsa_pct"]), ("DAS28", r["severity"]["das28"])) if v != -1]
    if sev:
        html += ui.label("Severity") + field_rows(sev)
    if r["medications"]:
        html += ui.label(f"Medications · {len(r['medications'])}") + '<table class="pa-table"><thead><tr><th>Drug</th><th>Dates</th><th>Outcome</th></tr></thead><tbody>' + "".join(
            f'<tr><td><b>{escape(m["drug"])}</b><div class="pa-sub">{escape(m["dose"])}</div></td><td>{escape(m["start"] or "?")} → {escape(m["end"] or "?")}</td>'
            f'<td>{escape(m["outcome"])}</td></tr>' for m in r["medications"]) + "</tbody></table>"
    if r["labs"]:
        html += ui.label(f"Labs · {len(r['labs'])}") + '<table class="pa-table"><thead><tr><th>Test</th><th>Result</th></tr></thead><tbody>' + "".join(
            f'<tr><td>{escape(l["test"])}</td><td><b>{escape(l["result"])}</b> {escape(l["unit"])} {ui.pill(l["flag"], "amber") if l["flag"] else ""}</td></tr>'
            for l in r["labs"]) + "</tbody></table>"
    for c in r["contraindications"]:
        html += (ui.label("Contraindication") + f'<div class="pa-req">{ui.pill("from prescriber", "green") if c["stated_by_prescriber"] else ui.pill("relayed, not prescriber", "amber")}'
                 f'<span class="pa-req-text"><b>{escape(c["drug_class"])}</b><br><span class="pa-sub">“{escape(c["evidence"])}”</span></span></div>')
    if r["missing"]:
        html += ui.label("Not on the document") + " ".join(f'<span class="pa-chip">{escape(m)}</span>' for m in r["missing"])
    case = link.get("case")
    html += ui.label("Matched to") + (
        f'<div class="pa-req">{ui.pill("by AI" if link.get("method") == "ai" else "exact", "accent" if link.get("method") == "ai" else "green")}'
        f'<span class="pa-req-text"><b>{case}</b> · {escape(engine.load_case(case)["patient"]["name"])}<br><span class="pa-sub">{escape(link["how"].removeprefix("AI: "))}</span></span></div>'
        if case else f'<div class="pa-req">{ui.pill("Needs a person", "amber")}<span class="pa-req-text">No case. {escape(link.get("how", "Not gathered yet.").removeprefix("AI: "))}</span></div>')
    if i["issues"]:
        html += "".join(f'<div class="pa-req">{ui.pill("Check", "red")}<span class="pa-req-text">{escape(x)}</span></div>' for x in i["issues"])
    st.markdown(html, unsafe_allow_html=True)
    with st.expander("Schema output (JSON)"):
        st.json({k: v for k, v in r.items() if k != "transcription"}, expanded=False)


def set_aside_card(item: dict) -> str:
    kept = "The file is still in the raw inbox." if (engine.ROOT / item["raw_file"]).exists() else "The file wasn’t kept."
    return ('<div class="pa-step manual" style="margin-top:0"><div class="pa-kicker">Set aside</div>'
            '<div class="pa-headline">This isn’t a prior-auth document</div>'
            f'<div class="pa-sendto">{escape(item["schema"]["relevance"]["reason"] or "It doesn’t look like anything about a patient’s prescription or coverage.")}</div>'
            f'<div class="pa-sub" style="margin-top:8px">Nothing from it was added to a case. {kept} '
            'Intake only takes clinical, insurer, coverage and office documents.</div></div>')


def render_intake() -> None:
    if note := st.session_state.pop("intake_toast", None):
        st.toast(note)
    st.markdown(
        '<div class="pa-flow">'
        '<div><b>1 · Arrives</b>Scanned fax, PDF, email, phone photo, EHR export</div>'
        '<div><b>2 · Detect</b>Format and whether there is text to read</div>'
        '<div><b>3 · Read</b>Code for structured data; AI vision for scans and photos</div>'
        '<div><b>4 · Schematize</b>One schema for every document, nothing guessed</div>'
        '<div><b>5 · Check</b>Dates, identity, confidence; flag for review</div>'
        '<div><b>6 · Match</b>Gather links it to a case and rebuilds that case</div>'
        "</div>", unsafe_allow_html=True)

    up_col, inbox_col = st.columns([2.2, 1], vertical_alignment="bottom")
    n = st.session_state.setdefault("upload_n", 0)
    up = up_col.file_uploader("Drop in a document", type=[e.lstrip(".") for e in ingest.SUPPORTED], key=f"upload-{n}", disabled=not has_key,
                              help="PDF, scan, photo, email (.eml), EHR export (FHIR .json) or text. Synthetic documents only: no real patient information.")
    waiting = ingest.pending()
    if inbox_col.button(f"Process raw inbox ({len(waiting)} new)", use_container_width=True, disabled=not waiting or not has_key):
        with st.spinner(f"Reading {len(waiting)} file{'s' if len(waiting) != 1 else ''}…"):
            done = ingest.process_inbox()
            st.session_state.last_gather = gather.gather(skill)["report"]
        st.session_state.intake_sel = done[-1]["key"]
        st.rerun()
    if up:
        dest = ingest.RAW_DIR / re.sub(r"[^\w.\-]", "_", Path(up.name).name)
        if dest.exists() and dest.read_bytes() != up.getvalue():
            dest = dest.with_stem(f"{dest.stem}-{datetime.now():%H%M%S}")
        dest.write_bytes(up.getvalue())
        with st.spinner(f"Reading {up.name} and matching it to a case…"):
            item = ingest.process_file(dest, via="upload")
            st.session_state.last_gather = gather.gather(skill)["report"]
        st.session_state.intake_sel = item["key"]
        st.session_state.upload_n = n + 1  # clears the uploader
        if item["intake"].get("set_aside"):
            st.session_state.intake_toast = f"{up.name} isn’t a prior-auth document, so it was set aside."
        st.rerun()

    if not processed:
        st.markdown('<div class="pa-sub">No documents read yet.</div>', unsafe_allow_html=True)
        return
    links = state.get("links", {})

    def status(d: dict) -> str:
        if d["intake"].get("set_aside"):
            return ui.pill("set aside")
        if d["intake"]["needs_review"]:
            return ui.pill("review", "red")
        return ui.pill(d["schema"]["confidence"], "green" if d["schema"]["confidence"] == "high" else "amber")

    rows = "".join(
        f'<tr><td><b>{escape(Path(d["raw_file"]).name)}</b>{" " + ui.pill({"upload": "uploaded", "slack": "from Slack"}[d["intake"]["via"]], "accent") if d["intake"].get("via") in ("upload", "slack") else ""}'
        f'<div class="pa-sub">{escape(d["intake"]["format"])}</div></td><td>{escape(d["intake"]["read_by"])}<div class="pa-sub">{d["intake"]["seconds"]}s</div></td>'
        f'<td>{escape(d["doc_type"])}<div class="pa-sub">{"not added to any case" if d["intake"].get("set_aside") else escape(d["patient_name"] or "no patient")}</div></td>'
        f'<td>{"—" if d["intake"].get("set_aside") else links.get(d["key"], {}).get("case") or ui.pill("unmatched", "amber")}</td>'
        f'<td>{status(d)}</td></tr>'
        for d in processed)
    st.markdown('<table class="pa-table"><thead><tr><th>File</th><th>Read by</th><th>Became</th><th>Case</th><th>Status</th></tr></thead>'
                f"<tbody>{rows}</tbody></table>", unsafe_allow_html=True)

    keys = [d["key"] for d in processed]
    by_key = {d["key"]: d for d in processed}
    sel = st.session_state.get("intake_sel")
    key = st.selectbox("Open a document", keys, index=keys.index(sel) if sel in keys else 0,
                       format_func=lambda k: f'{Path(by_key[k]["raw_file"]).name}  →  {by_key[k]["doc_type"]}')
    item = by_key[key]
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown(ui.label("Original"), unsafe_allow_html=True)
        show_original(engine.ROOT / item["raw_file"], "Not kept, because it isn’t a prior-auth document." if item["intake"].get("set_aside") else
                      "The original file is no longer in the raw inbox.")
    with right:
        st.markdown(ui.label("What intake extracted"), unsafe_allow_html=True)
        if item["intake"].get("set_aside"):
            st.markdown(set_aside_card(item), unsafe_allow_html=True)
        else:
            show_structured(item, links.get(key, {}))
        if (item["intake"].get("via") in ("upload", "slack") or item["intake"].get("set_aside")) and st.button("Remove this upload" if item["intake"].get("via") in ("upload", "slack") else "Remove from the list", key=f"rm-{key}"):
            with st.spinner("Removing it and gathering…"):
                ingest.remove(key)
                st.session_state.last_gather = gather.gather(skill)["report"]
            st.session_state.pop("intake_sel", None)
            st.rerun()

tab_in, tab_ai, tab_un, tab_sim, tab_cases = st.tabs([f"Document intake · {len(processed)}", "Matched by AI", f"Unmatched · {len(state.get('unmatched', []))}", "Simulate new information", "By case"])

with tab_in:
    render_intake()

with tab_ai:
    sys_items = {d["key"]: d for d in systems["inbox"]}
    rows = ""
    for key, link in state.get("links", {}).items():
        if link["method"] != "ai" or not link.get("case") or key not in sys_items:
            continue
        d = sys_items[key]
        rows += (f'<tr><td><b>{key}</b>{" " + ui.pill("simulated", "amber") if d.get("simulated") else ""}<div class="pa-sub">{escape(d["doc_type"])}</div></td>'
                 f'<td>“{escape(d.get("patient_name") or "(none)")}”<div class="pa-sub">DOB {escape(d.get("patient_dob") or "missing")} · {escape(d.get("from_office", ""))}</div></td>'
                 f'<td><b>{link["case"]}</b><div class="pa-sub">{escape(engine.load_case(link["case"])["patient"]["name"])}</div></td>'
                 f'<td>{escape(link["how"].removeprefix("AI: "))} {ui.pill(link.get("confidence", ""), "green" if link.get("confidence") == "high" else "amber")}</td></tr>')
    st.markdown('<div class="pa-sub" style="margin-bottom:8px">Faxes that no exact rule could place. The AI weighed name, date of birth, office, drug and content '
                'against a short list of likely cases.</div>'
                f'<table class="pa-table"><thead><tr><th>Item</th><th>Written as</th><th>Matched to</th><th>Why</th></tr></thead><tbody>{rows}</tbody></table>'
                if rows else '<div class="pa-sub">Nothing needed the AI.</div>', unsafe_allow_html=True)

with tab_un:
    un = state.get("unmatched", [])
    if not un:
        st.markdown('<div class="pa-sub">Everything was matched to a case.</div>', unsafe_allow_html=True)
    for x in un:
        d = next((it for it in systems["inbox"] if it["key"] == x["key"]), {})
        st.markdown(
            f'<div class="pa-req">{ui.pill("Needs a person", "amber")}<span class="pa-req-text"><b>{x["key"]} · {escape(d.get("doc_type", x["system"]))}</b>'
            f'<br><span class="pa-sub">From {escape(d.get("from_office", "?"))}: {escape(d.get("content", x["summary"]))[:140]}</span>'
            f'<br><span class="pa-sub">Why it wasn’t matched: {escape(x["why"].removeprefix("AI: "))}</span></span></div>',
            unsafe_allow_html=True,
        )

with tab_sim:
    active = gather.active_simulations()
    st.markdown('<div class="pa-sub" style="margin-bottom:10px">Drop new information into a source system, then gather. '
                'The affected case is rebuilt, and its next analysis includes the new item.</div>', unsafe_allow_html=True)
    for name, sim in gather.SIMULATIONS.items():
        with st.container(key=f"pa-card-sim-{name}"):
            a, b = st.columns([3, 1.2], vertical_alignment="center")
            a.markdown(f'<div class="pa-row-title">{escape(sim["title"])}</div><div class="pa-sub">{escape(sim["detail"])}</div>', unsafe_allow_html=True)
            if name in active:
                b.markdown(f'<div style="text-align:right">{ui.pill("Arrived", "green")}</div>', unsafe_allow_html=True)
            elif b.button("Simulate", key=f"sim-{name}", use_container_width=True, disabled=not has_key):
                with st.spinner("Adding it to the source system and gathering…"):
                    gather.simulate(name)
                    st.session_state.last_gather = gather.gather(skill)["report"]
                st.rerun()
    if active:
        st.markdown('<div class="pa-sub" style="margin:10px 0 6px">Open the case in Chat to see the updated next step. '
                    'Reset before running the evals: they expect the original information.</div>', unsafe_allow_html=True)
        if st.button("Reset simulations"):
            with st.spinner("Removing simulated items and gathering…"):
                gather.reset_simulations()
                st.session_state.last_gather = gather.gather(skill)["report"]
            st.rerun()

with tab_cases:
    rows = ""
    for cid in engine.cases_by_urgency(skill):
        c = engine.load_case(cid)
        mine = kb.for_case(c) if engine.gate(skill, c).applies else []
        ai = sum(1 for d in c["documents"] if d.get("_source", {}).get("linked_by", "").startswith("AI:"))
        run = engine.last_run(cid)
        fresh = ui.pill("current", "green") if engine.is_current(run, skill, c) else (ui.pill("needs a new run", "amber") if run else ui.pill("not run", ""))
        rows += (f'<tr><td><b>{cid}</b><div class="pa-sub">{escape(c["patient"]["name"])}</div></td><td>{len(c["documents"])}'
                 f'{" (" + str(ai) + " by AI)" if ai else ""}</td><td>{len(c["notes"])}</td><td>{" ".join(e["id"] for e in mine) or "—"}</td><td>{fresh}</td></tr>')
    st.markdown('<table class="pa-table"><thead><tr><th>Case</th><th>Documents</th><th>Case notes</th><th>Team knowledge</th><th>Analysis</th></tr></thead>'
                f'<tbody>{rows}</tbody></table>', unsafe_allow_html=True)
