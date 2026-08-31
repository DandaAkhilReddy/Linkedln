"""
Azure Functions (Python v2 model) — big-tech jobs -> LinkedIn auto-poster.
10 companies. LinkedIn-only: daily generate timers build branded cards
from new jobs and a drip drain posts them via the official LinkedIn API.
Emails are DISABLED (no email timers); batch_run/run_now remain for
manual catch-up only. Per-company state lives in blob storage.

HTTP triggers:
  run_now?company=<name>&hours=N   manual/catch-up run
  test_email                                email delivery check
"""

import os
import ssl
import json
import logging
import datetime
import smtplib
import traceback
from datetime import timezone, timedelta
from email.mime.text import MIMEText

import azure.functions as func
from azure.storage.blob import BlobServiceClient

import ms_jobs_pipeline
import apple_jobs_pipeline
import google_jobs_pipeline
import amazon_jobs_pipeline
import nvidia_jobs_pipeline
import meta_jobs_pipeline
import openai_jobs_pipeline
import anthropic_jobs_pipeline
import netflix_jobs_pipeline
import xai_jobs_pipeline
import linkedin_autopost
import growth_check
import card_builder

app = func.FunctionApp()

BATCH_SIZE = int(os.getenv("MAX_JOBS_TOTAL", "50"))

COMPANIES = {
    "microsoft": {"pipeline": ms_jobs_pipeline, "state": "state.json",
                  "prefix": "post", "subject": "\U0001F680 Microsoft jobs LinkedIn posts"},
    "apple": {"pipeline": apple_jobs_pipeline, "state": "apple_state.json",
              "prefix": "apple_post", "subject": "\U0001F34F Apple jobs LinkedIn posts"},
    "google": {"pipeline": google_jobs_pipeline, "state": "google_state.json",
               "prefix": "google_post", "subject": "\U0001F50D Google jobs LinkedIn posts"},
    "amazon": {"pipeline": amazon_jobs_pipeline, "state": "amazon_state.json",
               "prefix": "amazon_post", "subject": "\U0001F4E6 Amazon jobs LinkedIn posts"},
    "nvidia": {"pipeline": nvidia_jobs_pipeline, "state": "nvidia_state.json",
               "prefix": "nvidia_post", "subject": "\U0001F49A NVIDIA jobs LinkedIn posts"},
    # Meta exposes no posting dates: first run seeds all current ids, then only new ids email
    "meta": {"pipeline": meta_jobs_pipeline, "state": "meta_state.json",
             "prefix": "meta_post", "subject": "Ⓜ️ Meta jobs LinkedIn posts",
             "seed_first_run": True},
    "openai": {"pipeline": openai_jobs_pipeline, "state": "openai_state.json",
               "prefix": "openai_post", "subject": "\U0001F916 OpenAI jobs LinkedIn posts"},
    # Greenhouse updated_at churns on edits: seed first run, then only new ids
    "anthropic": {"pipeline": anthropic_jobs_pipeline, "state": "anthropic_state.json",
                  "prefix": "anthropic_post", "subject": "✳️ Anthropic jobs LinkedIn posts",
                  "seed_first_run": True},
    "netflix": {"pipeline": netflix_jobs_pipeline, "state": "netflix_state.json",
                "prefix": "netflix_post", "subject": "\U0001F3AC Netflix jobs LinkedIn posts"},
    "xai": {"pipeline": xai_jobs_pipeline, "state": "xai_state.json",
            "prefix": "xai_post", "subject": "✖️ xAI jobs LinkedIn posts",
            "seed_first_run": True},
}

# Split across invocations so each stays under the 10-minute function timeout
GROUP_A = ["microsoft", "apple", "google"]
GROUP_B = ["amazon", "nvidia", "meta"]
GROUP_C = ["openai", "anthropic", "netflix", "xai"]


def _container():
    conn = os.environ["AzureWebJobsStorage"]
    c = BlobServiceClient.from_connection_string(conn).get_container_client("linkedin-posts")
    try:
        c.create_container()
    except Exception:
        pass
    return c


def _load_state(c, blob):
    try:
        return json.loads(c.download_blob(blob).readall())
    except Exception:
        return {"last_run": None, "parked": [], "sent_ids": []}


def _save_state(c, blob, state):
    state["sent_ids"] = state.get("sent_ids", [])[-5000:]
    c.upload_blob(blob, json.dumps(state), overwrite=True)


def _send_email(post, subject_prefix, label):
    user = os.environ.get("GMAIL_USERNAME")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO")
    if not (user and pwd and to):
        return "email not configured"
    msg = MIMEText(post, "plain", "utf-8")
    msg["Subject"] = f"{subject_prefix} {label} — {datetime.date.today():%B %d, %Y}"
    msg["From"] = f"Jobs Bot <{user}>"
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return f"emailed to {to}"


