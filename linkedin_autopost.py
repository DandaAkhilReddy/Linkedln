"""
LinkedIn auto-post engine (drip model, fits the 10-min function timeout).

generate(): once/day — for each company pull new jobs (own dedupe state),
  split into N cards, render each, store PNG in blob, and append a queue entry
  with a staggered post_after time (SPACING_MIN apart).
drain(): frequent timer — post any queue entries whose post_after has passed
  (up to MAX_PER_DRAIN each run), so ~N*companies posts trickle out over the day.

Gated by env LINKEDIN_AUTOPOST_ENABLED == "true".
State/queue live in the same `linkedin-posts` blob container as the emailer.
"""

import os
import io
import re
import json
import random
import base64
import logging
import datetime
from datetime import timezone, timedelta

import card_builder
import linkedin_client

log = logging.getLogger("li-autopost")

def _cfg(key, default):
    import linkedin_client
    s = linkedin_client._blob_secrets()
    return str(s.get(key, os.getenv(key.upper(), default)))


QUEUE_BLOB = "li_queue.json"
CARDS_PREFIX = "li_cards/"
CARDS_PER_COMPANY = int(os.getenv("LINKEDIN_CARDS_PER_COMPANY", "5"))
SPACING_MIN = int(os.getenv("LINKEDIN_SPACING_MIN", "25"))
MAX_PER_DRAIN = int(os.getenv("LINKEDIN_MAX_PER_DRAIN", "2"))

# imported lazily to avoid circular import with function_app
COMPANIES = None

# Verified LinkedIn organization URNs (blue @mention tags). Only IDs we are
# sure of — a wrong ID would tag the wrong company; anthropic/xai stay plain.
ORG_URNS = {
    "microsoft": "urn:li:organization:1035",
    "google":    "urn:li:organization:1441",
    "amazon":    "urn:li:organization:1586",
    "apple":     "urn:li:organization:162479",
    "netflix":   "urn:li:organization:165158",
    "nvidia":    "urn:li:organization:3608",
    "meta":      "urn:li:organization:10667",
    "openai":    "urn:li:organization:11130470",
}

HOOK_VARIANTS = ["salary_hook", "question_hook", "urgency_hook"]


def _set_companies(companies):
    global COMPANIES
    COMPANIES = companies


def _li_state_blob(company):
    return f"li_{company}_state.json"


def _load(c, blob, default):
    try:
        return json.loads(c.download_blob(blob).readall())
    except Exception:
        return default


def _save(c, blob, obj):
    c.upload_blob(blob, json.dumps(obj), overwrite=True)


def _job_url(company, j):
    i = j.get("id")
    if company == "meta":
        return f"https://www.metacareers.com/jobs/{i}/"
    if company == "microsoft":
        return f"https://jobs.careers.microsoft.com/global/en/job/{i}"
    if company == "apple":
        return f"https://jobs.apple.com/en-us/details/{j.get('positionId', i)}/{j.get('transformedPostingTitle','')}"
    if company == "amazon":
        return "https://www.amazon.jobs" + (j.get("job_path") or "")
    if company == "nvidia":
        return "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job" + (j.get("externalPath") or "")
    if company == "google":
        return j.get("url") or ""
    return j.get("url") or ""


def _loc_str(l):
    """Amazon-style entries are serialized JSON blobs — extract City, State."""
    if isinstance(l, str) and l.lstrip().startswith("{"):
        try:
            d = json.loads(l)
            city = d.get("city")
            st = d.get("normalizedStateName") or d.get("normalizedCountryName")
            return ", ".join(x for x in (city, st) if x) or \
                   d.get("locationNonStemming") or "United States"
        except Exception:
            import re as _re
            m = _re.search(r'"city"\s*:\s*"([^"]+)"', l)
            n = _re.search(r'"normalizedStateName"\s*:\s*"([^"]+)"', l)
            return ", ".join(x.group(1) for x in (m, n) if x) or "United States"
    return l


def _job_loc(j):
    locs = j.get("locations")
    if isinstance(locs, list) and locs:
        if isinstance(locs[0], str):
            return "; ".join(_loc_str(l) for l in locs[:2])
        # apple-style list of dicts
        out = []
        for l in locs[:2]:
            out.append(", ".join(x for x in (l.get("city"), l.get("stateProvince"),
                                             l.get("countryName")) if x) or "United States")
        return "; ".join(out)
    if isinstance(j.get("location"), str):
        return j["location"]
    if j.get("locationsText"):
        return j["locationsText"]
    return "United States"


