"""
Growth formats the job cards can't deliver on their own.

  daily_poll(store)              one LinkedIn poll a day (polls are the
                                 highest-reach native format; no outbound link)
  daily_roundup(store, loader)   one PDF carousel a day: "10 highest-paying
                                 roles posted this week" (documents have the
                                 highest dwell time on LinkedIn)

Both go through the versioned Posts API (/rest/posts + LinkedIn-Version),
which our w_member_social member token is allowed to use (verified: polls
201, document upload 200). Comments/analytics need partner scopes we don't
have, so links stay in the job posts and the growth posts carry none.

Job facts (company, title, location, pay, url) are captured as a side
effect of card generation — record_facts() — into li_jobfacts.json, so the
carousel and the poll numbers never need extra fetching.

Commentary here is LinkedIn "little text format": reserved characters are
escaped, @mentions are @[Name](urn:li:organization:ID), hashtags are
{hashtag|\\#|Tag}.
"""

import io
import os
import re
import json
import random
import logging
import datetime
from datetime import timezone, timedelta

import requests

log = logging.getLogger("growth-posts")

API = "https://api.linkedin.com"
LI_VERSION = os.getenv("LINKEDIN_VERSION", "202608")
FACTS_BLOB = "li_jobfacts.json"
POST_LOG = "li_post_log.json"
FACTS_MAX_DAYS = 14
FACTS_MAX = 3000

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

FOLLOW_CTA = ("➕ Follow me — every new opening at 17 top tech companies, "
              "with the pay range, every day.")

# ---------- little text format ----------

_RESERVED = set("\\|{}@[]()<>#*_~")


def ltf(text):
    """Escape plain text for the Posts API commentary field."""
    return "".join("\\" + ch if ch in _RESERVED else ch for ch in text)


def ltf_mention(name, org_urn):
    return f"@[{ltf(name)}]({org_urn})"


def ltf_tag(tag):
    return "{hashtag|\\#|%s}" % tag


# ---------- Posts API ----------

def _headers(token):
    return {"Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": LI_VERSION,
            "Content-Type": "application/json"}


