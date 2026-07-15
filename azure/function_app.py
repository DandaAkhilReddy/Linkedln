"""
Azure Functions (Python v2 model) — daily timer trigger.
Runs the pipeline every day at 7:00 AM ET and:
  1. Saves the post(s) to Blob Storage (container: linkedin-posts)
  2. Emails them to you via Gmail SMTP

App settings required:
  AzureWebJobsStorage      -> set automatically at creation (used for blobs too)
  GMAIL_USERNAME           -> your Gmail address
  GMAIL_APP_PASSWORD       -> Gmail app password (myaccount.google.com/apppasswords)
  MAIL_TO                  -> where to receive the post
"""

import os
import ssl
import logging
import datetime
import smtplib
from email.mime.text import MIMEText

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from ms_jobs_pipeline import run as run_pipeline

app = func.FunctionApp()

# 11:00 UTC = 7:00 AM ET (summer)
@app.timer_trigger(schedule="0 0 11 * * *", arg_name="timer", run_on_startup=False)
def daily_ms_jobs(timer: func.TimerRequest) -> None:
    logging.info("MS Jobs pipeline triggered at %s", datetime.datetime.utcnow())

    post = run_pipeline()
    if not post:
        logging.info("No new jobs today; skipping.")
        return

    # 1) Save to Blob Storage (reuses the function's own storage account)
    try:
        conn = os.environ["AzureWebJobsStorage"]
        container = BlobServiceClient.from_connection_string(conn).get_container_client("linkedin-posts")
        try:
            container.create_container()
        except Exception:
            pass  # already exists
        blob_name = f"post_{datetime.date.today().isoformat()}.txt"
        container.upload_blob(blob_name, post, overwrite=True)
        logging.info("Uploaded %s", blob_name)
    except Exception as e:
        logging.warning("Blob upload failed: %s", e)

    # 2) Email via Gmail SMTP
    user = os.environ.get("GMAIL_USERNAME")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("MAIL_TO")
    if user and pwd and to:
        try:
            msg = MIMEText(post, "plain", "utf-8")
            msg["Subject"] = f"🚀 Your Microsoft jobs LinkedIn posts — {datetime.date.today():%B %d, %Y}"
            msg["From"] = f"MS Jobs Bot <{user}>"
            msg["To"] = to
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
                s.login(user, pwd)
                s.sendmail(user, [to], msg.as_string())
            logging.info("Email sent to %s", to)
        except Exception as e:
            logging.warning("Email failed: %s", e)
    else:
        logging.info("Email settings not configured; post is in blob storage.")
