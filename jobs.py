"""
Platform-agnostic entry points + the schedule.

Every host adapter (Azure Functions `function_app.py`, the portable
`worker.py`, a cron job, a CLI) calls these same functions. Nothing in here
knows what it's running on: storage comes from `storage.get_store()`,
config from env / the secrets blob.

SCHEDULE is the single source of truth for *when* things run (UTC, 5-field
cron). Azure timers and the worker loop are both generated from it.
"""

import re
import json
import logging
import datetime
import traceback
from datetime import timezone, timedelta

import card_builder
import linkedin_autopost
import growth_check
import growth_posts
import healthcheck
import strategy
import filler
import edu_content
import content_plan
import emailer
from storage import get_store
from companies import COMPANIES, GROUPS, GROUP_A, GROUP_B, GROUP_C, GROUP_D, GROUP_E

log = logging.getLogger("jobs")
linkedin_autopost._set_companies(COMPANIES)

POSTS = "linkedin-posts"     # queue, state, secrets, logs
LOGOS = "linkedin-logos"     # <company>.png


def posts_store():
    return get_store(POSTS)


def logo_loader(company):
    """Company logo bytes from the logo store; falls back to ./logos/ (repo)."""
    try:
        return get_store(LOGOS).download_blob(f"{company}.png").readall()
    except Exception:
        try:
            with open(f"logos/{company}.png", "rb") as f:
                return f.read()
        except Exception:
            return None


# ---------- LinkedIn auto-poster ----------

def generate(companies=None, hours=24):
    """Build + queue cards for `companies` (list, group letter, or None=all)."""
    if isinstance(companies, str):
        companies = GROUPS.get(companies.lower()) or [companies]
    return linkedin_autopost.generate(posts_store(), logo_loader, companies, hours)


MIN_QUEUE = 3
REFILL_WINDOWS_H = (24, 72, 168)     # widen until something is found


def ensure_queue(min_items=MIN_QUEUE):
    """FALLBACK: if the queue is (nearly) empty, refill from all companies with a
    widening lookback so there is always something to post. Dedup by posted
    ids means widening never re-posts a job."""
    store = posts_store()
    notes = []

    def depth():
        try:
            return len(json.loads(store.download_blob("li_queue.json").readall()))
        except Exception:
            return 0

    if depth() >= min_items:
        return ["queue ok"]
    # group by group (each call saves its own cards) so a serverless timeout
    # can never lose work; stop as soon as there is something to post
    for hours in REFILL_WINDOWS_H:
        for g, members in GROUPS.items():
            notes.append(f"queue={depth()} < {min_items}: refill group {g} @ {hours}h")
            notes += linkedin_autopost.generate(store, logo_loader, members, hours)
            if depth() >= min_items:
                return notes
    return notes


def _posted(notes):
    return any(re.match(r"^[1-9]\d* posted", n) for n in notes)


def _held(notes):
    """The gate said no (cap / spacing / window / disabled / token) — not a content problem."""
    return any(n.startswith(("holding queue", "daily cap", "autopost disabled", "token failure"))
               for n in notes)


def drain():
    """THE GUARANTEE — one post per slot, every 10 minutes:
    0. the slot planner says whether this slot is a job card or one of the
       educational tracks (10/day each, spread evenly);
    1. educational slot -> post the next Q&A (pool → on-the-fly → seed bank);
       if that fails, fall through to jobs so the slot is still filled;
    2. next job card from the queue;
    3. queue empty -> refill from all companies with a widening lookback, retry;
    4. still nothing (or the job post failed) -> an original news/chart post
       with an image from `filler`, so the slot is never skipped."""
    store = posts_store()
    ok, why = linkedin_autopost.gate(store)
    if not ok:
        return [why]
    out = []
    try:
        track = content_plan.pick(store)
    except Exception as e:
        track, out = "job", [f"planner error ({str(e)[:60]}); job slot"]
    if track != "job":
        out += edu_content.post_one(store, track)
        if _posted(out):
            healthcheck.record(store, "drain", True, f"edu {track}: " + out[0][:200])
            return out
    out += linkedin_autopost.drain(store)
    if _posted(out) or _held(out):
        return out
    if any(n.startswith("queue empty") for n in out):
        out += ensure_queue()
        out += linkedin_autopost.drain(store)
        if _posted(out):
            return out
    out += filler.post_one(store)
    healthcheck.record(store, "drain", _posted(out), out[-1][:200] if out else "no notes")
    return out


def edu_generate(track):
    """Morning timers: fill one track's pool for the day (time-boxed)."""
    return edu_content.ensure_pool(posts_store(), track)


def edu_generate_all():
    return edu_content.ensure_all(posts_store())


def drain_catchup():
    """Offset timer (:00, :10, ...). Only acts if the primary slot (:05, :15, ...)
    was missed — a host restart during a deploy, a timer hiccup — so a lost
    slot is recovered within 5 minutes instead of waiting for the next one."""
    store = posts_store()
    plog = linkedin_autopost._load(store, "li_post_log.json", [])
    last = linkedin_autopost._last_post_ts(plog)
    now = datetime.datetime.now(timezone.utc)
    if last and now - last < timedelta(minutes=15):
        return [f"catch-up not needed (last card {int((now - last).total_seconds() // 60)} min ago)"]
    return ["catch-up: primary slot missed"] + drain()


