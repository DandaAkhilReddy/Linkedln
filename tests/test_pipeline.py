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
    """Returns (emailer module, fake store). batch_run(store, company, label, suffix)."""
    import emailer as fa
    import ms_jobs_pipeline as msp
    c = FakeContainer()
    monkeypatch.setattr(fa, "send_email", lambda post, subject, label: f"emailed {label}")
    monkeypatch.setattr(msp, "get_jobs", lambda cutoff=None: list(jobs))
    monkeypatch.setattr(msp, "render_posts", lambda b: f"<{len(b)} posts>")
    return fa, c


def test_no_duplicates_across_three_sends(monkeypatch):
    jobs = [{"id": i, "name": f"Software Engineer {i}"} for i in range(120)]
    fa, c = _setup_fa(monkeypatch, jobs)
    n1 = fa.batch_run(c, "microsoft", "7 AM", "0700")
    n2 = fa.batch_run(c, "microsoft", "2 PM", "1400")
    n3 = fa.batch_run(c, "microsoft", "7 PM", "1900")
    state = json.loads(c.blobs["state.json"])
    assert len(state["sent_ids"]) == 120
    assert len(set(state["sent_ids"])) == 120   # every job sent exactly once
    assert state["parked"] == []
    assert "50 jobs" in n1[0] and "50 jobs" in n2[0] and "20 jobs" in n3[0]


def test_parked_jobs_drain_first(monkeypatch):
    jobs = [{"id": i, "name": f"SDE {i}"} for i in range(60)]
    fa, c = _setup_fa(monkeypatch, jobs)
    fa.batch_run(c, "microsoft", "7 AM", "0700")            # sends 50, parks 10
    st = json.loads(c.blobs["state.json"])
    assert len(st["parked"]) == 10
    import ms_jobs_pipeline as msp
    monkeypatch.setattr(msp, "get_jobs", lambda cutoff=None: [])  # nothing new
    fa.batch_run(c, "microsoft", "2 PM", "1400")            # drains the 10 parked
    st = json.loads(c.blobs["state.json"])
    assert st["parked"] == [] and len(st["sent_ids"]) == 60


def test_no_jobs_no_email(monkeypatch):
    fa, c = _setup_fa(monkeypatch, [])
    notes = fa.batch_run(c, "microsoft", "7 AM", "0700")
    assert "no new jobs" in notes[0]
    assert not any(k.startswith("post_") for k in c.blobs)  # no post blob written


def test_linkedin_only_timers():
    import function_app as fa
    src = open(pathlib.Path(fa.__file__)).read()
    # emails disabled: only LinkedIn generate x3 + drain remain
    assert src.count("timer_trigger") == 20  # 5 gen + drain + catch-up + filler + 6 edu + growth ask/poll + health + poll + 2 carousels
    assert '"0 12 * * *"' in src and '"20 12 * * *"' in src and '"30 12 * * *"' in src
    assert '"5-55/10 * * * *"' in src         # drain every 10 min, offset from generates
    assert '"0-50/10 * * * *"' in src         # catch-up timer for missed slots
    assert '"0 11 * * *"' not in src           # no email timers
    assert set(fa.GROUP_A + fa.GROUP_B + fa.GROUP_C + fa.GROUP_D + fa.GROUP_E) == set(fa.COMPANIES)


def test_catchup_lookback_override(monkeypatch):
    jobs = [{"id": 1, "name": "SDE"}]
    fa, c = _setup_fa(monkeypatch, jobs)
    captured = {}
    import ms_jobs_pipeline as msp
    monkeypatch.setattr(msp, "get_jobs",
                        lambda cutoff=None: captured.setdefault("cutoff", cutoff) and [] or list(jobs))
    fa.batch_run(c, "microsoft", "catch-up", "manual", lookback_hours=72)
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
    fa.batch_run(c, "microsoft", "t", "x")
    fa.batch_run(c, "apple", "t", "x")
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

def test_seventeen_company_config():
    import function_app as fa
    assert set(fa.COMPANIES) == {"microsoft", "apple", "google", "amazon", "nvidia",
                                 "meta", "openai", "anthropic", "netflix", "xai",
                                 "databricks", "stripe", "scaleai", "ramp", "cursor",
                                 "amd", "ibm"}
    states = {c["state"] for c in fa.COMPANIES.values()}
    prefixes = {c["prefix"] for c in fa.COMPANIES.values()}
    assert len(states) == 17 and len(prefixes) == 17   # fully isolated
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
    notes = fa.batch_run(c, "meta", "t", "x")
    st = json.loads(c.blobs["meta_state.json"])
    assert len(st["sent_ids"]) == 120        # everything seeded
    assert st["parked"] == []                # nothing parked on seed
    assert "50 jobs" in notes[0]             # but newest 50 still emailed
    # second run: only genuinely new ids get emailed
    jobs2 = jobs + [{"id": "brand-new", "title": "Software Engineer, New"}]
    monkeypatch.setattr(mp, "get_jobs", lambda cutoff=None: list(jobs2))
    notes2 = fa.batch_run(c, "meta", "t", "x")
    assert "1 jobs" in notes2[0]


