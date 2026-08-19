"""
Meta Careers -> LinkedIn Post Pipeline (metacareers.com GraphQL, Aug 2026)
Facebook TLS-fingerprints clients, so plain requests are rejected — this uses
curl_cffi's Chrome impersonation. One page load (LSD token) + one GraphQL call
returns the full job list (~850 jobs: id, title, locations, teams).

Meta listings expose NO posting dates, so novelty is tracked purely by job id
(the function app seeds all current ids on first run). US filter is heuristic:
locations ending in a US state code, or containing "United States"/"Remote, US".
"""

import os
import re
import json
import logging
from datetime import datetime

log = logging.getLogger("meta-jobs")

BASE = "https://www.metacareers.com"
DOC_ID_RESULTS = "27506805582236862"   # CareersJobSearchResultsDataQuery
JOBS_PER_POST = int(os.getenv("JOBS_PER_POST", "10"))

US_LOC_RE = re.compile(r", [A-Z]{2}$")

SEARCH_INPUT = {"q": None, "divisions": [], "offices": [], "roles": [],
                "leadership_levels": [], "saved_jobs": [], "saved_searches": [],
                "sub_teams": [], "teams": [], "is_leadership": False,
                "is_remote_only": False, "sort_by_new": True, "results_per_page": None}


def _session():
    from curl_cffi import requests as creq
    return creq.Session(impersonate="chrome124")


def _fetch_all_jobs():
    s = _session()
    r = s.get(f"{BASE}/jobsearch/", timeout=30)
    r.raise_for_status()
    m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', r.text)
    if not m:
        raise RuntimeError("Meta: LSD token not found (page layout changed?)")
    lsd = m.group(1)
    resp = s.post(f"{BASE}/graphql",
                  headers={"x-fb-lsd": lsd, "Origin": BASE,
                           "Referer": f"{BASE}/jobsearch/",
                           "Content-Type": "application/x-www-form-urlencoded"},
                  data={"lsd": lsd, "doc_id": DOC_ID_RESULTS,
                        "variables": json.dumps({"isLoggedIn": False,
                                                 "viewasUserID": None,
                                                 "search_input": SEARCH_INPUT})},
                  timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Meta GraphQL {resp.status_code}: {resp.text[:120]}")
    return resp.json()["data"]["job_search_with_featured_jobs"]["all_jobs"]


def _is_us(job):
    for l in job.get("locations") or []:
        if US_LOC_RE.search(l) or "United States" in l or "Remote, US" in l:
            return True
    return False


def sort_software_first(jobs):
    def key(j):
        t = (j.get("title") or j.get("name") or "").lower()
        if "software engineer" in t:
            return 0
        if "engineer" in t or "developer" in t or "scientist" in t or "research" in t:
            return 1
        return 2
    return sorted(jobs, key=key)


def fetch_detail(job_id):
    """Meta's list payload is all we use (details sit behind heavier defenses)."""
    return {}


def get_jobs(cutoff=None):
    """cutoff is ignored — Meta has no posting dates; id-seeding handles novelty."""
    jobs = [j for j in _fetch_all_jobs() if _is_us(j)]
    log.info("Meta: %d US jobs (no dates; id-diff dedupe)", len(jobs))
    return sort_software_first(jobs)


def build_post(jobs, date_str, part=None, total_parts=None):
    header = f"Ⓜ️ Meta is Hiring! | {date_str}"
    if total_parts and total_parts > 1 and len(jobs) > 1:
        header += f" (Part {part}/{total_parts})"
    lines = [header, "", "Newly listed roles \U0001F447", ""]

    for j in jobs:
        lines.append(f"\U0001F4BC {j.get('title', 'Untitled Role')}")
        lines.append(f"\U0001F4CD {'; '.join((j.get('locations') or ['United States'])[:2])}")
        teams = (j.get("teams") or []) + (j.get("sub_teams") or [])
        if teams:
            lines.append(f"\U0001F3AF {' | '.join(teams[:2])}")
        lines.append(f"\U0001F517 {BASE}/jobs/{j.get('id')}/")
        lines.append("")

    lines += [
        "♻️ Repost to help someone in your network!",
        "\U0001F514 Follow for daily Meta job updates.",
        "",
        "#MetaCareers #Hiring #TechJobs #SoftwareEngineering #JobSearch #NowHiring",
    ]
    return "\n".join(lines)


def render_posts(jobs, date_str=None):
    date_str = date_str or datetime.now().strftime("%B %d, %Y")
    chunks = [jobs[i:i + JOBS_PER_POST] for i in range(0, len(jobs), JOBS_PER_POST)]
    total = len(chunks)
    posts = [build_post(c, date_str, part=i + 1, total_parts=total)
             for i, c in enumerate(chunks)]
    divider = "\n\n" + "=" * 12 + "  ✂️ COPY NEXT POST SEPARATELY  " + "=" * 12 + "\n\n"
    return divider.join(posts)
