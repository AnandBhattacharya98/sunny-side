"""Nov 2026 security hardening: headers, link and fetch guards, passwords, sessions,
cross-account writes, untrusted HTML and resume uploads."""
import binascii
import hashlib
import io
import os

import pytest

import dashboard
import security
from auth import hash_password, verify_password, signup_user, LEGACY_PBKDF2_ROUNDS, needs_rehash
from db import get_conn, DB_PATH

A, B = 801, 802


@pytest.fixture
def conn():
    c = get_conn(DB_PATH)
    for uid in (A, B):
        c.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM application_notes WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM application_timeline WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM tailored_resumes WHERE user_id = ?", (uid,))
        c.execute("INSERT OR IGNORE INTO users (id, username, password_hash, name, resume_text) "
                  "VALUES (?, ?, ?, 'Me', 'my resume')", (uid, f"hard{uid}", "x"))
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password("first-pass-phrase"), uid))
    c.execute("INSERT INTO jobs (job_id, title, company, status, url, location, user_id) VALUES "
              "('hd_a', 'PM', 'Acme', 'new', 'javascript:alert(1)', 'Pune', ?), "
              "('hd_b', 'PM', 'Beta', 'new', 'https://beta.example/jobs/1', 'Pune', ?)", (A, B))
    c.execute("INSERT OR REPLACE INTO application_notes (job_id, note, user_id) VALUES ('hd_b', 'B private note', ?)", (B,))
    c.commit()
    dashboard._disabled_cache.clear()
    yield c
    c.close()


def client_for(uid):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


# ── Headers ───────────────────────────────────────────────────────────────

def test_security_headers_on_every_page():
    res = dashboard.app.test_client().get("/login")
    h = res.headers
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "object-src 'none'" in h["Content-Security-Policy"]
    assert h["X-Frame-Options"] == "DENY"
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "microphone=(self)" in h["Permissions-Policy"]
    assert "Strict-Transport-Security" not in h  # plain http in tests


def test_hsts_behind_https_proxy():
    res = dashboard.app.test_client().get("/login", headers={"X-Forwarded-Proto": "https"})
    assert res.headers["Strict-Transport-Security"].startswith("max-age=31536000")


# ── Links and outbound fetches ────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://acme.com/jobs/1", "https://acme.com/jobs/1"),
    ("mailto:hr@acme.com", "mailto:hr@acme.com"),
    ("/job/1/modal", "/job/1/modal"),
    ("www.acme.com/jobs", "https://www.acme.com/jobs"),
    ("javascript:alert(1)", "#"),
    ("  JaVa\tScRiPt:alert(1)", "#"),
    ("data:text/html,<script>alert(1)</script>", "#"),
    ("vbscript:x", "#"),
    ("//evil.example", "#"),
    ("", "#"),
])
def test_safe_url(url, expected):
    assert security.safe_url(url) == expected


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",
                                "0.0.0.0", "::1", "fd00::1", "::ffff:127.0.0.1", "100.64.0.1"])
def test_fetch_guard_refuses_private_addresses(monkeypatch, ip):
    monkeypatch.setattr(security, "_resolve", lambda host, port: [ip])
    with pytest.raises(security.UnsafeURL):
        security.check_public_url("https://sneaky.example/")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://acme.com/x", "http://localhost/", "http://db.internal/",
                                 "http://user:pw@acme.com/", "http://acme.com:6379/", "gopher://acme.com/"])
def test_fetch_guard_refuses_odd_urls(url):
    with pytest.raises(security.UnsafeURL):
        security.check_public_url(url)


def test_fetch_guard_allows_public_sites():
    assert security.check_public_url("https://boards.greenhouse.io/acme") == "https://boards.greenhouse.io/acme"


def test_safe_get_rechecks_redirects(monkeypatch):
    class Redirect:
        is_redirect = True
        headers = {"Location": "http://internal-only.example/admin"}

        def close(self):
            pass

    monkeypatch.setattr(security, "_resolve",
                        lambda host, port: ["10.1.2.3"] if host == "internal-only.example" else ["93.184.216.34"])
    monkeypatch.setattr(security.requests, "get", lambda url, **k: Redirect())
    with pytest.raises(security.UnsafeURL):
        security.safe_get("https://public.example/job")


def test_whatsapp_link_to_metadata_service_is_not_fetched(monkeypatch):
    fetched = []
    monkeypatch.setattr(security, "_resolve", lambda host, port: ["169.254.169.254"])
    monkeypatch.setattr(security.requests, "get", lambda url, **k: fetched.append(url))
    out = dashboard.scrape_job_url("http://metadata.example/latest/meta-data/")
    assert fetched == [] and out["title"] == "Untitled role"


# ── Passwords ─────────────────────────────────────────────────────────────

def test_new_hashes_use_strong_format():
    h = hash_password("a decent pass phrase")
    assert h.startswith("pbkdf2:sha256:") and verify_password("a decent pass phrase", h)
    assert not verify_password("wrong", h)
    assert not verify_password("x" * 10_000, h)


def test_legacy_hash_still_works_and_is_upgraded_on_login(conn):
    salt = binascii.hexlify(os.urandom(16))
    key = hashlib.pbkdf2_hmac("sha256", b"old-but-fine-pw", salt, LEGACY_PBKDF2_ROUNDS)
    legacy = binascii.hexlify(salt + b":" + key).decode()
    assert verify_password("old-but-fine-pw", legacy) and needs_rehash(legacy)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (legacy, A))
    conn.commit()
    dashboard.login_throttle.reset()
    res = dashboard.app.test_client().post("/login", data={"username": f"hard{A}", "password": "old-but-fine-pw"})
    assert res.status_code == 302
    stored = conn.execute("SELECT password_hash FROM users WHERE id = ?", (A,)).fetchone()[0]
    assert stored.startswith("pbkdf2:sha256:") and verify_password("old-but-fine-pw", stored)


