"""
Optional email delivery of copy-paste-ready posts (the original product).
No scheduled timers call this anymore — LinkedIn auto-posting replaced it —
but `jobs.email_batch()` / the `run_now` HTTP route keep it available for
manual catch-up runs.
"""

import os
import ssl
import json
import smtplib
import datetime
from datetime import timezone, timedelta
from email.mime.text import MIMEText

from companies import COMPANIES

BATCH_SIZE = int(os.getenv("MAX_JOBS_TOTAL", "50"))


def _load_state(store, blob):
    try:
        return json.loads(store.download_blob(blob).readall())
    except Exception:
        return {"last_run": None, "parked": [], "sent_ids": []}


def _save_state(store, blob, state):
    state["sent_ids"] = state.get("sent_ids", [])[-5000:]
    store.upload_blob(blob, json.dumps(state), overwrite=True)


def send_email(post, subject_prefix, label):
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


def batch_run(store, company, label, blob_suffix, lookback_hours=None):
    """Email one company's new jobs since its last send; park overflow."""
    cfg = COMPANIES[company]
    pipeline, state_blob = cfg["pipeline"], cfg["state"]
    state = _load_state(store, state_blob)
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
    queue = pipeline.sort_software_first(
        parked + [j for j in fresh if j.get("id") not in sent_ids and j.get("id") not in seen])

    if not queue:
        state["last_run"] = now.isoformat()
        _save_state(store, state_blob, state)
        return [f"{company}: no new jobs since {cutoff:%H:%M UTC}"]

    seeding = cfg.get("seed_first_run") and not state.get("last_run")
    batch = queue[:BATCH_SIZE]
    rest = [] if seeding else queue[BATCH_SIZE:]
    post = pipeline.render_posts(batch)

    notes = []
    blob_name = f"{cfg['prefix']}_{datetime.date.today().isoformat()}_{blob_suffix}.txt"
    store.upload_blob(blob_name, post, overwrite=True)
    notes.append(f"{company}: blob {blob_name} ({len(batch)} jobs)")
    try:
        notes.append(send_email(post, cfg["subject"], f"({label} — {len(batch)} jobs)"))
    except Exception as e:
        notes.append(f"email failed: {e}")

    state["last_run"] = now.isoformat()
    state["parked"] = rest
    state["sent_ids"] = list(sent_ids) + [j.get("id") for j in (queue if seeding else batch)]
    _save_state(store, state_blob, state)
    if rest:
        notes.append(f"{len(rest)} parked for next send")
    return notes
