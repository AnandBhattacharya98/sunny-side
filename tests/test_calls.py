"""Call log: GET/POST /api/calls, personal API keys, practice-call tokens and logging."""
import base64
import hashlib
import hmac
import importlib
import json
import os

import pytest

import calls as call_log
import dashboard
from db import get_conn, DB_PATH

U1, U2 = 701, 702


@pytest.fixture
def conn():
    c = get_conn(DB_PATH)
    for uid in (U1, U2):
        c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (?, ?, 'x')", (uid, f"calls{uid}"))
        c.execute("UPDATE users SET calls_api_key_hash = NULL, calls_api_key_hint = NULL, name = NULL, "
                  "designation = NULL, resume_text = NULL WHERE id = ?", (uid,))
    c.execute("DELETE FROM calls WHERE user_id IN (?, ?)", (U1, U2))
    c.commit()
    dashboard.ve.rate_limiter = dashboard.ve.RateLimiter()
    yield c
    c.close()


def _client(uid=U1):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


BODY = {"caller_name": "Priya at Acme", "phone_number": "+91 98765 43210", "status": "completed",
        "duration_seconds": 420, "summary": "Recruiter screen, next round Friday"}


def test_post_then_get_signed_in(conn):
    c = _client()
    r = c.post("/api/calls", json=BODY)
    assert r.status_code == 201
    saved = r.json["call"]
    assert saved["id"] and saved["caller_name"] == "Priya at Acme" and saved["kind"] == "logged"
    assert saved["created_at"].endswith("Z")
    listed = c.get("/api/calls").json["calls"]
    assert listed[0]["id"] == saved["id"]


def test_calls_are_private_to_each_user(conn):
    _client(U1).post("/api/calls", json=BODY)
    assert _client(U2).get("/api/calls").json["calls"] == []
    call_id = _client(U1).get("/api/calls").json["calls"][0]["id"]
    assert _client(U2).get(f"/api/calls/{call_id}").status_code == 404


@pytest.mark.parametrize("bad, msg", [
    ({"phone_number": "123"}, "caller_name is required"),
    ({"caller_name": "A"}, "phone_number is required"),
    ({"caller_name": "A", "phone_number": "12"}, "at least 3"),
    ({"caller_name": "A", "phone_number": "123", "status": "busy"}, "status must be one of"),
    ({"caller_name": "A", "phone_number": "123", "duration_seconds": -5}, "between 0 and 86400"),
    ({"caller_name": "A", "phone_number": "123", "duration_seconds": "ten"}, "whole number"),
    ({"caller_name": "A", "phone_number": "123", "duration_seconds": True}, "whole number"),
    ({"caller_name": "A" * 201, "phone_number": "123"}, "at most 200"),
])
def test_bad_bodies_are_rejected(conn, bad, msg):
    r = _client().post("/api/calls", json=bad)
    assert r.status_code == 400 and msg in r.json["error"]


def test_non_json_body_is_rejected(conn):
    r = _client().post("/api/calls", data="caller_name=A", content_type="application/x-www-form-urlencoded")
    assert r.status_code == 400


def test_signed_out_without_key_is_refused(conn):
    with dashboard.app.test_client() as c:
        assert c.get("/api/calls").status_code == 401
        assert c.post("/api/calls", json=BODY).status_code == 401


def test_api_key_works_like_curl(conn):
    key = _client().post("/api/calls/api-key").json["api_key"]
    assert key.startswith("ss_")
    stored = conn.execute("SELECT calls_api_key_hash FROM users WHERE id = ?", (U1,)).fetchone()[0]
    assert stored == hashlib.sha256(key.encode()).hexdigest() and key not in stored
    with dashboard.app.test_client() as curl:  # no cookie at all
        r = curl.post("/api/calls", json=BODY, headers={"X-API-Key": key})
        assert r.status_code == 201
        assert curl.get("/api/calls", headers={"X-API-Key": key}).json["calls"][0]["caller_name"] == "Priya at Acme"
    # It lands on the owner's page
    assert _client(U1).get("/api/calls").json["calls"][0]["id"] == r.json["call"]["id"]


