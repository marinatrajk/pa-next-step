"""PA next-step engine: gate -> assemble context -> Claude -> guardrails.

The engine is generic. Everything specific to a workflow (SOP, payer rules,
actions, escalation owners, model settings) lives in a skill pack folder, so
another team reuses this file by writing a new pack, not new code.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import os

import anthropic

ROOT = Path(__file__).parent

# Local secrets: ANTHROPIC_API_KEY=... in .env (gitignored). Real env vars win.
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
CASES_DIR = ROOT / "data" / "cases"
RUNS_DIR = ROOT / "data" / "runs"
LOG_FILE = ROOT / "data" / "logs" / "decisions.jsonl"
DEFAULT_SKILL = ROOT / "skills" / "pa_next_step"

ACTION_LABELS = {
    "RESUBMIT_WITH_DOCS": "Resubmit with documentation",
    "REQUEST_RECORDS": "Request records from provider",
    "STEP_THERAPY_EXCEPTION": "Submit step-therapy exception (LMN)",
    "FORMULARY_EXCEPTION": "Submit formulary exception",
    "PEER_TO_PEER": "Coordinate peer-to-peer review",
    "REVERIFY_BENEFITS": "Re-verify benefits",
    "HOLD_FOR_ESCALATION": "Hold and escalate",
}

REASON_LABELS = {
    "ST-DOC": "Step-therapy notes missing",
    "ST-NOTMET": "Step therapy not met",
    "NF-PREF": "Non-preferred drug",
    "CLIN-INFO": "Clinical info missing",
    "ELIG": "Coverage not active",
}

TIER_LABELS = {
    1: "Payer policy (authoritative on criteria)",
    2: "SOP (authoritative on process)",
    3: "Provider record (clinical evidence)",
    4: "Operator note (secondhand)",
    5: "Team knowledge base (saved from Slack; context only)",
}

# The three separate places information comes from. The prompt keeps them in separate sections.
SECTIONS = {
    "official_rules": ("Official rules", "SOP and payer policies, maintained by process owners. Authoritative.", (1, 2)),
    "case_record": ("Case record", "This case, its documents and its case notes. Facts about this case.", (3, 4)),
    "team_knowledge": ("Team knowledge base", "Slack conversations operators saved with @kb. Useful context; never authoritative.", (5,)),
}


def section_of(tier: int) -> str:
    return next(k for k, (_, _, tiers) in SECTIONS.items() if tier in tiers)


# ---------------------------------------------------------------- loading


@dataclass
class Skill:
    path: Path
    manifest: dict
    sop: str
    policies: dict[str, str]

    @property
    def triggers(self) -> dict:
        return self.manifest["triggers"]


def load_skill(path: Path = DEFAULT_SKILL) -> Skill:
    manifest = json.loads((path / "skill.json").read_text())
    sop = (path / manifest["sop_file"]).read_text()
    policies = {p.stem: p.read_text() for p in sorted((path / manifest["payer_policies_dir"]).glob("*.md"))}
    return Skill(path=path, manifest=manifest, sop=sop, policies=policies)


def list_cases() -> list[str]:
    return sorted(p.stem for p in CASES_DIR.glob("*.json"))


def cases_by_urgency(skill: "Skill") -> list[str]:
    """Least time left in the payer window first; cases without a deadline last."""
    def hours(cid: str) -> float:
        h = gate(skill, load_case(cid)).hours_left
        return float("inf") if h is None else h
    return sorted(list_cases(), key=hours)


def load_case(case_id: str) -> dict:
    """Simulated case-system + Slack integration: one JSON file per case."""
    return json.loads((CASES_DIR / f"{case_id}.json").read_text())


def case_summary(skill: "Skill", case_id: str) -> dict:
    """Display fields for a case: short drug name, payer, plain-language status, hours left."""
    import re
    c = load_case(case_id)
    g = gate(skill, c)
    code = c["pa"]["reason_code"]
    return {
        "id": case_id,
        "drug": re.split(r"\s\d", c["drug"].split(" (")[0])[0],
        "payer": c["payer_policy"].replace("_", " ").title(),
        "status": f"{c['pa']['status'].title()} · {REASON_LABELS.get(code, code)}",
        "code": code,
        "hours": g.hours_left,
    }


# ------------------------------------------------------------------- gate


@dataclass
class Gate:
    applies: bool
    reasons: list[str] = field(default_factory=list)  # technical, for logs and reviewers
    notes: list[str] = field(default_factory=list)  # the same reasons in plain words, for operators
    hours_left: float | None = None
    deadline_at_risk: bool = False


def _get(d: dict, dotted: str):
    for key in dotted.split("."):
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


def gate(skill: Skill, case: dict) -> Gate:
    """Deterministic checks. Dates and applicability are never left to the model."""
    m = skill.manifest
    reasons, notes = [], []
    missing = [f for f in m["required_case_fields"] if _get(case, f) in (None, "")]
    if missing:
        reasons.append(f"Missing required case fields: {', '.join(missing)}")
        notes.append("The case record is missing details this tool needs (" + ", ".join(f.split(".")[-1].replace("_", " ") for f in missing)
                     + "). Update the case in the case system, then try again.")
    status = _get(case, "pa.status")
    if status and status not in m["applies_to_status"]:
        reasons.append(f"Status {status} is out of scope (skill handles {', '.join(m['applies_to_status'])})")
        notes.append(f"This tool only handles denied or pended requests, and this one is {status.lower()}.")
    policy = case.get("payer_policy")
    if policy and policy not in skill.policies:
        reasons.append(f"No payer policy '{policy}' in this skill pack")
        notes.append(f"This tool doesn't have {policy.replace('_', ' ').title()}'s prior-auth rules yet, so it can't recommend a next step.")

    hours_left = None
    if _get(case, "pa.window_closes") and case.get("as_of"):
        closes = datetime.fromisoformat(case["pa"]["window_closes"])
        as_of = datetime.fromisoformat(case["as_of"])
        hours_left = round((closes - as_of).total_seconds() / 3600, 1)

    return Gate(
        applies=not reasons,
        reasons=reasons,
        notes=notes,
        hours_left=hours_left,
        deadline_at_risk=hours_left is not None and hours_left < m["deadline_risk_hours"],
    )


# ---------------------------------------------------------------- context


def build_sources(skill: Skill, case: dict) -> list[dict]:
    """Every piece of context gets an ID and a trust tier (SOP section 2)."""
    sources = [
        {"id": "POLICY", "tier": 1, "label": f"Payer policy: {case['payer_policy']}", "text": skill.policies[case["payer_policy"]]},
        {"id": "SOP", "tier": 2, "label": "SOP OPS-PA-014", "text": skill.sop},
    ]
    record = {k: case[k] for k in ("case_id", "patient", "drug", "indication", "prescriber", "pa")}
    # the analysis never needs the patient's name or date of birth: those are only for matching faxes to the case
    record["patient"] = {k: v for k, v in case["patient"].items() if k not in ("name", "dob")}
    sources.append({"id": "CASE", "tier": 3, "label": "Case record (case system)", "text": json.dumps(record, indent=2)})
    for doc in case.get("documents", []):
        sources.append({"id": doc["doc_id"], "tier": 3, "label": f"{doc['type']} (received {doc['received']})", "text": doc["content"]})
    for i, note in enumerate(case.get("notes", []), 1):
        sources.append({"id": f"NOTE-{i}", "tier": 4, "label": f"{note['author']}, {note['ts']}", "text": note["text"]})
    for i, msg in enumerate(case.get("messages", []), 1):  # older case files only; Slack now arrives via the knowledge base
        sources.append({"id": f"MSG-{i}", "tier": 5, "label": f"{msg['channel']} / {msg['author']}, {msg['ts']}", "text": msg["text"]})
    import kb
    for e in kb.for_case(case):
        scope = "about this case" if e["case_id"] == case["case_id"] else "general know-how"
        sources.append({"id": e["id"], "tier": 5, "label": f"{e['title']} ({scope}; saved by {e['added_by']}, {e['added_at'][:10]})",
                        "text": f"{e['summary']}\nThread:\n{e['transcript']}"})
    return sources


SYSTEM_PROMPT = """You are the next-step assistant for Forus Access Operations. An operator gives you one prior-authorization case that was denied or pended. Recommend the single next action under the SOP, show the evidence behind it, and be explicit about what you cannot determine and what a human must decide. The operator reviews your output before anything is sent; you never contact payers or providers yourself.

