"""
LinkedIn auto-post engine (drip model, fits the 10-min function timeout).

generate(): once/day — for each company pull new jobs (own dedupe state),
  split into N cards, render each, store PNG in blob, and append a queue entry
  with a staggered post_after time (SPACING_MIN apart).
drain(): frequent timer — post any queue entries whose post_after has passed
  (up to MAX_PER_DRAIN each run), so ~N*companies posts trickle out over the day.

Gated by env LINKEDIN_AUTOPOST_ENABLED == "true".
State/queue live in the same `linkedin-posts` blob container as the emailer.
"""

import os
import io
import re
import json
import random
import base64
import logging
import datetime
from datetime import timezone, timedelta

import card_builder
import linkedin_client

log = logging.getLogger("li-autopost")

def _cfg(key, default):
    import linkedin_client
    s = linkedin_client._blob_secrets()
    return str(s.get(key, os.getenv(key.upper(), default)))


QUEUE_BLOB = "li_queue.json"
CARDS_PREFIX = "li_cards/"
CARDS_PER_COMPANY = int(os.getenv("LINKEDIN_CARDS_PER_COMPANY", "10"))
JOBS_PER_CARD = int(os.getenv("LINKEDIN_JOBS_PER_CARD", "4"))
SPACING_MIN = int(os.getenv("LINKEDIN_SPACING_MIN", "10"))
MAX_PER_DRAIN = int(os.getenv("LINKEDIN_MAX_PER_DRAIN", "1"))
DAILY_CAP = int(os.getenv("LINKEDIN_DAILY_CAP", "145"))     # LinkedIn API: 150/member/day
STALE_HOURS = 36                                            # drop cards older than this

# imported lazily to avoid circular import with function_app
COMPANIES = None

# Verified LinkedIn organization URNs (blue @mention tags). Only IDs we are
# sure of — a wrong ID would tag the wrong company. All 10 verified.
ORG_URNS = {
    "microsoft": "urn:li:organization:1035",
    "google":    "urn:li:organization:1441",
    "amazon":    "urn:li:organization:1586",
    "apple":     "urn:li:organization:162479",
    "netflix":   "urn:li:organization:165158",
    "nvidia":    "urn:li:organization:3608",
    "meta":      "urn:li:organization:10667",
    "openai":    "urn:li:organization:11130470",
    "anthropic": "urn:li:organization:74126343",
    # xAI rebranded to SpaceXAI; linkedin.com/company/xai redirects to the
    # official SpaceXAI page (verified via x.ai + the LinkedIn redirect)
    "xai":       "urn:li:organization:96151950",
    # verified from each company's public LinkedIn page (page title matched)
    "databricks": "urn:li:organization:3477522",
    "stripe":     "urn:li:organization:2135371",
    "scaleai":    "urn:li:organization:17998520",
    "amd":        "urn:li:organization:1497",
    "ibm":        "urn:li:organization:1009",
    "ramp":       "urn:li:organization:1406226",
    "cursor":     "urn:li:organization:105614038",   # linkedin.com/company/cursorai (from cursor.com footer)
}

HOOK_VARIANTS = ["salary_hook", "question_hook", "grab_hook"]


def _set_companies(companies):
    global COMPANIES
    COMPANIES = companies


def _li_state_blob(company):
    return f"li_{company}_state.json"


def _load(c, blob, default):
    try:
        return json.loads(c.download_blob(blob).readall())
    except Exception:
        return default


def _save(c, blob, obj):
    c.upload_blob(blob, json.dumps(obj), overwrite=True)


def _job_url(company, j):
    i = j.get("id")
    if company == "meta":
        return f"https://www.metacareers.com/jobs/{i}/"
    if company == "microsoft":
        return f"https://jobs.careers.microsoft.com/global/en/job/{i}"
    if company == "apple":
        return f"https://jobs.apple.com/en-us/details/{j.get('positionId', i)}/{j.get('transformedPostingTitle','')}"
    if company == "amazon":
        return "https://www.amazon.jobs" + (j.get("job_path") or "")
    if company == "nvidia":
        return "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job" + (j.get("externalPath") or "")
    if company == "google":
        return j.get("url") or ""
    return j.get("url") or ""


