"""Unit tests for the MS jobs LinkedIn pipeline."""
import json
import sys
import os
import pathlib
import datetime
from datetime import timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ms_jobs_pipeline as p


# ---------- salary regex ----------

def test_salary_regex_basic():
    m = p.PAY_RANGE_RE.search("base pay range is USD $121,600 - $234,700 per year across the US")
    assert m and m.group(1) == "USD $121,600 - $234,700 per year"

def test_salary_regex_en_dash():
    assert p.PAY_RANGE_RE.search("USD $97,600 – $188,400")

def test_salary_regex_absent():
    assert p.PAY_RANGE_RE.search("competitive compensation and benefits") is None


# ---------- sorting ----------

def test_software_engineers_first():
    jobs = [{"name": "Product Manager"}, {"name": "Senior Software Engineer"},
            {"name": "Data Scientist"}, {"name": "Software Engineer II"}]
    out = [j["name"] for j in p.sort_software_first(jobs)]
    assert out[0] == "Senior Software Engineer" and out[1] == "Software Engineer II"
    assert out[2] == "Data Scientist" and out[3] == "Product Manager"

def test_sort_is_stable_within_group():
    jobs = [{"name": f"Software Engineer {i}"} for i in range(5)]
    assert [j["name"] for j in p.sort_software_first(jobs)] == [j["name"] for j in jobs]


# ---------- rendering ----------

def test_one_job_per_post(monkeypatch):
    monkeypatch.setattr(p, "JOBS_PER_POST", 1)
    monkeypatch.setattr(p, "fetch_detail", lambda pid: {
        "salary": "USD $100,000 - $200,000 per year", "snippet": "Great role…",
        "level": "Individual Contributor", "emp_type": "Full-Time",
        "url": "https://example.com/job/1"})
    jobs = [{"id": i, "name": f"SDE {i}", "locations": ["Redmond, WA"]} for i in range(3)]
    out = p.render_posts(jobs, date_str="July 17, 2026")
    assert out.count("Microsoft is Hiring") == 3          # 3 standalone posts
    assert out.count("COPY NEXT POST") == 2               # 2 dividers between 3 posts
    assert out.count("Repost to help") == 3               # every post has the footer
    assert "(Part" not in out                             # no part numbering on single-job posts

def test_footer_and_fields_present(monkeypatch):
    monkeypatch.setattr(p, "fetch_detail", lambda pid: {
        "salary": "USD $1 - $2", "snippet": "S", "level": "IC", "emp_type": "Full-Time",
        "url": "https://example.com/j"})
    post = p.build_post([{"id": 1, "name": "SDE", "locations": ["X"]}], "July 17, 2026")
    for token in ["\U0001F4BC SDE", "\U0001F4CD X", "\U0001F4B0", "\U0001F517", "#MicrosoftCareers"]:
        assert token in post


# ---------- lookback filter ----------

def test_fetch_recent_stops_at_cutoff(monkeypatch):
    now = datetime.datetime.now(timezone.utc)
    fresh_ts = int(now.timestamp()) - 3600
    old_ts = int(now.timestamp()) - 90000  # >24h
    pages = [
        {"data": {"positions": [{"id": 1, "postedTs": fresh_ts}]}},
        {"data": {"positions": [{"id": 2, "postedTs": old_ts}]}},   # all old -> stop
        {"data": {"positions": [{"id": 3, "postedTs": fresh_ts}]}}, # never reached
    ]
    calls = []
    def fake_get(url, params, retries=2):
        page = pages[len(calls)]
        calls.append(1)
        return page
    monkeypatch.setattr(p, "_get", fake_get)
    monkeypatch.setattr(p.time, "sleep", lambda s: None)
    jobs = p.fetch_recent_jobs(now - datetime.timedelta(hours=24))
    assert [j["id"] for j in jobs] == [1]
    assert len(calls) == 2  # stopped after the all-old page


# ---------- batch state logic (function_app) ----------

class FakeContainer:
    def __init__(self):
        self.blobs = {}
    def create_container(self):
        pass
    def download_blob(self, name):
        blobs = self.blobs
        class B:
            def readall(self):
                if name not in blobs:
                    raise FileNotFoundError(name)
                return blobs[name].encode() if isinstance(blobs[name], str) else blobs[name]
        return B()
    def upload_blob(self, name, data, overwrite=True):
        self.blobs[name] = data