def _base_post(author, commentary):
    return {"author": author, "commentary": commentary, "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}


def _create(body, token):
    r = requests.post(f"{API}/rest/posts", headers=_headers(token), json=body, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"posts API {r.status_code}: {r.text[:200]}")
    return r.headers.get("x-restli-id", "ok")


def post_poll(question, options, commentary, token, author, duration="THREE_DAYS"):
    """options: 2-4 strings (<=30 chars). question <=140 chars. Returns post URN."""
    body = _base_post(author, commentary)
    body["content"] = {"poll": {"question": question[:140],
                                "options": [{"text": o[:30]} for o in options[:4]],
                                "settings": {"duration": duration}}}
    return _create(body, token)


def post_document(pdf_bytes, title, commentary, token, author):
    """Upload a PDF as a LinkedIn document (carousel) and publish it."""
    r = requests.post(f"{API}/rest/documents?action=initializeUpload",
                      headers=_headers(token),
                      json={"initializeUploadRequest": {"owner": author}}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"document init {r.status_code}: {r.text[:200]}")
    v = r.json()["value"]
    up = requests.put(v["uploadUrl"], headers={"Authorization": f"Bearer {token}",
                                                "Content-Type": "application/pdf"},
                      data=pdf_bytes, timeout=120)
    if up.status_code not in (200, 201):
        raise RuntimeError(f"document upload {up.status_code}: {up.text[:200]}")
    body = _base_post(author, commentary)
    body["content"] = {"media": {"title": title[:100], "id": v["document"]}}
    return _create(body, token)


# ---------- job facts (side effect of generate) ----------

def _pay_top(sal):
    """Largest yearly figure in a salary string; '250K' scales, '250,000k' does not."""
    best = 0
    for m in re.finditer(r"\$?\s?([\d][\d,]*(?:\.\d+)?)\s*([Kk])?", sal or ""):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if m.group(2) and v < 10000:
            v *= 1000
        if 10000 <= v <= 3_000_000:
            best = max(best, v)
    return int(best)


def _clean_salary(sal):
    s = (sal or "").replace(" per year", "").replace(" USD", "").replace("USD ", "")
    s = re.sub(r"(\d)\.00\b", r"\1", s)                                              # 330,000.00 -> 330,000
    s = re.sub(r"(\d{3},\d{3})[kK]\b", r"\1", s)                                      # 250,000k -> 250,000
    s = re.sub(r"(?<![\d,.$])(\d{2,3},\d{3})", r"$\1", s)                             # 320,000 -> $320,000
    s = re.sub(r"\$(\d{4,7})(?!,|\d)", lambda m: "${:,}".format(int(m.group(1))), s)   # $262000 -> $262,000
    s = re.sub(r"\s+", " ", s).strip(" .;:,")
    return s[:40]


def _short_loc(loc):
    """'United States, Washington, Redmond; ...' -> 'Redmond, Washington' (first location, deduped)."""
    first = (loc or "").split(";")[0]
    parts = [p.strip() for p in first.split(",") if p.strip()]
    parts = [p for i, p in enumerate(parts) if p not in parts[:i] and p != "Multiple Locations"]
    if parts and parts[0] == "United States" and len(parts) > 1:
        parts = parts[1:][::-1]
    return ", ".join(parts)[:34] or "United States"


def _load(store, name, default):
    try:
        return json.loads(store.download_blob(name).readall())
    except Exception:
        return default


def record_facts(store, company, jobs, job_loc, job_url):
    """Append (company, title, loc, salary, top, url) for jobs that have pay."""
    try:
        facts = _load(store, FACTS_BLOB, [])
        now = datetime.datetime.now(timezone.utc)
        keep_after = (now - timedelta(days=FACTS_MAX_DAYS)).isoformat()
        facts = [f for f in facts if f.get("ts", "") >= keep_after]
        seen = {(f.get("company"), f.get("url")) for f in facts}
        for j in jobs:
            det = j.get("_detail") or {}
            sal = j.get("salary") or det.get("salary") or ""
            top = _pay_top(sal)
            url = det.get("url") or job_url(company, j) or ""
            if not top or (company, url) in seen:
                continue
            facts.append({"ts": now.isoformat(), "company": company,
                          "title": (j.get("title") or j.get("name") or "Role")[:90],
                          "loc": (job_loc(j) or "United States")[:60],
                          "salary": _clean_salary(sal), "top": top, "url": url})
            seen.add((company, url))
        store.upload_blob(FACTS_BLOB, json.dumps(facts[-FACTS_MAX:]), overwrite=True)
        return len(facts)
    except Exception as e:
        log.warning("record_facts failed: %s", e)
        return 0


def top_paid(store, days=7, n=10, per_company=2):
    """Highest-paying roles of the last `days` days, at most `per_company` each."""
    facts = _load(store, FACTS_BLOB, [])
    cutoff = (datetime.datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    recent = [f for f in facts if f.get("ts", "") >= cutoff and f.get("top")]
    recent.sort(key=lambda f: -f["top"])
    out, count = [], {}
    for f in recent:
        c = f["company"]
        if count.get(c, 0) >= per_company:
            continue
        count[c] = count.get(c, 0) + 1
        out.append(f)
        if len(out) >= n:
            break
    return out


def company_tops(store, days=7):
    """{company: (top_pay, title)} over the window — feeds the poll numbers."""
    best = {}
    for f in _load(store, FACTS_BLOB, []):
        if f.get("ts", "") < (datetime.datetime.now(timezone.utc) - timedelta(days=days)).isoformat():
            continue
        if f.get("top", 0) > best.get(f["company"], (0, ""))[0]:
            best[f["company"]] = (f["top"], f.get("title", ""))
    return best


def _money(v):
    return f"${v // 1000}K" if v >= 1000 else f"${v}"


# ---------- carousel (PDF) ----------

# Where to apply, shown on each slide (ATS hosts like greenhouse.io mean nothing to readers)
CAREERS_SITE = {
    "microsoft": "careers.microsoft.com", "apple": "jobs.apple.com", "google": "google.com/careers",
    "amazon": "amazon.jobs", "nvidia": "nvidia.com/careers", "meta": "metacareers.com",
    "openai": "openai.com/careers", "anthropic": "anthropic.com/careers", "netflix": "jobs.netflix.com",
    "xai": "x.ai/careers", "databricks": "databricks.com/careers", "stripe": "stripe.com/jobs",
    "scaleai": "scale.com/careers", "ramp": "ramp.com/careers", "cursor": "cursor.com/careers",
    "amd": "careers.amd.com", "ibm": "ibm.com/careers",
}

PW, PH = 1080, 1350
NAVY = (11, 31, 58)
GREEN = (10, 125, 60)
INK = (17, 24, 39)
GRAY = (107, 114, 128)
LIGHT = (243, 244, 246)


def _font(size, weight="Bold"):
    from PIL import ImageFont
    for path in (os.path.join(FONT_DIR, f"Poppins-{weight}.ttf"),
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text, font, max_w, max_lines=3):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and (len(words) > sum(len(l.split()) for l in lines)):
        lines[-1] = lines[-1][:-1].rstrip() + "…"
    return lines


def _logo_img(company, logo_loader, max_w=380, max_h=110):
    from PIL import Image
    import card_builder
    raw = card_builder._logo_bytes(company, logo_loader)
    if not raw:
        return None
    try:
        lg = card_builder._trim_logo(Image.open(io.BytesIO(raw)))
        scale = min(max_w / lg.width, max_h / lg.height, 2.5)
        return lg.resize((max(1, int(lg.width * scale)), max(1, int(lg.height * scale))))
    except Exception:
        return None


def _cover(date_str, n, companies):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (PW, PH), NAVY)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 28, PH], fill=GREEN)
    y = 250
    for line in [f"{n} highest-paying", "tech jobs posted", "this week"]:
        d.text((90, y), line, font=_font(92), fill="white")
        y += 112
    d.text((90, y + 30), "with salary ranges", font=_font(56), fill=(134, 239, 172))
    d.text((90, y + 130), f"{len(companies)} companies · {date_str}", font=_font(36, "Medium"), fill=(203, 213, 225))
    d.text((90, PH - 200), "swipe »", font=_font(48), fill="white")
    d.text((90, PH - 120), "new roles with pay, posted daily · follow for the feed",
           font=_font(30, "Regular"), fill=(148, 163, 184))
    return im


