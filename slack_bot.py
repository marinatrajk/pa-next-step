"""Slack bot: the knowledge base's front door, and the next-step assistant inside Slack.

    .venv/bin/python slack_bot.py      (needs SLACK_BOT_TOKEN and SLACK_APP_TOKEN in .env; see slack/README.md)

In any channel the bot is in:
  @kb add                      save this thread to the team knowledge base
  @kb add: <note>              same, with a note for the team
  @kb what's next for 1077?    next step, who to send it to, why, and a draft, in the thread
  @kb why is it escalated?     a follow-up about the case this thread is about
  @kb + an attached file       read a fax, PDF, photo, email or EHR export and match it to its case
  /next-step <case or words>   the same next step, from anywhere
  /kb-search <question>        search the knowledge base by meaning

Runs in Socket Mode: it connects out to Slack, so no public URL or server is needed. It uses the same engine,
knowledge base and decision log as the web app, so both always agree.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from datetime import datetime, timezone

import chat_ai
import engine
import gather
import ingest
import kb

SKILL = engine.load_skill()
STATUS_FILE = engine.ROOT / "data" / "logs" / "slack_bot.json"  # which workspace the standalone bot is in
WEB_APP = os.environ.get("PA_WEB_APP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "http://localhost:8517"
MENTION = re.compile(r"<@[A-Z0-9]+>")
ADD = re.compile(r"^\s*add\b(?:\s+(?:this\s+)?to\s+(?:the\s+)?(?:knowledge\s*base|kb))?\s*:?\s*(.*)$", re.I | re.S)
FOCUS: dict[str, str] = {}  # thread -> the case that thread is about, for follow-ups like "why?"
ACTIONS = SKILL.manifest["actions"]


# ------------------------------------------------------------------ helpers


def esc(text: str) -> str:
    """Slack mrkdwn needs &, < and > escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def iso(ts: str) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def get_run(cid: str) -> tuple[dict, bool]:
    """Reuse the saved analysis while the case's information is unchanged. Returns (run, re-analyzed)."""
    saved = engine.last_run(cid)
    if engine.is_current(saved, SKILL, engine.load_case(cid)) and "operator_summary" in saved["result"]:
        return saved, False
    return engine.run_case(cid, SKILL), bool(saved)


def case_line(cid: str) -> str:
    c = engine.case_summary(SKILL, cid)
    h = c["hours"]
    left = "no deadline" if h is None else (f"{h:.0f}h left" if h < 72 else f"{h / 24:.0f} days left")
    dot = ":red_circle:" if h is not None and h < 48 else ":large_orange_circle:" if h is not None and h < 168 else ":large_green_circle:"
    return f"*{cid}* · {esc(c['drug'])} · {esc(c['payer'])} · {esc(c['status'])} · {dot} {left}"


def user_name(client, user_id: str, cache: dict = {}) -> str:
    if user_id not in cache:
        try:
            u = client.users_info(user=user_id)["user"]
            cache[user_id] = u.get("real_name") or u["profile"].get("display_name") or u["name"]
        except Exception:
            cache[user_id] = user_id
    return cache[user_id]


# ------------------------------------------------------------- message blocks


def result_blocks(cid: str, run: dict, intro: str = "") -> list[dict]:
    r = run["result"]
    s = r.get("operator_summary") or {"headline": engine.ACTION_LABELS[r["primary_action"]], "why": [r["action_rationale"]], "then": []}
    d = r["draft"]
    flag = ":large_orange_circle: *Escalate · don't act yet*" if r["escalate"] else ":large_green_circle: *Ready to act*"
    draft = f"Subject: {d['subject']}\n\n{d['body']}"
    if len(draft) > 2800:
        draft = draft[:2800] + "\n…(full draft in the web app)"
    blocks = []
    if intro:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": esc(intro)}]})
    blocks += [
        {"type": "section", "text": {"type": "mrkdwn", "text": case_line(cid)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{flag}\n*{esc(s['headline'])}*\nSend to: {esc(d['to'])}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Why*\n" + "\n".join(f"• {esc(x)}" for x in s["why"])}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Draft message*\n```" + draft.replace("```", "'''") + "```"}},
    ]
    if s.get("then"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*After you send it*\n" + "\n".join(f"☐ {esc(x)}" for x in s["then"])}})
    value = json.dumps({"case": cid, "ran_at": run["ran_at"]})
    blocks += [
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "Mark as sent"}, "style": "primary", "action_id": "mark_sent", "value": value},
            {"type": "button", "text": {"type": "plain_text", "text": "Override"}, "action_id": "override", "value": value},
            {"type": "button", "text": {"type": "plain_text", "text": "Open in web app"}, "action_id": "open_web", "url": f"{WEB_APP}/case-review"},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Analyzed {run['ran_at'][5:16].replace('T', ' ')} · confidence {r['confidence']} · "
                                                                     f"reads the SOP, the case record and the team knowledge base"}]},
    ]
    return blocks


