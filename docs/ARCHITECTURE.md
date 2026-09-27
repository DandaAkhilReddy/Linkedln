# Architecture: modules, functions, classes, data structures

Python 3.11, ~3,900 lines, 24 modules, **208 functions, 5 classes**. The codebase
is deliberately *functional*: modules expose plain functions and pass data
around as dicts/lists (JSON-shaped), so every piece of state can be written
straight to a blob/file and inspected by hand. Classes appear only where an
object needs to carry state or satisfy an external interface.

## 1. Layers

```
host adapters      function_app.py (Azure)      worker.py (anywhere)
                          └────────────┬────────────┘
core                            jobs.py  ── SCHEDULE, generate/drain/growth_*
                     ┌───────────────┼──────────────────┬──────────────┐
posting          linkedin_autopost  card_builder   linkedin_client   growth_check + llm_chat
                     │
sources          companies.py ──> 10 <name>_jobs_pipeline.py + board_pipelines.py (7 boards)
                     │
infra            storage.py (Azure Blob | FileStore)   emailer.py (optional)   migrate_storage.py
```

## 2. The pipeline interface (duck-typed "protocol")

Every job source — ten per-company modules and every object produced by
`board_pipelines` — exposes the same five functions, so the core never
special-cases a company:

| Function | Contract |
|---|---|
| `get_jobs(cutoff=None) -> list[job]` | Jobs posted since `cutoff` (UTC datetime), software roles first |
| `sort_software_first(jobs) -> list[job]` | Stable sort: software eng → other eng → rest (board pipelines also put US roles first) |
| `fetch_detail(id) -> detail` | Salary / snippet / level / url for one job (HTTP or cached) |
| `build_post(jobs, date_str, part, total_parts) -> str` | The long email-style text for a chunk of jobs |
| `render_posts(jobs, date_str=None) -> str` | All chunks joined with a copy-here divider |

`board_pipelines._make()` is the factory: give it a `fetch_recent(cutoff)`
function and a `detail_lookup(id)` function for a board, and it returns a
`SimpleNamespace` implementing the interface. `greenhouse()`, `ashby()`,
`amd()`, `ibm()` are the four board adapters; `databricks`, `stripe`,
`scaleai`, `ramp`, `cursor`, `amd`, `ibm` are the instances.

## 3. Modules and their functions

### Core
- **jobs.py** — `posts_store()`, `logo_loader(company)`, `generate(companies|group|None, hours)`, `ensure_queue()`, **`drain()`** (the guarantee chain: planner → Q&A → card → refill+retry → filler), `edu_generate(track)`, `edu_generate_all()`, `drain_catchup()` (offset timer, acts only if the primary slot was missed), `filler_refill()`, `heal()`, `health()`, `health_report()`, `growth_ask()`, `growth_poll()`, `daily_poll()`, `daily_roundup()`, `strategy_summary()`, `email_batch(companies, hours)`, `test_card(company)`, `run(name)`; constant `SCHEDULE` (list of `(name, cron, fn)`).
- **companies.py** — no functions; the `COMPANIES` registry and `GROUP_A..E` / `GROUPS`.
- **storage.py** — `backend()`, `get_store(container)`; classes `FileStore`, `_Downloaded`, `_Entry`.

### Posting
- **linkedin_autopost.py** — config: `_cfg(key, default)`, `_set_companies()`; storage: `_load`, `_save`, `_li_state_blob`; caption helpers: `_job_url`, `_loc_str`, `_job_loc`, `_job_team`, `_top_pay`, `_caption(company, jobs, part, total, style)`; the two workhorses **`generate(store, logo_loader, companies, hours)`** and **`drain(store)`**; `_in_posting_window(now)`. Constants: `ORG_URNS` (17 verified LinkedIn org URNs), `HOOK_VARIANTS`, `CARDS_PER_COMPANY`, `JOBS_PER_CARD`, `SPACING_MIN`, `MAX_PER_DRAIN`, `DAILY_CAP`, `STALE_HOURS`.
- **card_builder.py** — `display_name(company)`, `_font(size, bold)`, `_logo_bytes(company, loader)`, `_trim_logo(img)` (auto-crops margins), `build_card(company, jobs, ..., logo_loader)` (logo-only 1200×627 PNG), `split_into(items, n)`. Dicts `THEME`, `DISPLAY`.
- **linkedin_client.py** — `_blob_secrets()`, `_token()`, `person_urn(token)`, `_register_image()`, `_upload_image()`, `_utf16_len(text)`, `_commentary(text, mention)` (builds the @mention annotation), **`post_with_image(text, png, title, token, urn, mention)`**, `post_text()`, `token_valid()`.

