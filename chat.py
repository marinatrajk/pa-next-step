"""Chat view: the operator asks for a case's next step and gets the step, who to send to, why, and a draft.

Commands:  /next-step <case>   /investigate <question>   /cases   /help   (a bare case number works too)
Everything behind the recommendation stays one click away under "Details".
"""

from __future__ import annotations

import json
import os
import re

import streamlit as st
import streamlit.components.v1 as components

import chat_ai
import engine
import ui
from html import escape

skill = engine.load_skill()
m = skill.manifest
has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
STATUS_ICON = {"met": "✅", "not_met": "❌", "unknown": "❓"}
REQ_PILL = {"met": ("Met", "green"), "not_met": ("Not met", "red"), "unknown": ("Unknown", "amber")}


def short_criterion(text: str) -> str:
    """'Step 3a: medium-to-super-high-potency TCS for >=28 days, with drug, dates and outcome' -> 'Medium-to-super-high-potency TCS for ≥28 days'."""
    t = re.sub(r"^(?:\d+[a-z]?[.)]|step \d+[a-z]?:)\s*", "", text.strip(), flags=re.I)
    head, sep, tail = t.partition(":")
    if sep and len(head) <= 30 and tail.strip():  # "Step therapy: methotrexate ..." -> "methotrexate ..."
        t = tail.strip()
    t = re.sub(r"\s*\([^)]*\)", "", t)  # drop parentheticals, keep what follows
    t = re.split(r", with |; ", t)[0].replace(">=", "≥").strip()
    t = t[:1].upper() + t[1:]
    return t if len(t) <= 76 else t[:74].rsplit(" ", 1)[0] + "…"
HELP = (
    "Just ask in plain English, for example:\n"
    "- *What's next for the Humira case that's due tomorrow?*\n"
    "- *Why is 1077 escalated?* or, after opening a case, simply *What's still missing?*\n\n"
    "Or use commands: `/next-step 1077`, `/next-step` to search, `/cases` for your most urgent cases.\n\n"
    "For open questions across every source, use `/investigate`, e.g. "
    "*`/investigate which cases are waiting on records from Riverside Dermatology?`*"
)
INVESTIGATE_HELP = ("Ask an open question across every source (tracker, faxes, notes, Slack, knowledge base, SOP and policies), e.g.\n"
                    "- `/investigate which cases are waiting on records from Riverside Dermatology?`\n"
                    "- `/investigate has Bluepeak ever approved brand Humira on an office note alone?`")
AVATAR = {"assistant": ":material/assistant:", "user": "user"}

ui.inject()
st.markdown(
    "<style>[data-testid='stChatMessage'], [data-testid='stChatInput'] {max-width: 820px; margin-left: auto; margin-right: auto;}"
    ".block-container {max-width: 900px;}</style>",
    unsafe_allow_html=True,
)

if "chat" not in st.session_state:
    st.session_state.chat = [{"role": "assistant", "kind": "picker", "text": "Which case are you working on? Ask me in plain English, or pick one of your most urgent:", "cases": None}]
if "pending" not in st.session_state:
    st.session_state.pending = None


# ------------------------------------------------------------------ helpers


def case_line(cid: str) -> str:
    """One-line label, same wording as the queue: 'CASE-1077 · Humira · Bluepeak Commercial · Pended · Non-preferred drug · 38h left'."""
    c = engine.case_summary(skill, cid)
    h = c["hours"]
    left = "no deadline" if h is None else (f"{h:.0f}h left" if h < 72 else f"{h / 24:.0f} days left")
    return f"{cid} · {c['drug']} · {c['payer']} · {c['status']} · {left}"


def case_info(cid: str) -> dict:
    return engine.case_summary(skill, cid)


def by_urgency(ids: list[str]) -> list[str]:
    return sorted(ids, key=lambda cid: engine.gate(skill, engine.load_case(cid)).hours_left or 1e9)


def match_cases(query: str) -> list[str]:
    q = query.upper().replace("CASE-", "").replace("#", "").strip()
    return [cid for cid in engine.list_cases() if q and q in cid]


def set_focus(cid: str) -> None:
    st.session_state.focus = cid


def ask_for(cid: str) -> None:
    """Button callback: behaves as if the operator typed the command."""
    st.session_state.chat.append({"role": "user", "kind": "text", "text": f"/next-step {cid}"})
    st.session_state.pending = cid


