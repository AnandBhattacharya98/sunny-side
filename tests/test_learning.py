"""Tests for the Learn page: courses on resume gaps, lessons, quizzes and progress. No model is reached:
Gemini and LiveKit are stubbed, so the AI path and the built-in fallback are both covered offline."""
import json

import pytest

import dashboard
import learning
import livekit_llm
from db import get_conn, DB_PATH

UID = 411
OTHER = 412


OUTLINE = {
    "title": "SQL for product managers", "tagline": "Query your own data.", "level": "Beginner",
    "outcomes": ["Write SELECT queries", "Join tables"],
    "lessons": [{"title": f"Lesson {i}", "summary": "s", "minutes": 12} for i in range(5)],
    "resume_line": "Wrote SQL queries to size a feature's reach.",
}


def quiz(n, tag):
    return [{"q": f"{tag} question {i}?", "options": [f"right {i}", f"wrong a{i}", f"wrong b{i}", f"wrong c{i}"],
             "answer": 0, "why": "because"} for i in range(n)]


def lesson_json(i):
    return {"sections": [{"heading": "Basics", "body": "Para one.\n\n- point a\n- point b"}],
            "key_points": ["k1"], "example": "SELECT 1;", "practice": "Try it.", "quiz": quiz(4, f"L{i}")}


@pytest.fixture
def ai(monkeypatch):
    """Gemini answers with a fixed course; records the prompts it was given."""
    prompts = []

    def fake(prompt, response_json=False, api_key=None):
        prompts.append(prompt)
        if "Plan exactly" in prompt:
            return json.dumps(OUTLINE)
        if "final assessment" in prompt:
            return json.dumps({"quiz": quiz(8, "F")})
        idx = int(prompt.split("Write lesson ")[1].split(":")[0]) - 1
        return "```json\n" + json.dumps(lesson_json(idx)) + "\n```"

    import ai_engine
    monkeypatch.setattr(ai_engine, "_call_gemini", fake)
    monkeypatch.setattr(dashboard, "get_fallback_gemini_key", lambda: "test-key")
    monkeypatch.setattr(livekit_llm, "is_configured", lambda: False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    return prompts


@pytest.fixture
def no_ai(monkeypatch):
    import ai_engine

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(ai_engine, "_call_gemini", boom)
    monkeypatch.setattr(dashboard, "get_fallback_gemini_key", lambda: "")
    monkeypatch.setattr(livekit_llm, "is_configured", lambda: False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)


@pytest.fixture
def client():
    dashboard.ve.rate_limiter.reset()
    conn = get_conn(DB_PATH)
    for uid in (UID, OTHER):
        conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash) VALUES (?, ?, 'x')", (uid, f"learner{uid}"))
        conn.execute("DELETE FROM jobs WHERE user_id = ?", (uid,))
        conn.execute("DELETE FROM courses WHERE user_id = ?", (uid,))
    conn.execute("UPDATE users SET resume_text = ? WHERE id = ?", ("Product manager. Figma, roadmaps.", UID))
    rows = [("lj1", "Product Manager", "Acme", ["SQL", "A/B Testing", "Figma"], 8.5, "new"),
            ("lj2", "Senior PM", "Beta", ["sql", "Tableau"], 7.0, "shortlisted"),
            ("lj3", "PM", "Gamma", ["SQL"], 6.0, "archived")]
    for jid, title, company, missing, score, status in rows:
        conn.execute("INSERT INTO jobs (job_id, title, company, url, status, user_id, missing_skills, ai_score) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (jid, title, company, f"https://example.com/{jid}", status, UID,
                                                         json.dumps(missing), score))
    conn.commit()
    conn.close()
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = UID
        s["username"] = f"learner{UID}"
    return c


def other_client():
    c = dashboard.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = OTHER
        s["username"] = f"learner{OTHER}"
    return c


def test_gaps_rank_by_jobs_and_skip_resume_skills(client, ai):
    data = client.get("/api/learn/catalog").get_json()
    skills = [g["skill"] for g in data["gaps"]]
    assert skills[0] == "SQL" and data["gaps"][0]["jobs"] == 2  # archived job not counted, case folded
    assert "Figma" not in skills  # already on the resume
    assert set(skills) == {"SQL", "A/B Testing", "Tableau"}
    assert "Product Manager at Acme" in data["gaps"][0]["examples"]
    assert data["courses"] == []


def test_learn_pages_render(client, ai):
    page = client.get("/learn").get_data(as_text=True)
    assert 'id="lv-root"' in page and 'href="/learn"' in page
    page = client.get("/learn?skill=SQL").get_data(as_text=True)
    assert 'data-skill="SQL"' in page
    assert client.get("/learn/999999").status_code == 302


