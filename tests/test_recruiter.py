"""Recruiter path: recruiter accounts, pasting a job, admin approval, matching to seekers, applying."""
import json
from datetime import datetime, timedelta

import pytest

import dashboard
import recruiter as rec
from auth import hash_password
from db import get_conn, DB_PATH, init_db, delete_user_data

SEEKER_FIT, SEEKER_MISS, SEEKER_LATE = 811, 812, 813
DESC = ("We are hiring a Senior Backend Engineer to build our payments platform. You will design Python services, "
        "own PostgreSQL schemas, review code, mentor two engineers and work with product managers on the roadmap. "
        "We ship weekly, care about tests, and run everything on AWS with Terraform. The team is twelve people "
        "across Bengaluru and Pune, and we meet in person once a month.")
JD = f"""Senior Backend Engineer
Company: Acme Labs
Location: Bengaluru (Hybrid)
Salary: ₹30-40 LPA
Full-time

{DESC}

Requirements
- 5+ years building backend services in Python
- PostgreSQL and AWS
- Clear written communication

Apply at https://acme.example.com/jobs/42
"""
FORM = {"title": "Senior Backend Engineer", "company": "Acme Labs", "location": "Bengaluru", "salary": "₹30-40 LPA",
        "employment_type": "Full-time", "experience": "5+ years", "requirements": "Python\nPostgreSQL\nAWS",
        "description": DESC, "apply_url": "acme.example.com/jobs/42"}


STARTED = []  # posts whose background matching was kicked off


def fake_score(title, company, description, resume_text=None, api_key=None, user_id=None):
    score = 8.5 if "python" in (resume_text or "").lower() else 3.0
    return {"score": score, "fit_summary": f"fit {score}", "key_requirements": ["Python"],
            "matched_skills": json.dumps(["Python"]), "missing_skills": json.dumps(["Terraform"])}


@pytest.fixture
def conn(monkeypatch):
    init_db(DB_PATH)
    dashboard.login_throttle.reset()
    dashboard._disabled_cache.clear()
    dashboard._recruiter_cache.clear()
    STARTED.clear()
    monkeypatch.setattr(rec, "match_post_in_background", lambda pid: STARTED.append(pid))
    monkeypatch.setattr(dashboard, "_notify_async", lambda *a, **k: None)
    c = get_conn(DB_PATH)
    for t in ("recruiter_posts", "recruiter_matches", "recruiter_applications"):
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM users WHERE account_type = 'recruiter'")
    for uid in (SEEKER_FIT, SEEKER_MISS, SEEKER_LATE):
        c.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM users WHERE id = ?", (uid,))
    c.execute("INSERT INTO users (id, username, password_hash, resume_text, name, email) VALUES (?, ?, ?, ?, ?, ?)",
              (SEEKER_FIT, "rec_seeker_fit", hash_password("pw-12345678"), "Backend engineer, 6 years of Python and Postgres",
               "Asha Fit", "asha@example.com"))
    c.execute("INSERT INTO users (id, username, password_hash, resume_text) VALUES (?, ?, ?, ?)",
              (SEEKER_MISS, "rec_seeker_miss", hash_password("pw-12345678"), "Pastry chef with ten years in kitchens"))
    c.commit()
    yield c
    c.close()


def client_for(uid=None):
    c = dashboard.app.test_client()
    if uid:
        with c.session_transaction() as s:
            s["user_id"] = uid
    return c


def signup_recruiter(email="hire@acme.example.com", company="Acme Labs"):
    c = client_for()
    r = c.post("/hiring/signup", data={"name": "Priya Recruiter", "company": company, "email": email,
                                       "password": "hiring-pass-1"})
    return c, r


def recruiter_id(conn, email="hire@acme.example.com"):
    return conn.execute("SELECT id FROM users WHERE username = ?", (email,)).fetchone()[0]


