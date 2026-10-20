"""Investigate: open-ended questions across every source the team has.

    .venv/bin/python investigate.py "which Bluepeak cases are waiting on records from an office?"

The per-case next step never uses this: it keeps its exact case record (gather.py) and its trust tiers. This is
for questions that hop between systems, like "has Bluepeak approved brand Humira on an office note alone?", where
one search's result (an office, a name, a case number) decides the next search.

- One index over the PA tracker, document inbox, case notes, Slack #access-ops, the team knowledge base, the SOP and
  the insurer policies. Every item keeps its source, its trust level and the case it belongs to.
- Search is hybrid: dense vectors (Chroma) plus keyword scores (BM25), fused by rank, so exact codes like NF-PREF
  match as well as meaning does. `search` is the one retriever function, so a dedicated search model such as
  Chroma Context-1 can be swapped in later and compared on the same questions.
- Claude runs the investigation with three tools (search, find_cases, read) and finishes by calling `answer`.
  Code drops any citation to an item it never saw, and the answer has to say when a claim rests only on Slack.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections import Counter
from functools import lru_cache

import engine
import gather
import kb

INDEX_DIR = engine.ROOT / "data" / "investigate"
MAX_STEPS = 8
TRUST = {1: "official insurer policy", 2: "official SOP", 3: "case record", 4: "operator note", 5: "unverified (Slack / team knowledge)"}
SOURCES = ["PA tracker", "Document inbox", "Case notes", "Slack #access-ops", "Team knowledge base", "SOP", "Insurer policy"]

# ------------------------------------------------------------------ the index


def _sections(text: str) -> list[tuple[str, str]]:
    parts = re.split(r"\n(?=## )", text)
    return [(p.splitlines()[0].lstrip("# ").strip(), p.strip()) for p in parts if p.strip()]


def items() -> list[dict]:
    """Every searchable item, from every system, with where it came from and how far to trust it."""
    skill = engine.load_skill()
    sys_ = gather.load_systems()
    links = gather.load_state().get("links", {})
    out = []
    for c in sys_["tracker"]:
        pa, pr, p = c["pa"], c["prescriber"], c["patient"]
        out.append({"id": c["case_id"], "source": "PA tracker", "tier": 3, "case": c["case_id"], "date": pa["event_date"],
                    "title": f"{c['case_id']} · {c['drug']}",
                    "text": f"{c['case_id']}: {c['drug']} for {c['indication']}. Insurer {c['payer_policy'].replace('_', ' ')}. "
                            f"Status {pa['status']} ({pa['reason_code']}: {pa['reason_text']}) on {pa['event_date']}, submission "
                            f"#{pa['submission_count']}, window closes {pa['window_closes']}. Prescriber {pr['name']} ({pr['specialty']}), "
                            f"{pr['office']}. Patient {p['name']}, born {p['dob']}."})
    for d in sys_["inbox"]:
        out.append({"id": d["key"], "source": "Document inbox", "tier": 3, "case": links.get(d["key"], {}).get("case"),
                    "date": d["received"], "title": f"{d['doc_type']} from {d.get('from_office') or 'unknown sender'}",
                    "text": f"{d['doc_type']} received {d['received']} via {d['channel']} from {d.get('from_office', '?')}. "
                            f"Patient written as {d.get('patient_name') or '(none)'}. {d['content']}"})
    for n in sys_["notes"]:
        out.append({"id": n["key"], "source": "Case notes", "tier": 4, "case": n["case_id"], "date": n["ts"][:10],
                    "title": f"Note by {n['author']} on {n['case_id']}", "text": f"{n['author']} ({n['ts'][:16]}): {n['text']}"})
    for m in sys_["slack"]:
        found = re.search(r"CASE-\d{4}", m["text"])
        out.append({"id": m["key"], "source": "Slack #access-ops", "tier": 5, "case": found.group(0) if found else None,
                    "date": m["ts"][:10], "title": f"Slack message from {m['author']}", "text": f"{m['author']} ({m['ts'][:16]}): {m['text']}",
                    "thread": m.get("thread")})
    for e in kb.entries():
        out.append({"id": e["id"], "source": "Team knowledge base", "tier": 5, "case": e["case_id"] or None, "date": e["added_at"][:10],
                    "title": e["title"], "text": f"{e['title']}. {e['summary']}"})
    for i, (head, body) in enumerate(_sections(skill.sop), 1):
        out.append({"id": f"SOP-{i:02d}", "source": "SOP", "tier": 2, "case": None, "date": "", "title": f"SOP: {head}", "text": body})
    for name, policy in skill.policies.items():
        for i, (head, body) in enumerate(_sections(policy), 1):
            out.append({"id": f"POL-{name.split('_')[0][:2].upper()}-{i:02d}", "source": "Insurer policy", "tier": 1, "case": None,
                        "date": "", "title": f"{name.replace('_', ' ').title()} policy: {head}", "text": body})
    return out


def _fingerprint(its: list[dict]) -> str:
    return hashlib.sha256(json.dumps([[i["id"], i["text"], i["case"]] for i in its]).encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def _index(fp: str) -> tuple[dict, object, dict]:
    """(items by id, Chroma collection, BM25 stats), rebuilt whenever any source changes."""
    import chromadb
    its = items()
    client = chromadb.PersistentClient(path=str(INDEX_DIR / "chroma"))
    meta = INDEX_DIR / "fingerprint.txt"
    if not meta.exists() or meta.read_text() != fp:
        try:
            client.delete_collection("all_sources")
        except Exception:
            pass
        col = client.create_collection("all_sources", metadata={"hnsw:space": "cosine"})
        col.add(ids=[i["id"] for i in its], documents=[f"{i['title']}. {i['text']}" for i in its],
                metadatas=[{"source": i["source"], "case": i["case"] or ""} for i in its])
        meta.write_text(fp)
    col = client.get_collection("all_sources")
    docs = {i["id"]: _tokens(f"{i['title']} {i['text']}") for i in its}
    df = Counter(t for toks in docs.values() for t in set(toks))
    stats = {"docs": docs, "df": df, "n": len(docs), "avg": sum(map(len, docs.values())) / max(1, len(docs))}
    return {i["id"]: i for i in its}, col, stats


def index():
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    return _index(_fingerprint(items()))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower())


def _bm25(query: str, ids: list[str], stats: dict, k1=1.4, b=0.75) -> list[str]:
    q = set(_tokens(query))
    scores = {}
    for i in ids:
        toks = stats["docs"][i]
        tf, s = Counter(toks), 0.0
        for t in q & set(toks):
            idf = math.log(1 + (stats["n"] - stats["df"][t] + 0.5) / (stats["df"][t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(toks) / stats["avg"]))
        if s > 0:
            scores[i] = s
    return sorted(scores, key=scores.get, reverse=True)


# ------------------------------------------------------------------ the tools


def search(query: str, source: str | None = None, case_id: str | None = None, k: int = 8) -> list[dict]:
    """Hybrid search over every source: vector and keyword rankings fused by reciprocal rank."""
    by_id, col, stats = index()
    pool = [i for i, it in by_id.items() if (not source or it["source"] == source) and (not case_id or it["case"] == case_id)]
    if not pool:
        return []
    where = {k_: v for k_, v in (("source", source), ("case", case_id)) if v}
    where = {"$and": [{k_: v} for k_, v in where.items()]} if len(where) > 1 else where or None
    dense = col.query(query_texts=[query], n_results=min(30, len(pool)), where=where)["ids"][0]
    keyword = _bm25(query, pool, stats)[:30]
    fused = Counter()
    for ranking in (dense, keyword):
        for rank, i in enumerate(ranking):
            fused[i] += 1 / (60 + rank)
    return [by_id[i] for i, _ in fused.most_common(k)]


def find_cases(insurer: str = "", status: str = "", drug: str = "", office: str = "", reason_code: str = "") -> list[dict]:
    """Structured filter over the PA tracker."""
    skill = engine.load_skill()
    hits = []
    for c in gather.load_systems()["tracker"]:
        fields = {"insurer": c["payer_policy"], "status": c["pa"]["status"], "drug": c["drug"],
                  "office": c["prescriber"]["office"], "reason_code": c["pa"]["reason_code"]}
        want = {"insurer": insurer, "status": status, "drug": drug, "office": office, "reason_code": reason_code}
        if all(not v or v.lower().replace(" ", "_") in fields[k].lower().replace(" ", "_") for k, v in want.items()):
            s = engine.case_summary(skill, c["case_id"])
            hits.append({"case_id": c["case_id"], "drug": c["drug"], "insurer": c["payer_policy"], "status": c["pa"]["status"],
                         "reason": f"{c['pa']['reason_code']}: {c['pa']['reason_text']}", "office": c["prescriber"]["office"],
                         "hours_left": s["hours"]})
    return hits


def read(item_id: str) -> dict | None:
    return index()[0].get(item_id)


def _brief(it: dict, full: bool = False) -> dict:
    text = it["text"] if full else (it["text"][:280] + ("…" if len(it["text"]) > 280 else ""))
    return {"id": it["id"], "source": it["source"], "trust": TRUST[it["tier"]], "case": it["case"], "date": it["date"],
            "title": it["title"], "text": text}


TOOLS = [
    {"name": "search", "description": "Search every source by meaning and by exact words. Returns the best matches with their source, "
                                      "trust level, case and date. Narrow with source or case_id when you know them.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "source": {"type": "string", "enum": SOURCES, "description": "Only this source"},
         "case_id": {"type": "string", "description": "Only items belonging to this case, e.g. CASE-1077"}},
         "required": ["query"]}},
    {"name": "find_cases", "description": "Filter the PA tracker. Every field is optional and matches partially, e.g. insurer 'bluepeak', "
                                          "status 'pended', office 'riverside', reason_code 'NF-PREF'.",
     "input_schema": {"type": "object", "properties": {k: {"type": "string"} for k in ("insurer", "status", "drug", "office", "reason_code")}}},
    {"name": "read", "description": "Read one item in full by its id (a case id, D-0001, N-0003, S-0101, KB-0004, SOP-02, POL-BL-03).",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
]
ANSWER_TOOL = {
    "name": "answer", "description": "Give the final answer. Call this once, when you can answer or have shown the sources can't.",
    "input_schema": {"type": "object", "required": ["answer", "citations", "cases", "confidence", "gaps"], "properties": {
        "answer": {"type": "string", "description": "Direct answer first, then the evidence. Short paragraphs or bullets."},
        "citations": {"type": "array", "items": {"type": "object", "required": ["id", "supports"],
                                                 "properties": {"id": {"type": "string"}, "supports": {"type": "string"}}}},
        "cases": {"type": "array", "items": {"type": "string"}, "description": "Case ids the answer is about"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "gaps": {"type": "array", "items": {"type": "string"}, "description": "What the sources don't show"}}}}

SYSTEM = """You investigate open-ended questions for a prior-authorization operations team. The sources: the PA tracker
(cases, status, deadlines), the document inbox (faxes, letters, emails, lab reports), operators' case notes, Slack
#access-ops, the team knowledge base, the SOP and the insurer policies.

