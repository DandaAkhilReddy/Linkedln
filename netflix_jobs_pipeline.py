"""
Netflix Careers -> LinkedIn Post Pipeline (Eightfold apply/v2 API).
explore.jobs.netflix.net exposes positions with t_create timestamps and
full job descriptions inline — salary parsed from the pay range in text.
"""

import os
import re
import time
import html
import logging
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("netflix-jobs")

BASE = "https://explore.jobs.netflix.net"
SEARCH_URL = BASE + "/api/apply/v2/jobs"

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))
PAGE_SIZE = 10
MAX_PAGES = 12

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Accept": "application/json"}

PAY_RE = re.compile(
    r"(\$\s?[\d][\d,]*(?:\.\d+)?\s*(?:[-–—]|to)\s*\$?\s?[\d][\d,]*(?:\.\d+)?)")

_DETAILS = {}  # id -> detail dict, filled by get_jobs


def _clean_loc(l):
    parts = [x.strip() for x in (l or "").split(",")
             if x.strip() and "United States of America" not in x]
    return ", ".join(parts[:2]) or "United States"


def fetch_recent_jobs(cutoff):
    jobs = []
    for page in range(MAX_PAGES):
        params = {"domain": "netflix.com", "start": page * PAGE_SIZE,
                  "num": PAGE_SIZE, "sort_by": "new"}
        r = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=30)
        r.raise_for_status()
        batch = r.json().get("positions") or []
        if not batch:
            break
        fresh_in_page = 0
        for j in batch:
            posted = datetime.fromtimestamp(j.get("t_create") or 0, tz=timezone.utc)
            if posted < cutoff:
                continue
            fresh_in_page += 1
            locs = j.get("locations") or ([j.get("location")] if j.get("location") else [])
            jobs.append({
                "id": str(j.get("id")),
                "name": j.get("name", "Untitled Role"),
                "title": j.get("name", "Untitled Role"),
                "locations": [_clean_loc(l) for l in locs if l][:3] or ["United States"],
                "team": j.get("department") or j.get("business_unit") or "",
                "salary": None,
                "url": j.get("canonicalPositionUrl") or f"{BASE}/careers/job/{j.get('id')}",
            })
        if fresh_in_page == 0:   # sorted newest-first
            break
        time.sleep(0.4)
    log.info("Netflix: %d fresh jobs", len(jobs))
    return jobs


def fetch_detail(job_id):
    """Full JD lives only on the detail endpoint; fetch once, cache."""
    jid = str(job_id)
    if jid in _DETAILS:
        return _DETAILS[jid]
    try:
        time.sleep(0.6)
        r = requests.get(f"{SEARCH_URL}/{jid}", params={"domain": "netflix.com"},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        d = r.json()
        text = re.sub(r"<[^>]+>", " ", html.unescape(d.get("job_description") or ""))
        text = re.sub(r"\s+", " ", text).strip()
        m = PAY_RE.search(text)
        salary = m.group(1).strip() if m else None
        snippet = text[:220].rsplit(" ", 1)[0] + "…" if len(text) > 220 else text
        det = {"salary": salary, "snippet": snippet,
               "level": d.get("department") or "", "emp_type": "",
               "url": f"{BASE}/careers/job/{jid}"}
    except Exception as e:
        log.warning("Netflix detail failed for %s: %s", jid, e)
        det = {"salary": None, "snippet": "", "level": "", "emp_type": "",
               "url": f"{BASE}/careers/job/{jid}"}
    _DETAILS[jid] = det
    return det


def sort_software_first(jobs):
    def key(j):
        t = (j.get("title") or j.get("name") or "").lower()
        if "software engineer" in t:
            return 0
        if "engineer" in t or "developer" in t or "scientist" in t:
            return 1
        return 2
    return sorted(jobs, key=key)


def get_jobs(cutoff=None):
    if cutoff is None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    return sort_software_first(fetch_recent_jobs(cutoff))


def build_post(jobs, date_str, part=None, total_parts=None):
    header = f"\U0001F3AC Netflix is Hiring! | {date_str}"
    if total_parts and total_parts > 1 and len(jobs) > 1:
        header += f" (Part {part}/{total_parts})"
    lines = [header, "", "Fresh roles posted in the last 24 hours \U0001F447", ""]
    for j in jobs:
        d = j.get("_detail") or fetch_detail(j.get("id"))
        lines.append(f"\U0001F4BC {j.get('title', 'Untitled Role')}")
        lines.append(f"\U0001F4CD {'; '.join(j.get('locations', ['United States'])[:2])}")
        if j.get("team"):
            lines.append(f"\U0001F3AF {j['team']}")
        if d.get("salary"):
            lines.append(f"\U0001F4B0 {d['salary']}")
        if d.get("snippet"):
            lines.append(f"\U0001F4DD {d['snippet']}")
        lines.append(f"\U0001F517 {d.get('url') or j.get('url')}")
        lines.append("")
    lines += ["♻️ Repost to help someone in your network!",
              "\U0001F514 Follow for daily Netflix job updates.", "",
              "#NetflixCareers #Hiring #TechJobs #SoftwareEngineering #JobSearch #NowHiring"]
    return "\n".join(lines)


def render_posts(jobs, date_str=None):
    date_str = date_str or datetime.now().strftime("%B %d, %Y")
    chunks = [jobs[i:i + JOBS_PER_POST] for i in range(0, len(jobs), JOBS_PER_POST)]
    total = len(chunks)
    posts = [build_post(c, date_str, part=i + 1, total_parts=total) for i, c in enumerate(chunks)]
    divider = "\n\n" + "=" * 12 + "  ✂️ COPY NEXT POST SEPARATELY  " + "=" * 12 + "\n\n"
    return divider.join(posts)
