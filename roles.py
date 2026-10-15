"""
roles.py — The roles a user's board is built around.

The job title a user typed in wins. Without one, the roles come from their own
resume: the titles they have held, or failing that, what their skills point to.
Nothing here falls back to a fixed role, so every board follows its owner's resume.
"""

import json
import re

MAX_ROLES = 3

# Words that make a line read as a job title ("Data Analyst", "Staff Nurse", "Head of Sales").
ROLE_NOUNS = (
    "engineer", "developer", "programmer", "architect", "analyst", "scientist", "researcher",
    "manager", "director", "officer", "executive", "president", "vp",
    "designer", "consultant", "accountant", "auditor", "administrator", "specialist",
    "coordinator", "associate", "assistant", "intern", "trainee", "apprentice",
    "nurse", "doctor", "physician", "pharmacist", "therapist", "teacher", "lecturer", "professor",
    "tutor", "recruiter", "writer", "editor", "copywriter", "journalist", "marketer", "strategist",
    "technician", "tester", "lawyer", "advocate", "counsel", "paralegal", "owner", "representative",
    "agent", "advisor", "adviser", "planner", "buyer", "merchandiser", "chef", "pilot", "founder",
    "co-founder", "partner", "supervisor", "operator", "photographer", "animator", "illustrator",
    "pm", "apm", "sde", "swe",
)
# "Lead" and "Head" only count as "Head of X" or at the end ("Tech Lead"), never "Lead Generation".
_ROLE_NOUN_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in ROLE_NOUNS) + r")\b|\bhead of\b|\blead$", re.IGNORECASE)

# Headings and phrases that contain a role noun but are not a title.
_NOT_A_TITLE = re.compile(
    r"\b(experience|education|skills|summary|objective|references|projects|certifications|responsibilities|"
    r"achievements|contact|profile|languages|hobbies|interests|email|phone|linkedin|github)\b|\S+@\S+\.\S+|https?://|\d{3,}[\s-]?\d{3,}",
    re.IGNORECASE)
# Where a title line usually goes on to name the company or the dates.
_SPLIT = re.compile(r"\s+(?:at|@)\s+|\s*[|•·,;:(]\s*|\s+[–—-]\s+")
_DATES = re.compile(r"\b(?:19|20)\d{2}\b.*$|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d.*$|\bpresent\b.*$",
                    re.IGNORECASE)
# Seniority words dropped before searching, so "Senior Data Analyst" finds every data analyst role.
_SENIORITY_WORD = re.compile(r"senior|sr\.?|junior|jr\.?|lead|principal|associate|trainee|i|ii|iii", re.IGNORECASE)
_EARLY_CAREER = re.compile(r"\b(intern|internship|trainee|apprentice)\b", re.IGNORECASE)
_ABBREVIATIONS = {"pm": "Product Manager", "apm": "Product Manager", "sde": "Software Engineer",
                  "swe": "Software Engineer"}

# For a resume that names no title at all (a fresher, say): what the skills point to.
# Order matters, the first matches win.
_SKILL_ROLES = [
    (r"\b(product management|product roadmap\w*|product owner|go-to-market)\b", "Product Manager"),
    (r"\b(machine learning|deep learning|data science|nlp|computer vision)\b", "Data Scientist"),
    (r"\b(react|angular|vue|node\.?js|django|flask|spring|java|javascript|typescript|golang|c\+\+|c#|"
     r"software development|backend|frontend|full[- ]stack)\b", "Software Engineer"),
    (r"\b(devops|kubernetes|docker|terraform|ci/cd|site reliability)\b", "DevOps Engineer"),
    (r"\b(sql|tableau|power bi|looker|data analysis|data analytics|excel dashboards?)\b", "Data Analyst"),
    (r"\b(figma|sketch|wireframing|user experience|ux|ui design|adobe xd)\b", "UX Designer"),
    (r"\b(seo|sem|content marketing|performance marketing|social media marketing|digital marketing)\b", "Digital Marketing"),
    (r"\b(financial modeling|financial analysis|valuation|accounting|tally|gst|taxation|audit)\b", "Financial Analyst"),
    (r"\b(recruiting|recruitment|talent acquisition|sourcing candidates)\b", "Recruiter"),
    (r"\b(lead generation|b2b sales|inside sales|business development|account management)\b", "Business Development"),
    (r"\b(supply chain|logistics|procurement|inventory|warehouse)\b", "Supply Chain"),
    (r"\b(customer success|customer support|client servicing)\b", "Customer Success"),
    (r"\b(nursing|patient care|clinical)\b", "Nurse"),
    (r"\b(teaching|curriculum|lesson plan\w*)\b", "Teacher"),
    (r"\b(python)\b", "Software Engineer"),
]


