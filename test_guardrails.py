"""Offline checks that the code guardrails catch bad model output.  Run:  .venv/bin/python test_guardrails.py"""

import copy

import engine

skill = engine.load_skill()
case = engine.load_case("CASE-1077")
g = engine.gate(skill, case)
sources = engine.build_sources(skill, case)

BASE = {
    "situation": "", "criteria": [], "conflicts": [], "missing_information": [], "triggers_fired": [],
    "primary_action": "FORMULARY_EXCEPTION", "action_rationale": "", "parallel_actions": [],
    "could_not_determine": [], "needs_human_judgment": [],
    "draft": {"kind": "escalation_message", "to": "Team Lead", "subject": "CASE-1077", "body": "CASE-1077 needs a decision on the NF-PREF pend before the window closes; details and the two options are below."}, "confidence": "high",
}


def check(name, result, expect):
    out, notes = engine.apply_guardrails(skill, copy.deepcopy(result), g, sources)
    ok = expect(out, notes)
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    for n in notes:
        print(f"      {n}")
    return ok


results = [
    check(
        "Submission blocked while an unverified rule change (E3) is open",
        BASE | {"triggers_fired": [{"id": "E3", "reason": "", "source_ids": ["MSG-1"]}]},
        lambda o, n: o["primary_action"] == "HOLD_FOR_ESCALATION" and o["escalate"],
    ),
    check(
        "E4 added when model misses deadline risk with an unknown criterion",
        BASE | {"primary_action": "REQUEST_RECORDS",
                "criteria": [{"criterion": "Biosimilar trial", "status": "unknown", "evidence": "", "source_ids": ["DOC-1077-02"]}]},
        lambda o, n: any(t["id"] == "E4" for t in o["triggers_fired"]) and o["escalate"],
    ),
    check(
        "Made-up citation removed and reported",
        BASE | {"primary_action": "REQUEST_RECORDS",
                "criteria": [{"criterion": "Biosimilar trial", "status": "met", "evidence": "", "source_ids": ["DOC-9999"]}]},
        lambda o, n: o["criteria"][0]["source_ids"] == [] and any("DOC-9999" in x for x in n),
    ),
    check(
        "Empty draft is flagged",
        BASE | {"primary_action": "REQUEST_RECORDS", "draft": BASE["draft"] | {"body": ""}},
        lambda o, n: any("Draft is empty" in x for x in n),
    ),
    check(
        "Clean output passes through untouched",
        BASE | {"primary_action": "REQUEST_RECORDS"},
        lambda o, n: o["primary_action"] == "REQUEST_RECORDS" and not n and not o["escalate"],
    ),
]
raise SystemExit(0 if all(results) else 1)