def test_recruiter_signup_makes_a_separate_account(conn):
    c, r = signup_recruiter()
    assert r.status_code == 302 and r.headers["Location"].endswith("/recruiter/new")
    row = conn.execute("SELECT account_type, company_name, email, share_profile, daily_recs_enabled FROM users WHERE username = ?",
                       ("hire@acme.example.com",)).fetchone()
    assert tuple(row) == ("recruiter", "Acme Labs", "hire@acme.example.com", 0, 0)
    assert c.get("/recruiter").status_code == 200
    # A recruiter gets no board, Sunny or seeker APIs
    assert c.get("/").headers["Location"].endswith("/recruiter")
    assert c.get("/api/followups").status_code == 403
    # Same email twice is refused
    _, again = signup_recruiter()
    assert b"already an account" in again.data


def test_seekers_cant_open_recruiter_pages(conn):
    c = client_for(SEEKER_FIT)
    assert c.get("/recruiter").headers["Location"].endswith("/hiring")
    assert c.post("/api/recruiter/parse", json={"text": JD}).status_code == 403


def test_hiring_page_is_public_and_linked(conn):
    r = client_for().get("/hiring")
    assert r.status_code == 200 and b"Create a recruiter account" in r.data
    assert b'href="/hiring"' in client_for().get("/").data
    assert b'href="/hiring"' in client_for().get("/signup").data


def test_parse_without_ai_reads_the_labelled_fields(conn, monkeypatch):
    import ai_engine
    monkeypatch.setattr(ai_engine, "ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    f = rec.parse_posting(JD, company_hint="Fallback Co")
    assert f["mode"] == "local"
    assert f["title"] == "Senior Backend Engineer"
    assert f["company"] == "Acme Labs"
    assert f["location"].startswith("Bengaluru")
    assert "30-40" in f["salary"]
    assert f["employment_type"] == "Full-time"
    assert f["requirements"][0].startswith("5+ years")
    assert f["apply_url"] == "https://acme.example.com/jobs/42"

    c, _ = signup_recruiter()
    r = c.post("/api/recruiter/parse", json={"text": JD})
    assert r.get_json()["fields"]["title"] == "Senior Backend Engineer"
    assert c.post("/api/recruiter/parse", json={"text": "too short"}).status_code == 400


def test_short_or_empty_descriptions_are_refused(conn):
    c, _ = signup_recruiter()
    r = c.post("/recruiter/new", data={**FORM, "description": "Great job. Apply now!"})
    assert r.status_code == 400 and b"too short" in r.data
    assert conn.execute("SELECT COUNT(*) FROM recruiter_posts").fetchone()[0] == 0


def test_first_post_waits_for_admin_then_later_posts_go_live(conn):
    c, _ = signup_recruiter()
    rid = recruiter_id(conn)
    assert c.post("/recruiter/new", data=FORM).status_code == 302
    first = conn.execute("SELECT post_id, status, apply_url FROM recruiter_posts WHERE recruiter_id = ?", (rid,)).fetchone()
    assert first[1] == "pending" and first[2] == "https://acme.example.com/jobs/42"
    assert STARTED == []
    # Not public while it waits; the admin sees it in the console
    assert client_for().get(f"/jobs/r/{first[0]}").status_code == 404
    assert b"Senior Backend Engineer" in client_for(1).get("/users").data
    # Only the admin can approve
    assert c.post(f"/api/admin/recruiter-posts/{first[0]}/approve").status_code == 403
    assert client_for(1).post(f"/api/admin/recruiter-posts/{first[0]}/approve").get_json()["ok"]
    post = rec.get_post(conn, first[0])
    assert post["status"] == "live" and post["expires_at"] and STARTED == [first[0]]
    assert client_for().get(f"/jobs/r/{first[0]}").status_code == 200

    c.post("/recruiter/new", data={**FORM, "title": "Staff Backend Engineer"})
    second = conn.execute("SELECT post_id, status FROM recruiter_posts WHERE title = 'Staff Backend Engineer'").fetchone()
    assert second[1] == "live" and STARTED[-1] == second[0]


def test_rejected_post_goes_back_for_review_after_an_edit(conn):
    c, _ = signup_recruiter()
    c.post("/recruiter/new", data=FORM)
    pid = conn.execute("SELECT post_id FROM recruiter_posts").fetchone()[0]
    client_for(1).post(f"/api/admin/recruiter-posts/{pid}/reject", json={"note": "Add the salary band"})
    assert rec.get_post(conn, pid)["review_note"] == "Add the salary band"
    assert b"Add the salary band" in c.get(f"/recruiter/post/{pid}").data
    c.post(f"/recruiter/post/{pid}/edit", data={**FORM, "salary": "₹32-38 LPA"})
    p = rec.get_post(conn, pid)
    assert p["status"] == "pending" and p["salary"] == "₹32-38 LPA"


def live_post(conn, rid=None):
    if rid is None:
        _, _ = signup_recruiter()
        rid = recruiter_id(conn)
        conn.execute("UPDATE users SET recruiter_trusted = 1 WHERE id = ?", (rid,))
        conn.commit()
    fields, err = rec.validate_fields(FORM)
    assert not err
    return rec.create_post(conn, rid, fields)


def test_matching_adds_post_only_to_boards_it_fits(conn):
    post = live_post(conn)
    assert rec.match_post(post["post_id"], score_fn=fake_score) == 1
    job = conn.execute("SELECT source, status, ai_score, url, job_id FROM jobs WHERE user_id = ?", (SEEKER_FIT,)).fetchone()
    assert tuple(job[:4]) == ("recruiter", "scored", 8.5, f"/jobs/r/{post['post_id']}")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (SEEKER_MISS,)).fetchone()[0] == 0
    # Scored once: running again adds nothing, even after the seeker deletes the card
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job[4],))
    conn.commit()
    assert rec.match_post(post["post_id"], score_fn=fake_score) == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = ?", (SEEKER_FIT,)).fetchone()[0] == 0


