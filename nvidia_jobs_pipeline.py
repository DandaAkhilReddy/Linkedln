"""
NVIDIA Careers -> LinkedIn Post Pipeline (Workday CXS API, Aug 2026)
POST nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs
US filter via locationHierarchy1 facet GUID. postedOn is textual
("Posted Today" / "Posted Yesterday" / "Posted N Days Ago") — parsed to dates;
id-dedupe handles same-day repeats.

Mirrors the shared pipeline interface.
"""

import os
import re
import time
import html
import logging
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("nvidia-jobs")

BASE = "https://nvidia.wd5.myworkdayjobs.com"
CXS = BASE + "/wday/cxs/nvidia/NVIDIAExternalCareerSite"
US_LOCATION_GUID = os.getenv("NVIDIA_US_GUID", "2fcb99c455831013ea52fb338f2932d8")

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))
MAX_PAGES = int(os.getenv("NVIDIA_MAX_PAGES", "10"))         # 20 jobs/page
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))
DETAIL_DELAY_S = float(os.getenv("DETAIL_DELAY_S", "1.2"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

PAY_RANGE_RE = re.compile(
    r"([\d,]+\s*USD\s*(?:-|–|to)\s*[\d,]+\s*USD|\$[\d,]+(?:\.\d+)?\s*(?:-|–|to)\s*\$?[\d,]+(?:\.\d+)?)",
    re.IGNORECASE)


def _post(url, body, retries=2):
    for attempt in range(retries + 1):
        r = requests.post(url, json=body,
                          headers={"User-Agent": UA, "Content-Type": "application/json"},
                          timeout=30)
        if r.status_code == 429 and attempt < retries:
            time.sleep(15 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def _posted_date(text):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' -> UTC date."""
    now = datetime.now(timezone.utc)
    t = (text or "").lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    return now - timedelta(days=365)  # "30+ days" fallback / unknown: treat as old


def fetch_recent_jobs(cutoff):
    jobs = []
    for page in range(MAX_PAGES):
        d = _post(f"{CXS}/jobs",
                  {"limit": 20, "offset": page * 20, "searchText": "",
                   "appliedFacets": {"locationHierarchy1": [US_LOCATION_GUID]}})
        batch = d.get("jobPostings") or []
        if not batch:
            break
        fresh_in_page = 0
        for j in batch:
            posted = _posted_date(j.get("postedOn"))
            if posted.date() >= cutoff.date():
                j["id"] = (j.get("bulletFields") or [j.get("externalPath")])[0]
                jobs.append(j)
                fresh_in_page += 1
        if fresh_in_page == 0:
            break
        time.sleep(0.5)
    log.info("NVIDIA: %d jobs within lookback window", len(jobs))
    return jobs


def sort_software_first(jobs):
    def key(j):
        t = (j.get("title") or j.get("name") or "").lower()
        if "software" in t:
            return 0
        if "engineer" in t or "developer" in t or "scientist" in t or "architect" in t:
            return 1
        return 2
    return sorted(jobs, key=key)


def fetch_detail(external_path):
    """Workday job detail: description HTML with salary for US roles."""
    try:
        time.sleep(DETAIL_DELAY_S)
        r = requests.get(f"{CXS}/job{external_path}" if not str(external_path).startswith("/job")
                         else f"{CXS}{external_path}",
                         headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        info = r.json().get("jobPostingInfo") or {}
        text = html.unescape(re.sub(r"<[^>]+>", " ", info.get("jobDescription", "") or ""))
        text = re.sub(r"\s+", " ", text).strip()
        m = PAY_RANGE_RE.search(text)
        salary = None
        if m:
            s = m.group(1)
            salary = s if "USD" in s.upper() else "USD " + s
        snippet = text[:220].rsplit(" ", 1)[0] + "…" if len(text) > 220 else text
        return {"salary": salary, "snippet": snippet,
                "url": info.get("externalUrl") or BASE}
    except Exception as e:
        log.warning("NVIDIA detail failed for %s: %s", external_path, e)
        return {"salary": None, "snippet": "", "url": BASE}


def build_post(jobs, date_str, part=None, total_parts=None):
    header = f"\U0001F49A NVIDIA is Hiring! | {date_str}"
    if total_parts and total_parts > 1 and len(jobs) > 1:
        header += f" (Part {part}/{total_parts})"
    lines = [header, "", "Fresh roles posted in the last 24 hours \U0001F447", ""]

    for j in jobs:
        d = j.get("_detail") or fetch_detail(j.get("externalPath", ""))
        lines.append(f"\U0001F4BC {j.get('title', 'Untitled Role')}")
        lines.append(f"\U0001F4CD {j.get('locationsText') or 'United States'}")
        if d.get("salary"):
            lines.append(f"\U0001F4B0 {d['salary']}")
        if d.get("snippet"):
            lines.append(f"\U0001F4DD {d['snippet']}")
        lines.append(f"\U0001F517 {d.get('url', BASE)}")
        lines.append("")

    lines += [
        "♻️ Repost to help someone in your network!",
        "\U0001F514 Follow for daily NVIDIA job updates.",
        "",
        "#NVIDIACareers #Hiring #TechJobs #SoftwareEngineering #JobSearch #NowHiring",
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
