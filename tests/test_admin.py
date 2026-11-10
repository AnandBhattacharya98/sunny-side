"""Admin console: sign-in history, turning accounts off, and admin-only password resets."""
import pytest

import dashboard
from auth import hash_password, verify_password
from db import get_conn, DB_PATH, init_db

UID, NAME, PW = 701, "adminpanel_user", "correct-horse-701"


@pytest.fixture
def conn():
    init_db(DB_PATH)
    dashboard.login_throttle.reset()
    dashboard._disabled_cache.clear()
    c = get_conn(DB_PATH)
    c.execute("DELETE FROM login_events WHERE user_id = ?", (UID,))
    c.execute("DELETE FROM users WHERE id = ?", (UID,))
    c.execute("INSERT INTO users (id, username, password_hash, gemini_api_key) VALUES (?, ?, ?, 'secret-key')",
              (UID, NAME, hash_password(PW)))
    c.commit()
    yield c
    c.close()


def client_for(uid=None):
    c = dashboard.app.test_client()
    if uid:
        with c.session_transaction() as s:
            s["user_id"] = uid
    return c


def events(conn):
    return conn.execute("SELECT method, success, user_agent FROM login_events WHERE user_id = ? ORDER BY id",
                        (UID,)).fetchall()


def test_password_logins_are_recorded(conn):
    c = client_for()
    assert c.post("/login", data={"username": NAME, "password": "nope"}).status_code == 200
    assert c.post("/login", data={"username": NAME, "password": PW},
                  headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120.0 Mobile"}).status_code == 302
    rows = events(conn)
    assert [(r[0], r[1]) for r in rows] == [("password", 0), ("password", 1)]
    user = conn.execute("SELECT login_count, last_login_at FROM users WHERE id = ?", (UID,)).fetchone()
    assert user[0] == 1 and user[1]


def test_unknown_usernames_are_not_logged(conn):
    before = conn.execute("SELECT COUNT(*) FROM login_events").fetchone()[0]
    client_for().post("/login", data={"username": "no_such_person_xyz", "password": "whatever1"})
    assert conn.execute("SELECT COUNT(*) FROM login_events").fetchone()[0] == before


def test_console_is_admin_only(conn):
    assert client_for(UID).get("/users").status_code == 302
    assert client_for(UID).post(f"/api/admin/users/{UID}/disable", json={"disabled": True}).status_code == 403
    assert client_for(UID).post(f"/api/admin/users/{UID}/reset-password").status_code == 403


def test_console_lists_accounts_and_history_without_secrets(conn):
    client_for().post("/login", data={"username": NAME, "password": PW},
                      headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120.0 Mobile"})
    page = client_for(1).get(f"/users?user={UID}").get_data(as_text=True)
    assert "Admin Console" in page and NAME in page and "Chrome on Android" in page
    assert "secret-key" not in page and "password_hash" not in page


def test_turning_an_account_off_blocks_it(conn):
    user = client_for(UID)
    assert user.get("/api/stats").status_code == 200
    assert client_for(1).post(f"/api/admin/users/{UID}/disable", json={"disabled": True}).get_json()["ok"]
    res = user.get("/api/stats")
    assert res.status_code == 401 and "turned off" in res.get_json()["error"]
    res = client_for().post("/login", data={"username": NAME, "password": PW})
    assert res.status_code == 403 and b"turned off" in res.data
    assert client_for(1).post(f"/api/admin/users/{UID}/disable", json={"disabled": False}).get_json()["ok"]
    assert client_for().post("/login", data={"username": NAME, "password": PW}).status_code == 302


def test_admin_cannot_be_turned_off_or_reset(conn):
    admin = client_for(1)
    assert admin.post("/api/admin/users/1/disable", json={"disabled": True}).status_code == 400
    assert admin.post("/api/admin/users/1/reset-password").status_code == 400
    assert admin.get("/users").status_code == 200


def test_reset_password_gives_a_working_temporary_one(conn):
    data = client_for(1).post(f"/api/admin/users/{UID}/reset-password").get_json()
    assert data["ok"] and data["username"] == NAME
    stored = conn.execute("SELECT password_hash FROM users WHERE id = ?", (UID,)).fetchone()[0]
    assert verify_password(data["temporary_password"], stored) and not verify_password(PW, stored)


def test_deleting_an_account_removes_its_history(conn):
    client_for().post("/login", data={"username": NAME, "password": PW})
    assert client_for(1).post(f"/api/users/{UID}/delete").get_json()["ok"]
    assert conn.execute("SELECT COUNT(*) FROM login_events WHERE user_id = ?", (UID,)).fetchone()[0] == 0


@pytest.mark.parametrize("ua,label", [
    ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit Chrome/120.0 Safari/537.36 Edg/120.0", "Edge on Windows"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit Version/17.0 Mobile Safari/604.1", "Safari on iPhone"),
    ("Mozilla/5.0 (Linux; Android 14) Chrome/120.0 SunnySideApp", "Sunny-Side app (Android)"),
    ("", "Unknown device"),
])
def test_device_label(ua, label):
    assert dashboard._device_label(ua) == label
