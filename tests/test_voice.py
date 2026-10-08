"""Tests for the Board Assistant. Run with: python -m pytest tests/

Uses a throwaway SQLite database and no Gemini key, so everything runs on the local rules."""
import os
import sys
import tempfile
from datetime import datetime

import pytest

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["GEMINI_API_KEY"] = ""
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_engine as ve  # noqa: E402
import dashboard  # noqa: E402
from db import get_conn, DB_PATH  # noqa: E402

JOBS = [
    {"job_id": "j1", "title": "Product Manager - Payments", "company": "Razorpay", "status": "new", "score": 8.7},
    {"job_id": "j2", "title": "Senior Product Manager", "company": "Swiggy", "status": "shortlisted", "score": 7.9},
    {"job_id": "j3", "title": "Product Manager", "company": "CRED", "status": "applied", "score": 6.5},
    {"job_id": "j4", "title": "Growth PM", "company": "New Relic", "status": "new", "score": 7.0},
]


# ── Local classifier ─────────────────────────────────────────────────────

def classify(text, history=None, context=None):
    return ve.classify_intent_and_slot(text, JOBS, chat_history=history, context_job_ids=context)


@pytest.mark.parametrize("text,intent", [
    ("What's new today?", "daily_digest"),
    ("how is my pipeline looking", "pipeline_stats"),
    ("I'd like to know my stats", "pipeline_stats"),
    ("what are my top matches", "top_matches"),
    ("help", "help"),
    ("what can you do?", "help"),
    ("hey", "greeting"),
    ("thanks!", "thanks"),
    ("refresh my listings", "trigger_refresh"),
    ("how many emails do I have", "email_count"),
    ("when did I last refresh", "last_sync"),
])
def test_general_intents(text, intent):
    assert classify(text)["intent"] == intent


@pytest.mark.parametrize("text,intent,job_id", [
    ("tell me about the Swiggy job", "job_lookup", "j2"),
    ("why did the Razorpay job get an 8?", "job_fit", "j1"),
    ("did I apply to CRED yet", "job_status", "j3"),
    ("I like the Swiggy job", "thumbs_up", "j2"),
    ("not interested in Razorpay", "thumbs_down", "j1"),
    ("archive the CRED job", "archive_job", "j3"),
    ("quiz me on Swiggy", "quiz_mode", "j2"),
    ("any replies from Razorpay?", "email_lookup", "j1"),
    ("do I have a cover letter for Swiggy", "cover_letter_status", "j2"),
    ("rewrite the cover letter for Razorpay", "regenerate_cover_letter", "j1"),
])
def test_job_intents(text, intent, job_id):
    res = classify(text)
    assert res["intent"] == intent
    assert res["slots"]["job_id"] == job_id


def test_move_uses_the_last_column_named():
    res = classify("move the New Relic job to applied")
    assert res["intent"] == "move_job"
    assert res["slots"]["job_id"] == "j4"
    assert res["slots"]["status"] == "applied"


def test_column_count_aliases():
    res = classify("how many jobs are shortlisted")
    assert res["intent"] == "column_count"
    assert res["slots"]["column"] == "shortlisted"
    assert classify("how many in my inbox")["slots"]["column"] == "new"


def test_company_match_is_whole_word():
    # "incredible" must not match CRED
    assert ve._resolve_job_locally("that was incredible", JOBS) is None


def test_generic_title_words_do_not_pick_a_job():
    assert ve._resolve_job_locally("show me a product manager role", JOBS) is None


def test_pronouns_use_cards_on_screen():
    res = classify("move it to interviewing", context=["j2", "j1"])
    assert res["slots"]["job_id"] == "j2"
    assert classify("tell me about the second one", context=["j2", "j1"])["slots"]["job_id"] == "j1"


def test_pronouns_fall_back_to_conversation():
    history = [{"role": "user", "text": "tell me about the Razorpay job"}, {"role": "model", "text": "Here it is."}]
    res = classify("why did it score that", history=history)
    assert res["intent"] == "job_fit"
    assert res["slots"]["job_id"] == "j1"


def test_validation_drops_unknown_ids_and_intents():
    res = ve.validate_classification(
        {"intent": "delete_everything", "slots": {"job_id": "someone-elses-job", "status": "Applied"}}, JOBS)
    assert res["intent"] is None
    assert res["slots"]["job_id"] is None
    assert res["slots"]["status"] == "applied"


