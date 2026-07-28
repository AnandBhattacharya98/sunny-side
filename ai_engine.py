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
    key = os.getenv("GEMINI_API_KEY", "")
    if key:
        return key
    try:
        from db import get_conn, DB_PATH
        conn = get_conn(DB_PATH)
        row = conn.execute("SELECT gemini_api_key FROM users WHERE id = 1").fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return ""

# ── Your profile — edit this section ──────────────────────────────────────
PROFILE = {
    "name": os.getenv("YOUR_NAME", "Anand Bhattacharya"),
    "years_exp": int(os.getenv("YEARS_EXPERIENCE", "1")),
    "location": os.getenv("LOCATION", "Bengaluru"),
    "current_role": "Software Engineer",
    "domain": "web development, API design, and cloud architecture",
    "strengths": [
        "full stack web development",
        "API integrations",
        "database scaling",
        "system performance tuning",
        "CI/CD workflows"
    ],
    "tools": ["Python", "Flask", "JavaScript", "SQL", "Docker", "Git", "Jira"],
    "achievements": [
        "Designed and optimized backend APIs for scale, reducing response latency by 20%",
        "Developed auto-ingestion pipelines, cutting data syncing cycle time by 30%",
        "Collaborated with cross-functional teams to ship new client dashboards, increasing engagement by 40%"
    ],
    "education": "BE in Computer Science",
}

RESUME_TEXT = f"""
Name: {PROFILE['name']}
Role: {PROFILE['current_role']} | {PROFILE['years_exp']}+ years
Location: {PROFILE['location']}
Domain: {PROFILE['domain']}

Strengths: {', '.join(PROFILE['strengths'])}
Tools: {', '.join(PROFILE['tools'])}
Education: {PROFILE['education']}

Key achievements:
""" + "\n".join(f"- {a}" for a in PROFILE["achievements"])

# ── Keyword scoring weights ────────────────────────────────────────────────
# Updated weights to match Anand's AI APM profile
POSITIVE_SIGNALS = {
    "ai": 1.5, "machine learning": 1.2, "voice": 1.5, "conversational": 1.5,
    "llm": 1.2, "speech": 1.2, "tts": 1.0, "stt": 1.0, "bfsi": 1.2,
    "nlp": 1.0, "data science": 0.8, "python": 0.6, "sql": 0.5,
    "product manager": 0.8, "apm": 0.8, "associate product manager": 1.0,
    "bengaluru": 0.5, "bangalore": 0.5, "remote": 0.4,
    "roadmap": 0.4, "stakeholder": 0.4, "user research": 0.5,
}
NEGATIVE_SIGNALS = {
    "5+ years": -1.5, "8+ years": -2.0, "10+ years": -2.5, "12+ years": -3.0,
    "unpaid": -5.0,
}
YEARS_PATTERN = re.compile(r"(\d+)\+?\s*years?", re.IGNORECASE)


# ── Scoring ────────────────────────────────────────────────────────────────

def _local_score(title: str, company: str, description: str, liked_titles: list = None, disliked_titles: list = None,
                 w_up: float = 1.0, w_app: float = 1.0, w_down: float = -1.0, w_rej: float = -1.5, resume_text: str = None) -> dict:
    """Rule-based fallback scoring — no API needed."""
    text = f"{title} {description}".lower()
    score = 5.0

    # Build dynamic positive signals based on resume content keywords
    custom_signals = dict(POSITIVE_SIGNALS)
    if resume_text and len(resume_text.strip()) > 50:
        first_part = resume_text[:600].lower()
        roles = ["software engineer", "frontend", "backend", "full stack", "data scientist", "product manager", "designer", "analyst", "developer", "marketing", "sales", "consultant"]
        for r in roles:
            if r in first_part:
                custom_signals[r] = 1.5
        
        words = re.findall(r"\b[a-zA-Z]{4,15}\b", first_part)
        stops = {"with", "that", "this", "from", "have", "about", "their", "there", "which", "would", "could", "should"}
        words = [w for w in words if w not in stops]
        from collections import Counter
        common = Counter(words).most_common(8)
        for word, count in common:
            if word not in custom_signals:
                custom_signals[word] = 0.5

    for kw, weight in custom_signals.items():
        if kw in text:
            score += weight
    for kw, weight in NEGATIVE_SIGNALS.items():
        if kw in text:
            score += weight  # weights are negative

    # Year experience check
    for m in YEARS_PATTERN.finditer(text):
        req_yrs = int(m.group(1))
        if req_yrs > PROFILE["years_exp"] + 2:
            score -= 1.5
        elif req_yrs <= PROFILE["years_exp"] + 1:
            score += 0.5

    # Title word overlap adjustments based on liked/disliked jobs and user weights
    score_adj = 0.0
    title_words = set(re.findall(r"\w+", title.lower()))
    if liked_titles:
        for t in liked_titles:
            overlap = len(title_words.intersection(set(re.findall(r"\w+", t.lower()))))
            if overlap > 1:
                weight = 0.5 * (w_up + w_app)
                score_adj += 0.5 * (overlap - 1) * weight
    if disliked_titles:
        for t in disliked_titles:
            overlap = len(title_words.intersection(set(re.findall(r"\w+", t.lower()))))
            if overlap > 1:
                weight = 0.5 * (w_down + w_rej)
                score_adj += 0.5 * (overlap - 1) * weight
    score += score_adj

    score = max(1.0, min(10.0, score))

    reqs = []
    for kw in ["sql", "data", "roadmap", "stakeholder", "user research", "agile", "a/b test"]:
        if kw in text:
            reqs.append(kw.title())

    fit_parts = []
    if score >= 8:
        fit_parts.append(f"Strong fit — role aligns well with your {PROFILE['domain']} background.")
    elif score >= 6:
        fit_parts.append("Solid match — most requirements align with your experience.")
    else:
        fit_parts.append("Partial match — some requirements may be a stretch.")

    if "bengaluru" in text or "bangalore" in text or "remote" in text:
        fit_parts.append("Location is ideal.")
    if any(k in text for k in ["ai", "voice", "conversational", "llm", "speech", "tts", "stt"]):
        fit_parts.append("Domain aligns with your conversational AI and voice agent experience.")

    return {
        "score": round(score, 1),
        "fit_summary": " ".join(fit_parts),
        "key_requirements": reqs[:5],
        "mode": "local",
    }


