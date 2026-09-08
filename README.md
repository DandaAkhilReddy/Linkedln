# LinkedIn Jobs Autopilot

Scrapes new job openings from **17 tech companies** every morning and auto-posts
them to a personal LinkedIn profile through the **official LinkedIn API** — one
post every 10 minutes, around the clock, each with the company's logo, a
salary hook, the @company tag, and apply links. A daily email check-in tracks
follower growth, and you can steer the whole system by replying to that email
from your phone (an LLM reads it and applies safe config changes).

Companies: Microsoft, Apple, Google, Amazon, NVIDIA, Meta, OpenAI, Anthropic,
Netflix, xAI, Databricks, Stripe, Scale AI, Ramp, Cursor, AMD, IBM.

## How it works

```
 careers APIs ──> *_jobs_pipeline.py / board_pipelines.py   (jobs, salary, links)
                    │
   08:00–08:50 ET   ▼  jobs.generate()   builds up to 10 cards/company, round-robin,
                  queue (li_queue.json)  favorites first, new jobs only
                    │
   every 10 min     ▼  jobs.drain()      posts ONE card via LinkedIn ugcPosts API
                  LinkedIn               (logo image + hook caption + @mention)
                    │
   09:00 ET daily   ▼  growth_check      emails "what's your follower count?";
                  Gmail (IMAP/SMTP)      replies are logged, analysed, or answered
                                         by Azure OpenAI (can change settings)
```

- **Portable core**: `jobs.py` (entry points + `SCHEDULE`), `storage.py` (Azure Blob *or* local files, same API), `companies.py` (registry).
- **Host adapters**: `function_app.py` (Azure Functions, current production) and `worker.py` (Railway / Docker / any VM). Both run the same `SCHEDULE`.
- **Content**: `card_builder.py` (logo card), `linkedin_autopost.py` (queue, captions, A/B hook variants, daily cap 145 = LinkedIn's limit), `linkedin_client.py` (API).
- **Growth loop**: `growth_check.py` + `llm_chat.py`.

## Run it

**Azure Functions (current):** zip the repo root (`function_app.py`, `host.json`, modules, `requirements.txt`) → Kudu zipdeploy. App settings: `AzureWebJobsStorage`, `GMAIL_USERNAME`, `GMAIL_APP_PASSWORD`, `MAIL_TO`. Secrets/config live in blob `linkedin-posts/li_secrets.json`.

**Railway / Docker / anywhere:** see [docs/MIGRATION.md](docs/MIGRATION.md). Short version:

```bash
cp .env.example .env            # STORAGE_BACKEND=file, DATA_DIR, Gmail, tokens
python migrate_storage.py --from-azure "<conn>" --to-dir ./data   # bring the live data
python worker.py                # scheduler + /health on $PORT
```

**Manual controls** (any host): `python worker.py run drain`, `python worker.py generate a 48`, `python worker.py status`. On Azure the same actions are HTTP routes (`linkedin_run`, `growth_run`).

**Steer from your phone:** reply to the daily "LinkedIn Growth Check-in" email with a number (logged + analysed) or a sentence ("pause posting", "make it 5 per company", "how's the queue?") — answered within 20 minutes, changes applied.

## Adding a company

1. Pipeline: one line in `board_pipelines.py` if it's Greenhouse/Ashby (or a small module for a custom API).
2. Entry in `companies.py` (+ a generate group).
3. Logo PNG in `logos/` (official mark; transparent or white background).
4. Verified LinkedIn organization URN in `linkedin_autopost.ORG_URNS` — verify on the company's public LinkedIn page; a wrong ID tags the wrong company.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v      # 37 tests: parsers, dedup, schedule parity, storage, cron
```

## Notes

- Only the official LinkedIn API is used (`w_member_social`); no scraping or automation of linkedin.com.
- Company logos are the companies' own marks, used to identify the company being posted about.
- Volume is capped at LinkedIn's 150 posts/member/day; the drip is 1 post / 10 min.
