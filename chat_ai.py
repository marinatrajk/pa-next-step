"""Plain-English chat: work out which case an operator means, and answer questions about a case.

route()   "what's next for the Humira case due tomorrow?" -> intent + case id (or a short list to choose from)
answer()  "why is it escalated?" -> a short answer grounded in that case's documents and the tool's analysis

Neither ever sees the whole queue: shortlist() narrows thousands of cases to ~20 by keywords first.
"""

from __future__ import annotations

import re

import engine

STOP = {"the", "and", "for", "with", "what", "whats", "what's", "next", "step", "steps", "case", "cases", "about", "that", "this",
        "one", "show", "tell", "give", "need", "should", "does", "doing", "patient", "please", "can", "you", "how", "why", "who",
        "when", "where", "which", "is", "it", "do", "me", "my", "on", "of", "to", "a", "an", "in", "are", "there", "any"}
URGENT = {"urgent", "today", "tomorrow", "due", "deadline", "soon", "asap", "closing", "expiring", "first"}
SHORTLIST = 20


def describe(skill: engine.Skill, cid: str) -> str:
    """One line per case for the model: enough to recognize it, nothing clinical."""
    c, s = engine.load_case(cid), engine.case_summary(skill, cid)
    h = s["hours"]
    left = "no deadline" if h is None else (f"{h:.0f}h left" if h < 72 else f"{h / 24:.0f} days left")
    p = c["patient"]
    return (f"{cid} | {c['drug']} | {s['payer']} | {s['status']} | {c['indication']} | prescriber {c['prescriber']['name']} "
            f"({c['prescriber']['office']}) | patient {p['ref']}, {p['age']}{p['sex']} | {left}")


def shortlist(skill: engine.Skill, text: str, focus: str | None) -> list[str]:
    """Keyword pre-filter so the model reads ~20 candidates, not the whole queue."""
    words = {w for w in re.findall(r"[a-z0-9-]+", text.lower()) if len(w) > 2 and w not in STOP}
    by_urgency = engine.cases_by_urgency(skill)
    scored = []
    for cid in by_urgency:
        hay = describe(skill, cid).lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, cid))
    ids = [cid for _, cid in sorted(scored, key=lambda x: -x[0])][:SHORTLIST]
    if not ids or words & URGENT:
        ids += [cid for cid in by_urgency[:10] if cid not in ids]
    if focus and focus not in ids:
        ids.insert(0, focus)
    return ids[:SHORTLIST + 10]


ROUTE_SYSTEM = """You read one message an Access Operator typed into a prior-authorization chat and decide what they want.

intent:
- "next_step": they want to know what to do on a case (including "show me", "open", "what's next", or just naming a case).
- "question": they ask something specific about a case (why, what's missing, who, when, which documents, what did the note say).
- "urgent": they want to see their most urgent cases.
- "help": they ask how to use this chat.
- "other": anything else. Answer in one short sentence if it's a quick general question about prior auth, otherwise say what you can help with.

case_id: the one case they mean, chosen ONLY from the candidate list. Use the case in focus for follow-ups ("why?", "that one",
"what's missing") when no other case is named. Leave it "" if no case fits or several fit equally well.
other_case_ids: when several candidates fit, list them (best first) and put a one-line question in reply asking which one.
reply: one short, friendly sentence in plain English. When you picked a case, say which one in a few words
(for example "Looks like CASE-1077, the Humira case for Bluepeak, due in 38 hours."). Never invent case IDs."""

ANSWER_SYSTEM = """You answer an Access Operator's question about one prior-authorization case.

- Use only the sources given. Cite the source IDs you used.
- Provider records are facts; operator notes and team messages are only what someone said; say which when it matters.
- If the sources don't answer the question, say so plainly and say where the answer would come from (for example the provider office).
- The ANALYSIS source is the tool's own recommendation: use it to explain what the tool recommends and why, not as evidence.
- Two or three short sentences in plain English. No headings, no lists, no jargon codes unless the operator used them."""


def _schema_route() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["intent", "case_id", "other_case_ids", "reply"],
            "properties": {"intent": {"type": "string", "enum": ["next_step", "question", "urgent", "help", "other"]},
                           "case_id": {"type": "string"}, "other_case_ids": {"type": "array", "items": {"type": "string"}},
                           "reply": {"type": "string"}}}


def _schema_answer() -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["answer", "source_ids", "found"],
            "properties": {"answer": {"type": "string"}, "source_ids": {"type": "array", "items": {"type": "string"}},
                           "found": {"type": "boolean"}}}


def route(skill: engine.Skill, text: str, focus: str | None) -> dict:
    ids = shortlist(skill, text, focus)
    lines = "\n".join(describe(skill, cid) for cid in ids)
    user = (f"<case_in_focus>{focus or 'none'}</case_in_focus>\n<candidates>\n{lines}\n</candidates>\n"
            f"<message>{text}</message>")
    out, usage = engine.call_claude(model=skill.manifest["model"], effort="low", system=ROUTE_SYSTEM, user=user,
                                    schema=_schema_route(), max_tokens=2000)
    valid = set(ids)
    out["case_id"] = out["case_id"] if out["case_id"] in valid else ""  # never trust an ID the model made up
    out["other_case_ids"] = [c for c in out["other_case_ids"] if c in valid and c != out["case_id"]]
    out["usage"] = usage
    return out


def answer(skill: engine.Skill, cid: str, question: str) -> dict:
    case = engine.load_case(cid)
    sources = engine.build_sources(skill, case)
    run = engine.last_run(cid)
    parts = [f'<source id="{s["id"]}" label="{s["label"]}">\n{s["text"]}\n</source>' for s in sources]
    if run and run.get("status") == "ok":
        r = run["result"]
        summary = r.get("operator_summary") or {}
        parts.append('<source id="ANALYSIS" label="The tool\'s recommendation">\n'
                     f'Recommended step: {engine.ACTION_LABELS[r["primary_action"]]}. {summary.get("headline", "")}\n'
                     f'Why: {" ".join(summary.get("why", [])) or r["action_rationale"]}\n'
                     f'Escalation triggers: {", ".join(t["id"] + " " + skill.triggers[t["id"]]["name"] for t in r["triggers_fired"]) or "none"}\n'
                     f'Could not determine: {"; ".join(r["could_not_determine"]) or "nothing"}\n</source>')
    user = "\n\n".join(parts) + f"\n\nCase {cid}. Question: {question}"
    out, usage = engine.call_claude(model=skill.manifest["model"], effort="low", system=ANSWER_SYSTEM, user=user,
                                    schema=_schema_answer(), max_tokens=3000)
    known = {s["id"] for s in sources} | {"ANALYSIS"}
    out["source_ids"] = [s for s in out["source_ids"] if s in known]
    out["usage"] = usage
    return out
