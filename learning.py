"""Learn: short courses on the skills a user's resume is missing for the jobs they want.

A course is made for one skill. Its outline (title, outcomes, lesson titles) is written when the course is
started, and each lesson's text and quiz are written the first time that lesson is opened, so starting a
course is quick. Gemini writes them when a key is available, then LiveKit's model, and when neither works a
built-in study plan stands in, so the page always works.

Quiz answers never leave the server: lessons are sent with their options only, and answers are graded here.
"""
import hashlib
import json
import logging
import random
import re
from datetime import datetime

import db

log = logging.getLogger(__name__)

LESSONS = 5
LESSON_QUESTIONS = 4
FINAL_QUESTIONS = 8
PASS_RATIO = 0.7
MAX_SKILL_CHARS = 60
MAX_GAPS = 12


# ── Storage ───────────────────────────────────────────────────────────────

def migrate(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            skill        TEXT,
            skill_key    TEXT,
            outline      TEXT,
            lessons      TEXT,
            final_quiz   TEXT,
            progress     TEXT,
            source       TEXT,
            created_at   TEXT,
            updated_at   TEXT,
            completed_at TEXT
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_courses_user ON courses (user_id)")
    conn.commit()


def delete_user_courses(conn, user_id: int) -> None:
    conn.execute("DELETE FROM courses WHERE user_id = ?", (user_id,))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def skill_key(skill: str) -> str:
    return " ".join(str(skill or "").lower().split())


def clean_skill(skill) -> str:
    s = re.sub(r"\s+", " ", str(skill or "")).strip(" .,;:-•")
    return s[:MAX_SKILL_CHARS]


def _loads(text, default):
    try:
        v = json.loads(text) if text else default
        return v if isinstance(v, type(default)) else default
    except (TypeError, ValueError):
        return default


def _row(conn, user_id: int, course_id: int):
    return conn.execute("SELECT id, user_id, skill, skill_key, outline, lessons, final_quiz, progress, source, "
                        "created_at, updated_at, completed_at FROM courses WHERE id = ? AND user_id = ?",
                        (course_id, user_id)).fetchone()


def _course(row) -> dict:
    return {
        "id": row[0], "user_id": row[1], "skill": row[2], "skill_key": row[3],
        "outline": _loads(row[4], {}), "lessons": _loads(row[5], {}), "final_quiz": _loads(row[6], []),
        "progress": _loads(row[7], {}), "source": row[8] or "local",
        "created_at": row[9], "updated_at": row[10], "completed_at": row[11],
    }


def get_course(conn, user_id: int, course_id: int) -> dict | None:
    row = _row(conn, user_id, course_id)
    return _course(row) if row else None


def find_course(conn, user_id: int, skill: str) -> dict | None:
    row = conn.execute("SELECT id FROM courses WHERE user_id = ? AND skill_key = ? ORDER BY id LIMIT 1",
                       (user_id, skill_key(skill))).fetchone()
    return get_course(conn, user_id, row[0]) if row else None


def _save(conn, course: dict, *fields) -> None:
    cols = {"outline": json.dumps(course["outline"]), "lessons": json.dumps(course["lessons"]),
            "final_quiz": json.dumps(course["final_quiz"]), "progress": json.dumps(course["progress"]),
            "source": course["source"], "completed_at": course.get("completed_at")}
    sets = [f"{f} = ?" for f in fields] + ["updated_at = ?"]
    conn.execute(f"UPDATE courses SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                 [cols[f] for f in fields] + [now_iso(), course["id"], course["user_id"]])
    conn.commit()


def delete_course(conn, user_id: int, course_id: int) -> bool:
    cur = conn.execute("DELETE FROM courses WHERE id = ? AND user_id = ?", (course_id, user_id))
    conn.commit()
    return bool(getattr(cur, "rowcount", 1))


# ── What the user is missing ─────────────────────────────────────────────

def skill_gaps(conn, user_id: int, resume_text: str = "") -> list[dict]:
    """Skills the user's jobs ask for that their resume lacks, most asked for first.
    A skill that has since made it onto the resume is left out."""
    rows = conn.execute("SELECT title, company, missing_skills, COALESCE(ai_score, 0) FROM jobs "
                        "WHERE user_id = ? AND status NOT IN ('archived', 'rejected') AND missing_skills IS NOT NULL",
                        (user_id,)).fetchall()
    resume = skill_key(resume_text)
    found = {}
    for title, company, missing, score in rows:
        skills = _loads(missing, [])
        seen_here = set()
        for s in skills:
            s = clean_skill(s) if isinstance(s, str) else ""
            k = skill_key(s)
            if not k or k in seen_here or len(k) < 2:
                continue
            seen_here.add(k)
            if resume and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", resume):
                continue
            g = found.setdefault(k, {"skill": s, "jobs": 0, "best": 0.0, "examples": []})
            g["jobs"] += 1
            g["best"] = max(g["best"], float(score or 0))
            label = " at ".join(x for x in ((title or "").strip()[:60], (company or "").strip()[:40]) if x)
            if label and label not in g["examples"] and len(g["examples"]) < 3:
                g["examples"].append(label)
    gaps = sorted(found.values(), key=lambda g: (-g["jobs"], -g["best"], g["skill"].lower()))
    return [{"skill": g["skill"], "jobs": g["jobs"], "examples": g["examples"]} for g in gaps[:MAX_GAPS]]


def jobs_asking_for(conn, user_id: int, skill: str, limit: int = 3) -> list[str]:
    k = skill_key(skill)
    out = []
    for title, company, missing in conn.execute(
            "SELECT title, company, missing_skills FROM jobs WHERE user_id = ? AND status != 'archived' "
            "AND missing_skills IS NOT NULL ORDER BY COALESCE(ai_score, 0) DESC", (user_id,)).fetchall():
        if any(isinstance(s, str) and skill_key(s) == k for s in _loads(missing, [])):
            label = " at ".join(x for x in ((title or "").strip()[:60], (company or "").strip()[:40]) if x)
            if label and label not in out:
                out.append(label)
            if len(out) >= limit:
                break
    return out


# ── Writing course content ───────────────────────────────────────────────

def _strs(v, n, size=200) -> list[str]:
    return [re.sub(r"\s+", " ", str(x)).strip()[:size] for x in (v if isinstance(v, list) else []) if str(x).strip()][:n]


def _text(v, size) -> str:
    return str(v or "").strip()[:size]


def _clean_quiz(items, n, seed: str) -> list[dict]:
    """Keeps well-formed multiple choice questions and shuffles each one's options, so the right answer
    isn't always the first one the model wrote."""
    out = []
    for i, q in enumerate(items if isinstance(items, list) else []):
        if not isinstance(q, dict):
            continue
        question = _text(q.get("q") or q.get("question"), 300)
        options = _strs(q.get("options") or q.get("choices"), 5, 160)
        try:
            answer = int(q.get("answer", q.get("correct", -1)))
        except (TypeError, ValueError):
            answer = -1
        if not question or len(options) < 2 or len(set(o.lower() for o in options)) != len(options):
            continue
        if not 0 <= answer < len(options):
            continue
        right = options[answer]
        rng = random.Random(f"{seed}|{i}|{question}")
        rng.shuffle(options)
        out.append({"q": question, "options": options, "answer": options.index(right),
                    "why": _text(q.get("why") or q.get("explanation"), 400)})
        if len(out) >= n:
            break
    return out


def _clean_outline(data, skill: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    lessons = []
    for l in data.get("lessons") if isinstance(data.get("lessons"), list) else []:
        if not isinstance(l, dict) or not _text(l.get("title"), 90):
            continue
        try:
            minutes = max(5, min(60, int(l.get("minutes") or 15)))
        except (TypeError, ValueError):
            minutes = 15
        lessons.append({"title": _text(l.get("title"), 90), "summary": _text(l.get("summary"), 240), "minutes": minutes})
    if len(lessons) < 3:
        return None
    level = _text(data.get("level"), 20) or "Beginner"
    return {
        "title": _text(data.get("title"), 90) or f"{skill} essentials",
        "tagline": _text(data.get("tagline"), 200),
        "level": level if level in ("Beginner", "Intermediate", "Advanced") else "Beginner",
        "outcomes": _strs(data.get("outcomes"), 4, 160),
        "lessons": lessons[:LESSONS + 1],
        "resume_line": _text(data.get("resume_line"), 240),
    }


def _clean_lesson(data, seed: str) -> dict | None:
    if not isinstance(data, dict):
        return None
    sections = []
    for s in data.get("sections") if isinstance(data.get("sections"), list) else []:
        if isinstance(s, dict) and _text(s.get("body"), 2400):
            sections.append({"heading": _text(s.get("heading"), 90), "body": _text(s.get("body"), 2400)})
    quiz = _clean_quiz(data.get("quiz"), LESSON_QUESTIONS, seed)
    if not sections or len(quiz) < 2:
        return None
    return {
        "sections": sections[:5],
        "key_points": _strs(data.get("key_points"), 5, 220),
        "example": _text(data.get("example"), 1600),
        "practice": _text(data.get("practice"), 600),
        "quiz": quiz,
    }


def _ask_model(prompt: str, api_key: str = "", max_tokens: int = 2500):
    """Parsed JSON from Gemini, else LiveKit's model, plus which one answered. (None, "") if neither could."""
    from ai_engine import _call_gemini
    import livekit_llm
    if api_key:
        try:
            return _json(_call_gemini(prompt, response_json=True, api_key=api_key)), "ai"
        except Exception as e:  # network, quota or a bad reply: try the next writer
            log.warning("Course writing via Gemini failed: %s", e)
    if livekit_llm.is_configured():
        try:
            return _json(livekit_llm.complete(prompt, temperature=0.5, max_tokens=max_tokens)), "ai"
        except Exception as e:
            log.warning("Course writing via LiveKit failed: %s", e)
    return None, ""


def _json(text: str):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    return json.loads(text)


_QUIZ_SHAPE = ('{"q": "<question>", "options": ["<A>", "<B>", "<C>", "<D>"], "answer": <index of the right option, 0-3>, '
               '"why": "<one sentence on why that answer is right>"}')


def outline_prompt(skill: str, targets: list[str], resume: str) -> str:
    return f"""You are designing a short, practical online course, in the style of a Coursera course, for a job seeker.
They need to learn "{skill}" because jobs they are applying to ask for it and their resume does not show it.
Jobs asking for it: {', '.join(targets) or 'not specified'}.

THEIR RESUME (to pitch the level right; do not repeat it back):
{(resume or '(No resume provided.)')[:2500]}

Plan exactly {LESSONS} lessons that take them from the basics to being able to use {skill} at work and talk about it in
an interview. Each lesson should be 10 to 25 minutes. Be concrete and specific to {skill}, not generic study advice.

Reply ONLY with JSON:
{{"title": "<course title>", "tagline": "<one sentence on what they will be able to do>",
"level": "Beginner|Intermediate|Advanced", "outcomes": ["<skill they will gain>", "... 3 or 4"],
"lessons": [{{"title": "<lesson title>", "summary": "<one sentence>", "minutes": <number>}}],
"resume_line": "<one resume bullet they could honestly write after doing the course's practice project>"}}"""


def lesson_prompt(skill: str, outline: dict, index: int) -> str:
    lessons = outline.get("lessons") or []
    this = lessons[index]
    plan = "\n".join(f"{i + 1}. {l['title']}" for i, l in enumerate(lessons))
    return f"""You are writing lesson {index + 1} of a short online course called "{outline.get('title') or skill}" that teaches "{skill}".
Course plan:
{plan}

Write lesson {index + 1}: "{this['title']}" ({this.get('summary') or ''}).
Teach it clearly for a working professional: plain language, real terms of the trade, a worked example.
Use plain text only (no markdown symbols except "- " at the start of a bullet line). Keep it to about {this.get('minutes', 15) * 60} words in total.
Then write {LESSON_QUESTIONS} multiple choice questions that check understanding of THIS lesson (not trivia), each with 4 options.

Reply ONLY with JSON:
{{"sections": [{{"heading": "<heading>", "body": "<2-4 short paragraphs>"}}],
"key_points": ["<takeaway>", "... 3 to 5"],
"example": "<a worked example, step by step>",
"practice": "<a 10-30 minute hands-on exercise they can do on their own>",
"quiz": [{_QUIZ_SHAPE}]}}"""


def final_prompt(skill: str, outline: dict) -> str:
    plan = "\n".join(f"- {l['title']}: {l.get('summary', '')}" for l in outline.get("lessons") or [])
    return f"""Write the final assessment for a short course on "{skill}" that covered:
{plan}

Write {FINAL_QUESTIONS} multiple choice questions, each with 4 options, that test whether someone could use {skill} at
work: mostly short scenarios, a few definitions. Spread them across the lessons.

Reply ONLY with JSON: {{"quiz": [{_QUIZ_SHAPE}]}}"""


# The built-in course: a study plan that works for any skill, used when no model can be reached.

def local_outline(skill: str, targets: list[str]) -> dict:
    for_what = f" for roles like {targets[0]}" if targets else ""
    return {
        "title": f"{skill}: a starter course",
        "tagline": f"Get from zero to something you can show{for_what}.",
        "level": "Beginner",
        "outcomes": [f"Know what employers mean by {skill}", f"Build one small {skill} project",
                     f"Put {skill} on your resume honestly", f"Answer interview questions about {skill}"],
        "lessons": [
            {"title": f"What {skill} means on a job posting", "summary": "What employers are really asking for.", "minutes": 10},
            {"title": f"Learn the core of {skill}", "summary": "A focused plan for the basics.", "minutes": 20},
            {"title": f"Practice {skill} on a small project", "summary": "Learn by building one real thing.", "minutes": 25},
            {"title": f"Show {skill} on your resume", "summary": "Turn practice into proof.", "minutes": 10},
            {"title": f"Talk about {skill} in interviews", "summary": "Answer with a story, not a claim.", "minutes": 15},
        ],
        "resume_line": f"Built a small {skill} project end to end and documented the results.",
    }


def local_lesson(skill: str, index: int) -> dict:
    s = skill
    lessons = [
        {
            "sections": [
                {"heading": "Read the posting like a recruiter",
                 "body": f"When a job lists {s}, it rarely means mastery. It usually means you can do the everyday "
                         f"tasks the team uses {s} for without hand-holding.\n\nLook at the duties around the word. "
                         f"They tell you which part of {s} matters for that job."},
                {"heading": "How they check it",
                 "body": f"Recruiters look for {s} in your resume's bullets, not just a skills list. Interviewers ask "
                         f"you to describe a time you used it, or give you a small task.\n\nSo the goal is one real "
                         f"piece of work you can point to and explain."},
            ],
            "key_points": [f"Postings use {s} to mean everyday, working use", "The duties around the word show what matters",
                           "One real example beats a long skills list"],
            "practice": f"Open two jobs on your board that ask for {s}. Write down, in one line each, what they would want you to do with it.",
            "quiz": [
                {"q": f"When a job posting lists {s}, what does it usually mean?",
                 "options": ["You can do the everyday tasks with it on your own", "You are a world expert", "You have a paid certificate", "You have used it for ten years"],
                 "answer": 0, "why": "Most postings want working, everyday use."},
                {"q": f"Where do recruiters look for proof of {s}?",
                 "options": ["In your resume's bullets about real work", "Only in the skills list", "In your hobbies", "In your cover letter's greeting"],
                 "answer": 0, "why": "Bullets that show the skill being used carry the most weight."},
                {"q": "What best tells you which part of a skill a job needs?",
                 "options": ["The duties listed around it", "The company's logo", "The salary band", "The job's posting date"],
                 "answer": 0, "why": "The duties show how the skill is used day to day."},
            ],
        },
        {
            "sections": [
                {"heading": "Pick one good source",
                 "body": f"Start with one beginner course or video series on {s} and finish it before adding others. "
                         f"Jumping between many tutorials feels productive but leaves gaps.\n\nThe videos with this course are a good first pick."},
                {"heading": "Study in short blocks",
                 "body": "Thirty focused minutes a day beats one long weekend session. End each block by writing three "
                         "new terms in your own words.\n\nIf you can't explain a term simply, go back to it the next day."},
            ],
            "key_points": ["Finish one source before starting another", "Short daily blocks beat long sessions",
                           "Write new terms in your own words"],
            "practice": f"Watch the first video for this course and write five {s} terms in your own words.",
            "quiz": [
                {"q": "What's the better way to learn the basics?",
                 "options": ["Finish one good source first", "Sample many tutorials at once", "Skip the basics", "Only read reviews of courses"],
                 "answer": 0, "why": "Finishing one source avoids gaps."},
                {"q": "Which study rhythm does this lesson recommend?",
                 "options": ["About thirty focused minutes a day", "One long session a month", "Only before an interview", "Whenever you feel like it"],
                 "answer": 0, "why": "Short, regular blocks help it stick."},
                {"q": "How do you check you understood a new term?",
                 "options": ["Explain it simply in your own words", "Copy the definition word for word", "Highlight it", "Skip it"],
                 "answer": 0, "why": "If you can explain it simply, you understand it."},
            ],
        },
        {
            "sections": [
                {"heading": "Choose a tiny project",
                 "body": f"Pick something you could finish in a weekend that uses {s} the way the jobs on your board do. "
                         f"Real data or a real problem from your past work makes it more convincing.\n\nSmall and finished beats big and half done."},
                {"heading": "Write down what you did",
                 "body": "Keep short notes: the problem, what you tried, what worked, and the result in numbers if you can.\n\n"
                         "These notes become your resume bullet and your interview story."},
            ],
            "key_points": ["Small and finished beats big and half done", "Use a real problem where you can",
                           "Notes become your resume bullet and interview story"],
            "practice": f"Write a one-paragraph plan for a weekend {s} project: the problem, the data or tools, and what done looks like.",
            "quiz": [
                {"q": "What makes a good first practice project?",
                 "options": ["Small enough to finish, close to real work", "As large as possible", "Copied from a tutorial", "Something unrelated to the jobs you want"],
                 "answer": 0, "why": "A finished, relevant project is what you can show."},
                {"q": "Why keep notes while you build?",
                 "options": ["They become your resume bullet and interview story", "To make the project slower", "Recruiters ask for raw notes", "Notes are not useful"],
                 "answer": 0, "why": "Notes give you the facts and numbers to talk about later."},
                {"q": "What should the notes include?",
                 "options": ["Problem, what you tried, what worked, the result", "Only the tools' names", "Only screenshots", "Nothing about results"],
                 "answer": 0, "why": "That shape is a ready-made story."},
            ],
        },
        {
            "sections": [
                {"heading": "Write the bullet",
                 "body": f"Lead with an action verb, say what you did with {s}, and end with a result. For example: "
                         f"\"Used {s} to ... which cut ... by 20%\".\n\nOnly claim what you actually did."},
                {"heading": "Place it well",
                 "body": f"Put the bullet under a project or the role where you used it, and add {s} to your skills list too. "
                         f"Then update your resume in Profile Settings so Sunny Side scores your jobs again."},
            ],
            "key_points": ["Action verb, what you did, the result", "Only claim what you actually did",
                           "Update your resume so your matches improve"],
            "practice": f"Write one resume bullet about your {s} project using the action, what, result shape.",
            "quiz": [
                {"q": "What's the shape of a strong resume bullet?",
                 "options": ["Action verb, what you did, the result", "A list of tools only", "A paragraph about yourself", "The course name only"],
                 "answer": 0, "why": "It shows the skill being used and why it mattered."},
                {"q": "What should you never do on a resume?",
                 "options": ["Claim work you didn't do", "Use numbers", "Mention a project", "Use action verbs"],
                 "answer": 0, "why": "Interviewers will ask about it."},
                {"q": "Why update your resume in Profile Settings afterwards?",
                 "options": ["So your jobs are scored again with the new skill", "It deletes old jobs", "It sends your resume to recruiters", "It isn't needed"],
                 "answer": 0, "why": "Your match scores use the resume on file."},
            ],
        },
        {
            "sections": [
                {"heading": "Tell it as a story",
                 "body": f"When asked about {s}, use a short story: the situation, the task, what you did, and the result. "
                         f"Your project notes already hold every part.\n\nTwo minutes is plenty."},
                {"heading": "Be honest about level",
                 "body": f"If you're new to {s}, say so and show how fast you learned: what you built and what you'd do next. "
                         f"Interviewers value a clear learner over a vague expert.\n\nPractise out loud with Sunny's mock interview on the Calls page."},
            ],
            "key_points": ["Situation, task, action, result", "About two minutes per answer",
                           "Being a clear, honest learner wins trust"],
            "practice": f"Say your {s} story out loud in under two minutes, or run a mock interview with Sunny on the Calls page.",
            "quiz": [
                {"q": "How should you answer a question about a skill?",
                 "options": ["With a short story: situation, task, action, result", "With a list of buzzwords", "By changing the subject", "With a one-word answer"],
                 "answer": 0, "why": "A story shows you have actually used it."},
                {"q": "If you're new to the skill, what's the best approach?",
                 "options": ["Say so and show what you built and learned", "Pretend to be an expert", "Avoid the question", "Say you've never heard of it"],
                 "answer": 0, "why": "Honesty plus proof of progress builds trust."},
                {"q": "About how long should a story answer take?",
                 "options": ["About two minutes", "Ten minutes", "Five seconds", "As long as possible"],
                 "answer": 0, "why": "Long enough for detail, short enough to keep attention."},
            ],
        },
    ]
    lesson = dict(lessons[index % len(lessons)])
    lesson["example"] = ""
    lesson["quiz"] = _clean_quiz(lesson["quiz"], LESSON_QUESTIONS, f"local|{skill_key(skill)}|{index}")
    return lesson


# ── Course lifecycle ─────────────────────────────────────────────────────

def start_course(conn, user_id: int, skill: str, resume: str = "", api_key: str = "") -> tuple[dict, bool]:
    """The user's course for this skill, writing a new one if they don't have it. Returns (course, created)."""
    skill = clean_skill(skill)
    existing = find_course(conn, user_id, skill)
    if existing:
        return existing, False
    targets = jobs_asking_for(conn, user_id, skill)
    data, source = _ask_model(outline_prompt(skill, targets, resume), api_key, max_tokens=1500)
    outline = _clean_outline(data, skill) if data else None
    if not outline:
        outline, source = local_outline(skill, targets), "local"
    outline["lessons"] = outline["lessons"][:LESSONS]
    outline["targets"] = targets
    ts = now_iso()
    sql = ("INSERT INTO courses (user_id, skill, skill_key, outline, lessons, final_quiz, progress, source, created_at, updated_at) "
           "VALUES (?, ?, ?, ?, '{}', '[]', '{}', ?, ?, ?)")
    params = (user_id, skill, skill_key(skill), json.dumps(outline), source, ts, ts)
    if db.IS_POSTGRES:
        cid = conn.execute(sql + " RETURNING id", params).fetchone()[0]
    else:
        cid = conn.execute(sql, params).lastrowid
    conn.commit()
    return get_course(conn, user_id, cid), True


def lesson(conn, course: dict, index: int, api_key: str = "") -> dict | None:
    """A lesson's content, writing it the first time it's opened."""
    lessons = course["outline"].get("lessons") or []
    if not 0 <= index < len(lessons):
        return None
    key = str(index)
    if key not in course["lessons"]:
        content = None
        if course["source"] == "ai":
            data, _ = _ask_model(lesson_prompt(course["skill"], course["outline"], index), api_key, max_tokens=3000)
            content = _clean_lesson(data, f"{course['id']}|{index}") if data else None
        if not content:
            content = local_lesson(course["skill"], index)
            content["fallback"] = course["source"] == "ai"
        course["lessons"][key] = content
        _save(conn, course, "lessons")
    return course["lessons"][key]


def final_quiz(conn, course: dict, api_key: str = "") -> list[dict]:
    if not course["final_quiz"]:
        quiz = []
        if course["source"] == "ai":
            data, _ = _ask_model(final_prompt(course["skill"], course["outline"]), api_key, max_tokens=2500)
            quiz = _clean_quiz((data or {}).get("quiz") if isinstance(data, dict) else data, FINAL_QUESTIONS,
                               f"{course['id']}|final")
        if len(quiz) < 4:
            # Built from the lessons' own questions, reshuffled
            pool = []
            for i in range(len(course["outline"].get("lessons") or [])):
                pool.extend((lesson(conn, course, i, api_key) or {}).get("quiz") or [])
            rng = random.Random(f"{course['id']}|final-pool")
            rng.shuffle(pool)
            quiz = _clean_quiz(pool, FINAL_QUESTIONS, f"{course['id']}|final-pool")
        course["final_quiz"] = quiz
        _save(conn, course, "final_quiz")
    return course["final_quiz"]


def public_quiz(quiz: list[dict]) -> list[dict]:
    """Questions and options only: the answers stay on the server."""
    return [{"q": q["q"], "options": q["options"]} for q in quiz]


def grade(quiz: list[dict], answers) -> dict:
    answers = answers if isinstance(answers, list) else []
    results, right = [], 0
    for i, q in enumerate(quiz):
        try:
            picked = int(answers[i]) if i < len(answers) and answers[i] is not None else -1
        except (TypeError, ValueError):
            picked = -1
        ok = picked == q["answer"]
        right += ok
        results.append({"correct": ok, "picked": picked, "answer": q["answer"], "why": q.get("why", "")})
    total = len(quiz)
    return {"score": right, "total": total, "passed": total > 0 and right / total >= PASS_RATIO, "results": results}


def record_lesson_quiz(conn, course: dict, index: int, answers) -> dict:
    content = course["lessons"].get(str(index))
    if not content:
        raise KeyError("lesson not opened")
    result = grade(content["quiz"], answers)
    lp = course["progress"].setdefault("lessons", {})
    prev = lp.get(str(index)) or {}
    lp[str(index)] = {
        "best": max(prev.get("best", 0), result["score"]), "total": result["total"],
        "passed": bool(prev.get("passed")) or result["passed"], "attempts": prev.get("attempts", 0) + 1,
        "at": prev.get("at") if prev.get("passed") else (now_iso() if result["passed"] else None),
    }
    course["progress"]["last"] = index
    _save(conn, course, "progress")
    result["lesson"] = lp[str(index)]
    result["summary"] = summary(course)
    return result


def record_final(conn, course: dict, answers) -> dict:
    if not lessons_done(course):
        raise PermissionError("lessons not finished")
    result = grade(course["final_quiz"], answers)
    prev = course["progress"].get("final") or {}
    course["progress"]["final"] = {"best": max(prev.get("best", 0), result["score"]), "total": result["total"],
                                   "passed": bool(prev.get("passed")) or result["passed"],
                                   "attempts": prev.get("attempts", 0) + 1}
    fields = ["progress"]
    if result["passed"] and not course.get("completed_at"):
        course["completed_at"] = now_iso()
        fields.append("completed_at")
    _save(conn, course, *fields)
    result["summary"] = summary(course)
    return result


def lessons_done(course: dict) -> bool:
    n = len(course["outline"].get("lessons") or [])
    lp = course["progress"].get("lessons") or {}
    return n > 0 and all((lp.get(str(i)) or {}).get("passed") for i in range(n))


def summary(course: dict) -> dict:
    """What the catalog and the course page header show."""
    lessons = course["outline"].get("lessons") or []
    lp = course["progress"].get("lessons") or {}
    done = sum(1 for i in range(len(lessons)) if (lp.get(str(i)) or {}).get("passed"))
    final = course["progress"].get("final") or {}
    steps = len(lessons) + 1
    finished = done + (1 if final.get("passed") else 0)
    nxt = next((i for i in range(len(lessons)) if not (lp.get(str(i)) or {}).get("passed")), None)
    return {
        "id": course["id"], "skill": course["skill"], "title": course["outline"].get("title") or course["skill"],
        "tagline": course["outline"].get("tagline", ""), "level": course["outline"].get("level", "Beginner"),
        "lessons": len(lessons), "lessons_done": done, "minutes": sum(l.get("minutes", 15) for l in lessons),
        "percent": round(100 * finished / steps) if steps else 0,
        "final_unlocked": lessons_done(course), "final": final or None,
        "completed": bool(course.get("completed_at")), "completed_at": course.get("completed_at"),
        "next_lesson": nxt, "source": course["source"], "updated_at": course.get("updated_at"),
    }


def course_view(course: dict) -> dict:
    """The course page's data: outline, progress, and nothing that gives away an answer."""
    lp = course["progress"].get("lessons") or {}
    outline = course["outline"]
    return {
        **summary(course),
        "outcomes": outline.get("outcomes") or [],
        "targets": outline.get("targets") or [],
        "resume_line": outline.get("resume_line", "") if course.get("completed_at") else "",
        "outline": [{**l, "index": i, "progress": lp.get(str(i)) or None, "ready": str(i) in course["lessons"]}
                    for i, l in enumerate(outline.get("lessons") or [])],
    }


def list_courses(conn, user_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, user_id, skill, skill_key, outline, lessons, final_quiz, progress, source, "
                        "created_at, updated_at, completed_at FROM courses WHERE user_id = ? "
                        "ORDER BY COALESCE(updated_at, created_at) DESC", (user_id,)).fetchall()
    return [summary(_course(r)) for r in rows]


def certificate_id(course: dict) -> str:
    return hashlib.sha256(f"{course['id']}|{course['user_id']}|{course.get('completed_at')}".encode()).hexdigest()[:10].upper()