Work like an investigator. Search, read what looks relevant, and let what you find shape the next search: an office,
a name, a date, a case number. Use find_cases for questions about the queue. Most questions take 3 to 6 tool calls.

Trust: insurer policies and the SOP are official. The tracker, documents and notes are facts about specific cases.
Slack and the team knowledge base are unverified: when a claim rests only on them, say so plainly.

This is research, not a decision. Never tell anyone to submit, appeal or send something; for a case's next step,
point them to /next-step. Finish by calling `answer`, citing only ids you saw. If the sources don't answer the
question, say that, and say what's missing.

Keep the answer under 180 words: the direct answer in one or two sentences, then the key evidence as short bullets
with the ids in brackets. At most 3 gaps."""


# ------------------------------------------------------------------ the loop


def _run_tool(name: str, args: dict, seen: set) -> tuple[str, str]:
    """Run one tool. Returns (result for the model, a line for the action log)."""
    if name == "search":
        hits = search(args.get("query", ""), args.get("source") or None, args.get("case_id") or None)
        seen.update(h["id"] for h in hits)
        scope = " in ".join(x for x in (args.get("source"), args.get("case_id")) if x)
        return (json.dumps([_brief(h) for h in hits]),
                f"Searched{' ' + scope if scope else ''} for “{args.get('query', '')}”: {len(hits)} results")
    if name == "find_cases":
        hits = find_cases(**{k: v for k, v in args.items() if k in ("insurer", "status", "drug", "office", "reason_code")})
        seen.update(h["case_id"] for h in hits)
        filt = ", ".join(f"{k} {v}" for k, v in args.items() if v) or "all cases"
        return json.dumps(hits), f"Filtered the queue ({filt}): {len(hits)} cases"
    if name == "read":
        it = read(args.get("id", ""))
        if not it:
            return json.dumps({"error": f"no item {args.get('id')}"}), f"Tried to read {args.get('id')}: not found"
        seen.add(it["id"])
        return json.dumps(_brief(it, full=True)), f"Read {it['id']}: {it['title']}"
    return json.dumps({"error": f"unknown tool {name}"}), f"Unknown tool {name}"


def investigate(question: str, on_step=None) -> dict:
    """Run one investigation. on_step(text, seconds) gets each action, for a live log. Returns the checked answer."""
    import time
    skill, t0, log, seen = engine.load_skill(), time.time(), [], set()

    def step(text: str) -> None:
        log.append({"t": round(time.time() - t0, 1), "text": text})
        if on_step:
            on_step(text, log[-1]["t"])

    index()  # build or refresh before the clock matters
    step("Indexed every source: tracker, documents, notes, Slack, knowledge base, SOP, policies")
    messages = [{"role": "user", "content": question}]
    usage = {"input_tokens": 0, "output_tokens": 0, "steps": 0}
    for n in range(MAX_STEPS + 1):
        last = n == MAX_STEPS
        if last:
            messages.append({"role": "user", "content": "Time is up: answer now with what you have, using the answer tool."})
        engine.count_call()
        resp = engine.client().beta.messages.create(
            model=skill.manifest["model"], max_tokens=4000, system=SYSTEM, messages=messages,
            tools=[ANSWER_TOOL] if last else TOOLS + [ANSWER_TOOL],
            output_config={"effort": "medium"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        usage["steps"] += 1
        calls = [b for b in resp.content if b.type == "tool_use"]
        final = next((b for b in calls if b.name == "answer"), None)
        if final:
            out = dict(final.input)
            bad = [c["id"] for c in out.get("citations", []) if c["id"] not in seen]
            out["citations"] = [c for c in out.get("citations", []) if c["id"] in seen]
            out["cases"] = [c for c in out.get("cases", []) if re.fullmatch(r"CASE-\d{4}", c)]
            by_id = index()[0]
            out["sources"] = [{**_brief(by_id[c["id"]]), "supports": c["supports"]} for c in out["citations"] if c["id"] in by_id]
            out["dropped_citations"] = bad
            out["unverified_only"] = bool(out["sources"]) and all(s["trust"].startswith("unverified") for s in out["sources"])
            step(f"Answered from {len(out['sources'])} sources" + (f" (dropped {len(bad)} citations it never saw)" if bad else ""))
            return {**out, "question": question, "log": log, "usage": usage | {"seconds": round(time.time() - t0, 1)}}
        if not calls:  # it wrote text instead of using a tool: ask for the answer tool
            messages += [{"role": "assistant", "content": resp.content},
                         {"role": "user", "content": "Please finish by calling the answer tool."}]
            continue
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in calls:
            result, line = _run_tool(b.name, b.input, seen)
            step(line)
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})
        messages.append({"role": "user", "content": results})
    raise RuntimeError("The investigation didn't finish")


if __name__ == "__main__":
    r = investigate(" ".join(sys.argv[1:]) or "Which Bluepeak cases are waiting on records from an office?",
                    on_step=lambda text, t: print(f"{t:5.1f}s  {text}"))
    print("\n" + r["answer"] + "\n")
    for s in r["sources"]:
        print(f"  [{s['id']}] {s['source']} · {s['trust']} · {s['supports']}")
    print("gaps:", r["gaps"], "| confidence:", r["confidence"], "| usage:", r["usage"])