def clean_title(line: str) -> str:
    """'Senior Software Engineer at Google | 2021 - Present' -> 'Senior Software Engineer'.
    Returns '' when the line doesn't read as a job title."""
    line = (line or "").strip().lstrip("-*•·>").strip()
    if not line or len(line) > 90 or _NOT_A_TITLE.search(line):
        return ""
    for part in _SPLIT.split(line):
        part = _DATES.sub("", part).strip(" .-–—")
        words = part.split()
        if not words or len(words) > 6 or not _ROLE_NOUN_RE.search(part):
            continue
        # A sentence ("Worked closely with the product manager") is not a title.
        if words[0].lower().endswith("ed") or part.endswith("."):
            continue
        if any(w.lower() in ("with", "the", "and", "for", "to", "my", "our", "i") for w in words):
            continue
        return part
    return ""


def extract_titles(raw_text: str, limit: int = 5) -> list:
    """Job titles a resume names, in the order it names them (usually most recent first)."""
    titles = []
    seen = set()
    for line in (raw_text or "").splitlines():
        title = clean_title(line)
        key = title.lower()
        if title and key not in seen:
            seen.add(key)
            titles.append(title)
            if len(titles) >= limit:
                break
    return titles


def search_phrase(title: str) -> str:
    """The phrase to search job boards with for a title: no seniority, no company, no dates."""
    title = (title or "").strip()
    if title.lower() in _ABBREVIATIONS:
        return _ABBREVIATIONS[title.lower()]
    parts = [_DATES.sub("", p).strip(" .-–—") for p in _SPLIT.split(title)]
    title = next((p for p in parts if _ROLE_NOUN_RE.search(p)), parts[0] if parts else "")
    words = title.split()
    kept = [w for w in words if not _SENIORITY_WORD.fullmatch(w)]
    if len(kept) >= 2 or (kept and len(words) == 1):
        words = kept
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words)


def roles_from_skills(text: str) -> list:
    """Roles the skills point to, the best supported first."""
    lower = (text or "").lower()
    hits = {}
    for i, (pattern, role) in enumerate(_SKILL_ROLES):
        n = len(re.findall(pattern, lower))
        if n:
            count, first = hits.get(role, (0, i))
            hits[role] = (count + n, min(first, i))
    return [r for r, _ in sorted(hits.items(), key=lambda kv: (-kv[1][0], kv[1][1]))]


def roles_for(designation: str = "", profile: dict | None = None, resume_text: str = "",
              limit: int = MAX_ROLES) -> list:
    """Up to `limit` search phrases for a user, most important first."""
    profile = profile or {}
    candidates = []
    if designation and designation.strip():
        candidates.append(designation.strip())
    candidates += [t for t in (profile.get("titles") or []) if isinstance(t, str)]
    candidates += extract_titles(resume_text)
    # Past internships only count when they are all the resume has
    if any(not _EARLY_CAREER.search(c) for c in candidates):
        candidates = [c for c in candidates if not _EARLY_CAREER.search(c)]
    if not candidates:
        skills_text = " ".join(str(s) for k in ("skills", "tools", "domains") for s in (profile.get(k) or []))
        candidates += roles_from_skills(skills_text + "\n" + (resume_text or ""))

    roles, seen = [], set()
    for c in candidates:
        phrase = search_phrase(c)
        key = " ".join(sorted(re.findall(r"[a-z0-9+#]+", phrase.lower())))
        if phrase and key and key not in seen:
            seen.add(key)
            roles.append(phrase)
        if len(roles) >= limit:
            break
    return roles


def roles_for_user(conn, user_id: int, limit: int = MAX_ROLES) -> list:
    row = conn.execute("SELECT designation, resume_profile_json, resume_text FROM users WHERE id = ?",
                       (user_id,)).fetchone()
    if not row:
        return []
    try:
        profile = json.loads(row[1]) if row[1] else {}
    except (TypeError, ValueError):
        profile = {}
    return roles_for(row[0] or "", profile if isinstance(profile, dict) else {}, row[2] or "", limit=limit)