def test_full_course_flow_with_ai(client, ai):
    r = client.post("/api/learn/courses", json={"skill": "SQL"}).get_json()
    assert r["ok"] and r["created"]
    cid = r["id"]
    assert "Product Manager at Acme" in ai[0]  # the outline prompt names the jobs asking for it
    # Starting the same skill again opens the same course
    again = client.post("/api/learn/courses", json={"skill": " sql "}).get_json()
    assert again["id"] == cid and not again["created"]

    course = client.get(f"/api/learn/courses/{cid}").get_json()["course"]
    assert course["title"] == "SQL for product managers" and course["lessons"] == 5 and course["percent"] == 0
    assert course["resume_line"] == ""  # only shown once finished
    assert not course["final_unlocked"]
    assert client.get(f"/api/learn/courses/{cid}/final").status_code == 403

    for i in range(5):
        lesson = client.get(f"/api/learn/courses/{cid}/lessons/{i}").get_json()
        assert lesson["ok"] and len(lesson["quiz"]) == 4
        assert all("answer" not in q for q in lesson["quiz"])  # answers stay on the server
        right = [q["options"].index(f"right {n}") for n, q in enumerate(lesson["quiz"])]
        if i == 0:
            wrong = [(a + 1) % 4 for a in right]
            res = client.post(f"/api/learn/courses/{cid}/lessons/0/quiz", json={"answers": wrong}).get_json()
            assert not res["passed"] and res["score"] == 0 and res["results"][0]["why"] == "because"
        res = client.post(f"/api/learn/courses/{cid}/lessons/{i}/quiz", json={"answers": right}).get_json()
        assert res["passed"] and res["score"] == 4
    assert res["summary"]["final_unlocked"] and res["summary"]["lessons_done"] == 5

    final = client.get(f"/api/learn/courses/{cid}/final").get_json()
    assert len(final["quiz"]) == 8
    right = [q["options"].index(f"right {n}") for n, q in enumerate(final["quiz"])]
    res = client.post(f"/api/learn/courses/{cid}/final", json={"answers": right}).get_json()
    assert res["passed"] and res["summary"]["completed"] and res["summary"]["percent"] == 100
    assert res["resume_line"].startswith("Wrote SQL") and res["certificate"]

    catalog = client.get("/api/learn/catalog").get_json()
    assert catalog["courses"][0]["completed"]
    assert next(g for g in catalog["gaps"] if g["skill"] == "SQL")["course_id"] == cid

    # Lessons are written once, then served from the database
    n = len(ai)
    client.get(f"/api/learn/courses/{cid}/lessons/2")
    assert len(ai) == n


def test_options_are_shuffled_but_graded_right():
    q = learning._clean_quiz(quiz(8, "S"), 8, "seed")
    assert {x["answer"] for x in q} != {0}
    assert all(x["options"][x["answer"]].startswith("right") for x in q)
    bad = learning._clean_quiz([{"q": "x", "options": ["a", "a"], "answer": 0}, {"q": "y", "options": ["a", "b"], "answer": 5}], 4, "s")
    assert bad == []


def test_builtin_course_when_no_model(client, no_ai):
    cid = client.post("/api/learn/courses", json={"skill": "Tableau"}).get_json()["id"]
    course = client.get(f"/api/learn/courses/{cid}").get_json()["course"]
    assert course["source"] == "local" and course["lessons"] == 5
    lesson = client.get(f"/api/learn/courses/{cid}/lessons/0").get_json()
    assert "Tableau" in lesson["sections"][0]["body"] and len(lesson["quiz"]) == 3
    for i in range(5):
        client.get(f"/api/learn/courses/{cid}/lessons/{i}")
        conn = get_conn(DB_PATH)
        c = learning.get_course(conn, UID, cid)
        conn.close()
        answers = [q["answer"] for q in c["lessons"][str(i)]["quiz"]]
        assert client.post(f"/api/learn/courses/{cid}/lessons/{i}/quiz", json={"answers": answers}).get_json()["passed"]
    final = client.get(f"/api/learn/courses/{cid}/final").get_json()
    assert final["ok"] and len(final["quiz"]) == 8  # drawn from the lessons' questions


def test_courses_are_private(client, ai):
    cid = client.post("/api/learn/courses", json={"skill": "SQL"}).get_json()["id"]
    other = other_client()
    assert other.get(f"/api/learn/courses/{cid}").status_code == 404
    assert other.get(f"/api/learn/courses/{cid}/lessons/0").status_code == 404
    assert other.post(f"/api/learn/courses/{cid}/lessons/0/quiz", json={"answers": [0]}).status_code == 404
    other.post(f"/api/learn/courses/{cid}/delete")
    assert client.get(f"/api/learn/courses/{cid}").status_code == 200
    client.post(f"/api/learn/courses/{cid}/delete")
    assert client.get(f"/api/learn/courses/{cid}").status_code == 404


def test_quiz_before_lesson_and_bad_input(client, ai):
    cid = client.post("/api/learn/courses", json={"skill": "SQL"}).get_json()["id"]
    assert client.post(f"/api/learn/courses/{cid}/lessons/1/quiz", json={"answers": [0]}).status_code == 400
    assert client.get(f"/api/learn/courses/{cid}/lessons/9").status_code == 404
    assert client.post("/api/learn/courses", json={"skill": " "}).status_code == 400
    client.get(f"/api/learn/courses/{cid}/lessons/1")
    res = client.post(f"/api/learn/courses/{cid}/lessons/1/quiz", json={"answers": ["x", None]}).get_json()
    assert res["ok"] and res["score"] == 0


def test_account_delete_removes_courses(client, ai):
    from db import delete_user_data
    client.post("/api/learn/courses", json={"skill": "SQL"})
    conn = get_conn(DB_PATH)
    delete_user_data(conn, UID)
    assert conn.execute("SELECT COUNT(*) FROM courses WHERE user_id = ?", (UID,)).fetchone()[0] == 0
    conn.close()
