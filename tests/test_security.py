"""Login throttling and CSRF checks. Run with: python -m pytest tests/"""
import os
import sys
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
from auth import signup_user  # noqa: E402
from db import get_conn, DB_PATH  # noqa: E402


@pytest.fixture
def client():
    dashboard.login_throttle.reset()
    dashboard.app.config["CSRF_ENABLED"] = False
    conn = get_conn(DB_PATH)
    if not conn.execute("SELECT id FROM users WHERE username = ?", ("throttle_me",)).fetchone():
        signup_user(conn, "throttle_me", "correct-horse")
    conn.close()
    with dashboard.app.test_client() as c:
        yield c
    dashboard.login_throttle.reset()


def _login(c, pw):
    return c.post("/login", data={"username": "throttle_me", "password": pw})


def test_login_locks_after_repeated_failures(client):
    for _ in range(dashboard.LOGIN_FAILS_PER_USER):
        assert _login(client, "wrong").status_code == 200
    res = _login(client, "correct-horse")
    assert res.status_code == 429
    assert b"Too many attempts" in res.data


def test_successful_login_not_counted(client):
    for _ in range(dashboard.LOGIN_FAILS_PER_USER + 2):
        assert _login(client, "correct-horse").status_code == 302


def test_signup_is_rate_limited_per_ip(client):
    for i in range(dashboard.SIGNUPS_PER_IP_PER_HOUR):
        client.post("/signup", data={"username": "", "password": ""})
    res = client.post("/signup", data={"username": "", "password": ""})
    assert res.status_code == 429


@pytest.mark.csrf
def test_csrf_blocks_api_post_without_token():
    dashboard.app.config["CSRF_ENABLED"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 1
            s["csrf_token"] = "tok"
        assert c.post("/api/voice/cancel").status_code == 403
        assert c.post("/api/voice/cancel", headers={"X-CSRF-Token": "bad"}).status_code == 403
        assert c.post("/api/voice/cancel", headers={"X-CSRF-Token": "tok"}).status_code != 403


@pytest.mark.csrf
def test_login_form_carries_token_and_is_checked():
    dashboard.login_throttle.reset()
    dashboard.app.config["CSRF_ENABLED"] = True
    with dashboard.app.test_client() as c:
        page = c.get("/login").get_data(as_text=True)
        with c.session_transaction() as s:
            tok = s["csrf_token"]
        assert f'name="csrf_token" value="{tok}"' in page
        assert f'name="csrf-token" content="{tok}"' in page
        assert c.post("/login", data={"username": "x", "password": "y"}).status_code == 403
        assert c.post("/login", data={"username": "x", "password": "y", "csrf_token": tok}).status_code == 200


@pytest.mark.csrf
def test_cron_endpoint_is_exempt():
    dashboard.app.config["CSRF_ENABLED"] = True
    with dashboard.app.test_client() as c:
        assert c.post("/api/cron/daily-recommendations").status_code != 403
