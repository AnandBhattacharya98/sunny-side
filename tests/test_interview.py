"""Mock interviews: starting one for a job, logging it, and scoring each answer."""
import json

import pytest

import calls as call_log
import dashboard
import interview
from db import get_conn, DB_PATH
from tests.test_calls import _decode

U1, U2 = 721, 722

TRANSCRIPT = [
    {"role": "agent", "text": "Hi Anand, I'm Maya. Ready?", "t": 0},
    {"role": "user", "text": "Yes, ready.", "t": 4.2},
    {"role": "agent", "text": "Tell me about a payments launch you led.", "t": 6},
    {"role": "user", "text": "I led UPI autopay and cut failures by twenty percent.", "t": 9.5},
    {"role": "agent", "text": "How would you reduce checkout drop-off?", "t": 40},
    {"role": "user", "text": "I'd look at the funnel and test fewer steps.", "t": 44.1},
]
REPLY = json.dumps({"overall": 4, "summary": "Clear stories. Add more numbers.", "questions": [
    {"question": "A payments launch you led", "line": 2, "score": 4, "worked": "Named the metric",
     "sharpen": "Say your part", "stronger": "I owned the rollout..."},
    {"question": "Reduce checkout drop-off", "line": 4, "score": "3", "worked": "Funnel first",
     "sharpen": "Pick one test", "stronger": "Cut OTP friction..."},
    {"question": "", "line": 0, "score": 5}, {"question": "no score", "line": 1},
]})


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://demo.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIdemo")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 40)
    monkeypatch.setattr(dashboard, "run_in_background", lambda key, fn, *a: fn(*a))
    c = get_conn(DB_PATH)
    for uid in (U1, U2):
        c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (?, ?, 'x')", (uid, f"int{uid}"))
        c.execute("UPDATE users SET name = 'Anand', resume_text = 'PM at X, payments' WHERE id = ?", (uid,))
    c.execute("DELETE FROM calls WHERE user_id IN (?, ?)", (U1, U2))
    c.execute("DELETE FROM jobs WHERE job_id IN ('int-j1', 'int-j2')")
    c.execute("DELETE FROM application_timeline WHERE job_id = 'int-j1'")
    c.execute("INSERT INTO jobs (job_id, title, company, description, key_reqs, status, user_id) VALUES "
              "('int-j1', 'Product Manager - Payments', 'Razorpay', 'Own checkout conversion.', 'Payments, SQL', 'applied', ?)", (U1,))
    c.execute("INSERT INTO jobs (job_id, title, company, status, user_id) VALUES ('int-j2', 'PM', 'Zepto', 'new', ?)", (U2,))
    c.commit()
    dashboard.ve.rate_limiter = dashboard.ve.RateLimiter()
    yield c
    c.close()


def _client(uid=U1):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_start_builds_interview_from_posting_and_resume(conn):
    r = _client().post("/api/calls/interview/start", json={"job_id": "int-j1", "length": "full"})
    assert r.status_code == 200 and r.json["room"].startswith(f"interview-{U1}-") and r.json["questions"] == 7
    claims = _decode(r.json["token"], "s" * 40)
    meta = json.loads(claims["roomConfig"]["agents"][0]["metadata"])
    assert meta["mode"] == "interview" and meta["role"] == "Product Manager" and meta["company"] == "Razorpay"
    assert meta["posting"] == "Own checkout conversion." and meta["key_reqs"] == "Payments, SQL"
    assert meta["resume"] == "PM at X, payments" and meta["identity"] == f"user-{U1}" and meta["questions"] == 7
    assert _client().post("/api/calls/interview/start", json={"job_id": "int-j1", "length": "huge"}).json["questions"] == 4


def test_start_only_for_your_own_jobs(conn):
    assert _client().post("/api/calls/interview/start", json={"job_id": "int-j2"}).status_code == 404


def test_start_needs_livekit(conn, monkeypatch):
    monkeypatch.delenv("LIVEKIT_URL")
    assert _client().post("/api/calls/interview/start", json={"job_id": "int-j1"}).status_code == 503


