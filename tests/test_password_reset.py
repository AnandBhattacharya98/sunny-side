"""Forgot-password flow. Run with: python -m pytest tests/"""
import os
import re
import sys
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
from auth import signup_user, login_user  # noqa: E402
from db import get_conn, DB_PATH  # noqa: E402


@pytest.fixture
def mailbox(monkeypatch):
    sent = []

    class SyncThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(dashboard, "email_configured", lambda: True)
    monkeypatch.setattr(dashboard, "send_email", lambda to, subject, html: sent.append((to, html)) or True)
    monkeypatch.setattr(dashboard.threading, "Thread", SyncThread)
    dashboard.login_throttle.reset()
    conn = get_conn(DB_PATH)
    if not conn.execute("SELECT id FROM users WHERE username = ?", ("reset.me@example.com",)).fetchone():
        signup_user(conn, "reset.me@example.com", "old-password")
    conn.close()
    yield sent
    dashboard.login_throttle.reset()


def _link(sent):
    return re.search(r'href="https?://[^/]+(/reset/[^"]+)"', sent[-1][1]).group(1)


def test_full_reset_flow(mailbox):
    c = dashboard.app.test_client()
    res = c.post("/forgot", data={"identifier": "Reset.Me@example.com"})
    assert b"reset link is on its way" in res.data
    assert mailbox[-1][0] == "reset.me@example.com"
    path = _link(mailbox)
    assert c.get(path).status_code == 200
    assert b"at least 8" in c.post(path, data={"password": "short"}).data
    assert b"has been changed" in c.post(path, data={"password": "brand-new-pass"}).data
    conn = get_conn(DB_PATH)
    assert login_user(conn, "reset.me@example.com", "brand-new-pass")
    assert not login_user(conn, "reset.me@example.com", "old-password")
    conn.close()
    # Links are single use
    assert b"expired or was already used" in c.post(path, data={"password": "another-pass"}).data


def test_unknown_account_gets_same_answer_and_no_email(mailbox):
    c = dashboard.app.test_client()
    res = c.post("/forgot", data={"identifier": "nobody-here"})
    assert b"reset link is on its way" in res.data
    assert mailbox == []


def test_expired_link_is_rejected(mailbox):
    c = dashboard.app.test_client()
    c.post("/forgot", data={"identifier": "reset.me@example.com"})
    path = _link(mailbox)
    conn = get_conn(DB_PATH)
    conn.execute("UPDATE password_resets SET expires_at = '2000-01-01T00:00:00'")
    conn.commit()
    conn.close()
    assert b"expired" in c.get(path).data


def test_forgot_is_rate_limited(mailbox):
    c = dashboard.app.test_client()
    for _ in range(5):
        c.post("/forgot", data={"identifier": "reset.me@example.com"})
    assert len(mailbox) == 3


def test_without_email_setup_user_is_told(monkeypatch):
    monkeypatch.setattr(dashboard, "email_configured", lambda: False)
    res = dashboard.app.test_client().post("/forgot", data={"identifier": "x"})
    assert b"set up on this site yet" in res.data


def test_login_page_links_to_forgot():
    assert b'href="/forgot"' in dashboard.app.test_client().get("/login").data