def batch_run(company, label, blob_suffix, lookback_hours=None):
    cfg = COMPANIES[company]
    pipeline, state_blob = cfg["pipeline"], cfg["state"]
    c = _container()
    state = _load_state(c, state_blob)
    now = datetime.datetime.now(timezone.utc)

    if lookback_hours:
        cutoff = now - timedelta(hours=lookback_hours)
    elif state.get("last_run"):
        cutoff = max(datetime.datetime.fromisoformat(state["last_run"]),
                     now - timedelta(hours=24))
    else:
        cutoff = now - timedelta(hours=24)

    fresh = pipeline.get_jobs(cutoff)
    sent_ids = set(state.get("sent_ids", []))
    parked = state.get("parked", [])
    seen = {j.get("id") for j in parked}

    queue = parked + [j for j in fresh
                      if j.get("id") not in sent_ids and j.get("id") not in seen]
    queue = pipeline.sort_software_first(queue)

    if not queue:
        state["last_run"] = now.isoformat()
        _save_state(c, state_blob, state)
        return [f"{company}: no new jobs since {cutoff:%H:%M UTC}"]

    seeding = cfg.get("seed_first_run") and not state.get("last_run")
    batch = queue[:BATCH_SIZE]
    rest = [] if seeding else queue[BATCH_SIZE:]
    post = pipeline.render_posts(batch)

    notes = []
    blob_name = f"{cfg['prefix']}_{datetime.date.today().isoformat()}_{blob_suffix}.txt"
    c.upload_blob(blob_name, post, overwrite=True)
    notes.append(f"{company}: blob {blob_name} ({len(batch)} jobs)")
    try:
        notes.append(_send_email(post, cfg["subject"], f"({label} — {len(batch)} jobs)"))
    except Exception as e:
        notes.append(f"email failed: {e}")

    state["last_run"] = now.isoformat()
    state["parked"] = rest
    state["sent_ids"] = list(sent_ids) + [j.get("id") for j in (queue if seeding else batch)]
    _save_state(c, state_blob, state)
    if rest:
        notes.append(f"{len(rest)} parked for next send")
    return notes


def _run_group(companies, label, suffix):
    for company in companies:
        try:
            logging.info("%s %s: %s", company, label, "; ".join(batch_run(company, label, suffix)))
        except Exception:
            logging.error("%s %s crashed:\n%s", company, label, traceback.format_exc())


# ---- email timers removed (user request: LinkedIn posts only) ----
# run_now below still allows a manual email batch if ever needed.


@app.route(route="run_now", auth_level=func.AuthLevel.FUNCTION)
def run_now(req: func.HttpRequest) -> func.HttpResponse:
    try:
        hours = int(req.params.get("hours", "0")) or None
        company = req.params.get("company", "")
        targets = [company] if company in COMPANIES else list(COMPANIES)
        notes = []
        for t in targets:
            notes += batch_run(t, "manual batch", "manual", lookback_hours=hours)
        return func.HttpResponse("NOTES: " + "; ".join(notes), status_code=200,
                                 mimetype="text/plain; charset=utf-8")
    except Exception:
        return func.HttpResponse("CRASH:\n" + traceback.format_exc(), status_code=500,
                                 mimetype="text/plain; charset=utf-8")


@app.route(route="test_email", auth_level=func.AuthLevel.FUNCTION)
def test_email(req: func.HttpRequest) -> func.HttpResponse:
    try:
        user = os.environ.get("GMAIL_USERNAME")
        to = os.environ.get("MAIL_TO")
        info = [f"GMAIL_USERNAME set: {bool(user)}",
                f"GMAIL_APP_PASSWORD set: {bool(os.environ.get('GMAIL_APP_PASSWORD'))}",
                f"MAIL_TO: {to}"]
        result = _send_email("Test email — delivery works! ✅", "\U0001F9EA Jobs bot", "(delivery test)")
        return func.HttpResponse("\n".join(info) + "\nRESULT: " + result, status_code=200,
                                 mimetype="text/plain; charset=utf-8")
    except Exception:
        return func.HttpResponse("EMAIL ERROR:\n" + traceback.format_exc(), status_code=500,
                                 mimetype="text/plain; charset=utf-8")


# ---------------- LinkedIn auto-poster wiring ----------------
linkedin_autopost._set_companies(COMPANIES)


def _logo_loader(company):
    """Pull a company logo PNG from the 'linkedin-logos' blob container."""
    try:
        conn = os.environ["AzureWebJobsStorage"]
        lc = BlobServiceClient.from_connection_string(conn).get_container_client("linkedin-logos")
        return lc.download_blob(f"{company}.png").readall()
    except Exception:
        return None


