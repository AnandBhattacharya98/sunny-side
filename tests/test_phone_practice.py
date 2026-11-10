"""Phone practice over LiveKit SIP: dial-in codes, Call me, and the agent's logging endpoints."""
import json

import pytest

import calls as call_log
import dashboard
import phone_practice as phone
from db import get_conn, DB_PATH

U1, U2 = 711, 712
SECRET = "agent-secret-" + "x" * 20
AGENT = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://demo.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIdemo")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 40)
    monkeypatch.setenv("SUNNY_AGENT_SECRET", SECRET)
    monkeypatch.setenv("LIVEKIT_SIP_NUMBER", "+1 415 555 0100")
    monkeypatch.setenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "ST_out123")
    monkeypatch.delenv("PHONE_PRACTICE_COUNTRIES", raising=False)
    monkeypatch.setattr(dashboard, "run_in_background", lambda key, fn, *a: fn(*a))
    monkeypatch.setattr(call_log, "summarize_practice", lambda t, role, key: f"Phone call for {role}.")
    c = get_conn(DB_PATH)
    for uid in (U1, U2):
        c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (?, ?, 'x')", (uid, f"phone{uid}"))
        c.execute("UPDATE users SET name = 'Anand', designation = 'Product Manager', resume_text = 'PM at X' "
                  "WHERE id = ?", (uid,))
    c.execute("DELETE FROM calls WHERE user_id IN (?, ?)", (U1, U2))
    c.execute("DELETE FROM phone_codes")
    c.commit()
    dashboard.ve.rate_limiter = dashboard.ve.RateLimiter()
    yield c
    c.close()


def _client(uid=U1):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _agent():
    return dashboard.app.test_client()


# ── Dial in ──

def test_code_then_claim_gives_agent_the_callers_details(conn):
    r = _client().post("/api/calls/phone/code", json={"role": "Data Analyst"})
    assert r.status_code == 200 and len(r.json["code"]) == 6 and r.json["code"].isdigit()
    assert r.json["number"] == "+1 415 555 0100" and r.json["expires_in"] == 600
    code = r.json["code"]
    # The code itself is never stored, only a keyed hash
    assert not conn.execute("SELECT 1 FROM phone_codes WHERE code_hash = ?", (code,)).fetchone()

    claim = _agent().post("/api/agent/phone/claim", headers=AGENT,
                          json={"code": " ".join(code), "room": "phone-in-_+14155550199_abc", "caller": "+919876543210"})
    assert claim.status_code == 200
    assert claim.json["details"] == {"candidate_name": "Anand", "role": "Data Analyst", "company": "",
                                     "resume": "PM at X"}
    ref = phone.read_call_ref(dashboard.app.secret_key, claim.json["call_ref"])
    assert ref["u"] == U1 and ref["p"] == "Phone •••• 3210"
    # Codes work once
    again = _agent().post("/api/agent/phone/claim", headers=AGENT, json={"code": code, "room": "phone-in-2"})
    assert again.status_code == 404


def test_new_code_replaces_the_old_one(conn):
    first = _client().post("/api/calls/phone/code", json={}).json["code"]
    second = _client().post("/api/calls/phone/code", json={}).json["code"]
    if first != second:
        assert _agent().post("/api/agent/phone/claim", headers=AGENT,
                             json={"code": first, "room": "phone-in-a"}).status_code == 404
    assert _agent().post("/api/agent/phone/claim", headers=AGENT,
                         json={"code": second, "room": "phone-in-b"}).status_code == 200


def test_expired_code_is_refused(conn):
    code = _client().post("/api/calls/phone/code", json={}).json["code"]
    conn.execute("UPDATE phone_codes SET expires_at = '2000-01-01T00:00:00Z'")
    conn.commit()
    assert _agent().post("/api/agent/phone/claim", headers=AGENT,
                         json={"code": code, "room": "phone-in-a"}).status_code == 404


