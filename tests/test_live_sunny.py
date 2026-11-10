"""Real-time Sunny: the token for a LiveKit room with Sunny or the Quiz Mode coach."""
import json

import pytest

import dashboard
import voice_engine as ve
from tests.test_calls import _decode
from tests.test_voice import client  # noqa: F401  (fixture: user 101 with a board)


@pytest.fixture
def live(monkeypatch, client):  # noqa: F811
    monkeypatch.setenv("LIVEKIT_URL", "wss://demo.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIdemo")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 40)
    monkeypatch.delenv("SUNNY_LIVE", raising=False)
    dashboard.ve.rate_limiter = ve.RateLimiter()
    return client


def _meta(res):
    claims = _decode(res.json["token"], "s" * 40)
    agent = claims["roomConfig"]["agents"][0]
    assert agent["agentName"] == "sunny-screener"
    return claims, json.loads(agent["metadata"])


def test_welcome_says_live_is_available(live):
    assert live.get("/api/voice/welcome").json["live_voice"] is True


def test_welcome_without_livekit(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    assert client.get("/api/voice/welcome").json["live_voice"] is False
    assert client.post("/api/voice/live/start", json={}).status_code == 503


def test_live_can_be_switched_off(live, monkeypatch):
    monkeypatch.setenv("SUNNY_LIVE", "0")
    assert live.get("/api/voice/welcome").json["live_voice"] is False
    assert live.post("/api/voice/live/start", json={}).status_code == 503


def test_sunny_session_carries_the_board(live):
    r = live.post("/api/voice/live/start", json={"mode": "sunny", "lang": "hi"})
    assert r.status_code == 200 and r.json["room"].startswith("sunny-101-") and r.json["mode"] == "sunny"
    claims, meta = _meta(r)
    assert claims["sub"] == "user-101" and claims["video"]["room"] == r.json["room"]
    assert claims["exp"] - claims["nbf"] > 3000
    assert meta["mode"] == "sunny" and meta["lang"] == "hi" and meta["identity"] == "user-101"
    assert meta["candidate_name"] == "Asha Rao" and meta["resumed"] is False
    companies = {b["company"] for b in meta["board"]}
    assert "Zepto" not in companies and len(meta["board"]) == 4  # only this user's jobs
    assert set(meta["board"][0]) == {"title", "company", "column", "score"}


def test_quiz_session_carries_questions(live):
    qs = [{"q": "Tell me about a launch you led", "hints": "STAR"}, {"q": "  "}, "junk",
          {"q": "Why payments?", "hints": "x" * 2000}]
    job_id = "j1"
    r = live.post("/api/voice/live/start", json={"mode": "quiz", "job_id": job_id, "questions": qs, "start": 9})
    assert r.status_code == 200 and r.json["room"].startswith("quiz-101-")
    _, meta = _meta(r)
    assert meta["mode"] == "quiz" and meta["job_id"] == job_id
    assert [q["q"] for q in meta["questions"]] == ["Tell me about a launch you led", "Why payments?"]
    assert len(meta["questions"][1]["hints"]) == 600 and meta["start"] == 1
    assert meta["title"] and meta["company"]


def test_quiz_needs_your_own_job_and_questions(live):
    assert live.post("/api/voice/live/start", json={"mode": "quiz", "job_id": "other1",
                                                     "questions": [{"q": "x"}]}).status_code == 404
    job_id = "j1"
    assert live.post("/api/voice/live/start", json={"mode": "quiz", "job_id": job_id,
                                                     "questions": []}).status_code == 400


def test_live_start_is_rate_limited(live):
    for _ in range(20):
        assert live.post("/api/voice/live/start", json={}).status_code == 200
    assert live.post("/api/voice/live/start", json={}).status_code == 429


def test_live_start_needs_login():
    with dashboard.app.test_client() as c:
        assert c.post("/api/voice/live/start", json={}).status_code == 401
