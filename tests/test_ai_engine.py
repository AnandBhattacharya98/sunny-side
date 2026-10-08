"""Tests for the shared Gemini call in ai_engine. Run with: python -m pytest tests/"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_engine  # noqa: E402


class FakeResponse:
    def __init__(self, status, text="ok"):
        self.status_code = status
        self._text = text

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": self._text}]}}]}


def test_call_gemini_uses_current_model_header_key_and_timeout(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return FakeResponse(200, " hello ")

    monkeypatch.setattr(requests, "post", fake_post)
    assert ai_engine._call_gemini("hi", api_key="secret-key") == "hello"
    url, headers, timeout = calls[0]
    assert "gemini-1.5" not in url
    assert ai_engine.GEMINI_MODEL in url
    assert "secret-key" not in url
    assert headers["x-goog-api-key"] == "secret-key"
    assert timeout and timeout > 0


def test_call_gemini_retries_once_on_rate_limit(monkeypatch):
    statuses = iter([429, 200])
    monkeypatch.setattr(__import__("time"), "sleep", lambda s: None)
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(next(statuses), "done"))
    assert ai_engine._call_gemini("hi", api_key="k") == "done"


def test_call_gemini_error_never_contains_key(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(403))
    with pytest.raises(RuntimeError) as exc:
        ai_engine._call_gemini("hi", api_key="secret-key")
    assert "secret-key" not in str(exc.value)


def test_call_gemini_timeout_is_reported(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout()
    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(RuntimeError, match="timed out"):
        ai_engine._call_gemini("hi", api_key="k")


def _fake_gemini(monkeypatch, text):
    monkeypatch.setattr(ai_engine, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(200, text))


def test_gemini_score_is_normalized(monkeypatch):
    _fake_gemini(monkeypatch, '{"score": "14", "key_requirements": "SQL"}')
    res = ai_engine.score_job("Product Manager", "Acme", "Own the roadmap. SQL.", resume_text="PM with SQL", api_key="k")
    assert res["score"] == 10.0
    assert res["mode"] == "gemini"
    assert res["fit_summary"]
    assert res["key_requirements"] == ["SQL"]
    assert "matched_skills" in res


def test_bad_gemini_reply_falls_back_to_full_local_scorer(monkeypatch):
    _fake_gemini(monkeypatch, "not json")
    res = ai_engine.score_job("Product Manager", "Acme", "Own the roadmap.", resume_text="PM", api_key="k")
    assert res["mode"] == "local"
    assert "AI scoring was unavailable" in res["fit_summary"]


def test_force_local_skips_ai(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no network expected")
    monkeypatch.setattr(requests, "post", boom)
    res = ai_engine.score_job("Product Manager", "Acme", "Own the roadmap.", api_key="k", force_local=True)
    assert res["mode"] == "local"
    assert "unavailable" not in res["fit_summary"]


def test_rescore_saturated_scores_only_touches_inbox_tens():
    from db import get_conn, DB_PATH, init_db
    init_db(DB_PATH)
    conn = get_conn(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (901, 'sat_user', 'x')")
    for jid, score, status in [("sat1", 10, "scored"), ("sat2", 10, "applied"), ("sat3", 6.5, "scored")]:
        conn.execute("DELETE FROM jobs WHERE job_id = ?", (jid,))
        conn.execute("INSERT INTO jobs (job_id, title, company, description, ai_score, status, user_id) VALUES (?, ?, ?, ?, ?, ?, 901)",
                     (jid, "Chef", "Cafe", "Cook food", score, status))
    conn.commit()
    conn.close()
    assert ai_engine.rescore_saturated_scores(DB_PATH) >= 1
    conn = get_conn(DB_PATH)
    scores = {r[0]: r[1] for r in conn.execute("SELECT job_id, ai_score FROM jobs WHERE user_id = 901")}
    conn.close()
    assert scores["sat1"] < 9.5
    assert scores["sat2"] == 10
    assert scores["sat3"] == 6.5
