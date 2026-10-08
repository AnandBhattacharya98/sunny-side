"""Follow-up reminders and the weekly recap. Run with: python -m pytest tests/"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ["GEMINI_API_KEY"] = ""
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
import followups  # noqa: E402
import voice_engine as ve  # noqa: E402
from db import get_conn, DB_PATH  # noqa: E402

UID, OTHER = 701, 702
NOW = datetime(2026, 10, 12, 9, 0)  # a Monday


def _ago(days):
    return (NOW - timedelta(days=days)).isoformat()


@pytest.fixture
def board(monkeypatch):
    monkeypatch.setattr(followups, "datetime", type("FixedDT", (datetime,), {"now": classmethod(lambda cls: NOW)}))
    conn = get_conn(DB_PATH)
    for uid in (UID, OTHER):
        conn.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM application_timeline WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM received_emails WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.execute("INSERT INTO users (id, username, password_hash, name) VALUES (?, ?, 'x', 'Asha Rao')",
                     (uid, f"fu{uid}"))
    rows = [
        # job_id, status, scraped days ago, last timeline days ago, source, user
        ("q1", "applied", 30, 10, "linkedin", UID),       # quiet 10 days
        ("q2", "interviewing", 30, 8, "greenhouse", UID),  # quiet 8 days
        ("f1", "applied", 30, 2, "linkedin", UID),        # recent activity
        ("n1", "new", 30, None, "linkedin", UID),         # not applied
        ("o1", "applied", 30, 20, "linkedin", OTHER),     # someone else's
    ]
    for jid, status, scraped, last, source, uid in rows:
        conn.execute("INSERT INTO jobs (job_id, title, company, status, scraped_at, source, user_id) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (jid, f"PM {jid}", f"Co {jid}", status, _ago(scraped), source, uid))
        if last is not None:
            conn.execute("INSERT INTO application_timeline (job_id, event, created_at, user_id) VALUES (?, ?, ?, ?)",
                         (jid, f"Status → {status}", _ago(last), uid))
    conn.commit()
    yield conn
    conn.close()


def test_stale_applications_finds_only_quiet_applied_jobs(board):
    stale = followups.stale_applications(board, UID, now=NOW)
    assert [j["job_id"] for j in stale] == ["q1", "q2"]
    assert stale[0]["days_quiet"] == 10


def test_a_reply_email_counts_as_activity(board):
    board.execute("INSERT INTO received_emails (job_id, sender, subject, received_at, user_id) VALUES ('q1', 'hr', 'hi', ?, ?)",
                  (_ago(1), UID))
    board.commit()
    assert [j["job_id"] for j in followups.stale_applications(board, UID, now=NOW)] == ["q2"]


def test_snooze_hides_a_reminder(board):
    followups.snooze(board, UID, "q1")
    assert [j["job_id"] for j in followups.stale_applications(board, UID, now=NOW)] == ["q2"]


def test_template_draft_without_ai(board):
    job = followups.stale_applications(board, UID, now=NOW)[0]
    d = followups.draft_followup(job, candidate_name="Asha Rao")
    assert d["mode"] == "template"
    assert "PM q1" in d["body"] and "Co q1" in d["body"] and d["body"].endswith("Asha Rao")


def test_weekly_summary_counts(board):
    board.execute("INSERT INTO application_timeline (job_id, event, created_at, user_id) VALUES ('f1', 'Email sent → applied', ?, ?)",
                  (_ago(2), UID))
    board.execute("INSERT INTO application_timeline (job_id, event, created_at, user_id) VALUES ('q2', 'Pipeline → interviewing', ?, ?)",
                  (_ago(3), UID))
    board.execute("INSERT INTO jobs (job_id, title, company, status, scraped_at, source, user_id) VALUES ('w1', 'PM', 'Co', 'new', ?, 'linkedin', ?)",
                  (_ago(1), UID))
    board.commit()
    s = followups.weekly_summary(board, UID, now=NOW)
    assert s["applied"] == 2  # f1's status change + email sent
    assert s["interviews"] == 1
    assert s["new_jobs"] == 1
    linkedin = next(x for x in s["sources"] if x["source"] == "linkedin")
    assert linkedin["applied"] == 2 and linkedin["rate"] == 0
    greenhouse = next(x for x in s["sources"] if x["source"] == "greenhouse")
    assert greenhouse["rate"] == 100
    assert "gone quiet" in followups.summary_sentence(s)
    html = followups.weekly_email_html(s, name="Asha Rao", board_url="https://example.com/")
    assert "Hi Asha" in html and "Worth a follow-up" in html


def _client(uid=UID):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_followup_endpoints_are_scoped(board, monkeypatch):
    monkeypatch.setattr(followups, "FOLLOWUP_DAYS", 7)
    c = _client()
    ids = [j["job_id"] for j in c.get("/api/followups").get_json()["jobs"]]
    assert "q1" in ids and "o1" not in ids
    d = c.post("/api/job/q1/followup/draft").get_json()
    assert d["ok"] and d["subject"]
    assert c.post("/api/job/o1/followup/draft").status_code == 404
    assert c.post("/api/job/o1/followup/done", json={"sent": True}).status_code == 404
    assert c.post("/api/job/q1/followup/done", json={"sent": True}).get_json()["ok"]
    assert "q1" not in [j["job_id"] for j in c.get("/api/followups").get_json()["jobs"]]
    events = [r[0] for r in board.execute("SELECT event FROM application_timeline WHERE job_id = 'q1' AND user_id = ?", (UID,))]
    assert "Followed up" in events


def test_weekly_summary_endpoint(board):
    data = _client().get("/api/summary/weekly").get_json()
    assert data["ok"] and data["text"]


def test_weekly_email_only_on_mondays_and_once_a_week(board, monkeypatch):
    sent = []
    monkeypatch.setattr(dashboard, "send_email", lambda to, subject, html: sent.append(to) or True)
    tuesday = NOW + timedelta(days=1)
    assert not dashboard.send_weekly_summary_if_due(UID, "a@b.c", now=tuesday)
    assert dashboard.send_weekly_summary_if_due(UID, "a@b.c", now=NOW)
    assert not dashboard.send_weekly_summary_if_due(UID, "a@b.c", now=NOW + timedelta(hours=3))
    assert dashboard.send_weekly_summary_if_due(UID, "a@b.c", now=NOW + timedelta(days=7))
    assert sent == ["a@b.c", "a@b.c"]


@pytest.mark.parametrize("text,intent", [
    ("who should i follow up with", "follow_ups"),
    ("which applications have gone quiet", "follow_ups"),
    ("मुझे किसे फॉलो-अप करना चाहिए", "follow_ups"),
    ("how did my week go", "weekly_summary"),
    ("give me a weekly recap", "weekly_summary"),
    ("मेरा हफ्ता कैसा रहा", "weekly_summary"),
])
def test_sunny_understands_new_requests(text, intent):
    assert ve._local_classify(text.lower(), [])["intent"] == intent


def test_sunny_answers_follow_ups_and_week(board):
    c = _client()
    res = c.post("/api/voice/query", json={"transcript": "who should I follow up with"}).get_json()
    assert res["intent"] == "follow_ups" and "gone quiet" in res["reply_text"]
    res = c.post("/api/voice/query", json={"transcript": "how did my week go"}).get_json()
    assert res["intent"] == "weekly_summary" and res["reply_cards"][0]["type"] == "week"
    welcome = c.get("/api/voice/welcome").get_json()
    assert welcome["followup_count"] >= 1
    assert "Who should I follow up with?" in welcome["suggestions"]
