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
