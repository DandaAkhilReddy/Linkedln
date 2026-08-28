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
import json
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


def _job_loc(j):
    locs = j.get("locations")
    if isinstance(locs, list) and locs:
        if isinstance(locs[0], str):
            return "; ".join(locs[:2])
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


def _caption(company, jobs, part, total):
    name = card_builder.display_name(company)
    date_str = datetime.datetime.now().strftime("%B %d, %Y")
    tag = f"\U0001F680 {name} is Hiring! | {date_str}"
    if total > 1:
        tag += f" (Part {part}/{total})"
    lines = [tag, "", "Fresh roles posted in the last 24 hours \U0001F447", ""]
    for j in jobs[:8]:
        det = j.get("_detail") or {}
        title = j.get("title") or j.get("name") or j.get("postingTitle") or "Role"
        lines.append(f"\U0001F4BC {title}")
        lines.append(f"\U0001F4CD {_job_loc(j)}")
        team = _job_team(company, j)
        if team:
            lines.append(f"\U0001F3AF {team}")
        sal = j.get("salary") or det.get("salary")
        if sal:
            lines.append(f"\U0001F4B0 {sal}")
        url = det.get("url") or _job_url(company, j)
        if url:
            lines.append(f"\U0001F517 {url}")
        lines.append("")
    lines += [
        "\u267B\ufe0f Repost to help someone in your network!",
        f"\U0001F514 Follow for daily {name} job updates.",
        "",
        f"#{name}Careers #Hiring #TechJobs #SoftwareEngineering #JobSearch #NowHiring",
    ]
    cap = "\n".join(lines)
    if len(cap) > 2900:            # LinkedIn hard limit is 3000
        cap = cap[:2870].rsplit("\n", 1)[0] + "\n\n#Hiring #TechJobs #NowHiring"
    return cap


def generate(container, logo_loader=None, companies=None):
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
        cutoff = now - timedelta(hours=24)
        try:
            fresh = cfg["pipeline"].get_jobs(cutoff)
        except Exception as e:
            notes.append(f"{company}: fetch failed {e}")
            continue
        posted = set(state.get("posted_ids", []))
        new = [j for j in fresh if str(j.get("id")) not in posted]
        new = cfg["pipeline"].sort_software_first(new)[:50]
        if not new:
            state["last_run"] = now.isoformat()
            _save(container, _li_state_blob(company), state)
            notes.append(f"{company}: no new jobs")
            continue
        chunks = card_builder.split_into(new, CARDS_PER_COMPANY)
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
            caption = _caption(company, chunk, i + 1, len(chunks))
            added.append({
                "company": company,
                "card_blob": f"{CARDS_PREFIX}{card_id}.png",
                "caption": caption,
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


def drain(container):
    """Post due queue entries (up to MAX_PER_DRAIN). Returns notes."""
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
            share = linkedin_client.post_with_image(item["caption"], png,
                                                    title=item["title"],
                                                    token=token, urn=urn)
            notes.append(f"posted {item['company']} {share}")
            posted += 1
        except Exception as e:
            item["retries"] = item.get("retries", 0) + 1
            if item["retries"] < 3:
                remaining.append(item)
            notes.append(f"{item['company']} failed ({item.get('retries')}): {e}")
    _save(container, QUEUE_BLOB, remaining)
    notes.append(f"{posted} posted, {len(remaining)} left")
    return notes