How to work:
- Follow the SOP procedure (section 4) in order: map the reason code, list every payer criterion for this drug, find evidence for each, check the deadline, check every escalation trigger (section 6), then choose the action.
- Every source has an ID and a trust tier. When sources disagree, apply the SOP's source-of-truth hierarchy (section 2). Cite source IDs for every criterion, conflict and trigger, and cite only IDs that appear in the sources.
- A clinical fact counts as established only when a provider record states it. Operator notes and team messages are leads to verify, not evidence.
- The input has three separate sections: official rules (SOP, payer policy), the case record (case, documents, case notes) and the team knowledge base (Slack conversations operators saved). Never treat team knowledge as a rule or as a fact about the patient; use it as context and say when a step relies on it.
- A team message or knowledge-base entry saying a payer rule changed is not actionable unless the payer policy or its bulletin confirms it.
- "unknown" is a correct answer. If the evidence is not in the sources, say so instead of inferring it, and do not fill gaps with your own clinical knowledge. Whether a finding counts as a contraindication or intolerance is a Clinical Intelligence decision (E1), even when a teammate is confident.
- Use the deadline facts the system computed; do not recompute dates.
- Rule on every trigger in trigger_checks (E1 to E6), one by one, with a reason even when it does not fire. Name the source IDs in the reason. A trigger fires when its SOP condition is met, whether or not another trigger already fired.
- If an escalation trigger fires, still list payer-facing work that can continue in parallel (for example, chasing records) in parallel_actions.
- The draft body is the full message, never empty or a placeholder. It must be ready to send after a quick read: short, specific, with the case ID and exactly what is needed. For an escalation, address the owner named in SOP section 6 and state the decision you need from them.
- Keep every text field concise. Operators scan this in under a minute.
- operator_summary is the only thing the operator sees at first; everything else sits behind a "details" toggle. Write it last, in plain words with no trigger codes, source IDs or SOP section numbers:
  - headline: the next step as an imperative, at most 8 words (for example "Hold the case and escalate to your Team Lead").
  - why: 2 or 3 bullets, each under 15 words, giving the reasons a busy operator needs to trust the step.
  - then: 1 to 3 things the operator does after sending the draft, each under 12 words."""


def render_user_message(case: dict, sources: list[dict], g: Gate, skill: Skill) -> str:
    parts = [
        f"<system_facts>\nas_of: {case['as_of']}\nwindow_closes: {case['pa']['window_closes']}\n"
        f"hours_left_in_window: {g.hours_left}\n"
        f"deadline_at_risk (under {skill.manifest['deadline_risk_hours']}h): {g.deadline_at_risk}\n"
        f"submission_count: {case['pa']['submission_count']}\n</system_facts>"
    ]
    for key, (name, about, tiers) in SECTIONS.items():
        mine = [s for s in sources if s["tier"] in tiers]
        body = "\n\n".join(f'<source id="{s["id"]}" tier="{s["tier"]}: {TIER_LABELS[s["tier"]]}" label="{s["label"]}">\n{s["text"]}\n</source>' for s in mine)
        parts.append(f'<{key} title="{name}" about="{about}">\n{body or "(nothing)"}\n</{key}>')
    parts.append(f"Case {case['case_id']}: what is the next step?")
    return "\n\n".join(parts)


def output_schema(skill: Skill) -> dict:
    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}

    s, ids = {"type": "string"}, {"type": "array", "items": {"type": "string"}}
    return obj({
        "situation": s,
        "criteria": {"type": "array", "items": obj({
            "criterion": s,
            "status": {"type": "string", "enum": ["met", "not_met", "unknown"]},
            "evidence": s,
            "source_ids": ids,
        })},
        "conflicts": {"type": "array", "items": obj({"description": s, "source_ids": ids, "resolution": s})},
        "missing_information": {"type": "array", "items": obj({"item": s, "why_it_matters": s, "how_to_get": s})},
        # One required verdict per trigger so none is skipped by omission. No per-trigger citation list:
        # the API caps output-format size, so the reason names its source IDs in the text.
        "trigger_checks": obj({t: obj({"fires": {"type": "boolean"}, "reason": s}) for t in skill.triggers}),
        "primary_action": {"type": "string", "enum": skill.manifest["actions"]},
        "action_rationale": s,
        "parallel_actions": {"type": "array", "items": s},
        "could_not_determine": {"type": "array", "items": s},
        "needs_human_judgment": {"type": "array", "items": s},
        "draft": obj({
            "kind": {"type": "string", "enum": ["resubmission_note", "records_request", "escalation_message", "peer_to_peer_request", "benefits_reverification_note"]},
            "to": s,
            "subject": s,
            "body": s,
        }),
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "operator_summary": obj({
            "headline": s,
            "why": {"type": "array", "items": s},
            "then": {"type": "array", "items": s},
        }),
    })


# ------------------------------------------------------------------ model


_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


_calls_lock = threading.Lock()


def count_call() -> None:
    """A hosted demo sets MAX_MODEL_CALLS_PER_DAY so a shared link can't run up the API bill."""
    limit = int(os.environ.get("MAX_MODEL_CALLS_PER_DAY") or 0)
    if not limit:
        return
    path = ROOT / "data" / "logs" / "model_calls.json"
    with _calls_lock:
        today = datetime.now().date().isoformat()
        counts = json.loads(path.read_text()) if path.exists() else {}
        if counts.get(today, 0) >= limit:
            raise RuntimeError(f"This demo has reached its limit of {limit} AI calls for today. It resets at midnight; "
                               "saved analyses still work.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({today: counts.get(today, 0) + 1}))


def call_claude(*, model: str, effort: str, system: str, user: str | list, schema: dict, max_tokens: int = 16000,
                on_field=None) -> tuple[dict, dict]:
    """One structured-output call. Server-side fallback re-runs a refused request on another model.
    With on_field, the answer is streamed and on_field(name) is called as each top-level field starts."""
    count_call()
    kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}],
                  output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                  betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    if on_field is None:
        response = client().beta.messages.create(**kwargs)
    else:
        fields, seen, text = list(schema["properties"]), 0, ""
        with client().beta.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "content_block_start" and event.content_block.type == "thinking":
                    on_field("_thinking")
                elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                    text += event.delta.text
                    # fields arrive in schema order: look for the next ones only, so nested keys can't match
                    for i in range(seen, len(fields)):
                        if re.search(rf'"{fields[i]}"\s*:', text[-400:]):
                            seen = i + 1
                            on_field(fields[i])
                            break
            response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Model declined the request ({response.stop_details})")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Response hit max_tokens before finishing")
    text = next(b.text for b in response.content if b.type == "text")
    usage = {"model": response.model, "input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
    return json.loads(text), usage


# ------------------------------------------------------------- guardrails


def apply_guardrails(skill: Skill, result: dict, g: Gate, sources: list[dict]) -> tuple[dict, list[str]]:
    """Code-enforced rules from the SOP. Each override is reported, never silent."""
    notes = []
    known = {s["id"] for s in sources}

    cited = [sid for section in ("criteria", "conflicts", "triggers_fired") for item in result[section] for sid in item["source_ids"]]
    bad = sorted({sid for sid in cited if sid not in known})
    if bad:
        notes.append(f"Citations to unknown sources removed: {', '.join(bad)}. Treat the claims that used them with caution.")
        for section in ("criteria", "conflicts", "triggers_fired"):
            for item in result[section]:
                item["source_ids"] = [sid for sid in item["source_ids"] if sid in known]

    fired = {t["id"] for t in result["triggers_fired"]}
    has_gaps = any(c["status"] == "unknown" for c in result["criteria"]) or bool(result["missing_information"])
    if g.deadline_at_risk and has_gaps and "E4" not in fired:
        result["triggers_fired"].append({
            "id": "E4",
            "reason": f"Added by guardrail: {g.hours_left}h left in the payer window and required information is still missing.",
            "source_ids": ["CASE"],
        })
        fired.add("E4")
        notes.append("Guardrail added E4 (deadline at risk): under the threshold with open gaps.")

    blocking = sorted(t for t in fired if skill.triggers[t]["blocks_submission"])
    if blocking and result["primary_action"] in skill.manifest["payer_facing_submissions"]:
        notes.append(f"Guardrail changed action {result['primary_action']} -> HOLD_FOR_ESCALATION: SOP forbids payer submission while {', '.join(blocking)} is open.")
        result["parallel_actions"].insert(0, f"After escalation resolves, consider {result['primary_action']}.")
        result["primary_action"] = "HOLD_FOR_ESCALATION"

    if result["primary_action"] == "HOLD_FOR_ESCALATION" and not fired:
        notes.append("Model chose HOLD_FOR_ESCALATION without naming a trigger. Review before accepting.")

    if len(result["draft"]["body"].strip()) < 80:
        notes.append("Draft is empty or too short to send. Write it before accepting.")

    result["escalate"] = bool(fired)
    result["escalate_to"] = sorted({o.strip() for t in fired for o in skill.triggers[t]["owner"].split("+")})
    return result, notes


# -------------------------------------------------------------------- run


# what the operator sees in the action log while each output field is being written
FIELD_STEPS = {"_thinking": "Thinking it through", "situation": "Reading the denial and where the case stands",
               "criteria": "Checking each insurer requirement against the records", "conflicts": "Looking for information that conflicts",
               "missing_information": "Listing what's still missing", "trigger_checks": "Checking the six escalation triggers (E1–E6)",
               "primary_action": "Choosing the next step", "needs_human_judgment": "Flagging what needs a person's judgment",
               "draft": "Writing the draft message", "operator_summary": "Writing the short summary"}


def run_case(case_id: str, skill: Skill | None = None, save: bool = True, on_step=None) -> dict:
    """Analyze one case. on_step(text, seconds) is called for each action, for a live log; the log is saved with the run."""
    skill = skill or load_skill()
    t0, log = time.time(), []

    def step(text: str) -> None:
        log.append({"t": round(time.time() - t0, 1), "text": text})
        if on_step:
            on_step(text, log[-1]["t"])

    case = load_case(case_id)
    g = gate(skill, case)
    run = {
        "case_id": case_id,
        "skill": f"{skill.manifest['id']}@{skill.manifest['version']}",
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "gate": g.__dict__,
        "log": log,
    }
    if not g.applies:
        step("Scope check: this tool doesn't cover this case, so it goes to a person")
        run["status"] = "not_applicable"
        return run
    step(f"Scope check: {case['payer_policy'].replace('_', ' ').title()} rules on file"
         + ("" if g.hours_left is None else f", {g.hours_left:.0f} hours left in the window" if g.hours_left < 48
            else f", {g.hours_left / 24:.0f} days left in the window"))

    sources = build_sources(skill, case)
    kb_ids = [s["id"] for s in sources if s["tier"] == 5]
    docs = len(case.get("documents", []))
    step(f"Gathered the SOP and insurer policy, the case record ({docs} document{'' if docs == 1 else 's'}, "
         f"{len(case.get('notes', []))} note{'' if len(case.get('notes', [])) == 1 else 's'}) "
         f"and {len(kb_ids)} team-knowledge entr{'y' if len(kb_ids) == 1 else 'ies'}"
         + (f" ({', '.join(kb_ids)})" if kb_ids else ""))
    step(f"Asked {skill.manifest['model']} for the next step")
    started = time.time()
    result, usage = call_claude(
        model=skill.manifest["model"],
        effort=skill.manifest["effort"],
        system=SYSTEM_PROMPT,
        user=render_user_message(case, sources, g, skill),
        schema=output_schema(skill),
        on_field=lambda f: f in FIELD_STEPS and step(FIELD_STEPS[f]),
    )
    raw_action = result["primary_action"]
    result["triggers_fired"] = [{"id": t, "reason": c["reason"], "source_ids": []} for t, c in result["trigger_checks"].items() if c["fires"]]
    result, notes = apply_guardrails(skill, result, g, sources)
    fired = [t["id"] for t in result["triggers_fired"]]
    step(f"Model's pick: {ACTION_LABELS.get(raw_action, raw_action)}" + (f" · triggers {', '.join(fired)}" if fired else " · no escalation triggers"))
    step("Safety checks passed: citations, escalation rules, draft" if not notes
         else f"Safety checks: {len(notes)} correction{'s' if len(notes) != 1 else ''} made" + (
             f", final step {ACTION_LABELS.get(result['primary_action'], result['primary_action'])}" if result["primary_action"] != raw_action else ""))
    step(f"Saved · {usage['input_tokens']:,} tokens read, {usage['output_tokens']:,} written")
    run.update({
        "status": "ok",
        "result": result,
        "model_action": raw_action,
        "guardrail_notes": notes,
        "usage": usage | {"seconds": round(time.time() - started, 1)},
        "input_fingerprint": input_fingerprint(skill, case),
    })
    if save:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        (RUNS_DIR / f"{case_id}.json").write_text(json.dumps(run, indent=2))
    return run


def last_run(case_id: str) -> dict | None:
    path = RUNS_DIR / f"{case_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def provenance(case: dict) -> dict:
    """Source id (DOC-1077-02, NOTE-1, MSG-3) -> where the gatherer found it and how it was matched."""
    out = {d["doc_id"]: d.get("_source", {}) for d in case.get("documents", [])}
    out |= {f"NOTE-{i}": n.get("_source", {}) for i, n in enumerate(case.get("notes", []), 1)}
    out |= {f"MSG-{i}": m.get("_source", {}) for i, m in enumerate(case.get("messages", []), 1)}
    return out


def input_fingerprint(skill: Skill, case: dict) -> str:
    """Hash of exactly what the analysis reads. Changes only when a source item or the skill pack changes."""
    import hashlib
    parts = [(s["id"], s["tier"], s["text"]) for s in build_sources(skill, case)] + [skill.manifest["version"]]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


def is_current(run: dict | None, skill: Skill, case: dict) -> bool:
    """True when a saved analysis was made from the same information the case holds now."""
    return bool(run) and run.get("status") == "ok" and run.get("input_fingerprint") == input_fingerprint(skill, case)


def log_decision(case_id: str, run: dict, decision: str, reason: str = "", final_action: str | None = None,
                 edited_draft: str | None = None, view: str = "") -> dict:
    """Operator decisions are the feedback loop: overrides become new eval cases."""
    r, d = run["result"], run["result"]["draft"]
    as_drafted = {d["body"].strip(), f"Subject: {d['subject']}\n\n{d['body']}".strip()}
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "case_id": case_id,
        "skill": run.get("skill"),
        "run_at": run.get("ran_at"),
        "view": view,
        "recommended_action": r["primary_action"],
        "headline": (r.get("operator_summary") or {}).get("headline", ""),
        "escalate": r.get("escalate", False),
        "triggers": [t["id"] for t in r.get("triggers_fired", [])],
        "decision": decision,
        "final_action": final_action or r["primary_action"],
        "reason": reason,
        "draft_edited": edited_draft is not None and edited_draft.strip() not in as_drafted,
        "draft_text": edited_draft if edited_draft is not None else d["body"],
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_decisions() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    return [json.loads(line) for line in LOG_FILE.read_text().splitlines() if line.strip()]


def add_eval_candidate(skill: Skill, entry: dict) -> bool:
    """An override, queued for an expert to turn into a labeled eval case. Returns False if already queued."""
    path = skill.path / "evals" / "candidates.json"
    candidates = json.loads(path.read_text()) if path.exists() else []
    if any(c["from_decision"] == entry["ts"] and c["case_id"] == entry["case_id"] for c in candidates):
        return False
    candidates.append({
        "case_id": entry["case_id"],
        "from_decision": entry["ts"],
        "tool_said": entry["recommended_action"],
        "operator_said": entry["final_action"],
        "operator_reason": entry["reason"],
        "status": "needs expert label",
    })
    path.write_text(json.dumps(candidates, indent=2))
    return True