def test_new_seekers_pick_up_live_posts_on_their_next_sync(conn):
    post = live_post(conn)
    rec.match_post(post["post_id"], score_fn=fake_score)
    conn.execute("INSERT INTO users (id, username, password_hash, resume_text) VALUES (?, ?, 'x', ?)",
                 (SEEKER_LATE, "rec_seeker_late", "Python developer"))
    conn.commit()
    assert rec.match_open_posts_for_user(SEEKER_LATE, score_fn=fake_score) == 1
    assert rec.match_open_posts_for_user(SEEKER_LATE, score_fn=fake_score) == 0


def test_board_shows_the_recruiter_badge_and_ranks_ties_first(conn):
    post = live_post(conn)
    rec.match_post(post["post_id"], score_fn=fake_score)
    conn.execute("INSERT INTO jobs (job_id, title, company, url, source, ai_score, status, user_id) "
                 "VALUES ('li_tie_u811', 'Backend Engineer', 'Other Co', 'https://linkedin.com/x', 'linkedin', 8.5, 'scored', ?)",
                 (SEEKER_FIT,))
    conn.commit()
    html = client_for(SEEKER_FIT).get("/").get_data(as_text=True)
    assert "Posted directly by recruiter" in html
    assert html.index(f'data-job-id="rp_{post["post_id"]}_u{SEEKER_FIT}"') < html.index('data-job-id="li_tie_u811"')
    assert 'data-source-type="recruiter"' in html


