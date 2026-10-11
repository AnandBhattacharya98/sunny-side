"""Facts read out of a job's own text: whether it's remote, and the pay if it's stated.

Used for the board's filters and the salary chip on cards. Nothing here calls the
network or the AI, so it runs on every page load."""
import re

_REMOTE = re.compile(r"\b(remote|work from home|wfh|work from anywhere|distributed team)\b", re.I)
_NOT_REMOTE = re.compile(r"\b(no remote|not remote|non-remote|remote not available|on-?site only)\b", re.I)

_NUM = r"\d{1,3}(?:[,.]\d{2,3})*(?:\.\d+)?"
_SALARY_PATTERNS = [
    # ₹12 - 18 LPA, 12-18 LPA, INR 20 LPA, 25 lakhs per annum
    re.compile(rf"(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:-|–|to)\s*(?:₹|rs\.?|inr)?\s*{_NUM}\s*(?:lpa|lakhs?|lacs?|l\b)(?:\s*(?:per annum|p\.?a\.?|/yr))?", re.I),
    re.compile(rf"(?:₹|rs\.?|inr)\s*{_NUM}\s*(?:lpa|lakhs?|lacs?|cr|crores?)\b", re.I),
    re.compile(rf"\b{_NUM}\s*(?:lpa)\b", re.I),
    # $90k - $120k, $120,000–$150,000, USD 80,000 - 100,000, €60k
    re.compile(rf"(?:[$€£]|usd|eur|gbp)\s*{_NUM}\s*k?\s*(?:-|–|to)\s*(?:[$€£]|usd|eur|gbp)?\s*{_NUM}\s*k?", re.I),
    re.compile(rf"(?:₹|rs\.?|inr)\s*{_NUM}\s*(?:-|–|to)\s*(?:₹|rs\.?|inr)?\s*{_NUM}", re.I),
    re.compile(rf"[$€£]\s*{_NUM}\s*k\b", re.I),
]
# Pay words must sit near the number, so "5+ years" or "$2B raised" aren't read as salary.
_PAY_CONTEXT = re.compile(r"salary|ctc|compensation|pay|package|lpa|lakh|per annum|base|stipend|₹|inr|usd|\$|€|£", re.I)


def is_remote(location: str = "", title: str = "", description: str = "") -> bool:
    head = f"{location or ''} {title or ''} {(description or '')[:600]}"
    return bool(_REMOTE.search(head)) and not _NOT_REMOTE.search(head)


def extract_salary(text: str) -> str:
    text = text or ""
    for pat in _SALARY_PATTERNS:
        for m in pat.finditer(text):
            window = text[max(0, m.start() - 40):m.end() + 10]
            found = re.sub(r"\s+", " ", m.group(0)).strip(" -–")
            if not re.search(r"\d", found):
                continue
            if _PAY_CONTEXT.search(window):
                return found[:40]
    return ""


def annotate(job: dict) -> dict:
    """Adds is_remote and salary_text to a job dict in place."""
    job["is_remote"] = is_remote(job.get("location"), job.get("title"), job.get("description"))
    job["salary_text"] = extract_salary(f"{job.get('title') or ''}\n{job.get('description') or ''}")
    return job