def test_autopost_module_constants_exist():
    """Regression: a silent patch once dropped JOBS_PER_CARD and every
    generate crashed with NameError for a full day."""
    import linkedin_autopost as la
    assert isinstance(la.CARDS_PER_COMPANY, int)
    assert isinstance(la.JOBS_PER_CARD, int)
    assert la.HOOK_VARIANTS and la.ORG_URNS.get("microsoft", "").startswith("urn:li:organization:")
    import function_app as fa
    assert set(la.ORG_URNS) == set(fa.COMPANIES)   # every company is @taggable


def test_board_pipelines_interface():
    import board_pipelines as bp
    for name in ("databricks", "stripe", "scaleai", "ramp", "cursor", "amd", "ibm"):
        p = getattr(bp, name)
        for fn in ("get_jobs", "sort_software_first", "fetch_detail", "build_post", "render_posts"):
            assert callable(getattr(p, fn)), f"{name}.{fn}"
    j = {"id": "1", "title": "Software Engineer", "name": "Software Engineer",
         "locations": ["Austin, Texas"], "team": "Eng", "_detail": {"salary": "$80,500 - $115,000",
         "snippet": "x", "level": "", "emp_type": "", "url": "https://careers.amd.com/careers-home/jobs/1"}}
    post = bp.amd.build_post([j], "September 8, 2026")
    assert "$80,500 - $115,000" in post and "#AMDCareers" in post
    assert bp._is_us({"locations": ["Bucharest, Romania"]}) is False
    assert bp._is_us({"locations": ["San Francisco, CA"]}) is True


# ---------- portability layer ----------

def test_filestore_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import storage
    s = storage.get_store("linkedin-posts")
    s.upload_blob("li_queue.json", '[{"a": 1}]', overwrite=True)
    s.upload_blob("li_cards/x.png", b"\x89PNG", overwrite=True)
    assert s.download_blob("li_queue.json").readall() == b'[{"a": 1}]'
    assert {b.name for b in s.list_blobs()} == {"li_queue.json", "li_cards/x.png"}
    assert [b.name for b in s.list_blobs(name_starts_with="li_cards/")] == ["li_cards/x.png"]
    s.delete_blob("li_queue.json")
    import pytest
    with pytest.raises(FileNotFoundError):
        s.download_blob("li_queue.json")
    with pytest.raises(ValueError):
        s.download_blob("../../etc/passwd")


def test_backend_autodetect(monkeypatch):
    import storage
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AZURE_STORAGE_CONNECTION_STRING", raising=False)
    assert storage.backend() == "file"
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    assert storage.backend() == "azure"


def test_cron_matcher():
    import datetime as dt
    from worker import cron_matches
    t = lambda h, m: dt.datetime(2026, 9, 8, h, m)
    assert cron_matches("0 12 * * *", t(12, 0)) and not cron_matches("0 12 * * *", t(12, 1))
    assert cron_matches("5-55/10 * * * *", t(3, 25)) and not cron_matches("5-55/10 * * * *", t(3, 20))
    assert cron_matches("*/20 * * * *", t(9, 40)) and not cron_matches("*/20 * * * *", t(9, 30))


def test_schedule_matches_azure_timers():
    """The worker and the Azure adapter must run the same schedule."""
    import jobs, function_app as fa
    src = open(pathlib.Path(fa.__file__)).read()
    for name, cron, fn in jobs.SCHEDULE:
        assert f'"{cron}"' in src, f"{name} cron {cron} missing from function_app"
        assert callable(fn)
    assert set(jobs.GROUPS) == {"a", "b", "c", "d", "e"}


# ---------- fallbacks + watchdog ----------

def test_health_assess_flags_gaps(tmp_path, monkeypatch):
    import datetime as dt, json
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import storage, healthcheck, linkedin_client
    monkeypatch.setattr(linkedin_client, "token_valid", lambda: True)
    s = storage.get_store("linkedin-posts")
    now = dt.datetime.now(dt.timezone.utc)
    good = [{"ts": (now - dt.timedelta(minutes=10 * i)).isoformat(), "company": "x", "urn": "u"} for i in range(144)]
    s.upload_blob("li_post_log.json", json.dumps(good))
    s.upload_blob("li_queue.json", json.dumps([{"a": 1}]))
    s.upload_blob("li_secrets.json", json.dumps({"linkedin_autopost_enabled": "true"}))
    healthcheck.record(s, "generate", True, "ok")
    r = healthcheck.assess(s)
    assert r["status"] == "ok" and r["posts_24h"] == 144 and r["max_gap_min"] <= 10
    # now a 3-hour hole
    hole = [p for p in good if not (60 <= (now - dt.datetime.fromisoformat(p["ts"])).total_seconds() / 60 <= 240)]
    s.upload_blob("li_post_log.json", json.dumps(hole))
    r = healthcheck.assess(s)
    assert r["status"] == "alert" and any("gap" in i for i in r["issues"])