# 12:00 UTC = 8:00 AM ET — build + queue the day's LinkedIn cards
@app.timer_trigger(schedule="0 0 12 * * *", arg_name="timer", run_on_startup=False)
def linkedin_generate_a(timer: func.TimerRequest) -> None:
    try:
        logging.info("LI gen A: %s", "; ".join(linkedin_autopost.generate(_container(), _logo_loader, GROUP_A)))
    except Exception:
        logging.error("LI gen A crashed:\n%s", traceback.format_exc())


@app.timer_trigger(schedule="0 20 12 * * *", arg_name="timer", run_on_startup=False)
def linkedin_generate_b(timer: func.TimerRequest) -> None:
    try:
        logging.info("LI gen B: %s", "; ".join(linkedin_autopost.generate(_container(), _logo_loader, GROUP_B)))
    except Exception:
        logging.error("LI gen B crashed:\n%s", traceback.format_exc())


@app.timer_trigger(schedule="0 30 12 * * *", arg_name="timer", run_on_startup=False)
def linkedin_generate_c(timer: func.TimerRequest) -> None:
    try:
        logging.info("LI gen C: %s", "; ".join(linkedin_autopost.generate(_container(), _logo_loader, GROUP_C)))
    except Exception:
        logging.error("LI gen C crashed:\n%s", traceback.format_exc())


# Mon/Thu 13:00 UTC (9 AM ET) — email the follower-count check-in question
@app.timer_trigger(schedule="0 0 13 * * 1,4", arg_name="timer", run_on_startup=False)
def growth_ask(timer: func.TimerRequest) -> None:
    try:
        logging.info("growth ask: %s", "; ".join(growth_check.send_ask(_container())))
    except Exception:
        logging.error("growth ask crashed:\n%s", traceback.format_exc())


# every 2h at :30 — read email replies, log count, send analysis back
@app.timer_trigger(schedule="0 30 */2 * * *", arg_name="timer", run_on_startup=False)
def growth_poll(timer: func.TimerRequest) -> None:
    try:
        logging.info("growth poll: %s", "; ".join(growth_check.poll_replies(_container())))
    except Exception:
        logging.error("growth poll crashed:\n%s", traceback.format_exc())


@app.route(route="growth_run", auth_level=func.AuthLevel.FUNCTION)
def growth_run(req: func.HttpRequest) -> func.HttpResponse:
    """Manual: ?action=ask | poll"""
    try:
        act = req.params.get("action", "poll")
        out = (growth_check.send_ask(_container()) if act == "ask"
               else growth_check.poll_replies(_container()))
        return func.HttpResponse("NOTES: " + "; ".join(out), status_code=200,
                                 mimetype="text/plain; charset=utf-8")
    except Exception:
        return func.HttpResponse("CRASH:\n" + traceback.format_exc(), status_code=500,
                                 mimetype="text/plain; charset=utf-8")


# every 15 min — drip-post any due cards
@app.timer_trigger(schedule="0 10/15 * * * *", arg_name="timer", run_on_startup=False)
def linkedin_drain(timer: func.TimerRequest) -> None:
    try:
        logging.info("LI drain: %s", "; ".join(linkedin_autopost.drain(_container())))
    except Exception:
        logging.error("LI drain crashed:\n%s", traceback.format_exc())


@app.route(route="linkedin_run", auth_level=func.AuthLevel.FUNCTION)
def linkedin_run(req: func.HttpRequest) -> func.HttpResponse:
    """Manual: ?action=generate | drain | testcard&company=microsoft"""
    try:
        action = req.params.get("action", "drain")
        c = _container()
        if action == "generate":
            g = req.params.get("group", "")
            one = req.params.get("company", "")
            hours = int(req.params.get("hours", "24"))
            subset = ([one] if one in COMPANIES else
                      GROUP_A if g == "a" else GROUP_B if g == "b" else
                      GROUP_C if g == "c" else None)
            out = linkedin_autopost.generate(c, _logo_loader, subset, hours)
        elif action == "testcard":
            company = req.params.get("company", "microsoft")
            jobs = COMPANIES[company]["pipeline"].get_jobs()[:6]
            png = card_builder.build_card(company, jobs, logo_loader=_logo_loader)
            import linkedin_client
            share = linkedin_client.post_with_image(
                linkedin_autopost._caption(company, jobs, 1, 1), png,
                title=f"{company.title()} is hiring")
            out = [f"test card posted: {share}"]
        else:
            out = linkedin_autopost.drain(c)
        return func.HttpResponse("NOTES: " + "; ".join(out), status_code=200,
                                 mimetype="text/plain; charset=utf-8")
    except Exception:
        return func.HttpResponse("CRASH:\n" + traceback.format_exc(), status_code=500,
                                 mimetype="text/plain; charset=utf-8")