def test_bad_or_revoked_key_is_refused(conn):
    key = _client().post("/api/calls/api-key").json["api_key"]
    with dashboard.app.test_client() as curl:
        assert curl.get("/api/calls", headers={"X-API-Key": "ss_wrong"}).status_code == 401
        assert curl.post("/api/calls", json=BODY, headers={"X-API-Key": "nope"}).status_code == 401
        _client().post("/api/calls/api-key/revoke")
        assert curl.get("/api/calls", headers={"X-API-Key": key}).status_code == 401


def test_replacing_a_key_kills_the_old_one(conn):
    old = _client().post("/api/calls/api-key").json["api_key"]
    new = _client().post("/api/calls/api-key").json["api_key"]
    with dashboard.app.test_client() as curl:
        assert curl.get("/api/calls", headers={"X-API-Key": old}).status_code == 401
        assert curl.get("/api/calls", headers={"X-API-Key": new}).status_code == 200


def test_key_only_opens_call_endpoints(conn):
    key = _client().post("/api/calls/api-key").json["api_key"]
    with dashboard.app.test_client() as curl:
        assert curl.get("/api/stats", headers={"X-API-Key": key}).status_code == 401
        assert curl.post("/api/calls/api-key", headers={"X-API-Key": key}).status_code == 401


@pytest.mark.csrf
def test_csrf_still_guards_cookie_posts_but_not_key_posts(conn):
    dashboard.app.config["CSRF_ENABLED"] = True
    c = _client()
    with c.session_transaction() as s:
        s["csrf_token"] = "tok"
    assert c.post("/api/calls", json=BODY).status_code == 403
    assert c.post("/api/calls", json=BODY, headers={"X-CSRF-Token": "tok"}).status_code == 201
    key = c.post("/api/calls/api-key", headers={"X-CSRF-Token": "tok"}).json["api_key"]
    with dashboard.app.test_client() as curl:
        assert curl.post("/api/calls", json=BODY, headers={"X-API-Key": key}).status_code == 201


def test_calls_page_renders(conn, monkeypatch):
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    _client().post("/api/calls", json={**BODY, "caller_name": "<script>x</script>"})
    html = _client().get("/calls").get_data(as_text=True)
    assert "Practice recruiter screen" in html and "/api/calls" in html
    assert "Practice calls need LiveKit" in html
    assert "<script>x</script>" not in html  # the name is escaped in the embedded JSON
    assert 'href="/calls"' in _client().get("/progress").get_data(as_text=True)


def _decode(token, secret):
    head, body, sig = token.split(".")
    want = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    assert sig == want.rstrip(b"=").decode()
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