def answer_blocks(cid: str, a: dict) -> list[dict]:
    src = " ".join(f"`{x}`" for x in a["source_ids"])
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": case_line(cid)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": esc(a["answer"])}},
    ]
    if src:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"From {src}" + ("" if a["found"] else " · the case documents don't answer this directly")}]})
    return blocks


def cases_blocks(text: str, ids: list[str]) -> list[dict]:
    lines = "\n".join(f"• {case_line(cid)}" for cid in ids[:3])
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"{esc(text)}\n{lines}"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Reply with the case number, e.g. `@kb what's next for 1077`"}]}]


HELP = ("*I can help with:*\n"
        "• `@kb add` in a thread: save it to the team knowledge base (`@kb add: note` to add a note)\n"
        "• `@kb what's next for the Humira case due tomorrow?`: next step, reasons and a draft\n"
        "• `@kb why is it escalated?`: a follow-up about the case in this thread\n"
        "• Attach a document and mention `@kb`: I read it, pull out the fields and match it to its case\n"
        "• `/next-step 1077` and `/kb-search <question>`")


# ------------------------------------------------------------------ actions


def save_thread(client, channel: str, thread_ts: str, mention_ts: str, user_id: str, note: str) -> dict:
    """Fetch the thread from Slack and save it as one knowledge-base entry."""
    replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=200)["messages"]
    msgs = []
    for m in replies:
        if m.get("ts") == mention_ts or m.get("bot_id"):
            continue  # not the "@kb add" itself, not the bot's own replies
        text = MENTION.sub("", m.get("text", "")).strip()
        if text:
            msgs.append({"author": user_name(client, m.get("user", "")), "ts": iso(m["ts"]), "text": text, "key": m["ts"]})
    if not msgs and note:
        msgs = [{"author": user_name(client, user_id), "ts": iso(mention_ts), "text": note, "key": mention_ts}]
        note = ""
    try:
        link = client.chat_getPermalink(channel=channel, message_ts=thread_ts)["permalink"]
    except Exception:
        link = None
    return kb.capture_messages(msgs, user_name(client, user_id), note, thread_ref=f"slack:{channel}:{thread_ts}",
                               mention_ref=f"slack:{channel}:{mention_ts}", link=link)


def saved_text(e: dict) -> str:
    kind = {"tip": "tip", "general": "general know-how", "case_update": "case update", "policy_claim": "claim about an insurer's rules"}[e["kind"]]
    scope = f"about *{e['case_id']}*" if e["case_id"] else "general, shared with cases for " + (", ".join(p.replace("_", " ").title() for p in e["payers"]) or "every insurer")
    extra = "\n_Claims about insurer rules are only used on their own case until Payer Relations confirms them._" if e["kind"] == "policy_claim" and not e["case_id"] else ""
    return f":books: Saved to the knowledge base as *{e['id']}*: {esc(e['title'])}\n{kind} · {scope}{extra}"


MAX_UPLOAD = 20_000_000