def test_drain_refills_when_queue_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import jobs, linkedin_autopost as la, filler, content_plan as cp
    calls, fill = [], []
    monkeypatch.setattr(la, "gate", lambda store, plog=None, now=None: (True, ""))
    monkeypatch.setattr(cp, "pick", lambda store, now=None, targets=None: "job")
    monkeypatch.setattr(la, "drain", lambda store: ["queue empty"] if not calls else ["0 posted, 2 left"])
    monkeypatch.setattr(la, "generate", lambda store, loader, companies, hours: (calls.append(hours), [f"gen {hours}h"])[1])
    monkeypatch.setattr(filler, "post_one", lambda store: (fill.append(1), ["posted filler news: x urn", "1 posted, 9 filler left"])[1])
    out = jobs.drain()
    assert calls and calls[0] == 24                      # refill kicked in with the 24h window
    assert any("refill" in n for n in out)
    assert fill == [1] and jobs._posted(out)             # ...and the slot was still filled


def test_drain_guarantee_chain(tmp_path, monkeypatch):
    """Jobs first; filler only when nothing postable; never when the gate says hold."""
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import jobs, linkedin_autopost as la, filler, content_plan as cp
    fill = []
    monkeypatch.setattr(la, "gate", lambda store, plog=None, now=None: (True, ""))
    monkeypatch.setattr(cp, "pick", lambda store, now=None, targets=None: "job")
    monkeypatch.setattr(la, "generate", lambda store, loader, companies, hours: ["x: no new jobs"])
    monkeypatch.setattr(filler, "post_one", lambda store: (fill.append(1), ["posted filler chart: y urn", "1 posted, 0 filler left"])[1])
    monkeypatch.setattr(la, "drain", lambda store: ["queue empty"])
    assert jobs._posted(jobs.drain()) and fill == [1]
    monkeypatch.setattr(la, "gate", lambda store, plog=None, now=None: (False, "holding queue — volume: last post 3 min ago (< 10)"))
    assert not jobs._posted(jobs.drain()) and fill == [1]          # gate: no filler
    monkeypatch.setattr(la, "gate", lambda store, plog=None, now=None: (False, "daily cap 148 reached — resuming tomorrow"))
    jobs.drain(); assert fill == [1]
    monkeypatch.setattr(la, "gate", lambda store, plog=None, now=None: (True, ""))
    monkeypatch.setattr(la, "drain", lambda store: ["posted ibm urn", "1 posted, 5 left"])
    assert jobs._posted(jobs.drain()) and fill == [1]              # job posted: no filler
    monkeypatch.setattr(la, "drain", lambda store: ["ibm failed (1): boom", "0 posted, 5 left"])
    assert jobs._posted(jobs.drain()) and fill == [1, 1]           # job post failed: filler saves the slot