def test_seeker_sends_resume_and_recruiter_sees_it(conn):
    post = live_post(conn)
    rid = post["recruiter_id"]
    rec.match_post(post["post_id"], score_fn=fake_score)
    jid = rec.seeker_job_id(post["post_id"], SEEKER_FIT)
    seeker = client_for(SEEKER_FIT)
    modal = seeker.get(f"/job/{jid}/modal").get_data(as_text=True)
    assert "Send my resume" in modal and "asha@example.com" in modal

    r = seeker.post(f"/api/job/{jid}/recruiter-apply", json={"email": "asha@example.com", "note": "Can join in 30 days"})
    assert r.get_json()["ok"]
    assert conn.execute("SELECT status FROM jobs WHERE job_id = ?", (jid,)).fetchone()[0] == "applied"
    again = seeker.post(f"/api/job/{jid}/recruiter-apply", json={"email": "asha@example.com"})
    assert again.status_code == 400 and "already" in again.get_json()["error"]
    assert "Resume sent" in seeker.get(f"/job/{jid}/modal").get_data(as_text=True)

    rc = client_for(rid)
    page = rc.get(f"/recruiter/post/{post['post_id']}").get_data(as_text=True)
    assert "Asha Fit" in page and "8.5/10" in page and "Can join in 30 days" in page and "6 years of Python" in page
    home = rc.get("/recruiter").get_data(as_text=True)
    assert "Resumes" in home

    # Another recruiter can't see it, and a seeker can't apply to a card that isn't a recruiter post
    other, _ = signup_recruiter("other@beta.example.com", "Beta")
    assert other.get(f"/recruiter/post/{post['post_id']}").status_code == 404
    assert seeker.post("/api/job/li_nope/recruiter-apply", json={"email": "a@b.co"}).status_code == 400


def test_closing_and_expiry_mark_seeker_cards_closed(conn):
    post = live_post(conn)
    rec.match_post(post["post_id"], score_fn=fake_score)
    jid = rec.seeker_job_id(post["post_id"], SEEKER_FIT)
    rc = client_for(post["recruiter_id"])
    rc.post(f"/recruiter/post/{post['post_id']}/close")
    assert rec.get_post(conn, post["post_id"])["status"] == "closed"
    assert conn.execute("SELECT closed_at FROM jobs WHERE job_id = ?", (jid,)).fetchone()[0]
    r = client_for(SEEKER_FIT).post(f"/api/job/{jid}/recruiter-apply", json={"email": "asha@example.com"})
    assert "no longer open" in r.get_json()["error"]

    rc.post(f"/recruiter/post/{post['post_id']}/renew")
    assert rec.get_post(conn, post["post_id"])["status"] == "live"
    assert conn.execute("SELECT closed_at FROM jobs WHERE job_id = ?", (jid,)).fetchone()[0] is None

    conn.execute("UPDATE recruiter_posts SET expires_at = ? WHERE post_id = ?",
                 ((datetime.now() - timedelta(minutes=1)).isoformat(), post["post_id"]))
    conn.commit()
    assert rec.expire_posts(conn) == 1
    assert rec.get_post(conn, post["post_id"])["status"] == "expired"
    assert conn.execute("SELECT closed_at FROM jobs WHERE job_id = ?", (jid,)).fetchone()[0]


def test_edits_reach_seekers_cards(conn):
    post = live_post(conn)
    rec.match_post(post["post_id"], score_fn=fake_score)
    client_for(post["recruiter_id"]).post(f"/recruiter/post/{post['post_id']}/edit",
                                          data={**FORM, "title": "Lead Backend Engineer", "location": "Remote"})
    row = conn.execute("SELECT title, location FROM jobs WHERE job_id = ?",
                       (rec.seeker_job_id(post["post_id"], SEEKER_FIT),)).fetchone()
    assert tuple(row) == ("Lead Backend Engineer", "Remote")