def handle(text: str) -> None:
    st.session_state.chat.append({"role": "user", "kind": "text", "text": text})
    t = text.strip()
    cmd, _, arg = t.partition(" ")
    cmd = cmd.lower()
    if cmd in ("/next-step", "/next", "/ns") or (not t.startswith("/") and match_cases(t)):
        query = arg if t.startswith("/") else t
        if not query.strip():
            st.session_state.chat.append({"role": "assistant", "kind": "picker", "text": "Which case?", "cases": None})
            return
        found = match_cases(query)
        if len(found) == 1:
            st.session_state.pending = found[0]
        elif found:
            st.session_state.chat.append({"role": "assistant", "kind": "picker", "text": f"A few cases match **{query}**. Which one?", "cases": found})
        else:
            st.session_state.chat.append({"role": "assistant", "kind": "picker", "text": f"No case matches **{query}**. Pick one from the queue:", "cases": None})
    elif cmd in ("/investigate", "/inv"):
        q = t.split(" ", 1)[1].strip() if " " in t else ""
        if q:
            st.session_state.pending = ("inv", q)
        else:
            st.session_state.chat.append({"role": "assistant", "kind": "text", "text": INVESTIGATE_HELP})
    elif cmd == "/cases":
        st.session_state.chat.append({"role": "assistant", "kind": "picker", "text": "Your most urgent cases:", "cases": None})
    elif cmd == "/help" or t.startswith("/"):
        st.session_state.chat.append({"role": "assistant", "kind": "text", "text": HELP})
    else:  # plain English: AI works out which case and what they want
        st.session_state.pending = ("nl", t)


def get_run(cid: str, fresh: bool, on_step=None) -> dict:
    """Reuse the saved analysis while the case's information is unchanged; otherwise analyze again."""
    if not fresh:
        saved = engine.last_run(cid)
        if engine.is_current(saved, skill, engine.load_case(cid)) and "operator_summary" in saved["result"]:
            if on_step:
                on_step(f"Nothing new since the last analysis ({saved['ran_at'].replace('T', ' ')}), so I reused it", 0.0)
            return saved
    return engine.run_case(cid, skill, on_step=on_step)


def copy_button(text: str, key: str) -> None:
    components.html(
        f"""<link href="https://fonts.googleapis.com/css2?family=Inter:wght@600&display=swap" rel="stylesheet">
<style>html,body{{margin:0}} #b{{width:100%;height:40px;font:600 14.5px Inter,-apple-system,sans-serif;border:0;border-radius:10px;background:#4F46E5;color:#fff;cursor:pointer;transition:background .15s}} #b:hover{{background:#4338CA}}</style>
<button id="b">Copy message</button>
<script>
const t = {json.dumps(text)}, b = document.getElementById('b');
b.onclick = async () => {{
  try {{ await navigator.clipboard.writeText(t); }}
  catch (e) {{ const a = document.createElement('textarea'); a.value = t; document.body.appendChild(a); a.select(); document.execCommand('copy'); a.remove(); }}
  b.textContent = 'Copied ✓'; setTimeout(() => b.textContent = 'Copy message', 1500);
}};
</script>""",
        height=44,
    )


COMMANDS = [
    {"cmd": "/next-step", "hint": "Get the next step for a case", "takes_case": True},
    {"cmd": "/investigate", "hint": "An open question across every source", "takes_case": False, "takes_text": True},
    {"cmd": "/cases", "hint": "Your 3 most urgent cases", "takes_case": False},
    {"cmd": "/help", "hint": "How to use this", "takes_case": False},
]


