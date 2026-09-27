"""
The posting guarantee: 144 slots a day, one every 10 minutes, no matter what.

Jobs always go first. When the job queue has nothing left (and the refill
found nothing new), drain() posts one of these instead, so a slot is never
skipped. Everything here is original material with an image we render:

  news   — headline card for a fresh item from the companies' own
           engineering/AI blogs, Hacker News (official API) or Hugging Face
           daily papers. Caption = title + a short summary in our own words
           (Azure OpenAI when configured, else a one-liner) + link + follow CTA.
  chart  — bar chart drawn from our own pay facts (li_jobfacts.json):
           top of the posted range by company, roles-with-pay by company,
           median SWE pay by company, ...

refill(store)    daily timer + on demand: tops up li_filler_queue.json
                 (dedup against li_filler_state.json, freshness window)
post_one(store)  pops the next item, posts it with its image (text-only
                 fallback), logs variant news/paper/chart to li_post_log.json
"""

import io
import re
import json
import html
import hashlib
import logging
import datetime
import statistics
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import timezone, timedelta

import requests

log = logging.getLogger("filler")

QUEUE = "li_filler_queue.json"
STATE = "li_filler_state.json"
CARDS = "li_filler_cards/"
TARGET = 60                 # keep this many ready
MAX_AGE_DAYS = 7            # news older than this is not "latest"
LLM_BUDGET_S = 200          # summarisation time budget per refill
UA = {"User-Agent": "Mozilla/5.0 (compatible; jobs-autopilot/1.0)"}

# (source label, url, kind, company key for the @mention or None)
FEEDS = [
    ("OpenAI", "https://openai.com/news/rss.xml", "news", "openai"),
    ("Anthropic", "https://www.anthropic.com/rss.xml", "news", "anthropic"),
    ("Google AI", "https://blog.google/technology/ai/rss/", "news", "google"),
    ("Google Research", "https://research.google/blog/rss/", "news", "google"),
    ("Microsoft AI", "https://blogs.microsoft.com/ai/feed/", "news", "microsoft"),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", "news", "microsoft"),
    ("Meta Engineering", "https://engineering.fb.com/feed/", "news", "meta"),
    ("Meta AI", "https://ai.meta.com/blog/rss/", "news", "meta"),
    ("Netflix Tech Blog", "https://netflixtechblog.com/feed", "news", "netflix"),
    ("Stripe", "https://stripe.com/blog/feed.rss", "news", "stripe"),
    ("Databricks", "https://www.databricks.com/feed", "news", "databricks"),
    ("NVIDIA", "https://blogs.nvidia.com/feed/", "news", "nvidia"),
    ("NVIDIA Developer", "https://developer.nvidia.com/blog/feed", "news", "nvidia"),
    ("AWS", "https://aws.amazon.com/blogs/aws/feed/", "news", "amazon"),
    ("Apple ML Research", "https://machinelearning.apple.com/rss.xml", "news", "apple"),
    ("AMD", "https://www.amd.com/en/blogs.rss", "news", "amd"),
    ("IBM Research", "https://research.ibm.com/blog/rss.xml", "news", "ibm"),
    ("GitHub", "https://github.blog/feed/", "news", None),
    ("Hugging Face", "https://huggingface.co/blog/feed.xml", "news", None),
]
HN_BEST = "https://hacker-news.firebaseio.com/v0/beststories.json"
HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{}.json"
HF_PAPERS = "https://huggingface.co/api/daily_papers?limit=25"

TECH_RX = re.compile(r"\b(AI|LLM|GPT|Claude|Gemini|Llama|model|agent|engineer|software|developer|"
                     r"open[- ]source|Python|Rust|TypeScript|JavaScript|database|cloud|Kubernetes|GPU|chip|"
                     r"startup|hiring|layoff|salary|compiler|Linux|API|infra|inference|training|data|"
                     r"security|robot|semiconductor|quantum|compute)\b", re.I)

LABEL = {"news": "AI & ENGINEERING NEWS", "paper": "NEW PAPER", "hn": "TRENDING ON HACKER NEWS",
         "chart": "THIS WEEK IN PAY"}
NICE = {"news": "AI & engineering news", "paper": "New paper", "hn": "Trending on Hacker News",
        "chart": "This week in pay"}
EMOJI = {"news": "\U0001F9E0", "paper": "\U0001F4DA", "hn": "\U0001F525", "chart": "\U0001F4CA"}


# ---------- storage ----------

