"""
Apple Careers -> LinkedIn Post Pipeline (jobs.apple.com API, July 2026)
Uses the same public API the jobs.apple.com search page calls:
  GET  /api/v1/CSRFToken            -> X-Apple-CSRF-Token header + cookies
  POST /api/v1/search               -> {"filters":{"locations":["postLocation-USA"]},...}
  GET  /api/v1/jobDetails/{id}      -> description / pay (id like "PIPE-<positionId>")

Mirrors ms_jobs_pipeline's interface: get_jobs(cutoff), render_posts(jobs),
sort_software_first(jobs), fetch_detail(id).
"""

import os
import re
import time
import html
import logging
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("apple-jobs")

BASE = "https://jobs.apple.com"
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
MAX_PAGES = int(os.getenv("APPLE_MAX_PAGES", "15"))          # 20 results/page
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))
FILTER_LOCATION = os.getenv("APPLE_FILTER_LOCATION", "postLocation-USA")
DETAIL_DELAY_S = float(os.getenv("DETAIL_DELAY_S", "1.2"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

PAY_RANGE_RE = re.compile(
    r"(\$\s?[\d,]+(?:\.\d+)?\s*(?:and|-|–|—|to)\s*\$\s?[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

_session = None
_csrf = None


def _get_session():
    """Session with cookies + CSRF token (Apple requires both on every call)."""
    global _session, _csrf
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": UA,
                                 "Referer": f"{BASE}/en-us/search"})
        r = _session.get(f"{BASE}/api/v1/CSRFToken", timeout=30)
        _csrf = r.headers.get("X-Apple-CSRF-Token", "")
        _session.headers["X-Apple-CSRF-Token"] = _csrf
    return _session


def _reset_session():
    global _session
    _session = None


def _post(url, body, retries=2):
    for attempt in range(retries + 1):
        s = _get_session()
        r = s.post(url, json=body, timeout=30)
        if r.status_code in (429, 436, 403) and attempt < retries:
            log.warning("Apple API %s; refreshing session", r.status_code)
            _reset_session()
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def fetch_recent_jobs(cutoff):
    """Paginate newest-first; stop when a whole page is older than cutoff."""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        body = {"query": "", "filters": {"locations": [FILTER_LOCATION]},
                "page": page, "locale": "en-us", "sort": "newest",
                # Apple's API returns 0 results if this format block is missing
                "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}}
        res = _post(f"{BASE}/api/v1/search", body).get("res") or {}
        batch = res.get("searchResults") or []
        if not batch:
            break
        fresh_in_page = 0
        for j in batch:
            raw = j.get("postDateInGMT") or ""
            # Apple uses nanosecond precision; trim to microseconds for fromisoformat
            raw = re.sub(r"\.(\d{6})\d*", r".\1", raw)
            try:
                posted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if posted >= cutoff:
                jobs.append(j)
                fresh_in_page += 1
        if fresh_in_page == 0:
            break
        time.sleep(0.5)
    log.info("Apple: %d jobs within lookback window", len(jobs))
    return jobs


def sort_software_first(jobs):
    def key(j):
        t = (j.get("postingTitle") or j.get("name") or "").lower()
        if "software" in t or "swe" in t:
            return 0
        if "engineer" in t or "developer" in t or "scientist" in t or "machine learning" in t:
            return 1
        return 2
    return sorted(jobs, key=key)


def fetch_detail(job_id):
    """job_id is the 'id' field, e.g. PIPE-200663414."""
    try:
        time.sleep(DETAIL_DELAY_S)
        s = _get_session()
        r = s.get(f"{BASE}/api/v1/jobDetails/{job_id}?languageCd=en-us", timeout=30)
        if r.status_code in (429, 436, 403):
            _reset_session()
            s = _get_session()
            r = s.get(f"{BASE}/api/v1/jobDetails/{job_id}?languageCd=en-us", timeout=30)
        r.raise_for_status()
        res = r.json().get("res") or {}
        blob = " ".join(str(res.get(k) or "") for k in
                        ("jobSummary", "description", "payAndBenefits"))
        text = html.unescape(re.sub(r"<[^>]+>", " ", blob))
        text = re.sub(r"\s+", " ", text).strip()
        m = PAY_RANGE_RE.search(str(res.get("payAndBenefits") or "") + " " + text)
        salary = ("USD " + m.group(1).strip()) if m else None
        snippet = text[:220].rsplit(" ", 1)[0] + "…" if len(text) > 220 else text
        return {"salary": salary, "snippet": snippet,
                "team": (res.get("team") or {}).get("teamName", "")}
    except Exception as e:
        log.warning("Apple detail failed for %s: %s", job_id, e)
        return {"salary": None, "snippet": "", "team": ""}


def _job_url(j):
    return f"{BASE}/en-us/details/{j.get('positionId')}/{j.get('transformedPostingTitle') or ''}"


def build_post(jobs, date_str, part=None, total_parts=None):
    header = f"\U0001F34F Apple is Hiring! | {date_str}"
    if total_parts and total_parts > 1 and len(jobs) > 1:
        header += f" (Part {part}/{total_parts})"
    lines = [header, "", "Fresh roles posted in the last 24 hours \U0001F447", ""]

    for j in jobs:
        d = j.get("_detail") or fetch_detail(j.get("id"))
        locs = j.get("locations") or []
        loc_names = [", ".join(x for x in (l.get("city"), l.get("stateProvince"),
                                           l.get("countryName")) if x) or "United States"
                     for l in locs[:2]] or ["United States"]
        team = (j.get("team") or {}).get("teamName", "")

        lines.append(f"\U0001F4BC {j.get('postingTitle', 'Untitled Role')}")
        lines.append(f"\U0001F4CD {'; '.join(loc_names)}")
        if team:
            lines.append(f"\U0001F3AF Team: {team}")
        if d["salary"]:
            lines.append(f"\U0001F4B0 {d['salary']}")
        if d["snippet"]:
            lines.append(f"\U0001F4DD {d['snippet']}")
        lines.append(f"\U0001F517 {_job_url(j)}")
        lines.append("")

    lines += [
        "♻️ Repost to help someone in your network!",
        "\U0001F514 Follow for daily Apple job updates.",
        "",
        "#AppleCareers #Hiring #TechJobs #SoftwareEngineering #JobSearch #NowHiring",
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