def autocomplete() -> None:
    """Slash-command menu on st.chat_input, which has no autocomplete of its own.

    A zero-height component injects a small script into the page that watches the chat box:
    "/" lists commands, "/next-step 10" lists matching cases. Arrows/Tab/Enter or a click fills the box.
    """
    cases = [{"id": cid, "label": case_line(cid)} for cid in by_urgency(engine.list_cases())]
    components.html(
        """<script>
const P = window.parent, D = P.document;
P.__pansData = {commands: %s, cases: %s};
if (!P.__pansAttached) {
  P.__pansAttached = true;
  const menu = D.createElement('div');
  menu.id = 'pans-menu';
  menu.style.cssText = 'position:fixed;z-index:1000;display:none;background:#fff;border:1px solid #E6E8EC;border-radius:14px;box-shadow:0 12px 32px rgba(16,24,40,.14);padding:6px;font:14px Inter,-apple-system,sans-serif;max-height:340px;overflow-y:auto;';
  D.body.appendChild(menu);
  let items = [], active = 0, box = null, extra = 0;
  const MAX = 6;  // with thousands of cases, suggest a few and ask for more digits

  const setValue = (ta, v) => {
    Object.getOwnPropertyDescriptor(P.HTMLTextAreaElement.prototype, 'value').set.call(ta, v);
    ta.dispatchEvent(new P.Event('input', {bubbles: true}));
    ta.focus(); ta.setSelectionRange(v.length, v.length);
  };
  const hide = () => { menu.style.display = 'none'; items = []; shown = ''; };
  const suggest = v => {
    const {commands, cases} = P.__pansData;
    if (!v.startsWith('/')) return [];
    const sp = v.indexOf(' ');
    extra = 0;
    if (sp === -1) return commands.filter(c => c.cmd.startsWith(v.toLowerCase()))
      .map(c => ({main: c.cmd, sub: c.hint, fill: c.cmd + (c.takes_case || c.takes_text ? ' ' : '')}));
    const cmd = v.slice(0, sp).toLowerCase(), q = v.slice(sp + 1).toUpperCase().replace('CASE-', '').trim();
    if (!['/next-step', '/next', '/ns'].includes(cmd)) return [];
    const hits = cases.filter(c => c.id.includes(q));
    extra = Math.max(0, hits.length - MAX);
    return hits.slice(0, MAX).map(c => ({main: c.id, sub: c.label.split(' · ').slice(1).join(' · '), fill: '/next-step ' + c.id}));
  };
  let shown = '';
  const highlight = () => [...menu.children].slice(0, items.length).forEach((row, i) => row.style.background = i === active ? '#EEF0FF' : 'transparent');
  const draw = () => {
    const key = items.map(it => it.fill).join('|') + '#' + extra;
    if (key !== shown) {
      shown = key;
      menu.innerHTML = '';
      items.forEach((it, i) => {
        const row = D.createElement('div');
        row.style.cssText = 'padding:9px 12px;border-radius:9px;cursor:pointer;display:flex;gap:14px;align-items:baseline;';
        row.innerHTML = '<span style="font-weight:650;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13.5px;color:#4F46E5;min-width:96px"></span><span style="color:#667085;font-size:13px"></span>';
        row.children[0].textContent = it.main; row.children[1].textContent = it.sub;
        row.addEventListener('mousedown', e => { e.preventDefault(); accept(i); });
        row.addEventListener('mouseenter', () => { active = i; highlight(); });
        menu.appendChild(row);
      });
      if (extra) {
        const foot = D.createElement('div');
        foot.style.cssText = 'padding:8px 12px 6px;border-top:1px solid #E6E8EC;margin-top:4px;color:#667085;font-size:12.5px;';
        foot.textContent = '+' + extra.toLocaleString() + ' more \u00b7 keep typing the case number';
        menu.appendChild(foot);
      }
      const r = box.closest('[data-testid="stChatInput"]').getBoundingClientRect();
      Object.assign(menu.style, {display: 'block', left: r.left + 'px', width: r.width + 'px', bottom: (P.innerHeight - r.top + 8) + 'px'});
    }
    highlight();
  };
  const accept = i => { const it = items[i]; if (!it) return; setValue(box, it.fill); refresh(); };
  const refresh = () => {
    items = suggest(box.value);
    if (items.length === 1 && items[0].fill.trim() === box.value.trim()) items = [];
    active = Math.min(active, Math.max(items.length - 1, 0));
    items.length ? draw() : hide();
  };
  const isChat = t => t && t.matches && t.matches('[data-testid="stChatInputTextArea"]');
  let timer = null;
  D.addEventListener('input', e => {
    if (!isChat(e.target)) return;
    box = e.target; active = 0;
    clearTimeout(timer); timer = setTimeout(refresh, 40);
  }, true);
  D.addEventListener('focusin', e => { if (isChat(e.target)) { box = e.target; refresh(); } }, true);
  D.addEventListener('focusout', e => { if (isChat(e.target)) setTimeout(hide, 120); }, true);
  D.addEventListener('keydown', e => {
    if (!isChat(e.target) || !items.length) return;
    if (e.key === 'ArrowDown') { active = (active + 1) %% items.length; draw(); }
    else if (e.key === 'ArrowUp') { active = (active - 1 + items.length) %% items.length; draw(); }
    else if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) accept(active);
    else if (e.key === 'Escape') hide();
    else return;
    e.preventDefault(); e.stopPropagation();
  }, true);
}
</script>""" % (json.dumps(COMMANDS), json.dumps(cases)),
        height=0,
    )