def test_end_logs_scores_and_adds_to_timeline(conn, monkeypatch):
    monkeypatch.setattr(interview, "score_interview",
                        lambda tr, role, company, posting, key, **k: interview.parse_report(REPLY, tr))
    r = _client().post("/api/calls/interview/end", json={"room": f"interview-{U1}-abc", "job_id": "int-j1",
                                                          "duration_seconds": 300, "transcript": TRANSCRIPT})
    assert r.status_code == 201
    call = _client().get(f"/api/calls/{r.json['call']['id']}").json["call"]
    assert call["kind"] == "interview" and call["job_id"] == "int-j1" and call["status"] == "completed"
    assert call["caller_name"] == "Mock interview · Product Manager at Razorpay"
    assert call["transcript"][3] == {"role": "user", "text": TRANSCRIPT[3]["text"], "t": 9.5}
    rep = call["report"]
    assert rep["overall"] == 4 and [q["score"] for q in rep["questions"]] == [4, 3]
    assert [q["t"] for q in rep["questions"]] == [9.5, 44.1]  # where each answer starts in the recording
    assert call["summary"].startswith("Mock interview for Product Manager: 4/5 across 2 questions")
    listed = _client().get("/api/calls").json["calls"][0]
    assert listed["has_report"] and listed["job_id"] == "int-j1"
    events = [r[0] for r in conn.execute("SELECT event FROM application_timeline WHERE job_id = 'int-j1'")]
    assert events == ["Mock interview: 4/5 across 2 questions"]


def test_end_too_short_is_not_scored(conn, monkeypatch):
    monkeypatch.setattr(interview, "score_interview", lambda *a, **k: pytest.fail("should not score"))
    r = _client().post("/api/calls/interview/end", json={"room": f"interview-{U1}-x", "job_id": "int-j1",
                                                          "transcript": TRANSCRIPT[:2]})
    assert r.json["call"]["status"] == "missed" and "Too short" in r.json["call"]["summary"]


def test_end_rejects_someone_elses_room(conn):
    r = _client().post("/api/calls/interview/end", json={"room": f"interview-{U2}-x", "transcript": TRANSCRIPT})
    assert r.status_code == 400


def test_scoring_failure_is_reported(conn, monkeypatch):
    monkeypatch.setattr(interview, "score_interview", lambda *a, **k: {"error": "Couldn't score this one."})
    r = _client().post("/api/calls/interview/end", json={"room": f"interview-{U1}-y", "job_id": "int-j1",
                                                          "transcript": TRANSCRIPT})
    call = _client().get(f"/api/calls/{r.json['call']['id']}").json["call"]
    assert call["report"] == {"error": "Couldn't score this one."} and "Couldn't score" in call["summary"]
    assert not conn.execute("SELECT 1 FROM application_timeline WHERE job_id = 'int-j1'").fetchone()


def test_parse_report_handles_fences_and_bad_json():
    tr = interview.clean_timed_transcript(TRANSCRIPT)
    rep = interview.parse_report("```json\n" + REPLY + "\n```", tr)
    assert rep["overall"] == 4 and len(rep["questions"]) == 2
    with pytest.raises(ValueError):
        interview.parse_report("not json", tr)
    with pytest.raises(ValueError):
        interview.parse_report('{"overall": 3, "questions": []}', tr)
    # Missing overall is averaged; out-of-range scores are clamped; bad lines give no time
    rep = interview.parse_report('{"questions": [{"question": "Q", "score": 9, "line": 99}]}', tr)
    assert rep["overall"] == 5 and rep["questions"][0]["t"] is None