def _job_page(i, n, f, logo_loader):
    from PIL import Image, ImageDraw
    import card_builder
    im = Image.new("RGB", (PW, PH), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, PW, 16], fill=GREEN)
    # rank badge
    d.ellipse([80, 90, 200, 210], fill=NAVY)
    rank = f"#{i}"
    fb = _font(52)
    tw = d.textlength(rank, font=fb)
    d.text((140 - tw / 2, 116), rank, font=fb, fill="white")
    # logo or name
    lg = _logo_img(f["company"], logo_loader)
    if lg is not None:
        im.paste(lg, (240, 150 - lg.height // 2), lg if lg.mode == "RGBA" else None)
    else:
        d.text((240, 110), card_builder.display_name(f["company"]), font=_font(60), fill=INK)
    # pay
    d.text((80, 330), "PAY RANGE", font=_font(30, "Medium"), fill=GRAY)
    money = f["salary"] or _money(f["top"])
    fm = _font(84 if len(money) <= 20 else 64)
    d.text((80, 370), money, font=fm, fill=GREEN)
    # title
    y = 520
    for line in _wrap(d, f["title"], _font(62), PW - 160, 3):
        d.text((80, y), line, font=_font(62), fill=INK)
        y += 78
    # meta
    y += 20
    d.text((80, y), f"{card_builder.display_name(f['company'])}  •  {_short_loc(f['loc'])}",
           font=_font(34, "Medium"), fill=GRAY)
    y += 60
    d.text((80, y), "posted this week", font=_font(34, "Medium"), fill=GRAY)
    # apply box
    d.rounded_rectangle([80, PH - 330, PW - 80, PH - 190], radius=24, fill=LIGHT)
    d.text((110, PH - 305), "How to apply", font=_font(30, "Medium"), fill=GRAY)
    site = CAREERS_SITE.get(f["company"]) or re.sub(r"^https?://(www\.)?", "", f.get("url", "")).split("/")[0] or "company careers page"
    d.text((110, PH - 262), f"{site} — search the title", font=_font(38), fill=INK)
    d.text((80, PH - 120), f"{i} / {n}   ·   swipe »", font=_font(30, "Medium"), fill=GRAY)
    return im


def _last_page():
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (PW, PH), NAVY)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 28, PH], fill=GREEN)
    y = 300
    for line in ["Want the next one", "the minute it's posted?"]:
        d.text((90, y), line, font=_font(80), fill="white")
        y += 100
    d.text((90, y + 40), "Follow me", font=_font(96), fill=(134, 239, 172))
    y += 200
    for line in ["17 companies · every new role", "salary range on every post",
                 "links in each post"]:
        d.text((90, y), "•  " + line, font=_font(40, "Medium"), fill=(203, 213, 225))
        y += 66
    d.text((90, PH - 140), "repost to help someone land one", font=_font(32, "Regular"),
           fill=(148, 163, 184))
    return im


