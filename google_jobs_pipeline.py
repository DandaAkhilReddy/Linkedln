"""
Google Careers -> LinkedIn Post Pipeline (google.com/about/careers, Aug 2026)
Google renders job data server-side into AF_initDataCallback blobs on
/about/careers/applications/jobs/results (20 jobs/page, sort_by=date).
Salary lives only on the per-job detail page (regex-extracted).

Mirrors the ms/apple pipeline interface: get_jobs(cutoff), render_posts(jobs),
sort_software_first(jobs), fetch_detail(id).
"""

import os
import re
import json
import time
import html
import logging
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("google-jobs")

BASE = "https://www.google.com/about/careers/applications"
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
MAX_PAGES = int(os.getenv("GOOGLE_MAX_PAGES", "10"))         # 20 jobs/page
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))
FILTER_LOCATION = os.getenv("GOOGLE_FILTER_LOCATION", "United States")
DETAIL_DELAY_S = float(os.getenv("DETAIL_DELAY_S", "1.2"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

PAY_RANGE_RE = re.compile(r"(\$[\d,]{4,}(?:\.\d+)?\s*[-–—]\s*\$[\d,]{4,}(?:\.\d+)?(?:\s*\(USD\))?)")
_AF_RE = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.DOTALL)
_DATA_RE = re.compile(r"data:(\[.*\])(?:, sideChannel|\})", re.DOTALL)


def _get(url, retries=2):
    for attempt in range(retries + 1):
        r = requests.get(url, headers={"User-Agent": UA}, timeout=45)
        if r.status_code == 429 and attempt < retries:
            time.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.text
    raise RuntimeError("unreachable")


def _parse_jobs_page(page_html):
    """Extract normalized job dicts from the AF_initDataCallback data blob."""
    jobs = []
    for block in _AF_RE.findall(page_html):
        m = _DATA_RE.search(block)
        if not m or len(m.group(1)) < 5000:
            continue
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        entries = data[0] if data and isinstance(data[0], list) else []
        for e in entries:
            if not (isinstance(e, list) and len(e) > 14 and isinstance(e[1], str)):
                continue
            try:
                posted_ts = (e[12] or [0])[0]
                locs = [l[0] for l in (e[9] or []) if isinstance(l, list) and l and l[0]]
                desc = ""
                if isinstance(e[10], list) and len(e[10]) > 1 and e[10][1]:
                    desc = e[10][1]
                jobs.append({
                    "id": str(e[0]),
                    "title": e[1],
                    "posted_ts": posted_ts,
                    "locations": locs,
                    "description_html": desc,
                    "url": f"{BASE}/jobs/results/{e[0]}",
                })
            except (IndexError, TypeError):
                continue
    return jobs


def fetch_recent_jobs(cutoff):
    """Paginate newest-first; stop when a whole page is older than cutoff."""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        url = (f"{BASE}/jobs/results?location={requests.utils.quote(FILTER_LOCATION)}"
               f"&sort_by=date&page={page}")
        batch = _parse_jobs_page(_get(url))
        if not batch:
            break
        fresh_in_page = 0
        for j in batch:
            posted = datetime.fromtimestamp(j["posted_ts"], tz=timezone.utc)
            if posted >= cutoff:
                jobs.append(j)
                fresh_in_page += 1
        if fresh_in_page == 0:
            break
        time.sleep(0.5)
    log.info("Google: %d jobs within lookback window", len(jobs))
    return jobs


def sort_software_first(jobs):
    def key(j):
        t = (j.get("title") or j.get("name") or "").lower()
        if "software engineer" in t or "software developer" in t:
            return 0
        if "engineer" in t or "developer" in t or "scientist" in t or "machine learning" in t:
            return 1
        return 2
    return sorted(jobs, key=key)


def fetch_detail(job_id):
    """Detail page carries the salary range; regex it out of the raw HTML."""
    try:
        time.sleep(DETAIL_DELAY_S)
        page = _get(f"{BASE}/jobs/results/{job_id}")
        m = PAY_RANGE_RE.search(page)
        salary = None
        if m:
            salary = "USD " + m.group(1).replace("(USD)", "").strip()
        return {"salary": salary}
    except Exception as e:
        log.warning("Google detail failed for %s: %s", job_id, e)
        return {"salary": None}


def _snippet(desc_html):
    text = html.unescape(re.sub(r"<[^>]+>", " ", desc_html or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:220].rsplit(" ", 1)[0] + "…" if len(text) > 220 else text


def build_post(jobs, date_str, part=None, total_parts=None):
    header = f"\U0001F50D Google is Hiring! | {date_str}"
    if total_parts and total_parts > 1 and len(jobs) > 1:
        header += f" (Part {part}/{total_parts})"
    lines = [header, "", "Fresh roles posted in the last 24 hours \U0001F447", ""]

    for j in jobs:
        d = j.get("_detail") or fetch_detail(j.get("id"))
        lines.append(f"\U0001F4BC {j.get('title', 'Untitled Role')}")
        lines.append(f"\U0001F4CD {'; '.join(j.get('locations', [])[:2]) or 'United States'}")
        if d.get("salary"):
            lines.append(f"\U0001F4B0 {d['salary']}")
        snip = _snippet(j.get("description_html"))
        if snip:
            lines.append(f"\U0001F4DD {snip}")
        lines.append(f"\U0001F517 {j.get('url')}")
        lines.append("")

    lines += [
        "♻️ Repost to help someone in your network!",
        "\U0001F514 Follow for daily Google job updates.",
        "",
        "#GoogleCareers #Hiring #TechJobs #SoftwareEngineering #JobSearch #NowHiring",
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
