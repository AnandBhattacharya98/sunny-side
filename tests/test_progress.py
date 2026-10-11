"""Progress page numbers, interview times and calendar invites."""
from datetime import datetime, timedelta, timezone

import pytest

import dashboard
import progress
from db import get_conn, DB_PATH

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)  # a Wednesday


@pytest.fixture
def conn():
    c = get_conn(DB_PATH)
    c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (501, 'pg501', 'x')")
    c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (502, 'pg502', 'x')")
    c.execute("DELETE FROM application_timeline WHERE job_id LIKE 'pg_%'")
    c.execute("DELETE FROM jobs WHERE user_id IN (501, 502)")
    jobs = [("pg_a", "scored", "linkedin"), ("pg_b", "shortlisted", "linkedin"), ("pg_c", "applied", "linkedin"),
            ("pg_d", "interviewing", "direct"), ("pg_e", "offer", "direct"), ("pg_f", "rejected", "linkedin")]
    for jid, st, src in jobs:
        c.execute("INSERT INTO jobs (job_id, title, company, status, source, user_id) VALUES (?, 'PM', ?, ?, ?, 501)",
                  (jid, jid.upper(), st, src))
    c.execute("INSERT INTO jobs (job_id, title, company, status, source, user_id) VALUES ('pg_x', 'PM', 'X', 'offer', 'direct', 502)")
    moves = [("pg_c", "Status → applied", NOW - timedelta(days=1)),
             ("pg_d", "Pipeline → applied", NOW - timedelta(days=9)),
             ("pg_e", "Bulk → applied", NOW - timedelta(days=20)),
             ("pg_f", "Email sent → applied", NOW - timedelta(days=2)),
             ("pg_f", "Status → rejected", NOW - timedelta(days=1))]
    for jid, ev, at in moves:
        c.execute("INSERT INTO application_timeline (job_id, event, created_at) VALUES (?, ?, ?)", (jid, ev, at.isoformat()))
    c.commit()
    yield c
    c.close()


def test_funnel_counts_furthest_stage(conn):
    data = progress.compute_progress(conn, 501, now=NOW)
    counts = {s["stage"]: s["count"] for s in data["funnel"]}
    # The rejected job had applied first, so it counts as applied.
    assert counts == {"saved": 6, "shortlisted": 5, "applied": 4, "interviewing": 2, "offer": 1}
    assert data["funnel"][2]["rate"] == 80
    assert data["rejected"] == 1
    assert data["total_applied"] == 4 and data["reply_rate"] == 75


def test_reply_rate_by_source(conn):
    data = progress.compute_progress(conn, 501, now=NOW)
    by = {r["source"]: r for r in data["sources"]}
    assert (by["direct"]["applied"], by["direct"]["replied"], by["direct"]["rate"]) == (2, 2, 100)
    assert (by["linkedin"]["applied"], by["linkedin"]["replied"]) == (2, 1)


def test_applications_per_week(conn):
    weeks = progress.compute_progress(conn, 501, now=NOW)["weeks"]
    assert len(weeks) == 8 and weeks[-1]["start"] == "2026-10-12"
    assert [w["applied"] for w in weeks[-4:]] == [1, 0, 1, 2]


def test_progress_page_is_per_user(conn):
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 501
        html = c.get("/progress").get_data(as_text=True)
    assert "From saved to offer" in html and "Applications sent" in html


# ── Interviews ───────────────────────────────────────────────────────────

@pytest.fixture
def client(conn):
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 501
        yield c


def test_round_with_time_and_calendar_invite(client, conn):
    res = client.post("/api/job/pg_d/interview-round", json={"round": "Round 2, Case; Final", "when": "2026-10-20T09:30:00.000Z"})
    assert res.get_json()["ok"]
    assert conn.execute("SELECT interview_at FROM jobs WHERE job_id = 'pg_d'").fetchone()[0] == "2026-10-20T09:30:00Z"
    ics = client.get("/api/job/pg_d/interview.ics")
    assert ics.status_code == 200 and ics.mimetype == "text/calendar"
    body = ics.get_data(as_text=True)
    assert "DTSTART:20261020T093000Z" in body and "DTEND:20261020T103000Z" in body
    assert r"SUMMARY:Round 2\, Case\; Final: PM at PG_D" in body
    assert body.count("BEGIN:VALARM") == 2 and "\r\n" in body
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))


def test_bad_time_and_other_users_job(client):
    assert client.post("/api/job/pg_d/interview-round", json={"round": "R1", "when": "next tuesday"}).status_code == 400
    assert client.get("/api/job/pg_x/interview.ics").status_code == 404
    assert client.get("/api/job/pg_a/interview.ics").status_code == 404  # no time set


def test_sunny_reminds_about_tomorrows_interview(client, conn):
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    conn.execute("UPDATE jobs SET interview_round = 'Round 1', interview_at = ? WHERE job_id = 'pg_d'",
                 (tomorrow.strftime("%Y-%m-%dT%H:%M:%SZ"),))
    conn.commit()
    data = client.get("/api/voice/welcome?lang=en", headers={"X-Timezone": "UTC"}).get_json()
    assert data["interview_reminder"] == "Your Round 1 with PG_D is tomorrow at 10:00 AM. Want to practise in Quiz Mode?"
    hi = client.get("/api/voice/welcome?lang=hi", headers={"X-Timezone": "UTC"}).get_json()
    assert hi["interview_reminder"].startswith("कल 10:00 AM")