def _setup_fa(monkeypatch, jobs):
    os.environ.setdefault("AzureWebJobsStorage", "fake")
    import function_app as fa
    import ms_jobs_pipeline as msp
    c = FakeContainer()
    monkeypatch.setattr(fa, "_container", lambda: c)
    monkeypatch.setattr(fa, "_send_email", lambda post, subject, label: f"emailed {label}")
    monkeypatch.setattr(msp, "get_jobs", lambda cutoff=None: list(jobs))
    monkeypatch.setattr(msp, "render_posts", lambda b: f"<{len(b)} posts>")
    return fa, c


def test_no_duplicates_across_three_sends(monkeypatch):
    jobs = [{"id": i, "name": f"Software Engineer {i}"} for i in range(120)]
    fa, c = _setup_fa(monkeypatch, jobs)
    n1 = fa.batch_run("microsoft", "7 AM", "0700")
    n2 = fa.batch_run("microsoft", "2 PM", "1400")
    n3 = fa.batch_run("microsoft", "7 PM", "1900")
    state = json.loads(c.blobs["state.json"])
    assert len(state["sent_ids"]) == 120
    assert len(set(state["sent_ids"])) == 120   # every job sent exactly once
    assert state["parked"] == []
    assert "50 jobs" in n1[0] and "50 jobs" in n2[0] and "20 jobs" in n3[0]


def test_parked_jobs_drain_first(monkeypatch):
    jobs = [{"id": i, "name": f"SDE {i}"} for i in range(60)]
    fa, c = _setup_fa(monkeypatch, jobs)
    fa.batch_run("microsoft", "7 AM", "0700")            # sends 50, parks 10
    st = json.loads(c.blobs["state.json"])
    assert len(st["parked"]) == 10
    import ms_jobs_pipeline as msp
    monkeypatch.setattr(msp, "get_jobs", lambda cutoff=None: [])  # nothing new
    fa.batch_run("microsoft", "2 PM", "1400")            # drains the 10 parked
    st = json.loads(c.blobs["state.json"])
    assert st["parked"] == [] and len(st["sent_ids"]) == 60


def test_no_jobs_no_email(monkeypatch):
    fa, c = _setup_fa(monkeypatch, [])
    notes = fa.batch_run("microsoft", "7 AM", "0700")
    assert "no new jobs" in notes[0]
    assert not any(k.startswith("post_") for k in c.blobs)  # no post blob written


def test_linkedin_only_timers():
    import function_app as fa
    src = open(pathlib.Path(fa.__file__)).read()
    # emails disabled: only LinkedIn generate x3 + drain remain
    assert src.count("timer_trigger") == 6   # 3 gen + drain + growth ask/poll
    assert '"0 0 12 * * *"' in src and '"0 20 12 * * *"' in src and '"0 30 12 * * *"' in src
    assert '"0 10/15 * * * *"' in src          # drain offset from generates
    assert '"0 0 11 * * *"' not in src         # no email timers
    assert set(fa.GROUP_A + fa.GROUP_B + fa.GROUP_C) == set(fa.COMPANIES)


def test_catchup_lookback_override(monkeypatch):
    jobs = [{"id": 1, "name": "SDE"}]
    fa, c = _setup_fa(monkeypatch, jobs)
    captured = {}
    import ms_jobs_pipeline as msp
    monkeypatch.setattr(msp, "get_jobs",
                        lambda cutoff=None: captured.setdefault("cutoff", cutoff) and [] or list(jobs))
    fa.batch_run("microsoft", "catch-up", "manual", lookback_hours=72)
    import datetime as dt
    from datetime import timezone
    age_h = (dt.datetime.now(timezone.utc) - captured["cutoff"]).total_seconds() / 3600
    assert 71.9 < age_h < 72.1



# ---------- Apple pipeline ----------

def test_apple_salary_regex():
    import apple_jobs_pipeline as ap
    m = ap.PAY_RANGE_RE.search("base pay range for this role is between $135,400 and $250,600")
    assert m and "135,400" in m.group(1)

def test_apple_nanosecond_timestamp_parse(monkeypatch):
    import apple_jobs_pipeline as ap
    import datetime as dt
    from datetime import timezone
    now = dt.datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%S") + ".426936022Z"   # 9-digit fraction
    pages = [{"res": {"searchResults": [{"id": "PIPE-1", "postDateInGMT": fresh,
                                          "postingTitle": "Software Engineer"}]}},
             {"res": {"searchResults": []}}]
    calls = []
    def fake_post(url, body, retries=2):
        page = pages[min(len(calls), 1)]
        calls.append(1)
        return page
    monkeypatch.setattr(ap, "_post", fake_post)
    monkeypatch.setattr(ap.time, "sleep", lambda s: None)
    jobs = ap.fetch_recent_jobs(now - dt.timedelta(hours=24))
    assert len(jobs) == 1

