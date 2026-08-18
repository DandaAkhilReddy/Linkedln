"""
Azure Functions (Python v2 model) — Microsoft + Apple + Google job emails.
2 sends per day per company (ET): 7 AM and 2 PM.
Each send contains only jobs posted since that company's previous send
(no duplicates); extras are parked and drained next run. Per-company
state lives in blob storage.

HTTP triggers:
  run_now?company=microsoft|apple|google&hours=N   manual/catch-up run
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

app = func.FunctionApp()

BATCH_SIZE = int(os.getenv("MAX_JOBS_TOTAL", "50"))

COMPANIES = {
    "microsoft": {"pipeline": ms_jobs_pipeline, "state": "state.json",
                  "prefix": "post", "subject": "\U0001F680 Microsoft jobs LinkedIn posts"},
    "apple": {"pipeline": apple_jobs_pipeline, "state": "apple_state.json",
              "prefix": "apple_post", "subject": "\U0001F34F Apple jobs LinkedIn posts"},
    "google": {"pipeline": google_jobs_pipeline, "state": "google_state.json",
               "prefix": "google_post", "subject": "\U0001F50D Google jobs LinkedIn posts"},
}


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
    state["sent_ids"] = state.get("sent_ids", [])[-500:]
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

    batch, rest = queue[:BATCH_SIZE], queue[BATCH_SIZE:]
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
    state["sent_ids"] = list(sent_ids) + [j.get("id") for j in batch]
    _save_state(c, state_blob, state)
    if rest:
        notes.append(f"{len(rest)} parked for next send")
    return notes


def _run_both(label, suffix):
    for company in COMPANIES:
        try:
            logging.info("%s %s: %s", company, label, "; ".join(batch_run(company, label, suffix)))
        except Exception:
            logging.error("%s %s crashed:\n%s", company, label, traceback.format_exc())


# 11:00 UTC = 7:00 AM ET (summer)
@app.timer_trigger(schedule="0 0 11 * * *", arg_name="timer", run_on_startup=False)
def batch_7am(timer: func.TimerRequest) -> None:
    _run_both("7 AM batch", "0700")


# 18:00 UTC = 2:00 PM ET (summer)
@app.timer_trigger(schedule="0 0 18 * * *", arg_name="timer", run_on_startup=False)
def batch_2pm(timer: func.TimerRequest) -> None:
    _run_both("2 PM batch", "1400")


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