def read_upload(f: dict, token: str) -> tuple[dict, bool]:
    """Download one Slack file into the raw inbox and run it through document intake. Returns (item, seen before)."""
    name = re.sub(r"[^\w.\-]", "_", f.get("name") or "upload")
    ext = os.path.splitext(name)[1].lower()
    if ext not in ingest.SUPPORTED:
        raise ValueError(f"I can't read {ext or 'that kind of'} files. Send a PDF, a photo or scan, an email (.eml), "
                         "an EHR export (FHIR .json) or a text file.")
    if f.get("size", 0) > MAX_UPLOAD:
        raise ValueError("That file is over 20 MB.")
    req = urllib.request.Request(f["url_private_download"], headers={"Authorization": f"Bearer {token}"})
    data = urllib.request.urlopen(req, timeout=60).read()
    if data[:200].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("Slack didn't let me download it. The app needs the `files:read` scope: add it under "
                         "OAuth & Permissions, then reinstall the app.")
    seen = ingest.log()["files"].get(hashlib.sha256(data).hexdigest()[:12])
    if seen:
        return seen, True
    dest = ingest.RAW_DIR / name
    if dest.exists():
        dest = dest.with_stem(f"{dest.stem}-{datetime.now():%H%M%S}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return ingest.process_file(dest, via="slack"), False


def intake_text(name: str, item: dict, link: dict, seen: bool) -> str:
    """What intake made of one file, for a Slack thread."""
    r, i = item["schema"], item["intake"]
    head = f":page_facing_up: *{esc(name)}* · {esc(i['format'])} · read by {esc(i['read_by'])}" + (" (read earlier)" if seen else f" in {i['seconds']}s")
    if i.get("set_aside"):
        return (f"{head}\n:no_entry_sign: *Set aside: this isn't a prior-auth document.* {esc(r['relevance']['reason'])} "
                "Nothing was added to a case, and the file wasn't kept.")
    facts = []
    d = r["insurer_decision"]
    if d["status"]:
        facts.append(f"{d['status']}" + (f" ({esc(d['reason_code'])})" if d["reason_code"] else ""))
    cov = r["coverage"]
    if cov["insurer"]:
        facts.append(f"{esc(cov['insurer'])}" + (f", member {esc(cov['member_id'])}" if cov["member_id"] else "")
                     + (f", effective {cov['effective_date']}" if cov["effective_date"] else ""))
    sev = [f"{k} {v:g}{unit}" for k, v, unit in (("IGA", r["severity"]["iga"], ""), ("BSA", r["severity"]["bsa_pct"], "%"),
                                                 ("DAS28", r["severity"]["das28"], "")) if v != -1]
    facts += sev
    facts += [f"{esc(m['drug'])} {m['start'] or '?'} to {m['end'] or '?'} ({m['outcome']})" for m in r["medications"][:3]]
    facts += [f"{esc(l['test'])}: {esc(l['result'])}" for l in r["labs"] if l["flag"]][:3]
    facts += [f"{esc(c['drug_class'])} contraindicated ({'stated by prescriber' if c['stated_by_prescriber'] else 'relayed, not from the prescriber'})"
              for c in r["contraindications"]]
    lines = [head, f"*Became:* {esc(item['doc_type'])} for {esc(r['patient']['name'] or 'no named patient')}"]
    if facts:
        lines.append("*Key fields:* " + " · ".join(facts))
    if link.get("case"):
        how = "exact match" if link.get("method") == "exact" else "matched by AI"
        lines.append(f"*Matched to:* {link['case']} ({how}: {esc(link['how'].removeprefix('AI: '))})")
    else:
        lines.append(f":warning: *Not matched to a case.* {esc(link.get('how', '').removeprefix('AI: '))} "
                     "A person needs to look at it: Data sources → Unmatched.")
    if i["issues"]:
        lines.append(":mag: *Check:* " + "; ".join(esc(x) for x in i["issues"]))
    if r["missing"]:
        lines.append("_Not on the document: " + ", ".join(esc(x) for x in r["missing"][:5]) + "_")
    return "\n".join(lines)


def handle_files(files: list[dict], token: str, thread_key: str, post) -> None:
    """Read every attached file, rebuild the cases they belong to, and report back in the thread."""
    results = []
    for f in files:
        try:
            item, seen = read_upload(f, token)
            results.append((f.get("name", "file"), item, seen, None))
        except Exception as e:
            results.append((f.get("name", "file"), None, False, str(e)))
    state = gather.gather(SKILL)["state"] if any(item for _, item, _, _ in results) else gather.load_state()
    texts, cases, fresh = [], [], False
    for name, item, seen, err in results:
        if err:
            texts.append(f":x: *{esc(name)}*: {esc(err)}")
            continue
        link = state["links"].get(item["key"], {})
        texts.append(intake_text(name, item, link, seen))
        if link.get("case"):
            cases.append(link["case"])
            fresh = fresh or not seen
    if len(set(cases)) == 1:
        FOCUS[thread_key] = cases[0]
        texts.append(f"{cases[0]} now includes this. Ask `@kb what's next?` here for its updated next step." if fresh
                     else f"{cases[0]} already had this document. Ask `@kb what's next?` here for its next step.")
    post("Document read", [{"type": "section", "text": {"type": "mrkdwn", "text": t[:2900]}} for t in texts])


def handle_text(text: str, thread_key: str, post) -> None:
    """A plain-English message: find the case, then show next steps or answer the question. `post(text, blocks)`."""
    t = text.strip()
    if not t or t.lower() in ("help", "?"):
        post("How I can help", [{"type": "section", "text": {"type": "mrkdwn", "text": HELP}}])
        return
    route = chat_ai.route(SKILL, t, FOCUS.get(thread_key))
    cid = route["case_id"]
    if route["intent"] == "urgent":
        post("Most urgent cases", cases_blocks(route["reply"] or "Your most urgent cases:", engine.cases_by_urgency(SKILL)))
    elif route["intent"] in ("next_step", "question") and cid:
        FOCUS[thread_key] = cid
        if not engine.gate(SKILL, engine.load_case(cid)).applies:
            notes = engine.gate(SKILL, engine.load_case(cid)).notes
            post("Handle manually", [{"type": "section", "text": {"type": "mrkdwn", "text": case_line(cid)}},
                                     {"type": "section", "text": {"type": "mrkdwn", "text": "*Handle this case manually.* " + esc(" ".join(notes))}}])
        elif route["intent"] == "next_step":
            run, again = get_run(cid)
            intro = (route["reply"] + " " if route["reply"] else "") + ("New information arrived since the last analysis, so I analyzed it again." if again else "")
            post(f"Next step for {cid}", result_blocks(cid, run, intro.strip()))
        else:
            post(f"About {cid}", answer_blocks(cid, chat_ai.answer(SKILL, cid, t)))
    elif route["other_case_ids"]:
        post("Which case?", cases_blocks(route["reply"] or "A few cases could fit. Which one?", route["other_case_ids"]))
    else:
        post(route["reply"] or "I couldn't tell which case you mean.", [{"type": "section", "text": {"type": "mrkdwn", "text": esc(route["reply"] or HELP)}}])


def override_modal(value: str, run: dict) -> dict:
    r = run["result"]
    return {
        "type": "modal", "callback_id": "override_submit", "private_metadata": value,
        "title": {"type": "plain_text", "text": "Override"}, "submit": {"type": "plain_text", "text": "Log override"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"The tool recommended *{engine.ACTION_LABELS[r['primary_action']]}*."}},
            {"type": "input", "block_id": "action", "label": {"type": "plain_text", "text": "Correct action"},
             "element": {"type": "static_select", "action_id": "value",
                         "initial_option": {"text": {"type": "plain_text", "text": engine.ACTION_LABELS[r["primary_action"]]}, "value": r["primary_action"]},
                         "options": [{"text": {"type": "plain_text", "text": engine.ACTION_LABELS[a]}, "value": a} for a in ACTIONS]}},
            {"type": "input", "block_id": "reason", "label": {"type": "plain_text", "text": "Why? (becomes a new eval case)"},
             "element": {"type": "plain_text_input", "action_id": "value", "multiline": True}},
        ],
    }


