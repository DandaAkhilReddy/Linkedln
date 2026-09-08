"""
Parametrized job pipelines for standard job boards, so adding a company is
one line instead of one file. Each factory returns an object exposing the
same interface as the per-company modules:
    get_jobs(cutoff), sort_software_first(jobs), fetch_detail(id),
    build_post(jobs, date_str, part, total_parts), render_posts(jobs)

Boards: Greenhouse (Databricks, Stripe, Scale AI), Ashby (Ramp, Cursor),
Jibe/iCIMS (AMD), IBM's careers search API.
"""

import re
import html
import time
import logging
import requests
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

log = logging.getLogger("board-pipelines")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Accept": "application/json"}
LOOKBACK_HOURS = 24
JOBS_PER_POST = 10

PAY_RE = re.compile(
    r"(\$\s?[\d][\d,]*(?:\.\d+)?\s*[Kk]?\s*(?:[-–—]|to)\s*\$?\s?[\d][\d,]*(?:\.\d+)?\s*[Kk]?(?:\s*USD)?)")


# ---------------- shared helpers ----------------

def _clean(html_s):
    t = re.sub(r"<[^>]+>", " ", html.unescape(html_s or ""))
    return re.sub(r"\s+", " ", t).strip()


def _snippet(text):
    return text[:220].rsplit(" ", 1)[0] + "…" if len(text) > 220 else text


def _parse_iso(s):
    s = (s or "").replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)      # +0000 -> +00:00 (py3.10)
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)


_US_HINTS = ("united states", ", us", "usa", "remote - us", "us remote", "san francisco",
             "new york", "seattle", "austin", "chicago", "boston", "denver", "los angeles",
             "mountain view", "palo alto", "redmond", "sunnyvale", "menlo park", "santa clara",
             "san jose", "washington", "atlanta", "dallas", "miami", "herndon", ", ca", ", ny",
             ", wa", ", tx", ", ma", ", co", ", ga", ", il", ", va", ", nc", ", fl", ", or", ", pa")


def _is_us(j):
    loc = " ".join(j.get("locations") or []).lower()
    return any(h in loc for h in _US_HINTS)


def _sort(jobs):
    """US roles first (salary transparency + audience), then software-first."""
    def key(j):
        t = (j.get("title") or j.get("name") or "").lower()
        cat = 0 if ("software engineer" in t or "research engineer" in t) else \
              1 if ("engineer" in t or "developer" in t or "scientist" in t) else 2
        return (0 if _is_us(j) else 1, cat)
    return sorted(jobs, key=key)


def _make(display, emoji, hashtags, fallback_url, fetch_recent, detail_lookup):
    """Assemble the standard pipeline interface around board-specific fetchers."""
    def get_jobs(cutoff=None):
        if cutoff is None:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        return _sort(fetch_recent(cutoff))

    def fetch_detail(job_id):
        return detail_lookup(str(job_id)) or {"salary": None, "snippet": "",
                                              "level": "", "emp_type": "",
                                              "url": fallback_url}

    def build_post(jobs, date_str, part=None, total_parts=None):
        header = f"{emoji} {display} is Hiring! | {date_str}"
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
                  f"\U0001F514 Follow for daily {display} job updates.", "", hashtags]
        return "\n".join(lines)

    def render_posts(jobs, date_str=None):
        date_str = date_str or datetime.now().strftime("%B %d, %Y")
        chunks = [jobs[i:i + JOBS_PER_POST] for i in range(0, len(jobs), JOBS_PER_POST)]
        posts = [build_post(c, date_str, part=i + 1, total_parts=len(chunks))
                 for i, c in enumerate(chunks)]
        divider = "\n\n" + "=" * 12 + "  ✂️ COPY NEXT POST SEPARATELY  " + "=" * 12 + "\n\n"
        return divider.join(posts)

    return SimpleNamespace(display=display, get_jobs=get_jobs,
                           sort_software_first=_sort, fetch_detail=fetch_detail,
                           build_post=build_post, render_posts=render_posts)


# ---------------- Greenhouse ----------------