def _ai_score(title: str, company: str, description: str, resume_text: str = None) -> dict:
    """Claude-powered scoring — used when API key is set."""
    if not resume_text:
        resume_text = RESUME_TEXT
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Score this PM job for fit with this candidate. Reply ONLY with JSON, no markdown.

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
        data = json.loads(raw)
        data["mode"] = "ai"
        return data
    except Exception as e:
        print(f"  [AI score fallback] {e}")
        result = _local_score(title, company, description)
        result["fit_summary"] += " (scored locally — add ANTHROPIC_API_KEY for AI scoring)"
        return result


def _call_gemini(prompt: str, response_json: bool = False, api_key: str = None) -> str:
    import requests
    key_to_use = api_key or get_fallback_gemini_key()
    if not key_to_use:
        raise ValueError("No Gemini API key configured. Provide it in profile settings or set GEMINI_API_KEY env.")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={key_to_use}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    if response_json:
        payload["generationConfig"] = {"responseMimeType": "application/json"}
    
    headers = {"Content-Type": "application/json"}
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    res_data = response.json()
    return res_data["candidates"][0]["content"]["parts"][0]["text"].strip()


def _gemini_score(title: str, company: str, description: str, resume_text: str = None, api_key: str = None,
                  liked_titles: list = None, disliked_titles: list = None,
                  w_up: float = 1.0, w_app: float = 1.0, w_down: float = -1.0, w_rej: float = -1.5) -> dict:
    if not resume_text:
        resume_text = RESUME_TEXT
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
        data = json.loads(raw)
        data["mode"] = "gemini"
        return data
    except Exception as e:
        print(f"  [Gemini score fallback] {e}")
        result = _local_score(title, company, description, liked_titles=liked_titles, disliked_titles=disliked_titles,
                             w_up=w_up, w_app=w_app, w_down=w_down, w_rej=w_rej, resume_text=resume_text)
        result["fit_summary"] += " (scored locally — Gemini API error)"
        return result


def score_job(title: str, company: str, description: str, resume_text: str = None, api_key: str = None, user_id: int = 1) -> dict:
    if not resume_text:
        resume_text = RESUME_TEXT

    liked_titles = []
    disliked_titles = []
    w_up, w_app, w_down, w_rej = 1.0, 1.0, -1.0, -1.5
    try:
        conn = get_conn(DB_PATH)
        row = conn.execute("SELECT weight_thumbs_up, weight_applied, weight_thumbs_down, weight_rejected FROM users WHERE id = ?", (user_id,)).fetchone()
        if row:
            w_up = row[0] if row[0] is not None else 1.0
            w_app = row[1] if row[1] is not None else 1.0
            w_down = row[2] if row[2] is not None else -1.0
            w_rej = row[3] if row[3] is not None else -1.5

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
        "docker", "kubernetes", "git", "github", "ci/cd", "devops"
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

    # Call underlying scoring engine
    if ANTHROPIC_API_KEY:
        res = _ai_score(title, company, description, resume_text)
    else:
        key_to_use = api_key or get_fallback_gemini_key()
        if key_to_use:
            res = _gemini_score(title, company, description, resume_text, api_key=key_to_use,
                                 liked_titles=liked_titles, disliked_titles=disliked_titles,
                                 w_up=w_up, w_app=w_app, w_down=w_down, w_rej=w_rej)
        else:
            res = _local_score(title, company, description, liked_titles=liked_titles, disliked_titles=disliked_titles,
                                w_up=w_up, w_app=w_app, w_down=w_down, w_rej=w_rej, resume_text=resume_text)

    # Attach matched and missing lists
    res["matched_skills"] = json.dumps(matched)
    res["missing_skills"] = json.dumps(missing)
    return res


