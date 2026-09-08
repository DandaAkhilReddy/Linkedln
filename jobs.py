"""
Platform-agnostic entry points + the schedule.

Every host adapter (Azure Functions `function_app.py`, the portable
`worker.py`, a cron job, a CLI) calls these same functions. Nothing in here
knows what it's running on: storage comes from `storage.get_store()`,
config from env / the secrets blob.

SCHEDULE is the single source of truth for *when* things run (UTC, 5-field
cron). Azure timers and the worker loop are both generated from it.
"""

import logging
import traceback

import card_builder
import linkedin_autopost
import growth_check
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


def drain():
    """Post the next due card (rate-limited inside)."""
    return linkedin_autopost.drain(posts_store())


# ---------- growth loop (email check-in + chat) ----------

def growth_ask():
    return growth_check.send_ask(posts_store())


def growth_poll():
    return growth_check.poll_replies(posts_store())


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
    ("drain",      "5-55/10 * * * *", drain),                       # one post / 10 min, 24/7
    ("growth_ask", "0 13 * * *",      growth_ask),                  # 9 AM ET check-in email
    ("growth_poll","*/20 * * * *",    growth_poll),                 # read replies / chat
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