def _loc_str(l):
    """Amazon-style entries are serialized JSON blobs — extract City, State."""
    if isinstance(l, str) and l.lstrip().startswith("{"):
        try:
            d = json.loads(l)
            city = d.get("city")
            st = d.get("normalizedStateName") or d.get("normalizedCountryName")
            return ", ".join(x for x in (city, st) if x) or \
                   d.get("locationNonStemming") or "United States"
        except Exception:
            import re as _re
            m = _re.search(r'"city"\s*:\s*"([^"]+)"', l)
            n = _re.search(r'"normalizedStateName"\s*:\s*"([^"]+)"', l)
            return ", ".join(x.group(1) for x in (m, n) if x) or "United States"
    return l


def _job_loc(j):
    locs = j.get("locations")
    if isinstance(locs, list) and locs:
        if isinstance(locs[0], str):
            return "; ".join(_loc_str(l) for l in locs[:2])
        # apple-style list of dicts
        out = []
        for l in locs[:2]:
            out.append(", ".join(x for x in (l.get("city"), l.get("stateProvince"),
                                             l.get("countryName")) if x) or "United States")
        return "; ".join(out)
    if isinstance(j.get("location"), str):
        return j["location"]
    if j.get("locationsText"):
        return j["locationsText"]
    return "United States"


def _job_team(company, j):
    t = j.get("team")
    if isinstance(t, dict):
        return t.get("teamName", "")
    if isinstance(t, str) and t:
        return t
    teams = (j.get("teams") or []) + (j.get("sub_teams") or [])
    if teams:
        return " | ".join(teams[:2])
    if j.get("job_category"):
        return j["job_category"]
    if company == "microsoft":
        props = j.get("properties") or {}
        lvl = props.get("roleType") or props.get("discipline")
        return lvl[0] if isinstance(lvl, list) and lvl else (lvl or "")
    return ""


def _top_pay(jobs):
    """Largest salary figure across the chunk, for the hook line."""
    best = 0
    for j in jobs:
        sal = j.get("salary") or (j.get("_detail") or {}).get("salary") or ""
        for m in re.finditer(r"\$?\s?([\d][\d,]*(?:\.\d+)?)\s*([Kk])?", sal):
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if m.group(2):
                v *= 1000
            best = max(best, v)
    return f"${int(best):,}" if best >= 10000 else None


def _caption(company, jobs, part, total, style="salary_hook"):
    name = card_builder.display_name(company)
    n = len(jobs)
    top = _top_pay(jobs)
    roles = "1 new role" if n == 1 else f"{n} new roles"
    # exact range string of the best-paid job (used by single-job / grab hooks)
    best_range = ""
    if top:
        for jb in jobs:
            sal = jb.get("salary") or (jb.get("_detail") or {}).get("salary") or ""
            if top.replace("$", "").replace(",", "") in sal.replace(",", ""):
                best_range = sal.replace(" per year", "").replace(" USD", "").strip()
                break
    # Money leads every hook when we have it; no fake urgency when we don't.
    if style == "question_hook":
        hook = (f"Want to earn up to {top} at {name}? {roles.capitalize()} just opened \U0001F440" if top
                else f"Want to work at {name}? {roles.capitalize()} just opened \U0001F440")
    elif style == "grab_hook":
        if top and n == 1 and best_range:
            hook = f"\U0001F4B0 {best_range} at {name} — grab this role before it's gone"
        elif top:
            hook = f"\U0001F4B0 Up to {top} at {name} — {roles}, grab yours before they're gone"
        else:
            hook = f"{name} is hiring — {roles}, grab yours before they're gone \U0001F680"
    elif n == 1:
        t = jobs[0].get("title") or jobs[0].get("name") or "a new role"
        hook = f"{name} is hiring: {t}" + (f" — {best_range or top} \U0001F4B0" if top else "")
    else:
        hook = f"{name} just posted {roles}" + (f" — pay up to {top} \U0001F4B0" if top else " \U0001F680")
    lines = [hook, "", "Fresh openings \U0001F447", ""]
    for j in jobs[:8]:
        det = j.get("_detail") or {}
        title = j.get("title") or j.get("name") or "Role"
        lines.append(f"\U0001F4BC {title}")
        lines.append(f"\U0001F4CD {_job_loc(j)}")
        sal = j.get("salary") or det.get("salary")
        if sal:
            lines.append(f"\U0001F4B0 {sal}")
        url = det.get("url") or _job_url(company, j)
        if url:
            lines.append(f"\U0001F517 {url}")
        lines.append("")
    lines += [
        "\u267B\ufe0f Repost to help a job seeker in your network.",
        "\U0001F4AC Which one are you applying to? \U0001F447",
        "",
        f"#{name.replace(' ', '')}Careers #Hiring #TechJobs #JobSearch",
    ]
    cap = "\n".join(lines)
    if len(cap) > 2900:
        cap = cap[:2870].rsplit("\n", 1)[0] + "\n\n#Hiring #TechJobs"
    return cap