# ── Cover letter ───────────────────────────────────────────────────────────

def _local_cover_letter(title: str, company: str, description: str,
                         contact_name: str, contact_title: str) -> dict:
    """Template cover letter — personalised from your profile, no API needed."""
    greeting = f"Hi {contact_name.split()[0]}," if contact_name and contact_name != "Hiring Team" else "Hi,"

    # Pull a relevant strength from the description
    domain_line = ""
    desc_lower = description.lower()
    if any(k in desc_lower for k in ["development", "web", "software", "engineering", "backend"]):
        domain_line = f"My experience building robust applications and backend systems aligns perfectly with this role."
    elif "data" in desc_lower or "analytics" in desc_lower:
        domain_line = f"Having designed and optimized data models and automated sync workflows, I am well-suited for your requirements."
    else:
        domain_line = f"With my background as a Software Engineer shipping scalable automation products, I can contribute immediately."

    achievement = PROFILE["achievements"][0]

    body = f"""{greeting}

{domain_line} {achievement}.

The {title} role at {company} is the kind of problem space I want to work in — high user impact, fast iteration, and a team that treats product as a first-class discipline. I noticed {description[:120].rstrip().rstrip('.') + '...' if description else 'the scope of this role'} and I think my background maps well.

A few things I'd bring on day one:
- Structured discovery process: I start with user problems, not solutions
- Strong cross-functional execution: I've shipped with engineering and design teams of 5–30 people
- Data-first mindset: I'm comfortable in SQL and Mixpanel, and I set metric targets before writing specs

I'd love 20 minutes to learn more about the team and share how I've tackled similar challenges. Happy to share specifics.

{PROFILE['name']}"""

    return {
        "subject": f"Application: {title} — {PROFILE['name']}",
        "body": body.strip(),
    }


def _ai_cover_letter(title: str, company: str, description: str,
                      contact_name: str, contact_title: str, resume_text: str = None) -> dict:
    """Claude-generated cover letter."""
    if not resume_text:
        resume_text = RESUME_TEXT
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
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
        return _local_cover_letter(title, company, description, contact_name, contact_title)


def _gemini_cover_letter(title: str, company: str, description: str,
                         contact_name: str, contact_title: str, resume_text: str = None, api_key: str = None) -> dict:
    if not resume_text:
        resume_text = RESUME_TEXT
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
        return _local_cover_letter(title, company, description, contact_name, contact_title)


def generate_cover_letter(title: str, company: str, description: str,
                           contact_name: str = "Hiring Team",
                           contact_title: str = "Recruiter",
                           resume_text: str = None, api_key: str = None) -> dict:
    if not resume_text:
        resume_text = RESUME_TEXT
    if ANTHROPIC_API_KEY:
        return _ai_cover_letter(title, company, description, contact_name, contact_title, resume_text)
    
    key_to_use = api_key or GEMINI_API_KEY
    if key_to_use:
        return _gemini_cover_letter(title, company, description, contact_name, contact_title, resume_text, api_key=key_to_use)
    return _local_cover_letter(title, company, description, contact_name, contact_title)


# ── LinkedIn note ──────────────────────────────────────────────────────────

def generate_linkedin_note(contact_name: str, contact_title: str,
                            company: str, job_title: str, api_key: str = None) -> str:
    first = contact_name.split()[0] if contact_name and contact_name != "Hiring Team" else "there"
    if ANTHROPIC_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
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


