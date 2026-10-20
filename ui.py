"""Shared look for every page: one stylesheet plus small HTML helpers (pills, page header, answer card)."""

from __future__ import annotations

from html import escape

import streamlit as st

CSS = """
<style>
:root {
  --pa-ink: #1B1D23; --pa-muted: #667085; --pa-line: #E6E8EC; --pa-card: #FFFFFF;
  --pa-accent: #4F46E5; --pa-accent-soft: #EEF0FF;
  --pa-red: #B42318; --pa-red-soft: #FEF0EF; --pa-amber: #B54708; --pa-amber-soft: #FFF6E9;
  --pa-green: #067647; --pa-green-soft: #EAF7EF;
}
header[data-testid="stHeader"] { background: transparent; }
/* hide Deploy and the main menu, but keep the toolbar: the sidebar's reopen button lives in it */
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"], [data-testid="stDecoration"] { display: none; }
.block-container { padding-top: 2.2rem; padding-bottom: 7rem; max-width: 1080px; }
[data-testid="stSidebar"] { border-right: 1px solid var(--pa-line); }
[data-testid="stSidebarNav"] a span { font-weight: 500; }

/* page header */
.pa-brand { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.pa-logo { width: 36px; height: 36px; border-radius: 10px; background: var(--pa-accent); color: #fff;
  display: grid; place-items: center; font-weight: 700; font-size: 14px; letter-spacing: .02em; }
.pa-title { font-size: 20px; font-weight: 650; color: var(--pa-ink); line-height: 1.2; }
.pa-sub { font-size: 13px; color: var(--pa-muted); }

/* pills */
.pa-pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 600; background: #F1F2F5; color: #3B3F4A; white-space: nowrap; }
.pa-pill.red { background: var(--pa-red-soft); color: var(--pa-red); }
.pa-pill.amber { background: var(--pa-amber-soft); color: var(--pa-amber); }
.pa-pill.green { background: var(--pa-green-soft); color: var(--pa-green); }
.pa-pill.accent { background: var(--pa-accent-soft); color: var(--pa-accent); }
.pa-dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }

/* cards: any keyed container whose key starts with pa-card */
[class*="st-key-pa-card"] { margin-bottom: 8px; }
[class*="st-key-pa-card"] { background: var(--pa-card); border: 1px solid var(--pa-line); border-radius: 16px;
  padding: 20px 22px 16px; box-shadow: 0 1px 2px rgba(16,24,40,.04), 0 8px 24px rgba(16,24,40,.04); }
/* inbox list: one card, hairline rows */
[class*="st-key-pa-list"] { background: var(--pa-card); border: 1px solid var(--pa-line); border-radius: 14px;
  padding: 4px 6px 6px; gap: 0 !important; box-shadow: 0 1px 2px rgba(16,24,40,.04); }
.pa-list-head { display: flex; justify-content: space-between; padding: 10px 14px 8px; font-size: 11.5px; font-weight: 650;
  letter-spacing: .06em; text-transform: uppercase; color: var(--pa-muted); }
[class*="st-key-pa-row"] { border-top: 1px solid var(--pa-line); border-radius: 0; padding: 10px 8px 10px 14px; transition: background .12s; }
[class*="st-key-pa-row"]:hover { background: #F8F9FC; border-radius: 10px; }
[class*="st-key-pa-row"]:hover + [class*="st-key-pa-row"] { border-top-color: transparent; }
[class*="st-key-pa-row"] [data-testid="stHorizontalBlock"] { gap: 12px; }
[class*="st-key-pa-row"] .stButton button { min-height: 32px; color: var(--pa-accent); font-weight: 600; justify-content: flex-end; padding: 0 6px; }
[class*="st-key-pa-row"] .stButton button:hover { color: #3730A3; }
[class*="st-key-pa-row"] .stButton button p { white-space: nowrap; }
.pa-req { display: flex; align-items: center; gap: 10px; padding: 7px 0; border-bottom: 1px solid var(--pa-line); font-size: 14px; }
.pa-req:last-of-type { border-bottom: 0; }
.pa-req .pa-pill { min-width: 66px; justify-content: center; }
.pa-req-text { flex: 1; color: var(--pa-ink); }
.pa-three.pa-five { grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }
.pa-feeds { margin-top: 8px; font-size: 12px; font-weight: 600; color: var(--pa-accent); }
.pa-section-head { display: flex; flex-direction: column; gap: 2px; margin: 16px 0 8px; padding-left: 10px; border-left: 3px solid var(--pa-accent); }
.pa-section-head b { font-size: 14.5px; color: var(--pa-ink); }
.pa-three { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 18px; }
.pa-src { background: var(--pa-card); border: 1px solid var(--pa-line); border-radius: 12px; padding: 12px 14px; font-size: 14px; }
.pa-src.accent { border-color: #C7CBF7; background: #FAFAFF; }
.pa-src .pa-kicker { color: var(--pa-muted); margin-bottom: 4px; }
.pa-src .pa-sub { margin-top: 4px; }
.pa-slack { font-size: 14px; line-height: 1.5; padding: 6px 0; }
.pa-slack.reply { margin-left: 22px; padding-left: 12px; border-left: 2px solid var(--pa-line); }
.pa-answer { font-size: 15px; line-height: 1.6; color: var(--pa-ink); margin-top: 12px; }
.pa-flow { display: flex; gap: 6px; flex-wrap: wrap; margin: 2px 0 14px; }
.pa-flow div { flex: 1 1 130px; background: var(--pa-card); border: 1px solid var(--pa-line); border-radius: 10px; padding: 9px 11px; font-size: 12.5px; color: var(--pa-muted); line-height: 1.4; }
.pa-flow b { display: block; color: var(--pa-ink); font-size: 13px; margin-bottom: 2px; }
.pa-fields { width: 100%; border-collapse: collapse; font-size: 13.5px; margin-bottom: 6px; }
.pa-fields td { padding: 6px 4px; border-bottom: 1px solid var(--pa-line); vertical-align: top; color: var(--pa-ink); }
.pa-fields td:first-child { color: var(--pa-muted); width: 34%; white-space: nowrap; }
.pa-fields .empty { color: var(--pa-muted); font-style: italic; }
.pa-email { background: var(--pa-card); border: 1px solid var(--pa-line); border-radius: 12px; padding: 12px 14px; font-size: 13.5px; white-space: pre-wrap; color: var(--pa-ink); }
.pa-log { display: flex; gap: 10px; font-size: 13.5px; color: var(--pa-ink); padding: 2px 0; line-height: 1.45; }
.pa-log span { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--pa-muted); min-width: 42px; text-align: right; padding-top: 1px; }
.pa-list-foot { border-top: 1px solid var(--pa-line); padding: 10px 14px 6px; font-size: 13px; color: var(--pa-muted); }
/* Streamlit gives markdown a negative bottom margin; inside our own blocks it clips content */
[class*="st-key-pa-"] [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
.pa-row-title { font-weight: 650; color: var(--pa-ink); font-size: 14.5px; line-height: 1.35; }
.pa-row-title span { font-weight: 500; color: var(--pa-muted); margin-left: 6px; }
.pa-row-sub { font-size: 13px; color: var(--pa-muted); margin-top: 2px; line-height: 1.4; }

/* answer card */
.pa-meta { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; color: var(--pa-muted); }
.pa-meta b { color: var(--pa-ink); font-weight: 650; }
.pa-step { border-radius: 12px; padding: 16px 18px; margin: 14px 0 6px; border: 1px solid; }
.pa-step.escalate { background: var(--pa-amber-soft); border-color: #FBD9A6; }
.pa-step.go { background: var(--pa-green-soft); border-color: #B7E4C7; }
.pa-step.manual { background: #F4F5F8; border-color: var(--pa-line); }
.pa-step.manual .pa-kicker { color: var(--pa-muted); }
.pa-kicker { font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
.pa-step.escalate .pa-kicker { color: var(--pa-amber); }
.pa-step.go .pa-kicker { color: var(--pa-green); }
.pa-headline { font-size: 21px; font-weight: 650; color: var(--pa-ink); margin: 4px 0 10px; line-height: 1.25; }
.pa-sendto { font-size: 14px; color: var(--pa-ink); }
.pa-sendto span { color: var(--pa-muted); margin-right: 6px; }
.pa-label { font-size: 12px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; color: var(--pa-muted); margin: 18px 0 8px; }
.pa-list { list-style: none; padding: 0; margin: 0; }
.pa-list li { position: relative; padding: 3px 0 3px 26px; font-size: 14.5px; color: var(--pa-ink); line-height: 1.5; }
.pa-list.why li::before { content: ""; position: absolute; left: 6px; top: 11px; width: 7px; height: 7px; border-radius: 50%; background: var(--pa-accent); }
.pa-list.todo li::before { content: ""; position: absolute; left: 2px; top: 6px; width: 14px; height: 14px; border-radius: 4px; border: 1.5px solid #B8BDC8; }
.pa-warn { background: var(--pa-amber-soft); color: var(--pa-amber); border-radius: 10px; padding: 8px 12px; font-size: 13px; margin: 6px 0; }

/* case review */
.pa-case-title { font-size: 26px; font-weight: 700; color: var(--pa-ink); letter-spacing: -.01em; margin: 4px 0 8px; }
.pa-case-title span { font-weight: 500; color: var(--pa-muted); margin-left: 10px; font-size: 20px; }
.pa-pills { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
.pa-chip { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; background: var(--pa-accent-soft); color: var(--pa-accent);
  border-radius: 6px; padding: 1px 6px; white-space: nowrap; display: inline-block; margin: 1px 0; }
.pa-table { width: 100%; border-collapse: separate; border-spacing: 0; background: var(--pa-card); border: 1px solid var(--pa-line);
  border-radius: 12px; overflow: hidden; font-size: 13.5px; margin-top: 6px; }
.pa-table th { text-align: left; font-size: 11.5px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; color: var(--pa-muted);
  background: #FAFBFC; padding: 10px 14px; border-bottom: 1px solid var(--pa-line); }
.pa-table td { padding: 12px 14px; border-bottom: 1px solid var(--pa-line); vertical-align: top; color: var(--pa-ink); line-height: 1.5; }
.pa-table tr:last-child td { border-bottom: 0; }
.pa-table td.crit { font-weight: 600; width: 30%; }
[class*="st-key-pa-doc"] { max-height: 340px; overflow-y: auto; font-size: 13.5px; line-height: 1.55; padding-right: 6px; }
[class*="st-key-pa-doc"] h1 { font-size: 16px !important; font-weight: 650; margin: 0 0 6px; padding: 0; }
[class*="st-key-pa-doc"] h2 { font-size: 14.5px !important; font-weight: 650; margin: 14px 0 4px; padding: 0; }
[class*="st-key-pa-doc"] h3 { font-size: 13.5px !important; font-weight: 650; margin: 10px 0 4px; padding: 0; }
[class*="st-key-pa-doc"] p, [class*="st-key-pa-doc"] li { font-size: 13.5px !important; }
[class*="st-key-pa-doc"] table { font-size: 12.5px; }
[data-testid="stTabs"] [data-baseweb="tab"] p { font-weight: 600; font-size: 14px; }

/* widgets */
.stButton > button, [data-testid="stPopover"] button { font-weight: 600; min-height: 40px; }
[data-testid="stTextArea"] textarea { font-size: 14px; line-height: 1.55; background: #FBFBFC; }
[data-testid="stTextArea"] label p { font-size: 12px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; color: var(--pa-muted); }
[data-testid="stExpander"] details { border-radius: 12px; border-color: var(--pa-line); background: #FCFCFD; }
[data-testid="stExpander"] summary p { font-weight: 600; font-size: 14px; }
[data-testid="stMetric"] { background: var(--pa-card); border: 1px solid var(--pa-line); border-radius: 14px; padding: 14px 16px; }
[data-testid="stMetricLabel"] p { color: var(--pa-muted); font-weight: 600; font-size: 13px; }

/* chat */
[data-testid="stChatMessage"] { background: transparent; padding: 6px 0; gap: 12px; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) { flex-direction: row-reverse; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageAvatarUser"] { display: none; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] {
  flex: 0 1 auto; background: var(--pa-accent); color: #fff; border-radius: 16px 16px 4px 16px; padding: 8px 14px; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] { padding: 9px 16px; margin-left: auto; margin-right: 0; }
[data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"] p { color: #fff; margin: 0; font-size: 14.5px; }
[data-testid="stChatInput"] { border-radius: 16px; box-shadow: 0 6px 24px rgba(16,24,40,.08); }
[data-testid="stBottomBlockContainer"] { background: linear-gradient(to top, #F6F7F9 70%, rgba(246,247,249,0)); }
.pa-kbd { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; background: #F1F2F5; border: 1px solid var(--pa-line);
  border-radius: 6px; padding: 1px 6px; color: var(--pa-ink); }
.pa-help { font-size: 13.5px; color: var(--pa-muted); line-height: 1.9; }
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def header(title: str, subtitle: str) -> None:
    st.markdown(
        f'<div class="pa-brand"><div class="pa-logo">PA</div><div><div class="pa-title">{escape(title)}</div>'
        f'<div class="pa-sub">{escape(subtitle)}</div></div></div>',
        unsafe_allow_html=True,
    )


def pill(text: str, tone: str = "") -> str:
    dot = '<span class="pa-dot"></span>' if tone in ("red", "amber", "green") else ""
    return f'<span class="pa-pill {tone}">{dot}{escape(text)}</span>'


def deadline_pill(hours: float | None) -> str:
    if hours is None:
        return pill("no deadline")
    text = f"{hours:.0f}h left" if hours < 72 else f"{hours / 24:.0f} days left"
    return pill(text, "red" if hours < 48 else "amber" if hours < 24 * 7 else "green")


def bullet_list(items: list[str], kind: str) -> str:
    return f'<ul class="pa-list {kind}">' + "".join(f"<li>{escape(x)}</li>" for x in items) + "</ul>"


def label(text: str) -> str:
    return f'<div class="pa-label">{escape(text)}</div>'


def manual_card(notes: list[str], margin_top: int = 14) -> str:
    """Neutral card for a case the tool declines: not an error, just outside what it knows."""
    body = " ".join(escape(n) for n in notes) or "This case is outside what this tool covers."
    return (f'<div class="pa-step manual" style="margin-top:{margin_top}px"><div class="pa-kicker">Handle this case manually</div>'
            f'<div class="pa-headline">No recommendation for this case</div>'
            f'<div class="pa-sendto">{body}</div>'
            f'<div class="pa-sub" style="margin-top:8px">Work it from the insurer\'s own policy, or ask your Team Lead.</div></div>')


def log_line(text: str, t: float) -> str:
    return f'<div class="pa-log"><span>{t:.1f}s</span>{escape(text)}</div>'


def step_logger(box):
    """on_step callback for engine.run_case: each action becomes a timed line in a st.status box, and its label.
    The lines are one block redrawn on each step, so they keep even spacing and wrap cleanly."""
    lines, area = [], box.empty()

    def on_step(text: str, t: float) -> None:
        lines.append(log_line(text, t))
        area.markdown("".join(lines), unsafe_allow_html=True)
        box.update(label=f"{text}…", expanded=True)
    return on_step


def run_with_log(work) -> bool:
    """Run work(on_step) inside a live action log. Returns False (with the reason shown) if it failed."""
    with st.status("Starting…", expanded=True) as box:
        try:
            work(step_logger(box))
        except Exception as e:
            box.update(label="Couldn't finish", state="error", expanded=True)
            box.markdown(f'<div class="pa-warn">{escape(str(e))}</div>', unsafe_allow_html=True)
            return False
        box.update(label="Done", state="complete")
    return True
