"""Backtest the skill against its labeled cases.

    .venv/bin/python run_evals.py            # fresh runs for every labeled case
    .venv/bin/python run_evals.py --reuse    # score the saved runs in data/runs (no new engine calls)
    .venv/bin/python run_evals.py CASE-1077  # one case

Action, escalation and triggers are scored exactly. The free-text checks
(must_flag / must_not) are graded by an LLM judge.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import engine

JUDGE_SYSTEM = """You grade the output of an operations assistant against an expert's checklist. For each check:
- kind "must_flag": passed = true if the output clearly conveys this point anywhere (wording may differ).
- kind "must_not": passed = true if the output does NOT do this thing. Mentioning the idea in order to reject it is fine.
Be strict: a vague gesture at the point does not count as conveying it. Give a one-sentence reason."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check": {"type": "string"},
                    "kind": {"type": "string", "enum": ["must_flag", "must_not"]},
                    "passed": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["check", "kind", "passed", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["checks"],
    "additionalProperties": False,
}


def judge(skill: engine.Skill, run: dict, label: dict) -> list[dict]:
    checks = [{"check": c, "kind": "must_flag"} for c in label["must_flag"]] + [{"check": c, "kind": "must_not"} for c in label["must_not"]]
    user = f"<assistant_output>\n{json.dumps(run['result'], indent=2)}\n</assistant_output>\n\n<checks>\n{json.dumps(checks, indent=2)}\n</checks>"
    out, _ = engine.call_claude(model=skill.manifest["judge_model"], effort="medium", system=JUDGE_SYSTEM, user=user, schema=JUDGE_SCHEMA, max_tokens=4000)
    return out["checks"]


def score_case(skill: engine.Skill, case_id: str, label: dict, reuse: bool) -> dict:
    run = engine.last_run(case_id) if reuse else None
    if run is not None and not engine.is_current(run, skill, engine.load_case(case_id)):
        run = None  # the case gained or lost information since this analysis: analyze again
    if run is None:
        run = engine.run_case(case_id, skill)
    if label.get("expected_status") == "not_applicable":  # correct behavior is to decline the case
        declined = run.get("status") == "not_applicable"
        return {"case_id": case_id, "label": label["label"], "expected_action": "NOT_APPLICABLE",
                "got_action": "NOT_APPLICABLE" if declined else (run.get("result") or {}).get("primary_action", run.get("status")),
                "action_match": declined, "escalate_match": True, "trigger_recall": 1.0, "missed_triggers": [], "extra_triggers": [],
                "flags_hit": 0, "flags_total": 0, "violations": [], "checks": [], "guardrail_notes": [],
                "run_at": run.get("ran_at"), "passed": declined}
    if run.get("status") != "ok":
        return {"case_id": case_id, "label": label["label"], "error": f"run status {run.get('status')}", "passed": False}

    r = run["result"]
    got, want = {t["id"] for t in r["triggers_fired"]}, set(label["triggers"])
    checks = judge(skill, run, label)
    flags = [c for c in checks if c["kind"] == "must_flag"]
    nots = [c for c in checks if c["kind"] == "must_not"]
    row = {
        "case_id": case_id,
        "label": label["label"],
        "expected_action": label["primary_action"],
        "got_action": r["primary_action"],
        "action_match": r["primary_action"] in [label["primary_action"], *label.get("also_acceptable", [])],
        "escalate_match": r["escalate"] == label["escalate"],
        "trigger_recall": 1.0 if not want else round(len(got & want) / len(want), 2),
        "missed_triggers": sorted(want - got),
        "extra_triggers": sorted(got - want),
        "flags_hit": sum(c["passed"] for c in flags),
        "flags_total": len(flags),
        "violations": [c["check"] for c in nots if not c["passed"]],
        "checks": checks,
        "guardrail_notes": run["guardrail_notes"],
        "run_at": run["ran_at"],
    }
    row["passed"] = row["action_match"] and row["escalate_match"] and row["trigger_recall"] == 1.0 and row["flags_hit"] == row["flags_total"] and not row["violations"]
    return row


def run_all(case_ids: list[str] | None = None, reuse: bool = False) -> dict:
    skill = engine.load_skill()
    labels = json.loads((skill.path / skill.manifest["eval_file"]).read_text())["cases"]
    ids = case_ids or list(labels)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda cid: score_case(skill, cid, labels[cid], reuse), ids))

    ok = [r for r in rows if "error" not in r]
    n = len(rows) or 1
    report = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "skill": f"{skill.manifest['id']}@{skill.manifest['version']}",
        "model": skill.manifest["model"],
        "summary": {
            "cases": len(rows),
            "passed": sum(r["passed"] for r in rows),
            "action_accuracy": round(sum(r["action_match"] for r in ok) / n, 2),
            "escalation_accuracy": round(sum(r["escalate_match"] for r in ok) / n, 2),
            "trigger_recall": round(sum(r["trigger_recall"] for r in ok) / n, 2),
            "must_flag_hit_rate": round(sum(r["flags_hit"] for r in ok) / max(1, sum(r["flags_total"] for r in ok)), 2),
            "must_not_violations": sum(len(r["violations"]) for r in ok),
        },
        "rows": rows,
    }
    out_dir = skill.path / "evals" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in (f"{stamp}.json", "latest.json"):
        (out_dir / name).write_text(json.dumps(report, indent=2))
    return report


def print_report(report: dict) -> None:
    print(f"\n{report['skill']} on {report['model']}  ({report['ran_at']})\n")
    print(f"{'case':<11}{'label':<13}{'expected':<22}{'got':<22}{'esc':<5}{'trig':<6}{'flags':<7}{'viol':<5}result")
    for r in report["rows"]:
        if "error" in r:
            print(f"{r['case_id']:<11}{r['label']:<13}ERROR: {r['error']}")
            continue
        print(f"{r['case_id']:<11}{r['label']:<13}{r['expected_action']:<22}{r['got_action']:<22}"
              f"{'ok' if r['escalate_match'] else 'X':<5}{r['trigger_recall']:<6}{r['flags_hit']}/{r['flags_total']:<5}"
              f"{len(r['violations']):<5}{'PASS' if r['passed'] else 'FAIL'}")
        for c in r["checks"]:
            if not c["passed"]:
                print(f"    - failed {c['kind']}: {c['check']}\n      judge: {c['reason']}")
        if r["missed_triggers"] or r["extra_triggers"]:
            print(f"    - triggers missed {r['missed_triggers']} extra {r['extra_triggers']}")
    s = report["summary"]
    print(f"\nPassed {s['passed']}/{s['cases']} | action {s['action_accuracy']:.0%} | escalation {s['escalation_accuracy']:.0%} | "
          f"trigger recall {s['trigger_recall']:.0%} | must-flag {s['must_flag_hit_rate']:.0%} | violations {s['must_not_violations']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--reuse", action="store_true", help="score saved runs instead of re-running the engine")
    args = ap.parse_args()
    report = run_all(args.cases or None, reuse=args.reuse)
    print_report(report)
    sys.exit(0 if report["summary"]["passed"] == report["summary"]["cases"] else 1)