def open_in_case_review(cid: str) -> None:
    st.session_state.wb_case = cid


# --------------------------------------------------------------- renderers


QUEUE_PREVIEW = 3  # a chat is not a queue screen: with hundreds of cases, show the few that matter and offer search


def render_picker(i: int, msg: dict) -> None:
    st.markdown(msg["text"])
    ids = by_urgency(msg["cases"] or engine.list_cases())
    shown = ids[:QUEUE_PREVIEW]
    count = "most urgent first" if msg["cases"] is None else f"{len(ids)} {'match' if len(ids) == 1 else 'matches'} · most urgent first"
    with st.container(key=f"pa-list-{i}"):
        st.markdown(f'<div class="pa-list-head"><span>Case</span><span>{count}</span></div>', unsafe_allow_html=True)
        for cid in shown:
            c = case_info(cid)
            with st.container(key=f"pa-row-{i}-{cid}"):
                left, mid, right = st.columns([4.4, 1.6, 1.2], vertical_alignment="center", gap="small")
                left.markdown(
                    f'<div class="pa-row-title">{c["id"]}<span>{escape(c["drug"])}</span></div>'
                    f'<div class="pa-row-sub">{escape(c["payer"])} · {escape(c["status"])}</div>',
                    unsafe_allow_html=True,
                )
                mid.markdown(f'<div style="text-align:right">{ui.deadline_pill(c["hours"])}</div>', unsafe_allow_html=True)
                right.button("Open →", key=f"pick-{i}-{cid}", on_click=ask_for, args=(cid,), type="tertiary", use_container_width=True)
        if len(ids) > len(shown):
            more = len(ids) - len(shown)
            hint = (f"{more:,} more {'case' if more == 1 else 'cases'} in the queue." if msg["cases"] is None
                    else f"{more:,} more {'match' if more == 1 else 'matches'}. Type more of the case number to narrow it down.")
            st.markdown(f'<div class="pa-list-foot">{hint}</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="pa-help" style="margin-top:10px">Looking for a different case? Type <span class="pa-kbd">/next-step</span> and its number, '
        'e.g. <span class="pa-kbd">/next-step 1077</span>.</div>',
        unsafe_allow_html=True,
    )


