"""
Azure Functions (Python v2 model).
- morning_batch:   7:00 AM ET  -> first 50 new jobs (software engineers first)
- afternoon_batch: 10:00 AM PT -> next 50, only if more than 50 were found
- run_now: HTTP trigger (function key) to run batch 1 on demand / debug
Saves post(s) to Blob Storage and emails via Gmail SMTP.
"""

import os
import ssl
import json
import logging
import datetime
import smtplib
import traceback
from email.mime.text import MIMEText

import azure.functions as func
from azure.storage.blob import BlobServiceClient

import ms_jobs_pipeline as pipeline

app = func.FunctionApp()

BATCH_SIZE = int(os.getenv("MAX_JOBS_TOTAL", "50"))


def _container():
    conn = os.environ["AzureWebJobsStorage"]
    c = BlobServiceClient.from_connection_string(conn).get_container_client("linkedin-posts")
    try:
        c.create_container()
    except Exception:
        pass
    return c


def _send_email(post, batch_label):
    user = os.environ.get("GMAIL_USERNAME")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO")
    if not (user and pwd and to):
        return "email not configured"
    msg = MIMEText(post, "plain", "utf-8")
    msg["Subject"] = f"🚀 Microsoft jobs LinkedIn posts {batch_label} — {datetime.date.today():%B %d, %Y}"
    msg["From"] = f"MS Jobs Bot <{user}>"
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return f"emailed to {to}"


def _deliver(jobs, batch_label, blob_suffix):
    """Render jobs -> blob + email. Returns notes."""
    notes = []
    post = pipeline.render_posts(jobs)
    c = _container()
    blob_name = f"post_{datetime.date.today().isoformat()}_{blob_suffix}.txt"
    c.upload_blob(blob_name, post, overwrite=True)
    notes.append(f"blob saved: {blob_name} ({len(jobs)} jobs)")
    try:
        notes.append(_send_email(post, batch_label))
    except Exception as e:
        notes.append(f"email failed: {e}")
    return notes


def morning():
    """Fetch everything new, send first BATCH_SIZE, park the rest for 10 AM PT."""
    jobs = pipeline.get_jobs()
    if not jobs:
        return ["no new jobs in window"]
    first, rest = jobs[:BATCH_SIZE], jobs[BATCH_SIZE:]
    notes = _deliver(first, f"(batch 1 — {len(first)} jobs)", "batch1")
    c = _container()
    overflow_blob = f"overflow_{datetime.date.today().isoformat()}.json"
    if rest:
        c.upload_blob(overflow_blob, json.dumps(rest), overwrite=True)
        notes.append(f"{len(rest)} jobs parked for the 10 AM PT batch")
    return notes


def afternoon():
    """Send the parked overflow jobs (up to BATCH_SIZE), if any."""
    c = _container()
    overflow_blob = f"overflow_{datetime.date.today().isoformat()}.json"
    try:
        rest = json.loads(c.download_blob(overflow_blob).readall())
    except Exception:
        return ["no overflow batch today"]
    if not rest:
        return ["no overflow batch today"]
    batch, remaining = rest[:BATCH_SIZE], rest[BATCH_SIZE:]
    notes = _deliver(batch, f"(batch 2 — {len(batch)} jobs)", "batch2")
    c.upload_blob(overflow_blob, json.dumps(remaining), overwrite=True)
    if remaining:
        notes.append(f"{len(remaining)} jobs still unposted (beyond 100/day)")
    return notes


# 11:00 UTC = 7:00 AM ET (summer)
@app.timer_trigger(schedule="0 0 11 * * *", arg_name="timer", run_on_startup=False)
def morning_batch(timer: func.TimerRequest) -> None:
    try:
        logging.info("Morning batch: %s", "; ".join(morning()))
    except Exception:
        logging.error("Morning batch crashed:\n%s", traceback.format_exc())


# 17:00 UTC = 10:00 AM PT (summer)
@app.timer_trigger(schedule="0 0 17 * * *", arg_name="timer", run_on_startup=False)
def afternoon_batch(timer: func.TimerRequest) -> None:
    try:
        logging.info("Afternoon batch: %s", "; ".join(afternoon()))
    except Exception:
        logging.error("Afternoon batch crashed:\n%s", traceback.format_exc())


@app.route(route="run_now", auth_level=func.AuthLevel.FUNCTION)
def run_now(req: func.HttpRequest) -> func.HttpResponse:
    try:
        which = req.params.get("batch", "1")
        notes = afternoon() if which == "2" else morning()
        return func.HttpResponse("NOTES: " + "; ".join(notes), status_code=200,
                                 mimetype="text/plain; charset=utf-8")
    except Exception:
        return func.HttpResponse("CRASH:\n" + traceback.format_exc(), status_code=500,
                                 mimetype="text/plain; charset=utf-8")
