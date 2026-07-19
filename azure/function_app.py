"""
Azure Functions (Python v2 model) — 3 sends per day (ET):
- batch_7am:  7:00 AM ET
- batch_2pm:  2:00 PM ET
- batch_7pm:  7:00 PM ET
Each run emails only jobs posted since the previous run (no duplicates),
up to BATCH_SIZE per email; extras are parked and drained next run.
State (last run time, parked jobs, sent ids) lives in blob storage.
- run_now: HTTP trigger (function key) to run a batch on demand / debug
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

import ms_jobs_pipeline as pipeline

app = func.FunctionApp()

BATCH_SIZE = int(os.getenv("MAX_JOBS_TOTAL", "50"))
STATE_BLOB = "state.json"


def _container():
    conn = os.environ["AzureWebJobsStorage"]
    c = BlobServiceClient.from_connection_string(conn).get_container_client("linkedin-posts")
    try:
        c.create_container()
    except Exception:
        pass
    return c


def _load_state(c):
    try:
        return json.loads(c.download_blob(STATE_BLOB).readall())
    except Exception:
        return {"last_run": None, "parked": [], "sent_ids": []}


def _save_state(c, state):
    state["sent_ids"] = state.get("sent_ids", [])[-500:]
    c.upload_blob(STATE_BLOB, json.dumps(state), overwrite=True)


def _send_email(post, label):
    user = os.environ.get("GMAIL_USERNAME")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO")
    if not (user and pwd and to):
        return "email not configured"
    msg = MIMEText(post, "plain", "utf-8")
    msg["Subject"] = f"🚀 Microsoft jobs LinkedIn posts {label} — {datetime.date.today():%B %d, %Y}"
    msg["From"] = f"MS Jobs Bot <{user}>"
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return f"emailed to {to}"


def batch_run(label, blob_suffix, lookback_hours=None):
    """Fetch jobs since last run, drain parked ones first, send up to BATCH_SIZE.
    lookback_hours overrides the window (catch-up runs); normally cutoff = last run."""
    c = _container()
    state = _load_state(c)
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
        _save_state(c, state)
        return [f"no new jobs since {cutoff:%H:%M UTC}"]

    batch, rest = queue[:BATCH_SIZE], queue[BATCH_SIZE:]
    post = pipeline.render_posts(batch)

    notes = []
    blob_name = f"post_{datetime.date.today().isoformat()}_{blob_suffix}.txt"
    c.upload_blob(blob_name, post, overwrite=True)
    notes.append(f"blob saved: {blob_name} ({len(batch)} jobs)")
    try:
        notes.append(_send_email(post, f"({label} — {len(batch)} jobs)"))
    except Exception as e:
        notes.append(f"email failed: {e}")

    state["last_run"] = now.isoformat()
    state["parked"] = rest
    state["sent_ids"] = list(sent_ids) + [j.get("id") for j in batch]
    _save_state(c, state)
    if rest:
        notes.append(f"{len(rest)} jobs parked for the next send")
    return notes


# 11:00 UTC = 7:00 AM ET (summer)
@app.timer_trigger(schedule="0 0 11 * * *", arg_name="timer", run_on_startup=False)
def batch_7am(timer: func.TimerRequest) -> None:
    try:
        logging.info("7 AM batch: %s", "; ".join(batch_run("7 AM batch", "0700")))
    except Exception:
        logging.error("7 AM batch crashed:\n%s", traceback.format_exc())


# 16:00 UTC = 12:00 PM ET (summer)
@app.timer_trigger(schedule="0 0 16 * * *", arg_name="timer", run_on_startup=False)
def batch_12pm(timer: func.TimerRequest) -> None:
    try:
        logging.info("12 PM batch: %s", "; ".join(batch_run("12 PM batch", "1200")))
    except Exception:
        logging.error("12 PM batch crashed:\n%s", traceback.format_exc())


# 21:00 UTC = 5:00 PM ET (summer)
@app.timer_trigger(schedule="0 0 21 * * *", arg_name="timer", run_on_startup=False)
def batch_5pm(timer: func.TimerRequest) -> None:
    try:
        logging.info("5 PM batch: %s", "; ".join(batch_run("5 PM batch", "1700")))
    except Exception:
        logging.error("5 PM batch crashed:\n%s", traceback.format_exc())


# 01:00 UTC = 9:00 PM ET (summer)
@app.timer_trigger(schedule="0 0 1 * * *", arg_name="timer", run_on_startup=False)
def batch_9pm(timer: func.TimerRequest) -> None:
    try:
        logging.info("9 PM batch: %s", "; ".join(batch_run("9 PM batch", "2100")))
    except Exception:
        logging.error("9 PM batch crashed:\n%s", traceback.format_exc())


@app.route(route="run_now", auth_level=func.AuthLevel.FUNCTION)
def run_now(req: func.HttpRequest) -> func.HttpResponse:
    try:
        hours = int(req.params.get("hours", "0")) or None
        notes = batch_run("manual batch", "manual", lookback_hours=hours)
        return func.HttpResponse("NOTES: " + "; ".join(notes), status_code=200,
                                 mimetype="text/plain; charset=utf-8")
    except Exception:
        return func.HttpResponse("CRASH:\n" + traceback.format_exc(), status_code=500,
                                 mimetype="text/plain; charset=utf-8")


@app.route(route="test_email", auth_level=func.AuthLevel.FUNCTION)
def test_email(req: func.HttpRequest) -> func.HttpResponse:
    """Send a tiny test email and report exactly what happened."""
    try:
        user = os.environ.get("GMAIL_USERNAME")
        to = os.environ.get("MAIL_TO")
        info = [f"GMAIL_USERNAME set: {bool(user)}",
                f"GMAIL_APP_PASSWORD set: {bool(os.environ.get('GMAIL_APP_PASSWORD'))}",
                f"MAIL_TO: {to}"]
        result = _send_email("Test email from your MS Jobs bot — delivery works! ✅", "(delivery test)")
        return func.HttpResponse("\n".join(info) + "\nRESULT: " + result, status_code=200,
                                 mimetype="text/plain; charset=utf-8")
    except Exception:
        return func.HttpResponse("EMAIL ERROR:\n" + traceback.format_exc(), status_code=500,
                                 mimetype="text/plain; charset=utf-8")
