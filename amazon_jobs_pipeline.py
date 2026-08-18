"""
Amazon Careers -> LinkedIn Post Pipeline (amazon.jobs JSON API, Aug 2026)
GET https://www.amazon.jobs/en/search.json?sort=recent&country=USA — clean
public JSON, day-granular posted_date (id-dedupe handles same-day repeats).

Mirrors the shared pipeline interface.
"""

import os
import re
import time
import html
import logging
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("amazon-jobs")

BASE = "https://www.amazon.jobs"
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
MAX_PAGES = int(os.getenv("AMAZON_MAX_PAGES", "6"))          # 50 jobs/page
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))
FILTER_COUNTRY = os.getenv("AMAZON_FILTER_COUNTRY", "USA")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

PAY_RANGE_RE = re.compile(
    r"(\$[\d,]+(?:\.\d+)?\s*/?(?:year|yr|hr|hour)?\s*(?:-|–|to|up to)\s*\$[\d,]+(?:\.\d+)?)",
    re.IGNORECASE)


def _get(url, params, retries=2):
    for attempt in range(retries + 1):
        r = requests.get(url, params=params, headers={"User-Agent": UA, "Accept": "application/json"},
                         timeout=30)
        if r.status_code == 429 and attempt < retries:
            time.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def fetch_recent_jobs(cutoff):
    """Paginate newest-first; stop when a whole page predates the cutoff date."""
    jobs = []
    for page in range(MAX_PAGES):
        d = _get(f"{BASE}/en/search.json",
                 {"sort": "recent", "result_limit": 50, "offset": page * 50,
                  "country": FILTER_COUNTRY})
        batch = d.get("jobs") or []
        if not batch:
            break
        fresh_in_page = 0
        for j in batch:
            try:  # "August 17, 2026" — day granularity
                posted = datetime.strptime(j.get("posted_date", ""), "%B %d, %Y").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            if posted.date() >= cutoff.date():
                j["id"] = j.get("id_icims") or j.get("id")
                jobs.append(j)
                fresh_in_page += 1
        if fresh_in_page == 0:
            break
        time.sleep(0.5)
    log.info("Amazon: %d jobs within lookback window", len(jobs))
    return jobs


def sort_software_first(jobs):
    def key(j):
        t = (j.get("title") or j.get("name") or "").lower()
        if "software" in t and ("engineer" in t or "developer" in t or "development" in t):
            return 0
        if "engineer" in t or "developer" in t or "scientist" in t:
            return 1
        return 2
    return sorted(jobs, key=key)


def fetch_detail(job_id):
    """Amazon's search.json already includes descriptions; nothing extra to fetch."""
    return {}


def _snippet(j):
    text = html.unescape(re.sub(r"<[^>]+>", " ",
                                j.get("description_short") or j.get("description") or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:220].rsplit(" ", 1)[0] + "…" if len(text) > 220 else text


def _salary(j):
    if j.get("salary_range"):
        return str(j["salary_range"])
    m = PAY_RANGE_RE.search(j.get("description") or "")
    return ("USD " + m.group(1)) if m else None


def build_post(jobs, date_str, part=None, total_parts=None):
    header = f"\U0001F4E6 Amazon is Hiring! | {date_str}"
    if total_parts and total_parts > 1 and len(jobs) > 1:
        header += f" (Part {part}/{total_parts})"
    lines = [header, "", "Fresh roles posted in the last 24 hours \U0001F447", ""]

    for j in jobs:
        lines.append(f"\U0001F4BC {j.get('title', 'Untitled Role')}")
        lines.append(f"\U0001F4CD {j.get('location') or 'United States'}")
        if j.get("job_category"):
            lines.append(f"\U0001F3AF {j['job_category']}")
        sal = _salary(j)
        if sal:
            lines.append(f"\U0001F4B0 {sal}")
        snip = _snippet(j)
        if snip:
            lines.append(f"\U0001F4DD {snip}")
        lines.append(f"\U0001F517 {BASE}{j.get('job_path', '')}")
        lines.append("")

    lines += [
        "♻️ Repost to help someone in your network!",
        "\U0001F514 Follow for daily Amazon job updates.",
        "",
        "#AmazonJobs #Hiring #TechJobs #SoftwareEngineering #JobSearch #NowHiring",
    ]
    return "\n".join(lines)


def get_jobs(cutoff=None):
    if cutoff is None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    return sort_software_first(fetch_recent_jobs(cutoff))


def render_posts(jobs, date_str=None):
    date_str = date_str or datetime.now().strftime("%B %d, %Y")
    chunks = [jobs[i:i + JOBS_PER_POST] for i in range(0, len(jobs), JOBS_PER_POST)]
    total = len(chunks)
    posts = [build_post(c, date_str, part=i + 1, total_parts=total)
             for i, c in enumerate(chunks)]
    divider = "\n\n" + "=" * 12 + "  ✂️ COPY NEXT POST SEPARATELY  " + "=" * 12 + "\n\n"
    return divider.join(posts)