def greenhouse(board, display, emoji, hashtags, fallback_url):
    details = {}
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"

    def fetch_recent(cutoff):
        r = requests.get(url, headers=UA, timeout=60)
        r.raise_for_status()
        jobs = []
        for j in r.json().get("jobs") or []:
            if _parse_iso(j.get("updated_at")) < cutoff:
                continue
            text = _clean(html.unescape(j.get("content") or ""))
            m = PAY_RE.search(text)
            salary = m.group(1).strip() if m else None
            depts = j.get("departments") or []
            team = depts[0].get("name", "") if depts else ""
            loc = (j.get("location") or {}).get("name") or "United States"
            locs = list(dict.fromkeys(p.split("|")[0].strip() for p in loc.split(";")))[:3]
            jid = str(j.get("id"))
            jobs.append({"id": jid, "name": j.get("title", "Untitled Role"),
                         "title": j.get("title", "Untitled Role"), "locations": locs,
                         "team": team, "salary": salary,
                         "url": j.get("absolute_url") or fallback_url})
            details[jid] = {"salary": salary, "snippet": _snippet(text), "level": team,
                            "emp_type": "", "url": j.get("absolute_url") or fallback_url}
        log.info("%s: %d fresh jobs", display, len(jobs))
        return jobs

    return _make(display, emoji, hashtags, fallback_url, fetch_recent, details.get)


# ---------------- Ashby ----------------

def ashby(board, display, emoji, hashtags, fallback_url, default_loc="United States"):
    details = {}
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"

    def fetch_recent(cutoff):
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        jobs = []
        for j in r.json().get("jobs") or []:
            if not j.get("isListed", True) or _parse_iso(j.get("publishedAt")) < cutoff:
                continue
            locs = [j.get("location") or default_loc]
            locs += [s.get("location") for s in (j.get("secondaryLocations") or []) if s.get("location")]
            comp = j.get("compensation") or {}
            salary = comp.get("scrapeableCompensationSalarySummary")
            desc = re.sub(r"\s+", " ", j.get("descriptionPlain") or "").strip()
            jid = str(j.get("id"))
            jobs.append({"id": jid, "name": j.get("title", "Untitled Role"),
                         "title": j.get("title", "Untitled Role"), "locations": locs,
                         "team": j.get("team") or j.get("department") or "",
                         "salary": salary, "url": j.get("jobUrl") or fallback_url})
            details[jid] = {"salary": salary, "snippet": _snippet(desc),
                            "level": j.get("employmentType") or "", "emp_type": "",
                            "url": j.get("jobUrl") or fallback_url}
        log.info("%s: %d fresh jobs", display, len(jobs))
        return jobs

    return _make(display, emoji, hashtags, fallback_url, fetch_recent, details.get)


# ---------------- AMD (Jibe / iCIMS careers site) ----------------

def amd():
    details = {}
    base = "https://careers.amd.com"

    def _money(tag):
        # "USD $179,900.00/Yr." -> "$179,900"
        m = re.search(r"\$\s?([\d,]+)", (tag or [""])[0] if isinstance(tag, list) else (tag or ""))
        return f"${m.group(1)}" if m else None

    def fetch_recent(cutoff):
        jobs = []
        for page in range(1, 8):
            r = requests.get(f"{base}/api/jobs",
                             params={"page": page, "sortBy": "posted_date", "descending": "true",
                                     "internal": "false", "country": "United States"},
                             headers=UA, timeout=30)
            r.raise_for_status()
            batch = r.json().get("jobs") or []
            if not batch:
                break
            fresh = 0
            for item in batch:
                j = item.get("data") or {}
                if _parse_iso(j.get("posted_date")) < cutoff:
                    continue
                fresh += 1
                lo, hi = _money(j.get("tags2")), _money(j.get("tags3"))
                salary = f"{lo} - {hi}" if lo and hi else None
                text = _clean(j.get("description") or "")
                if not salary:
                    m = PAY_RE.search(text)
                    salary = m.group(1).strip() if m else None
                jid = str(j.get("req_id") or j.get("slug"))
                loc = ", ".join(x for x in (j.get("city"), j.get("state")) if x) or "United States"
                cats = j.get("categories") or []
                team = cats[0].get("name", "") if cats else ""
                url = f"{base}/careers-home/jobs/{jid}"
                jobs.append({"id": jid, "name": j.get("title", "Untitled Role"),
                             "title": j.get("title", "Untitled Role"), "locations": [loc],
                             "team": team, "salary": salary, "url": url})
                details[jid] = {"salary": salary, "snippet": _snippet(text), "level": team,
                                "emp_type": j.get("employment_type") or "", "url": url}
            if fresh == 0:
                break
            time.sleep(0.4)
        log.info("AMD: %d fresh jobs", len(jobs))
        return jobs

    return _make("AMD", "\U0001F534", "#AMDCareers #Semiconductors #Hiring #TechJobs #NowHiring",
                 f"{base}/careers-home/jobs", fetch_recent, details.get)


