"""Knowledge base page: the three separate sources, the team knowledge base, the Slack channel it learns from, and search."""

from __future__ import annotations

import os
from html import escape

import streamlit as st

import engine
import gather
import kb
import ui

skill = engine.load_skill()
has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
KIND = {"tip": ("Tip", "green"), "general": ("General", ""), "case_update": ("Case update", "accent"), "policy_claim": ("Insurer-rule claim", "amber")}

ui.inject()
ui.header("Knowledge base", "Team know-how saved from Slack with @kb, kept separate from the SOPs and the case record")

entries = kb.entries()
systems = gather.load_systems()
st.markdown(
    '<div class="pa-three">'
    f'<div class="pa-src"><div class="pa-kicker">Official rules</div><b>SOP + {len(skill.policies)} payer policies</b>'
    '<div class="pa-sub">Written by process owners. Authoritative: the only place insurer rules come from.</div></div>'
    f'<div class="pa-src"><div class="pa-kicker">Case record</div><b>{len(systems["tracker"])} cases · {len(systems["inbox"])} documents · {len(systems["notes"])} case notes</b>'
    '<div class="pa-sub">PA tracker, document inbox, case notes. Facts about one case.</div></div>'
    f'<div class="pa-src accent"><div class="pa-kicker">Team knowledge base</div><b>{len(entries)} entries from Slack</b>'
    '<div class="pa-sub">Saved when someone tags <span class="pa-kbd">@kb add</span>. Useful context, never authoritative.</div></div>'
    '</div>',
    unsafe_allow_html=True,
)

tab_kb, tab_slack, tab_search = st.tabs([f"Entries · {len(entries)}", "Slack #access-ops", "Search"])


def entry_card(e: dict, i: str) -> None:
    label, tone = KIND[e["kind"]]
    pills = [ui.pill(label, tone)] + [ui.pill(p.replace("_", " ").title()) for p in e["payers"]]
    pills += [ui.pill(e["case_id"], "accent")] if e["case_id"] else []
    with st.container(key=f"pa-card-kb-{i}"):
        st.markdown(
            f'<div class="pa-pills">{"".join(pills)}<span class="pa-sub" style="margin-left:auto">{e["id"]} · saved by {escape(e["added_by"])}, '
            f'{e["added_at"][:10]}</span></div><div class="pa-row-title" style="margin-top:8px">{escape(e["title"])}</div>'
            f'<div class="pa-answer" style="margin-top:4px">{escape(e["summary"])}</div>',
            unsafe_allow_html=True,
        )
        a, b = st.columns([5, 1], vertical_alignment="center")
        with a.expander("Slack thread"):
            st.text(e["transcript"])
        if b.button("Remove", key=f"rm-{i}", type="tertiary"):
            kb.remove(e["id"])
            st.rerun()


with tab_kb:
    if not entries:
        st.info("Nothing saved yet. In Slack, reply to a thread with “@kb add”.")
    groups = [
        ("General know-how", "Shared with every case for the same insurer, when relevant.", [e for e in entries if not e["case_id"] and e["kind"] in ("tip", "general")]),
        ("About a specific case", "Used only on that case.", [e for e in entries if e["case_id"]]),
        ("Insurer-rule claims awaiting confirmation", "Never used as general knowledge. Payer Relations confirms these and updates the policy, or rejects them.",
         [e for e in entries if not e["case_id"] and e["kind"] == "policy_claim"]),
    ]
    for title, about, items in groups:
        if not items:
            continue
        st.markdown(ui.label(f"{title} · {len(items)}") + f'<div class="pa-sub" style="margin:-4px 0 8px">{about}</div>', unsafe_allow_html=True)
        for e in items:
            entry_card(e, e["id"])

with tab_slack:
    st.markdown('<div class="pa-sub" style="margin-bottom:10px">The channel the bot listens to. Only threads someone tags with '
                '<span class="pa-kbd">@kb add</span> become knowledge. Everything else stays in Slack.</div>', unsafe_allow_html=True)
    msgs = kb.slack()
    saved = {e["slack_thread"]: e["id"] for e in entries}
    roots = sorted([m for m in msgs if not m.get("thread") and not kb.BOT.search(m["text"])], key=lambda m: m["ts"], reverse=True)
    for r in roots:
        replies = sorted([m for m in msgs if m.get("thread") == r["key"]], key=lambda m: m["ts"])
        lines = [r] + replies
        with st.container(key=f"pa-card-slack-{r['key']}"):
            html = ""
            for m in lines:
                bot = kb.BOT.search(m["text"])
                style = ' style="color:var(--pa-accent)"' if bot else ""
                html += (f'<div class="pa-slack{" reply" if m is not r else ""}"><b>{escape(m["author"])}</b> '
                         f'<span class="pa-sub">{m["ts"][5:16].replace("T", " ")}</span><div{style}>{escape(m["text"])}</div></div>')
            st.markdown(html, unsafe_allow_html=True)
            if r["key"] in saved:
                st.markdown(f'<div class="pa-pills">{ui.pill("Saved to the knowledge base as " + saved[r["key"]], "green")}</div>', unsafe_allow_html=True)
            else:
                with st.popover("Tag @kb add", disabled=not has_key):
                    note = st.text_input("Note for the team (optional)", key=f"note-{r['key']}")
                    if st.button("Post “@kb add” in this thread", key=f"tag-{r['key']}", type="primary"):
                        with st.spinner("The bot is saving the thread…"):
                            kb.tag_thread(r["key"], "You (demo)", note.strip())
                        st.rerun()

with tab_search:
    q = st.text_input("Search the team knowledge base by meaning", placeholder="e.g. what does Bluepeak need to accept a biosimilar intolerance?")
    if q.strip():
        hits = kb.search(q.strip(), k=5)
        if not hits:
            st.info("The knowledge base is empty.")
        for h in hits:
            st.markdown(
                f'<div class="pa-req"><span class="pa-pill {"green" if h["similarity"] >= 0.5 else "amber" if h["similarity"] >= kb.MIN_SIMILARITY else ""}">'
                f'{h["similarity"]:.2f}</span><span class="pa-req-text"><b>{escape(h["title"])}</b><br><span class="pa-sub">{escape(h["summary"])}</span></span>'
                f'<span class="pa-chip">{h["id"]}</span></div>',
                unsafe_allow_html=True,
            )
        st.markdown(f'<div class="pa-sub" style="margin-top:8px">Similarity runs from 0 to 1. Below {kb.MIN_SIMILARITY:.2f} an entry is not used for a case.</div>',
                    unsafe_allow_html=True)
