"""Followed companies, closed listings and duplicate jobs."""
import pytest

import dashboard
import scraper
import voice_engine as ve
from db import get_conn, init_db, DB_PATH


class Resp:
    def __init__(self, text="", status=200, data=None):
        self.text, self.status_code, self._data = text, status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.fixture
def conn(tmp_path):
    c = init_db(str(tmp_path / "sources.db"))
    c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (7, 'u7', 'x')")
    c.commit()
    yield c
    c.close()


def add_job(conn, job_id, title, company, status="scored", source="linkedin", url="", scraped_at="2026-10-01"):
    conn.execute("INSERT INTO jobs (job_id, title, company, status, source, url, scraped_at, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, 7)",
                 (job_id, title, company, status, source, url, scraped_at))
    conn.commit()


# ── Careers links ────────────────────────────────────────────────────────

def test_parse_board_url():
    assert scraper.parse_board_url("https://jobs.lever.co/acme/123-abc") == {"ats": "lever", "board": "acme", "region": ""}
    assert scraper.parse_board_url("job-boards.greenhouse.io/zeta") == {"ats": "greenhouse", "board": "zeta", "region": ""}
    assert scraper.parse_board_url("https://job-boards.eu.greenhouse.io/groww")["region"] == "eu"
    assert scraper.parse_board_url("greenhouse:acme")["board"] == "acme"
    for bad in ("https://acme.com/careers", "http://169.254.169.254/", "jobs.lever.co/../etc", ""):
        assert scraper.parse_board_url(bad) is None


# ── Duplicates ───────────────────────────────────────────────────────────

def test_same_opening_matches_across_spellings(conn):
    add_job(conn, "a_u7", "Sr. Product Manager (Remote)", "Acme Technologies Pvt Ltd")
    assert scraper.find_duplicate(conn, 7, "Senior Product Manager", "Acme") == "a_u7"
    assert scraper.find_duplicate(conn, 7, "Product Manager", "Acme") is None
    assert scraper.find_duplicate(conn, 7, "Senior Product Manager", "Beta") is None


def test_insert_skips_duplicates(conn):
    add_job(conn, "a_u7", "Product Manager", "Acme")
    job = {"job_id": "direct_acme_9", "title": "Product Manager", "company": "Acme Inc.", "url": "u", "source": "direct"}
    assert scraper._insert_job(conn, job, user_id=7) is False


def test_fold_keeps_the_copy_you_worked_on(conn):
    add_job(conn, "a_u7", "Product Manager", "Acme", status="scored", scraped_at="2026-10-01")
    add_job(conn, "b_u7", "Product Manager", "Acme Ltd", status="applied", scraped_at="2026-10-03")
    add_job(conn, "c_u7", "Product Manager", "ACME", status="new", scraped_at="2026-10-05")
    add_job(conn, "d_u7", "Designer", "Acme")
    assert scraper.fold_duplicates(conn, 7) == 2
    status = dict(conn.execute("SELECT job_id, status FROM jobs WHERE user_id = 7").fetchall())
    assert status == {"a_u7": "archived", "b_u7": "applied", "c_u7": "archived", "d_u7": "scored"}


def test_fold_never_archives_progressed_copies(conn):
    add_job(conn, "a_u7", "Product Manager", "Acme", status="applied")
    add_job(conn, "b_u7", "Product Manager", "Acme", status="interviewing")
    assert scraper.fold_duplicates(conn, 7) == 0


# ── Closed listings ──────────────────────────────────────────────────────

def test_company_feed_marks_missing_roles_closed_and_reopens(conn):
    cfg = {"company": "Acme", "ats": "lever", "board": "acme"}
    add_job(conn, scraper.board_job_id("Acme", "1") + "_u7", "PM", "Acme", source="direct")
    add_job(conn, scraper.board_job_id("Acme", "2") + "_u7", "APM", "Acme", source="direct")
    add_job(conn, scraper.board_job_id("Other", "1") + "_u7", "PM", "Other", source="direct")
    scraper.mark_closed_board_postings(conn, 7, cfg, [{"id": "1"}])
    closed = dict(conn.execute("SELECT job_id, closed_at FROM jobs WHERE user_id = 7").fetchall())
    assert closed["direct_acme_1_u7"] is None and closed["direct_acme_2_u7"] and closed["direct_other_1_u7"] is None
    scraper.mark_closed_board_postings(conn, 7, cfg, [{"id": "1"}, {"id": "2"}])
    assert conn.execute("SELECT closed_at FROM jobs WHERE job_id = 'direct_acme_2_u7'").fetchone()[0] is None