def _load(store, name, default):
    try:
        return json.loads(store.download_blob(name).readall())
    except Exception:
        return default


def _save(store, name, obj):
    store.upload_blob(name, json.dumps(obj), overwrite=True)


def _now():
    return datetime.datetime.now(timezone.utc)


# ---------- feeds ----------

def _text(el):
    raw = el.text if el is not None and el.text else ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def _when(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(xml_bytes):
    """RSS 2.0 or Atom -> [{title, url, snippet, when}] (newest first, unsorted input ok)."""
    out = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for it in root.iter("item"):                                   # RSS
        title, link = _text(it.find("title")), (it.findtext("link") or "").strip()
        if not link:
            g = it.find("guid")
            link = (g.text or "").strip() if g is not None and (g.text or "").startswith("http") else ""
        out.append({"title": title, "url": link, "snippet": _text(it.find("description"))[:600],
                    "when": _when(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))})
    for e in root.iter("{http://www.w3.org/2005/Atom}entry"):     # Atom
        link = ""
        for l in e.findall("a:link", ns):
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                link = l.get("href")
                break
        out.append({"title": _text(e.find("a:title", ns)), "url": link,
                    "snippet": (_text(e.find("a:summary", ns)) or _text(e.find("a:content", ns)))[:600],
                    "when": _when(e.findtext("a:published", namespaces=ns) or e.findtext("a:updated", namespaces=ns))})
    return [x for x in out if x["title"] and x["url"].startswith("http")]


def fetch_feeds(timeout=8):
    items = []
    cutoff = _now() - timedelta(days=MAX_AGE_DAYS)
    for source, url, kind, company in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code != 200:
                continue
            for x in parse_feed(r.content)[:15]:
                if x["when"] and x["when"] < cutoff:
                    continue
                x.update({"source": source, "kind": kind, "company": company})
                items.append(x)
        except Exception as e:
            log.info("feed %s failed: %s", source, e)
    return items


def fetch_hn(limit=12, min_score=150, timeout=6):
    items = []
    try:
        ids = requests.get(HN_BEST, timeout=timeout).json()[:40]
    except Exception:
        return items
    cutoff = _now() - timedelta(days=3)
    for i in ids:
        try:
            it = requests.get(HN_ITEM.format(i), timeout=timeout).json() or {}
        except Exception:
            continue
        url, title = it.get("url"), it.get("title", "")
        when = datetime.datetime.fromtimestamp(it.get("time", 0), timezone.utc)
        if not url or it.get("score", 0) < min_score or when < cutoff or not TECH_RX.search(title):
            continue
        items.append({"title": title, "url": url, "snippet": "", "when": when, "source": "Hacker News",
                      "kind": "hn", "company": None, "score": it.get("score", 0)})
        if len(items) >= limit:
            break
    return items


def fetch_papers(limit=10, timeout=8):
    items = []
    try:
        data = requests.get(HF_PAPERS, headers=UA, timeout=timeout).json()
    except Exception:
        return items
    for d in data[:limit * 2]:
        p = d.get("paper") or {}
        if not p.get("id") or not p.get("title"):
            continue
        items.append({"title": p["title"], "url": f"https://huggingface.co/papers/{p['id']}",
                      "snippet": (p.get("summary") or "")[:600], "when": _when(d.get("publishedAt") or p.get("publishedAt")),
                      "source": "Hugging Face Papers", "kind": "paper", "company": None,
                      "score": p.get("upvotes", 0)})
        if len(items) >= limit:
            break
    return items


# ---------- charts from our own pay data ----------

def _facts(store, days=7):
    try:
        import growth_posts
        return growth_posts._recent_facts(store, days)
    except Exception:
        return []


def _money(v):
    return f"${v // 1000}K"