@pytest.mark.parametrize("pw", ["password123", "12345678", "aaaaaaaa", "hardnewuser1"])
def test_weak_passwords_refused_at_signup(pw):
    c = get_conn(DB_PATH)
    try:
        with pytest.raises(ValueError):
            signup_user(c, "hardnewuser", pw)
    finally:
        c.close()


def test_lockout_survives_restart_and_other_workers(conn):
    dashboard.login_throttle.reset()
    client = dashboard.app.test_client()
    for _ in range(dashboard.LOGIN_FAILS_PER_USER):
        client.post("/login", data={"username": f"hard{B}", "password": "nope-nope"})
    dashboard.login_throttle.reset()  # as if a fresh worker picked up the next request
    res = client.post("/login", data={"username": f"hard{B}", "password": "first-pass-phrase"})
    assert res.status_code == 429
    conn.execute("DELETE FROM login_events WHERE user_id = ?", (B,))
    conn.commit()


# ── Sessions ──────────────────────────────────────────────────────────────

def test_password_change_signs_out_other_devices(conn):
    dashboard.login_throttle.reset()
    conn.execute("DELETE FROM login_events WHERE user_id = ?", (A,))
    conn.commit()
    phone = dashboard.app.test_client()
    assert phone.post("/login", data={"username": f"hard{A}", "password": "first-pass-phrase"}).status_code == 302
    assert phone.get("/api/stats").status_code == 200
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password("second-pass-phrase"), A))
    conn.commit()
    dashboard._disabled_cache.clear()
    res = phone.get("/api/stats")
    assert res.status_code == 401 and "password changed" in res.get_json()["error"]


def test_admin_sessions_expire(conn):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["login_at"] = 0
    assert c.get("/api/stats").status_code == 401


# ── Cross-account writes ──────────────────────────────────────────────────

def test_cannot_overwrite_someone_elses_note(conn):
    res = client_for(A).post("/api/job/hd_b/note", json={"note": "hijacked"})
    assert res.status_code == 404
    note = conn.execute("SELECT note, user_id FROM application_notes WHERE job_id = 'hd_b'").fetchone()
    assert note[0] == "B private note" and note[1] == B


def test_status_change_on_someone_elses_job_leaves_no_trace(conn):
    client_for(A).post("/api/job/hd_b/status", json={"status": "archived"})
    client_for(A).post("/api/pipeline/move", json={"job_id": "hd_b", "status": "offer"})
    assert conn.execute("SELECT status FROM jobs WHERE job_id = 'hd_b'").fetchone()[0] == "new"
    assert conn.execute("SELECT COUNT(*) FROM application_timeline WHERE job_id = 'hd_b'").fetchone()[0] == 0


def test_pipeline_move_rejects_unknown_status(conn):
    assert client_for(A).post("/api/pipeline/move", json={"job_id": "hd_a", "status": "<b>x"}).status_code == 400


# ── Untrusted HTML ────────────────────────────────────────────────────────

def test_job_popup_neutralises_bad_link_and_quotes(conn):
    conn.execute("INSERT INTO jobs (job_id, title, company, status, url, user_id) VALUES "
                 "(?, 'PM', 'Acme', 'new', 'javascript:alert(1)', ?)", ("hd_q');alert(1);('", A))
    conn.commit()
    html = client_for(A).get("/job/hd_a/modal").get_data(as_text=True)
    assert "javascript:alert" not in html and 'href="#"' in html
    html = client_for(A).get("/job/hd_q');alert(1);('/modal").get_data(as_text=True)
    assert "closeJobModal(&#34;hd_q&#39;);alert(1);(&#39;&#34;)" in html or "\\u0027" in html
    assert "closeJobModal('hd_q');alert(1)" not in html


def test_printed_resume_escapes_html(conn):
    conn.execute("INSERT INTO tailored_resumes (job_id, resume_content, created_at, user_id) VALUES (?, ?, 'now', ?)",
                 ("hd_a", "# Me\n<script>alert(1)</script>\n<img src=x onerror=alert(1)>\n"
                          "**Role** <span style=\"float: right;\">2024</span>", A))
    conn.commit()
    html = client_for(A).get("/resume/hd_a/print").get_data(as_text=True)
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html and '<span style="float: right;">2024</span>' in html


# ── Resume uploads ────────────────────────────────────────────────────────

def _upload(uid, name, data):
    return client_for(uid).post("/api/resume/parse", data={"resume_file": (io.BytesIO(data), name)},
                                content_type="multipart/form-data")


def test_upload_refuses_files_that_are_not_what_they_claim(conn):
    res = _upload(A, "resume.pdf", b"MZ\x90\x00 not a pdf")
    assert res.status_code == 400 and "PDF" in res.get_json()["error"]
    res = _upload(A, "resume.docx", b"%PDF-1.4 not a docx")
    assert res.status_code == 400
    res = _upload(A, "resume.exe", b"MZ")
    assert res.status_code == 400


def test_upload_size_cap(conn, monkeypatch):
    monkeypatch.setattr(security, "RESUME_MAX_BYTES", 1000)
    res = _upload(A, "resume.txt", b"a" * 2000)
    assert res.status_code == 400 and "too big" in res.get_json()["error"]


def test_upload_errors_do_not_leak_internals(conn):
    res = _upload(A, "resume.pdf", b"%PDF-1.4\n garbage that is not a real pdf")
    body = res.get_json()
    assert res.status_code == 400 and "Traceback" not in body["error"] and "pypdf" not in body["error"].lower()
