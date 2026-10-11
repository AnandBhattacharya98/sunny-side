"""Tests for the YouTube videos shown under a job's skill gaps. YouTube is stubbed so they stay offline."""
import json

import pytest

import dashboard
import learning_videos as lv
from db import get_conn, DB_PATH

UID = 311
OTHER = 312


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def yt_payload(skill):
    return {"items": [
        {"id": {"videoId": f"{skill[:3]}1"}, "snippet": {"title": f"{skill} for beginners", "channelTitle": "Chan A",
                                                         "thumbnails": {"medium": {"url": "https://i.ytimg.com/a.jpg"}}}},
        {"id": {"videoId": f"{skill[:3]}2"}, "snippet": {"title": f"{skill} crash course", "channelTitle": "Chan B", "thumbnails": {}}},
    ]}


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_get(url, params=None, timeout=None):
        seen.append(params["q"])
        return FakeResp(200, yt_payload(params["q"].replace(" tutorial", "")))

    monkeypatch.setattr(lv.requests, "get", fake_get)
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    return seen


@pytest.fixture
def client():
    conn = get_conn(DB_PATH)
    conn.execute("DELETE FROM skill_videos")
    for uid in (UID, OTHER):
        conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (?, ?, 'x')", (uid, f"user{uid}"))
        conn.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
    conn.execute("INSERT INTO jobs (job_id, title, company, url, status, user_id, missing_skills) VALUES "
                 "('gv1', 'PM', 'Acme', 'https://example.com/gv1', 'new', ?, ?)",
                 (UID, json.dumps(["SQL", "A/B Testing", "sql", "Figma", "Jira", "Tableau"])))
    conn.execute("INSERT INTO jobs (job_id, title, company, url, status, user_id, missing_skills) VALUES "
                 "('gv2', 'PM', 'Beta', 'https://example.com/gv2', 'new', ?, ?)", (UID, json.dumps(["SQL"])))
    conn.execute("INSERT INTO jobs (job_id, title, company, url, status, user_id, missing_skills) VALUES "
                 "('gv3', 'PM', 'Gamma', 'https://example.com/gv3', 'new', ?, '[]')", (UID,))
    conn.commit()
    conn.close()
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = UID
        yield c


def test_two_videos_per_gap_capped_and_deduped(client, calls):
    data = client.get("/api/job/gv1/videos").get_json()
    assert data["ok"]
    skills = [g["skill"] for g in data["gaps"]]
    assert skills == ["SQL", "A/B Testing", "Figma", "Jira"]  # "sql" is a duplicate; capped at MAX_GAPS
    for g in data["gaps"]:
        assert len(g["videos"]) == 2
        assert g["videos"][0]["url"].startswith("https://www.youtube.com/watch?v=")
        assert g["videos"][1]["thumb"].startswith("https://i.ytimg.com/vi/")
        assert g["more"].startswith("https://www.youtube.com/results?search_query=")
    assert len(calls) == 4


def test_videos_are_saved_and_shared_across_jobs(client, calls):
    client.get("/api/job/gv1/videos")
    n = len(calls)
    again = client.get("/api/job/gv1/videos").get_json()
    other_job = client.get("/api/job/gv2/videos").get_json()
    assert len(calls) == n  # nothing fetched twice
    assert again["gaps"][0]["videos"][0]["title"] == "SQL for beginners"
    assert other_job["gaps"][0]["videos"][0]["title"] == "SQL for beginners"


def test_without_a_key_each_gap_gets_a_search_link(client, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setattr(lv.requests, "get", lambda *a, **k: pytest.fail("YouTube should not be called"))
    data = client.get("/api/job/gv2/videos").get_json()
    assert data["gaps"] == [{"skill": "SQL", "videos": [], "more": lv.search_link("SQL")}]


def test_youtube_errors_fall_back_and_are_not_saved(client, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
    monkeypatch.setattr(lv.requests, "get", lambda *a, **k: FakeResp(403, {"error": {"message": "quotaExceeded"}}))
    data = client.get("/api/job/gv2/videos").get_json()
    assert data["gaps"][0]["videos"] == []
    conn = get_conn(DB_PATH)
    assert conn.execute("SELECT COUNT(*) FROM skill_videos").fetchone()[0] == 0
    conn.close()


def test_job_with_no_gaps_returns_nothing(client, calls):
    assert client.get("/api/job/gv3/videos").get_json()["gaps"] == []


def test_other_users_jobs_are_hidden(client, calls):
    with client.session_transaction() as s:
        s["user_id"] = OTHER
    assert client.get("/api/job/gv1/videos").status_code == 404


def test_modal_has_the_videos_section(client):
    html = client.get("/job/gv1/modal").get_data(as_text=True)
    assert 'id="gapv-gv1"' in html
    assert 'id="gapv-gv3"' not in client.get("/job/gv3/modal").get_data(as_text=True)
