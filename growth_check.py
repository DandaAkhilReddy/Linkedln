"""
Phone-friendly follower check-in loop, fully serverless:
  - send_ask(): emails "reply with your follower count" (Mon/Thu timer)
  - poll_replies(): reads the reply via Gmail IMAP, logs it to blob
    growth_log.json, and emails back a growth analysis vs the daily goal.
Reuses the function app's existing GMAIL_USERNAME / GMAIL_APP_PASSWORD /
MAIL_TO settings. Only touches messages whose subject matches SUBJECT.
"""

import os
import re
import ssl
import json
import email
import imaplib
import smtplib
import datetime
from email.mime.text import MIMEText

SUBJECT = "LinkedIn Growth Check-in"
BLOB = "growth_log.json"
DEFAULT_GOAL = 200


def _creds():
    return (os.environ.get("GMAIL_USERNAME"),
            os.environ.get("GMAIL_APP_PASSWORD"),
            os.environ.get("MAIL_TO"))


def _send(subject, body):
    user, pwd, to = _creds()
    if not (user and pwd and to):
        return "email not configured"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"Growth Bot <{user}>"
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                          context=ssl.create_default_context()) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    return f"sent to {to}"


def _load_log(container):
    try:
        return json.loads(container.download_blob(BLOB).readall())
    except Exception:
        return {"goal_per_day": DEFAULT_GOAL, "entries": []}


def send_ask(container):
    log_ = _load_log(container)
    last = log_["entries"][-1] if log_["entries"] else None
    base = (f"Last logged: {last['followers']:,} on {last['date']}."
            if last else "No entries yet — this will be the baseline.")
    body = ("Reply to this email with just your current LinkedIn follower "
            f"count (e.g. 15400).\n\n{base}\n\nOptional: mention which hook "
            "style got the most impressions — salary / question / urgency — "
            "and I'll factor it in.\n\n— your jobs bot")
    return ["ask " + _send("\U0001F4C8 " + SUBJECT, body)]


def _extract_count(body):
    """First follower-count-looking number in the un-quoted top of a reply."""
    top_lines = []
    for line in body.splitlines():
        if line.strip().startswith(">") or re.match(r"^On .{5,80} wrote:", line):
            break
        top_lines.append(line)
    top = "\n".join(top_lines)[:500]
    m = re.search(r"(\d{1,3}(?:,\d{3})+|\d{4,8}|\d+(?:\.\d+)?\s*[kK])", top)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.lower().endswith("k"):
        return int(float(raw[:-1]) * 1000)
    return int(raw.replace(",", ""))


def poll_replies(container):
    user, pwd, _ = _creds()
    if not (user and pwd):
        return ["email not configured"]
    notes = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        M.login(user, pwd)
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN", "SUBJECT", f'"{SUBJECT}"')
        for num in (data[0] or b"").split():
            typ, msgdata = M.fetch(num, "(RFC822)")   # fetch marks it seen
            m = email.message_from_bytes(msgdata[0][1])
            subj = m.get("Subject", "")
            if "re:" not in subj.lower():
                continue        # our own ask (self-delivered), not a reply
            body = ""
            if m.is_multipart():
                for part in m.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = m.get_payload(decode=True).decode(errors="ignore")
            count = _extract_count(body)
            if count is None or count < 100:
                notes.append("reply found but no count parsed")
                _send("Re: " + SUBJECT,
                      "Couldn't find a number in your reply — send just the "
                      "count, e.g. 15400.\n— your jobs bot")
                continue
            log_ = _load_log(container)
            goal = log_.get("goal_per_day", DEFAULT_GOAL)
            entries = log_["entries"]
            prev = entries[-1] if entries else None
            today = datetime.date.today().isoformat()
            entries.append({"date": today, "followers": count, "note": "via email"})
            container.upload_blob(BLOB, json.dumps(log_, indent=1), overwrite=True)
            if prev and prev["date"] != today:
                d0 = datetime.date.fromisoformat(prev["date"])
                days = max((datetime.date.today() - d0).days, 1)
                gained = count - prev["followers"]
                rate = gained / days
                verdict = ("ON TRACK \U0001F680" if rate >= goal else
                           f"below the {goal}/day goal — hold volume, double "
                           "down on replying to comments within the first hour")
                analysis = (f"Logged {count:,}.\n"
                            f"+{gained:,} in {days} day(s) = {rate:.0f}/day. {verdict}\n\n"
                            "Current strategy: 5 posts/company max, salary/question/"
                            "urgency hooks rotating, @company tags, 7:30a-7p ET.\n"
                            "— your jobs bot")
            else:
                analysis = f"Logged {count:,}. — your jobs bot"
            _send("Re: " + SUBJECT, analysis)
            notes.append(f"logged {count:,}")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return notes or ["no new replies"]
