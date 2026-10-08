"""
ai_engine.py — Scores jobs and generates cover letters.

WITHOUT an Anthropic key: uses local rule-based scoring and template cover letters.
WITH an Anthropic key:    uses Claude for rich, personalised output.

Set ANTHROPIC_API_KEY in .env to unlock AI mode. Everything works without it.
"""

import os
import re
import json
import sqlite3
from datetime import datetime
from db import DB_PATH, get_conn

from dotenv import load_dotenv
base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(dotenv_path=os.path.join(base_dir, ".env"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

def get_fallback_gemini_key() -> str:
    """The server-wide Gemini key (GEMINI_API_KEY), used when a user hasn't added their own.
    A key someone saved in their personal settings is never shared with other users."""
    return os.getenv("GEMINI_API_KEY", "")

# ── Fallback profile ───────────────────────────────────────────────────────
# Only used when a user hasn't uploaded a resume yet. Every real user's scoring,
# cover letters and prep come from their own resume.
PROFILE = {
    "name": os.getenv("YOUR_NAME", ""),
    "years_exp": int(os.getenv("YEARS_EXPERIENCE", "2")),
    "location": os.getenv("LOCATION", ""),
    "domain": "professional",
}

RESUME_TEXT = ""

# ── Keyword scoring weights ────────────────────────────────────────────────
# Role-neutral signals; resume keywords are added per user in _local_score.
POSITIVE_SIGNALS = {
    "remote": 0.4, "hybrid": 0.2,
}
NEGATIVE_SIGNALS = {
    "unpaid": -5.0,
}
YEARS_PATTERN = re.compile(r"(\d+)\+?\s*years?", re.IGNORECASE)


# ── Scoring ────────────────────────────────────────────────────────────────

_TITLE_STOPWORDS = {"and", "the", "of", "for", "in", "at", "to", "a", "an", "with", "i", "ii", "iii", "iv",
                    "sr", "senior", "jr", "junior", "lead", "principal", "staff", "associate", "head", "remote",
                    "hybrid", "india", "team", "role", "position", "job"}
_SENIOR_WORDS = re.compile(r"\b(senior|sr\.?|lead|principal|staff|head|director|vp|vice president)\b", re.IGNORECASE)
_JUNIOR_WORDS = re.compile(r"\b(intern|internship|junior|jr\.?|associate|apm|trainee|fresher|graduate)\b", re.IGNORECASE)


def _title_tokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9+#]+", (text or "").lower()) if w not in _TITLE_STOPWORDS and len(w) > 1}


def _title_similarity(a: str, b: str) -> float:
    """Overlap of meaningful title words, relative to the shorter title (0..1)."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _local_score(title: str, company: str, description: str, liked_titles: list = None, disliked_titles: list = None,
                 w_up: float = 1.0, w_app: float = 1.0, w_down: float = -1.0, w_rej: float = -1.5, resume_text: str = None,
                 years_exp: float = None, target_titles: list = None, user_skills: set = None,
                 job_skills: set = None) -> dict:
    """Rule-based fallback scoring — no API needed.

    Every component is bounded, so scores spread across the 1-10 range instead of
    piling up at 10 when a user has many similar liked jobs or a long resume:
      base 3.5 · title fit up to +3 · skill coverage up to +2.5 · experience -1.5..+0.5
      · seniority mismatch up to -1 · remote +0.3 · preference similarity about -1.25..+1
    """
    text = f"{title} {description}".lower()
    has_resume = bool(resume_text) and not resume_text.startswith("(No resume provided")
    score = 3.5

    # 1. Title fit against the roles the user is targeting (designation + past titles)
    targets = [t for t in (target_titles or []) if t and _title_tokens(t)]
    title_fit = max((_title_similarity(title, t) for t in targets), default=0.5)
    score += 3.0 * title_fit

    # 2. Skill coverage: how many of the skills the job asks for are on the resume
    job_skills = job_skills or set()
    user_skills = user_skills or set()
    if job_skills and has_resume:
        coverage = len(job_skills & user_skills) / len(job_skills)
    else:
        coverage = 0.4  # neutral when we can't tell
    score += 2.5 * coverage

    # 3. Experience required vs. the candidate's own
    if years_exp is None:
        years_exp = PROFILE["years_exp"]
        if has_resume:
            from resume_parser import parse_resume_local
            years_exp = parse_resume_local(resume_text).get("years_experience") or years_exp
    required = [int(m.group(1)) for m in YEARS_PATTERN.finditer(text) if 0 < int(m.group(1)) <= 25]
    if required:
        req_yrs = min(required)
        if req_yrs > years_exp + 2:
            score -= 1.5
        elif req_yrs > years_exp:
            score -= 0.5
        else:
            score += 0.5

    # 4. Seniority mismatch in the title
    if _SENIOR_WORDS.search(title) and years_exp < 4:
        score -= 1.0
    elif _JUNIOR_WORDS.search(title) and years_exp > 6:
        score -= 0.5

    for kw, weight in POSITIVE_SIGNALS.items():
        if kw in text:
            score += weight
    for kw, weight in NEGATIVE_SIGNALS.items():
        if kw in text:
            score += weight  # weights are negative

    # 5. Preferences: closest liked / disliked title (max, not sum, so it can't run away)
    liked_sim = max((_title_similarity(title, t) for t in (liked_titles or [])), default=0.0)
    disliked_sim = max((_title_similarity(title, t) for t in (disliked_titles or [])), default=0.0)
    score += 0.5 * (w_up + w_app) * 0.5 * liked_sim
    score += 0.5 * (w_down + w_rej) * 0.8 * disliked_sim

    score = max(1.0, min(10.0, score))

    reqs = sorted(job_skills)[:5] if job_skills else []
    if not reqs:
        for kw in ["sql", "python", "data", "roadmap", "stakeholder", "user research", "agile", "a/b test", "communication", "leadership"]:
            if kw in text:
                reqs.append(kw)
    reqs = [r.upper() if len(r) <= 3 else r.title() for r in reqs]

    fit_parts = []
    if score >= 8:
        fit_parts.append("Strong fit — role aligns well with your background.")
    elif score >= 6:
        fit_parts.append("Solid match — most requirements align with your experience.")
    else:
        fit_parts.append("Partial match — some requirements may be a stretch.")
    if job_skills and has_resume:
        fit_parts.append(f"You cover {len(job_skills & user_skills)} of {len(job_skills)} skills this role mentions.")
    if "remote" in text:
        fit_parts.append("Remote-friendly.")

    return {
        "score": round(score, 1),
        "fit_summary": " ".join(fit_parts),
        "key_requirements": reqs[:5],
        "mode": "local",
    }


def _ai_score(title: str, company: str, description: str, resume_text: str = None) -> dict:
    """Claude-powered scoring — used when API key is set."""
    if not resume_text:
        resume_text = RESUME_TEXT or "(No resume provided yet. Keep the assessment general.)"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=45)
        prompt = f"""Score this job for fit with this candidate. Reply ONLY with JSON, no markdown.

CANDIDATE RESUME:
{resume_text}

JOB: {title} at {company}
DESCRIPTION: {description[:2000]}

Return exactly:
{{"score": <0-10 float>, "fit_summary": "<2 sentences>", "key_requirements": ["<req1>","<req2>","<req3>"]}}"""

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = re.sub(r"```json|```", "", msg.content[0].text).strip()
        data = _normalize_ai_score(json.loads(raw))
        data["mode"] = "ai"
        return data
    except Exception as e:
        print(f"  [AI score fallback] {e}")
        return None


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "45"))


def _call_gemini(prompt: str, response_json: bool = False, api_key: str = None) -> str:
    import time
    import requests
    key_to_use = api_key or get_fallback_gemini_key()
    if not key_to_use:
        raise ValueError("No Gemini API key configured. Provide it in profile settings or set GEMINI_API_KEY env.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    if response_json:
        payload["generationConfig"] = {"responseMimeType": "application/json"}

    # The key goes in a header, not the URL, so it can't leak into logs or exception text
    headers = {"Content-Type": "application/json", "x-goog-api-key": key_to_use}
    for attempt in range(2):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=GEMINI_TIMEOUT)
        except requests.Timeout:
            raise RuntimeError(f"Gemini timed out after {GEMINI_TIMEOUT:.0f}s")
        if response.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            time.sleep(1.5)
            continue
        if response.status_code != 200:
            raise RuntimeError(f"Gemini returned HTTP {response.status_code}")
        break
    res_data = response.json()
    return res_data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _gemini_score(title: str, company: str, description: str, resume_text: str = None, api_key: str = None,
                  liked_titles: list = None, disliked_titles: list = None,
                  w_up: float = 1.0, w_app: float = 1.0, w_down: float = -1.0, w_rej: float = -1.5) -> dict:
    if not resume_text:
        resume_text = RESUME_TEXT or "(No resume provided yet. Keep the assessment general.)"
    try:
        prompt = f"""Score this job for fit with this candidate. Reply ONLY with JSON, no markdown.

CANDIDATE RESUME:
{resume_text}
"""
        if liked_titles:
            prompt += f"\nUSER PREFERENCES (Roles the user liked or actively applied to; weight thumbs up = {w_up}, weight active stages = {w_app}):\n" + "\n".join(f"- {t}" for t in liked_titles[:10])
        if disliked_titles:
            prompt += f"\nUSER PREFERENCES (Roles the user disliked or rejected; weight thumbs down = {w_down}, weight rejected = {w_rej}):\n" + "\n".join(f"- {t}" for t in disliked_titles[:10])

        prompt += f"""\nJOB: {title} at {company}
DESCRIPTION: {description[:2000]}

Return exactly:
{{"score": <0-10 float>, "fit_summary": "<2 sentences>", "key_requirements": ["<req1>","<req2>","<req3>"]}}"""

        raw = _call_gemini(prompt, response_json=True, api_key=api_key)
        data = _normalize_ai_score(json.loads(raw))
        data["mode"] = "gemini"
        return data
    except Exception as e:
        print(f"  [Gemini score fallback] {e}")
        return None


def _normalize_ai_score(data) -> dict:
    """Checks a model's score reply and coerces it to the shape the app stores."""
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("score reply is not an object")
    score = float(data.get("score"))
    if score != score:  # NaN
        raise ValueError("score is not a number")
    reqs = data.get("key_requirements") or []
    if not isinstance(reqs, list):
        reqs = [str(reqs)]
    return {
        "score": round(max(1.0, min(10.0, score)), 1),
        "fit_summary": str(data.get("fit_summary") or "").strip() or "Scored by AI.",
        "key_requirements": [str(r) for r in reqs][:5],
    }


def score_job(title: str, company: str, description: str, resume_text: str = None, api_key: str = None, user_id: int = 1,
              force_local: bool = False) -> dict:
    if not resume_text:
        resume_text = RESUME_TEXT or "(No resume provided yet. Keep the assessment general.)"

    liked_titles = []
    disliked_titles = []
    designation = ""
    w_up, w_app, w_down, w_rej = 1.0, 1.0, -1.0, -1.5
    try:
        conn = get_conn(DB_PATH)
        row = conn.execute("SELECT weight_thumbs_up, weight_applied, weight_thumbs_down, weight_rejected, designation FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            w_up = row[0] if row[0] is not None else 1.0
            w_app = row[1] if row[1] is not None else 1.0
            w_down = row[2] if row[2] is not None else -1.0
            w_rej = row[3] if row[3] is not None else -1.5
            designation = row[4] or ""

        liked = conn.execute(
            "SELECT DISTINCT title FROM jobs WHERE user_id = ? AND (feedback = 1 OR status IN ('applied', 'shortlisted', 'interviewing', 'offer'))",
            (user_id,)
        ).fetchall()
        liked_titles = [r[0] for r in liked if r[0]]
        
        disliked = conn.execute(
            "SELECT DISTINCT title FROM jobs WHERE user_id = ? AND (feedback = -1 OR status = 'rejected')",
            (user_id,)
        ).fetchall()
        disliked_titles = [r[0] for r in disliked if r[0]]
        conn.close()
    except Exception as e:
        print(f"Error fetching liked/disliked jobs: {e}")

    # Fetch parsed resume profile for skills analysis
    resume_profile = {}
    try:
        conn = get_conn(DB_PATH)
        p_row = conn.execute("SELECT resume_profile_json FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        if p_row and p_row[0]:
            resume_profile = json.loads(p_row[0])
    except Exception as e:
        print(f"Error reading resume profile: {e}")

    if not resume_profile or not resume_profile.get("skills"):
        from resume_parser import parse_resume_local
        resume_profile = parse_resume_local(resume_text or "")

    user_skills = set(
        [s.lower().strip() for s in resume_profile.get("skills", [])] +
        [t.lower().strip() for t in resume_profile.get("tools", [])] +
        [d.lower().strip() for d in resume_profile.get("domains", [])]
    )

    skills_lexicon = [
        "python", "sql", "javascript", "java", "c++", "c#", "go", "rust", "ruby", "php", "typescript",
        "product roadmapping", "product management", "user research", "agile", "scrum", "kanban", "jira", "confluence",
        "stakeholder management", "go-to-market", "gtm", "market research", "ab testing", "a/b testing", "data analytics",
        "tableau", "power bi", "looker", "mixpanel", "amplitude", "figma", "sketch", "wireframing", "prototyping",
        "machine learning", "deep learning", "nlp", "llm", "conversational ai", "prompt engineering", "retell", "deepgram",
        "bfsi", "fintech", "saas", "edtech", "healthcare", "e-commerce", "retail", "cloud computing", "aws", "gcp", "azure",
        "docker", "kubernetes", "git", "github", "ci/cd", "devops",
        "react", "node.js", "django", "flask", "spring", "html", "css", "excel", "statistics", "spark", "airflow",
        "salesforce", "hubspot", "crm", "seo", "sem", "content marketing", "performance marketing", "copywriting",
        "b2b", "b2c", "lead generation", "negotiation", "account management", "financial modeling", "accounting",
        "supply chain", "operations", "logistics", "project management", "six sigma", "user experience", "ux", "ui",
        "adobe", "photoshop", "illustrator", "communication", "leadership", "customer success", "recruiting"
    ]

    job_skills = set()
    desc_lower = description.lower()
    title_lower = title.lower()
    for word in skills_lexicon:
        pattern = rf"\b{re.escape(word)}\b"
        if re.search(pattern, desc_lower) or re.search(pattern, title_lower):
            job_skills.add(word)

    matched = []
    missing = []
    for s in job_skills:
        name = s.upper() if len(s) <= 3 else s.title()
        if s in user_skills:
            matched.append(name)
        else:
            missing.append(name)

    # AI scoring when a key is set; the full local scorer is the fallback if it fails
    res = None
    key_to_use = None if force_local else (api_key or get_fallback_gemini_key())
    if ANTHROPIC_API_KEY and not force_local:
        res = _ai_score(title, company, description, resume_text)
    elif key_to_use:
        res = _gemini_score(title, company, description, resume_text, api_key=key_to_use,
                            liked_titles=liked_titles, disliked_titles=disliked_titles,
                            w_up=w_up, w_app=w_app, w_down=w_down, w_rej=w_rej)
    if res is None:
        try:
            years_exp = float(resume_profile.get("years_experience") or 0) or None
        except (TypeError, ValueError):
            years_exp = None
        target_titles = [designation] + list(resume_profile.get("titles") or [])
        res = _local_score(title, company, description, liked_titles=liked_titles, disliked_titles=disliked_titles,
                           w_up=w_up, w_app=w_app, w_down=w_down, w_rej=w_rej, resume_text=resume_text,
                           years_exp=years_exp, target_titles=target_titles,
                           user_skills=user_skills, job_skills=job_skills)
        if not force_local and (ANTHROPIC_API_KEY or key_to_use):
            res["fit_summary"] += " (scored locally, AI scoring was unavailable)"

    # Attach matched and missing lists
    res["matched_skills"] = json.dumps(matched)
    res["missing_skills"] = json.dumps(missing)
    return res


# ── Cover letter ───────────────────────────────────────────────────────────

def _local_cover_letter(title: str, company: str, description: str,
                         contact_name: str, contact_title: str, candidate_name: str = "") -> dict:
    """Template cover letter — personalised from your profile, no API needed."""
    greeting = f"Hi {contact_name.split()[0]}," if contact_name and contact_name != "Hiring Team" else "Hi,"

    candidate_name = candidate_name or PROFILE["name"]
    signoff = candidate_name or ""

    body = f"""{greeting}

I'm excited to apply for the {title} role at {company}. I noticed {description[:120].rstrip().rstrip('.') + '...' if description else 'the scope of this role'} and I think my background maps well.

A few things I'd bring on day one:
- A habit of starting from the problem and the people affected by it
- Steady cross-functional execution with the teams around me
- A data-informed approach: I set clear goals and measure against them

I'd love 20 minutes to learn more about the team and share how I've tackled similar challenges.

{signoff}"""

    subject = f"Application: {title}" + (f" — {candidate_name}" if candidate_name else "")
    return {
        "subject": subject,
        "body": body.strip(),
    }


def _ai_cover_letter(title: str, company: str, description: str,
                      contact_name: str, contact_title: str, resume_text: str = None, candidate_name: str = "") -> dict:
    """Claude-generated cover letter."""
    if not resume_text:
        resume_text = RESUME_TEXT or "(No resume provided yet. Keep the assessment general.)"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=45)
        prompt = f"""Write a cover letter for this job application. Under 300 words. 
Start with impact — not "I am writing to express my interest". Be specific, confident, human.
Reply ONLY with JSON, no markdown.

CANDIDATE:
{resume_text}

JOB: {title} at {company}
CONTACT: {contact_name} ({contact_title})
DESCRIPTION: {description[:1800]}

Return exactly:
{{"subject": "<subject line>", "body": "<full cover letter>"}}"""

        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = re.sub(r"```json|```", "", msg.content[0].text).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  [AI cover letter fallback] {e}")
        return _local_cover_letter(title, company, description, contact_name, contact_title, candidate_name)


def _gemini_cover_letter(title: str, company: str, description: str,
                         contact_name: str, contact_title: str, resume_text: str = None, api_key: str = None,
                         candidate_name: str = "") -> dict:
    if not resume_text:
        resume_text = RESUME_TEXT or "(No resume provided yet. Keep the assessment general.)"
    try:
        prompt = f"""Write a cover letter for this job application. Under 300 words. 
Start with impact — not "I am writing to express my interest". Be specific, confident, human.
Reply ONLY with JSON, no markdown.

CANDIDATE:
{resume_text}

JOB: {title} at {company}
CONTACT: {contact_name} ({contact_title})
DESCRIPTION: {description[:1800]}

Return exactly:
{{"subject": "<subject line>", "body": "<full cover letter>"}}"""

        raw = _call_gemini(prompt, response_json=True, api_key=api_key)
        return json.loads(raw)
    except Exception as e:
        print(f"  [Gemini cover letter fallback] {e}")
        return _local_cover_letter(title, company, description, contact_name, contact_title, candidate_name)


def generate_cover_letter(title: str, company: str, description: str,
                           contact_name: str = "Hiring Team",
                           contact_title: str = "Recruiter",
                           resume_text: str = None, api_key: str = None,
                           candidate_name: str = "") -> dict:
    if ANTHROPIC_API_KEY:
        return _ai_cover_letter(title, company, description, contact_name, contact_title, resume_text, candidate_name)
    
    key_to_use = api_key or get_fallback_gemini_key()
    if key_to_use:
        return _gemini_cover_letter(title, company, description, contact_name, contact_title, resume_text,
                                    api_key=key_to_use, candidate_name=candidate_name)
    return _local_cover_letter(title, company, description, contact_name, contact_title, candidate_name)


# ── LinkedIn note ──────────────────────────────────────────────────────────

def generate_linkedin_note(contact_name: str, contact_title: str,
                            company: str, job_title: str, api_key: str = None) -> str:
    first = contact_name.split()[0] if contact_name and contact_name != "Hiring Team" else "there"
    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=45)
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=80,
                messages=[{"role": "user", "content":
                    f"Write a LinkedIn connection note (under 280 chars) from a candidate applying for "
                    f"{job_title} at {company} to {contact_name} ({contact_title}). "
                    f"Warm, specific, not salesy. Return only the note text."}],
            )
            return msg.content[0].text.strip()
        except Exception:
            pass
            
    key_to_use = api_key or get_fallback_gemini_key()
    if key_to_use:
        try:
            prompt = f"Write a LinkedIn connection note (under 280 chars) from a candidate applying for {job_title} at {company} to {contact_name} ({contact_title}). Warm, specific, not salesy. Return only the note text."
            return _call_gemini(prompt, response_json=False, api_key=key_to_use)
        except Exception:
            pass
            
    return (
        f"Hi {first}, I came across the {job_title} opening at {company} and "
        f"was really drawn to the product challenges you're solving. "
        f"Would love to connect and learn more about the team."
    )


def generate_tailored_resume(job_description: str, job_title: str, company: str, resume_text: str = None,
                             api_key: str = None) -> str:
    """Generate a tailored resume based on the candidate profile and job description."""
    if not resume_text:
        return "# No resume on file\n\nUpload your resume in settings to generate a tailored version for this job."
    prompt = f"""
You are an expert resume writer. Given the candidate's base resume and the target job description (JD) at {company} for the role of {job_title}, generate a highly tailored professional resume in Markdown format.

Base Resume:
{resume_text}

Target Job Description:
{job_description}

Guidelines for tailoring:
1. Retain the core structure, contact details, certifications, dates, and locations exactly as is (do not modify years of experience, degree names, or job duration dates).
2. Adjust the language of the professional experience and academic projects bullet points to subtly emphasize alignment with the JD's requirements and keywords.
3. For all job and project headings, preserve the HTML span float layout exactly for side-by-side alignment:
   **[Role Name or Project Name]** <span style="float: right;">[Dates]</span>
   _[Company Name or Context] | [Location or Details]_
4. Order the list of SKILLS so that the most relevant tools for the JD are highlighted first.
5. Format the output as clean, professional Markdown matching the structure of the base resume.
6. Do not include any introductory remarks or meta-commentary; output ONLY the Markdown resume.
"""
    try:
        key_to_use = api_key or get_fallback_gemini_key()
        if key_to_use:
            return _call_gemini(prompt, api_key=key_to_use)
    except Exception as e:
        print(f"[AI Resume] Gemini failed: {e}")
        
    return resume_text


# ── Process all new jobs ───────────────────────────────────────────────────

def rescore_saturated_scores(db_path: str = DB_PATH) -> int:
    """Jobs scored by the old keyword scorer mostly sit at 10. Re-score those inbox jobs with
    the current local scorer (no API calls) so the board's ranking means something again."""
    conn = get_conn(db_path)
    rows = conn.execute(
        "SELECT j.job_id, j.title, j.company, j.description, j.user_id, u.resume_text FROM jobs j "
        "JOIN users u ON u.id = j.user_id "
        "WHERE j.ai_score >= 9.5 AND j.status IN ('new', 'scored', 'ready')"
    ).fetchall()
    conn.close()
    done = 0
    for job_id, title, company, description, uid, resume_text in [tuple(r) for r in rows]:
        data = score_job(title or "", company or "", description or "", resume_text=resume_text,
                         user_id=uid, force_local=True)
        conn = get_conn(db_path)
        conn.execute(
            "UPDATE jobs SET ai_score=?, ai_summary=?, key_reqs=?, matched_skills=?, missing_skills=? WHERE job_id=? AND user_id=?",
            (data["score"], data["fit_summary"], json.dumps(data.get("key_requirements", [])),
             data["matched_skills"], data["missing_skills"], job_id, uid),
        )
        conn.commit()
        conn.close()
        done += 1
    print(f"Re-scored {done} jobs that still had saturated scores")
    return done


def process_new_jobs(db_path: str = DB_PATH, min_score: float = 6.0, user_id: int = 1) -> list[dict]:
    """Score and generate cover letters for all 'new' jobs in the DB."""
    conn = get_conn(db_path)
    from db import get_user_secrets
    row = conn.execute("SELECT resume_text, name FROM users WHERE id = ?", (user_id,)).fetchone()
    resume_text = row[0] if row else None
    candidate_name = (row[1] if row else "") or ""
    api_key = get_user_secrets(conn, user_id)["gemini_api_key"] or None

    jobs = conn.execute(
        "SELECT job_id, title, company, location, url, description FROM jobs WHERE status = 'new' AND user_id = ?",
        (user_id,)
    ).fetchall()

    if ANTHROPIC_API_KEY:
        mode = "AI (Claude)"
    elif api_key or get_fallback_gemini_key():
        mode = "AI (Gemini)"
    else:
        mode = "local rules (add API keys for AI)"
    print(f"\n[Scoring {len(jobs)} jobs — mode: {mode}]")

    digest = []
    for job in jobs:
        job_id, title, company, location, url, description = tuple(job)
        print(f"  {title} @ {company}...", end=" ")

        score_data = score_job(title, company, description or "", resume_text=resume_text, api_key=api_key, user_id=user_id)
        score = score_data["score"]

        key_reqs_json = json.dumps(score_data.get("key_requirements", []))
        conn.execute(
            "UPDATE jobs SET ai_score=?, ai_summary=?, key_reqs=?, status=?, matched_skills=?, missing_skills=? WHERE job_id=? AND user_id=?",
            (score, score_data["fit_summary"], key_reqs_json, "scored", score_data["matched_skills"], score_data["missing_skills"], job_id, user_id),
        )
        conn.commit()

        print(f"score {score}")

        if score < min_score:
            continue

        contact = conn.execute(
            "SELECT name, title FROM contacts WHERE job_id=? AND user_id=? LIMIT 1", (job_id, user_id)
        ).fetchone()
        contact_name = contact[0] if contact else "Hiring Team"
        contact_title = contact[1] if contact else "Recruiter"

        letter = generate_cover_letter(title, company, description or "",
                                       contact_name, contact_title, resume_text=resume_text, api_key=api_key,
                                       candidate_name=candidate_name)
        linkedin_note = generate_linkedin_note(contact_name, contact_title, company, title, api_key=api_key)

        conn.execute(
            """INSERT OR REPLACE INTO cover_letters
               (job_id, subject, body, linkedin_note, created_at, user_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, letter["subject"], letter["body"],
             linkedin_note, datetime.now().isoformat(), user_id),
        )
        conn.execute(
            "UPDATE jobs SET status='ready' WHERE job_id=? AND user_id=?", (job_id, user_id)
        )
        conn.commit()

        digest.append({
            "job_id": job_id, "title": title, "company": company,
            "location": location, "url": url, "score": score,
            "fit_summary": score_data["fit_summary"],
            "key_requirements": score_data.get("key_requirements", []),
            "contact_name": contact_name, "contact_title": contact_title,
            "cover_letter_subject": letter["subject"],
            "cover_letter_body": letter["body"],
            "linkedin_note": linkedin_note,
        })

    conn.close()
    print(f"\n  {len(digest)} jobs scored above {min_score} — cover letters generated\n")
    return digest


def generate_interview_prep(title: str, company: str, description: str,
                            resume_text: str = None, api_key: str = None) -> dict:
    if not resume_text:
        resume_text = RESUME_TEXT or "(No resume provided yet. Keep the assessment general.)"
    if ANTHROPIC_API_KEY:
        try:
            return _ai_interview_prep(title, company, description, resume_text)
        except Exception:
            pass
    
    key_to_use = api_key or get_fallback_gemini_key()
    if key_to_use:
        try:
            return _gemini_interview_prep(title, company, description, resume_text, api_key=key_to_use)
        except Exception as e:
            print(f"Gemini prep failed, falling back to local: {e}")
            
    return _local_interview_prep(title, company)


def _local_interview_prep(title, company):
    quick = [
        {
            "q": f"Why do you want to join {company} as a {title}?",
            "a": "Mention your passion for their industry, specify 1-2 product highlights of theirs, and explain how your background aligns with their current expansion."
        },
        {
            "q": "Walk me through your resume in 60 seconds.",
            "a": "State your current role/focus, highlight 2 key achievements (ideally quantitative), and tie your career trajectory back to why you are here today."
        },
        {
            "q": f"What do you think are the core challenges {company} is facing in the market?",
            "a": "Identify their main competitors, highlight current macro/technical shifts, and propose 1-2 ways a person in your role can help mitigate them."
        },
        {
            "q": "Tell me about a time you managed a difficult stakeholder or team conflict.",
            "a": "Describe the context, highlight how you practiced active listening to align goals, explain the solution implemented, and name the resulting metrics."
        },
        {
            "q": "What are your salary expectations and availability?",
            "a": "Keep it professional. Mention that you are open to competitive market rates depending on total package value, and state your standard notice period."
        }
    ]
    deep = [
        {
            "q": f"How would you approach designing a new feature or optimization for {company}'s core product?",
            "hints": "Define goals -> Identify user segments -> Ideate solutions -> Prioritize using a framework -> Define metrics."
        },
        {
            "q": "Tell me about a project you led that had significant business impact. What were the key metrics?",
            "hints": "Use STAR method. Focus on your specific contribution, the outcome, and quantifiable metrics (revenue, conversion, etc.)."
        },
        {
            "q": "How do you prioritize competing requests from multiple teams or leadership?",
            "hints": "Explain your framework (e.g., ROI, effort vs. impact, alignment with company objectives). Mention communication."
        },
        {
            "q": "Describe a time you failed or made a major mistake. What did you learn and how did you handle it?",
            "hints": "Choose a real but professional mistake. Take full ownership, explain the mitigation steps, and highlight the long-term learning."
        },
        {
            "q": "What technical or analytical tools do you rely on to make product and engineering decisions?",
            "hints": "Mention specific tools (SQL, Mixpanel, Tableau, Jira) and explain how data/metrics guide your roadmap decisions."
        }
    ]
    return {"quick_questions": quick, "deep_questions": deep}


def _ai_interview_prep(title, company, description, resume_text):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=45)
        prompt = f"""
        You are an elite interview coach preparing a candidate for a {title} role at {company}.
        
        Job Description:
        {description}
        
        Candidate Resume:
        {resume_text}
        
        Generate exactly the following:
        1. 5 warm-up 'Quick-fire' questions. For each question, draft a brief, personalized model answer draft (2-3 sentences) tailored specifically to the candidate's actual experience/credentials in their resume.
        2. Exactly 7 'Deeper prep' questions tailored to the gaps/matches between the resume and the job description, along with bulleted model answer outlines/hints. Wherever possible, structure these outlines using the STAR framework (Situation, Task, Action, Result) to guide the candidate. Ensure these questions are structured across these categories:
           - Motivation & Fit for this specific domain.
           - Customer Engagement & Ownership (handling client alignment or expectations).
           - Core Judgment Call (how to decide configuration/prompt fix vs. core product roadmap gap).
           - Guardrails & Compliance constraints relevant to this industry.
           - Experimentation & Metrics (how to design A/B testing or track performance).
           - Walkthrough of their first 30 days on this deployment.
           - Troubleshooting (funnel analysis of a failing product/operational metric).
        
        Return EXACTLY a JSON object matching this structure. Do not add markdown fences:
        {{
          "quick_questions": [
             {{"q": "Elevator Pitch or why this role?", "a": "Personalized outline/draft based on candidate's resume..."}},
             ...
          ],
          "deep_questions": [
             {{"q": "Question 1", "hints": "Model answer outline bullet points..."}},
             ...
          ]
        }}
        """
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        res_text = msg.content[0].text.strip()
        if res_text.startswith("```"):
            res_text = re.sub(r"^```(?:json)?\n|```$", "", res_text, flags=re.MULTILINE)
        return json.loads(res_text.strip())
    except Exception as e:
        print(f"Claude interview prep failed: {e}")
        raise e


def _gemini_interview_prep(title, company, description, resume_text, api_key):
    prompt = f"""
    You are an elite interview coach preparing a candidate for a {title} role at {company}.
    
    Job Description:
    {description}
    
    Candidate Resume:
    {resume_text}
    
    Generate exactly the following:
    1. 5 warm-up 'Quick-fire' questions. For each question, draft a brief, personalized model answer draft (2-3 sentences) tailored specifically to the candidate's actual experience/credentials in their resume.
    2. Exactly 7 'Deeper prep' questions tailored to the gaps/matches between the resume and the job description, along with bulleted model answer outlines/hints. Wherever possible, structure these outlines using the STAR framework (Situation, Task, Action, Result) to guide the candidate. Ensure these questions are structured across these categories:
       - Motivation & Fit for this specific domain.
       - Customer Engagement & Ownership (handling client alignment or expectations).
       - Core Judgment Call (how to decide configuration/prompt fix vs. core product roadmap gap).
       - Guardrails & Compliance constraints relevant to this industry.
       - Experimentation & Metrics (how to design A/B testing or track performance).
       - Walkthrough of their first 30 days on this deployment.
       - Troubleshooting (funnel analysis of a failing product/operational metric).
    
    Return EXACTLY a JSON object matching this structure. Do not add markdown fences:
    {{
      "quick_questions": [
         {{"q": "Elevator Pitch or why this role?", "a": "Personalized outline/draft based on candidate's resume..."}},
         ...
      ],
      "deep_questions": [
         {{"q": "Question 1", "hints": "Model answer outline bullet points..."}},
         ...
      ]
    }}
    """
    res_text = _call_gemini(prompt, response_json=True, api_key=api_key)
    if res_text.startswith("```"):
        res_text = re.sub(r"^```(?:json)?\n|```$", "", res_text, flags=re.MULTILINE)
    return json.loads(res_text.strip())


if __name__ == "__main__":
    results = process_new_jobs()
    for r in results:
        print(f"\n{'='*55}")
        print(f"{r['title']} @ {r['company']} — {r['score']}/10")
        print(r["fit_summary"])
