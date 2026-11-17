"""How a resume stacks up against one job posting: the match, what's covered and what's missing.

Used by the Career Switch job details panel. Gemini writes the comparison when a key
is set; otherwise (or if it fails) a rule-based comparison built from the same skills
lexicon the board's scorer uses fills in. Every missing skill links to the learning page."""
import hashlib
import json
import re
import threading
import time
from urllib.parse import quote

from ai_engine import _call_gemini, find_job_skills, skill_label, YEARS_PATTERN

# Text after one of these headings describes optional skills.
_NICE_HEADING = re.compile(r"(nice to have|nice-to-have|good to have|preferred|bonus|plus points|is a plus|would be a plus|desirable)", re.I)
_LEVELS = [
    ("intern", re.compile(r"\b(intern|internship|trainee|apprentice)\b", re.I)),
    ("lead", re.compile(r"\b(lead|principal|staff|head|director|vp|vice president|chief)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|iii|experienced)\b", re.I)),
    ("entry", re.compile(r"\b(junior|jr\.?|entry|graduate|fresher|associate|apprentice|i)\b", re.I)),
]
_TYPES = [
    ("internship", re.compile(r"\b(internship|intern)\b", re.I)),
    ("contract", re.compile(r"\b(contract|contractor|freelance|temporary|fixed[- ]term)\b", re.I)),
    ("part-time", re.compile(r"\bpart[- ]time\b", re.I)),
    ("full-time", re.compile(r"\bfull[- ]time\b|\bpermanent\b", re.I)),
]


def learn_link(skill: str) -> str:
    """Where "Start learning" goes for one skill (the learning page reads ?skill=)."""
    return f"/learn?skill={quote(skill)}&from=career-switch"


def job_level(title: str, criteria: dict | None = None) -> str:
    """intern, entry, mid, senior or lead, from LinkedIn's seniority field or the title."""
    seniority = ((criteria or {}).get("Seniority level") or "").lower()
    if seniority:
        if "intern" in seniority:
            return "intern"
        if "entry" in seniority:
            return "entry"
        if "director" in seniority or "executive" in seniority:
            return "lead"
        if "mid-senior" in seniority:
            return "senior"
        if "associate" in seniority:
            return "mid"
    for name, pat in _LEVELS:
        if pat.search(title or ""):
            return name
    return "mid"


def job_type(title: str, description: str = "", criteria: dict | None = None) -> str:
    employment = ((criteria or {}).get("Employment type") or "").lower()
    text = f"{employment}\n{title or ''}\n{(description or '')[:1500]}"
    for name, pat in _TYPES:
        if pat.search(text):
            return name
    return "full-time"


def required_years(description: str) -> int | None:
    found = [int(m.group(1)) for m in YEARS_PATTERN.finditer(description or "") if 0 < int(m.group(1)) <= 25]
    return min(found) if found else None


def _user_skills(resume_text: str, profile: dict) -> set:
    return {s.lower().strip() for key in ("skills", "tools", "domains") for s in (profile or {}).get(key) or [] if isinstance(s, str)}


def _has_skill(skill: str, resume_lower: str, user_skills: set) -> bool:
    if skill in user_skills:
        return True
    if any(skill in s or s in skill for s in user_skills if len(s) > 3 and len(skill) > 3):
        return True
    return bool(re.search(rf"\b{re.escape(skill)}\b", resume_lower))


def _is_nice(skill: str, description: str) -> bool:
    """True when the skill is first named after a "nice to have" style heading."""
    text = (description or "").lower()
    m = re.search(rf"\b{re.escape(skill)}\b", text)
    if not m:
        return False
    nice = _NICE_HEADING.search(text)
    if nice and nice.start() < m.start():
        return True
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    return bool(_NICE_HEADING.search(text[line_start:line_end if line_end != -1 else None]))


def verdict(score: int) -> str:
    if score >= 75:
        return "Strong match"
    if score >= 55:
        return "Good match"
    if score >= 35:
        return "Stretch"
    return "Big leap"


