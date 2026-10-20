"""Team knowledge base: Slack conversations operators chose to keep, stored in a vector database.

How things get in: someone tags the bot in a Slack thread ("@kb add" or "@kb add: <note>"). The bot saves that
whole thread, asks the model for a one-line title, what kind of knowledge it is, and which case it is about (if any),
and stores it with an embedding so it can be found by meaning later.

How things come out: for a case, `for_case()` returns the entries tagged to that case plus general know-how for
the same insurer. Entries about other patients' cases are never returned for this one.

This is one of three separate sources the engine reads:
  official rules  SOP + payer policies (skill pack)        authoritative
  case record     the case, its documents, its case notes  facts about this case
  team knowledge  this module                              useful context, never authoritative
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache

import engine

KB_DIR = engine.ROOT / "data" / "kb"
ENTRIES = KB_DIR / "entries.json"
CHROMA_DIR = KB_DIR / "chroma"
SLACK_FILE = engine.ROOT / "data" / "systems" / "slack_access_ops.json"
TRACKER_FILE = engine.ROOT / "data" / "systems" / "pa_tracker.json"
MIN_SIMILARITY = 0.30  # cosine similarity below this is not "relevant"
GENERAL_K = 3          # general entries per case, at most
BOT = re.compile(r"@kb\s+add\b:?\s*(.*)", re.I)


# ------------------------------------------------------------------ storage


def entries() -> list[dict]:
    return json.loads(ENTRIES.read_text())["entries"] if ENTRIES.exists() else []


def _save(items: list[dict]) -> None:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    ENTRIES.write_text(json.dumps({"about": "Team knowledge base: Slack threads saved with @kb", "entries": items}, indent=2))
    _version.cache_clear()


@lru_cache(maxsize=1)
def _version() -> str:
    return str(len(entries())) + "|" + (entries()[-1]["id"] if entries() else "")


@lru_cache(maxsize=1)
def _collection():
    import chromadb
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection("team_knowledge", metadata={"hnsw:space": "cosine"})


def _index_text(e: dict) -> str:
    return f"{e['title']}. {e['summary']}\n{e['transcript']}"


def slack() -> list[dict]:
    return json.loads(SLACK_FILE.read_text())["messages"]


# ------------------------------------------------------------------ capture


CAPTURE_SYSTEM = """An operator tagged the knowledge-base bot on a Slack thread to save it for the team. Describe what was saved.

