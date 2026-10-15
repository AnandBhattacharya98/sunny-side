"""Each board searches for the roles on its owner's resume, never a fixed "Product Manager"."""
import pytest

import dashboard
import db
import roles
from db import get_conn, DB_PATH
from resume_parser import parse_resume_local

SWE = """Priya Sharma
Bengaluru | priya@example.com | +91 98765 43210
SUMMARY
Backend developer with 4 years of experience building payment APIs.
EXPERIENCE
Senior Software Engineer, Razorpay | Jan 2023 - Present
- Worked closely with the product manager to scope the refunds API
Software Engineer - Infosys | 2020 - 2022
SKILLS
Python, Go, Java, SQL, Docker, Kubernetes, AWS, Git, Lead Generation
"""

NURSE = """Anjali Menon
Kochi, Kerala
Staff Nurse at Apollo Hospitals (2019 – Present)
Provided patient care in a 30-bed ICU. Trained 6 junior nurses.
Education: B.Sc Nursing
"""

ANALYST = """Rahul Verma — Data Analyst
Experience
Data Analyst @ Swiggy (Mar 2022 – Present)
 • Built Tableau and Power BI dashboards for city ops; SQL, Python, Excel
Business Analyst Intern, Deloitte, 2021
"""

MARKETING = """Sneha Kapoor
WORK HISTORY
Marketing Manager | Nykaa | 2022 - Present
Content Marketing Executive | Zomato | 2020 - 2022
"""

FRESHER = """Arjun Rao
B.Tech 2025, JNTU
Projects: Built a React + Node.js expense tracker and a Django blog.
Skills: JavaScript, React, Node.js, Python, SQL
"""


@pytest.mark.parametrize("resume,expected", [
    (SWE, ["Software Engineer"]),
    (NURSE, ["Staff Nurse"]),
    (ANALYST, ["Data Analyst"]),
    (MARKETING, ["Marketing Manager", "Content Marketing Executive"]),
])
def test_roles_come_from_the_titles_on_the_resume(resume, expected):
    assert roles.roles_for("", parse_resume_local(resume), resume) == expected


def test_a_resume_without_titles_uses_its_skills():
    found = roles.roles_for("", parse_resume_local(FRESHER), FRESHER)
    assert found[0] == "Software Engineer"
    assert "Product Manager" not in found


def test_typed_title_comes_first():
    assert roles.roles_for("UX Designer", {}, SWE)[:2] == ["UX Designer", "Software Engineer"]


def test_no_resume_and_no_title_means_no_search():
    assert roles.roles_for("", {}, "") == []


@pytest.mark.parametrize("raw,phrase", [
    ("Sr. Backend Developer at Zeta (2021-2024)", "Backend Developer"),
    ("Associate Product Manager", "Product Manager"),
    ("SDE", "Software Engineer"),
    ("Head of Sales", "Head Of Sales"),
])
def test_search_phrase(raw, phrase):
    assert roles.search_phrase(raw) == phrase


@pytest.mark.parametrize("line", [
    "Worked closely with the product manager to scope the API",
    "Python, SQL, Lead Generation",
    "EXPERIENCE",
    "priya@example.com",
])
def test_lines_that_are_not_titles(line):
    assert roles.clean_title(line) == ""


def test_local_scoring_targets_the_resume_roles(monkeypatch):
    import ai_engine
    monkeypatch.setattr(ai_engine, "ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    c = get_conn(DB_PATH)
    c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (711, 'roles711', 'x')")
    c.execute("UPDATE users SET designation = '', resume_text = ?, resume_profile_json = NULL WHERE id = 711", (ANALYST,))
    c.execute("DELETE FROM jobs WHERE user_id = 711")
    c.commit()
    c.close()
    analyst = ai_engine.score_job("Data Analyst", "Acme", "SQL and Tableau dashboards.", resume_text=ANALYST, user_id=711)
    pm = ai_engine.score_job("Product Manager", "Acme", "Own the roadmap.", resume_text=ANALYST, user_id=711)
    assert analyst["score"] > pm["score"] + 2


def _client(uid):
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_signup_without_a_title_does_not_become_product_manager(monkeypatch):
    monkeypatch.setattr(dashboard, "run_in_background", lambda *a: True)
    res = dashboard.app.test_client().post("/signup", data={
        "username": "nurse_signup", "password": "a-long-password-1", "resume_text": NURSE})
    assert res.status_code in (302, 303)
    c = get_conn(DB_PATH)
    row = c.execute("SELECT id, designation FROM users WHERE username = 'nurse_signup'").fetchone()
    assert (row[1] or "") == ""
    assert roles.roles_for_user(c, row[0]) == ["Staff Nurse"]
    c.close()


def test_refresh_asks_for_a_resume_when_there_is_nothing_to_search(monkeypatch):
    started = []
    monkeypatch.setattr(dashboard, "run_in_background", lambda *a: started.append(a) or True)
    c = get_conn(DB_PATH)
    c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (712, 'roles712', 'x')")
    c.execute("UPDATE users SET designation = '', resume_text = '', resume_profile_json = NULL WHERE id = 712")
    c.commit()
    c.close()
    data = _client(712).post("/api/refresh").get_json()
    assert data["ok"] is False and "resume" in data["error"]
    assert started == []


def test_old_default_title_is_cleared_for_non_product_resumes():
    c = get_conn(DB_PATH)
    for uid, resume in ((713, NURSE), (714, "Product Manager at Acme, owned the roadmap"), (715, "")):
        c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (?, ?, 'x')", (uid, f"roles{uid}"))
        c.execute("UPDATE users SET designation = 'Product Manager', resume_text = ? WHERE id = ?", (resume, uid))
        c.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
    c.execute("INSERT INTO jobs (job_id, title, company, status, url, user_id, feedback) VALUES "
              "('r713_new', 'Product Manager', 'A', 'new', 'u', 713, 0), "
              "('r713_liked', 'Product Manager', 'B', 'new', 'u', 713, 1), "
              "('r713_applied', 'Product Manager', 'C', 'applied', 'u', 713, 0), "
              "('r714_new', 'Product Manager', 'D', 'new', 'u', 714, 0)")
    c.commit()
    db._drop_default_pm_title(c)
    titles = dict(c.execute("SELECT id, designation FROM users WHERE id IN (713, 714, 715)").fetchall())
    assert titles == {713: "", 714: "Product Manager", 715: "Product Manager"}
    left = {r[0] for r in c.execute("SELECT job_id FROM jobs WHERE user_id IN (713, 714)").fetchall()}
    assert left == {"r713_liked", "r713_applied", "r714_new"}
    c.close()


def test_changing_the_resume_clears_untouched_suggestions(monkeypatch):
    monkeypatch.setattr(dashboard, "rescore_inbox_in_background", lambda uid: None)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    c = get_conn(DB_PATH)
    c.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (716, 'roles716', 'x')")
    c.execute("UPDATE users SET designation = '', resume_text = ?, resume_profile_json = NULL, gemini_api_key = NULL "
              "WHERE id = 716", (SWE,))
    c.execute("DELETE FROM jobs WHERE user_id = 716")
    c.execute("INSERT INTO jobs (job_id, title, company, status, url, user_id) VALUES "
              "('r716_new', 'Software Engineer', 'A', 'new', 'u', 716)")
    c.commit()
    c.close()
    res = _client(716).post("/api/profile", json={"resume_text": NURSE, "designation": ""})
    assert res.get_json()["ok"]
    c = get_conn(DB_PATH)
    assert c.execute("SELECT COUNT(*) FROM jobs WHERE user_id = 716").fetchone()[0] == 0
    c.close()