def generate(container, logo_loader=None, companies=None, hours=24):
    """Build the day's cards + queue them staggered. Returns notes."""
    if _cfg("linkedin_autopost_enabled", "false").lower() != "true":
        return ["autopost disabled (set LINKEDIN_AUTOPOST_ENABLED=true)"]
    now = datetime.datetime.now(timezone.utc)
    date_str = datetime.datetime.now().strftime("%B %d, %Y")
    queue = _load(container, QUEUE_BLOB, [])
    added = []
    notes = []
    slot = len(queue)  # continue staggering after anything already queued
    targets = companies or list(COMPANIES)
    per_company = []   # entries per company, merged round-robin at the end
    for company in targets:
        cfg = COMPANIES[company]
        try:
            _generate_one(container, logo_loader, company, cfg, now, date_str, per_company, notes, hours)
        except Exception as e:
            notes.append(f"{company}: crashed {type(e).__name__}: {str(e)[:120]}")
    _finish_generate(container, queue, added, per_company, now, slot, notes)
    return notes


def _generate_one(container, logo_loader, company, cfg, now, date_str, per_company, notes, hours):
    """One company's fetch → chunk → card → caption. Raises on failure (caller isolates)."""
    state = _load(container, _li_state_blob(company),
                  {"posted_ids": [], "last_run": None})
    cutoff = now - timedelta(hours=hours)
    try:
        fresh = cfg["pipeline"].get_jobs(cutoff)
    except Exception as e:
        notes.append(f"{company}: fetch failed {e}")
        return
    posted = set(state.get("posted_ids", []))
    new = [j for j in fresh if str(j.get("id")) not in posted]
    cards_cap = max(1, min(10, int(_cfg("cards_per_company", CARDS_PER_COMPANY))))
    jpc = max(1, min(6, int(_cfg("jobs_per_card", JOBS_PER_CARD))))
    new = cfg["pipeline"].sort_software_first(new)[:cards_cap * jpc]
    if not new:
        state["last_run"] = now.isoformat()
        _save(container, _li_state_blob(company), state)
        notes.append(f"{company}: no new jobs")
        return
    # divide across up to cards_cap posts (1..jpc jobs each)
    chunks = card_builder.split_into(new, min(cards_cap, len(new)))
    comp_entries = []
    for i, chunk in enumerate(chunks):
        # enrich for salary on the card (best-effort, bounded to this chunk)
        for j in chunk:
            try:
                key = j.get("externalPath") if company == "nvidia" else j.get("id")
                det = cfg["pipeline"].fetch_detail(key) or {}
                j["_detail"] = det
                if det.get("salary"):
                    j["salary"] = det["salary"]
            except Exception:
                pass
        card_blob = f"{CARDS_PREFIX}{company}_logo.png"
        try:
            container.download_blob(card_blob).readall()   # exists — reuse
        except Exception:
            png = card_builder.build_card(company, chunk, logo_loader=logo_loader)
            container.upload_blob(card_blob, png, overwrite=True)
        style = random.choice(HOOK_VARIANTS)
        caption = _caption(company, chunk, i + 1, len(chunks), style)
        comp_entries.append({
            "company": company,
            "card_blob": card_blob,
            "caption": caption,
            "variant": style,
            "title": f"{card_builder.display_name(company)} is hiring",
            "created": now.isoformat(),
        })
    per_company.append(comp_entries)
    state["posted_ids"] = list(dict.fromkeys(
        list(posted) + [str(j.get("id")) for j in fresh]))[-8000:]
    state["last_run"] = now.isoformat()
    _save(container, _li_state_blob(company), state)
    notes.append(f"{company}: queued {len(chunks)} cards ({len(new)} jobs)")


def _finish_generate(container, queue, added, per_company, now, slot, notes):
    while any(per_company):
        for lst in per_company:
            if lst:
                e = lst.pop(0)
                e["post_after"] = (now + timedelta(minutes=SPACING_MIN * slot)).isoformat()
                slot += 1
                added.append(e)
    # merge-on-save: re-read so a concurrent drain's queue update isn't clobbered
    queue = _load(container, QUEUE_BLOB, []) + added
    _save(container, QUEUE_BLOB, queue)
    notes.append(f"queue length {len(queue)}")
    try:
        import healthcheck
        healthcheck.record(container, "generate", True, f"+{len(added)} cards, queue {len(queue)}")
    except Exception:
        pass


