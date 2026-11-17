"""Feature switches in the Admin Console, the staging label, and the health check."""
import pytest

import dashboard
import flags
from auth import hash_password
from db import get_conn, DB_PATH, init_db

UID = 702


@pytest.fixture(autouse=True)
def conn():
    init_db(DB_PATH)
    c = get_conn(DB_PATH)
    c.execute("DELETE FROM feature_flags")
    c.execute("DELETE FROM users WHERE id = ?", (UID,))
    c.execute("INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)", (UID, "flags_user", hash_password("pw-flags-702")))
    c.commit()
    flags.clear_cache()
    dashboard._disabled_cache.clear()
    yield c
    c.execute("DELETE FROM feature_flags")
    c.commit()
    c.close()
    flags.clear_cache()


def client_for(uid):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
        s["username"] = "admin" if uid == 1 else "flags_user"
    return c


def test_paths_map_to_their_flag():
    assert flags.flag_for_path("/learn") == "learn"
    assert flags.flag_for_path("/learn/12") == "learn"
    assert flags.flag_for_path("/api/learn/courses") == "learn"
    assert flags.flag_for_path("/learning") is None
    assert flags.flag_for_path("/api/voice/chat") == "sunny"
    assert flags.flag_for_path("/career-switch") == "career_switch"
    assert flags.flag_for_path("/api/calls/practice/start") == "calls"
    assert flags.flag_for_path("/") is None


def test_features_are_on_by_default():
    for key, f in flags.FLAGS.items():
        if f.default == "on":
            assert flags.enabled(key)
    # Unfinished features ship dark: only the admin sees them until they're switched on
    assert flags.FLAGS["home_refresh"].default == "admin"


def test_env_sets_the_default_until_the_console_changes_it(conn, monkeypatch):
    monkeypatch.setenv("FLAG_LEARN", "off")
    assert not flags.enabled("learn", is_admin=True)
    flags.set_state(conn, "learn", "on")
    assert flags.enabled("learn")


def test_switching_a_feature_off_hides_it(conn):
    flags.set_state(conn, "learn", "off")
    user = client_for(UID)
    r = user.get("/learn")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    r = user.get("/api/learn/courses")
    assert r.status_code == 404 and r.get_json()["feature_off"] == "learn"
    assert b'href="/learn"' not in user.get("/progress").data


def test_admin_only_lets_the_admin_try_it_first(conn):
    flags.set_state(conn, "career_switch", "admin")
    assert client_for(UID).get("/career-switch").status_code == 302
    assert b'href="/career-switch"' in client_for(1).get("/progress").data
    assert b'href="/career-switch"' not in client_for(UID).get("/progress").data


def test_sunny_off_removes_the_assistant_from_the_board(conn):
    flags.set_state(conn, "sunny", "off")
    page = client_for(UID).get("/").data
    assert b'id="va-fab"' not in page and b'id="voice-overlay"' not in page
    r = client_for(UID).post("/api/voice/chat", json={"message": "hi"})
    assert r.status_code == 404 and r.get_json()["feature_off"] == "sunny"


def test_only_the_admin_can_flip_switches(conn):
    assert client_for(UID).post("/api/admin/flags/learn", json={"state": "off"}).status_code == 403
    admin = client_for(1)
    assert admin.post("/api/admin/flags/nope", json={"state": "off"}).status_code == 404
    assert admin.post("/api/admin/flags/learn", json={"state": "maybe"}).status_code == 400
    r = admin.post("/api/admin/flags/learn", json={"state": "admin"})
    assert r.get_json() == {"ok": True, "key": "learn", "state": "admin"}
    assert flags.state("learn") == "admin"
    page = admin.get("/users").data
    assert b'id="feature-switches"' in page and b"Admin only" in page


def test_healthz_reports_env_and_version(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567")
    body = dashboard.app.test_client().get("/healthz").get_json()
    assert body == {"ok": True, "env": "production", "version": "abcdef1"}


def test_staging_is_labelled_and_not_indexed(monkeypatch):
    monkeypatch.setattr(dashboard, "APP_ENV", "staging")
    r = dashboard.app.test_client().get("/login")
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert b"env-ribbon" in r.data and b'name="robots"' in r.data


def test_production_has_no_staging_label():
    r = dashboard.app.test_client().get("/login")
    assert "X-Robots-Tag" not in r.headers and b"env-ribbon" not in r.data
