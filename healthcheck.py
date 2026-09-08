"""
Watchdog + heartbeat.

- record(store, key, ok, note): heartbeat written by generate/drain each run
  (li_health.json) so we can tell "never ran" from "ran and failed".
- assess(store): measures what actually happened — posts in the last 24h,
  the largest gap between consecutive posts, queue depth, last generate
  time, token validity, last errors — and returns {"status": "ok"|"alert",
  "issues": [...], ...metrics}.
- daily_report(store): emails the assessment once a day (subject flags ALERT).
- alert(store, subject, body): immediate email for hard failures (bad token),
  rate-limited to one per 6h per subject so a broken night doesn't spam.

Thresholds are deliberately simple: a gap > MAX_GAP_MIN between posts, or
fewer than MIN_POSTS_24H posts, is a failure of the "one post every 10 min"
contract and gets flagged.
"""

import os
import json
import datetime
from datetime import timezone, timedelta

HEALTH_BLOB = "li_health.json"
POST_LOG = "li_post_log.json"
QUEUE_BLOB = "li_queue.json"

MAX_GAP_MIN = int(os.getenv("HEALTH_MAX_GAP_MIN", "45"))      # 10-min cadence → 3 missed slots
MIN_POSTS_24H = int(os.getenv("HEALTH_MIN_POSTS_24H", "60"))   # cadence gives ~144; <60 = broken
GENERATE_MAX_AGE_H = 26
ALERT_COOLDOWN_H = 6


def _now():
    return datetime.datetime.now(timezone.utc)


def _load(store, name, default):
    try:
        return json.loads(store.download_blob(name).readall())
    except Exception:
        return default


def _save(store, name, obj):
    store.upload_blob(name, json.dumps(obj, indent=1), overwrite=True)


def record(store, key, ok=True, note=""):
    """Heartbeat: li_health.json[key] = {ts, ok, note}. Never raises."""
    try:
        h = _load(store, HEALTH_BLOB, {})
        h[key] = {"ts": _now().isoformat(), "ok": bool(ok), "note": str(note)[:300]}
        _save(store, HEALTH_BLOB, h)
    except Exception:
        pass


def _ts(s):
    try:
        d = datetime.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def assess(store):
    now = _now()
    issues = []
    plog = _load(store, POST_LOG, [])
    recent = sorted(t for t in (_ts(p.get("ts", "")) for p in plog) if t and now - t <= timedelta(hours=24))
    posts_24h = len(recent)
    max_gap = 0
    if recent:
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(recent, recent[1:])]
        gaps.append((now - recent[-1]).total_seconds() / 60)                    # silence since last post
        gaps.append((recent[0] - (now - timedelta(hours=24))).total_seconds() / 60)  # silence before first
        max_gap = round(max(gaps))
    else:
        max_gap = 24 * 60

    queue = _load(store, QUEUE_BLOB, [])
    health = _load(store, HEALTH_BLOB, {})
    last_gen = max((_ts(v.get("ts")) for k, v in health.items() if k.startswith("generate") and _ts(v.get("ts"))),
                   default=None)
    last_drain = health.get("drain", {})

    enabled = True
    try:
        sec = _load(store, "li_secrets.json", {})
        enabled = str(sec.get("linkedin_autopost_enabled", "true")).lower() == "true"
    except Exception:
        pass

    token_ok = None
    try:
        import linkedin_client
        token_ok = bool(linkedin_client.token_valid())
    except Exception:
        token_ok = False

    if not enabled:
        issues.append("autopost is DISABLED (linkedin_autopost_enabled=false)")
    else:
        if posts_24h < MIN_POSTS_24H:
            issues.append(f"only {posts_24h} posts in 24h (expected ~144)")
        if max_gap > MAX_GAP_MIN:
            issues.append(f"largest gap between posts: {max_gap} min (limit {MAX_GAP_MIN})")
    if not queue:
        issues.append("queue is EMPTY")
    if last_gen is None or now - last_gen > timedelta(hours=GENERATE_MAX_AGE_H):
        issues.append("no successful generate in the last 26h")
    if last_drain and not last_drain.get("ok", True):
        issues.append(f"last drain error: {last_drain.get('note')}")
    if token_ok is False:
        issues.append("LinkedIn access token INVALID/expired — re-authorize")

    return {
        "status": "alert" if issues else "ok",
        "checked_at": now.isoformat(),
        "posts_24h": posts_24h,
        "max_gap_min": max_gap,
        "queue": len(queue),
        "last_generate": last_gen.isoformat() if last_gen else None,
        "last_post": recent[-1].isoformat() if recent else None,
        "token_ok": token_ok,
        "enabled": enabled,
        "issues": issues,
    }


def alert(store, subject, body):
    """Immediate email, at most once per ALERT_COOLDOWN_H per subject."""
    try:
        h = _load(store, HEALTH_BLOB, {})
        key = "alert:" + subject
        last = _ts((h.get(key) or {}).get("ts", ""))
        if last and _now() - last < timedelta(hours=ALERT_COOLDOWN_H):
            return "alert suppressed (cooldown)"
        import growth_check
        growth_check._send("\U0001F6A8 LinkedIn Autopilot ALERT: " + subject, body + "\n\n— your jobs bot")
        h[key] = {"ts": _now().isoformat(), "ok": False, "note": body[:200]}
        _save(store, HEALTH_BLOB, h)
        return "alert sent"
    except Exception as e:
        return f"alert failed: {e}"


def daily_report(store):
    r = assess(store)
    ok = r["status"] == "ok"
    head = ("✅ All good — posting every 10 minutes." if ok else
            "⚠️ PROBLEMS FOUND:\n  - " + "\n  - ".join(r["issues"]))
    body = (f"{head}\n\n"
            f"Posts in last 24h: {r['posts_24h']}\n"
            f"Largest gap between posts: {r['max_gap_min']} min\n"
            f"Queue depth now: {r['queue']}\n"
            f"Last generate: {r['last_generate'] or 'never'}\n"
            f"Last post: {r['last_post'] or 'never'}\n"
            f"Token valid: {r['token_ok']}\n\n"
            + ("" if ok else "Self-heal runs automatically every 10 min (refill + retry). "
               "If this persists, reply 'status' to the check-in email or open the laptop.\n\n")
            + "— your jobs bot")
    import growth_check
    subject = ("\U0001FA7A LinkedIn Autopilot: OK" if ok else
               "\U0001F6A8 LinkedIn Autopilot: ALERT — " + r["issues"][0])
    sent = growth_check._send(subject, body)
    record(store, "health_report", ok, "; ".join(r["issues"]) or "ok")
    return [f"health {r['status']}: posts24h={r['posts_24h']} gap={r['max_gap_min']}m queue={r['queue']} ({sent})"]