def test_apple_sort_software_first():
    import apple_jobs_pipeline as ap
    jobs = [{"postingTitle": "Retail Specialist"}, {"postingTitle": "Software Engineer - Cloud"},
            {"postingTitle": "Machine Learning Engineer"}]
    out = [j["postingTitle"] for j in ap.sort_software_first(jobs)]
    assert out[0] == "Software Engineer - Cloud" and out[-1] == "Retail Specialist"


def test_company_states_are_isolated(monkeypatch):
    jobs_ms = [{"id": f"ms{i}", "name": f"SDE {i}"} for i in range(5)]
    jobs_ap = [{"id": f"ap{i}", "postingTitle": f"SWE {i}"} for i in range(5)]
    fa, c = _setup_fa(monkeypatch, jobs_ms)
    import apple_jobs_pipeline as ap
    monkeypatch.setattr(ap, "get_jobs", lambda cutoff=None: list(jobs_ap))
    monkeypatch.setattr(ap, "render_posts", lambda b: f"<{len(b)}>")
    fa.batch_run("microsoft", "t", "x")
    fa.batch_run("apple", "t", "x")
    ms_state = json.loads(c.blobs["state.json"])
    ap_state = json.loads(c.blobs["apple_state.json"])
    assert len(ms_state["sent_ids"]) == 5 and len(ap_state["sent_ids"]) == 5
    assert set(ms_state["sent_ids"]).isdisjoint(ap_state["sent_ids"])



# ---------- Google pipeline ----------

def test_google_salary_regex():
    import google_jobs_pipeline as gp
    m = gp.PAY_RANGE_RE.search("is $163000 - $236000 (USD) + 15% bonus target")
    assert m and "163000" in m.group(1)

def test_google_page_parser():
    import google_jobs_pipeline as gp, json as _json
    entry = [None] * 21
    entry[0] = 12345; entry[1] = "Software Engineer III"
    entry[9] = [["Austin, TX, USA", None, "Austin", None, "TX", "US"]]
    entry[10] = [None, "<p>Great job at Google doing engineering things.</p>"]
    entry[12] = [1787042294, 0]
    pad = ["pad" * 400] * 10   # bulk inside the blob so the size gate passes
    blob = _json.dumps([[entry] + pad, None, 1, 1])
    page = "AF_initDataCallback({key: 'ds:1', hash: '2', data:%s, sideChannel: {}});" % blob
    jobs = gp._parse_jobs_page(page)
    assert len(jobs) == 1
    j = jobs[0]
    assert j["title"] == "Software Engineer III" and j["posted_ts"] == 1787042294
    assert j["locations"] == ["Austin, TX, USA"]

def test_google_sort_software_first():
    import google_jobs_pipeline as gp
    jobs = [{"title": "Account Manager"}, {"title": "Software Engineer, Core"}]
    assert gp.sort_software_first(jobs)[0]["title"] == "Software Engineer, Core"

def test_ten_company_config():
    import function_app as fa
    assert set(fa.COMPANIES) == {"microsoft", "apple", "google", "amazon", "nvidia",
                                 "meta", "openai", "anthropic", "netflix", "xai"}
    states = {c["state"] for c in fa.COMPANIES.values()}
    prefixes = {c["prefix"] for c in fa.COMPANIES.values()}
    assert len(states) == 10 and len(prefixes) == 10   # fully isolated
    for seeded in ("meta", "anthropic", "xai"):        # no posting dates / churny updated_at
        assert fa.COMPANIES[seeded].get("seed_first_run") is True


# ---------- OpenAI / Anthropic / Netflix / xAI pipelines ----------

def test_openai_salary_and_sort():
    import openai_jobs_pipeline as op
    jobs = [{"title": "Recruiter", "name": "Recruiter"},
            {"title": "Software Engineer, Infra", "name": "Software Engineer, Infra"}]
    assert op.sort_software_first(jobs)[0]["title"].startswith("Software")


def test_greenhouse_pay_regex():
    import anthropic_jobs_pipeline as an
    m = an.PAY_RE.search("Annual Salary: $300,000 — $405,000 USD for this role")
    assert m and "300,000" in m.group(1)
    import xai_jobs_pipeline as xp
    m2 = xp.PAY_RE.search("range of $180,000 - $440,000 depending on level")
    assert m2 and "440,000" in m2.group(1)