def test_practice_start_mints_token_that_dispatches_agent(conn, monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://demo.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIdemo")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 40)
    conn.execute("UPDATE users SET name = 'Anand', designation = 'Product Manager', resume_text = 'PM at X' WHERE id = ?", (U1,))
    conn.commit()
    r = _client().post("/api/calls/practice/start", json={})
    assert r.status_code == 200 and r.json["url"] == "wss://demo.livekit.cloud"
    assert r.json["room"].startswith(f"practice-{U1}-") and r.json["role"] == "Product Manager"
    claims = _decode(r.json["token"], "s" * 40)
    assert claims["iss"] == "APIdemo" and claims["sub"] == f"user-{U1}"
    assert claims["video"] == {"roomJoin": True, "room": r.json["room"], "canPublish": True,
                               "canSubscribe": True, "canPublishData": True}
    agent = claims["roomConfig"]["agents"][0]
    assert agent["agentName"] == "sunny-screener"
    meta = json.loads(agent["metadata"])
    assert meta == {"candidate_name": "Anand", "role": "Product Manager", "company": "", "resume": "PM at X"}
    # The role typed on the page wins over the profile's
    assert _client().post("/api/calls/practice/start", json={"role": "Data Analyst"}).json["role"] == "Data Analyst"


def test_token_matches_livekit_sdk_claims():
    """Same claim shape as livekit-api's AccessToken, checked field by field above; here the JWT header."""
    tok = call_log.room_token("k", "s" * 32, identity="u", name="n", room="r", agent_metadata={})
    head = json.loads(base64.urlsafe_b64decode(tok.split(".")[0] + "=="))
    assert head == {"alg": "HS256", "typ": "JWT"}


def test_practice_start_needs_livekit(conn, monkeypatch):
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    assert _client().post("/api/calls/practice/start", json={}).status_code == 503


def test_practice_end_logs_call_and_summary(conn, monkeypatch):
    monkeypatch.setattr(call_log, "summarize_practice", lambda t, role, key: f"Good call for {role}.")
    ran = []
    monkeypatch.setattr(dashboard, "run_in_background", lambda key, fn, *a: (ran.append(key), fn(*a)))
    transcript = [{"role": "agent", "text": "Hi, is now a good time?"}, {"role": "user", "text": "Yes, go ahead."},
                  {"role": "hacker", "text": "  lots   of   space "}, {"role": "agent", "text": ""}, "junk"]
    r = _client().post("/api/calls/practice/end", json={"room": f"practice-{U1}-abc", "duration_seconds": 185,
                                                         "transcript": transcript, "role": "PM"})
    assert r.status_code == 201
    call = _client().get(f"/api/calls/{r.json['call']['id']}").json["call"]
    assert call["kind"] == "practice" and call["status"] == "completed" and call["duration_seconds"] == 185
    assert call["caller_name"] == "Practice screen · PM" and call["summary"] == "Good call for PM."
    assert call["transcript"] == [{"role": "agent", "text": "Hi, is now a good time?"},
                                  {"role": "user", "text": "Yes, go ahead."},
                                  {"role": "user", "text": "lots of space"}]
    assert ran


def test_practice_end_without_answers_is_missed(conn):
    r = _client().post("/api/calls/practice/end", json={"room": f"practice-{U1}-x", "transcript": []})
    assert r.json["call"]["status"] == "missed" and "didn't answer" in r.json["call"]["summary"]


def test_practice_end_rejects_someone_elses_room(conn):
    r = _client().post("/api/calls/practice/end", json={"room": f"practice-{U2}-x", "transcript": []})
    assert r.status_code == 400


def test_summary_falls_back_without_gemini(monkeypatch):
    import ai_engine
    def boom(*a, **k):
        raise ValueError("no key")
    monkeypatch.setattr(ai_engine, "_call_gemini", boom)
    t = [{"role": "agent", "text": "Q"}, {"role": "user", "text": "A"}]
    assert call_log.summarize_practice(t, "PM", None) == "Practice recruiter screen for PM. You answered 1 time."


def test_deleting_account_removes_calls(conn):
    from db import delete_user_data
    _client(U2).post("/api/calls", json=BODY)
    delete_user_data(conn, U2)
    assert conn.execute("SELECT COUNT(*) FROM calls WHERE user_id = ?", (U2,)).fetchone()[0] == 0


PG_URL = os.getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")
def test_add_call_on_postgres(monkeypatch):
    import db
    monkeypatch.setenv("DB_PATH", db.DB_PATH)
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    pg = importlib.reload(db)
    try:
        c = pg.init_db()
        c.execute("INSERT INTO users (id, username, password_hash) VALUES (9701, 'pgcalls', 'x') ON CONFLICT (id) DO NOTHING")
        c.commit()
        saved = call_log.add_call(c, 9701, call_log.validate(BODY), kind="practice",
                                  transcript=[{"role": "user", "text": "hi"}])
        assert saved["id"] and saved["kind"] == "practice" and saved["has_transcript"]
        assert call_log.list_calls(c, 9701)[0]["id"] == saved["id"]
        key = call_log.new_api_key(c, 9701)
        assert call_log.user_for_api_key(c, key) == 9701
        c.execute("DELETE FROM calls WHERE user_id = 9701")
        c.execute("DELETE FROM users WHERE id = 9701")
        c.commit()
        c.close()
    finally:
        monkeypatch.delenv("DATABASE_URL")
        importlib.reload(db)