def chart_candidates(store):
    """[(chart_id, title, subtitle, rows[(company, value)], fmt)] with enough data."""
    facts = _facts(store)
    out = []
    if len(facts) < 15:
        return out
    by_c = {}
    for f in facts:
        by_c.setdefault(f["company"], []).append(f)
    tops = sorted(((c, max(x["top"] for x in v)) for c, v in by_c.items()), key=lambda t: -t[1])[:8]
    if len(tops) >= 5:
        out.append(("top_by_company", "Top of the posted pay range, by company",
                    "highest-paying role posted this week", tops, _money))
    counts = sorted(((c, len(v)) for c, v in by_c.items()), key=lambda t: -t[1])[:8]
    if len(counts) >= 5:
        out.append(("roles_with_pay", "Roles posted with a public pay range, by company",
                    "new postings this week that state a salary", counts, lambda v: str(v)))
    swe = re.compile(r"software (engineer|development)|\bSWE\b|\bSDE\b", re.I)
    med = []
    for c, v in by_c.items():
        s = [x["top"] for x in v if swe.search(x["title"])]
        if len(s) >= 3:
            med.append((c, int(statistics.median(s))))
    med = sorted(med, key=lambda t: -t[1])[:8]
    if len(med) >= 5:
        out.append(("median_swe", "Software Engineer pay: median top-of-range, by company",
                    "roles titled Software Engineer, posted this week", med, _money))
    cats = {"AI / ML": r"\b(AI|ML|machine learning|research|LLM|deep learning|applied scien|data scien)",
            "Infrastructure": r"infra|platform|cloud|SRE|reliab|systems|kernel|network",
            "Security": r"security|trust|privacy", "Data": r"\bdata\b|analytics",
            "Product / Design": r"product|design|UX", "Management": r"manager|director|head of|\bVP\b"}
    cat_rows = []
    for name, rx in cats.items():
        s = [x["top"] for x in facts if re.search(rx, x["title"], re.I)]
        if len(s) >= 3:
            cat_rows.append((name, max(s)))
    cat_rows = sorted(cat_rows, key=lambda t: -t[1])
    if len(cat_rows) >= 4:
        out.append(("by_category", "Highest posted pay this week, by role type",
                    "top of range across 17 tech companies", cat_rows, _money))
    return out


def bar_chart_png(title, subtitle, rows, fmt, logo_loader=None):
    """1200x627 horizontal bar chart (original image for the post)."""
    from PIL import Image, ImageDraw
    import growth_posts as gp
    import card_builder
    W, H = 1200, 627
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 12], fill=gp.GREEN)
    d.text((60, 40), title[:70], font=gp._font(38), fill=gp.INK)
    d.text((60, 92), subtitle[:90], font=gp._font(24, "Medium"), fill=gp.GRAY)
    top = max(v for _, v in rows) or 1
    n = len(rows)
    y0, avail = 140, H - 140 - 60
    row_h = min(58, avail // max(n, 1))
    x_label, x_bar0, x_bar1 = 60, 330, W - 90
    for i, (key, val) in enumerate(rows):
        cy = y0 + i * row_h
        name = card_builder.display_name(key) if key in card_builder.DISPLAY else key
        lg = gp._logo_img(key, logo_loader, max_w=60, max_h=30) if key in card_builder.DISPLAY else None
        if lg is not None:
            im.paste(lg, (x_label + (60 - lg.width) // 2, cy + 4 + (30 - lg.height) // 2), lg if lg.mode == "RGBA" else None)
        d.text((x_label + 75, cy + 6), name[:22], font=gp._font(24, "Medium"), fill=gp.INK)
        w = int((x_bar1 - x_bar0) * val / top)
        d.rounded_rectangle([x_bar0, cy + 6, x_bar0 + max(w, 6), cy + 34], radius=8,
                            fill=gp.GREEN if i < 3 else (52, 105, 170))
        label = fmt(val)
        lw = d.textlength(label, font=gp._font(22))
        inside = x_bar0 + w + 10 + lw > x_bar1
        d.text((x_bar0 + w - lw - 10 if inside else x_bar0 + w + 10, cy + 8), label,
               font=gp._font(22), fill="white" if inside else gp.INK)
    d.text((60, H - 44), "source: pay ranges published in this week's postings at 17 tech companies  ·  follow for the daily feed",
           font=gp._font(20, "Regular"), fill=gp.GRAY)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def news_card_png(kind, title, source, when):
    """1200x627 headline card: our own design, no third-party images."""
    from PIL import Image, ImageDraw
    import growth_posts as gp
    W, H = 1200, 627
    im = Image.new("RGB", (W, H), gp.NAVY)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 22, H], fill=gp.GREEN)
    d.text((70, 52), LABEL.get(kind, "NEWS"), font=gp._font(26, "Medium"), fill=(134, 239, 172))
    size = 60 if len(title) <= 70 else 50 if len(title) <= 110 else 42
    lines = gp._wrap(d, title, gp._font(size), W - 150, 4)
    y = 120
    for line in lines:
        d.text((70, y), line, font=gp._font(size), fill="white")
        y += int(size * 1.25)
    date_s = when.strftime("%b %d, %Y") if when else datetime.date.today().strftime("%b %d, %Y")
    d.text((70, H - 110), f"{source}  ·  {date_s}", font=gp._font(28, "Medium"), fill=(203, 213, 225))
    d.text((70, H - 62), "follow for daily tech jobs with pay ranges", font=gp._font(22, "Regular"), fill=(148, 163, 184))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


# ---------- captions ----------

def _tags(kind, company):
    base = {"news": "#AI #Engineering #TechNews #SoftwareEngineering",
            "hn": "#TechNews #Engineering #Programming #HackerNews",
            "paper": "#AI #MachineLearning #Research #LLM",
            "chart": "#TechJobs #Salary #Compensation #Hiring"}[kind]
    if company:
        import card_builder
        base += f" #{card_builder.display_name(company).replace(' ', '')}"
    return base


def caption_for(item):
    import linkedin_autopost as la
    import card_builder
    kind = item["kind"]
    lines = [f"{EMOJI[kind]} {NICE[kind]}: {item['title']}", ""]
    if item.get("summary"):
        lines += [item["summary"], ""]
    if item.get("company"):
        lines += [f"\U0001F4E3 From {card_builder.display_name(item['company'])}", ""]
    if item.get("url"):
        lines += [f"\U0001F517 {item['url']}", ""]
    lines += [la.FOLLOW_CTA, "\U0001F4AC What do you think? \U0001F447", "", _tags(kind, item.get("company"))]
    return "\n".join(lines)[:2900]


def chart_caption(chart):
    import linkedin_autopost as la
    import card_builder
    cid, title, subtitle, rows, fmt = chart
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]
    lines = [f"\U0001F4CA {title}", ""]
    for m, (key, val) in zip(medals, rows[:3]):
        name = card_builder.display_name(key) if key in card_builder.DISPLAY else key
        lines.append(f"{m} {name} — {fmt(val)}")
    lines += ["", f"({subtitle}; source: pay ranges published in this week's postings at 17 tech companies)", "",
              la.FOLLOW_CTA, "\U0001F4AC Surprised by any of these? \U0001F447", "", _tags("chart", None)]
    return "\n".join(lines)