def _job_team(company, j):
    t = j.get("team")
    if isinstance(t, dict):
        return t.get("teamName", "")
    if isinstance(t, str) and t:
        return t
    teams = (j.get("teams") or []) + (j.get("sub_teams") or [])
    if teams:
        return " | ".join(teams[:2])
    if j.get("job_category"):
        return j["job_category"]
    if company == "microsoft":
        props = j.get("properties") or {}
        lvl = props.get("roleType") or props.get("discipline")
        return lvl[0] if isinstance(lvl, list) and lvl else (lvl or "")
    return ""


def _top_pay(jobs):
    """Largest salary figure across the chunk, for the hook line."""
    best = 0
    for j in jobs:
        sal = j.get("salary") or (j.get("_detail") or {}).get("salary") or ""
        for m in re.finditer(r"\$?\s?([\d][\d,]*(?:\.\d+)?)\s*([Kk])?", sal):
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if m.group(2):
                v *= 1000
            best = max(best, v)
    return f"${int(best):,}" if best >= 10000 else None


def _caption(company, jobs, part, total, style="salary_hook"):
    name = card_builder.display_name(company)
    n = len(jobs)
    top = _top_pay(jobs)
    roles = "1 new role" if n == 1 else f"{n} new roles"
    if style == "question_hook":
        hook = f"Want to work at {name}? {roles.capitalize()} just opened 👀"
        if top:
            hook += f"\nPay up to {top} 💰"
    elif style == "urgency_hook":
        hook = f"🚨 {name} opened {roles} TODAY — early applicants win"
        if top:
            hook += f" (up to {top} 💰)"
    elif n == 1:
        t = jobs[0].get("title") or jobs[0].get("name") or "a new role"
        hook = f"{name} is hiring: {t}"
        if top:
            hook += f" — {top} 💰"
    else:
        hook = f"{name} just posted {roles}"
        if top:
            hook += f" — pay up to {top} 💰"
        else:
            hook += " 🚀"
    lines = [hook, "", "Fresh openings \U0001F447", ""]
    for j in jobs[:8]:
        det = j.get("_detail") or {}
        title = j.get("title") or j.get("name") or "Role"
        lines.append(f"\U0001F4BC {title}")
        lines.append(f"\U0001F4CD {_job_loc(j)}")
        sal = j.get("salary") or det.get("salary")
        if sal:
            lines.append(f"\U0001F4B0 {sal}")
        url = det.get("url") or _job_url(company, j)
        if url:
            lines.append(f"\U0001F517 {url}")
        lines.append("")
    lines += [
        "\u267B\ufe0f Repost to help a job seeker in your network.",
        "\U0001F4AC Which one are you applying to? \U0001F447",
        "",
        f"#{name}Careers #Hiring #TechJobs #JobSearch",
    ]
    cap = "\n".join(lines)
    if len(cap) > 2900:
        cap = cap[:2870].rsplit("\n", 1)[0] + "\n\n#Hiring #TechJobs"
    return cap


