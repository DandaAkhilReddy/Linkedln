"""
Microsoft Careers -> LinkedIn Post Pipeline (Eightfold API edition, July 2026)
Microsoft moved careers to Eightfold (apply.careers.microsoft.com); the old
gcsservices.careers.microsoft.com API is dead. This uses the same public,
unauthenticated JSON API the careers site itself calls.

Generates copy-paste-ready LinkedIn posts (up to MAX_JOBS_TOTAL jobs, split
into chunks of JOBS_PER_POST). Writes to OUTPUT_FILE when there are new jobs.
"""

import os
import re
import time
import html
import logging
import requests
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ms-jobs")

# ---------------- CONFIG (override via env vars) ----------------
BASE = "https://apply.careers.microsoft.com"
SEARCH_URL = BASE + "/api/pcsx/search"
DETAIL_URL = BASE + "/api/pcsx/position_details"
DOMAIN = "microsoft.com"

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
MAX_JOBS_TOTAL = int(os.getenv("MAX_JOBS_TOTAL", "30"))     # jobs covered per day
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))       # LinkedIn ~3000 char limit
PAGE_SIZE = 10                                              # API returns 10 per page
MAX_PAGES = 25

FILTER_LOCATION = os.getenv("FILTER_COUNTRY", "United States")  # "" for worldwide
FILTER_TITLE_KEYWORDS = [k.strip().lower() for k in os.getenv("FILTER_TITLE_KEYWORDS", "").split(",") if k.strip()]

OUTPUT_FILE = os.getenv("OUTPUT_FILE", "post.txt")
DETAIL_DELAY_S = float(os.getenv("DETAIL_DELAY_S", "1.2"))  # be polite; avoid 429s

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
}

PAY_RANGE_RE = re.compile(
    r"(USD\s*\$?[\d,]+(?:\.\d+)?\s*[-–—]\s*\$?[\d,]+(?:\.\d+)?(?:\s*per\s*year)?)",
    re.IGNORECASE,
)


def _get(url, params, retries=2):
    for attempt in range(retries + 1):
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        if r.status_code == 429 and attempt < retries:
            wait = 15 * (attempt + 1)
            log.warning("429 rate-limited; sleeping %ss", wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def fetch_recent_jobs(cutoff):
    """Paginate the search API (newest first); stop once older than cutoff."""
    jobs = []
    for page in range(MAX_PAGES):
        params = {
            "domain": DOMAIN,
            "query": "",
            "location": FILTER_LOCATION,
            "start": page * PAGE_SIZE,
            "sort_by": "timestamp",
        }
        data = _get(SEARCH_URL, params).get("data") or {}
        batch = data.get("positions") or []
        if not batch:
            break
        fresh_in_page = 0
        for j in batch:
            posted = datetime.fromtimestamp(j.get("postedTs") or 0, tz=timezone.utc)
            if posted >= cutoff:
                jobs.append(j)
                fresh_in_page += 1
        if fresh_in_page == 0:   # sorted newest-first; everything after is older
            break
        time.sleep(0.5)
    log.info("Fetched %d jobs within lookback window", len(jobs))
    return jobs


def filter_title_keywords(jobs):
    if not FILTER_TITLE_KEYWORDS:
        return jobs
    kept = [j for j in jobs if any(k in j.get("name", "").lower() for k in FILTER_TITLE_KEYWORDS)]
    log.info("%d jobs match title keywords %s", len(kept), FILTER_TITLE_KEYWORDS)
    return kept


def fetch_detail(position_id):
    """Pull pay range, snippet, level, employment type from the detail endpoint."""
    try:
        time.sleep(DETAIL_DELAY_S)
        pos = _get(DETAIL_URL, {"domain": DOMAIN, "position_id": position_id}).get("data") or {}
        text = html.unescape(re.sub(r"<[^>]+>", " ", pos.get("jobDescription", "") or ""))
        text = re.sub(r"\s+", " ", text).strip()

        m = PAY_RANGE_RE.search(text)
        salary = m.group(1).strip() if m else None
        snippet = text[:220].rsplit(" ", 1)[0] + "…" if len(text) > 220 else text

        def first(v):
            return v[0] if isinstance(v, list) and v else (v or "")

        return {
            "salary": salary,
            "snippet": snippet,
            "level": first(pos.get("efcustomTextRoletype")),
            "emp_type": first(pos.get("efcustomTextEmploymentType")),
            "url": pos.get("publicUrl") or f"{BASE}/careers/job/{position_id}",
        }
    except Exception as e:
        log.warning("Detail fetch failed for %s: %s", position_id, e)
        return {"salary": None, "snippet": "", "level": "", "emp_type": "",
                "url": f"{BASE}/careers/job/{position_id}"}


def build_post(jobs, date_str, part=None, total_parts=None):
    header = f"\U0001F680 Microsoft is Hiring! | {date_str}"
    # Part numbering only for multi-job posts; single-job posts stay clean
    if total_parts and total_parts > 1 and len(jobs) > 1:
        header += f" (Part {part}/{total_parts})"
    lines = [header, "", "Fresh roles posted in the last 24 hours \U0001F447", ""]

    for j in jobs:
        d = fetch_detail(j.get("id"))
        location = ", ".join((j.get("locations") or ["Multiple Locations"])[:2])

        lines.append(f"\U0001F4BC {j.get('name', 'Untitled Role')}")
        lines.append(f"\U0001F4CD {location}")
        if d["level"]:
            lines.append(f"\U0001F3AF Level: {d['level']}" + (f" | {d['emp_type']}" if d["emp_type"] else ""))
        if d["salary"]:
            lines.append(f"\U0001F4B0 {d['salary']}")
        if d["snippet"]:
            lines.append(f"\U0001F4DD {d['snippet']}")
        lines.append(f"\U0001F517 {d['url']}")
        lines.append("")

    lines += [
        "♻️ Repost to help someone in your network!",
        "\U0001F514 Follow for daily Microsoft job updates.",
        "",
        "#MicrosoftCareers #Hiring #TechJobs #SoftwareEngineering #JobSearch #NowHiring",
    ]
    return "\n".join(lines)


def run():
    date_str = datetime.now().strftime("%B %d, %Y")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    jobs = filter_title_keywords(fetch_recent_jobs(cutoff))[:MAX_JOBS_TOTAL]
    if not jobs:
        log.info("No new jobs in window — nothing to post today.")
        return None

    chunks = [jobs[i:i + JOBS_PER_POST] for i in range(0, len(jobs), JOBS_PER_POST)]
    total = len(chunks)
    posts = [build_post(c, date_str, part=i + 1, total_parts=total) for i, c in enumerate(chunks)]

    divider = "\n\n" + "=" * 12 + "  ✂️ COPY NEXT POST SEPARATELY  " + "=" * 12 + "\n\n"
    output = divider.join(posts)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(output)
    log.info("%d jobs → %d post(s) written to %s", len(jobs), total, OUTPUT_FILE)
    print("\n" + output)
    return output


if __name__ == "__main__":
    run()
