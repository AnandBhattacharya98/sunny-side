"""Export your data, delete your account, health check."""
import csv
import io

import pytest

import dashboard
from db import get_conn, DB_PATH, add_timeline


@pytest.fixture
def conn():
    c = get_conn(DB_PATH)
    for uid in (601, 602):
        c.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM user_company_boards WHERE user_id = ?", (uid,))
        c.execute("INSERT OR IGNORE INTO users (id, username, password_hash, name, resume_text, gemini_api_key) "
                  "VALUES (?, ?, 'x', 'Me', 'my resume', 'secret-key')", (uid, f"acct{uid}"))
    c.execute("INSERT INTO jobs (job_id, title, company, status, url, user_id) VALUES "
              "('ac_1', '=HYPERLINK(\"x\")', 'Acme', 'applied', 'https://a', 601), "
              "('ac_2', 'PM', 'Other', 'new', 'https://b', 602)")
    c.execute("INSERT OR REPLACE INTO application_notes (job_id, note, user_id) VALUES ('ac_1', 'Called HR', 601)")
    c.execute("INSERT INTO user_company_boards (user_id, company, ats, board, created_at) VALUES (601, 'Z', 'lever', 'z', 'now')")
    c.commit()
    add_timeline(c, "ac_1", "Status → applied")
    yield c
    c.close()


def client_for(uid):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_csv_export_is_mine_and_formula_safe(conn):
    res = client_for(601).get("/api/account/export?format=csv")
    assert res.mimetype == "text/csv" and "attachment" in res.headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(res.get_data(as_text=True))))
    assert len(rows) == 1 and rows[0]["company"] == "Acme" and rows[0]["note"] == "Called HR"
    assert rows[0]["title"].startswith("'=")


def test_json_export_has_no_secrets(conn):
    data = client_for(601).get("/api/account/export?format=json").get_json()
    assert data["profile"]["resume_text"] == "my resume"
    assert "gemini_api_key" not in data["profile"] and "password_hash" not in data["profile"]
    assert [j["job_id"] for j in data["jobs"]] == ["ac_1"]
    assert data["timeline"][0]["event"] == "Status → applied"
    assert data["followed_companies"] == [{"company": "Z", "ats": "lever", "board": "z"}]


def test_delete_needs_confirmation_and_removes_only_mine(conn):
    c = client_for(601)
    assert c.post("/api/account/delete", json={"confirm": "yes"}).status_code == 400
    assert c.post("/api/account/delete", json={"confirm": "delete"}).get_json()["ok"]
    for sql in ("SELECT COUNT(*) FROM users WHERE id = 601", "SELECT COUNT(*) FROM jobs WHERE user_id = 601",
                "SELECT COUNT(*) FROM application_notes WHERE job_id = 'ac_1'",
                "SELECT COUNT(*) FROM application_timeline WHERE job_id = 'ac_1'",
                "SELECT COUNT(*) FROM user_company_boards WHERE user_id = 601"):
        assert conn.execute(sql).fetchone()[0] == 0, sql
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE user_id = 602").fetchone()[0] == 1
    # Signed out afterwards.
    assert c.get("/api/account/export").status_code == 401


def test_admin_cannot_delete_itself():
    assert client_for(1).post("/api/account/delete", json={"confirm": "DELETE"}).status_code == 400


def test_healthz_needs_no_login():
    res = dashboard.app.test_client().get("/healthz")
    assert res.status_code == 200 and res.get_json() == {"ok": True}


def test_api_errors_come_back_as_json(monkeypatch):
    monkeypatch.setattr(dashboard, "_export_rows", lambda *a: 1 / 0)
    dashboard.app.config["PROPAGATE_EXCEPTIONS"] = False
    try:
        res = client_for(602).get("/api/account/export")
    finally:
        dashboard.app.config["PROPAGATE_EXCEPTIONS"] = None
    assert res.status_code == 500 and res.get_json()["ok"] is False