def build_roundup_pdf(items, logo_loader=None, date_str=None):
    date_str = date_str or datetime.datetime.now().strftime("%b %d, %Y")
    companies = sorted({f["company"] for f in items})
    pages = [_cover(date_str, len(items), companies)]
    for i, f in enumerate(items, 1):
        pages.append(_job_page(i, len(items), f, logo_loader))
    pages.append(_last_page())
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=96.0)
    return buf.getvalue()


# ---------- the two daily jobs ----------

def _guard(store):
    import linkedin_autopost as la
    if la._cfg("linkedin_autopost_enabled", "false").lower() != "true":
        return "autopost disabled"
    plog = _load(store, POST_LOG, [])
    today = datetime.datetime.now(timezone.utc).date().isoformat()
    if sum(1 for p in plog if str(p.get("ts", "")).startswith(today)) >= la.DAILY_CAP:
        return "daily cap reached"
    return None


def _log_post(store, variant, urn, company="multi", extra=None):
    plog = _load(store, POST_LOG, [])
    e = {"ts": datetime.datetime.now(timezone.utc).isoformat(), "company": company,
         "variant": variant, "urn": urn}
    if extra:
        e.update(extra)
    plog.append(e)
    store.upload_blob(POST_LOG, json.dumps(plog[-2000:]), overwrite=True)


def _already_today(store, variant):
    today = datetime.datetime.now(timezone.utc).date().isoformat()
    return any(p.get("variant") == variant and str(p.get("ts", "")).startswith(today)
               for p in _load(store, POST_LOG, []))


def _names_and_urns():
    import card_builder, linkedin_autopost as la
    return card_builder.display_name, la.ORG_URNS


def daily_roundup(store, logo_loader=None):
    why = _guard(store)
    if why:
        return [f"roundup skipped: {why}"]
    if _already_today(store, "carousel"):
        return ["roundup already posted today"]
    items = top_paid(store)
    if len(items) < 5:
        return [f"roundup skipped: only {len(items)} paid roles on record"]
    import linkedin_client
    display, urns = _names_and_urns()
    token = linkedin_client._token()
    author = linkedin_client.person_urn(token)
    pdf = build_roundup_pdf(items, logo_loader)
    lead = items[0]
    companies = []
    for f in items:
        if f["company"] not in companies:
            companies.append(f["company"])
    tags = " ".join(ltf_mention(display(c), urns[c]) if urns.get(c) else ltf(display(c))
                    for c in companies[:6])
    commentary = (
        ltf(f"\U0001F4B0 The {len(items)} highest-paying tech roles posted this week — with the pay ranges. Swipe \U0001F449")
        + "\n\n" + ltf("#1: ") + ltf(f"{lead['title']} at {display(lead['company'])} — {lead['salary'] or _money(lead['top'])}")
        + "\n\n" + ltf("Inside: ") + tags
        + "\n\n" + ltf(FOLLOW_CTA)
        + "\n" + ltf("♻️ Repost so someone in your network sees it.")
        + "\n" + ltf("\U0001F4AC Which one would you take? \U0001F447")
        + "\n\n" + " ".join(ltf_tag(t) for t in ("TechJobs", "Hiring", "Salary", "JobSearch", "Careers")))
    urn = post_document(pdf, f"{len(items)} highest-paying tech jobs this week", commentary, token, author)
    _log_post(store, "carousel", urn, extra={"items": len(items)})
    return [f"roundup posted {urn} ({len(items)} roles, {len(pdf) // 1024} KB)"]


