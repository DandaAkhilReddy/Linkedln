"""
OpenAI Careers -> LinkedIn Post Pipeline (Ashby job board API).
Single unauthenticated call returns every listed role with compensation,
description, team, and publish date — no per-job detail fetches needed.
"""

import os
import re
import logging
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("openai-jobs")

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true"

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Accept": "application/json"}

_DETAILS = {}  # id -> detail dict, filled by get_jobs


def _parse_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def fetch_recent_jobs(cutoff):
    r = requests.get(BOARD_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    raw = r.json().get("jobs") or []
    jobs = []
    for j in raw:
        if not j.get("isListed", True):
            continue
        if _parse_ts(j.get("publishedAt")) < cutoff:
            continue
        locs = [j.get("location") or "San Francisco"]
        locs += [s.get("location") for s in (j.get("secondaryLocations") or []) if s.get("location")]
        comp = j.get("compensation") or {}
        salary = comp.get("scrapeableCompensationSalarySummary")
        desc = re.sub(r"\s+", " ", j.get("descriptionPlain") or "").strip()
        snippet = desc[:220].rsplit(" ", 1)[0] + "…" if len(desc) > 220 else desc
        job = {
            "id": str(j.get("id")),
            "name": j.get("title", "Untitled Role"),
            "title": j.get("title", "Untitled Role"),
            "locations": locs,
            "team": j.get("team") or j.get("department") or "",
            "salary": salary,
            "url": j.get("jobUrl") or "https://openai.com/careers",
        }
        _DETAILS[job["id"]] = {"salary": salary, "snippet": snippet,
                               "level": j.get("employmentType") or "",
                               "emp_type": "", "url": job["url"]}
        jobs.append(job)
    log.info("OpenAI: %d fresh jobs", len(jobs))
    return jobs


def fetch_detail(job_id):
    return _DETAILS.get(str(job_id)) or {"salary": None, "snippet": "", "level": "",
                                         "emp_type": "", "url": "https://openai.com/careers"}


def sort_software_first(jobs):
    def key(j):
        t = (j.get("title") or j.get("name") or "").lower()
        if "software engineer" in t or "research engineer" in t:
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
    header = f"\U0001F916 OpenAI is Hiring! | {date_str}"
    if total_parts and total_parts > 1 and len(jobs) > 1:
        header += f" (Part {part}/{total_parts})"
    lines = [header, "", "Fresh roles posted in the last 24 hours \U0001F447", ""]
    for j in jobs:
        d = j.get("_detail") or fetch_detail(j.get("id"))
        lines.append(f"\U0001F4BC {j.get('title', 'Untitled Role')}")
        lines.append(f"\U0001F4CD {'; '.join(j.get('locations', ['San Francisco'])[:2])}")
        if j.get("team"):
            lines.append(f"\U0001F3AF {j['team']}")
        if d.get("salary"):
            lines.append(f"\U0001F4B0 {d['salary']}")
        if d.get("snippet"):
            lines.append(f"\U0001F4DD {d['snippet']}")
        lines.append(f"\U0001F517 {d.get('url') or j.get('url')}")
        lines.append("")
    lines += ["♻️ Repost to help someone in your network!",
              "\U0001F514 Follow for daily OpenAI job updates.", "",
              "#OpenAICareers #AI #Hiring #TechJobs #SoftwareEngineering #NowHiring"]
    return "\n".join(lines)


def render_posts(jobs, date_str=None):
    date_str = date_str or datetime.now().strftime("%B %d, %Y")
    chunks = [jobs[i:i + JOBS_PER_POST] for i in range(0, len(jobs), JOBS_PER_POST)]
    total = len(chunks)
    posts = [build_post(c, date_str, part=i + 1, total_parts=total) for i, c in enumerate(chunks)]
    divider = "\n\n" + "=" * 12 + "  ✂️ COPY NEXT POST SEPARATELY  " + "=" * 12 + "\n\n"
    return divider.join(posts)