def test_sanitizers_cap_input():
    assert len(ve.sanitize_transcript("a" * 5000)) == ve.MAX_TRANSCRIPT_CHARS
    history = [{"role": "user", "text": "x" * 1000}] * 50 + ["junk", None]
    clean = ve.sanitize_history(history)
    assert len(clean) <= ve.MAX_HISTORY_TURNS
    assert all(len(t["text"]) <= ve.MAX_HISTORY_TURN_CHARS for t in clean)
    assert ve.sanitize_job_ids(["j1", "nope", 5], JOBS) == ["j1"]


def test_rate_limiter():
    rl = ve.RateLimiter()
    assert all(rl.allow(1, "q", 3) for _ in range(3))
    assert not rl.allow(1, "q", 3)
    assert rl.allow(2, "q", 3)  # other users unaffected


def test_gemini_errors_never_include_the_key(monkeypatch):
    import requests

    def boom(*a, **kw):
        raise requests.ConnectionError("https://example.test/?key=SECRET123")
    monkeypatch.setattr(ve.requests, "post", boom)
    monkeypatch.setattr(ve.time, "sleep", lambda s: None)
    with pytest.raises(ve.GeminiError) as err:
        ve.gemini_generate("SECRET123", [{"text": "hi"}])
    assert "SECRET123" not in str(err.value)


def test_pcm_to_wav_header():
    wav = ve.pcm_to_wav(b"\x00\x00" * 10, sample_rate=24000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and len(wav) == 44 + 20


# ── Routes ────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    ve.rate_limiter.reset()
    conn = get_conn(DB_PATH)
    now = datetime.now().isoformat()
    for uid in (101, 102):
        conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, name) VALUES (?, ?, 'x', ?)",
                     (uid, f"user{uid}", "Asha Rao" if uid == 101 else "Other"))
    conn.execute("DELETE FROM jobs WHERE user_id IN (101, 102)")
    conn.execute("DELETE FROM received_emails WHERE user_id IN (101, 102)")
    for j in JOBS:
        conn.execute("INSERT INTO jobs (job_id, title, company, status, ai_score, ai_summary, location, scraped_at, user_id) "
                     "VALUES (?,?,?,?,?,?,?,?,101)",
                     (j["job_id"], j["title"], j["company"], j["status"], j["score"], "Strong payments overlap.", "Bengaluru", now))
    conn.execute("INSERT INTO jobs (job_id, title, company, status, ai_score, scraped_at, user_id) "
                 "VALUES ('other1', 'PM', 'Zepto', 'new', 9.9, ?, 102)", (now,))
    conn.execute("INSERT INTO received_emails (job_id, sender, subject, body, received_at, user_id) "
                 "VALUES ('other1', 'hr@zepto.com', 'Secret Zepto offer', 'private', ?, 102)", (now,))
    conn.commit()
    conn.close()
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 101
        yield c


def ask(client, text, **extra):
    res = client.post("/api/voice/query", json={"transcript": text, **extra})
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def test_requires_login():
    with dashboard.app.test_client() as c:
        assert c.post("/api/voice/query", json={"transcript": "hi"}).status_code == 401


def test_welcome(client):
    data = client.get("/api/voice/welcome").get_json()
    assert data["ok"] and "Asha" in data["greeting"]
    assert data["suggestions"]
    assert data["server_voice"] is False


def test_pipeline_stats_counts_shortlisted(client):
    data = ask(client, "how's my pipeline")
    card = data["reply_cards"][0]
    assert card["shortlisted"] == 1 and card["applied"] == 1 and card["total"] == 4


def test_job_fit_includes_the_reason(client):
    data = ask(client, "why did the Razorpay job score that")
    assert "Strong payments overlap" in data["reply_text"]
    assert data["context_job_ids"] == ["j1"]
    assert data["suggestions"]


def test_cannot_see_other_users_jobs_or_email(client):
    data = ask(client, "tell me about the Zepto job")
    assert all(c.get("job_id") != "other1" for c in data["reply_cards"])
    data = ask(client, "any emails from Zepto?")
    assert all("Secret" not in (c.get("subject") or "") for c in data["reply_cards"])
    # A forged context id from another user is ignored
    data = ask(client, "archive it", context_job_ids=["other1"])
    assert not data["requires_confirmation"]