POLL_SETS = [
    ["openai", "anthropic", "google", "microsoft"],
    ["apple", "meta", "amazon", "nvidia"],
    ["stripe", "databricks", "ramp", "cursor"],
    ["anthropic", "openai", "xai", "scaleai"],
    ["microsoft", "google", "ibm", "amd"],
]

GENERIC_POLLS = [
    ("What matters most in your next tech job?", ["Pay", "Remote / flexibility", "Team & manager", "Brand name"]),
    ("Biggest red flag in a job post?", ["No salary range", "'Fast-paced environment'", "5+ interview rounds", "Return to office"]),
    ("How many applications did you send this week?", ["0", "1-5", "6-20", "20+"]),
    ("Remote or office in 2026?", ["Fully remote", "Hybrid", "Office", "Depends on the pay"]),
    ("Would you switch companies for +$50K?", ["Yes, tomorrow", "Only for a great team", "No, I like it here", "Only if remote"]),
]


def _poll_plan(store, day_index):
    """Alternate: company-vs-company polls (with real pay numbers) and generic ones.
    Returns (question, options, [(company, top_pay, title), ...])."""
    display, urns = _names_and_urns()
    tops = company_tops(store)
    if day_index % 2 == 0:
        for s in [POLL_SETS[(day_index // 2) % len(POLL_SETS)]] + POLL_SETS:
            have = [c for c in s if c in tops][:4]
            if len(have) >= 3:
                opts = [f"{display(c)} ({_money(tops[c][0])})"[:30] for c in have]
                q = "Same offer from all of them — which one do you take?"
                return q, opts, [(c, tops[c][0], tops[c][1]) for c in have]
    q, opts = GENERIC_POLLS[(day_index // 2) % len(GENERIC_POLLS)]
    ranked = sorted(tops.items(), key=lambda kv: -kv[1][0])[:3]
    return q, opts, [(c, v[0], v[1]) for c, v in ranked]


def poll_commentary(q, facts):
    """LTF commentary for a poll: question, real pay lines with @mentions, follow CTA."""
    display, urns = _names_and_urns()
    lines = [ltf(q + " \U0001F447"), ""]
    if facts:
        lines.append(ltf("Real pay from this week's postings:"))
        for c, top, title in facts:
            name = display(c)
            lines.append(ltf("• ") + (ltf_mention(name, urns[c]) if urns.get(c) else ltf(name))
                         + ltf(f" — up to {_money(top)} ({title})"))
        lines.append("")
    lines += [ltf(FOLLOW_CTA), "",
              " ".join(ltf_tag(t) for t in ("TechJobs", "Hiring", "JobSearch", "Salary"))]
    return "\n".join(lines)


def daily_poll(store):
    why = _guard(store)
    if why:
        return [f"poll skipped: {why}"]
    if _already_today(store, "poll"):
        return ["poll already posted today"]
    import linkedin_client
    day_index = (datetime.date.today() - datetime.date(2026, 9, 9)).days
    q, opts, facts = _poll_plan(store, max(day_index, 0))
    commentary = poll_commentary(q, facts)
    token = linkedin_client._token()
    author = linkedin_client.person_urn(token)
    urn = post_poll(q, opts, commentary, token, author)
    _log_post(store, "poll", urn, extra={"question": q})
    return [f"poll posted {urn}: {q}"]
