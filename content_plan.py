"""
What goes in the next 10-minute slot: a job card or one of the educational
tracks. Quota-based, so it self-corrects — every track gets exactly its
daily count, spread evenly through the UTC day, and jobs take every other
slot. A missed slot doesn't shift the whole plan; a track that is behind its
pace simply gets the next slot.

  pick(store, now) -> "job" | "dsa" | "sd" | "mlsd" | "ai" | "papers"

Rule: track T is "due" when posted_today(T) < ceil(target(T) * slot / 144),
where slot = minutes since UTC midnight // 10 (+1 so the first slot counts).
The most-behind due track wins; none due -> job.
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


def pick(store, now=None, targets=None):
    import edu_content
    now = now or datetime.datetime.now(timezone.utc)
    targets = targets if targets is not None else edu_content.targets(store)
    if not any(targets.values()):
        return "job"
    counts = _today_counts(store, now)
    slot = min(SLOTS_PER_DAY, (now.hour * 60 + now.minute) // 10 + 1)
    best, best_gap = "job", 0.0
    for track in edu_content.TRACKS:
        tgt = targets.get(track, 0)
        if not tgt:
            continue
        done = counts.get(track, 0)
        if done >= tgt:
            continue
        expected = math.ceil(tgt * slot / SLOTS_PER_DAY)
        gap = (expected - done) / tgt          # relative lag, so small tracks aren't starved
        if expected > done and gap > best_gap:
            best, best_gap = track, gap
    return best


def summary(store, now=None):
    """'dsa 4/10 · sd 3/10 · …' for logs and the daily email."""
    import edu_content
    now = now or datetime.datetime.now(timezone.utc)
    tg = edu_content.targets(store)
    counts = _today_counts(store, now)
    return " · ".join(f"{t} {counts.get(t, 0)}/{tg[t]}" for t in edu_content.TRACKS)
