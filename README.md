# LinkedIn Jobs Autopilot

Scrapes new job openings from **17 tech companies** every morning and auto-posts
them to a personal LinkedIn profile through the **official LinkedIn API** — one
post every 10 minutes, around the clock, each with the company's logo, a
salary hook, the @company tag, and apply links. Fifty of the daily slots are educational Q&A
posts (DSA, system design, ML system design, AI engineering, papers) with
designed question cards. One daily email shows what went out and what's planned;
reply to it from your phone to change anything (an LLM reads it and applies
safe config changes).

Companies: Microsoft, Apple, Google, Amazon, NVIDIA, Meta, OpenAI, Anthropic,
Netflix, xAI, Databricks, Stripe, Scale AI, Ramp, Cursor, AMD, IBM.

## Growth engine (goal: +200 followers/day)

Every post carries a **follow CTA** and a verified **@company tag**. On top of the
job cards, native high-reach formats go out daily through the versioned Posts
API, none with an outbound link: a **poll** at 8:32 AM ET (company-vs-company
with real pay numbers, alternating with career questions) and **two PDF
carousels** (12:12 PM and 5:12 PM ET) built from the pay ranges the generator
records (`li_jobfacts.json`). Carousel themes rotate by weekday
(`growth_posts.ROTATION`): 10 highest-paying jobs · remote $200K+ · AI & ML ·
company spotlight · the $400K+ Staff/Principal club · Bay Area / Seattle / NYC ·
early-career · engineering leadership · "who pays the most for a Software
Engineer" leaderboard · weekly recap. A theme that lacks data falls back to the
next one; roles already featured aren't repeated.

Cadence is fixed at **1 post / 10 min, 24/7** (`strategy.py` arm `volume`; the
bandit only auto-picks this one). The follower count you reply with each morning
is credited to the active arm and shown as a scoreboard in every email. `prime`
(1 post / 30 min, 7am–9pm ET) exists only as an explicit email override
(`switch to prime`; `auto` or `back to volume` returns to the guarantee).

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
- **Growth loop**: `growth_check.py` (email check-in + chat) + `growth_posts.py` (poll + carousel) + `strategy.py` (arms + bandit) + `llm_chat.py`.

## Run it

**Azure Functions (current):** **push to `main` deploys automatically** (`.github/workflows/deploy.yml`: tests → `Azure/functions-action` with the publish-profile secret → `/health` smoke test). Manual alternative: zip the repo root → Kudu zipdeploy. App settings: `AzureWebJobsStorage`, `GMAIL_USERNAME`, `GMAIL_APP_PASSWORD`, `MAIL_TO`. Secrets/config live in blob `linkedin-posts/li_secrets.json`.

**Railway / Docker / anywhere:** see [docs/MIGRATION.md](docs/MIGRATION.md); module/function/data-structure map in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Short version:

```bash
cp .env.example .env            # STORAGE_BACKEND=file, DATA_DIR, Gmail, tokens
python migrate_storage.py --from-azure "<conn>" --to-dir ./data   # bring the live data
python worker.py                # scheduler + /health on $PORT
```

**Manual controls** (any host): `python worker.py run drain`, `python worker.py generate a 48`, `python worker.py status`. On Azure the same actions are HTTP routes (`linkedin_run`, `growth_run`).

**Ops from your phone (no laptop, no Azure login):** GitHub → Actions → **Ops** → Run workflow → pick `health`, `strategy`, `heal`, `edu_plan`, `edu_generate`, `edu_post_dsa`, … — the result prints in the run log.

**Steer from your phone:** reply to the daily "LinkedIn Growth Check-in" email with a number (logged, credited to the active strategy, analysed) or a sentence ("pause posting", "make it 5 per company", "switch to prime", "how's the queue?") — answered within 20 minutes, changes applied.

## What a day looks like (144 slots, one every 10 minutes)

