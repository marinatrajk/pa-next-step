# Slack bot setup (about 5 minutes)

The bot runs on your laptop in Socket Mode: it connects out to Slack, so there is no server or public URL to set up.

> Use synthetic cases only. This prototype's data is fictional; real patient information belongs only in a
> HIPAA-eligible Slack workspace with a signed BAA.

## 1. Create the app from the manifest

1. Go to <https://api.slack.com/apps> → **Create New App** → **From a manifest**.
2. Pick your workspace.
3. Paste the contents of `slack/manifest.yml` (YAML tab) → **Next** → **Create**.

This sets the bot's handle to `@kb` and adds `/next-step` and `/kb-search`.

## 2. Get the two tokens

1. **App-level token:** Basic Information → *App-Level Tokens* → **Generate Token and Scopes** →
   name it `socket`, add the scope `connections:write` → **Generate**. Copy the token (starts with `xapp-`).
2. **Bot token:** Install App → **Install to Workspace** → **Allow**. Copy the *Bot User OAuth Token* (starts with `xoxb-`).

## 3. Add them to `.env`

Open `.env` in the project folder (the same file as your Anthropic key) and add two lines:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

## 4. Run the bot

From the project folder:

```bash
.venv/bin/python slack_bot.py
```

Leave it running. It prints `Slack bot running.` when connected.

## 5. Try it

Create a test channel, then type `/invite @kb` in it.

| Type this | What happens |
| --- | --- |
| Start a thread, then reply `@kb add` | The bot saves the whole thread to the team knowledge base and says what it saved |
| `@kb add: Bluepeak wants dates and the reaction on biosimilar records` | Saves a single tip with your note |
| `@kb what's next for the Humira case due tomorrow?` | Next step, who to send it to, why, a draft, and Mark as sent / Override buttons |
| `@kb why is it escalated?` (in the same thread) | Answers about the case that thread is about |
| Attach a fax, PDF, photo or email and mention `@kb` | Reads it, pulls out the fields, matches it to its case, and says what changed |
| `/next-step 1077` | The same next step, visible only to you |
| `/kb-search biosimilar intolerance` | Knowledge-base search by meaning |

Entries show up on the web app's **Knowledge base** page, and Mark as sent / Override land in the **Decision log**.

Already created the app before document uploads were added? Add the `files:read` scope under
**OAuth & Permissions → Bot Token Scopes**, then **Reinstall to Workspace**.
