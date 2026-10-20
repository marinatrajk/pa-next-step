"""Document intake: raw files in any common format -> one schema -> the document inbox.

    .venv/bin/python ingest.py            # process every new file in data/raw_inbox/
    .venv/bin/python ingest.py FILE...    # process specific files

Each format gets the cheapest reliable reader:
  FHIR JSON (EHR export)     parsed by code, no AI
  email (.eml)               headers parsed by code; the body read by AI
  digital PDF                read by AI (text layer and layout)
  scanned PDF, photo, image  read by AI vision (no text layer to rely on)
  plain text                 read by AI

Every result is mapped to skills/pa_next_step/document_schema.json, checked, and written to the document
inbox with a pointer to the original file. The gatherer then matches it to a case. A file is processed once:
it's remembered by a hash of its bytes.
"""

from __future__ import annotations

import base64
import email
import hashlib
import json
import re
import sys
from datetime import datetime
from email import policy
from pathlib import Path

import engine

RAW_DIR = engine.ROOT / "data" / "raw_inbox"
INTAKE_LOG = engine.ROOT / "data" / "intake" / "processed.json"
SCHEMA_FILE = engine.DEFAULT_SKILL / "document_schema.json"
INBOX = engine.ROOT / "data" / "systems" / "document_inbox.json"
DOC_LABEL = {"chart_note": "Chart notes", "prescriber_letter": "Prescriber letter", "lab_result": "Lab report",
             "insurer_decision": "Insurer decision letter", "insurance_card": "Insurance card", "referral": "Referral",
             "pa_form": "PA form", "medication_history": "Medication history (EHR)", "email_update": "Email from office",
             "fax_cover": "Fax cover sheet", "other": "Document"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
SUPPORTED = [".pdf", ".eml", ".json", ".txt", *IMAGE_TYPES]

EXTRACT_SYSTEM = """You turn one incoming document for a prior-authorization team into a fixed schema. Read it the way a careful
operator would: scanned faxes are skewed and speckled, photos are blurry, offices write names and dates inconsistently.

Rules:
- transcription: the document's text as written, line by line. Don't correct it or add to it.
- Record only what the document states. Unknown: "" for text, -1 for numbers, [] for lists. Never guess.
- Dates as YYYY-MM-DD when the document gives a full date; a month and year only as YYYY-MM; otherwise "".
- patient.name as "First Last" even if written "Last, First"; keep initials if that's all there is.
- medications: every drug trial or current therapy the document states, with dates, dose and outcome as written.
- contraindications: only what the document says about a drug class being unsafe or not tolerated; stated_by_prescriber
  is true only when the prescriber states it.
- evidence: a short quote (under 15 words) from the document.
- confidence: high = clean and complete; medium = readable but some fields unclear; low = hard to read or ambiguous.
- missing: the fields an operator would need from this kind of document that it doesn't have (e.g. "date of birth").
- relevance: related is true for anything a PA team handles about a patient's prescription or coverage: clinical notes,
  prescriber letters, labs, insurer letters, insurance cards, referrals, PA forms, pharmacy or office messages, fax covers.
  Anything else (a résumé, an invoice, an ad, a personal letter, a blank page) is unrelated: related false, reason = what
  it is in one plain sentence, doc_type "other", and leave every other field empty. Don't transcribe or summarize its contents."""


def schema() -> dict:
    s = json.loads(SCHEMA_FILE.read_text())
    return {k: v for k, v in s.items() if k not in ("name", "version", "about")}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def log() -> dict:
    return json.loads(INTAKE_LOG.read_text()) if INTAKE_LOG.exists() else {"files": {}}


# ------------------------------------------------------------------ readers


def detect(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "Digital PDF" if b"/Font" in path.read_bytes() else "Scanned PDF (no text layer)"
    if ext in IMAGE_TYPES:
        return "Photo / image"
    if ext == ".eml":
        return "Email"
    if ext == ".json":
        try:
            return "EHR export (FHIR)" if json.loads(path.read_text()).get("resourceType") == "Bundle" else "JSON"
        except Exception:
            return "JSON"
    return "Text"


def ai_extract(content: list) -> tuple[dict, dict]:
    skill = engine.load_skill()
    return engine.call_claude(model=skill.manifest["model"], effort="low", system=EXTRACT_SYSTEM, user=content,
                              schema=schema(), max_tokens=8000)


def read_pdf_or_image(path: Path) -> tuple[dict, dict, dict]:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    if path.suffix.lower() == ".pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": IMAGE_TYPES[path.suffix.lower()], "data": data}}
    rec, usage = ai_extract([block, {"type": "text", "text": "Map this document to the schema."}])
    return rec, usage, {}


