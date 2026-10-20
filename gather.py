"""Gather each case from the separate source systems into one case file.

    .venv/bin/python gather.py            # gather everything, print what changed
    .venv/bin/python gather.py --score    # also score linking against evals/gather_gold.json

Sources (data/systems/): the PA tracker (one record per case), the document inbox (faxes and submissions) and
case notes. Two other sources stay separate and are read at decision time, not copied into case files:
the official rules (SOP + payer policies, in the skill pack) and the team knowledge base (Slack threads saved
with @kb, see kb.py).

Linking, cheapest first:
  1. exact, in code: a case number on the item, a patient name + date of birth match, or a reply in a linked Slack thread
  2. AI, only for what's left: one call decides which case each loose item belongs to, or none, with a reason.
     Decisions are cached by the item's content, so an item is never judged twice.

Case files (data/cases/) are rebuilt from the sources; a file is rewritten only when its content changed,
so downstream analyses know exactly which cases need a new run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime

import engine

SYSTEMS = engine.ROOT / "data" / "systems"
GATHER_DIR = engine.ROOT / "data" / "gather"
STATE_FILE = GATHER_DIR / "state.json"
LINK_CACHE = GATHER_DIR / "link_cache"
GOLD_FILE = engine.DEFAULT_SKILL / "evals" / "gather_gold.json"
LINKER_VERSION = "l1"
FILES = {"tracker": "pa_tracker.json", "inbox": "document_inbox.json", "notes": "case_notes.json", "slack": "slack_access_ops.json"}
SYSTEM_LABEL = {"inbox": "Document inbox", "notes": "Case notes", "slack": "Slack #access-ops"}


# ------------------------------------------------------------------ sources


def load_systems() -> dict:
    raw = {k: json.loads((SYSTEMS / f).read_text()) for k, f in FILES.items()}
    return {"tracker": raw["tracker"]["cases"], "inbox": raw["inbox"]["documents"], "notes": raw["notes"]["notes"], "slack": raw["slack"]["messages"]}


def save_system(name: str, items: list) -> None:
    path = SYSTEMS / FILES[name]
    data = json.loads(path.read_text())
    data[{"inbox": "documents", "notes": "notes", "slack": "messages"}[name]] = items
    path.write_text(json.dumps(data, indent=2))


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", name.lower())).strip()


# ------------------------------------------------------------------ linking


def exact_links(sys: dict) -> dict:
    """item key -> (case_id, how). Only certain matches; everything else is left for the AI step."""
    cases = {c["case_id"]: c for c in sys["tracker"]}
    by_person = {(_norm(c["patient"]["name"]), c["patient"]["dob"]): c["case_id"] for c in sys["tracker"]}
    number = {cid[-4:]: cid for cid in cases}
    links = {}
    for d in sys["inbox"]:
        if d.get("case_ref") in cases:
            links[d["key"]] = (d["case_ref"], "case number on the submission")
        elif d.get("patient_name") and d.get("patient_dob") and (_norm(d["patient_name"]), d["patient_dob"]) in by_person:
            links[d["key"]] = (by_person[(_norm(d["patient_name"]), d["patient_dob"])], "patient name and date of birth")
    for n in sys["notes"]:
        if n.get("case_id") in cases:
            links[n["key"]] = (n["case_id"], "filed on the case")
    for msg in sys["slack"]:
        hits = {number[x] for x in re.findall(r"\b(1\d{3})\b", msg["text"]) if x in number}
        if len(hits) == 1:
            links[msg["key"]] = (hits.pop(), "case number in the message")
    return links


def describe(c: dict) -> str:
    p = c["patient"]
    return (f"{c['case_id']} | patient {p['name']}, DOB {p['dob']} | {c['drug']} | {c['payer_policy'].replace('_', ' ')} | "
            f"{c['pa']['status']} {c['pa']['reason_code']} | prescriber {c['prescriber']['name']}, {c['prescriber']['office']}")


def item_text(kind: str, it: dict) -> str:
    if kind == "inbox":
        return (f"{it['doc_type']} received {it['received']} via {it['channel']} from {it.get('from_office', '?')}; "
                f"patient written as '{it.get('patient_name') or '(none)'}', DOB '{it.get('patient_dob') or '(none)'}': {it['content'][:400]}")
    return f"Slack message by {it['author']} at {it['ts']}: {it['text']}"


def shortlist(text: str, sys: dict, k: int = 5) -> list[str]:
    """Keyword pre-filter: the AI compares an item with a handful of cases, never the whole tracker."""
    words = {w for w in re.findall(r"[a-z0-9-]{3,}", text.lower())}
    scored = []
    for c in sys["tracker"]:
        hay = (describe(c) + " " + " ".join(n["text"] for n in sys["notes"] if n.get("case_id") == c["case_id"])).lower()
        score = sum(1 for w in words if w in hay)
        if score:
            scored.append((score, c["case_id"]))
    return [cid for _, cid in sorted(scored, key=lambda x: -x[0])[:k]]


LINK_SYSTEM = """You match loose items (faxes, Slack messages) to prior-authorization cases.