def local_compare(title: str, company: str, description: str, resume_text: str, profile: dict | None = None) -> dict:
    profile = profile or {}
    resume_lower = (resume_text or "").lower()
    user_skills = _user_skills(resume_text, profile)
    job_skills = find_job_skills(title, description)
    matched, missing = [], []
    for s in sorted(job_skills):
        if _has_skill(s, resume_lower, user_skills):
            matched.append(skill_label(s))
        else:
            nice = _is_nice(s, description)
            missing.append({"skill": skill_label(s), "importance": "nice" if nice else "must",
                            "why": f"The posting {'lists' if not nice else 'would like'} {skill_label(s)}"
                                   f"{'' if not nice else ' as a bonus'}, and it isn't on your resume yet."})
    missing.sort(key=lambda m: m["importance"] != "must")

    try:
        yours = float(profile.get("years_experience") or 0) or None
    except (TypeError, ValueError):
        yours = None
    if yours is None and resume_text:
        from resume_parser import parse_resume_local
        yours = parse_resume_local(resume_text).get("years_experience") or None
    need = required_years(description)

    if job_skills:
        must = [m for m in missing if m["importance"] == "must"]
        weight = len(matched) + len(must) + 0.4 * (len(missing) - len(must))
        score = 100 * len(matched) / weight if weight else 50
    else:
        score = 50
    if need and yours is not None:
        if need > yours + 2:
            score -= 15
        elif need > yours:
            score -= 6
    score = int(max(5, min(97, round(score))))

    summary = []
    if not resume_text:
        summary.append("Add your resume in Settings to see how you compare.")
    elif job_skills:
        summary.append(f"You already cover {len(matched)} of the {len(job_skills)} skills this posting names.")
    else:
        summary.append("This posting doesn't name many specific skills, so the match is a rough guess.")
    if need and yours is not None:
        summary.append(f"It asks for {need}+ years and your resume shows about {yours:g}.")

    tips = []
    if matched:
        tips.append(f"Lead your resume summary with {', '.join(matched[:3])}, since this role asks for them.")
    must = [m["skill"] for m in missing if m["importance"] == "must"]
    if must:
        tips.append(f"Add a small project that shows {must[0]}, so the gap reads as \"learning\" rather than \"missing\".")
    if need and yours is not None and need > yours:
        tips.append("Mention related work from side projects or past roles to bridge the experience ask.")

    return {
        "score": score, "verdict": verdict(score), "summary": " ".join(summary),
        "matched": matched, "missing": missing, "strengths": [], "tips": tips[:3],
        "experience": {"required": need, "yours": yours},
        "mode": "local",
    }


def _clean_ai(data, base: dict) -> dict | None:
    if not isinstance(data, dict):
        return None
    try:
        score = int(round(float(data.get("score"))))
    except (TypeError, ValueError):
        return None
    score = max(0, min(100, score))

    def strs(v, n, size=160):
        return [str(x).strip()[:size] for x in (v or []) if isinstance(x, (str, int, float)) and str(x).strip()][:n] \
            if isinstance(v, list) else []

    missing = []
    for m in data.get("missing") or []:
        if isinstance(m, dict) and str(m.get("skill") or "").strip():
            missing.append({"skill": str(m["skill"]).strip()[:60],
                            "importance": "nice" if str(m.get("importance", "")).lower().startswith("nice") else "must",
                            "why": str(m.get("why") or "").strip()[:220]})
        elif isinstance(m, str) and m.strip():
            missing.append({"skill": m.strip()[:60], "importance": "must", "why": ""})
    missing = missing[:10]
    missing.sort(key=lambda m: m["importance"] != "must")
    return {
        "score": score, "verdict": verdict(score),
        "summary": str(data.get("summary") or "").strip()[:600] or base["summary"],
        "matched": [s[:60] for s in strs(data.get("matched"), 14, 60)],
        "missing": missing,
        "strengths": strs(data.get("strengths"), 4),
        "tips": strs(data.get("tips"), 4, 220) or base["tips"],
        "experience": base["experience"],
        "mode": "ai",
    }


def ai_compare(title: str, company: str, description: str, resume_text: str, api_key: str, base: dict) -> dict | None:
    prompt = f"""You are a frank career coach. Compare this candidate's resume against the job posting.
Reply ONLY with JSON, no markdown.

RESUME:
{(resume_text or '')[:6000]}

JOB: {title} at {company}
POSTING:
{(description or '')[:5000]}

Return exactly:
{{"score": <0-100 integer, how ready they are to apply today>,
  "summary": "<2 sentences, second person, plain words>",
  "matched": ["<skill or requirement from the posting the resume shows>", ...],
  "missing": [{{"skill": "<short learnable skill name, 1-4 words>", "importance": "must" | "nice", "why": "<one sentence on why the job needs it>"}}, ...],
  "strengths": ["<a specific strength from the resume that helps for this job>", ...],
  "tips": ["<one concrete action to close the gap or position the resume>", ...]}}
Use at most 12 matched, 8 missing, 3 strengths, 3 tips. Missing skills must be things a course could teach."""
    try:
        return _clean_ai(json.loads(_call_gemini(prompt, response_json=True, api_key=api_key)), base)
    except Exception as e:
        print(f"  [job compare fallback] {e}")
        return None


_cache: dict = {}
_lock = threading.Lock()
_TTL = 3600


def compare(user_id: int, key: str, title: str, company: str, description: str, resume_text: str,
            profile: dict | None = None, api_key: str = "") -> dict:
    """The comparison for one posting, cached per user, posting and resume for an hour."""
    resume_hash = hashlib.sha1((resume_text or "").encode()).hexdigest()[:12]
    ck = (user_id, key, resume_hash)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(ck)
        if hit and now - hit[0] < _TTL:
            return hit[1]
    base = local_compare(title, company, description, resume_text, profile)
    result = None
    if api_key and resume_text and len(description or "") > 200:
        result = ai_compare(title, company, description, resume_text, api_key, base)
    result = result or base
    for m in result["missing"]:
        m["learn_url"] = learn_link(m["skill"])
    with _lock:
        if len(_cache) > 500:
            for k in [k for k, (t, _) in _cache.items() if now - t > _TTL] or list(_cache)[:100]:
                _cache.pop(k, None)
        _cache[ck] = (now, result)
    return result