def test_linkedin_closures(conn, monkeypatch):
    add_job(conn, "li_1_u7", "PM", "A", url="https://in.linkedin.com/jobs/view/1")
    add_job(conn, "li_2_u7", "PM", "B", url="https://in.linkedin.com/jobs/view/2")
    add_job(conn, "li_3_u7", "PM", "C", url="https://in.linkedin.com/jobs/view/3")
    pages = {"1": Resp("<h1>PM</h1> No longer accepting applications"), "2": Resp("<h1>PM</h1> Apply"),
             "3": Resp("", status=429)}
    monkeypatch.setattr(scraper.requests, "get", lambda url, **k: pages[url[-1]])
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    assert scraper.check_linkedin_closures(conn, 7) == 1
    rows = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT job_id, closed_at, closed_checked_at FROM jobs WHERE user_id = 7")}
    assert rows["li_1_u7"][0] and rows["li_2_u7"][0] is None and rows["li_2_u7"][1]
    assert rows["li_3_u7"] == (None, None)  # blocked: try again next refresh
    # Checked or closed recently: only the blocked one is fetched again.
    fetched = []
    monkeypatch.setattr(scraper.requests, "get", lambda url, **k: fetched.append(url) or Resp("", status=429))
    scraper.check_linkedin_closures(conn, 7)
    assert fetched == ["https://in.linkedin.com/jobs/view/3"]


# ── Followed companies API ───────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    ve.rate_limiter.reset()
    c = get_conn(DB_PATH)
    for uid in (401, 402):
        c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (?, ?, 'x')", (uid, f"co{uid}"))
    c.execute("DELETE FROM user_company_boards WHERE user_id IN (401, 402)")
    c.commit()
    c.close()
    monkeypatch.setattr(scraper, "_fetch_board_postings",
                        lambda cfg: [{"id": "9", "title": "Product Manager", "url": f"https://jobs.lever.co/{cfg['board']}/9",
                                      "location": "Bengaluru", "description": "PM role", "posted_at": ""}])
    with dashboard.app.test_client() as cl:
        with cl.session_transaction() as s:
            s["user_id"] = 401
        yield cl


def test_follow_list_and_unfollow(client):
    res = client.post("/api/companies", json={"url": "https://jobs.lever.co/zepto", "company": "Zepto"}).get_json()
    assert res == {"ok": True, "company": "Zepto", "open_roles": 1}
    assert client.post("/api/companies", json={"url": "jobs.lever.co/zepto"}).status_code == 400  # already
    assert client.post("/api/companies", json={"url": "https://zepto.com/careers"}).status_code == 400
    assert client.post("/api/companies", json={"url": "jobs.lever.co/cred"}).status_code == 400  # built in
    companies = client.get("/api/companies").get_json()["companies"]
    assert [c["company"] for c in companies] == ["Zepto"]
    with dashboard.app.test_client() as other:
        with other.session_transaction() as s:
            s["user_id"] = 402
        assert other.get("/api/companies").get_json()["companies"] == []
        other.post(f"/api/companies/{companies[0]['id']}/delete")
    assert len(client.get("/api/companies").get_json()["companies"]) == 1  # not theirs to delete
    client.post(f"/api/companies/{companies[0]['id']}/delete")
    assert client.get("/api/companies").get_json()["companies"] == []


def test_followed_company_is_searched_and_saved(client, monkeypatch):
    client.post("/api/companies", json={"url": "https://jobs.lever.co/zepto", "company": "Zepto"})
    scraper._BOARD_CACHE.clear()
    monkeypatch.setattr(scraper, "search_linkedin", lambda *a, **k: [])
    data = client.get("/api/jobs/search?q=Product+Manager&location=Bengaluru").get_json()
    zepto = [r for r in data["results"] if r["company"] == "Zepto"]
    assert zepto and zepto[0]["ref"] == "zepto:9"
    monkeypatch.setattr(dashboard, "run_in_background", lambda *a: True)
    res = client.post("/api/jobs/search/add", json={"source": "direct", "ref": "zepto:9"}).get_json()
    assert res["ok"] and not res.get("already")