# ---------------------------------------------------------------------- app


def build_app(token: str | None = None, **kwargs):
    from slack_bolt import App

    app = App(token=token or os.environ["SLACK_BOT_TOKEN"], **kwargs)

    @app.event("app_mention")
    def on_mention(event, client, logger):
        channel, ts = event["channel"], event["ts"]
        thread_ts = event.get("thread_ts") or ts
        text = MENTION.sub("", event.get("text", "")).strip()
        files = event.get("files") or []
        if not files and event.get("subtype") == "file_share":  # some events leave the files out: fetch the message
            msgs = client.conversations_replies(channel=channel, ts=ts, limit=1).get("messages", [])
            files = msgs[0].get("files", []) if msgs else []
        if files:
            reading = client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                              text=f":hourglass_flowing_sand: Reading {len(files)} document{'s' if len(files) != 1 else ''}…")
            try:
                handle_files(files, client.token, f"{channel}:{thread_ts}",
                             lambda fallback, blocks: client.chat_update(channel=channel, ts=reading["ts"], text=fallback, blocks=blocks))
            except Exception as e:
                logger.exception("document intake failed")
                client.chat_update(channel=channel, ts=reading["ts"], text=f"Sorry, I couldn't read that: {e}")
            return
        add = ADD.match(text)
        if add:
            try:
                client.reactions_add(channel=channel, timestamp=ts, name="eyes")
            except Exception:
                pass
            e = save_thread(client, channel, thread_ts, ts, event.get("user", ""), add.group(1).strip())
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=saved_text(e))
            return
        thinking = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=":hourglass_flowing_sand: Working on it…")

        def post(fallback, blocks):
            client.chat_update(channel=channel, ts=thinking["ts"], text=fallback, blocks=blocks)

        try:
            handle_text(text, f"{channel}:{thread_ts}", post)
        except Exception as e:  # tell the person, don't fail silently
            logger.exception("mention failed")
            post("Sorry, that failed", [{"type": "section", "text": {"type": "mrkdwn", "text": f"Sorry, that failed: `{esc(str(e))}`"}}])

    @app.command("/next-step")
    def on_next_step(ack, command, respond, logger):
        ack(":hourglass_flowing_sand: Working on it…")
        text = command.get("text", "").strip()
        if re.fullmatch(r"(?:case-?)?\d{3,5}", text, re.I):
            text = f"what's next for case {re.sub(r'\D', '', text)}"
        try:
            handle_text(text or "help", f"cmd:{command['user_id']}",
                        lambda fallback, blocks: respond(text=fallback, blocks=blocks, response_type="ephemeral", replace_original=True))
        except Exception as e:
            logger.exception("/next-step failed")
            respond(text=f"Sorry, that failed: {e}", response_type="ephemeral")

    @app.command("/kb-search")
    def on_kb_search(ack, command, respond):
        ack()
        q = command.get("text", "").strip()
        hits = [h for h in kb.search(q, k=5) if h["similarity"] >= kb.MIN_SIMILARITY] if q else []
        if not hits:
            respond(text="Nothing in the knowledge base matches that." if q else "Usage: `/kb-search <question>`", response_type="ephemeral")
            return
        lines = "\n".join(f"• *{esc(h['title'])}* `{h['id']}` ({h['similarity']:.2f})\n   {esc(h['summary'])}" for h in hits)
        respond(text="Knowledge base results", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"*Knowledge base: {esc(q)}*\n{lines}"}}],
                response_type="ephemeral")

    @app.action("mark_sent")
    def on_mark_sent(ack, body, client):
        ack()
        v = json.loads(body["actions"][0]["value"])
        run = engine.last_run(v["case"])
        engine.log_decision(v["case"], run, "accept", view="slack")
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"].get("thread_ts") or body["message"]["ts"],
                                text=f":white_check_mark: Logged as sent by <@{body['user']['id']}>. It's in the Decision log.")

    @app.action("override")
    def on_override(ack, body, client):
        ack()
        v = body["actions"][0]["value"]
        meta = json.dumps({**json.loads(v), "channel": body["channel"]["id"], "thread": body["message"].get("thread_ts") or body["message"]["ts"]})
        client.views_open(trigger_id=body["trigger_id"], view=override_modal(meta, engine.last_run(json.loads(v)["case"])))

    @app.view("override_submit")
    def on_override_submit(ack, body, view, client):
        ack()
        meta = json.loads(view["private_metadata"])
        vals = view["state"]["values"]
        action, reason = vals["action"]["value"]["selected_option"]["value"], vals["reason"]["value"]["value"]
        engine.log_decision(meta["case"], engine.last_run(meta["case"]), "override", reason=reason, final_action=action, view="slack")
        client.chat_postMessage(channel=meta["channel"], thread_ts=meta["thread"],
                                text=f":pencil2: <@{body['user']['id']}> overrode this to *{engine.ACTION_LABELS[action]}*: “{esc(reason)}”. Logged for review.")

    @app.action("open_web")
    def on_open_web(ack):
        ack()

    return app


if __name__ == "__main__":
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)} in .env. See slack/README.md.")
    app = build_app()
    me = app.client.auth_test()
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({"team": me["team"], "bot_user": me["user"], "pid": os.getpid(),
                                       "since": datetime.now().strftime("%H:%M")}))
    print(f"Slack bot running in {me['team']}. Mention it in a channel it's been invited to, or use /next-step. Ctrl+C to stop.")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
