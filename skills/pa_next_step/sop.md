# SOP: Prior Authorization Exception Handling

**SOP ID:** OPS-PA-014 · **Version:** 3.2 · **Effective:** 2026-08-01 · **Owner:** Access Operations
**Applies to:** Access Operators handling PA cases in status `DENIED` or `PENDED` (more information requested).

> FICTIONAL DOCUMENT. Every procedure, payer and patient in this repo is synthetic.

---

## 1. Purpose

When a PA comes back denied or pended, the operator picks the single correct next action fast enough to stay inside the payer's response window. They escalate when the decision isn't theirs to make.

## 2. Source-of-truth hierarchy

When sources disagree, trust them in this order:

1. **Payer policy document** (current effective date, in `payers/`). This is the only authority on payer criteria.
2. **This SOP.** It's the authority on Forus process: what action, who acts, by when.
3. **Case record**: structured fields and documents received from the provider office.
4. **Operator case notes**: what a teammate observed or was told. This is useful, but secondhand.
5. **Team messages** (Slack, etc.): context only. **A message reporting a payer rule change is NOT actionable until it's confirmed against a payer bulletin or the policy doc.** (See §6, trigger E3.)

Clinical facts about the patient (drug trials, reactions, contraindications) count as established **only if they're documented in provider records**. An operator note saying "MA said patient reacted to X" is a lead to verify, not evidence.

## 3. Standard actions

Every case resolves to exactly one primary action:

| Code | Action | Use when |
|---|---|---|
| `RESUBMIT_WITH_DOCS` | Resubmit the PA with the missing documentation attached | Denial or pend is for missing docs **and** we already hold documents that satisfy the criteria |
| `REQUEST_RECORDS` | Request specific records from the provider office | The criteria are probably met but we don't hold the proof (chart notes, labs, scores, trial dates) |
| `STEP_THERAPY_EXCEPTION` | Submit a step-therapy exception with a letter of medical necessity (LMN) | The patient didn't complete the required step **and** there is a documented contraindication, intolerance, or failure |
| `FORMULARY_EXCEPTION` | Submit a formulary or non-preferred exception | The drug is non-formulary or non-preferred **and** the payer's exception criteria are documented as met |
| `PEER_TO_PEER` | Coordinate a peer-to-peer review between the prescriber and the payer medical director | **Second** denial for the same reason after a complete resubmission |
| `REVERIFY_BENEFITS` | Re-run the benefits investigation and restart with the correct payer | Denial for eligibility or coverage-not-active, or evidence the patient changed plans |
| `HOLD_FOR_ESCALATION` | Take no payer-facing action yet; escalate per §6 | Any escalation trigger fires and the right action depends on the answer |

## 4. Procedure

1. **Read the denial or pend reason code.** Map it with §5.
2. **Check the payer policy** for the drug and indication. List every criterion.
3. **For each criterion, find the evidence** in the case record or documents. Mark it *met*, *not met*, or *unknown*. Cite the document.
4. **Check the deadline.** Compute the hours left in the payer response window (see payer doc).
5. **Check the §6 escalation triggers.** If any fire, escalate, even if a payer-facing action also seems possible.
6. **Take or queue the primary action.** Log it in case notes with the criteria checklist.

## 5. Denial or pend reason → default action

| Reason code | Meaning | Default action |
|---|---|---|
| `ST-DOC` | Step therapy: documentation of prior trials missing | `RESUBMIT_WITH_DOCS` if we hold the docs, otherwise `REQUEST_RECORDS` |
| `ST-NOTMET` | Step therapy: required prior trials not completed | `STEP_THERAPY_EXCEPTION` if a contraindication or intolerance is documented, otherwise contact the provider about the required step |
| `CLIN-INFO` | Missing clinical information (scores, labs, severity) | `REQUEST_RECORDS`, or `RESUBMIT_WITH_DOCS` if we already hold it |
| `NF-PREF` | Non-preferred or non-formulary product | `FORMULARY_EXCEPTION` if criteria are documented, otherwise `REQUEST_RECORDS` |
| `ELIG` | Member not eligible or coverage inactive | `REVERIFY_BENEFITS` |
| Any code, **second denial** after a complete resubmission | | `PEER_TO_PEER` |

## 6. Escalation triggers

Escalate when **any** of these is true. Escalate to the listed owner, in the case-system escalation queue, with a one-paragraph summary.

| ID | Trigger | Escalate to |
|---|---|---|
| E1 | **Clinical judgment required.** Deciding whether a finding counts as a contraindication or intolerance, interpreting labs, or dosing outside the label | Clinical Intelligence (PharmD) |
| E2 | **Conflicting patient facts.** Sources disagree on what the patient has tried, reacted to, or been diagnosed with, and provider records don't settle it | Team Lead |
| E3 | **Unverified payer-rule change.** A teammate or message says the payer rules have changed, it conflicts with the current policy doc, and no bulletin confirms it | Team Lead, who loops in Payer Relations |
| E4 | **Deadline at risk.** Less than 48 hours left in the payer window **and** the primary action needs information we don't hold | Team Lead (deadline extension or expedited request) |
| E5 | **Second denial.** Peer-to-peer needed | Team Lead (FYI) and coordinate with the provider office |
| E6 | **Patient safety concern** of any kind | Clinical Intelligence, immediately |

**Never:**
- Submit to a payer based on an unverified rule change (E3).
- Treat an undocumented clinical claim as established.
- Let a deadline lapse silently. Escalate before the window closes.

## 7. Internal SLAs

- Denied or pended case: first action within **1 business day** of receipt.
- Records request to provider: follow up at **48 hours** if there's no response.
- Escalations: Team Lead responds within **4 business hours**; Clinical Intelligence within **1 business day**, or **2 hours** for E6.
