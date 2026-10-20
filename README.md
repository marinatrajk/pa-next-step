# PA Next Step

An assistant for prior-authorization operators. When an insurer denies or pends a prescription's prior
authorization, it reads the case, the SOP, the insurer's policy and the team's know-how, then says what to do
next, who to send it to, why, and drafts the message. A person always makes the call.

> **Synthetic data only.** Every case, patient, fax and message in this project is made up. Don't load real
> patient information into it.

## Run it locally (about 5 minutes)

You need **Python 3.12 or newer** (`python3 --version`) and an **Anthropic API key**
([console.anthropic.com](https://console.anthropic.com)). If your Python is older, use
[uv](https://docs.astral.sh/uv/) instead of the two venv/pip steps: `uv venv --python 3.13 && uv pip install -r requirements.txt`.

```bash
git clone https://github.com/marinatrajk/pa-next-step.git
```

```bash
cd pa-next-step
```

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Open `.env` and put your key on the `ANTHROPIC_API_KEY=` line. Delete the other lines, or leave them for
later: they're only for hosting and Slack.

```bash
.venv/bin/streamlit run app.py
```

It opens at <http://localhost:8501>. Saved analyses are included, so most cases show a next step right away
without spending anything. A new analysis takes about 30 seconds and costs about 10¢.

### What to try

| Page | Try this |
| --- | --- |
| **Chat** | `/next-step 1077`, or ask in plain English: "what's next for the Humira case due tomorrow?" |
| **Case review** | Pick a case: the requirements, conflicts, sources and draft behind a recommendation. The **Evals** tab runs the labeled test cases. |
| **Data sources** | **Document intake**: drop in a file from `data/samples/` (an email, a scanned referral) and see it turned into structured fields and matched to a case. **Simulate new information** shows a case updating when a fax arrives. |
| **Knowledge base** | Team know-how saved from Slack, and search by meaning |
| **Decision log** | What operators did with each recommendation, and why they overrode it |
| **Connect Slack** | Run the `@kb` bot in your own Slack workspace |

## Slack (optional)

Either use the **Connect Slack** page in the app (one button creates the Slack app, then paste two tokens),
or run the bot on its own with the tokens in `.env`. See [`slack/README.md`](slack/README.md).

```bash
.venv/bin/python slack_bot.py
```

## Tests and evals

```bash
.venv/bin/python test_guardrails.py
```

Checks the code-enforced safety rules. No API calls.

```bash
.venv/bin/python gather.py --score
```

Rebuilds the case records from the source systems and scores how many documents and notes landed on the right
case. Only new, unmatched documents use the AI.

```bash
.venv/bin/python run_evals.py
```

Runs all 17 labeled cases and grades them. It makes a fresh analysis per case plus a grading call, so it costs a
few dollars. Use `--reuse` to grade the saved analyses instead (only out-of-date ones are re-run), or name
cases: `run_evals.py CASE-1077 CASE-1042`.

```bash
.venv/bin/python ingest.py
```

Reads any new files in `data/raw_inbox/` into the document inbox.

## Start over

Everything the app writes lives in `data/`. To put the demo data back the way it was:

```bash
git checkout -- data && git clean -fd data
```

## How it's laid out

| Path | What it is |
| --- | --- |
| `app.py` | Entry point and page list |
| `engine.py` | The analysis: scope check, sources, the model call, guardrails, saved runs |
| `gather.py` | Finds each case's documents and notes across systems (exact matches in code, the rest by AI) |
| `ingest.py` | Document intake: PDFs, scans, photos, email and EHR exports into one schema |
| `kb.py` | Team knowledge base (Chroma vector store) fed from Slack |
| `chat.py`, `case_review.py`, `sources.py`, `knowledge.py`, `decisions.py`, `slack_page.py` | The pages |
| `slack_bot.py`, `slack_connect.py` | The Slack bot, run on its own or inside the app |
| `skills/pa_next_step/` | The skill pack: SOP, insurer policies, document schema, labeled eval cases |
| `data/` | The simulated source systems (PA tracker, document inbox, case notes, Slack) and everything generated |

## Hosting

To give people a link, see [`DEPLOY.md`](DEPLOY.md): one Docker container on Render, with an access code and a
daily cap on AI calls.