### Growth loop
- **growth_check.py** — `_secrets`, `_save_secrets`, `_creds`, `_send(subject, body)`, `_load_log`, `send_ask(store)`, `_decode_subj`, `_top_text`, `_extract_count(body)`, **`log_count(store, count, note)`** (one entry per day, credits the strategy arms, returns the analysis text), **`poll_replies(store)`** (IMAP UID cursor → number path or chat path), `_chat_reply(store, subj, text)` (LLM + guarded `ACTION:` application; keys: `linkedin_autopost_enabled`, `cards_per_company`, `jobs_per_card`, `log_followers`, `strategy_arm`).
- **growth_posts.py** — the two native high-reach formats, via the versioned Posts API (`/rest/posts`, `LinkedIn-Version`): little-text-format helpers `ltf`, `ltf_mention`, `ltf_tag`; `post_poll(question, options, commentary, token, author)`, `post_document(pdf, title, commentary, token, author)` (initializeUpload → PUT → post); job facts `record_facts(store, company, jobs, job_loc, job_url)` (side effect of generate → `li_jobfacts.json`), `top_paid(store, days, n, per_company)`, `company_tops(store)`; the theme catalog `THEMES` (title/sub/hook/filter/pay floor/per-company cap; kinds `jobs` and `ranking`), `ROTATION` (weekday × slot), `select(store, theme, company, exclude, n)`, `rank_companies(store, theme)`, `spotlight_company(store, day_index)`, `plan_deck(store, slot)` (rotation entry → fallbacks with enough data, never the same theme twice a day); rendering `build_deck(theme, items, logo_loader, date_str, company)` (Pillow multi-page PDF, 1080×1350, bundled Poppins fonts; `_cover`, `_job_page`, `_ranking_page`, `_last_page`), `deck_commentary(theme, items, company)`; the daily jobs **`daily_poll(store)`** and **`daily_roundup(store, logo_loader, slot)`** (log to `li_post_log.json` with variant `poll` / `carousel` + `theme`, respect the daily cap, max 2 carousels/day; featured urls kept in `li_carousel_state.json`).
- **strategy.py** — the strategy bandit: `ARMS` (`volume`: 1/10 min 24/7; `prime`: 1/30 min 7am–9pm ET), `arm_for(store, day)` (3-day blocks; explore each arm, then exploit the best followers/day, re-testing the runner-up every 4th block; `li_secrets.strategy_arm` overrides), `policy(store, now)` (settings for right now + logs today's arm), `should_post(store, now, last_post_ts)` (window + spacing gate used by `drain`), `expected(policy)` (posts/day + max gap for the health thresholds), `credit(store, prev_date, prev_count, date, count)` (splits a reported gain over the days in between), `summary(store)` (scoreboard text for the emails).
- **edu_content.py** — the educational tracks (`TRACKS`, `TRACK_NAMES`, `TRACK_TAGS`, `GUIDE` prompts): `targets(store)` (per-track daily counts from `li_secrets`: `edu_enabled`, `edu_dsa`…), `validate(item, track)` (normalises an LLM/seed record, id = sha1 of the question), `seed_items(track)` (`edu_seed.json`), `generate_one(store, track, sec, state, paper)` (one Azure OpenAI call → JSON → validated item; papers get a real paper from `_paper_candidates`: HF daily papers then arXiv), `ensure_pool(store, track, n, budget_s)` (time-boxed morning fill, persists item by item, seeds when the LLM is down), `ensure_all`, `reset_pool`, `caption(item)` (hook → question → "comment first" → answer under the fold → code → complexity → takeaway → CTA → tags), **`post_one(store, track)`** (pool → on-the-fly → seed; image post with the rendered card, text-only fallback; logs variant = track), `plan_text` / `posted_text` (for the daily email).
- **edu_cards.py** — the card design system: `render(track, question, visual, number, difficulty)` → 1200×627 PNG; primitives `draw_array`, `draw_code` (syntax-tinted mono panel), `draw_boxes` (flow with arrows, snake layout), `draw_graph` (circle layout, directed edges — agent loops), `draw_tree` (level-order), `draw_metric`, `draw_compare`, `draw_table`; `fit_text`/`wrap`; per-track palette `TRACKS`. A broken spec degrades to a text-only card, never an exception.
- **content_plan.py** — `pick(store, now)` → `"job"` or a track: track T is due when `posted_today(T) < ceil(target(T) × slot/144)`; most-behind track wins; `summary(store)`.
- **filler.py** — the posting guarantee's last resort: `parse_feed(xml)` (RSS 2.0 + Atom), `fetch_feeds()` (19 company/AI blogs in `FEEDS`), `fetch_hn()` (Hacker News official API, score ≥ 150, tech titles), `fetch_papers()` (Hugging Face daily papers), `chart_candidates(store)` (from `li_jobfacts.json`), `bar_chart_png(...)`, `news_card_png(kind, title, source, when)` (original 1200×627 images), `caption_for(item)`, `chart_caption(chart)`, `_llm_summary(item, secrets)` (own-words summary via Azure OpenAI, `_fallback_summary` otherwise), **`refill(store, quick, logo_loader)`** (tops `li_filler_queue.json` up to `TARGET`, interleaves sources, dedups with `li_filler_state.json`), **`post_one(store)`** (pops → `post_with_image` with @mention → text-only fallback → logs variant `news`/`hn`/`paper`/`chart`).
- **llm_chat.py** — `chat(endpoint, key, deployment, messages)` (classic Azure OpenAI or AI-Foundry `/openai/v1`).

### Hosts / ops
- **function_app.py** — 20 timer functions (`linkedin_generate_a..e`, `linkedin_drain`, `linkedin_drain_catchup`, `filler_refill`, `edu_dsa/sd/mlsd/ai/papers`, `edu_topup`, `growth_ask`, `growth_poll`, `health_report`, `daily_poll`, `daily_roundup`, `daily_roundup_pm`), 5 HTTP routes (`health`, `linkedin_run`, `growth_run?action=ask|poll|poll_post|carousel|carousel_pm|strategy`, `run_now`, `test_email`), helpers `_ncron`, `_text`.
- **worker.py** — `_field_matches`, `cron_matches(cron5, dt)`, `_serve_health`, `scheduler_loop()`, `status()`; class `_Health` (HTTP handler). CLI: `run <job>`, `generate <group> [hours]`, `status`.
- **emailer.py** — `_load_state`, `_save_state`, `send_email`, `batch_run(store, company, label, suffix, lookback_hours)`.
- **migrate_storage.py** — `azure_store`, `copy(src, dst)`, `main()`.

### Sources (per company, all implement §2)
`ms_jobs_pipeline` (Eightfold pcsx), `apple_jobs_pipeline` (CSRF session), `google_jobs_pipeline` (embedded JSON parser), `amazon_jobs_pipeline` (search.json), `nvidia_jobs_pipeline` (Workday CXS), `meta_jobs_pipeline` (GraphQL via curl_cffi), `openai_jobs_pipeline` (Ashby), `anthropic_jobs_pipeline` / `xai_jobs_pipeline` (Greenhouse), `netflix_jobs_pipeline` (Eightfold apply/v2), plus the seven in `board_pipelines`.

## 4. Classes (all five)

| Class | Where | Why it's a class |
|---|---|---|
| `FileStore` | storage.py | Stateful (root dir) and must mirror the Azure `ContainerClient` interface: `download_blob`, `upload_blob`, `list_blobs`, `delete_blob`, `create_container` |
| `_Downloaded` | storage.py | Mimics Azure's download object so callers can `.readall()` |
| `_Entry` | storage.py | Mimics Azure's blob-listing item (`.name`) |
| `_Health` | worker.py | `BaseHTTPRequestHandler` subclass — required by `http.server` |
| `FakeContainer` | tests | In-memory store double for tests |

Everything else is functions + dicts on purpose: state must be serializable, and pipelines are interchangeable by *shape*, not by inheritance.

## 5. Data structures

**Job** (produced by every pipeline; JSON-safe dict)
```json
{"id": "92001", "title": "Software Engineer", "name": "Software Engineer",
 "locations": ["Austin, Texas"], "team": "Engineering",
 "salary": "$80,500 - $115,000", "url": "https://…",
 "_detail": {"salary": "…", "snippet": "…", "level": "…", "emp_type": "…", "url": "…"}}
```
`_detail` is attached by `generate()` after `fetch_detail()`; `build_post()` reuses it to avoid a second fetch.

**Queue entry** — `li_queue.json` is a list of these:
```json
{"company": "amd", "card_blob": "li_cards/amd_logo.png", "caption": "…",
 "variant": "salary_hook|question_hook|grab_hook",
 "title": "AMD is hiring", "created": "<iso>", "post_after": "<iso>", "retries": 0}
```
`drain()` posts the first entry whose `post_after ≤ now`, at most `MAX_PER_DRAIN` per run, under `DAILY_CAP`, dropping entries older than `STALE_HOURS`.

**Per-company LinkedIn state** — `li_<company>_state.json`: `{"posted_ids": [...≤8000], "last_run": iso}` (dedup; *all* fetched ids are marked so reposts never flood).

**Per-company email state** — `<company>_state.json`: `{"last_run", "parked": [jobs], "sent_ids"}`.

**Secrets / config** — `li_secrets.json`: `access_token`, `person_urn`, `aoai_endpoint|key|deployment`, `linkedin_autopost_enabled`, `cards_per_company`, `jobs_per_card`. Read by `_cfg()`; the email chat's `ACTION:` lines write the last three.

**Post log** — `li_post_log.json`: `[{"ts", "company", "variant", "urn"}]` — variants are the three job hooks, the five educational tracks (`dsa`, `sd`, `mlsd`, `ai`, `papers`), `news`/`hn`/`paper`/`chart` fillers, plus `poll` and `carousel`; the A/B dataset, the daily-cap count, and the spacing clock for `drain`.

**Growth log** — `growth_log.json`: `{"goal_per_day": 200, "entries": [{"date", "followers", "note"}]}` (one entry per day).

**Job facts** — `li_jobfacts.json`: `[{"ts", "company", "title", "loc", "salary", "top", "url"}]` for every role with a pay range seen by `generate()` (14-day window). Feeds the carousel (`top_paid`) and the poll numbers (`company_tops`).

**Educational item** — one record per question (`li_edu_pool.json` = `{track: [item…]}`; used ones summarised in `li_edu_state.json` `{"used": [{id, track, q, ts, urn}], "papers_seen": [urls], "counters": {track: n}}`):
```json
{"id": "dsa-3f9a…", "track": "dsa", "hook": "…", "question": "…", "options": [], "difficulty": "medium",
 "answer": "…", "code": "def …", "complexity": "Time O(n) · Space O(1)", "takeaway": "…",
 "visual": {"kind": "array", "values": [2,7,11,15], "highlight": [0,3], "pointers": {"0": "L"}},
 "tags": ["TwoPointers"], "url": "", "source": "llm|seed|paper", "number": 37, "card_blob": "li_edu_cards/dsa-3f9a….png"}
```

**Filler queue / state** — `li_filler_queue.json`: `[{"kind": "news|hn|paper|chart", "title", "url", "source", "company", "summary", "caption", "card_blob", "created"}]`; `li_filler_state.json`: `{"seen": [urls], "charts": {chart_id: date}}`. Cards live under `li_filler_cards/`.

**Strategy state** — `li_strategy.json`: `{"blocks": {"<block#>": arm}, "days": {"<date>": arm}, "stats": {arm: {"days", "gained"}}, "credits": [...]}`. `blocks` freezes each 3-day block's decision; `stats` is what the bandit ranks on.

**Registry** — `COMPANIES[name] = {"pipeline": module, "state": blob, "prefix": str, "subject": str, "seed_first_run": bool}`; `GROUPS = {"a": [...], … "e": [...]}`.

**Schedule** — `jobs.SCHEDULE = [(name, "5-field UTC cron", callable), …]`; a test asserts each cron string also appears in `function_app.py`, so the two hosts can't drift.

## 6. Control flow, end to end

1. **08:00–08:50 UTC+4 (ET)** `generate(group)`: for each company → `get_jobs(24h)` → drop ids in `posted_ids` → cap to `cards_per_company × jobs_per_card` → `split_into` chunks → `fetch_detail` each job → ensure the company's logo card blob exists (built once) → `_caption()` with a random hook variant → round-robin merge across companies → assign `post_after` slots 10 min apart → merge-on-save into `li_queue.json`.
2. **Every 10 min** `drain()`: cap/prune checks → pick the first due entry → `post_with_image()` (register upload → PUT PNG → `ugcPosts` with the @mention annotation) → append to `li_post_log.json` → remove from queue (or bump `retries`).
3. **09:00 ET** `send_ask()` emails the check-in; **every 20 min** `poll_replies()` reads new IMAP UIDs: a bare number → log + analysis email; anything else → `_chat_reply()` → LLM answer (+ applied `ACTION`s) emailed back.