# ---------------- IBM (careers search API) ----------------

def ibm():
    details = {}
    api = "https://www-api.ibm.com/search/api/v2"
    hdr = dict(UA, **{"Content-Type": "application/json", "Origin": "https://www.ibm.com",
                      "Referer": "https://www.ibm.com/careers/search"})

    def fetch_recent(cutoff):
        jobs = []
        for start in (0, 50, 100):
            body = {"appId": "careers", "scopes": ["careers2"],
                    "query": {"bool": {"must": [{"match": {"field_keyword_05": "United States"}}]}},
                    "size": 50, "from": start, "sort": [{"dcdate": "desc"}],
                    "sm": {"query": "", "lang": "en"},
                    "_source": ["title", "url", "description", "dcdate", "field_keyword_05",
                                "field_keyword_08", "field_keyword_18", "field_keyword_19"]}
            r = requests.post(api, headers=hdr, json=body, timeout=30)
            r.raise_for_status()
            hits = (r.json().get("hits") or {}).get("hits") or []
            if not hits:
                break
            fresh = 0
            for h in hits:
                s = h.get("_source") or {}
                try:
                    posted = datetime.strptime(s.get("dcdate", ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except Exception:
                    posted = datetime.fromtimestamp(0, tz=timezone.utc)
                # dcdate has day granularity: keep anything dated on/after the cutoff day
                if posted.date() < cutoff.date():
                    continue
                fresh += 1
                jid = str(h.get("_id"))
                text = _clean(s.get("description") or "")
                m = PAY_RE.search(text)
                jobs.append({"id": jid, "name": s.get("title", "Untitled Role"),
                             "title": s.get("title", "Untitled Role"),
                             "locations": [s.get("field_keyword_19") or "United States"],
                             "team": s.get("field_keyword_08") or "",
                             "salary": m.group(1).strip() if m else None,
                             "url": s.get("url") or "https://www.ibm.com/careers/search"})
                details[jid] = {"salary": m.group(1).strip() if m else None,
                                "snippet": _snippet(text), "level": s.get("field_keyword_18") or "",
                                "emp_type": "", "url": s.get("url") or "https://www.ibm.com/careers/search"}
            if fresh == 0:
                break
        log.info("IBM: %d fresh jobs", len(jobs))
        return jobs

    return _make("IBM", "\U0001F535", "#IBMCareers #Hiring #TechJobs #SoftwareEngineering #NowHiring",
                 "https://www.ibm.com/careers/search", fetch_recent, details.get)


# ---------------- instances ----------------

databricks = greenhouse("databricks", "Databricks", "\U0001F9F1",
                        "#DatabricksCareers #Data #AI #Hiring #TechJobs #NowHiring",
                        "https://www.databricks.com/company/careers")
stripe = greenhouse("stripe", "Stripe", "\U0001F4B3",
                    "#StripeCareers #Fintech #Hiring #TechJobs #NowHiring",
                    "https://stripe.com/jobs")
scaleai = greenhouse("scaleai", "Scale AI", "\U0001F4D0",
                     "#ScaleAICareers #AI #Hiring #TechJobs #NowHiring",
                     "https://scale.com/careers")
ramp = ashby("ramp", "Ramp", "\U0001F7E8",
             "#RampCareers #Fintech #Hiring #TechJobs #NowHiring",
             "https://ramp.com/careers", default_loc="New York")
cursor = ashby("cursor", "Cursor", "⌨️",
               "#CursorCareers #AI #Hiring #TechJobs #NowHiring",
               "https://cursor.com/careers", default_loc="San Francisco")
amd = amd()
ibm = ibm()