def generate(container, logo_loader=None, companies=None, hours=24):
    """Build the day's cards + queue them staggered. Returns notes."""
    if _cfg("linkedin_autopost_enabled", "false").lower() != "true":
        return ["autopost disabled (set LINKEDIN_AUTOPOST_ENABLED=true)"]
    now = datetime.datetime.now(timezone.utc)
    date_str = datetime.datetime.now().strftime("%B %d, %Y")
    queue = _load(container, QUEUE_BLOB, [])
    added = []
    notes = []
    slot = len(queue)  # continue staggering after anything already queued
    targets = companies or list(COMPANIES)
    for company in targets:
        cfg = COMPANIES[company]
        state = _load(container, _li_state_blob(company),
                      {"posted_ids": [], "last_run": None})
        cutoff = now - timedelta(hours=hours)
        try:
            fresh = cfg["pipeline"].get_jobs(cutoff)
        except Exception as e:
            notes.append(f"{company}: fetch failed {e}")
            continue
        posted = set(state.get("posted_ids", []))
        new = [j for j in fresh if str(j.get("id")) not in posted]
        cards_cap = max(1, min(5, int(_cfg("cards_per_company", CARDS_PER_COMPANY))))
        jpc = max(2, min(6, int(_cfg("jobs_per_card", JOBS_PER_CARD))))
        new = cfg["pipeline"].sort_software_first(new)[:cards_cap * jpc]
        if not new:
            state["last_run"] = now.isoformat()
            _save(container, _li_state_blob(company), state)
            notes.append(f"{company}: no new jobs")
            continue
        chunks = [new[i:i + jpc] for i in range(0, len(new), jpc)]
        for i, chunk in enumerate(chunks):
            # enrich for salary on the card (best-effort, bounded to this chunk)
            for j in chunk:
                try:
                    key = j.get("externalPath") if company == "nvidia" else j.get("id")
                    det = cfg["pipeline"].fetch_detail(key) or {}
                    j["_detail"] = det
                    if det.get("salary"):
                        j["salary"] = det["salary"]
                except Exception:
                    pass
            png = card_builder.build_card(company, chunk, part=i + 1,
                                          total=len(chunks), logo_loader=logo_loader)
            card_id = f"{company}_{now:%Y%m%d}_{i+1}"
            container.upload_blob(f"{CARDS_PREFIX}{card_id}.png", png, overwrite=True)
            style = random.choice(HOOK_VARIANTS)
            caption = _caption(company, chunk, i + 1, len(chunks), style)
            added.append({
                "company": company,
                "card_blob": f"{CARDS_PREFIX}{card_id}.png",
                "caption": caption,
                "variant": style,
                "title": f"{card_builder.display_name(company)} is hiring",
                "post_after": (now + timedelta(minutes=SPACING_MIN * slot)).isoformat(),
            })
            slot += 1
        state["posted_ids"] = list(dict.fromkeys(
            list(posted) + [str(j.get("id")) for j in fresh]))[-8000:]
        state["last_run"] = now.isoformat()
        _save(container, _li_state_blob(company), state)
        notes.append(f"{company}: queued {len(chunks)} cards ({len(new)} jobs)")
    # merge-on-save: re-read so a concurrent drain's queue update isn't clobbered
    queue = _load(container, QUEUE_BLOB, []) + added
    _save(container, QUEUE_BLOB, queue)
    notes.append(f"queue length {len(queue)}")
    return notes


def _in_posting_window(now):
    mins = now.hour * 60 + now.minute
    return 11 * 60 + 30 <= mins < 23 * 60      # 7:30a–7p ET (EDT)


def drain(container):
    """Post due queue entries (up to MAX_PER_DRAIN). Returns notes."""
    now = datetime.datetime.now(timezone.utc)
    if not _in_posting_window(now):
        return ["outside posting window (7:30a-7p ET) — holding queue"]
    if _cfg("linkedin_autopost_enabled", "false").lower() != "true":
        return ["autopost disabled"]
    now = datetime.datetime.now(timezone.utc)
    queue = _load(container, QUEUE_BLOB, [])
    if not queue:
        return ["queue empty"]
    try:
        token = linkedin_client._token()
    except Exception:
        return ["no LINKEDIN_ACCESS_TOKEN (env or blob)"]
    urn = linkedin_client.person_urn(token)
    remaining, posted, notes = [], 0, []
    for item in queue:
        due = datetime.datetime.fromisoformat(item["post_after"]) <= now
        if posted >= MAX_PER_DRAIN or not due:
            remaining.append(item)
            continue
        try:
            png = container.download_blob(item["card_blob"]).readall()
            mention = None
            org = ORG_URNS.get(item["company"])
            if org:
                mention = (card_builder.display_name(item["company"]), org)
            share = linkedin_client.post_with_image(item["caption"], png,
                                                    title=item["title"],
                                                    token=token,
                                                    mention=mention)
            notes.append(f"posted {item['company']} {share}")
            try:
                plog = _load(container, "li_post_log.json", [])
                plog.append({"ts": now.isoformat(), "company": item["company"],
                             "variant": item.get("variant", "salary_hook"),
                             "urn": share})
                _save(container, "li_post_log.json", plog[-2000:])
            except Exception:
                pass
            posted += 1
        except Exception as e:
            item["retries"] = item.get("retries", 0) + 1
            if item["retries"] < 3:
                remaining.append(item)
            notes.append(f"{item['company']} failed ({item.get('retries')}): {e}")
    _save(container, QUEUE_BLOB, remaining)
    notes.append(f"{posted} posted, {len(remaining)} left")
    return notes
