"""Mock interviews: Maya (voice_agent/) interviews the user for one job on their board, built from the
job posting and their resume, and each answer is scored afterwards from the transcript.

The browser joins a LiveKit room, records the call on the device so it can be replayed, and posts
the timed transcript to /api/calls/interview/end. A report is written in the background: an overall
score, and for every main question a 1-5 score, what worked, what to sharpen and where in the
recording the answer starts.
"""
import json
import logging
import re

log = logging.getLogger("interview")

LENGTHS = {"short": 4, "full": 7}
MAX_POSTING = 2500
MAX_REPORT_QUESTIONS = 10


def interview_brief(job: dict, *, length: str, resume: str, name: str, lang: str) -> dict:
    """The job metadata for the agent."""
    return {
        "mode": "interview",
        "lang": lang,
        "candidate_name": " ".join((name or "").split())[:60],
        "role": " ".join((job.get("title") or "").split())[:120],
        "company": " ".join((job.get("company") or "").split())[:80],
        "posting": " ".join((job.get("description") or "").split())[:MAX_POSTING],
        "key_reqs": " ".join((job.get("key_reqs") or "").split())[:600],
        "resume": (resume or "")[:1500],
        "questions": LENGTHS.get(length, LENGTHS["short"]),
    }


def clean_timed_transcript(items, limit_chars: int = 40000) -> list[dict]:
    """[{role: agent|user, text, t}] where t is seconds from the start of the call."""
    out, total = [], 0
    if not isinstance(items, list):
        return out
    for it in items[:600]:
        if not isinstance(it, dict):
            continue
        text = " ".join(str(it.get("text") or "").split())[:2000]
        if not text:
            continue
        try:
            t = max(0.0, min(float(it.get("t") or 0), 24 * 3600))
        except (TypeError, ValueError):
            t = 0.0
        total += len(text)
        if total > limit_chars:
            break
        out.append({"role": "agent" if it.get("role") == "agent" else "user", "text": text, "t": round(t, 1)})
    return out


def _numbered(transcript: list[dict]) -> str:
    return "\n".join(f"[{i}] {'Interviewer' if t['role'] == 'agent' else 'Candidate'}: {t['text']}"
                     for i, t in enumerate(transcript))


def scoring_prompt(transcript: list[dict], role: str, company: str, posting: str) -> str:
    return (
        f"You are a hiring manager reviewing a mock interview for {role or 'a role'}"
        f"{' at ' + company if company else ''}.\n"
        + (f"Job posting (excerpt): {posting[:1500]}\n" if posting else "")
        + "Below is the transcript; each line has a number in brackets.\n\n"
        f"{_numbered(transcript)[:24000]}\n\n"
        "Score each MAIN interview question the candidate answered (skip greetings, small talk and the "
        "candidate's own questions; fold a follow-up into the question it follows). Be fair and specific: "
        "quote or paraphrase what they actually said, and judge against what this role needs.\n"
        "Reply with JSON only, in this shape:\n"
        '{"overall": 1-5, "summary": "two sentences: overall impression and the single biggest thing to work on",'
        ' "questions": [{"question": "the question as asked, shortened", "line": number of the interviewer line'
        ' that asked it, "score": 1-5, "worked": "one sentence", "sharpen": "one sentence",'
        ' "stronger": "one or two sentences a strong candidate might have said"}]}'
    )


def _json_from(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON in reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("reply isn't an object")
    return data


def _score(value) -> int | None:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(1, min(5, n))


def _line(s, limit=400) -> str:
    return " ".join(str(s or "").split())[:limit]


def parse_report(text: str, transcript: list[dict]) -> dict:
    """The model's JSON, checked and tied back to the transcript times."""
    data = _json_from(text)
    questions = []
    for q in (data.get("questions") or [])[:MAX_REPORT_QUESTIONS]:
        if not isinstance(q, dict) or not _line(q.get("question")):
            continue
        score = _score(q.get("score"))
        if score is None:
            continue
        try:
            line = int(q.get("line"))
        except (TypeError, ValueError):
            line = -1
        # Where the answer starts: the first candidate line after the question
        t = None
        if 0 <= line < len(transcript):
            t = transcript[line]["t"]
            for item in transcript[line + 1:]:
                if item["role"] == "user":
                    t = item["t"]
                    break
        questions.append({"question": _line(q.get("question"), 300), "score": score, "t": t,
                          "worked": _line(q.get("worked")), "sharpen": _line(q.get("sharpen")),
                          "stronger": _line(q.get("stronger"), 600)})
    overall = _score(data.get("overall"))
    if overall is None and questions:
        overall = round(sum(q["score"] for q in questions) / len(questions))
    if not questions or overall is None:
        raise ValueError("no scored questions")
    return {"overall": overall, "summary": _line(data.get("summary"), 600), "questions": questions, "source": "ai"}


def score_interview(transcript: list[dict], role: str, company: str, posting: str, api_key: str | None) -> dict:
    """Scores the interview with Gemini, then LiveKit's LLM. Returns a report, or {"error": ...}."""
    if sum(1 for t in transcript if t["role"] == "user") < 2:
        return {"error": "There weren't enough answers to score. Try a longer interview."}
    prompt = scoring_prompt(transcript, role, company, posting)
    attempts = []
    try:
        from ai_engine import _call_gemini, get_fallback_gemini_key
        if api_key or get_fallback_gemini_key():
            attempts.append(lambda: _call_gemini(prompt, response_json=True, api_key=api_key))
    except Exception:
        pass
    try:
        import livekit_llm
        if livekit_llm.is_configured():
            attempts.append(lambda: livekit_llm.complete(prompt, temperature=0.3, max_tokens=2500))
    except Exception:
        pass
    for attempt in attempts:
        try:
            return parse_report(attempt(), transcript)
        except Exception as e:  # no key, quota, timeout, bad JSON: try the next one
            log.info("interview scoring attempt failed: %s", e)
    return {"error": "Couldn't score this one. Add a Gemini key in Settings, or try again later."}


def summary_line(report: dict, role: str) -> str:
    what = f"Mock interview for {role}" if role else "Mock interview"
    if report.get("error"):
        return f"{what}. {report['error']}"
    n = len(report["questions"])
    return f"{what}: {report['overall']}/5 across {n} question{'s' if n != 1 else ''}. {report.get('summary') or ''}".strip()