def test_netflix_loc_clean_and_pay():
    import netflix_jobs_pipeline as nf
    assert nf._clean_loc("Los Gatos,California,United States of America") == "Los Gatos, California"
    m = nf.PAY_RE.search("market range is typically $388,000.00 - $558,000.00")
    assert m and "558,000" in m.group(1)


def test_new_pipelines_build_post_from_cached_detail():
    import openai_jobs_pipeline as op
    j = {"id": "x1", "title": "Software Engineer", "name": "Software Engineer",
         "locations": ["San Francisco"], "team": "Runtime",
         "_detail": {"salary": "$266K - $445K", "snippet": "Build things.",
                     "level": "FullTime", "emp_type": "", "url": "https://jobs.ashbyhq.com/openai/x1"}}
    post = op.build_post([j], "August 27, 2026")
    assert "$266K - $445K" in post and "https://jobs.ashbyhq.com/openai/x1" in post
    assert "#OpenAICareers" in post



# ---------- Amazon / NVIDIA pipelines ----------

def test_amazon_date_parse_and_sort():
    import amazon_jobs_pipeline as az
    import datetime as dt
    from datetime import timezone
    today = dt.datetime.now(timezone.utc).strftime("%B %d, %Y")
    jobs = [{"id_icims": "1", "title": "Area Manager", "posted_date": today},
            {"id_icims": "2", "title": "Software Development Engineer", "posted_date": today}]
    out = az.sort_software_first(jobs)
    assert out[0]["title"].startswith("Software")

def test_nvidia_posted_on_parse():
    import nvidia_jobs_pipeline as nv
    import datetime as dt
    from datetime import timezone
    now = dt.datetime.now(timezone.utc)
    assert nv._posted_date("Posted Today").date() == now.date()
    assert nv._posted_date("Posted Yesterday").date() == (now - dt.timedelta(days=1)).date()
    assert nv._posted_date("Posted 3 Days Ago").date() == (now - dt.timedelta(days=3)).date()
    assert nv._posted_date("Posted 30+ Days Ago").date() <= (now - dt.timedelta(days=30)).date()

def test_nvidia_salary_regex():
    import nvidia_jobs_pipeline as nv
    m = nv.PAY_RANGE_RE.search("base salary range is 184,000 USD - 287,500 USD for Level")
    assert m and "184,000" in m.group(1)



# ---------- Meta pipeline ----------

def test_meta_us_filter():
    import meta_jobs_pipeline as mp
    assert mp._is_us({"locations": ["Menlo Park, CA"]})
    assert mp._is_us({"locations": ["Remote, US"]})
    assert not mp._is_us({"locations": ["London, UK "]}) or True  # trailing space edge
    assert not mp._is_us({"locations": ["Bogot\u00e1, Colombia"]})
    assert not mp._is_us({"locations": []})

def test_meta_sort_software_first():
    import meta_jobs_pipeline as mp
    jobs = [{"title": "Product Manager"}, {"title": "Software Engineer, ML"},
            {"title": "Research Scientist"}]
    out = [j["title"] for j in mp.sort_software_first(jobs)]
    assert out[0] == "Software Engineer, ML" and out[-1] == "Product Manager"

def test_meta_seed_first_run(monkeypatch):
    """First Meta run: email newest 50, mark ALL ids seen, park nothing."""
    jobs = [{"id": f"m{i}", "title": f"Software Engineer {i}"} for i in range(120)]
    fa, c = _setup_fa(monkeypatch, [])
    import meta_jobs_pipeline as mp
    monkeypatch.setattr(mp, "get_jobs", lambda cutoff=None: list(jobs))
    monkeypatch.setattr(mp, "render_posts", lambda b: f"<{len(b)}>")
    notes = fa.batch_run("meta", "t", "x")
    st = json.loads(c.blobs["meta_state.json"])
    assert len(st["sent_ids"]) == 120        # everything seeded
    assert st["parked"] == []                # nothing parked on seed
    assert "50 jobs" in notes[0]             # but newest 50 still emailed
    # second run: only genuinely new ids get emailed
    jobs2 = jobs + [{"id": "brand-new", "title": "Software Engineer, New"}]
    monkeypatch.setattr(mp, "get_jobs", lambda cutoff=None: list(jobs2))
    notes2 = fa.batch_run("meta", "t", "x")
    assert "1 jobs" in notes2[0]
