"""
xAI Careers -> LinkedIn Post Pipeline (Greenhouse job board API).
One call with ?content=true returns every job with description HTML —
salary parsed from the pay-transparency range in the text.
"""

import os
import re
import html
import logging
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("xai-jobs")

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/xai/jobs?content=true"
FALLBACK_URL = "https://x.ai/careers"

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Accept": "application/json"}

PAY_RE = re.compile(
    r"(\$\s?[\d][\d,]*(?:\.\d+)?\s*[Kk]?\s*(?:[-–—]|to)\s*\$?\s?[\d][\d,]*(?:\.\d+)?\s*[Kk]?(?:\s*USD)?)")

_DETAILS = {}  # id -> detail dict, filled by get_jobs


def _parse_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def fetch_recent_jobs(cutoff):
    r = requests.get(BOARD_URL, headers=HEADERS, timeout=45)
    r.raise_for_status()
    raw = r.json().get("jobs") or []
    jobs = []
    for j in raw:
        if _parse_ts(j.get("updated_at")) < cutoff:
            continue
        text = html.unescape(j.get("content") or "")
        text = re.sub(r"<[^>]+>", " ", html.unescape(text))
        text = re.sub(r"\s+", " ", text).strip()
        m = PAY_RE.search(text)
        salary = m.group(1).strip() if m else None
        snippet = text[:220].rsplit(" ", 1)[0] + "…" if len(text) > 220 else text
        depts = j.get("departments") or []
        team = depts[0].get("name", "") if depts else ""
        job = {
            "id": str(j.get("id")),
            "name": j.get("title", "Untitled Role"),
            "title": j.get("title", "Untitled Role"),
            "locations": list(dict.fromkeys(p.split("|")[0].strip() for p in ((j.get("location") or {}).get("name") or "United States").split(";")))[:3],
            "team": team,
            "salary": salary,
            "url": j.get("absolute_url") or FALLBACK_URL,
        }
        _DETAILS[job["id"]] = {"salary": salary, "snippet": snippet, "level": team,
                               "emp_type": "", "url": job["url"]}
        jobs.append(job)
    log.info("xAI: %d fresh jobs", len(jobs))
    return jobs


def fetch_detail(job_id):
    return _DETAILS.get(str(job_id)) or {"salary": None, "snippet": "", "level": "",
                                         "emp_type": "", "url": FALLBACK_URL}


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
    header = f"✖️ xAI is Hiring! | {date_str}"
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
              "\U0001F514 Follow for daily xAI job updates.", "",
              "#xAICareers #AI #Hiring #TechJobs #SoftwareEngineering #NowHiring"]
    return "\n".join(lines)


def render_posts(jobs, date_str=None):
    date_str = date_str or datetime.now().strftime("%B %d, %Y")
    chunks = [jobs[i:i + JOBS_PER_POST] for i in range(0, len(jobs), JOBS_PER_POST)]
    total = len(chunks)
    posts = [build_post(c, date_str, part=i + 1, total_parts=total) for i, c in enumerate(chunks)]
    divider = "\n\n" + "=" * 12 + "  ✂️ COPY NEXT POST SEPARATELY  " + "=" * 12 + "\n\n"
    return divider.join(posts)
