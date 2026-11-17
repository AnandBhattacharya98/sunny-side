"""Tests for the Career Switch view. Network calls and Gemini are stubbed so they stay offline."""
import json

import pytest

import career_switch as cs
import dashboard
import scraper
import voice_engine as ve
from db import get_conn, DB_PATH

UID = 301


@pytest.fixture
def client(monkeypatch):
    ve.rate_limiter.reset()
    cs._cache.clear()
    monkeypatch.setattr(dashboard, "get_fallback_gemini_key", lambda: "")
    conn = get_conn(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, name) VALUES (?, ?, 'x', 'Switcher')",
                 (UID, f"user{UID}"))
    conn.execute("UPDATE users SET designation = 'Product Manager', resume_text = 'PM at a fintech', gemini_api_key = NULL WHERE id = ?", (UID,))
    conn.execute("DELETE FROM jobs WHERE user_id = ?", (UID,))
    conn.execute("INSERT INTO jobs (job_id, title, company, url, status, user_id) VALUES "
                 "('cs1', 'UX Researcher', 'Swiggy', 'https://example.com/ux-swiggy', 'new', ?)", (UID,))
    conn.commit()
    conn.close()
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = UID
        yield c


def fake_search(query, location="", **kwargs):
    return {"results": [
        {"title": "UX Researcher", "company": "Swiggy", "location": "Bengaluru", "url": "https://example.com/ux-swiggy",
         "source": "direct", "ref": "x:1", "posted_at": ""},
        {"title": "Senior UX Researcher", "company": "Zepto", "location": "Bengaluru", "url": "https://example.com/ux-zepto",
         "source": "linkedin", "ref": "https://in.linkedin.com/jobs/view/1", "posted_at": ""},
        {"title": "Senior Product Manager", "company": "CRED", "location": "Bengaluru", "url": "https://example.com/pm-cred",
         "source": "linkedin", "ref": "https://in.linkedin.com/jobs/view/2", "posted_at": ""},
    ], "failed": []}


# ── Suggestions ──────────────────────────────────────────────────────────

def test_detects_role_family():
    assert cs.detect_family("Senior Product Manager") == "product"
    assert cs.detect_family("Backend Software Engineer") == "engineering"
    assert cs.detect_family("Product Designer") == "design"
    assert cs.detect_family("Astronaut") is None


def test_local_paths_never_suggest_the_current_role():
    for role in ("Product Manager", "Data Analyst", "Software Engineer", "Recruiter", "Teacher"):
        paths = cs.local_paths(role)
        assert 1 <= len(paths) <= cs.MAX_PATHS
        assert all(not cs.is_current_track(p["query"], role) for p in paths)


def test_unknown_role_gets_general_paths():
    assert cs.local_paths("Astronaut") == cs.GENERAL_PATHS[:cs.MAX_PATHS]


def test_current_track_filter():
    assert cs.is_current_track("Senior Product Manager", "Product Manager")
    assert cs.is_current_track("Product Manager, Payments", "Product Manager")
    assert not cs.is_current_track("UX Researcher", "Product Manager")
    assert not cs.is_current_track("Product Marketing Manager", "Product Manager")
    # A loose search hit that looks more like the old role than the new path is dropped
    assert cs.is_current_track("Software Engineer", "Software Engineer II", "Solutions Engineer")
    assert not cs.is_current_track("Solutions Engineer", "Software Engineer", "Solutions Engineer")
    assert not cs.is_current_track("Anything", "")


def test_ai_paths_are_cleaned_and_filtered(monkeypatch):
    reply = [
        {"role": "Senior Product Manager", "query": "Senior Product Manager", "why": "More of the same."},
        {"role": "UX Researcher", "query": "UX Researcher", "why": "You talk to users.", "carry": ["Interviews"] * 9, "gaps": "not a list"},
        {"nope": True},
    ]
    monkeypatch.setattr(cs, "_call_gemini", lambda *a, **k: json.dumps(reply))
    paths = cs.ai_paths("Product Manager", "resume", "key")
    assert [p["role"] for p in paths] == ["UX Researcher"]
    assert len(paths[0]["carry"]) == 4 and paths[0]["gaps"] == []