def test_open_post_and_daily_limits(conn, monkeypatch):
    post = live_post(conn)
    rid = post["recruiter_id"]
    fields, _ = rec.validate_fields(FORM)
    for _ in range(rec.MAX_OPEN_POSTS - 1):
        rec.create_post(conn, rid, fields)
    assert "open posts" in rec.posting_limits_error(conn, rid)
    r = client_for(rid).post("/recruiter/new", data=FORM)
    assert r.status_code == 400
    # Closing frees a slot, but the daily cap still counts today's posts
    monkeypatch.setattr(rec, "MAX_NEW_PER_DAY", rec.MAX_OPEN_POSTS)
    rec.close_post(conn, post["post_id"])
    assert "daily limit" in rec.posting_limits_error(conn, rid)


def test_deleting_a_recruiter_closes_their_posts_for_seekers(conn):
    post = live_post(conn)
    rec.match_post(post["post_id"], score_fn=fake_score)
    delete_user_data(conn, post["recruiter_id"])
    assert rec.get_post(conn, post["post_id"]) is None
    assert conn.execute("SELECT closed_at FROM jobs WHERE job_id = ?",
                        (rec.seeker_job_id(post["post_id"], SEEKER_FIT),)).fetchone()[0]


def test_recruiters_are_left_out_of_daily_runs(conn, monkeypatch):
    signup_recruiter()
    rid = recruiter_id(conn)
    synced = []
    monkeypatch.setattr(dashboard, "run_user_sync", lambda uid, min_score=None: synced.append(uid))
    monkeypatch.setattr(dashboard, "send_weekly_summary_if_due", lambda *a, **k: False)
    monkeypatch.setattr(dashboard, "recipient_for_user", lambda uid: "")
    conn.execute("UPDATE users SET daily_recs_enabled = 1 WHERE id = ?", (rid,))
    conn.commit()
    dashboard.run_daily_recommendations()
    assert rid not in synced and SEEKER_FIT in synced


PG_URL = __import__("os").getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set")
def test_recruiter_flow_on_postgres(monkeypatch):
    import importlib
    import db
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    pg = importlib.reload(db)
    pgrec = importlib.reload(rec)
    try:
        c = pg.init_db()
        for t in ("recruiter_posts", "recruiter_matches", "recruiter_applications"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM jobs WHERE user_id IN (9801, 9802)")
        c.execute("DELETE FROM users WHERE id IN (9801, 9802)")
        c.execute("INSERT INTO users (id, username, password_hash, account_type, company_name, recruiter_trusted) "
                  "VALUES (9801, 'pgrecruiter@x.co', 'x', 'recruiter', 'Acme', 1)")
        c.execute("INSERT INTO users (id, username, password_hash, resume_text, email) "
                  "VALUES (9802, 'pgseeker', 'x', 'Python backend engineer', 'pg@x.co')")
        c.commit()
        assert pgrec.is_recruiter(c, 9801) and not pgrec.is_recruiter(c, 9802)
        fields, err = pgrec.validate_fields(FORM)
        assert not err
        post = pgrec.create_post(c, 9801, fields)
        assert post["status"] == "live"
        assert pgrec.match_post(post["post_id"], score_fn=fake_score) >= 1
        assert pgrec.match_post(post["post_id"], score_fn=fake_score) == 0
        jid = pgrec.seeker_job_id(post["post_id"], 9802)
        ok, msg, _ = pgrec.apply(c, 9802, jid, "pg@x.co", "hi")
        assert ok, msg
        assert pgrec.apply(c, 9802, jid, "pg@x.co")[0] is False
        assert pgrec.applicants(c, post["post_id"])[0]["score"] == 8.5
        assert pgrec.recruiter_posts(c, 9801)[0]["applicant_count"] == 1
        pgrec.update_post(c, post, {**fields, "title": "Lead"})
        c.execute("UPDATE recruiter_posts SET expires_at = '2000-01-01' WHERE post_id = ?", (post["post_id"],))
        c.commit()
        assert pgrec.expire_posts(c) == 1
        pg.delete_user_data(c, 9801)
        pg.delete_user_data(c, 9802)
        c.close()
    finally:
        monkeypatch.delenv("DATABASE_URL")
        importlib.reload(db)
        importlib.reload(rec)