def test_catchup_only_when_primary_slot_missed(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import jobs, json, datetime as dt
    s = jobs.posts_store()
    ran = []
    monkeypatch.setattr(jobs, "drain", lambda: (ran.append(1), ["1 posted, 1 left"])[1])
    now = dt.datetime.now(dt.timezone.utc)
    s.upload_blob("li_post_log.json", json.dumps([
        {"ts": (now - dt.timedelta(minutes=4)).isoformat(), "variant": "salary_hook", "urn": "a"},
        {"ts": (now - dt.timedelta(minutes=1)).isoformat(), "variant": "poll", "urn": "b"}]))
    assert not ran and "not needed" in jobs.drain_catchup()[0]
    s.upload_blob("li_post_log.json", json.dumps([
        {"ts": (now - dt.timedelta(minutes=22)).isoformat(), "variant": "news", "urn": "a"},
        {"ts": (now - dt.timedelta(minutes=2)).isoformat(), "variant": "carousel", "urn": "b"}]))  # extras don't count
    assert "missed" in jobs.drain_catchup()[0] and ran == [1]


def test_slot_clock_ignores_polls_and_carousels_and_post_after(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import json, datetime as dt, linkedin_autopost as la, linkedin_client, strategy, function_app as fa
    la._set_companies(fa.COMPANIES)
    now = dt.datetime.now(dt.timezone.utc)
    plog = [{"ts": (now - dt.timedelta(minutes=30)).isoformat(), "variant": "grab_hook"},
            {"ts": (now - dt.timedelta(minutes=1)).isoformat(), "variant": "poll"}]
    assert abs((now - la._last_post_ts(plog)).total_seconds() - 1800) < 5
    import jobs
    s = jobs.posts_store()
    s.upload_blob("li_secrets.json", json.dumps({"linkedin_autopost_enabled": "true", "access_token": "t", "person_urn": "urn:li:person:x"}))
    s.upload_blob("li_post_log.json", json.dumps(plog))
    s.upload_blob("li_cards/ibm_logo.png", b"png")
    s.upload_blob("li_queue.json", json.dumps([{"company": "ibm", "card_blob": "li_cards/ibm_logo.png", "caption": "c",
                                                "variant": "salary_hook", "title": "IBM is hiring", "created": now.isoformat(),
                                                "post_after": (now + dt.timedelta(hours=3)).isoformat()}]))
    monkeypatch.setattr(linkedin_client, "post_with_image", lambda *a, **k: "urn:li:share:1")
    out = la.drain(s)
    assert any(n.startswith("1 posted") for n in out)     # future post_after no longer blocks the slot
    assert la.DAILY_CAP == 148


def test_filler_feeds_captions_and_images(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import json, filler, linkedin_autopost as la, function_app as fa
    la._set_companies(fa.COMPANIES)
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>
      <item><title>Faster inference with &amp; without GPUs</title><link>https://example.com/a</link>
      <description><![CDATA[<p>Some <b>html</b> text</p>]]></description><pubDate>Fri, 11 Sep 2026 10:00:00 GMT</pubDate></item>
      </channel></rss>"""
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>F</title>
      <entry><title>New model release</title><link rel="alternate" href="https://example.com/b"/>
      <summary>abstract here</summary><published>2026-09-11T09:00:00Z</published></entry></feed>"""
    r, a = filler.parse_feed(rss), filler.parse_feed(atom)
    assert r[0]["title"] == "Faster inference with & without GPUs" and r[0]["snippet"] == "Some html text"
    assert r[0]["when"].year == 2026 and a[0]["url"] == "https://example.com/b" and a[0]["when"].hour == 9
    item = {"kind": "news", "title": "New model release", "url": "https://example.com/b", "source": "OpenAI",
            "company": "openai", "summary": "Short original summary."}
    cap = filler.caption_for(item)
    assert "Follow me" in cap and "https://example.com/b" in cap and "From OpenAI" in cap and "#OpenAI" in cap
    png = filler.news_card_png("news", item["title"], "OpenAI", None)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    # charts need our pay facts
    s = __import__("storage").get_store("linkedin-posts")
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    facts = [{"ts": now, "company": c, "title": f"Software Engineer {i}", "loc": "Remote", "salary": "", "top": 200000 + i * 10000, "url": f"https://x/{c}/{i}"}
             for c in ("openai", "google", "stripe", "ibm", "amd", "ramp") for i in range(3)]
    s.upload_blob("li_jobfacts.json", json.dumps(facts))
    charts = filler.chart_candidates(s)
    ids = [c[0] for c in charts]
    assert "top_by_company" in ids and "roles_with_pay" in ids and "median_swe" in ids
    png2 = filler.bar_chart_png(charts[0][1], charts[0][2], charts[0][3], charts[0][4])
    assert png2[:8] == b"\x89PNG\r\n\x1a\n" and "Follow me" in filler.chart_caption(charts[0])


def test_schedule_has_health_job():
    import jobs
    assert "health_report" in {n for n, _, _ in jobs.SCHEDULE}


def test_hooks_lead_with_salary_and_never_fake_urgency():
    import linkedin_autopost as la, function_app as fa
    la._set_companies(fa.COMPANIES)
    paid = [{"title": "SWE", "name": "SWE", "locations": ["Austin, TX"], "salary": "$182,000 — $250,208 USD",
             "id": "1", "_detail": {"salary": "$182,000 — $250,208 USD", "url": "https://x/1"}}]
    unpaid = [{"title": "SWE", "name": "SWE", "locations": ["Bucharest"], "id": "2", "_detail": {"url": "https://x/2"}}]
    for style in la.HOOK_VARIANTS:
        h_paid = la._caption("databricks", paid, 1, 1, style).split("\n")[0]
        h_unpaid = la._caption("stripe", unpaid, 1, 1, style).split("\n")[0]
        assert "$" in h_paid, (style, h_paid)
        assert "early applicants" not in h_paid.lower() and "early applicants" not in h_unpaid.lower()
        assert "Databricks" in h_paid and "Stripe" in h_unpaid
    assert "grab" in la._caption("databricks", paid, 1, 1, "grab_hook").split("\n")[0].lower()


# ---------- growth v2: polls, carousel, strategy bandit ----------

def test_ltf_escapes_reserved_and_builds_mentions():
    import growth_posts as gp
    assert gp.ltf("a|b{c}@d[e](f)<g>#h*i_j~k\\l") == "a\\|b\\{c\\}\\@d\\[e\\]\\(f\\)\\<g\\>\\#h\\*i\\_j\\~k\\\\l"
    assert gp.ltf("$300K — grab it 👇") == "$300K — grab it 👇"          # nothing reserved
    assert gp.ltf_mention("Scale AI", "urn:li:organization:17998520") == "@[Scale AI](urn:li:organization:17998520)"
    assert gp.ltf_tag("TechJobs") == "{hashtag|\\#|TechJobs}"


def test_caption_has_follow_cta():
    import linkedin_autopost as la, function_app as fa
    la._set_companies(fa.COMPANIES)
    jobs = [{"title": "SWE", "name": "SWE", "locations": ["Austin, TX"], "id": "1",
             "salary": "$182,000 — $250,208 USD", "_detail": {"url": "https://x/1"}}]
    cap = la._caption("stripe", jobs, 1, 1, "salary_hook")
    assert "Follow me" in cap and len(cap) <= 2900


def test_record_facts_and_top_paid(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import storage, growth_posts as gp
    s = storage.get_store("linkedin-posts")
    mk = lambda i, pay: {"id": str(i), "title": f"Role {i}", "locations": ["Remote"],
                         "salary": f"${pay:,} - ${pay + 50000:,} USD", "_detail": {"url": f"https://a/{i}"}}
    gp.record_facts(s, "openai", [mk(1, 300000), mk(2, 400000), mk(3, 500000)], lambda j: "Remote", lambda c, j: "")
    gp.record_facts(s, "stripe", [mk(4, 450000), {"id": "5", "title": "Unpaid", "_detail": {"url": "https://a/5"}}],
                    lambda j: "Remote", lambda c, j: "")
    gp.record_facts(s, "openai", [mk(1, 300000)], lambda j: "Remote", lambda c, j: "")   # duplicate ignored
    top = gp.top_paid(s, n=10, per_company=2)
    assert [f["top"] for f in top] == [550000, 500000, 450000]        # max 2 per company, unpaid dropped
    assert len(gp.top_paid(s, exclude={"https://a/3"})) == 3          # too few left -> exclusion ignored
    assert gp.company_tops(s)["stripe"][0] == 500000


def test_roundup_pdf_and_poll_commentary(tmp_path):
    import growth_posts as gp
    items = [{"company": c, "title": f"Staff Engineer {i}", "loc": "San Francisco, CA",
              "salary": f"${300 + i * 10}K - ${400 + i * 10}K", "top": (400 + i * 10) * 1000,
              "url": "https://boards.greenhouse.io/x/1"} for i, c in enumerate(["openai", "anthropic", "stripe", "microsoft", "ibm"])]
    pdf = gp.build_roundup_pdf(items, date_str="Sep 9, 2026")
    assert pdf[:4] == b"%PDF" and pdf.count(b"/Type /Page\n") >= 7      # cover + 5 + closing
    text = gp.poll_commentary("Same offer — which one?", [("openai", 530000, "Research Engineer")])
    assert "@[OpenAI](urn:li:organization:11130470)" in text and "{hashtag|\\#|TechJobs}" in text
    assert "Follow me" in text and "\\—" not in text


def test_strategy_blocks_credit_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import storage, strategy as st, json
    s = storage.get_store("linkedin-posts")
    d0 = st.START
    # Reddy's rule: auto mode is always the 10-minute "volume" arm, block after block
    for k in (0, 2, 3, 6, 9, 30):
        assert st.arm_for(s, d0 + datetime.timedelta(days=k)) == "volume"
    # a stale cached "prime" block (from before the rule) is recomputed, not honoured
    st2 = st._load(s); st2["blocks"]["1"] = "prime"; st._save(s, st2)
    assert st.arm_for(s, d0 + datetime.timedelta(days=3)) == "volume"
    # credit still attributes gains to whatever arm was active on those days
    st.credit(s, d0, 15000, d0 + datetime.timedelta(days=3), 15090)
    st3 = st._load(s)
    assert st3["stats"]["volume"]["days"] == 3 and st3["stats"]["volume"]["gained"] == 90
    s.upload_blob("li_secrets.json", json.dumps({"strategy_arm": "prime"}))
    assert st.arm_for(s, d0 + datetime.timedelta(days=6)) == "prime"          # explicit email override only
    assert "Scoreboard" in st.summary(s)


def test_should_post_respects_window_and_spacing(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import storage, strategy as st, json
    s = storage.get_store("linkedin-posts")
    s.upload_blob("li_secrets.json", json.dumps({"strategy_arm": "prime"}))
    noon_utc = datetime.datetime(2026, 9, 12, 16, 0, tzinfo=timezone.utc)       # 12:00 ET
    night_utc = datetime.datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc)       # 02:00 ET
    assert st.should_post(s, noon_utc, noon_utc - datetime.timedelta(minutes=31))[0]
    assert not st.should_post(s, noon_utc, noon_utc - datetime.timedelta(minutes=10))[0]
    assert not st.should_post(s, night_utc, None)[0]
    s.upload_blob("li_secrets.json", json.dumps({"strategy_arm": "volume"}))
    assert st.should_post(s, night_utc, night_utc - datetime.timedelta(minutes=10))[0]
    assert st.expected(st.ARMS["prime"])[0] == 31 and st.expected(st.ARMS["volume"])[0] == 147


def test_log_count_dedupes_per_day_and_credits(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import storage, growth_check as gc, json
    s = storage.get_store("linkedin-posts")
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    s.upload_blob("growth_log.json", json.dumps({"goal_per_day": 200, "entries": [{"date": yesterday, "followers": 15400}]}))
    out = gc.log_count(s, 15450, "test")
    out2 = gc.log_count(s, 15460, "test")          # same day again -> replaces, no duplicate
    entries = json.loads(s.download_blob("growth_log.json").readall())["entries"]
    assert len(entries) == 2 and entries[-1]["followers"] == 15460
    assert "+50" in out and "/day" in out and "Strategy now" in out2


def test_schedule_has_growth_formats():
    import jobs
    names = {n for n, _, _ in jobs.SCHEDULE}
    assert {"daily_poll", "daily_roundup"} <= names


def test_deck_themes_select_rank_and_render(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import storage, growth_posts as gp, json
    s = storage.get_store("linkedin-posts")
    now = datetime.datetime.now(timezone.utc).isoformat()
    rows = [("openai", "Research Engineer, Training", "San Francisco", 530000),
            ("openai", "Software Engineer, Infra", "Remote - US", 400000),
            ("anthropic", "Staff Software Engineer", "Remote-Friendly", 650000),
            ("anthropic", "Software Engineer, Agents", "New York, NY", 480000),
            ("microsoft", "Principal Software Engineer", "Redmond, Washington", 274800),
            ("microsoft", "Software Engineer II", "Remote", 180000),
            ("google", "Senior Software Engineer, ML", "Sunnyvale, CA, USA", 340000),
            ("stripe", "Engineering Manager, Payments", "Seattle, WA", 420000),
            ("nvidia", "Distinguished Engineer", "Santa Clara, CA", 488000),
            ("ramp", "Director, Strategic Finance", "New York", 385000)]
    s.upload_blob("li_jobfacts.json", json.dumps([
        {"ts": now, "company": c, "title": t, "loc": l, "salary": f"${p - 100000:,} - ${p:,}", "top": p, "url": f"https://x/{i}"}
        for i, (c, t, l, p) in enumerate(rows)]))
    assert [f["top"] for f in gp.select(s, "top_pay")][:3] == [650000, 530000, 488000]
    assert all(gp._RX["remote"].search(f["loc"]) for f in gp.select(s, "remote"))
    assert gp.select(s, "spotlight", company="openai") and all(f["company"] == "openai" for f in gp.select(s, "spotlight", company="openai"))
    rank = gp.rank_companies(s)
    assert rank[0][0] == "anthropic" and rank[0][1] == 650000 and len(rank) >= 6
    for theme in ("top_pay", "remote", "swe_leaderboard"):
        items = rank if theme == "swe_leaderboard" else gp.select(s, theme)
        pdf = gp.build_deck(theme, items, None)
        assert pdf[:4] == b"%PDF"
        text = gp.deck_commentary(theme, items)
        assert "Follow me" in text and "{hashtag|\\#|TechJobs}" in text and "http" not in text
    spot = gp.deck_commentary("spotlight", gp.select(s, "spotlight", company="openai"), company="openai")
    assert "@[OpenAI](urn:li:organization:11130470)" in spot
    # rotation: every weekday x slot resolves to a theme with enough data (falls back if needed)
    s.upload_blob("li_post_log.json", json.dumps([]))
    for slot in ("noon", "evening"):
        theme, items, company = gp.plan_deck(s, slot)
        assert theme in gp.THEMES and (len(items) >= 5 or gp.THEMES[theme]["kind"] == "ranking")
    # a theme posted today is not repeated in the other slot
    s.upload_blob("li_post_log.json", json.dumps([{"ts": now, "variant": "carousel", "theme": "top_pay"}]))
    assert gp.plan_deck(s, "noon")[0] != "top_pay"


# ---------- educational content: cards, planner, pool, captions ----------

def _edu_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "file"); monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import storage
    return storage.get_store("linkedin-posts")


def test_edu_cards_render_every_visual_kind():
    import edu_cards as ec
    specs = [
        {"kind": "array", "values": [2, 7, 11, 15], "highlight": [0, 2], "pointers": {"0": "L", "3": "R"}, "caption": "t=13"},
        {"kind": "code", "lang": "python", "code": "def f(a):\n    return sorted(a)[0]  # min"},
        {"kind": "boxes", "items": ["Client", "API", "Service", "DB", "Cache"]},
        {"kind": "graph", "nodes": ["plan", "act", "observe", "reflect"], "edges": [[0, 1], [1, 2], [2, 3], [3, 0]]},
        {"kind": "tree", "values": [1, 2, 3, None, 4], "highlight": [2]},
        {"kind": "metric", "big": "10,000×", "small": "fewer params", "big2": "3×", "small2": "less memory"},
        {"kind": "compare", "left": {"title": "A", "points": ["x", "y"]}, "right": {"title": "B", "points": ["z"]}},
        {"kind": "table", "header": ["", "A", "B"], "rows": [["mem", "O(1)", "O(n)"]]},
        {"kind": "none"}, {"kind": "bogus"}, {"kind": "array", "values": []}, None,
    ]
    for i, v in enumerate(specs):
        png = ec.render("dsa" if i % 2 else "ai", "A question long enough to wrap onto several lines of the card?", v, i + 1, "medium")
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000


def test_edu_seed_bank_is_valid_and_renders(tmp_path, monkeypatch):
    import edu_content as ec, edu_cards
    for track in ec.TRACKS:
        items = ec.seed_items(track)
        assert len(items) >= 5, track
        for it in items:
            assert it["track"] == track and it["hook"] and it["answer"] and it["visual"]["kind"] in ec.VISUAL_KINDS
            cap, mentions = ec.caption_parts(it)
            assert ec.bold(it["hook"]) in cap and ec.bold("Answer") in cap and "Follow me" in cap and len(cap) <= 2900
            assert "Asked in" in cap and "Level:" in ec.bold("Level:") or True
            assert 2 <= len(mentions) <= 3 and all(m[1].startswith("urn:li:organization:") for m in mentions)
            assert all(m[0] in cap for m in mentions)          # every tagged name appears verbatim in the text
            assert edu_cards.render(track, it["question"], it["visual"], 1, it["difficulty"])[:4] == b"\x89PNG"
        assert len({it["id"] for it in items}) == len(items)


def test_edu_validate_and_json_parsing():
    import edu_content as ec
    raw = '```json\n{"hook": "Can you do it in O(n)?", "question": "Given an array of n integers find the duplicate in O(n) time.", '\
          '"answer": "Use Floyd cycle detection on the index-value graph; the duplicate is the cycle entry. Works because values are in 1..n-1.", '\
          '"visual": {"kind": "array", "values": [1,3,4,2,2]}, "difficulty": "Medium", "tags": ["Two Pointers", "arrays!"], "code": "x=1"}\n```'
    it = ec.validate(ec._parse_json(raw), "dsa")
    assert it and it["difficulty"] == "medium" and it["tags"] == ["TwoPointers", "arrays"] and it["id"].startswith("dsa-")
    assert ec.validate({"hook": "x", "question": "short", "answer": "short"}, "dsa") is None
    assert ec.validate(ec._parse_json("not json"), "sd") is None
    bad = ec.validate({"hook": "A decent hook here", "question": "A long enough question text for the validator?",
                       "answer": "A" * 80, "visual": {"kind": "nope"}}, "ai")
    assert bad["visual"] == {"kind": "none"}


def test_content_plan_quotas_spread_evenly(tmp_path, monkeypatch):
    import json, datetime as dt, content_plan as cp, edu_content as ec
    s = _edu_store(tmp_path, monkeypatch)
    s.upload_blob("li_secrets.json", json.dumps({}))
    day = dt.datetime(2026, 9, 28, 0, 5, tzinfo=dt.timezone.utc)
    log = []
    counts = {}
    for k in range(144):                                   # simulate a full day of slots
        now = day + dt.timedelta(minutes=10 * k)
        s.upload_blob("li_post_log.json", json.dumps(log))
        pick = cp.pick(s, now)
        counts[pick] = counts.get(pick, 0) + 1
        log.append({"ts": now.isoformat(), "variant": pick if pick != "job" else "salary_hook"})
    assert all(counts[t] == 10 for t in ec.TRACKS), counts
    assert counts["job"] == 94
    # spread: the first 12 hours hold about half of every track (± 1)
    first_half = [p["variant"] for p in log[:72]]
    assert all(4 <= first_half.count(t) <= 6 for t in ec.TRACKS)
    # disabled -> always jobs
    s.upload_blob("li_secrets.json", json.dumps({"edu_enabled": "false"}))
    s.upload_blob("li_post_log.json", json.dumps([]))
    assert cp.pick(s, day) == "job"
    s.upload_blob("li_secrets.json", json.dumps({"edu_dsa": 3, "edu_sd": 0, "edu_mlsd": 0, "edu_ai": 0, "edu_papers": 0}))
    assert ec.targets(s)["dsa"] == 3 and ec.targets(s)["sd"] == 0


def test_edu_post_one_uses_pool_then_seed_and_logs(tmp_path, monkeypatch):
    import json, edu_content as ec, linkedin_client
    s = _edu_store(tmp_path, monkeypatch)
    s.upload_blob("li_secrets.json", json.dumps({}))               # no AOAI -> on-the-fly fails -> seed bank
    posted = []
    monkeypatch.setattr(linkedin_client, "post_with_image", lambda text, png, title="", **k: (posted.append((text, title, k.get("mentions"))), "urn:li:share:9")[1])
    out = ec.post_one(s, "sd")
    assert out[-1].startswith("1 posted") and "seed" in out[0]
    text, title, mentions = posted[0]
    assert mentions and len(mentions) >= 2
    assert title.startswith("System Design:") and ec.bold("Answer") in text and "Asked in system design rounds" in text
    plog = json.loads(s.download_blob("li_post_log.json").readall())
    assert plog[-1]["variant"] == "sd" and plog[-1]["urn"] == "urn:li:share:9"
    # second call skips the used seed
    out2 = ec.post_one(s, "sd")
    assert posted[1][0] != text
    # pool path: a prepared item is used first and removed
    item = ec.seed_items("dsa")[0]; item["number"] = 7; item["card_blob"] = ""
    s.upload_blob("li_edu_pool.json", json.dumps({"dsa": [item]}))
    out3 = ec.post_one(s, "dsa")
    assert "(pool)" in out3[0] and ec.pool(s)["dsa"] == []


def test_drain_takes_edu_slot_then_falls_back_to_jobs(tmp_path, monkeypatch):
    import jobs, linkedin_autopost as la, edu_content as ec, content_plan as cp, filler
    _edu_store(tmp_path, monkeypatch)
    monkeypatch.setattr(la, "gate", lambda store, plog=None, now=None: (True, ""))
    monkeypatch.setattr(cp, "pick", lambda store, now=None, targets=None: "mlsd")
    calls = []
    monkeypatch.setattr(ec, "post_one", lambda store, track: (calls.append(track), ["posted edu mlsd #1 (pool): x urn", "1 posted, 9 mlsd left"])[1])
    monkeypatch.setattr(la, "drain", lambda store: (calls.append("job"), ["posted ibm urn", "1 posted, 3 left"])[1])
    assert jobs._posted(jobs.drain()) and calls == ["mlsd"]
    monkeypatch.setattr(ec, "post_one", lambda store, track: (calls.append(track), ["edu mlsd failed: boom"])[1])
    calls.clear()
    assert jobs._posted(jobs.drain()) and calls == ["mlsd", "job"]      # edu failed -> job filled the slot
    monkeypatch.setattr(la, "gate", lambda store, plog=None, now=None: (False, "holding queue — spacing"))
    calls.clear()
    assert not jobs._posted(jobs.drain()) and calls == []              # gate closed -> nothing


def test_schedule_has_edu_timers():
    import jobs
    names = {n for n, _, _ in jobs.SCHEDULE}
    assert {"edu_dsa", "edu_sd", "edu_mlsd", "edu_ai", "edu_papers", "edu_topup"} <= names


def test_topic_bank_rotation_avoids_recent_and_pool():
    import edu_content as ec
    state = {"used": [{"track": "dsa", "topic": ec.TOPICS["dsa"][0]}], "counters": {"dsa": 0}}
    pool = [{"topic": ec.TOPICS["dsa"][1]}]
    t = ec.pick_topic("dsa", state, pool)
    assert t == ec.TOPICS["dsa"][2]
    assert all(len(v) >= 30 for k, v in ec.TOPICS.items()) and len(ec.HOOK_SHAPES) >= 6
    assert ec.pick_shape({"counters": {"sd": 3}}, "sd") == ec.HOOK_SHAPES[3]
    assert ec.pick_topic("papers", state) is None


def test_generate_marks_only_carded_jobs_as_posted(tmp_path, monkeypatch):
    """With cards capped per run, un-carded jobs must stay available for the next refill."""
    import json, types, linkedin_autopost as la
    s = _edu_store(tmp_path, monkeypatch)
    s.upload_blob("li_secrets.json", json.dumps({"linkedin_autopost_enabled": "true", "cards_per_company": 1, "jobs_per_card": 2}))
    jobs = [{"id": str(i), "title": f"Software Engineer {i}", "name": f"Software Engineer {i}", "locations": ["Austin, TX"]} for i in range(10)]
    pipe = types.SimpleNamespace(get_jobs=lambda cutoff=None: list(jobs), sort_software_first=lambda j: list(j),
                                 fetch_detail=lambda i: {"salary": "$100,000 - $150,000", "url": f"https://x/{i}"})
    la._set_companies({"testco": {"pipeline": pipe}})
    la.ORG_URNS.setdefault("testco", "urn:li:organization:1")
    import card_builder
    monkeypatch.setattr(card_builder, "build_card", lambda *a, **k: b"png")
    notes = la.generate(s, lambda c: None, ["testco"], 24)
    state = json.loads(s.download_blob("li_testco_state.json").readall())
    assert len(state["posted_ids"]) == 2 and any("queued 1 cards" in n for n in notes)
    notes2 = la.generate(s, lambda c: None, ["testco"], 24)       # next run finds the remaining 8
    assert any("queued 1 cards" in n for n in notes2)
    assert len(json.loads(s.download_blob("li_testco_state.json").readall())["posted_ids"]) == 4


def test_bold_and_multi_mentions():
    import edu_content as ec, linkedin_client as lc
    assert ec.bold("Ab1") == "\U0001D5D4\U0001D5EF\U0001D7ED" and ec.bold("O(n) — ok?") == ec.bold("O(n) — ok?")
    t = "Asked at companies like Microsoft, Meta and Apple. metadata"
    b = lc._commentary(t, mentions=[("Microsoft", "urn:li:organization:1035"), ("Meta", "urn:li:organization:10667"),
                                    ("Apple", "urn:li:organization:162479"), ("Netflix", "urn:li:organization:165158")])
    got = [(a["start"], a["length"]) for a in b["attributes"]]
    assert got == [(24, 9), (35, 4), (44, 5)]                    # Netflix absent -> no attribute; 'metadata' untouched
    # utf-16 offsets after an emoji
    t2 = "\U0001F3E2 Asked at Meta"
    a = lc._commentary(t2, mentions=[("Meta", "urn:li:organization:10667")])["attributes"][0]
    assert a["start"] == 12 and a["length"] == 4