def render_result(i: int, msg: dict) -> None:
    run, cid = msg["run"], msg["case_id"]
    r = run["result"]
    s = r.get("operator_summary") or {
        "headline": engine.ACTION_LABELS[r["primary_action"]],
        "why": [r["action_rationale"]],
        "then": r["parallel_actions"][:3],
    }
    d, c = r["draft"], case_info(cid)
    tone, kicker = ("escalate", "Escalate · don't act yet") if r["escalate"] else ("go", "Ready to act")

    with st.container(key=f"pa-card-{i}"):
        st.markdown(
            f'<div class="pa-meta"><b>{cid}</b> {escape(c["drug"])} · {escape(c["payer"])} · {escape(c["status"])} {ui.deadline_pill(c["hours"])}</div>'
            f'<div class="pa-step {tone}"><div class="pa-kicker">{kicker}</div>'
            f'<div class="pa-headline">{escape(s["headline"])}</div>'
            f'<div class="pa-sendto"><span>Send to</span>{escape(d["to"])}</div></div>'
            + ui.label("Why") + ui.bullet_list(s["why"], "why"),
            unsafe_allow_html=True,
        )
        st.markdown(ui.label("Draft message"), unsafe_allow_html=True)
        message = f"Subject: {d['subject']}\n\n{d['body']}"
        edited = st.text_area("Draft message", message, height=240, key=f"draft-{i}", label_visibility="collapsed")
        for note in run["guardrail_notes"]:
            st.markdown(f'<div class="pa-warn">{escape(note)}</div>', unsafe_allow_html=True)
        a, b, _ = st.columns([1.3, 1.3, 2.4], vertical_alignment="center")
        with a:
            copy_button(edited, key=f"copy-{i}")
        sent = msg.get("logged", False)
        if b.button("Sent ✓" if sent else "Mark as sent", key=f"sent-{i}", use_container_width=True, disabled=sent):
            engine.log_decision(cid, run, "accept", edited_draft=edited, view="chat")
            msg["logged"] = True
            st.toast("Logged as sent. See the Decision log.")
            st.rerun()

        if s["then"]:
            st.markdown(ui.label("After you send it") + ui.bullet_list(s["then"], "todo"), unsafe_allow_html=True)
        st.write("")

        met = sum(c["status"] == "met" for c in r["criteria"])
        with st.expander(f"Details · {met} of {len(r['criteria'])} requirements met"):
            rows = "".join(
                f'<div class="pa-req">{ui.pill(*REQ_PILL[c["status"]])}<span class="pa-req-text">{escape(short_criterion(c["criterion"]))}</span>'
                f'<span>{" ".join(f"<span class=pa-chip>{escape(x)}</span>" for x in c["source_ids"][:2])}</span></div>'
                for c in r["criteria"])
            unknown = r["could_not_determine"][:3]
            st.markdown(
                ui.label("Requirements") + rows
                + (ui.label("Still unknown") + ui.bullet_list(unknown, "why") if unknown else "")
                + '<div class="pa-sub" style="margin:14px 0 8px">Full evidence for each requirement is in Case review.</div>',
                unsafe_allow_html=True,
            )
            c1, c2, c3 = st.columns(3)
            with c1.popover("Override", use_container_width=True):
                new_action = st.selectbox("Correct action", m["actions"], index=m["actions"].index(r["primary_action"]),
                                          format_func=engine.ACTION_LABELS.get, key=f"ov-act-{i}")
                reason = st.text_area("Why? (becomes a new eval case)", key=f"ov-why-{i}")
                if st.button("Log override", key=f"ov-btn-{i}", type="primary", disabled=not reason.strip()):
                    engine.log_decision(cid, run, "override", reason=reason, final_action=new_action, edited_draft=edited, view="chat")
                    st.toast("Override logged. Review it in the Decision log.")
            if c2.button("Case review", key=f"wb-{i}", help="Open every source and the full evidence", on_click=open_in_case_review, args=(cid,), use_container_width=True):
                st.switch_page("case_review.py")
            if c3.button("Re-run", key=f"rerun-{i}", help="Analyze this case again from scratch", disabled=not has_key, use_container_width=True):
                st.session_state.chat.append({"role": "user", "kind": "text", "text": f"/next-step {cid} (re-run)"})
                st.session_state.pending = ("fresh", cid)
                st.rerun()
            st.caption(f"Analyzed {run['ran_at'][5:16].replace('T', ' ')} · confidence {r['confidence']}")


def render_answer(i: int, msg: dict) -> None:
    cid, a, c = msg["case_id"], msg["answer"], case_info(msg["case_id"])
    chips = " ".join(f'<span class="pa-chip">{escape(x)}</span>' for x in a["source_ids"])
    with st.container(key=f"pa-card-answer-{i}"):
        st.markdown(
            f'<div class="pa-meta"><b>{cid}</b> {escape(c["drug"])} · {escape(c["payer"])} · {escape(c["status"])} {ui.deadline_pill(c["hours"])}</div>'
            f'<div class="pa-answer">{escape(a["answer"])}</div>'
            + (f'<div class="pa-pills" style="margin-top:10px"><span class="pa-sub">From</span> {chips}</div>' if chips else "")
            + ("" if a["found"] else '<div class="pa-sub" style="margin-top:8px">The case documents don\u2019t answer this directly.</div>'),
            unsafe_allow_html=True,
        )
        st.button("See next steps for this case →", key=f"ans-next-{i}", on_click=ask_for, args=(cid,), type="tertiary")


TRUST_TONE = {"official insurer policy": "green", "official SOP": "green", "case record": "accent", "operator note": "",
              "unverified (Slack / team knowledge)": "amber"}