def read_email(path: Path) -> tuple[dict, dict, dict]:
    msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    headers = {"from": str(msg["From"] or ""), "date": str(msg["Date"] or ""), "subject": str(msg["Subject"] or "")}
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    rec, usage = ai_extract([{"type": "text", "text": f"Email headers (parsed): {json.dumps(headers)}\n\nEmail body:\n{text}\n\nMap this document to the schema."}])
    m = re.match(r'\s*"?([^"<]+?)"?\s*<([^>]+)>', headers["from"])
    if m:  # the sender is known for sure from the headers: don't leave it to the model
        rec["sender"]["organization"] = rec["sender"]["organization"] or m.group(1).strip()
        rec["sender"]["contact"] = m.group(2).strip()
    try:
        rec["document_date"] = rec["document_date"] or email.utils.parsedate_to_datetime(headers["date"]).date().isoformat()
    except Exception:
        pass
    return rec, usage, headers


def read_fhir(path: Path) -> tuple[dict, dict, dict]:
    """Structured data needs no AI: map the FHIR resources straight onto the schema."""
    bundle = json.loads(path.read_text())
    res = [e["resource"] for e in bundle.get("entry", [])]
    pt = next((r for r in res if r["resourceType"] == "Patient"), {})
    org = next((r for r in res if r["resourceType"] == "Organization"), {})
    name = " ".join((pt.get("name") or [{}])[0].get("given", []) + [(pt.get("name") or [{}])[0].get("family", "")]).strip()
    meds, lines = [], [f"Patient: {name}, born {pt.get('birthDate', '?')}", f"Source: {org.get('name', '?')} (EHR export)"]
    for r in res:
        if r["resourceType"] != "MedicationStatement":
            continue
        text = r.get("medicationCodeableConcept", {}).get("text", "")
        per = r.get("effectivePeriod", {})
        reason = " ".join(x.get("text", "") for x in r.get("statusReason", []))
        dose = re.search(r"\d+(?:\.\d+)?\s*mg", text)
        drug = re.sub(r"\s*\d+(?:\.\d+)?\s*mg.*$", "", text).strip() or text
        outcome = "inadequate" if "inadequate" in reason.lower() else ("ongoing" if r.get("status") == "active" else "unknown")
        meds.append({"drug": drug, "dose": dose.group(0) if dose else "", "start": per.get("start", ""), "end": per.get("end", ""),
                     "outcome": outcome, "evidence": f"{text} {per.get('start', '')} to {per.get('end', '')} {reason}".strip()})
        lines.append(f"{text}: {per.get('start', '?')} to {per.get('end', 'ongoing')} ({r.get('status', '')}{'; ' + reason if reason else ''})")
    rec = {"relevance": {"related": True, "reason": ""}, "doc_type": "medication_history", "patient": {"name": name, "dob": pt.get("birthDate", ""), "member_id": ""},
           "sender": {"organization": org.get("name", ""), "person": "", "contact": ""}, "document_date": "",
           "medications": meds, "severity": {"iga": -1, "bsa_pct": -1, "das28": -1}, "labs": [], "contraindications": [],
           "insurer_decision": {"status": "", "reason_code": "", "reason": ""}, "coverage": {"insurer": "", "member_id": "", "effective_date": ""},
           "transcription": "\n".join(lines),
           "summary": f"EHR medication history from {org.get('name', 'the clinic')}: " + "; ".join(
               f"{m['drug']} {m['dose']} {m['start'] or '?'} to {m['end'] or 'ongoing'} ({m['outcome']})" for m in meds) + ".",
           "confidence": "high", "missing": []}
    das = re.search(r"DAS28\s*([\d.]+)", " ".join(m["evidence"] for m in meds))
    if das:
        rec["severity"]["das28"] = float(das.group(1))
    return rec, {"input_tokens": 0, "output_tokens": 0}, {}


def read_text(path: Path) -> tuple[dict, dict, dict]:
    rec, usage = ai_extract([{"type": "text", "text": f"{path.read_text()}\n\nMap this document to the schema."}])
    return rec, usage, {}


READERS = {"Digital PDF": read_pdf_or_image, "Scanned PDF (no text layer)": read_pdf_or_image, "Photo / image": read_pdf_or_image,
           "Email": read_email, "EHR export (FHIR)": read_fhir, "JSON": read_text, "Text": read_text}
READ_BY = {"EHR export (FHIR)": "code (structured)", "Email": "code (headers) + AI (body)"}


# ---------------------------------------------------------------- checking


def related(rec: dict) -> bool:
    return rec.get("relevance", {}).get("related", True)


