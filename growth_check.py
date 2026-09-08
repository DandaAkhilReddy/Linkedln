"""
Phone-friendly follower check-in loop, fully serverless:
  - send_ask(): emails "reply with your follower count" (daily timer)
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
from email.header import decode_header

SUBJECT = "LinkedIn Growth Check-in"
BLOB = "growth_log.json"
CHAT_BLOB = "chat_history.json"
DEFAULT_GOAL = 200

ALLOWED_ACTIONS = {"linkedin_autopost_enabled", "cards_per_company",
                   "jobs_per_card", "log_followers", "strategy_arm"}


def _secrets(container):
    try:
        return json.loads(container.download_blob("li_secrets.json").readall())
    except Exception:
        return {}


def _save_secrets(container, sec):
    container.upload_blob("li_secrets.json", json.dumps(sec), overwrite=True)


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


def _strategy_text(container):
    try:
        import strategy
        return strategy.summary(container)
    except Exception:
        return ""


def log_count(container, count, note):
    """Append a follower count (deduped per day), credit the strategy arms
    with the gain since the previous day's count, return the analysis text."""
    log_ = _load_log(container)
    goal = log_.get("goal_per_day", DEFAULT_GOAL)
    entries = log_["entries"]
    today = datetime.date.today().isoformat()
    prev = next((e for e in reversed(entries) if e["date"] != today), None)
    entries[:] = [e for e in entries if e["date"] != today]     # one entry per day
    entries.append({"date": today, "followers": int(count), "note": note})
    container.upload_blob(BLOB, json.dumps(log_, indent=1), overwrite=True)
    if not prev:
        return f"Logged {count:,} (baseline)."
    d0 = datetime.date.fromisoformat(prev["date"])
    days = max((datetime.date.today() - d0).days, 1)
    gained = int(count) - prev["followers"]
    rate = gained / days
    try:
        import strategy
        strategy.credit(container, d0, prev["followers"], datetime.date.today(), int(count))
    except Exception:
        pass
    verdict = ("ON TRACK \U0001F680" if rate >= goal else
               f"{goal - rate:.0f}/day short of the {goal}/day goal")
    return (f"Logged {count:,}.\n+{gained:,} in {days} day(s) = {rate:+.0f}/day — {verdict}\n\n"
            + _strategy_text(container))


def send_ask(container):
    log_ = _load_log(container)
    last = log_["entries"][-1] if log_["entries"] else None
    base = (f"Last logged: {last['followers']:,} on {last['date']}."
            if last else "No entries yet — this will be the baseline.")
    body = ("Reply with just your current LinkedIn follower count (e.g. 15400) — "
            "one number a day is what trains the strategy picker.\n\n"
            f"{base}\n\n{_strategy_text(container)}\n\n"
            "You can also reply in plain words: 'switch to prime', 'back to volume', "
            "'auto', 'pause', 'status', 'how's the queue?'.\n\n— your jobs bot")
    return ["ask " + _send("\U0001F4C8 " + SUBJECT, body)]


def _decode_subj(raw):
    out = ""
    for txt, enc in decode_header(raw or ""):
        out += txt.decode(enc or "utf-8", "ignore") if isinstance(txt, bytes) else txt
    return out


def _top_text(body):
    top_lines = []
    for line in body.splitlines():
        if line.strip().startswith(">") or re.match(r"^On .{5,80} wrote:", line):
            break
        top_lines.append(line)
    return "\n".join(top_lines)[:1500].strip()


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
        # Self-sent replies arrive pre-SEEN, so track a UID cursor instead
        try:
            mstate = json.loads(container.download_blob("li_mail_state.json").readall())
        except Exception:
            mstate = {"last_uid": 0}
        last_uid = int(mstate.get("last_uid", 0))
        typ, data = M.uid("search", None, "SUBJECT", f'"{SUBJECT}"')
        uids = sorted(int(u) for u in (data[0] or b"").split())
        fresh_uids = [u for u in uids if u > last_uid]
        if uids:
            mstate["last_uid"] = max(uids)
            container.upload_blob("li_mail_state.json", json.dumps(mstate),
                                  overwrite=True)
        for num in fresh_uids:
            typ, msgdata = M.uid("fetch", str(num), "(RFC822)")
            m = email.message_from_bytes(msgdata[0][1])
            subj = _decode_subj(m.get("Subject", ""))
            low = subj.lower()
            if "re:" not in low and "jobs bot" not in low:
                continue        # our own ask (self-delivered), not a reply
            body = ""
            if m.is_multipart():
                for part in m.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(errors="ignore")
                        break
            else:
                body = m.get_payload(decode=True).decode(errors="ignore")
            top = _top_text(body)
            count = _extract_count(body)
            looks_numeric = count is not None and len(top.strip()) <= 40
            if not looks_numeric:
                notes.append(_chat_reply(container, subj, top))
                continue
            analysis = log_count(container, count, "via email") + "\n\n— your jobs bot"
            _send("Re: " + SUBJECT, analysis)
            notes.append(f"logged {count:,}")
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return notes or ["no new replies"]