def _in_posting_window(now):
    return True    # 24/7 posting: one card per drain run (6/hour)


def drain(container):
    """Post due queue entries (up to MAX_PER_DRAIN). Returns notes."""
    now = datetime.datetime.now(timezone.utc)
    if not _in_posting_window(now):
        return ["outside posting window (7:30a-7p ET) — holding queue"]
    # hard daily cap (LinkedIn allows 150 posts/member/day)
    try:
        plog = _load(container, "li_post_log.json", [])
        today = now.date().isoformat()
        if sum(1 for p in plog if str(p.get("ts", "")).startswith(today)) >= DAILY_CAP:
            return [f"daily cap {DAILY_CAP} reached — resuming tomorrow"]
    except Exception:
        pass
    # prune stale cards so a backlog never posts days-old roles
    q0 = _load(container, QUEUE_BLOB, [])
    cutoff_iso = (now - timedelta(hours=STALE_HOURS)).isoformat()
    q1 = [e for e in q0 if not e.get("created") or e["created"] >= cutoff_iso]
    if len(q1) != len(q0):
        _save(container, QUEUE_BLOB, q1)
    if _cfg("linkedin_autopost_enabled", "false").lower() != "true":
        return ["autopost disabled"]
    now = datetime.datetime.now(timezone.utc)
    queue = _load(container, QUEUE_BLOB, [])
    if not queue:
        return ["queue empty"]
    try:
        token = linkedin_client._token()
        urn = linkedin_client.person_urn(token)
    except Exception as e:
        import healthcheck
        healthcheck.record(container, "drain", False, f"token/urn failure: {e}")
        healthcheck.alert(container, "LinkedIn token problem",
                          f"Posting is blocked: {e}\nRe-authorize the LinkedIn app and store the new token.")
        return [f"token failure: {e}"]
    remaining, posted, notes = [], 0, []
    for item in queue:
        due = datetime.datetime.fromisoformat(item["post_after"]) <= now
        if posted >= MAX_PER_DRAIN or not due:
            remaining.append(item)
            continue
        try:
            png = container.download_blob(item["card_blob"]).readall()
            mention = None
            org = ORG_URNS.get(item["company"])
            if org:
                mention = (card_builder.display_name(item["company"]), org)
            share = linkedin_client.post_with_image(item["caption"], png,
                                                    title=item["title"],
                                                    token=token,
                                                    mention=mention)
            notes.append(f"posted {item['company']} {share}")
            try:
                plog = _load(container, "li_post_log.json", [])
                plog.append({"ts": now.isoformat(), "company": item["company"],
                             "variant": item.get("variant", "salary_hook"),
                             "urn": share})
                _save(container, "li_post_log.json", plog[-2000:])
            except Exception:
                pass
            posted += 1
        except Exception as e:
            err = str(e)
            # FALLBACK 1: image path broke (upload/asset/PNG) -> post the caption as text
            if any(k in err.lower() for k in ("registerupload", "upload", "asset", "png", "image", "media")):
                try:
                    share = linkedin_client.post_text(item["caption"], token=token)
                    notes.append(f"posted {item['company']} TEXT-ONLY fallback {share}")
                    plog = _load(container, "li_post_log.json", [])
                    plog.append({"ts": now.isoformat(), "company": item["company"],
                                 "variant": item.get("variant", "salary_hook"),
                                 "urn": share, "fallback": "text"})
                    _save(container, "li_post_log.json", plog[-2000:])
                    posted += 1
                    continue
                except Exception as e2:
                    err = f"{err} | text fallback: {e2}"
            item["retries"] = item.get("retries", 0) + 1
            if item["retries"] < 3:
                remaining.append(item)
            notes.append(f"{item['company']} failed ({item.get('retries')}): {err[:160]}")
            if "401" in err or "expired" in err.lower():
                import healthcheck
                healthcheck.alert(container, "LinkedIn token rejected",
                                  f"A post failed with an auth error: {err[:300]}")
    _save(container, QUEUE_BLOB, remaining)
    notes.append(f"{posted} posted, {len(remaining)} left")
    try:
        import healthcheck
        failed = [n for n in notes if "failed" in n]
        healthcheck.record(container, "drain", not failed,
                           failed[0] if failed else f"{posted} posted, {len(remaining)} left")
    except Exception:
        pass
    return notes
