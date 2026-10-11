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