def _chat_reply(container, subj, user_text):
    """Free-form email chat via Azure OpenAI; can apply config ACTIONs."""
    sec = _secrets(container)
    ep = sec.get("aoai_endpoint") or os.getenv("AOAI_ENDPOINT")
    key = sec.get("aoai_key") or os.getenv("AOAI_KEY")
    dep = sec.get("aoai_deployment") or os.getenv("AOAI_DEPLOYMENT", "gpt-4o-mini")
    reply_subj = subj if subj.lower().startswith("re:") else "Re: " + subj
    if not (ep and key):
        _send(reply_subj,
              "Chat brain isn't connected yet — an Azure OpenAI endpoint/key "
              "still needs to be added (ask Claude on the laptop).\n— your jobs bot")
        return "chat requested but AOAI not configured"
    import llm_chat
    log_ = _load_log(container)
    tail = log_["entries"][-5:]
    try:
        qlen = len(json.loads(container.download_blob("li_queue.json").readall()))
    except Exception:
        qlen = 0
    cfg_now = {k: sec.get(k) for k in ("linkedin_autopost_enabled",
                                       "cards_per_company", "jobs_per_card", "strategy_arm")}
    system = (
        "You are the email assistant for Reddy's LinkedIn jobs auto-poster "
        "(posts new job openings from 17 tech companies — Microsoft, Apple, Google, "
        "Amazon, NVIDIA, Meta, OpenAI, Anthropic, Netflix, xAI, Databricks, Stripe, "
        "Scale AI, Ramp, Cursor, AMD, IBM — to his personal LinkedIn with logo cards, "
        "salary hooks, @company tags, a follow CTA, plus one poll and one PDF "
        "carousel a day). Strategy arms: 'volume' = 1 post/10 min 24/7; 'prime' = "
        "1 post/30 min 7am-9pm ET; 'auto' = the bandit picks by followers/day. "
        "Answer his email briefly and concretely (plain text, no markdown). "
        "Growth goal: 200 followers/day. Recent growth log: "
        + json.dumps(tail) + ". Cards queued right now: " + str(qlen) +
        ". Current config overrides: " + json.dumps(cfg_now) +
        ". Strategy status:\n" + _strategy_text(container) +
        "\nIf (and only if) he asks for a settings change or reports a "
        "follower count, append a final line exactly like: "
        "ACTION: {\"cards_per_company\": 4} using only these keys: "
        "linkedin_autopost_enabled ('true'/'false'), cards_per_company (1-10), "
        "jobs_per_card (2-6), log_followers (integer), strategy_arm "
        "('volume'/'prime'/'auto'). Never invent other keys.")
    try:
        hist = json.loads(container.download_blob(CHAT_BLOB).readall())
    except Exception:
        hist = []
    msgs = ([{"role": "system", "content": system}] + hist[-10:] +
            [{"role": "user", "content": user_text}])
    try:
        out = llm_chat.chat(ep, key, dep, msgs)
    except Exception as e:
        _send(reply_subj, f"Chat brain error: {e}\n— your jobs bot")
        return f"chat error {e}"
    applied = []
    body_out = []
    for line in out.splitlines():
        if line.strip().startswith("ACTION:"):
            try:
                act = json.loads(line.split("ACTION:", 1)[1].strip())
            except Exception:
                continue
            sec2 = _secrets(container)
            for k, v in act.items():
                if k not in ALLOWED_ACTIONS:
                    continue
                if k == "log_followers":
                    applied.append(log_count(container, int(v), "via chat"))
                elif k == "strategy_arm":
                    arm = str(v).lower()
                    if arm == "auto":
                        sec2.pop("strategy_arm", None); applied.append("strategy=auto")
                    elif arm in ("volume", "prime"):
                        sec2["strategy_arm"] = arm; applied.append(f"strategy_arm={arm}")
                elif k == "cards_per_company":
                    sec2[k] = max(1, min(10, int(v))); applied.append(f"{k}={sec2[k]}")
                elif k == "jobs_per_card":
                    sec2[k] = max(2, min(6, int(v))); applied.append(f"{k}={sec2[k]}")
                else:
                    sec2[k] = str(v).lower(); applied.append(f"{k}={sec2[k]}")
            _save_secrets(container, sec2)
        else:
            body_out.append(line)
    answer = "\n".join(body_out).strip()
    if applied:
        answer += "\n\n\u2705 Applied: " + ", ".join(applied)
    answer += "\n\n\u2014 your jobs bot"
    hist += [{"role": "user", "content": user_text[:800]},
             {"role": "assistant", "content": answer[:800]}]
    container.upload_blob(CHAT_BLOB, json.dumps(hist[-16:]), overwrite=True)
    _send(reply_subj, answer)
    return "chat answered"