# ---------- summaries (our own words) ----------

def _llm_summary(item, sec):
    ep, key = sec.get("aoai_endpoint"), sec.get("aoai_key")
    if not (ep and key):
        return None
    import llm_chat
    dep = sec.get("aoai_deployment", "gpt-4o-mini")
    system = ("You write two short plain-English sentences (max 45 words total) for a LinkedIn post "
              "about a tech news item: what it is, and why it matters to software engineers or job "
              "seekers. Use your own wording only — never copy phrases from the source. No hashtags, "
              "no emojis, no links, no hype.")
    user = f"Title: {item['title']}\nSource: {item['source']}\nSnippet: {item.get('snippet', '')[:500]}"
    out = llm_chat.chat(ep, key, dep, [{"role": "system", "content": system}, {"role": "user", "content": user}])
    out = re.sub(r"\s+", " ", out or "").strip()
    return out[:400] if 20 < len(out) else None


def _fallback_summary(item):
    if item["kind"] == "paper":
        return "New research paper trending with the ML community today."
    if item["kind"] == "hn":
        return f"One of the most-discussed engineering stories today ({item.get('score', 0)} points on Hacker News)."
    return f"Fresh from {item['source']} this week."


# ---------- refill / post ----------

def _key(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def refill(store, quick=False, logo_loader=None):
    """Top the filler queue up to TARGET. quick=True: only the fast sources, no LLM."""
    import time
    t0 = time.time()
    queue = _load(store, QUEUE, [])
    state = _load(store, STATE, {"seen": [], "charts": {}})
    seen = set(state.get("seen", []))
    have = {q["url"] for q in queue}
    notes = []
    if len(queue) >= TARGET and not quick:
        return [f"filler queue ok ({len(queue)})"]
    cands = []
    if quick:
        cands += fetch_hn(limit=8) + fetch_papers(limit=6)
    else:
        cands += fetch_feeds() + fetch_hn() + fetch_papers()
    cands = [c for c in cands if c["url"] not in seen and c["url"] not in have]
    cands.sort(key=lambda c: (c.get("when") or _now() - timedelta(days=30)), reverse=True)
    # interleave sources so the queue isn't 15 posts from one blog in a row
    by_src, order, mixed = {}, [], []
    for c in cands:
        if c["source"] not in by_src:
            order.append(c["source"])
        by_src.setdefault(c["source"], []).append(c)
    while any(by_src.values()) and len(mixed) < TARGET:
        for s in order:
            if by_src[s]:
                mixed.append(by_src[s].pop(0))
    sec = {}
    if not quick:
        try:
            import linkedin_client
            sec = linkedin_client._blob_secrets()
        except Exception:
            sec = {}
    added = 0
    for c in mixed:
        if len(queue) >= TARGET:
            break
        summary = None
        if not quick and time.time() - t0 < LLM_BUDGET_S:
            try:
                summary = _llm_summary(c, sec)
            except Exception as e:
                log.info("summary failed: %s", e)
        item = {"kind": c["kind"], "title": c["title"][:200], "url": c["url"], "source": c["source"],
                "company": c.get("company"), "summary": summary or _fallback_summary(c),
                "when": c["when"].isoformat() if c.get("when") else None,
                "card_blob": CARDS + _key(c["url"]) + ".png", "created": _now().isoformat()}
        try:
            store.upload_blob(item["card_blob"], news_card_png(c["kind"], c["title"], c["source"], c.get("when")), overwrite=True)
        except Exception as e:
            log.info("card failed: %s", e)
            continue
        item["caption"] = caption_for(item)
        queue.append(item)
        seen.add(c["url"])
        added += 1
    # a couple of charts a day from our own data, spread through the queue
    today = _now().date().isoformat()
    charts_done = state.setdefault("charts", {})
    charts_added = 0
    if not quick:
        for chart in chart_candidates(store):
            if charts_done.get(chart[0]) == today or charts_added >= 2:
                continue
            try:
                png = bar_chart_png(chart[1], chart[2], chart[3], chart[4], logo_loader)
                blob = CARDS + f"chart_{chart[0]}_{today}.png"
                store.upload_blob(blob, png, overwrite=True)
                pos = min(len(queue), 5 + charts_added * 20)
                queue.insert(pos, {"kind": "chart", "title": chart[1], "url": "", "source": "our data",
                                   "company": None, "summary": "", "card_blob": blob,
                                   "caption": chart_caption(chart), "created": _now().isoformat()})
                charts_done[chart[0]] = today
                charts_added += 1
            except Exception as e:
                log.info("chart failed: %s", e)
    state["seen"] = list(seen)[-3000:]
    _save(store, STATE, state)
    _save(store, QUEUE, queue[:TARGET + 10])
    notes.append(f"filler refill: +{added} items, +{charts_added} charts, queue {len(queue)} ({time.time() - t0:.0f}s)")
    return notes


def post_one(store):
    """Post the next filler item. Never raises; returns notes."""
    import linkedin_client
    import linkedin_autopost as la
    queue = _load(store, QUEUE, [])
    if not queue:
        try:
            refill(store, quick=True)
        except Exception as e:
            return [f"filler refill failed: {e}"]
        queue = _load(store, QUEUE, [])
        if not queue:
            return ["filler: nothing available"]
    item = queue.pop(0)
    _save(store, QUEUE, queue)
    try:
        png = store.download_blob(item["card_blob"]).readall()
    except Exception:
        try:
            png = news_card_png(item["kind"], item["title"], item["source"], None)
        except Exception:
            png = None
    mention = None
    if item.get("company") and la.ORG_URNS.get(item["company"]):
        import card_builder
        mention = (card_builder.display_name(item["company"]), la.ORG_URNS[item["company"]])
    caption = item.get("caption") or caption_for(item)
    urn = None
    try:
        if png:
            urn = linkedin_client.post_with_image(caption, png, title=item["title"][:100], mention=mention)
        else:
            urn = linkedin_client.post_text(caption)
    except Exception as e:
        try:
            urn = linkedin_client.post_text(caption)
            note = f" (text-only fallback after: {str(e)[:80]})"
        except Exception as e2:
            return [f"filler post failed: {str(e)[:100]} | text: {str(e2)[:80]}"]
    else:
        note = ""
    try:
        plog = _load(store, "li_post_log.json", [])
        plog.append({"ts": _now().isoformat(), "company": item.get("company") or "filler",
                     "variant": item["kind"], "urn": urn, "title": item["title"][:120]})
        _save(store, "li_post_log.json", plog[-2000:])
    except Exception:
        pass
    return [f"posted filler {item['kind']}: {item['title'][:60]} {urn}{note}", f"1 posted, {len(queue)} filler left"]