def check(rec: dict) -> list[str]:
    """Problems a reviewer should see. The schema guarantees the shape; this checks the content."""
    issues = []
    if not related(rec):
        return issues
    for label, value in [("document date", rec["document_date"]), ("date of birth", rec["patient"]["dob"]),
                         *[(f"{m['drug']} dates", m["start"]) for m in rec["medications"]], *[(f"{m['drug']} dates", m["end"]) for m in rec["medications"]]]:
        if value and not re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", value):
            issues.append(f"{label} isn't a clean date: {value!r}")
    if not rec["patient"]["name"]:
        issues.append("no patient name: can't be matched to a case without a person")
    if rec["confidence"] == "low":
        issues.append("the reader wasn't confident in this document")
    return issues


# ------------------------------------------------------------------ intake


def process_file(path: Path, via: str = "inbox") -> dict:
    """Read one file, map it to the schema, add it to the document inbox (unless it's unrelated). Returns the item."""
    path = Path(path)
    h = sha(path)
    seen = log()
    if h in seen["files"]:
        return seen["files"][h]
    fmt = detect(path)
    started = datetime.now()
    rec, usage, extra = READERS[fmt](path)
    if not related(rec):  # not a PA document: keep only what it is, never its contents
        rec = {**rec, "transcription": "", "summary": rec["relevance"]["reason"], "patient": {"name": "", "dob": "", "member_id": ""}}
    issues = check(rec)
    item = {
        "key": f"D-IN-{h[:6]}",
        "received": rec["document_date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec["document_date"] or "") else datetime.fromtimestamp(path.stat().st_mtime).date().isoformat(),
        "doc_type": DOC_LABEL[rec["doc_type"]] if related(rec) else "Not a prior-auth document",
        "channel": {"Email": "email", "EHR export (FHIR)": "EHR feed", "Photo / image": "photo upload"}.get(fmt, "fax"),
        "from_office": rec["sender"]["organization"],
        "patient_name": rec["patient"]["name"],
        "patient_dob": rec["patient"]["dob"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec["patient"]["dob"] or "") else "",
        "content": rec["transcription"],
        "schema": rec,
        "raw_file": str(path.relative_to(engine.ROOT)) if path.is_relative_to(engine.ROOT) else str(path),
        "intake": {"format": fmt, "read_by": READ_BY.get(fmt, "AI (vision)" if fmt != "Text" else "AI"), "issues": issues,
                   "needs_review": bool(issues), "set_aside": not related(rec), "via": via, "processed_at": started.isoformat(timespec="seconds"),
                   "seconds": round((datetime.now() - started).total_seconds(), 1), "usage": usage, **({"email": extra} if extra else {})},
    }
    if related(rec):  # unrelated files are set aside: they never reach the inbox, a case or the AI matcher
        inbox = json.loads(INBOX.read_text())
        inbox["documents"] = [d for d in inbox["documents"] if d["key"] != item["key"]] + [item]
        INBOX.write_text(json.dumps(inbox, indent=2))
    elif via in ("upload", "slack"):  # nothing to keep an unrelated upload for
        path.unlink(missing_ok=True)
    seen["files"][h] = item
    INTAKE_LOG.parent.mkdir(parents=True, exist_ok=True)
    INTAKE_LOG.write_text(json.dumps(seen, indent=2))
    return item


def pending() -> list[Path]:
    done = log()["files"]
    return [p for p in sorted(RAW_DIR.iterdir()) if p.suffix.lower() in SUPPORTED and sha(p) not in done] if RAW_DIR.exists() else []


def remove(key: str) -> None:
    """Take an uploaded document back out (for demos): from the inbox, the intake log, and the raw folder."""
    inbox = json.loads(INBOX.read_text())
    item = next((d for d in inbox["documents"] if d["key"] == key), None)
    inbox["documents"] = [d for d in inbox["documents"] if d["key"] != key]
    INBOX.write_text(json.dumps(inbox, indent=2))
    seen = log()
    seen["files"] = {h: it for h, it in seen["files"].items() if it["key"] != key}
    INTAKE_LOG.write_text(json.dumps(seen, indent=2))
    if item and item["intake"].get("via") in ("upload", "slack"):
        (engine.ROOT / item["raw_file"]).unlink(missing_ok=True)


def process_inbox() -> list[dict]:
    return [process_file(p) for p in pending()]


if __name__ == "__main__":
    files = [Path(a) for a in sys.argv[1:]]
    items = [process_file(f) for f in files] if files else process_inbox()
    for it in items:
        s, i = it["schema"], it["intake"]
        print(f"{Path(it['raw_file']).name:<32} {i['format']:<28} -> {it['doc_type']:<26} patient '{it['patient_name']}' dob '{it['patient_dob']}' "
              f"| {len(s['medications'])} meds, {len(s['labs'])} labs | {s['confidence']} | read by {i['read_by']} {i['seconds']}s"
              + (f"\n    review: {'; '.join(i['issues'])}" if i["issues"] else ""))
    if not items:
        print("nothing new in data/raw_inbox")