def test_move_needs_confirmation_with_token(client):
    data = ask(client, "move the Swiggy job to applied")
    assert data["requires_confirmation"]
    token = data["action"]["token"]
    assert client.post("/api/voice/confirm", json={"token": "wrong"}).status_code == 409
    res = client.post("/api/voice/confirm", json={"token": token}).get_json()
    assert res["ok"] and res["board_changed"]
    conn = get_conn(DB_PATH)
    assert conn.execute("SELECT status FROM jobs WHERE job_id = 'j2'").fetchone()[0] == "applied"
    conn.close()
    # Tokens are single use
    assert client.post("/api/voice/confirm", json={"token": token}).status_code == 409


def test_follow_up_question_fills_in_the_job(client):
    data = ask(client, "archive it")
    assert "Which job" in data["reply_text"]
    data = ask(client, "the CRED one")
    assert data["intent"] == "archive_job" and data["requires_confirmation"]


def test_follow_up_question_fills_in_the_column(client):
    data = ask(client, "move the Razorpay job")
    assert "Which column" in data["reply_text"]
    data = ask(client, "shortlisted")
    assert data["intent"] == "move_job" and data["requires_confirmation"]
    assert "Shortlisted" in data["reply_text"]


def test_cancel_clears_pending(client):
    data = ask(client, "archive the CRED job")
    client.post("/api/voice/cancel")
    assert client.post("/api/voice/confirm", json={"token": data["action"]["token"]}).status_code == 409


def test_unknown_request_is_friendly(client):
    data = ask(client, "blorp")
    assert data["intent"] is None and data["suggestions"]


def test_rate_limit(client):
    for _ in range(30):
        client.post("/api/voice/query", json={"transcript": "hi"})
    assert client.post("/api/voice/query", json={"transcript": "hi"}).status_code == 429


def test_transcribe_without_key_asks_for_browser_fallback(client):
    from io import BytesIO
    res = client.post("/api/voice/transcribe", data={"file": (BytesIO(b"x" * 2000), "a.webm", "audio/webm")},
                      content_type="multipart/form-data")
    assert res.status_code == 400 and res.get_json()["provider_fallback"]


def test_transcribe_rejects_unknown_audio_types(client):
    from io import BytesIO
    res = client.post("/api/voice/transcribe", data={"file": (BytesIO(b"x" * 2000), "a.exe", "application/x-msdownload")},
                      content_type="multipart/form-data")
    assert res.status_code == 415


def test_digest(client):
    data = client.get("/api/voice/digest?peek=true").get_json()
    assert data["ok"] and data["count"] == 4
    assert all(c["job_id"] != "other1" for c in data["reply_cards"])


def test_gemini_answer_is_validated_against_the_users_jobs(monkeypatch):
    monkeypatch.setattr(ve, "_gemini_classify", lambda *a, **kw: {
        "intent": "archive_job", "slots": {"job_id": "other1", "status": None}, "ambiguous": False})
    res = ve.classify_intent_and_slot("archive the zepto job", JOBS, api_key="fake")
    assert res["intent"] == "archive_job" and res["slots"]["job_id"] is None


def test_gemini_failure_falls_back_to_local_rules(monkeypatch):
    def fail(*a, **kw):
        raise ve.GeminiError("Gemini returned HTTP 503")
    monkeypatch.setattr(ve, "gemini_generate", fail)
    res = ve.classify_intent_and_slot("tell me about the Swiggy job", JOBS, api_key="fake")
    assert res["intent"] == "job_lookup" and res["slots"]["job_id"] == "j2"


def test_speech_is_cached(monkeypatch):
    import base64 as b64
    calls = []

    def fake(api_key, parts, **kw):
        calls.append(1)
        return {"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000", "data": b64.b64encode(b"\x00\x00" * 8).decode()}}]}}]}
    monkeypatch.setattr(ve, "gemini_generate", fake)
    a = ve.synthesize_speech("Okay, cancelled.", "k")
    b = ve.synthesize_speech("Okay, cancelled.", "k")
    assert a == b and a[:4] == b"RIFF" and len(calls) == 1