def test_ai_failure_falls_back_to_local(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Gemini returned HTTP 500")
    monkeypatch.setattr(cs, "_call_gemini", boom)
    res = cs.suggest_paths(1, "Product Manager", "resume", api_key="key")
    assert res["mode"] == "local" and res["paths"]


def test_ai_paths_are_cached_until_refresh(monkeypatch):
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return json.dumps([{"role": "UX Researcher", "query": "UX Researcher"}])
    monkeypatch.setattr(cs, "_call_gemini", fake)
    cs._cache.clear()
    cs.suggest_paths(7, "Product Manager", "r", api_key="k")
    cs.suggest_paths(7, "Product Manager", "r", api_key="k")
    assert len(calls) == 1
    cs.suggest_paths(7, "Product Manager", "r", api_key="k", refresh=True)
    assert len(calls) == 2
    cs.suggest_paths(7, "Product Manager", "changed resume", api_key="k")
    assert len(calls) == 3


# ── API and page ─────────────────────────────────────────────────────────

def test_career_switch_requires_login():
    with dashboard.app.test_client() as c:
        assert c.get("/api/career-switch/paths").status_code == 401
        assert c.get("/api/career-switch/jobs?q=ux").status_code == 401


def test_page_renders(client):
    html = client.get("/career-switch").get_data(as_text=True)
    assert "Career Switch" in html and "loadCareerPaths" in html
    assert "Product Manager" in html


def test_paths_api_uses_local_map_without_a_key(client):
    data = client.get("/api/career-switch/paths").get_json()
    assert data["ok"] and data["mode"] == "local"
    assert data["current_role"] == "Product Manager"
    roles = [p["role"] for p in data["paths"]]
    assert "UX Researcher" in roles and "Product Marketing Manager" in roles


def test_jobs_api_hides_current_role_and_marks_board(client, monkeypatch):
    monkeypatch.setattr(scraper, "search_web_jobs", fake_search)
    data = client.get("/api/career-switch/jobs?q=UX+Researcher").get_json()
    assert data["ok"]
    titles = {r["title"]: r for r in data["results"]}
    assert "Senior Product Manager" not in titles
    assert data["hidden"] == 1
    assert titles["UX Researcher"]["on_board"] is True
    assert titles["Senior UX Researcher"]["on_board"] is False


def test_jobs_api_needs_a_query(client):
    assert client.get("/api/career-switch/jobs?q=a").status_code == 400


def test_other_users_board_is_not_used(client, monkeypatch):
    conn = get_conn(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, name) VALUES (302, 'user302', 'x', 'Other')")
    conn.execute("DELETE FROM jobs WHERE user_id = 302")
    conn.execute("INSERT INTO jobs (job_id, title, company, url, status, user_id) VALUES "
                 "('cs-other', 'Senior UX Researcher', 'Zepto', 'https://example.com/ux-zepto', 'new', 302)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(scraper, "search_web_jobs", fake_search)
    data = client.get("/api/career-switch/jobs?q=UX+Researcher").get_json()
    zepto = next(r for r in data["results"] if r["company"] == "Zepto")
    assert zepto["on_board"] is False


# ── Filters and job details ──────────────────────────────────────────────

from datetime import datetime, timedelta, timezone

import job_compare

LONG_DESC = ("About the role: you'll turn messy data into decisions for our growth team.\n"
             "Requirements:\n• 3+ years of experience\n• Strong SQL and Python\n• Tableau dashboards\n"
             "Nice to have:\n• AWS\n" + "We value curiosity and clear writing. " * 8)


def dated_search(query, location="", **kwargs):
    now = datetime.now(timezone.utc)
    dated_search.kwargs = kwargs
    return {"results": [
        {"title": "Data Analyst", "company": "Meesho", "location": "Remote · India", "url": "https://remotive.com/job/1",
         "source": "remotive", "ref": "remotive:1", "posted_at": (now - timedelta(hours=5)).isoformat(),
         "remote": True, "salary": "₹12–18 LPA", "description": LONG_DESC},
        {"title": "Junior Data Analyst", "company": "Swiggy", "location": "Bengaluru", "url": "https://example.com/da-swiggy",
         "source": "direct", "ref": "x:9", "posted_at": (now - timedelta(days=10)).isoformat()},
        {"title": "Data Analyst Intern", "company": "Zepto", "location": "Mumbai", "url": "https://in.linkedin.com/jobs/view/123456789",
         "source": "linkedin", "ref": "https://in.linkedin.com/jobs/view/123456789", "posted_at": ""},
    ], "failed": []}


def test_jobs_api_describes_each_result(client, monkeypatch):
    conn = get_conn(DB_PATH)
    conn.execute("UPDATE users SET resume_text = 'Analyst with 2 years of SQL and Excel work' WHERE id = ?", (UID,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(scraper, "search_web_jobs", dated_search)
    data = client.get("/api/career-switch/jobs?q=Data+Analyst").get_json()
    by = {r["company"]: r for r in data["results"]}
    assert by["Meesho"]["remote"] is True and by["Meesho"]["salary"] == "₹12–18 LPA"
    assert isinstance(by["Meesho"]["match"], int)
    assert by["Swiggy"]["level"] == "entry" and by["Swiggy"]["match"] is None
    assert by["Zepto"]["level"] == "intern" and by["Zepto"]["type"] == "internship"
    assert "description" not in by["Meesho"]


def test_date_filter_drops_older_and_undated_postings(client, monkeypatch):
    monkeypatch.setattr(scraper, "search_web_jobs", dated_search)
    data = client.get("/api/career-switch/jobs?q=Data+Analyst&days=3").get_json()
    assert [r["company"] for r in data["results"]] == ["Meesho"]
    assert data["older"] == 2 and data["days"] == 3
    assert dated_search.kwargs["days"] == 3


def test_work_mode_filters(client, monkeypatch):
    monkeypatch.setattr(scraper, "search_web_jobs", dated_search)
    onsite = client.get("/api/career-switch/jobs?q=Data+Analyst&mode=onsite").get_json()
    assert "Meesho" not in {r["company"] for r in onsite["results"]}
    client.get("/api/career-switch/jobs?q=Data+Analyst&mode=remote")
    assert dated_search.kwargs["remote_only"] is True


def test_details_compare_resume_against_posting(client, monkeypatch):
    job_compare._cache.clear()
    conn = get_conn(DB_PATH)
    conn.execute("UPDATE users SET resume_text = 'Analyst with 2 years of SQL and Excel work', resume_profile_json = NULL WHERE id = ?", (UID,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(scraper, "search_web_jobs", dated_search)
    client.get("/api/career-switch/jobs?q=Data+Analyst")
    data = client.post("/api/career-switch/details", json={"source": "remotive", "ref": "remotive:1"}).get_json()
    assert data["ok"] and data["has_resume"]
    assert data["job"]["description"].startswith("About the role")
    cmp = data["compare"]
    assert "SQL" in cmp["matched"]
    missing = {m["skill"]: m for m in cmp["missing"]}
    assert missing["Python"]["importance"] == "must"
    assert missing["AWS"]["importance"] == "nice"
    assert missing["Tableau"]["learn_url"] == "/learn?skill=Tableau&from=career-switch"
    assert cmp["experience"]["required"] == 3
    assert 0 <= cmp["score"] <= 100 and cmp["mode"] == "local"


def test_details_for_an_unknown_remote_ref_is_refused(client):
    r = client.post("/api/career-switch/details", json={"source": "remotive", "ref": "remotive:nope"})
    assert r.status_code == 404


def test_details_only_fetch_linkedin_job_links(client, monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "fetch_linkedin_posting", lambda url: calls.append(url) or {"description": LONG_DESC, "criteria": {"Seniority level": "Entry level", "Employment type": "Internship"}})
    bad = client.post("/api/career-switch/details", json={"source": "linkedin", "ref": "http://169.254.169.254/latest", "title": "x", "company": "y"})
    assert bad.status_code == 404 and not calls
    ok = client.post("/api/career-switch/details", json={"source": "linkedin", "ref": "https://in.linkedin.com/jobs/view/123456789",
                                                          "title": "Data Analyst", "company": "Zepto"}).get_json()
    assert ok["ok"] and calls == ["https://in.linkedin.com/jobs/view/123456789"]
    assert ok["job"]["level"] == "entry" and ok["job"]["type"] == "internship"


def test_details_uses_ai_when_a_key_is_set(client, monkeypatch):
    job_compare._cache.clear()
    monkeypatch.setattr(dashboard, "get_fallback_gemini_key", lambda: "k")
    reply = {"score": 64, "summary": "Close.", "matched": ["SQL"], "missing": [{"skill": "Python", "importance": "must", "why": "Daily work."}, "Looker"],
             "strengths": ["Analytics background"], "tips": ["Ship a notebook"]}
    monkeypatch.setattr(job_compare, "_call_gemini", lambda *a, **k: json.dumps(reply))
    monkeypatch.setattr(scraper, "search_web_jobs", dated_search)
    client.get("/api/career-switch/jobs?q=Data+Analyst")
    cmp = client.post("/api/career-switch/details", json={"source": "remotive", "ref": "remotive:1"}).get_json()["compare"]
    assert cmp["mode"] == "ai" and cmp["score"] == 64 and cmp["verdict"] == "Good match"
    assert [m["skill"] for m in cmp["missing"]] == ["Python", "Looker"]
    assert all(m["learn_url"].startswith("/learn?skill=") for m in cmp["missing"])


def test_details_requires_login():
    with dashboard.app.test_client() as c:
        assert c.post("/api/career-switch/details", json={}).status_code == 401


def test_job_level_and_type():
    assert job_compare.job_level("Senior Data Analyst") == "senior"
    assert job_compare.job_level("Head of Growth") == "lead"
    assert job_compare.job_level("Data Analyst") == "mid"
    assert job_compare.job_type("Analyst", "This is a 6 month contract role") == "contract"