For each item, pick the ONE candidate case it is about, or "" if it is about none of them (general chatter, a different
patient, a blank fax). Faxes name the patient however the office writes it: initials, typos and a missing date of birth are
normal, so weigh name, date of birth, office, drug and content together. Slack messages refer to cases loosely ("the Hudson
Valley Humira one"); use the candidate's notes for context. Only link when the evidence points to that case.
confidence: high = unmistakable, medium = very likely, low = a guess. reason: one short sentence."""


def ai_links(items: list[tuple[str, dict]], sys: dict, skill: engine.Skill) -> tuple[dict, dict]:
    """One call for every uncached loose item. Returns (key -> decision, usage)."""
    cases = {c["case_id"]: c for c in sys["tracker"]}
    LINK_CACHE.mkdir(parents=True, exist_ok=True)
    decisions, todo = {}, []
    for kind, it in items:
        text = item_text(kind, it)
        cands = shortlist(text, sys)
        h = hashlib.sha256(f"{LINKER_VERSION}|{text}|{','.join(cands)}".encode()).hexdigest()[:20]
        hit = LINK_CACHE / f"{h}.json"
        if hit.exists():
            decisions[it["key"]] = json.loads(hit.read_text())
        else:
            todo.append((it["key"], text, cands, hit))
    usage = {"input_tokens": 0, "output_tokens": 0, "items_judged": len(todo), "items_cached": len(items) - len(todo)}
    if not todo:
        return decisions, usage
    blocks = []
    for key, text, cands, _ in todo:
        lines = "\n".join(f"  - {describe(cases[c])}\n    notes: " + (" / ".join(n["text"] for n in sys["notes"] if n.get("case_id") == c)[:300] or "none")
                          for c in cands) or "  (no plausible candidates)"
        blocks.append(f'<item key="{key}">\n{text}\ncandidates:\n{lines}\n</item>')
    schema = {"type": "object", "additionalProperties": False, "required": ["links"], "properties": {"links": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["key", "case_id", "confidence", "reason"],
        "properties": {"key": {"type": "string"}, "case_id": {"type": "string"},
                       "confidence": {"type": "string", "enum": ["high", "medium", "low"]}, "reason": {"type": "string"}}}}}}
    out, u = engine.call_claude(model=skill.manifest["model"], effort="low", system=LINK_SYSTEM,
                                user="\n\n".join(blocks) + "\n\nDecide every item.", schema=schema, max_tokens=8000)
    usage.update(input_tokens=u["input_tokens"], output_tokens=u["output_tokens"])
    got = {d["key"]: d for d in out["links"]}
    for key, _, cands, path in todo:
        d = got.get(key, {"case_id": "", "confidence": "low", "reason": "No decision returned."})
        if d["case_id"] not in cands:  # never trust a case the model wasn't offered
            d = {**d, "case_id": ""}
        d = {"case_id": d["case_id"], "confidence": d["confidence"], "reason": d["reason"]}
        path.write_text(json.dumps(d, indent=2))
        decisions[key] = d
    return decisions, usage


# ----------------------------------------------------------------- assembly


def knowledge(skill: engine.Skill, payer_policy: str) -> dict:
    sop_line = skill.sop.splitlines()[2] if len(skill.sop.splitlines()) > 2 else ""
    policy = skill.policies.get(payer_policy, "")
    pid = re.search(r"\*\*Policy ID:\*\*\s*([^\s·]+)", policy)
    return {"sop": re.sub(r"\*", "", sop_line).split("·")[0].replace("SOP ID:", "").strip() or "OPS-PA-014",
            "payer_policy": pid[1] if pid else None}


def assemble(c: dict, items: dict, links: dict, skill: engine.Skill) -> dict:
    cid, num = c["case_id"], c["case_id"][-4:]
    mine = lambda kind: [it for it in items[kind] if links.get(it["key"], {}).get("case") == cid]
    src = lambda kind, it: {"system": SYSTEM_LABEL[kind], "ref": it["key"], "linked_by": links[it["key"]]["how"],
                            **({"simulated": True} if it.get("simulated") else {}),
                            **({"intake": {k: it["intake"][k] for k in ("format", "read_by")} | {"raw_file": it["raw_file"]}} if it.get("intake") else {})}
    # numbered in order of arrival, so a new document never renumbers the ones notes already refer to
    docs = sorted(mine("inbox"), key=lambda d: (d.get("intake", {}).get("processed_at", "")[:10] or d["received"], d["key"]))
    return {
        **{k: c[k] for k in ("case_id", "as_of", "patient", "payer_policy", "drug", "indication", "prescriber", "pa")},
        "documents": [{"doc_id": f"DOC-{num}-{i:02d}", "type": d["doc_type"], "received": d["received"], "content": d["content"],
                       "_source": src("inbox", d)} for i, d in enumerate(docs, 1)],
        "notes": [{"author": n["author"], "ts": n["ts"], "text": n["text"], "_source": src("notes", n)}
                  for n in sorted(mine("notes"), key=lambda n: n["ts"])],
        "messages": [],  # Slack lives in the team knowledge base, not in the case record
        "knowledge": knowledge(skill, c["payer_policy"]),
    }


def fingerprint(case: dict) -> str:
    return hashlib.sha256(json.dumps(case, sort_keys=True).encode()).hexdigest()[:16]


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"cases": {}, "links": {}}


def gather(skill: engine.Skill | None = None) -> dict:
    skill = skill or engine.load_skill()
    sys = load_systems()
    prev = load_state()
    links = {k: {"case": cid, "how": how, "method": "exact"} for k, (cid, how) in exact_links(sys).items()}

    # Slack is not part of the case record: conversations reach decisions through the team knowledge base (kb.py)
    links = {k: v for k, v in links.items() if not k.startswith("S-")}
    # 2. AI for loose items: faxes nobody could match exactly
    loose = [("inbox", d) for d in sys["inbox"] if d["key"] not in links]
    decisions, usage = ai_links(loose, sys, engine.load_skill()) if loose else ({}, {"items_judged": 0, "items_cached": 0})
    for key, d in decisions.items():
        if d["case_id"] and d["confidence"] in ("high", "medium"):
            links[key] = {"case": d["case_id"], "how": f"AI: {d['reason']}", "method": "ai", "confidence": d["confidence"]}
        else:
            links[key] = {"case": None, "how": f"AI: {d['reason']}", "method": "ai", "confidence": d["confidence"]}

    # 4. assemble, write only what changed
    engine.CASES_DIR.mkdir(parents=True, exist_ok=True)
    report, state_cases = {}, {}
    for c in sys["tracker"]:
        case = assemble(c, sys, links, skill)
        fp = fingerprint(case)
        keys = [d["_source"]["ref"] for d in case["documents"]] + [n["_source"]["ref"] for n in case["notes"]] + [s["_source"]["ref"] for s in case["messages"]]
        before = prev["cases"].get(c["case_id"])
        path = engine.CASES_DIR / f"{c['case_id']}.json"
        if before is None or before["fingerprint"] != fp or not path.exists():
            path.write_text(json.dumps(case, indent=2))
        status = "new" if before is None else ("updated" if before["fingerprint"] != fp else "unchanged")
        report[c["case_id"]] = {"status": status, "items": len(keys), "new_items": [k for k in keys if before and k not in before["items"]]}
        state_cases[c["case_id"]] = {"fingerprint": fp, "items": keys}
    for stale in engine.CASES_DIR.glob("*.json"):
        if stale.stem not in state_cases:
            stale.unlink()

    unmatched = [{"key": it["key"], "system": SYSTEM_LABEL[kind], "summary": item_text(kind, it)[:160], "why": links.get(it["key"], {}).get("how", "")}
                 for kind in ("inbox",) for it in sys[kind] if not links.get(it["key"], {}).get("case")]
    state = {"gathered_at": datetime.now().isoformat(timespec="seconds"), "cases": state_cases, "links": links,
             "unmatched": unmatched, "usage": usage,
             "counts": {"tracker": len(sys["tracker"]), "inbox": len(sys["inbox"]), "notes": len(sys["notes"]), "slack": len(sys["slack"])}}
    GATHER_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
    return {"report": report, "state": state}


def score(state: dict | None = None) -> dict:
    """Linking accuracy against the known truth: did each item land on the right case (or on none)?"""
    state = state or load_state()
    gold = json.loads(GOLD_FILE.read_text())["cases"]
    truth = {k: cid for cid, parts in gold.items() for part, ks in parts.items() if part != "messages" for k in ks}
    rows, right = [], 0
    for key, link in state["links"].items():
        if "SIM" in key:
            continue
        want, got = truth.get(key), link.get("case")
        ok = want == got
        right += ok
        if not ok or link["method"] != "exact":
            rows.append({"key": key, "expected": want, "got": got, "method": link["method"], "how": link["how"], "ok": ok})
    linked = [l for l in state["links"].values() if l.get("case")]
    scored = sum(1 for k in state["links"] if "SIM" not in k)
    return {"items": scored, "correct": right, "accuracy": right / max(1, scored),
            "exact": sum(l["method"] == "exact" for l in state["links"].values()),
            "ai": sum(l["method"] == "ai" for l in state["links"].values()),
            "thread": sum(l["method"] == "thread" for l in state["links"].values()),
            "linked": len(linked), "rows": rows}


# --------------------------------------------------------------- simulations
# New information arriving, for demos. Simulated items are marked so they can be removed again.

SIMULATIONS = {
    "fax_1081": {
        "title": "A fax arrives with CASE-1081's missing severity score",
        "detail": "Metro Allergy Partners faxes a visit note for \u201cK. Asante\u201d. No case number, an abbreviated name: the AI has to match it.",
        "item": {"key": "D-SIM1", "received": "2026-10-19", "doc_type": "Chart notes (provider office fax)", "channel": "fax",
                 "from_office": "Metro Allergy Partners", "patient_name": "K. Asante", "patient_dob": None,
                 "content": "10/19/2026 visit: IGA 3, BSA 16%, eczematous plaques on trunk and both arms. Severity: moderate-to-severe.",
                 "simulated": True},
    },
    "slack_1108": {
        "title": "A teammate saves a Slack update about CASE-1108",
        "detail": "Lee posts that the office corrected the Quinn PA form, then tags @kb add. It reaches the case through the knowledge base, not the case record.",
        "item": {"key": "S-SIM1", "channel": "#access-ops", "ts": "2026-10-19T09:40:00-04:00", "author": "Lee W.", "thread": None,
                 "text": "Lakeside called back about the Quinn Humira request: patient never had the biosimilar, the PA form was filled in wrong",
                 "simulated": True},
    },
}


def simulate(name: str) -> None:
    sim = SIMULATIONS[name]
    sys = load_systems()
    if name == "fax_1081":
        dob = next(c["patient"]["dob"] for c in sys["tracker"] if c["case_id"] == "CASE-1081")
        item = {**sim["item"], "patient_dob": dob}
        save_system("inbox", [d for d in sys["inbox"] if d["key"] != item["key"]] + [item])
    else:
        import kb
        item = sim["item"]
        save_system("slack", [m for m in sys["slack"] if m["key"] != item["key"]] + [item])
        kb.tag_thread(item["key"], "Lee W.", "Quinn case: office corrected the PA form")


def active_simulations() -> set[str]:
    keys = {it["key"] for kind in ("inbox", "slack") for it in load_systems()[kind] if it.get("simulated")}
    return {name for name, sim in SIMULATIONS.items() if sim["item"]["key"] in keys}


def reset_simulations() -> None:
    import kb
    sys = load_systems()
    sim_keys = {it["key"] for kind in ("inbox", "slack") for it in sys[kind] if it.get("simulated")}
    save_system("inbox", [d for d in sys["inbox"] if not d.get("simulated")])
    save_system("slack", [m for m in sys["slack"] if not m.get("simulated") and m.get("thread") not in sim_keys])
    for e in kb.entries():
        if e["slack_thread"] in sim_keys:
            kb.remove(e["id"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    args = ap.parse_args()
    res = gather()
    st, rep = res["state"], res["report"]
    u = st["usage"]
    print(f"gathered {len(rep)} cases from {st['counts']} | AI judged {u.get('items_judged', 0)} loose items, {u.get('items_cached', 0)} from cache")
    for cid, r in rep.items():
        if r["status"] != "unchanged":
            print(f"  {cid}: {r['status']}" + (f" (+{len(r['new_items'])} new)" if r["new_items"] else ""))
    print(f"unmatched: {len(st['unmatched'])}")
    for x in st["unmatched"]:
        print(f"  {x['key']} [{x['system']}] {x['summary'][:70]}... -> {x['why'][:90]}")
    if args.score:
        s = score(st)
        print(f"\nlinking accuracy {s['correct']}/{s['items']} ({s['accuracy']:.0%}) | exact {s['exact']} · AI {s['ai']} · thread {s['thread']}")
        for r in s["rows"]:
            print(f"  {'✓' if r['ok'] else '✗'} {r['key']} expected {r['expected']} got {r['got']} [{r['method']}] {r['how'][:90]}")