title: what someone would search for, under 12 words.
summary: the knowledge itself in one or two plain sentences, keeping who said it and how sure they were.
kind: "tip" (how-to that worked), "policy_claim" (someone says an insurer's rules work a certain way), "case_update"
(news about one specific case), or "general".
case_id: the one candidate case the thread is about, or "" if it is general know-how. Only from the candidates.
payers: insurers it applies to, from the candidates' insurers, or [] if any insurer."""


def _thread(root_key: str, upto_ts: str | None = None) -> list[dict]:
    msgs = [m for m in slack() if (m["key"] == root_key or m.get("thread") == root_key) and not BOT.search(m["text"])]
    return sorted([m for m in msgs if not upto_ts or m["ts"] <= upto_ts], key=lambda m: m["ts"])


def capture(root_key: str, added_by: str, note: str = "", added_at: str | None = None, mention_key: str | None = None) -> dict:
    """Save a thread from the simulated Slack export (data/systems) as one entry."""
    msgs = [{"author": m["author"], "ts": m["ts"], "text": m["text"], "key": m["key"]} for m in _thread(root_key)]
    return capture_messages(msgs, added_by, note, thread_ref=root_key, mention_ref=mention_key, added_at=added_at)


def capture_messages(msgs: list[dict], added_by: str, note: str = "", thread_ref: str = "", mention_ref: str | None = None,
                     added_at: str | None = None, link: str | None = None) -> dict:
    """Save any conversation as one entry. msgs: [{author, ts (ISO), text}], oldest first. Used by the simulated
    channel and by the real Slack bot (slack_bot.py). One small model call describes it."""
    skill = engine.load_skill()
    transcript = "\n".join(f"{m['author']} ({m['ts'][:16].replace('T', ' ')}): {m['text']}" for m in msgs)
    if note:
        transcript += f"\n{added_by} (saved with note): {note}"
    tracker = json.loads(TRACKER_FILE.read_text())["cases"]
    words = set(re.findall(r"[a-z0-9-]{3,}", transcript.lower()))
    def hay(c):
        p = c["patient"]
        return f"{c['case_id']} {p['name']} {c['drug']} {c['payer_policy']} {c['prescriber']['name']} {c['prescriber']['office']}".lower()
    cands = sorted(tracker, key=lambda c: -sum(w in hay(c) for w in words))[:6]
    lines = "\n".join(f"- {c['case_id']} | {c['patient']['name']} | {c['drug']} | {c['payer_policy']} | {c['prescriber']['office']}" for c in cands)
    payers = sorted({c["payer_policy"] for c in tracker})
    schema = {"type": "object", "additionalProperties": False, "required": ["title", "summary", "kind", "case_id", "payers"],
              "properties": {"title": {"type": "string"}, "summary": {"type": "string"},
                             "kind": {"type": "string", "enum": ["tip", "policy_claim", "case_update", "general"]},
                             "case_id": {"type": "string"}, "payers": {"type": "array", "items": {"type": "string", "enum": payers}}}}
    out, _ = engine.call_claude(model=skill.manifest["model"], effort="low", system=CAPTURE_SYSTEM,
                                user=f"<thread>\n{transcript}\n</thread>\n<candidate_cases>\n{lines}\n</candidate_cases>",
                                schema=schema, max_tokens=1500)
    items = entries()
    entry = {
        "id": f"KB-{max([int(e['id'][3:]) for e in items] + [0]) + 1:04d}", "title": out["title"], "summary": out["summary"], "kind": out["kind"],
        "case_id": out["case_id"] if out["case_id"] in {c["case_id"] for c in cands} else "",
        "payers": out["payers"], "transcript": transcript, "added_by": added_by,
        "added_at": added_at or datetime.now().isoformat(timespec="seconds"), "slack_thread": thread_ref,
        "slack_messages": [m.get("key", m["ts"]) for m in msgs], "mention": mention_ref, "link": link,
    }
    replaced = [e["id"] for e in items if e["slack_thread"] == thread_ref]
    items = [e for e in items if e["slack_thread"] != thread_ref] + [entry]  # re-tagging a thread refreshes its entry
    _save(items)
    col = _collection()
    if replaced:
        col.delete(ids=replaced)
    col.upsert(ids=[entry["id"]], documents=[_index_text(entry)],
               metadatas=[{"case_id": entry["case_id"], "payers": ",".join(entry["payers"]), "kind": entry["kind"]}])
    return entry


def process_mentions() -> list[dict]:
    """Find '@kb add' messages in Slack that haven't been handled yet and save their threads."""
    done = {e.get("mention") for e in entries()}
    new = []
    for m in sorted(slack(), key=lambda m: m["ts"]):
        hit = BOT.search(m["text"])
        if hit and m["key"] not in done:
            new.append(capture(m.get("thread") or m["key"], m["author"], hit.group(1).strip(), m["ts"], m["key"]))
    return new


def tag_thread(root_key: str, by: str, note: str = "") -> dict:
    """What happens when someone types '@kb add' in a thread: post the mention, then let the bot pick it up."""
    data = json.loads(SLACK_FILE.read_text())
    key = f"S-KB{sum(1 for m in data['messages'] if m['key'].startswith('S-KB')) + 1:03d}"
    data["messages"].append({"key": key, "channel": "#access-ops", "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "author": by, "text": "@kb add" + (f": {note}" if note else ""), "thread": root_key})
    SLACK_FILE.write_text(json.dumps(data, indent=2))
    return process_mentions()[-1]


def remove(entry_id: str) -> None:
    _save([e for e in entries() if e["id"] != entry_id])
    _collection().delete(ids=[entry_id])


# ------------------------------------------------------------------ search


def search(query: str, k: int = 5) -> list[dict]:
    if not entries():
        return []
    by_id = {e["id"]: e for e in entries()}
    res = _collection().query(query_texts=[query], n_results=min(k, len(by_id)))
    return [{**by_id[i], "similarity": round(1 - d, 3)} for i, d in zip(res["ids"][0], res["distances"][0]) if i in by_id]


@lru_cache(maxsize=256)
def _for_case(case_id: str, payer: str, query: str, version: str) -> tuple:
    mine = [e for e in entries() if e["case_id"] == case_id]
    # general know-how only: claims about an insurer's rules become official through the policy owner, not through Slack
    general = [e for e in search(query, k=8) if not e["case_id"] and e["kind"] in ("tip", "general")
               and (not e["payers"] or payer in e["payers"]) and e["similarity"] >= MIN_SIMILARITY][:GENERAL_K]
    return tuple(json.dumps(e) for e in mine + general)


def for_case(case: dict) -> list[dict]:
    """Entries tagged to this case, plus general know-how for this insurer that is relevant to it."""
    query = " ".join([case["drug"], case["indication"], case["payer_policy"].replace("_", " "), case["pa"]["reason_text"]]
                     + [n["text"] for n in case.get("notes", [])])
    return [json.loads(x) for x in _for_case(case["case_id"], case["payer_policy"], query, _version())]