def test_three_guesses_per_call(conn):
    code = _client().post("/api/calls/phone/code", json={}).json["code"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(3):
        r = _agent().post("/api/agent/phone/claim", headers=AGENT, json={"code": wrong, "room": "phone-in-z"})
        assert r.status_code == 404 and r.json["retry"] is True
    r = _agent().post("/api/agent/phone/claim", headers=AGENT, json={"code": code, "room": "phone-in-z"})
    assert r.status_code == 429 and r.json["retry"] is False


def test_agent_endpoints_need_the_shared_secret(conn):
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": SECRET}):
        assert _agent().post("/api/agent/phone/claim", headers=headers,
                             json={"code": "123456", "room": "phone-in-a"}).status_code == 401
        assert _agent().post("/api/agent/calls/end", headers=headers, json={}).status_code == 401
    # A signed-in user can't call them either
    assert _client().post("/api/agent/calls/end", json={}).status_code == 401


def test_agent_endpoints_off_without_a_secret(conn, monkeypatch):
    monkeypatch.setenv("SUNNY_AGENT_SECRET", "")
    assert _agent().post("/api/agent/phone/claim", headers={"Authorization": "Bearer "},
                         json={"code": "123456", "room": "phone-in-a"}).status_code == 401


def test_agent_endpoints_skip_csrf(conn):
    dashboard.app.config["CSRF_ENABLED"] = True
    assert _client().post("/api/calls/phone/code", json={}).status_code == 403  # the page still needs its token
    r = _agent().post("/api/agent/phone/claim", headers=AGENT, json={"code": "123456", "room": "phone-in-q"})
    assert r.status_code == 404  # reached the handler, not stopped by CSRF


def test_claim_rejects_rooms_that_arent_dial_in(conn):
    r = _agent().post("/api/agent/phone/claim", headers=AGENT, json={"code": "123456", "room": "practice-1-x"})
    assert r.status_code == 400


# ── Logging the call ──

def _claimed_ref(room="phone-in-log"):
    code = _client().post("/api/calls/phone/code", json={"role": "PM"}).json["code"]
    return _agent().post("/api/agent/phone/claim", headers=AGENT,
                         json={"code": code, "room": room, "caller": "+14155550123"}).json["call_ref"]


def test_agent_logs_the_finished_call_once(conn):
    ref = _claimed_ref()
    body = {"call_ref": ref, "room": "phone-in-log", "duration_seconds": 190,
            "transcript": [{"role": "agent", "text": "Hi Anand"}, {"role": "user", "text": "Hi Maya"}]}
    r = _agent().post("/api/agent/calls/end", headers=AGENT, json=body)
    assert r.status_code == 201
    call = _client().get(f"/api/calls/{r.json['call']['id']}").json["call"]
    assert call["kind"] == "phone" and call["status"] == "completed" and call["duration_seconds"] == 190
    assert call["caller_name"] == "Phone practice · PM" and call["phone_number"] == "Phone •••• 0123"
    assert call["summary"] == "Phone call for PM." and len(call["transcript"]) == 2
    # The agent retrying doesn't log it twice
    assert _agent().post("/api/agent/calls/end", headers=AGENT, json=body).json.get("duplicate")
    assert len(_client().get("/api/calls").json["calls"]) == 1
    assert _client(U2).get("/api/calls").json["calls"] == []


def test_end_refuses_forged_or_mismatched_refs(conn):
    ref = _claimed_ref()
    assert _agent().post("/api/agent/calls/end", headers=AGENT,
                         json={"call_ref": ref, "room": "phone-in-other"}).status_code == 400
    assert _agent().post("/api/agent/calls/end", headers=AGENT,
                         json={"call_ref": ref + "x", "room": "phone-in-log"}).status_code == 400
    other = phone.make_call_ref("another-secret", user_id=U1, room="phone-in-log", role="", phone="")
    assert _agent().post("/api/agent/calls/end", headers=AGENT,
                         json={"call_ref": other, "room": "phone-in-log"}).status_code == 400


def test_unanswered_call_is_logged_missed(conn):
    ref = _claimed_ref("phone-in-quiet")
    r = _agent().post("/api/agent/calls/end", headers=AGENT,
                      json={"call_ref": ref, "room": "phone-in-quiet", "transcript": []})
    call = _client().get(f"/api/calls/{r.json['call']['id']}").json["call"]
    assert call["status"] == "missed"


# ── Call me ──

def test_call_me_dispatches_agent_with_number(conn, monkeypatch):
    sent = []
    monkeypatch.setattr(phone, "dispatch_agent", lambda room, meta: sent.append((room, meta)))
    r = _client().post("/api/calls/phone/dial", json={"phone_number": "+91 98765-43210", "role": "Analyst"})
    assert r.status_code == 200 and r.json["room"].startswith(f"phone-out-{U1}-")
    room, meta = sent[0]
    assert room == r.json["room"]
    assert meta["dial_to"] == "+919876543210" and meta["trunk_id"] == "ST_out123"
    assert meta["role"] == "Analyst" and meta["candidate_name"] == "Anand"
    ref = phone.read_call_ref(dashboard.app.secret_key, meta["call_ref"])
    assert ref == {"u": U1, "r": room, "role": "Analyst", "p": "Phone •••• 3210", "k": "phone"}
    # The agent can log it with that ref
    end = _agent().post("/api/agent/calls/end", headers=AGENT,
                        json={"call_ref": meta["call_ref"], "room": room, "transcript": []})
    assert end.status_code == 201


@pytest.mark.parametrize("number,msg", [
    ("", "Enter"), ("98765 43210", "country code"), ("+0123456789", "country code"),
    ("+12", "country code"), ("+2348012345678", "starting"),
])
def test_call_me_rejects_bad_numbers(conn, monkeypatch, number, msg):
    monkeypatch.setattr(phone, "dispatch_agent", lambda room, meta: None)
    r = _client().post("/api/calls/phone/dial", json={"phone_number": number})
    assert r.status_code == 400 and msg in r.json["error"]


def test_call_me_is_rate_limited(conn, monkeypatch):
    monkeypatch.setattr(phone, "dispatch_agent", lambda room, meta: None)
    for _ in range(3):
        assert _client().post("/api/calls/phone/dial", json={"phone_number": "+14155550123"}).status_code == 200
    assert _client().post("/api/calls/phone/dial", json={"phone_number": "+14155550123"}).status_code == 429


def test_call_me_reports_livekit_failure(conn, monkeypatch):
    def boom(room, meta):
        raise RuntimeError("LiveKit CreateDispatch failed: 401")
    monkeypatch.setattr(phone, "dispatch_agent", boom)
    r = _client().post("/api/calls/phone/dial", json={"phone_number": "+14155550123"})
    assert r.status_code == 502 and "Try again" in r.json["error"]


def test_dispatch_talks_twirp_json(conn, monkeypatch):
    posted = []

    class Res:
        status_code, content, text = 200, b"{}", "{}"

        def json(self):
            return {}

    monkeypatch.setattr(phone.requests, "post", lambda url, **kw: (posted.append((url, kw)), Res())[1])
    phone.dispatch_agent("phone-out-1-ab", {"dial_to": "+1"})
    assert [u for u, _ in posted] == [
        "https://demo.livekit.cloud/twirp/livekit.RoomService/CreateRoom",
        "https://demo.livekit.cloud/twirp/livekit.AgentDispatchService/CreateDispatch",
    ]
    body = json.loads(posted[1][1]["data"])
    assert body["agent_name"] == "sunny-screener" and json.loads(body["metadata"]) == {"dial_to": "+1"}
    token = posted[1][1]["headers"]["Authorization"][7:]
    from tests.test_calls import _decode
    claims = _decode(token, "s" * 40)
    assert claims["video"] == {"roomCreate": True, "roomAdmin": True, "room": "phone-out-1-ab"}


def test_features_off_without_settings(conn, monkeypatch):
    monkeypatch.delenv("LIVEKIT_SIP_NUMBER")
    monkeypatch.delenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID")
    assert _client().post("/api/calls/phone/code", json={}).status_code == 503
    assert _client().post("/api/calls/phone/dial", json={"phone_number": "+14155550123"}).status_code == 503
    assert "Practise on your phone" not in _client().get("/calls").get_data(as_text=True)


def test_calls_page_shows_phone_card(conn):
    html = _client().get("/calls").get_data(as_text=True)
    assert "Practise on your phone" in html and "+1 415 555 0100" in html and 'href="tel:+14155550100"' in html
    assert "Call me" in html


def test_deleting_account_removes_codes(conn):
    from db import delete_user_data
    _client(U2).post("/api/calls/phone/code", json={})
    delete_user_data(conn, U2)
    assert conn.execute("SELECT COUNT(*) FROM phone_codes WHERE user_id = ?", (U2,)).fetchone()[0] == 0
