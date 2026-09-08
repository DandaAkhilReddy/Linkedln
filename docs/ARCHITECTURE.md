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
- **jobs.py** — `posts_store()`, `logo_loader(company)`, `generate(companies|group|None, hours)`, `drain()`, `growth_ask()`, `growth_poll()`, `email_batch(companies, hours)`, `test_card(company)`, `run(name)`; constant `SCHEDULE` (list of `(name, cron, fn)`).
- **companies.py** — no functions; the `COMPANIES` registry and `GROUP_A..E` / `GROUPS`.
- **storage.py** — `backend()`, `get_store(container)`; classes `FileStore`, `_Downloaded`, `_Entry`.

### Posting
- **linkedin_autopost.py** — config: `_cfg(key, default)`, `_set_companies()`; storage: `_load`, `_save`, `_li_state_blob`; caption helpers: `_job_url`, `_loc_str`, `_job_loc`, `_job_team`, `_top_pay`, `_caption(company, jobs, part, total, style)`; the two workhorses **`generate(store, logo_loader, companies, hours)`** and **`drain(store)`**; `_in_posting_window(now)`. Constants: `ORG_URNS` (17 verified LinkedIn org URNs), `HOOK_VARIANTS`, `CARDS_PER_COMPANY`, `JOBS_PER_CARD`, `SPACING_MIN`, `MAX_PER_DRAIN`, `DAILY_CAP`, `STALE_HOURS`.
- **card_builder.py** — `display_name(company)`, `_font(size, bold)`, `_logo_bytes(company, loader)`, `_trim_logo(img)` (auto-crops margins), `build_card(company, jobs, ..., logo_loader)` (logo-only 1200×627 PNG), `split_into(items, n)`. Dicts `THEME`, `DISPLAY`.
- **linkedin_client.py** — `_blob_secrets()`, `_token()`, `person_urn(token)`, `_register_image()`, `_upload_image()`, `_utf16_len(text)`, `_commentary(text, mention)` (builds the @mention annotation), **`post_with_image(text, png, title, token, urn, mention)`**, `post_text()`, `token_valid()`.

### Growth loop
- **growth_check.py** — `_secrets`, `_save_secrets`, `_creds`, `_send(subject, body)`, `_load_log`, `send_ask(store)`, `_decode_subj`, `_top_text`, `_extract_count(body)`, **`poll_replies(store)`** (IMAP UID cursor → number path or chat path), `_chat_reply(store, subj, text)` (LLM + guarded `ACTION:` application).
- **llm_chat.py** — `chat(endpoint, key, deployment, messages)` (classic Azure OpenAI or AI-Foundry `/openai/v1`).

### Hosts / ops
- **function_app.py** — 8 timer functions (`linkedin_generate_a..e`, `linkedin_drain`, `growth_ask`, `growth_poll`), 4 HTTP routes (`linkedin_run`, `growth_run`, `run_now`, `test_email`), helpers `_ncron`, `_text`.
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
 "variant": "salary_hook|question_hook|urgency_hook",
 "title": "AMD is hiring", "created": "<iso>", "post_after": "<iso>", "retries": 0}
```
`drain()` posts the first entry whose `post_after ≤ now`, at most `MAX_PER_DRAIN` per run, under `DAILY_CAP`, dropping entries older than `STALE_HOURS`.

**Per-company LinkedIn state** — `li_<company>_state.json`: `{"posted_ids": [...≤8000], "last_run": iso}` (dedup; *all* fetched ids are marked so reposts never flood).

**Per-company email state** — `<company>_state.json`: `{"last_run", "parked": [jobs], "sent_ids"}`.

**Secrets / config** — `li_secrets.json`: `access_token`, `person_urn`, `aoai_endpoint|key|deployment`, `linkedin_autopost_enabled`, `cards_per_company`, `jobs_per_card`. Read by `_cfg()`; the email chat's `ACTION:` lines write the last three.

**Post log** — `li_post_log.json`: `[{"ts", "company", "variant", "urn"}]` — the A/B dataset for the hook-style experiment and the source of the daily cap count.

**Growth log** — `growth_log.json`: `{"goal_per_day": 200, "entries": [{"date", "followers", "note"}]}`.

**Registry** — `COMPANIES[name] = {"pipeline": module, "state": blob, "prefix": str, "subject": str, "seed_first_run": bool}`; `GROUPS = {"a": [...], … "e": [...]}`.

**Schedule** — `jobs.SCHEDULE = [(name, "5-field UTC cron", callable), …]`; a test asserts each cron string also appears in `function_app.py`, so the two hosts can't drift.

## 6. Control flow, end to end

1. **08:00–08:50 UTC+4 (ET)** `generate(group)`: for each company → `get_jobs(24h)` → drop ids in `posted_ids` → cap to `cards_per_company × jobs_per_card` → `split_into` chunks → `fetch_detail` each job → ensure the company's logo card blob exists (built once) → `_caption()` with a random hook variant → round-robin merge across companies → assign `post_after` slots 10 min apart → merge-on-save into `li_queue.json`.
2. **Every 10 min** `drain()`: cap/prune checks → pick the first due entry → `post_with_image()` (register upload → PUT PNG → `ugcPosts` with the @mention annotation) → append to `li_post_log.json` → remove from queue (or bump `retries`).
3. **09:00 ET** `send_ask()` emails the check-in; **every 20 min** `poll_replies()` reads new IMAP UIDs: a bare number → log + analysis email; anything else → `_chat_reply()` → LLM answer (+ applied `ACTION`s) emailed back.
