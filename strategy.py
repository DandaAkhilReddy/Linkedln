"""
Strategy arms + a small bandit, so the system *tries* growth strategies and
learns from the follower counts Reddy reports by email.

An arm is a posting policy: cadence, hours, cards per company, and whether
the daily poll / carousel run. The active arm is decided per BLOCK_DAYS-day
block:
  1. li_secrets.json["strategy_arm"]   manual override from the email chatbot
                                       ("volume" / "prime" / "auto")
  2. exploration: cycle through ORDER until every arm has >= BLOCK_DAYS days
     of attributed follower data
  3. exploitation: best followers/day; every 4th block re-tests the runner-up

credit() is called by growth_check when a follower count arrives: the gain
since the previous count is split evenly over the days in between and added
to the arms that were active on those days (li_strategy.json["stats"]).
summary() renders the scoreboard for the check-in / analysis emails.
"""

import json
import datetime
from datetime import timezone, timedelta

STRAT_BLOB = "li_strategy.json"
BLOCK_DAYS = 3
START = datetime.date(2026, 9, 9)

ARMS = {
    "volume": {"label": "1 post / 10 min, 24/7 + daily poll + carousel",
               "spacing_min": 10, "window_et": None,
               "cards_per_company": 10, "jobs_per_card": 4,
               "poll": True, "carousel": True},
    "prime":  {"label": "1 post / 30 min, 7am-9pm ET + daily poll + carousel",
               "spacing_min": 30, "window_et": (7, 21),
               "cards_per_company": 3, "jobs_per_card": 5,
               "poll": True, "carousel": True},
}
ORDER = ["volume", "prime"]
DEFAULT_ARM = "volume"


def _load(store):
    try:
        st = json.loads(store.download_blob(STRAT_BLOB).readall())
    except Exception:
        st = {}
    st.setdefault("blocks", {})
    st.setdefault("days", {})
    st.setdefault("stats", {})
    return st


def _save(store, st):
    store.upload_blob(STRAT_BLOB, json.dumps(st, indent=1), overwrite=True)


def _et(now=None):
    now = now or datetime.datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/New_York"))
    except Exception:                      # no tz database: assume EDT
        return now.astimezone(timezone(timedelta(hours=-4)))


def et_date(now=None):
    return _et(now).date()


def _rate(stats, arm):
    s = stats.get(arm) or {}
    return (s.get("gained", 0.0) / s["days"]) if s.get("days") else None


def _override(store):
    try:
        sec = json.loads(store.download_blob("li_secrets.json").readall())
        arm = str(sec.get("strategy_arm", "")).lower()
        return arm if arm in ARMS else None
    except Exception:
        return None


def arm_for(store, day, st=None):
    """Arm for a calendar day (ET). Block decisions are cached so a block
    never flips mid-way when new follower data arrives."""
    ov = _override(store)
    if ov:
        return ov
    st = st if st is not None else _load(store)
    if day < START:
        return DEFAULT_ARM
    block = (day - START).days // BLOCK_DAYS
    cached = st["blocks"].get(str(block))
    if cached in ARMS:
        return cached
    stats = st["stats"]
    if any((stats.get(a) or {}).get("days", 0) < BLOCK_DAYS for a in ORDER):
        arm = ORDER[block % len(ORDER)]                       # explore
    else:
        ranked = sorted(ORDER, key=lambda a: -(_rate(stats, a) or 0))
        arm = ranked[1] if block % 4 == 3 else ranked[0]      # exploit (+ re-test runner-up)
    st["blocks"][str(block)] = arm
    _save(store, st)
    return arm


def policy(store, now=None):
    """Active arm + its settings for right now; logs the arm for today (ET)."""
    day = et_date(now)
    st = _load(store)
    arm = arm_for(store, day, st)
    if st["days"].get(day.isoformat()) != arm:
        st["days"][day.isoformat()] = arm
        _save(store, st)
    p = dict(ARMS[arm])
    p["arm"] = arm
    return p


def should_post(store, now, last_post_ts):
    """Gate for drain(): posting window + spacing of the active arm."""
    p = policy(store, now)
    if p["window_et"]:
        h = _et(now).hour + _et(now).minute / 60
        start, end = p["window_et"]
        if not (start <= h < end):
            return False, f"{p['arm']}: outside {start}:00-{end}:00 ET window"
    if last_post_ts is not None:
        gap = (now - last_post_ts).total_seconds() / 60
        if gap < p["spacing_min"] - 2:            # -2: timer jitter
            return False, f"{p['arm']}: last post {gap:.0f} min ago (< {p['spacing_min']})"
    return True, p["arm"]


def expected(p):
    """(expected posts/day, max acceptable gap in minutes) for a policy."""
    hours = 24 if not p["window_et"] else (p["window_et"][1] - p["window_et"][0])
    posts = int(hours * 60 / p["spacing_min"]) + int(p["poll"]) + int(p["carousel"])
    gap = p["spacing_min"] * 4.5
    if p["window_et"]:
        gap = max(gap, (24 - hours) * 60 + p["spacing_min"] + 15)
    return posts, int(gap)


def credit(store, prev_date, prev_count, date, count):
    """Split the gain between two check-ins over the days [prev_date, date)
    and add it to the arms active on those days."""
    if not prev_date or date <= prev_date:
        return None
    st = _load(store)
    days = []
    d = prev_date
    while d < date:
        days.append(d)
        d += timedelta(days=1)
    per_day = (count - prev_count) / len(days)
    for d in days:
        arm = st["days"].get(d.isoformat()) or arm_for(store, d, st)
        s = st["stats"].setdefault(arm, {"days": 0, "gained": 0.0})
        s["days"] += 1
        s["gained"] += per_day
    st.setdefault("credits", []).append({"from": prev_date.isoformat(), "to": date.isoformat(),
                                         "gain": count - prev_count, "days": len(days)})
    st["credits"] = st["credits"][-60:]
    _save(store, st)
    return per_day


def summary(store, now=None):
    st = _load(store)
    day = et_date(now)
    cur = arm_for(store, day, st)
    block = max((day - START).days // BLOCK_DAYS, 0)
    next_switch = START + timedelta(days=(block + 1) * BLOCK_DAYS)
    lines = [f"Strategy now: {cur} — {ARMS[cur]['label']}",
             f"Next block starts {next_switch.isoformat()} "
             f"({'manual override' if _override(store) else 'auto-chosen from the scoreboard'})",
             "Scoreboard (followers/day):"]
    for a in ORDER:
        r = _rate(st["stats"], a)
        d = (st["stats"].get(a) or {}).get("days", 0)
        lines.append(f"  - {a}: " + (f"{r:+.0f}/day over {d} day(s)" if r is not None else "no data yet"))
    return "\n".join(lines)
