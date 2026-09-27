"""
What goes in the next 10-minute slot: a job card or one of the educational
tracks. Quota-based, so it self-corrects — every track gets exactly its
daily count, spread evenly through the UTC day, and jobs take every other
slot. A missed slot doesn't shift the whole plan; a track that is behind its
pace simply gets the next slot.

  pick(store, now) -> "job" | "dsa" | "sd" | "mlsd" | "ai" | "papers"

Rhythm: two job posts, then one Q&A (J J E J J E …), 48 Q&A slots a day
shared by the tracks (most-behind first, "behind" = posted < ceil(target ×
slot/144)); a late-day catch-up lets a short track take an extra E slot but
never two E in a row. No track due -> job.
"""

import math
import json
import datetime
from datetime import timezone

SLOTS_PER_DAY = 144
POST_LOG = "li_post_log.json"


def _today_counts(store, now):
    try:
        plog = json.loads(store.download_blob(POST_LOG).readall())
    except Exception:
        plog = []
    day = now.date().isoformat()
    counts = {}
    for p in plog:
        if str(p.get("ts", "")).startswith(day):
            v = p.get("variant")
            counts[v] = counts.get(v, 0) + 1
    return counts


def _recent_slot_variants(store, now, n=2):
    """Variants of the last n slot posts (job cards, fillers, Q&A) — polls and
    carousels are extras and don't count."""
    try:
        plog = json.loads(store.download_blob(POST_LOG).readall())
    except Exception:
        plog = []
    import linkedin_autopost as la
    out = [p.get("variant") for p in plog if p.get("variant") in la.SLOT_VARIANTS]
    return out[-n:]


def pick(store, now=None, targets=None):
    """Reddy's rhythm: two job posts, then one Q&A — J J E J J E … — so jobs
    keep flowing all day and the five tracks share the E slots (most-behind
    track first). Late in the day a track that would otherwise miss its
    quota may take an extra E slot, but never two E in a row."""
    import edu_content
    now = now or datetime.datetime.now(timezone.utc)
    targets = targets if targets is not None else edu_content.targets(store)
    if not any(targets.values()):
        return "job"
    counts = _today_counts(store, now)
    slot = min(SLOTS_PER_DAY, (now.hour * 60 + now.minute) // 10 + 1)
    due = []
    for track in edu_content.TRACKS:
        tgt = targets.get(track, 0)
        done = counts.get(track, 0)
        if tgt and done < tgt:
            expected = math.ceil(tgt * slot / SLOTS_PER_DAY)
            due.append(((expected - done) / tgt, tgt - done, track))
    if not due:
        return "job"
    due.sort(reverse=True)
    recent = _recent_slot_variants(store, now, 2)
    edu_flags = [v in edu_content.TRACKS for v in recent]
    if len(recent) >= 2 and not any(edu_flags):          # J J -> E
        return due[0][2]
    # late-day catch-up: still short, last post was a job -> allow E after J E J
    slots_left = SLOTS_PER_DAY - slot
    short = sum(d[1] for d in due)
    if recent and not edu_flags[-1] and short >= slots_left / 3 + 1 and slots_left <= 36:
        return due[0][2]
    return "job"


def summary(store, now=None):
    """'dsa 4/10 · sd 3/10 · …' for logs and the daily email."""
    import edu_content
    now = now or datetime.datetime.now(timezone.utc)
    tg = edu_content.targets(store)
    counts = _today_counts(store, now)
    return " · ".join(f"{t} {counts.get(t, 0)}/{tg[t]}" for t in edu_content.TRACKS)