def filler_refill():
    """Keep the news/chart backlog topped up (daily + every 6h)."""
    return filler.refill(posts_store(), logo_loader=logo_loader)


def heal():
    """Manual/automatic kick: refill if needed, then post one. Safe to call anytime."""
    return ensure_queue() + drain()


def health():
    return healthcheck.assess(posts_store())


def health_report():
    return healthcheck.daily_report(posts_store())


# ---------- growth loop (email check-in + chat + strategy arms) ----------

def growth_ask():
    return growth_check.send_ask(posts_store())


def growth_poll():
    return growth_check.poll_replies(posts_store())


def daily_poll():
    """One LinkedIn poll a day (if the active strategy arm has polls on)."""
    store = posts_store()
    if not strategy.policy(store)["poll"]:
        return ["poll off in this strategy arm"]
    return growth_posts.daily_poll(store)


def daily_roundup(slot="noon"):
    """PDF carousel for the slot; theme from growth_posts.ROTATION (weekday x slot)."""
    store = posts_store()
    if not strategy.policy(store)["carousel"]:
        return ["carousel off in this strategy arm"]
    return growth_posts.daily_roundup(store, logo_loader, slot)


def daily_roundup_pm():
    return daily_roundup("evening")


def strategy_summary():
    return strategy.summary(posts_store())


# ---------- optional email batches (manual only) ----------

def email_batch(companies=None, hours=None, label="manual batch"):
    if isinstance(companies, str):
        companies = GROUPS.get(companies.lower()) or [companies]
    store = posts_store()
    notes = []
    for c in companies or list(COMPANIES):
        try:
            notes += emailer.batch_run(store, c, label, "manual", lookback_hours=hours)
        except Exception:
            notes.append(f"{c} crashed: {traceback.format_exc(limit=1)}")
    return notes


def test_card(company="microsoft"):
    """Publish one card for `company` right now (smoke test)."""
    import linkedin_client
    jobs = COMPANIES[company]["pipeline"].get_jobs()[:6]
    png = card_builder.build_card(company, jobs, logo_loader=logo_loader)
    return linkedin_client.post_with_image(
        linkedin_autopost._caption(company, jobs, 1, 1), png,
        title=f"{card_builder.display_name(company)} is hiring")


# ---------- schedule (UTC) ----------
# name, cron, callable. Kept identical across Azure timers and worker.py.
SCHEDULE = [
    ("generate_a", "0 12 * * *",      lambda: generate(GROUP_A)),   # 8:00 AM ET favorites
    ("generate_b", "20 12 * * *",     lambda: generate(GROUP_B)),
    ("generate_c", "30 12 * * *",     lambda: generate(GROUP_C)),
    ("generate_d", "40 12 * * *",     lambda: generate(GROUP_D)),
    ("generate_e", "50 12 * * *",     lambda: generate(GROUP_E)),
    ("drain",      "5-55/10 * * * *", drain),                       # one post / 10 min, 24/7 (guaranteed)
    ("drain_catchup", "0-50/10 * * * *", drain_catchup),            # recovers a missed slot within 5 min
    ("filler_refill", "40 11,17,23,5 * * *", filler_refill),        # news/chart backlog, 4x a day
    ("edu_dsa",    "2 9 * * *",        lambda: edu_generate("dsa")),     # today's Q&A pools, 5-6 AM ET
    ("edu_sd",     "12 9 * * *",       lambda: edu_generate("sd")),
    ("edu_mlsd",   "22 9 * * *",       lambda: edu_generate("mlsd")),
    ("edu_ai",     "32 9 * * *",       lambda: edu_generate("ai")),
    ("edu_papers", "42 9 * * *",       lambda: edu_generate("papers")),
    ("edu_topup",  "2 21 * * *",       edu_generate_all),                # evening top-up if anything ran short
    ("growth_ask", "0 13 * * *",      growth_ask),                  # 9 AM ET check-in email
    ("growth_poll","*/20 * * * *",    growth_poll),                 # read replies / chat
    ("health_report", "30 13 * * *",  health_report),               # 9:30 AM ET watchdog email
    ("daily_poll", "32 12 * * *",     daily_poll),                  # 8:32 AM ET LinkedIn poll
    ("daily_roundup", "12 16 * * *",  daily_roundup),               # 12:12 PM ET PDF carousel (noon theme)
    ("daily_roundup_pm", "12 21 * * *", daily_roundup_pm),          # 5:12 PM ET PDF carousel (evening theme)
]


def run(name):
    """Run one scheduled job by name; never raises (logs instead)."""
    for n, _, fn in SCHEDULE:
        if n == name:
            try:
                out = fn()
                log.info("%s: %s", name, "; ".join(out) if isinstance(out, list) else out)
                return out
            except Exception:
                log.error("%s crashed:\n%s", name, traceback.format_exc())
                return None
    raise KeyError(name)
