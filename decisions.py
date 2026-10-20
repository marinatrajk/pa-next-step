"""Decision log: every recommendation an operator acted on, what they did, and why they overrode it.

Overrides are the signal that improves the skill: each one can be queued as an eval candidate
for an expert to label (skills/pa_next_step/evals/candidates.json).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime

import streamlit as st

import engine
import ui

skill = engine.load_skill()
label = engine.ACTION_LABELS.get
VIEW_LABEL = {"workbench": "case review", "case_review": "case review"}  # older entries say "workbench"
entries = list(reversed(engine.read_decisions()))  # newest first
cand_path = skill.path / "evals" / "candidates.json"
candidates = json.loads(cand_path.read_text()) if cand_path.exists() else []
queued = {(c["case_id"], c["from_decision"]) for c in candidates}

ui.inject()
ui.header("Decision log", "What operators did with each recommendation, and why they overrode it")

if not entries:
    st.info("No decisions yet. In Chat, open a case and click **Mark as sent**, or use **Override** under Details.")
    st.stop()

# ------------------------------------------------------------------ summary

sent = [e for e in entries if e["decision"] == "accept"]
overrides = [e for e in entries if e["decision"] == "override"]
edited = [e for e in sent if e.get("draft_edited")]
k = st.columns(5)
st.caption("Accepted = sent as recommended. Drafts edited = share of accepted drafts the operator changed before sending.")
k[0].metric("Decisions", len(entries))
k[1].metric("Accepted", f"{len(sent) / len(entries):.0%}")
k[2].metric("Overrides", len(overrides))
k[3].metric("Drafts edited", f"{len(edited) / len(sent):.0%}" if sent else "n/a")
k[4].metric("Escalations", sum(1 for e in sent if e.get("escalate")))

# --------------------------------------------------------- overrides to review

pending = [e for e in overrides if (e["case_id"], e["ts"]) not in queued]
if overrides:
    st.markdown(f"#### Overrides to review ({len(pending)} not yet queued)")
    for e in overrides:
        with st.container(border=True):
            left, right = st.columns([4, 1.5])
            left.markdown(
                f"**{e['case_id']}** · {datetime.fromisoformat(e['ts']):%b %d, %H:%M}  \n"
                f"Tool said **{label(e['recommended_action'], e['recommended_action'])}** → operator chose **{label(e['final_action'], e['final_action'])}**  \n"
                f"*“{e['reason']}”*"
            )
            if (e["case_id"], e["ts"]) in queued:
                right.success("Queued")
            elif right.button("Queue for eval", key=f"q-{e['ts']}-{e['case_id']}", use_container_width=True):
                engine.add_eval_candidate(skill, e)
                st.rerun()

# -------------------------------------------------------------------- table

st.markdown("#### All decisions")
f1, f2 = st.columns([2, 3])
pick = f1.segmented_control("Show", ["All", "Sent", "Overridden"], default="All", label_visibility="collapsed")
cases = f2.multiselect("Cases", sorted({e["case_id"] for e in entries}), placeholder="All cases", label_visibility="collapsed")
rows = [e for e in entries
        if (pick in (None, "All") or (pick == "Sent") == (e["decision"] == "accept"))
        and (not cases or e["case_id"] in cases)]

table = [{
    "When": datetime.fromisoformat(e["ts"]).strftime("%b %d, %H:%M"),
    "Case": e["case_id"],
    "Tool recommended": label(e["recommended_action"], e["recommended_action"]),
    "Operator": "✅ Sent" if e["decision"] == "accept" else "✏️ Overrode",
    "Final action": label(e["final_action"], e["final_action"]),
    "Draft edited": bool(e.get("draft_edited")),
    "Triggers": " ".join(e.get("triggers", [])),
    "Reason": e.get("reason", ""),
} for e in rows]
picked = st.dataframe(table, use_container_width=True, hide_index=True, on_select="rerun", selection_mode="single-row",
                      column_config={"Reason": st.column_config.TextColumn(width="large")})

sel = picked.selection.rows
if sel:
    e = rows[sel[0]]
    with st.container(border=True):
        st.markdown(f"**{e['case_id']}** · {e.get('headline') or label(e['recommended_action'])}")
        st.caption(f"Analysis from {e.get('run_at', 'unknown')} · {e.get('skill')} · logged from {VIEW_LABEL.get(e.get('view'), e.get('view') or 'unknown')} view")
        st.text_area("What was sent", e.get("draft_text") or "(not recorded for this entry)", height=200, disabled=True)
else:
    st.caption("Select a row to see the message that was sent.")

# ------------------------------------------------------------------- export

buf = io.StringIO()
writer = csv.DictWriter(buf, fieldnames=list(table[0]) if table else ["When"])
writer.writeheader()
writer.writerows(table)
c1, c2 = st.columns([1, 3])
c1.download_button("Download CSV", buf.getvalue(), file_name="decision_log.csv", mime="text/csv", use_container_width=True)

if candidates:
    with st.expander(f"Eval candidates queued ({len(candidates)})"):
        st.caption("An expert labels each one (expected action, triggers, must-flag checks) and moves it into evals/expected.json.")
        st.dataframe(candidates, use_container_width=True, hide_index=True)

with st.expander("Reset log (demo only)"):
    sure = st.checkbox("I understand this deletes every logged decision")
    if st.button("Clear decision log", disabled=not sure):
        engine.LOG_FILE.unlink(missing_ok=True)
        st.rerun()
