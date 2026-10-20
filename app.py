"""PA Next Step.  Run:  .venv/bin/streamlit run app.py

Chat is what operators use day to day. Case review shows everything behind a
recommendation (sources, criteria, evals) for reviewers and skill owners.
Decision log shows what operators did with each recommendation, and why they overrode it.
Connect Slack runs the @kb bot inside this server, for a hosted demo.

Hosted, it reads APP_ACCESS_CODE (a shared code for the link) and MAX_MODEL_CALLS_PER_DAY from the environment;
see DEPLOY.md.
"""

import hashlib
import hmac
import os

import streamlit as st
import streamlit.components.v1 as components

import ui

st.set_page_config(page_title="PA Next Step", layout="wide")

code = os.environ.get("APP_ACCESS_CODE", "")
remember = hashlib.sha256(f"pa-next-step:{code}".encode()).hexdigest()[:32]  # the cookie holds this, not the code
if code and not st.session_state.get("authed") and hmac.compare_digest(st.context.cookies.get("pa_access", ""), remember):
    st.session_state.authed = True
if code and not st.session_state.get("authed"):
    ui.inject()
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid, st.container(key="pa-card-login"):
        ui.header("PA Next Step", "Next steps for denied and pended prior authorizations")
        st.markdown('<div class="pa-sub" style="margin:6px 0 4px">A prototype on synthetic cases. Enter the access code you were given.</div>',
                    unsafe_allow_html=True)
        with st.form("login", border=False):
            entered = st.text_input("Access code", type="password")
            if st.form_submit_button("Open", type="primary", use_container_width=True):
                if hmac.compare_digest(entered.strip(), code):
                    # remember this browser for 30 days, so links and reloads don't ask again
                    components.html(f"<script>window.parent.document.cookie = 'pa_access={remember}; path=/; max-age=2592000; SameSite=Lax';"
                                    "window.parent.location.reload();</script>", height=0)
                    st.stop()
                st.error("That code didn’t work.")
    st.stop()

st.sidebar.markdown('<div class="pa-sub" style="padding:4px 2px">Demo on synthetic cases. Don’t enter real patient information.</div>',
                    unsafe_allow_html=True)
st.navigation([
    st.Page("chat.py", title="Chat", icon=":material/chat:", default=True),
    st.Page("case_review.py", title="Case review", icon=":material/manage_search:", url_path="case-review"),
    st.Page("sources.py", title="Data sources", icon=":material/hub:"),
    st.Page("knowledge.py", title="Knowledge base", icon=":material/menu_book:"),
    st.Page("decisions.py", title="Decision log", icon=":material/fact_check:"),
    st.Page("slack_page.py", title="Connect Slack", icon=":material/forum:", url_path="connect-slack"),
]).run()