def test_score_interview_tries_gemini_then_livekit(monkeypatch):
    import ai_engine
    import livekit_llm
    tr = interview.clean_timed_transcript(TRANSCRIPT)
    monkeypatch.setattr(ai_engine, "_call_gemini", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota")))
    monkeypatch.setattr(livekit_llm, "is_configured", lambda: True)
    monkeypatch.setattr(livekit_llm, "complete", lambda prompt, **k: REPLY)
    rep = interview.score_interview(tr, "PM", "Razorpay", "posting", "gemini-key")
    assert rep["overall"] == 4 and rep["source"] == "ai"
    monkeypatch.setattr(livekit_llm, "is_configured", lambda: False)
    assert "error" in interview.score_interview(tr, "PM", "", "", "gemini-key")
    assert "enough answers" in interview.score_interview(tr[:2], "PM", "", "", "k")["error"]


def test_clean_timed_transcript():
    out = interview.clean_timed_transcript([{"role": "x", "text": "  hi  there ", "t": "3.14159"},
                                            {"role": "agent", "text": "", "t": 1}, "junk",
                                            {"role": "agent", "text": "ok", "t": "nope"}])
    assert out == [{"role": "user", "text": "hi there", "t": 3.1}, {"role": "agent", "text": "ok", "t": 0.0}]


def test_calls_page_has_interview_card(conn):
    html = _client().get("/calls?interview=int-j1").get_data(as_text=True)
    assert "Mock interview" in html and 'value="int-j1" selected' in html and "Zepto" not in html


# ── Recruiter call rehearsals: salary negotiation and follow-up ──

SALARY_TRANSCRIPT = [
    {"role": "agent", "text": "Good news, we'd like to offer you the role at 26 lakh fixed.", "t": 0},
    {"role": "user", "text": "Thank you. I was looking for 32, given the payments scope.", "t": 5},
    {"role": "agent", "text": "That's above our band. What's your notice period?", "t": 12},
    {"role": "user", "text": "Sixty days. Could a joining bonus bridge the gap?", "t": 16},
]
SALARY_REPLY = json.dumps({"overall": 3, "summary": "Good anchor. Close with a date.", "questions": [
    {"question": "Anchoring and the number you asked for", "line": 1, "score": 4, "worked": "Named 32 early",
     "sharpen": "Back it with a market number", "stronger": "Similar roles pay 30 to 34."},
    {"question": "Closing with clear next steps", "line": -1, "score": 2, "worked": "Polite",
     "sharpen": "Ask when you'll get the revised offer in writing", "stronger": "Can I expect it by Friday?"},
]})


def test_rehearsal_start_sends_scenario_and_goal(conn):
    r = _client().post("/api/calls/interview/start",
                       json={"job_id": "int-j1", "scenario": "salary", "target": "  32 LPA   fixed "})
    assert r.status_code == 200 and r.json["scenario"] == "salary"
    meta = json.loads(_decode(r.json["token"], "s" * 40)["roomConfig"]["agents"][0]["metadata"])
    assert meta["mode"] == "interview" and meta["scenario"] == "salary" and meta["target"] == "32 LPA fixed"
    r = _client().post("/api/calls/interview/start", json={"job_id": "int-j1", "scenario": "bogus"})
    assert r.json["scenario"] == "interview"


def test_rehearsal_is_scored_and_saved_to_timeline(conn, monkeypatch):
    seen = {}

    def fake(tr, role, company, posting, key, scenario="interview", target=""):
        seen.update(scenario=scenario, target=target)
        return interview.parse_report(SALARY_REPLY, tr)
    monkeypatch.setattr(interview, "score_interview", fake)
    r = _client().post("/api/calls/interview/end", json={
        "room": f"interview-{U1}-sal", "job_id": "int-j1", "scenario": "salary", "target": "32 LPA",
        "transcript": SALARY_TRANSCRIPT})
    call = _client().get(f"/api/calls/{r.json['call']['id']}").json["call"]
    assert seen == {"scenario": "salary", "target": "32 LPA"}
    assert call["kind"] == "rehearsal" and call["caller_name"] == "Salary negotiation · Product Manager at Razorpay"
    assert call["summary"].startswith("Salary negotiation for Product Manager: 3/5.")
    assert [q["t"] for q in call["report"]["questions"]] == [5, None]
    events = [r[0] for r in conn.execute("SELECT event FROM application_timeline WHERE job_id = 'int-j1'")]
    assert events == ["Salary negotiation rehearsal: 3/5. Next time: Ask when you'll get the revised offer in writing."]


def test_rehearsal_prompt_uses_rubric_and_goal():
    tr = interview.clean_timed_transcript(SALARY_TRANSCRIPT)
    p = interview.rehearsal_prompt(tr, "followup", "PM", "Razorpay", "final round on 3 Nov")
    assert "recruiter follow-up" in p and "Getting a clear timeline and next step" in p
    assert "final round on 3 Nov" in p and "[1] Candidate:" in p


def test_agent_plays_the_recruiter_for_rehearsals():
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "voice_agent"))
    interviewer = pytest.importorskip("interviewer")
    d = interviewer.interview_details({"scenario": "salary", "target": "32 LPA", "role": "PM", "company": "Razorpay"})
    text = interviewer.instructions_for(d)
    assert "recruiter" in text and "32 LPA" in text and "Ask exactly" not in text
    d = interviewer.interview_details({"scenario": "nope", "questions": 5})
    assert d["scenario"] == "interview" and "Ask exactly 5 main questions" in interviewer.instructions_for(d)


def test_calls_page_offers_rehearsals(conn):
    html = _client().get("/calls").get_data(as_text=True)
    assert 'value="salary"' in html and 'value="followup"' in html
