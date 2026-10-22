"""Offline checks that documents are matched to the right case when patients share a name and date of birth.
Run:  .venv/bin/python test_matching.py      (no API calls: the AI step is replaced by a stand-in)"""

import tempfile
from pathlib import Path

import engine
import gather

skill = engine.load_skill()


def case(cid, name, dob, drug, office, status="DENIED"):
    return {"case_id": cid, "patient": {"name": name, "dob": dob}, "drug": drug, "payer_policy": "northwind_commercial",
            "pa": {"status": status, "reason_code": "ST-DOC"}, "prescriber": {"name": "Dr. X", "office": office}}


def doc(key, name, dob, content, case_ref=None):
    return {"key": key, "doc_type": "Chart notes", "received": "2026-10-20", "channel": "fax", "from_office": "",
            "patient_name": name, "patient_dob": dob, "content": content, **({"case_ref": case_ref} if case_ref else {})}


SYS = {
    "tracker": [
        case("CASE-2001", "Ana Lee", "1980-01-01", "Dupixent", "Riverside Dermatology"),   # the same patient,
        case("CASE-2002", "Ana Lee", "1980-01-01", "Humira", "Hudson Valley Rheumatology"),  # two open PAs
        case("CASE-2003", "Bo Park", "1990-02-02", "Dupixent", "Metro Allergy"),
        case("CASE-2004", "Bo Park", "1990-02-02", "Humira", "Metro Allergy", status="APPROVED"),  # closed: not a candidate
        case("CASE-2005", "Cy Diaz", "1975-03-03", "Dupixent", "Coastal Dermatology"),
    ],
    "inbox": [
        doc("D-1", "Ana Lee", "1980-01-01", "Methotrexate 15 mg weekly, inadequate response. Plan: adalimumab."),
        doc("D-2", "Bo Park", "1990-02-02", "Topical steroid trial notes."),
        doc("D-3", "Ana Lee", "1980-01-01", "PA request form.", case_ref="CASE-2002"),
        doc("D-4", "ana  LEE", "1980-01-01", "Chart notes, no drug named."),
    ],
    "notes": [], "slack": [],
}


def check(name, ok):
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return ok


links, ties = gather.exact_links(SYS)

# a stand-in for the AI step: records what it was offered, picks the Humira case for D-1, answers "" for D-4
seen_prompts = []


def fake_claude(**kw):
    seen_prompts.append(kw["user"])
    return {"links": [{"key": "D-1", "case_id": "CASE-2002", "confidence": "high", "reason": "Methotrexate and adalimumab: the Humira case."},
                      {"key": "D-4", "case_id": "CASE-2005", "confidence": "high", "reason": "A case it wasn't offered."}]}, \
           {"input_tokens": 0, "output_tokens": 0}


real_claude, real_cache = engine.call_claude, gather.LINK_CACHE
engine.call_claude, gather.LINK_CACHE = fake_claude, Path(tempfile.mkdtemp())
try:
    loose = [("inbox", d) for d in SYS["inbox"] if d["key"] not in links]
    decisions, _ = gather.ai_links(loose, SYS, skill, ties)
finally:
    engine.call_claude, gather.LINK_CACHE = real_claude, real_cache
prompt = seen_prompts[0] if seen_prompts else ""
d1_block = prompt.split('<item key="D-1">')[1].split("</item>")[0] if '<item key="D-1">' in prompt else ""

results = [
    check("Same name + DOB on two open cases is NOT auto-matched", "D-1" not in links and ties.get("D-1") == ["CASE-2001", "CASE-2002"]),
    check("Name matching ignores case and spacing, and still sees the tie", ties.get("D-4") == ["CASE-2001", "CASE-2002"]),
    check("A closed case doesn't count: one open case fits, so it auto-matches", links.get("D-2", ("",))[0] == "CASE-2003"),
    check("A case number on the document wins over a name + DOB tie", links.get("D-3", ("",))[0] == "CASE-2002" and "D-3" not in ties),
    check("The AI step is offered only the tied cases, and told why",
          "CASE-2001" in d1_block and "CASE-2002" in d1_block and "CASE-2003" not in d1_block and "CASE-2005" not in d1_block
          and "same name and date of birth" in d1_block),
    check("The AI picks between the tied cases using the drug", decisions.get("D-1", {}).get("case_id") == "CASE-2002"),
    check("A case the AI wasn't offered is rejected (left for a person)", decisions.get("D-4", {}).get("case_id") == ""),
]
raise SystemExit(0 if all(results) else 1)