def render_investigation(i: int, msg: dict) -> None:
    r = msg["result"]
    with st.container(key=f"pa-card-inv-{i}"):
        st.markdown(f'<div class="pa-kicker">Investigation · {r["confidence"]} confidence</div>'
                    f'<div class="pa-row-title" style="margin:4px 0 8px">{escape(r["question"])}</div>', unsafe_allow_html=True)
        if r.get("unverified_only"):
            st.markdown('<div class="pa-warn" style="margin-bottom:8px">This rests only on Slack and team knowledge: unverified.</div>',
                        unsafe_allow_html=True)
        st.markdown(r["answer"])
        row = lambda s: (f'<div class="pa-req"><span class="pa-chip">{escape(s["id"])}</span>{ui.pill(s["trust"].split(" (")[0], TRUST_TONE.get(s["trust"], ""))}'
                         f'<span class="pa-req-text">{escape(s["supports"])}<br><span class="pa-sub">{escape(s["source"])}'
                         f'{" · " + escape(s["case"]) if s["case"] else ""}{" · " + s["date"] if s["date"] else ""}</span></span></div>')
        if r["sources"]:
            st.markdown(ui.label(f"Sources · {len(r['sources'])}") + "".join(map(row, r["sources"][:5])), unsafe_allow_html=True)
            if len(r["sources"]) > 5:
                with st.expander(f"{len(r['sources']) - 5} more sources"):
                    st.markdown("".join(map(row, r["sources"][5:])), unsafe_allow_html=True)
        if r.get("gaps"):
            st.markdown(ui.label("What the sources don't show") + ui.bullet_list(r["gaps"][:3], "why"), unsafe_allow_html=True)
        cases = [c for c in r.get("cases", []) if c in set(engine.list_cases())][:4]
        if cases:
            st.markdown(ui.label("Next step for"), unsafe_allow_html=True)
            for col, cid in zip(st.columns(4), cases):
                col.button(f"{cid} →", key=f"inv-next-{i}-{cid}", on_click=ask_for, args=(cid,), use_container_width=True,
                           help=f"Get the next step for {cid}")
        u = r["usage"]
        with st.expander(f"Action log · {len(r['log'])} steps · {u['seconds']}s"):
            st.markdown("".join(ui.log_line(x["text"], x["t"]) for x in r["log"]), unsafe_allow_html=True)


# --------------------------------------------------------------------- page

with st.sidebar:
    st.markdown(
        '<div class="pa-label" style="margin-top:4px">Just ask</div><div class="pa-help" style="line-height:1.6">'
        '\u201cWhat\u2019s next for the Humira case due tomorrow?\u201d<br>\u201cWhy is 1077 escalated?\u201d<br>\u201cWhat\u2019s still missing?\u201d</div>'
        '<div class="pa-label">Commands</div><div class="pa-help">'
        '<span class="pa-kbd">/next-step 1077</span> next step for a case<br>'
        '<span class="pa-kbd">/next-step</span> search the queue<br>'
        '<span class="pa-kbd">/investigate</span> ask across every source<br>'
        '<span class="pa-kbd">/cases</span> most urgent cases<br>'
        '<span class="pa-kbd">/help</span> how this works</div>',
        unsafe_allow_html=True,
    )
    st.markdown(f'<div class="pa-sub" style="margin:14px 0 10px">Skill {m["id"]} · v{m["version"]}</div>', unsafe_allow_html=True)
    if st.button("Clear chat", use_container_width=True):
        del st.session_state.chat
        st.rerun()
    if not has_key:
        st.warning("No ANTHROPIC_API_KEY set: only saved analyses can be shown.")

ui.header("Next Step", "Prior-auth exceptions · Access Operations")

for i, msg in enumerate(st.session_state.chat):
    with st.chat_message(msg["role"], avatar=AVATAR[msg["role"]]):
        if msg["kind"] == "picker":
            render_picker(i, msg)
        elif msg["kind"] == "result":
            if msg.get("intro"):
                st.markdown(msg["intro"])
            render_result(i, msg)
        elif msg["kind"] == "answer":
            render_answer(i, msg)
        elif msg["kind"] == "investigation":
            render_investigation(i, msg)
        elif msg["kind"] == "manual":
            if msg.get("intro"):
                st.markdown(msg["intro"])
            c = case_info(msg["case_id"])
            with st.container(key=f"pa-card-manual-{i}"):
                st.markdown(
                    f'<div class="pa-meta"><b>{c["id"]}</b> {escape(c["drug"])} · {escape(c["payer"])} · {escape(c["status"])} {ui.deadline_pill(c["hours"])}</div>'
                    + ui.manual_card(msg["notes"]),
                    unsafe_allow_html=True,
                )
        else:
            st.markdown(msg["text"])


