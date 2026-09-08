# Moving hosts (Azure Functions → Railway / Docker / anywhere)

The business logic never touches a platform API. Two things are host-specific
and both are already abstracted:

| Concern | Azure today | Anywhere else |
|---|---|---|
| Storage (queue, state, secrets, logs, logos) | Azure Blob (`AzureWebJobsStorage`) | a directory (`STORAGE_BACKEND=file`, `DATA_DIR=/data`) |
| Scheduling | timer triggers in `function_app.py` | `worker.py` loop (same cron table from `jobs.SCHEDULE`) |

## Railway in 6 steps

1. **Export the live data from Azure** (queue, per-company state, `li_secrets.json`, post log, 17 logos):
   ```bash
   python migrate_storage.py --from-azure "<AzureWebJobsStorage connection string>" --to-dir ./data
   ```
2. **Create a Railway service from this repo** — it detects the `Dockerfile` (or use `railway.json`). Add a **Volume** mounted at `/data`.
3. **Set variables** (see `.env.example`): `STORAGE_BACKEND=file`, `DATA_DIR=/data`, `GMAIL_USERNAME`, `GMAIL_APP_PASSWORD`, `MAIL_TO`. LinkedIn token and the Azure OpenAI key can stay inside `li_secrets.json` (they are read from the store), or be set as `LINKEDIN_ACCESS_TOKEN` / `AOAI_*` env vars.
4. **Seed the volume**: upload the `./data` folder to the volume (`railway run` / a one-off `railway shell` + `scp`, or a temporary upload script). The layout is `/data/linkedin-posts/…` and `/data/linkedin-logos/<company>.png`.
5. **Deploy** — the start command is `python worker.py`. Check logs for `worker up; 8 scheduled jobs`. `python worker.py status` (via `railway run`) prints queue length and today's post count.
6. **Turn Azure off** — stop the Function App (`func-linkedin-jobs-26418`) *before* the Railway worker's first drain so both don't post the same card. Once Railway posts its first card, delete the Azure resource group `automated-email`.

Rollback: `migrate_storage.py --from-dir ./data --to-azure "<conn>"` and restart the Function App.

## Any other host

Anything that can run a Python process (Render, Fly.io, a VPS, a Raspberry Pi):

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in
export $(grep -v '^#' .env | xargs)
python worker.py       # scheduler + health endpoint on $PORT
```

Or a plain cron-only host (no long-running process): schedule
`python worker.py run <job>` for each row of `jobs.SCHEDULE`.

## What lives where

```
DATA_DIR/linkedin-posts/
  li_queue.json          cards waiting to post (caption, variant, card blob, post_after)
  li_post_log.json       every published post (ts, company, hook variant, URN)
  li_<company>_state.json posted job ids per company (dedup)
  li_secrets.json        LinkedIn token, Azure OpenAI endpoint/key, config overrides
  li_cards/<co>_logo.png the reusable logo card per company
  growth_log.json        follower counts over time
  chat_history.json      email-chat memory
  li_mail_state.json     IMAP cursor
DATA_DIR/linkedin-logos/<company>.png   source logos (also in repo: logos/)
```