FULL_RESUME_MARKDOWN = """# Anand Bhattacharya
Bengaluru, India | +91-6364-123-572 | anandb9198@gmail.com | https://www.linkedin.com/in/anand-bhattacharya/

## PROFESSIONAL EXPERIENCE
**AI Associate Product Manager** <span style="float: right;">July 2025 – Present</span>
_Revrag.ai | Bengaluru, India_

- Designed and optimized multilingual (English, Hindi, Kannada) conversation flows for BFSI clients, reducing call errors by 20% and driving measurable improvement in end-user engagement across live deployments.
- Trained AI voice agents on 500+ real call recordings, cutting iteration cycles by 30% and accelerating client delivery timelines by 25%, equivalent to 2 weeks saved per project.
- Partnered with clients to co-define AI personas and deployment strategies, increasing adoption rates by 40% and improving user engagement across BFSI voice automation use cases.

**Data Science Intern** <span style="float: right;">March 2021 – April 2021</span>
_Kigyan Techno Solutions | Bengaluru, India_

- Built a Python regression module for retail sales forecasting, achieving 90% prediction accuracy and improving strategic planning reliability for the client.
- Applied Test-Driven Development (TDD) and paired programming practices, increasing forecasting model reliability by 15% against dynamic market conditions.

## ACADEMIC PROJECT EXPERIENCE
**ZoomWellness – AI Wellness Analytics Platform** <span style="float: right;">Jan 2025 – May 2025</span>
_Managing IT in the Analytics Age_

- Designed an AI engine integrating Zoom, calendar, and wearable data to detect employee burnout risk, targeting a 15% reduction in late-hour work indicators within year one.
- Defined an ML-based wellness nudge system leveraging behavioral and biometric signals, targeting 70% opt-in rate and weekly active usage within 6 months of launch.
- Built Explainable AI (XAI) framework with documented decision logic and consent controls, projected to boost retention by 5% and add 50+ enterprise clients within 18 months.

**Queue-less Lines** <span style="float: right;">Jan 2023 – May 2023</span>
_Entrepreneurial Experience_

- Led research into AT&T's 5G and video analytics technology to build a product innovation strategy targeting a 50% reduction in theme park wait times across high-traffic zones.
- Developed comprehensive product requirements and a go-to-market plan for Disney Theme Parks, projecting a 15% increase in ticket-linked profit from operational improvements.
- Established revenue models spanning budget tracking and merchandise, contributing to a projected $345M revenue increase across U.S. and emerging markets.

## SKILLS
- **AI Tools**: Retell, Deepgram, OpenAI TTS/STT, ElevenLabs, Replit, Cursor
- **PM & Agile**: Product Road mapping, Stakeholder Management, User Research, Agile, Jira, Go-to-Market Strategy
- **Data & Analytics**: MySQL, Tableau, Python, Java

## CERTIFICATIONS
- Certified Scrum Master (CSM) | AI for Product Management | IBM Generative AI: Prompt Engineering
- Machine Learning Foundations for Product Managers | Google Project Management Professional Certificate

## EDUCATION
**Dual Degree: MBA & MS in Information Technology Management** <span style="float: right;">May 2025</span>
_The University of Texas at Dallas | Richardson, Texas | GPA: 3.49/4.00_

**BE, Computer Science** <span style="float: right;">May 2021</span>
_Dayananda Sagar College of Engineering | Bengaluru, India | 7.45/10.00_
"""

def generate_tailored_resume(job_description: str, job_title: str, company: str, resume_text: str = None) -> str:
    """Generate a tailored resume based on the candidate profile and job description."""
    if not resume_text:
        resume_text = FULL_RESUME_MARKDOWN
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
        fallback_key = get_fallback_gemini_key()
        if fallback_key:
            return _call_gemini(prompt)
    except Exception as e:
        print(f"[AI Resume] Gemini failed: {e}")
        
    return resume_text


# ── Process all new jobs ───────────────────────────────────────────────────

def process_new_jobs(db_path: str = DB_PATH, min_score: float = 6.0, user_id: int = 1) -> list[dict]:
    """Score and generate cover letters for all 'new' jobs in the DB."""
    conn = get_conn(db_path)
    row = conn.execute("SELECT resume_text, gemini_api_key FROM users WHERE id = ?", (user_id,)).fetchone()
    resume_text = row[0] if row else None
    api_key = row[1] if row else None

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
                                       contact_name, contact_title, resume_text=resume_text, api_key=api_key)
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
        resume_text = RESUME_TEXT
    if ANTHROPIC_API_KEY:
        try:
            return _ai_interview_prep(title, company, description, resume_text)
        except Exception:
            pass
    
    key_to_use = api_key or GEMINI_API_KEY or get_fallback_gemini_key()
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
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""
        You are an elite interview coach preparing a candidate for a {title} role at {company}.
        
        Job Description:
        {description}
        
        Candidate Resume:
        {resume_text}
        
        Generate:
        1. 5 warm-up 'Quick-fire' questions. For each question, draft a brief, personalized model answer draft (2-3 sentences) tailored to the candidate's actual experience/credentials in their resume.
        2. 5 to 8 'Deeper prep' questions tailored specifically to the gaps/matches between the resume and the job description, along with bulleted model answer outlines/hints.
        
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
    
    Generate:
    1. 5 warm-up 'Quick-fire' questions. For each question, draft a brief, personalized model answer draft (2-3 sentences) tailored to the candidate's actual experience/credentials in their resume.
    2. 5 to 8 'Deeper prep' questions tailored specifically to the gaps/matches between the resume and the job description, along with bulleted model answer outlines/hints.
    
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