def run_next_step(cid: str, fresh: bool, intro: str = "") -> None:
    saved = engine.last_run(cid)
    updated = bool(saved) and saved.get("status") == "ok" and not engine.is_current(saved, skill, engine.load_case(cid))
    with st.status(f"Starting on {cid}…", expanded=True) as status:
        try:
            run = get_run(cid, fresh, on_step=ui.step_logger(status))
            if updated and not fresh:
                intro = (intro + " " if intro else "") + "New documents or messages arrived since the last analysis, so I analyzed it again."
            status.update(label="Done", state="complete")
            if run.get("status") == "not_applicable":
                st.session_state.chat.append({"role": "assistant", "kind": "manual", "case_id": cid, "notes": run["gate"].get("notes", []), "intro": intro})
            else:
                st.session_state.chat.append({"role": "assistant", "kind": "result", "case_id": cid, "run": run, "intro": intro})
            set_focus(cid)
        except Exception as e:
            status.update(label="Failed", state="error")
            st.session_state.chat.append({"role": "assistant", "kind": "text", "text": f"Sorry, the analysis failed: `{e}`"})


def run_investigation(question: str) -> None:
    import investigate
    holder = {}
    if ui.run_with_log(lambda log: holder.update(r=investigate.investigate(question, on_step=log))):
        st.session_state.chat.append({"role": "assistant", "kind": "investigation", "result": holder["r"]})
    else:
        st.session_state.chat.append({"role": "assistant", "kind": "text", "text": "Sorry, that investigation didn't finish. Try again, or narrow the question."})


def run_plain_english(text: str) -> None:
    focus = st.session_state.get("focus")
    with st.status("Working out which case you mean…") as status:
        try:
            r = chat_ai.route(skill, text, focus)
        except Exception as e:
            status.update(label="Failed", state="error")
            st.session_state.chat.append({"role": "assistant", "kind": "text", "text": f"Sorry, I couldn't read that: `{e}`"})
            return
        status.update(label="Got it", state="complete")
    intent, cid = r["intent"], r["case_id"]
    if intent == "urgent":
        st.session_state.chat.append({"role": "assistant", "kind": "picker", "text": r["reply"] or "Your most urgent cases:", "cases": None})
    elif intent == "help":
        st.session_state.chat.append({"role": "assistant", "kind": "text", "text": HELP})
    elif intent in ("next_step", "question") and cid:
        if intent == "next_step":
            run_next_step(cid, fresh=False, intro=r["reply"])
        else:
            with st.status(f"Checking {cid}'s documents…") as status:
                try:
                    a = chat_ai.answer(skill, cid, text)
                    status.update(label="Done", state="complete")
                    st.session_state.chat.append({"role": "assistant", "kind": "answer", "case_id": cid, "answer": a})
                    set_focus(cid)
                except Exception as e:
                    status.update(label="Failed", state="error")
                    st.session_state.chat.append({"role": "assistant", "kind": "text", "text": f"Sorry, I couldn't answer that: `{e}`"})
    elif intent in ("next_step", "question") and r["other_case_ids"]:
        st.session_state.chat.append({"role": "assistant", "kind": "picker", "text": r["reply"] or "A few cases could fit. Which one?",
                                      "cases": r["other_case_ids"][:QUEUE_PREVIEW]})
    else:
        st.session_state.chat.append({"role": "assistant", "kind": "text", "text": r["reply"] or HELP})


if st.session_state.pending:
    pending = st.session_state.pending
    st.session_state.pending = None
    with st.chat_message("assistant", avatar=AVATAR["assistant"]):
        if isinstance(pending, tuple) and pending[0] == "nl":
            run_plain_english(pending[1])
        elif isinstance(pending, tuple) and pending[0] == "inv":
            run_investigation(pending[1])
        else:
            fresh, cid = (True, pending[1]) if isinstance(pending, tuple) else (False, pending)
            run_next_step(cid, fresh)
    st.rerun()

autocomplete()
prompt = st.chat_input("Ask about a case in plain English, or type / for commands")
if prompt:
    handle(prompt)
    st.rerun()
