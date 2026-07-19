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
    c = FakeContainer()
    monkeypatch.setattr(fa, "_container", lambda: c)
    monkeypatch.setattr(fa, "_send_email", lambda post, label: f"emailed {label}")
    monkeypatch.setattr(fa.pipeline, "get_jobs", lambda cutoff=None: list(jobs))
    monkeypatch.setattr(fa.pipeline, "render_posts", lambda b: f"<{len(b)} posts>")
    return fa, c


def test_no_duplicates_across_three_sends(monkeypatch):
    jobs = [{"id": i, "name": f"Software Engineer {i}"} for i in range(120)]
    fa, c = _setup_fa(monkeypatch, jobs)
    n1 = fa.batch_run("7 AM", "0700")
    n2 = fa.batch_run("2 PM", "1400")
    n3 = fa.batch_run("7 PM", "1900")
    state = json.loads(c.blobs["state.json"])
    assert len(state["sent_ids"]) == 120
    assert len(set(state["sent_ids"])) == 120   # every job sent exactly once
    assert state["parked"] == []
    assert "50 jobs" in n1[0] and "50 jobs" in n2[0] and "20 jobs" in n3[0]


def test_parked_jobs_drain_first(monkeypatch):
    jobs = [{"id": i, "name": f"SDE {i}"} for i in range(60)]
    fa, c = _setup_fa(monkeypatch, jobs)
    fa.batch_run("7 AM", "0700")            # sends 50, parks 10
    st = json.loads(c.blobs["state.json"])
    assert len(st["parked"]) == 10
    monkeypatch.setattr(fa.pipeline, "get_jobs", lambda cutoff=None: [])  # nothing new
    fa.batch_run("2 PM", "1400")            # drains the 10 parked
    st = json.loads(c.blobs["state.json"])
    assert st["parked"] == [] and len(st["sent_ids"]) == 60


def test_no_jobs_no_email(monkeypatch):
    fa, c = _setup_fa(monkeypatch, [])
    notes = fa.batch_run("7 AM", "0700")
    assert "no new jobs" in notes[0]
    assert not any(k.startswith("post_") for k in c.blobs)  # no post blob written


def test_all_three_timers_exist():
    import function_app as fa
    src = open(pathlib.Path(fa.__file__)).read()
    assert '"0 0 11 * * *"' in src   # 7 AM ET
    assert '"0 0 18 * * *"' in src   # 2 PM ET
    assert '"0 0 23 * * *"' in src   # 7 PM ET
