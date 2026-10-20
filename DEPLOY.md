# Hosting the demo (about 15 minutes)

One Docker container runs the web app and the Slack bot together. Anyone with the link and the access code can
use it, and connect their own Slack workspace from the **Connect Slack** page.

> **Synthetic data only.** Every case, fax and message here is made up. This setup is not HIPAA-compliant:
> real patient information would need a host, an Anthropic agreement and a Slack plan that are each covered by a
> signed BAA. The app says so on every page, and the upload box asks for synthetic documents only.

## 1. Protect the API key first

Create a separate Anthropic API key for the demo in a workspace with a monthly spend limit
(console.anthropic.com → Workspaces → Limits). The app also stops itself after `MAX_MODEL_CALLS_PER_DAY` AI calls
(default 200, roughly 10¢ per full analysis). Saved analyses keep working after the limit.

## 2. Put the project on GitHub

A private repo is fine. `.env` and `data/logs/` are ignored, so no keys or test decisions are uploaded.

## 3. Deploy on Render

1. render.com → **New → Blueprint** → pick the repo. It reads `render.yaml`.
2. Fill in the values it asks for:
   - `ANTHROPIC_API_KEY`: the demo key from step 1
   - `APP_ACCESS_CODE`: the code you'll give reviewers
   - `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`: optional, to keep your own workspace connected. Leave them blank and
     people connect theirs on the Connect Slack page.
3. **Apply.** The first build takes about 5 minutes. You get a URL like `https://pa-next-step.onrender.com`.

The blueprint uses the Starter plan (about $7/month) with a 1 GB disk. It stays on, so the Slack bot stays
connected, and what reviewers do (runs, decisions, knowledge-base entries) survives restarts. To start fresh,
delete the disk's contents in Render's shell (`rm -rf /var/data/*`) and restart: the original demo data is
copied back.

Other hosts (Railway, Fly.io, a VM) work the same way: build the `Dockerfile`, set the same environment
variables, and expose port 8080 (or set `PORT`).

## 4. Send reviewers

- The URL and the access code
- "Connect Slack" in the sidebar: one button creates the Slack app from the manifest, then they paste two tokens.
  Tokens pasted there stay in the server's memory only and are never saved.

## Run the same container locally

```bash
docker build -t pa-next-step .
docker run --rm -p 8530:8080 --env-file .env -e APP_ACCESS_CODE=change-me pa-next-step
```

Then open <http://localhost:8530>. Settings are listed in `.env.example`.