| | per day | what |
|---|---|---|
| **DSA** | 10 | a problem with a concrete example → approach, complexity, Python |
| **System design** | 10 | a scenario with numbers → components, the trade-off, back-of-envelope |
| **ML system design** | 10 | feature stores, retrieval/ranking, evals, drift, imbalance… |
| **AI engineering** | 10 | agent loops & graphs, tool calling, RAG, evals, MCP, caching… |
| **Papers** | 10 | a real recent paper (HF daily papers / arXiv) → what changed, use cases |
| **Jobs** | 94 | new roles at 17 companies with pay + @tag (the rest of the slots) |
| + poll, 2 carousels | 3 | on top, outside the 10-minute clock |

Every educational post is a **question card** (designed figure: array with
pointers, code panel, flow boxes, agent graph, tree, metric, comparison,
table — see `edu_cards.py`) with the **answer under LinkedIn's "…more" fold**
and a "comment your answer before you expand" line. Questions are generated
the night before by Azure OpenAI (`edu_content.py`, one call per item, real
papers for the papers track), rendered and queued in `li_edu_pool.json`; if
the pool is empty at post time they're generated on the fly, and if the LLM is
down a hand-written seed bank (`edu_seed.json`) keeps the slot filled. The
slot planner (`content_plan.py`) is quota-based: each track gets exactly its
daily count spread evenly, jobs fill everything else, and a missed slot never
shifts the plan. Change the mix or the style by replying to the daily email
("more system design, fewer papers", "make DSA harder", "regenerate today").

## The guarantee: 144 slots a day, one every 10 minutes

`jobs.drain()` runs at :05, :15, … and always ends with a post:

0. the slot planner decides: job card or one of the five Q&A tracks;
   a Q&A slot posts from the pool → generated on the fly → seed bank, and
   falls through to jobs if all of that fails;
1. the next **job card** from the queue;
2. queue empty → **refill** from all 17 companies with a widening lookback (24h → 72h → 7 days), retry;
3. still nothing (or the job post failed) → an original **news card** (`filler.py`: the companies' own engineering/AI blogs, Hacker News via its official API, Hugging Face daily papers — headline + a summary in our own words + link, on an image we render) or a **pay chart** drawn from our own data (top of range by company, roles with pay by company, median SWE pay, pay by role type).

A **catch-up timer** at :00, :10, … posts only if the primary slot was missed (deploy restart, timer hiccup), so a lost slot is recovered within 5 minutes. The news/chart backlog is refilled four times a day (`filler_refill`) and on demand. Polls and carousels are extra posts on top of the 144 and don't touch the 10-minute clock. Daily cap 148 (LinkedIn allows 150).

## Fallbacks & watchdog ("posting is mandatory")

- **Queue never runs dry** — every drain that finds an empty queue refills it group-by-group with a widening lookback (24h → 72h → 7 days). Dedup means nothing is ever posted twice.
- **Image path fails → text-only post** (same caption) instead of skipping the slot.
- **One company crashes → the rest still generate** (per-company isolation).
- **Token dies → immediate alert email** (rate-limited), since that needs a human.
- **Heartbeats** (`li_health.json`) from every generate/drain.
- **Daily health email, 9:30 AM ET** — posts in last 24h, largest gap between posts, queue depth; subject starts with 🚨 ALERT when the 10-minute contract was broken.
- **External watchdog** — `.github/workflows/watchdog.yml` probes `/health` twice a day *from GitHub*; a 503 fails the run and GitHub emails you, so even a dead Azure host gets noticed.
- Manual kick from anywhere: `linkedin_run?action=heal` (Azure) or `python worker.py run drain` / `generate` (worker).

## Adding a company

1. Pipeline: one line in `board_pipelines.py` if it's Greenhouse/Ashby (or a small module for a custom API).
2. Entry in `companies.py` (+ a generate group).
3. Logo PNG in `logos/` (official mark; transparent or white background).
4. Verified LinkedIn organization URN in `linkedin_autopost.ORG_URNS` — verify on the company's public LinkedIn page; a wrong ID tags the wrong company.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v      # 61 tests: parsers, dedup, schedule parity, storage, cron, strategy, LTF
```

## Notes

- Only the official LinkedIn API is used (`w_member_social`); no scraping or automation of linkedin.com.
- Company logos are the companies' own marks, used to identify the company being posted about.
- Volume is capped at LinkedIn's 150 posts/member/day; the drip is 1 post / 10 min.
