"""Connect Slack page: create the Slack app from our manifest in one click, paste two tokens, and the @kb bot
runs inside this server. Opened from app.py."""

from __future__ import annotations

from html import escape

import streamlit as st

import slack_connect
import ui

ui.inject()
ui.header("Connect Slack", "Run the @kb bot in your own Slack workspace: about 3 minutes, nothing to install")

st.markdown('<div class="pa-warn" style="margin-bottom:14px">Use a test workspace. This demo runs on synthetic cases: '
            "don’t post real patient information in the channel you connect.</div>", unsafe_allow_html=True)

# ------------------------------------------------------------------ status

bots, own = slack_connect.connected(), slack_connect.server_bot()
with st.container(key="pa-card-slack-status"):
    if own:
        st.markdown(f'<div class="pa-row-title">{ui.pill("Connected", "green")} {escape(own["team"])} <span>as @{escape(own["bot_user"])}</span></div>'
                    f'<div class="pa-sub">Since {own["since"]}, from the server’s own settings. Connect another workspace below.</div>',
                    unsafe_allow_html=True)
    if not bots and not own:
        st.markdown('<div class="pa-row-title">Not connected</div><div class="pa-sub">Follow the three steps below.</div>',
                    unsafe_allow_html=True)
    for team_id, b in bots.items():
        a, c = st.columns([4, 1], vertical_alignment="center")
        a.markdown(f'<div class="pa-row-title">{ui.pill("Connected", "green")} {escape(b["team"])} '
                   f'<span>as @{escape(b["bot_user"])}</span></div>'
                   f'<div class="pa-sub">Since {b["since"]}, from {escape(b["source"])}.</div>', unsafe_allow_html=True)
        if c.button("Disconnect", key=f"dc-{team_id}", use_container_width=True):
            slack_connect.disconnect(team_id)
            st.rerun()

# ------------------------------------------------------------------ steps

with st.container(key="pa-card-slack-1"):
    a, b = st.columns([3, 1.2], vertical_alignment="center")
    a.markdown('<div class="pa-row-title">1 · Create the Slack app</div>'
               '<div class="pa-sub">Opens Slack with this app’s settings filled in. Pick your workspace, then Next → Create.</div>',
               unsafe_allow_html=True)
    b.link_button("Create the app", slack_connect.create_app_url(), type="primary", use_container_width=True)

with st.container(key="pa-card-slack-2"):
    st.markdown('<div class="pa-row-title">2 · Copy two tokens from the app’s Slack page</div>'
                '<div class="pa-sub" style="margin-top:6px"><b>App-level token</b> (starts with <code>xapp-</code>): '
                'Basic Information → App-Level Tokens → Generate Token and Scopes → add <code>connections:write</code> → Generate.</div>'
                '<div class="pa-sub" style="margin-top:4px"><b>Bot token</b> (starts with <code>xoxb-</code>): '
                'Install App → Install to Workspace → Allow → Bot User OAuth Token.</div>', unsafe_allow_html=True)

with st.container(key="pa-card-slack-3"):
    st.markdown('<div class="pa-row-title">3 · Paste them here</div>'
                '<div class="pa-sub">Kept in this server’s memory only, never saved. A server restart disconnects.</div>',
                unsafe_allow_html=True)
    with st.form("slack-tokens", border=False, clear_on_submit=True):
        bot_token = st.text_input("Bot token", type="password", placeholder="xoxb-…")
        app_token = st.text_input("App-level token", type="password", placeholder="xapp-…")
        if st.form_submit_button("Connect", type="primary"):
            try:
                with st.spinner("Connecting to Slack…"):
                    bot = slack_connect.connect(bot_token, app_token)
                st.session_state.slack_ok = f"Connected to {bot['team']}. Now invite @{bot['bot_user']} to a channel."
                st.rerun()
            except ValueError as e:
                st.error(str(e))

if note := st.session_state.pop("slack_ok", None):
    st.success(note)

# ------------------------------------------------------------------ try it

st.markdown(ui.label("Then try it") + """
<table class="pa-table"><thead><tr><th>In a channel, type</th><th>What happens</th></tr></thead><tbody>
<tr><td><code>/invite @kb</code></td><td>Adds the bot to the channel (do this first)</td></tr>
<tr><td><code>@kb what's next for the Humira case due tomorrow?</code></td><td>The next step, who to send it to, why, a draft, and Mark as sent / Override buttons</td></tr>
<tr><td><code>@kb why is it escalated?</code> (same thread)</td><td>Answers about the case that thread is about</td></tr>
<tr><td>Attach a fax, PDF, photo or email and mention <code>@kb</code></td><td>Reads it, pulls out the fields and matches it to its case</td></tr>
<tr><td>Start a thread, then reply <code>@kb add</code></td><td>Saves the thread to the team knowledge base</td></tr>
<tr><td><code>/investigate which cases are waiting on Riverside records?</code></td><td>An open question across every source, with its sources and their trust level</td></tr>
<tr><td><code>/next-step 1077</code></td><td>The same next step, visible only to you</td></tr>
<tr><td><code>/kb-search biosimilar intolerance</code></td><td>Searches the knowledge base by meaning</td></tr>
</tbody></table>
<div class="pa-sub" style="margin-top:8px">Saved threads show up on the Knowledge base page; Mark as sent and Override land in the Decision log.</div>
""", unsafe_allow_html=True)
