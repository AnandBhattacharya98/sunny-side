"""
Interactive interview practice: grades a spoken or typed answer to one prep question.

Uses Gemini when a key is available and falls back to simple local checks (length, a
STAR-shaped story, concrete numbers), so practice still works without any API key.
Everything returned is plain text the page escapes before showing.
"""
import json
import re

import voice_engine as ve

MAX_QUESTION_CHARS = 500
MAX_HINT_CHARS = 1500
MAX_ANSWER_CHARS = 3000
MAX_RESUME_CHARS = 6000


def _clean(text, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def grade_answer(question: str, hints: str, answer: str, title: str = "", company: str = "",
                 api_key: str = None, lang: str = "en", resume: str = "") -> dict:
    """Returns {"score": 1-5, "verdict", "strengths": [...], "improve": [...], "better_answer",
    "resume_example", "spoken", "source"}. `spoken` is what Sunny says aloud: a short,
    conversational walk-through of the feedback that ends by asking what to do next."""
    question = _clean(question, MAX_QUESTION_CHARS)
    hints = _clean(hints, MAX_HINT_CHARS)
    answer = _clean(answer, MAX_ANSWER_CHARS)
    resume = str(resume or "").strip()[:MAX_RESUME_CHARS]
    lang = lang if lang in ve.LANGS else "en"
    if api_key and answer:
        try:
            return _gemini_grade(question, hints, answer, title, company, api_key, lang, resume)
        except (ve.GeminiError, ValueError, KeyError, TypeError) as e:
            print(f"Quiz feedback via Gemini failed, using local checks: {e}")
    return local_grade(question, hints, answer, company, lang, resume)


def _gemini_grade(question, hints, answer, title, company, api_key, lang, resume="") -> dict:
    language = "Hindi (Devanagari script)" if lang == "hi" else ve.LANG_NAMES.get(lang, "English")
    resume_block = (f"CANDIDATE RESUME (their real experience; the only source for examples): {json.dumps(resume)}"
                    if resume else "CANDIDATE RESUME: not available")
    prompt = f"""You are Sunny, a warm, honest interview coach talking with a candidate who is practising out loud for a {title or 'job'} interview at {company or 'a company'}.
Grade their answer to the question below on a 1-5 scale (1 = off-topic or empty, 3 = decent but generic, 5 = specific, structured, with measurable results).
Be encouraging but concrete and detailed, like a good mentor in a one-on-one. Write everything in {language}, speaking to the candidate as "you".

QUESTION: {json.dumps(question)}
COACH NOTES (what a strong answer covers): {json.dumps(hints)}
CANDIDATE ANSWER (may be a speech transcript with filler words): {json.dumps(answer)}
{resume_block}

Pick the ONE experience from their resume that best answers this question (if the resume has one), and use it.
Never invent employers, numbers or projects that are not in their answer or resume.

Return only JSON:
{{"score": 1-5,
  "verdict": "one friendly sentence summing up how it went",
  "strengths": ["2-3 specific points about what worked, each one full sentence quoting or naming what they said"],
  "improve": ["2-3 specific, actionable changes, each one or two sentences saying what to add and why it matters to the interviewer"],
  "resume_example": "1-2 sentences naming the resume experience to use and why it fits, e.g. 'Your work leading X at Y is a great fit because ...' (empty string if no resume)",
  "better_answer": "a 5-8 sentence model answer in first person, structured as situation, task, action, result, built from their answer plus that resume experience",
  "spoken": "what you say out loud right after they finish: 4-6 natural, conversational sentences. React to their answer, name one thing that worked, explain the most important improvement, and walk them through how they could use the resume experience (mention it by name). No lists, no headings, no score numbers. End by asking whether they want to try it again with that story or move on to the next question."}}"""
    res = ve.gemini_generate(api_key, [{"text": prompt}],
                             generation_config={"responseMimeType": "application/json", "temperature": 0.3},
                             timeout=20)
    text = ve._first_part(res).get("text", "")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    data = json.loads(text)
    score = int(round(float(data.get("score", 3))))
    return {
        "score": max(1, min(5, score)),
        "verdict": _clean(data.get("verdict"), 200),
        "strengths": [_clean(s, 200) for s in (data.get("strengths") or [])[:2] if _clean(s, 200)],
        "improve": [_clean(s, 200) for s in (data.get("improve") or [])[:3] if _clean(s, 200)],
        "better_answer": _clean(data.get("better_answer"), 1500),
        "resume_example": _clean(data.get("resume_example"), 400),
        "spoken": _clean(data.get("spoken"), 1200) or _spoken_from(data, lang),
        "source": "ai",
    }


def _spoken_from(data: dict, lang: str) -> str:
    """A conversational spoken version when the model left `spoken` out."""
    parts = [_clean(data.get("verdict"), 200)]
    strengths = [s for s in (data.get("strengths") or []) if s]
    improve = [s for s in (data.get("improve") or []) if s]
    if strengths:
        parts.append(_t(lang, "What worked: ", "जो अच्छा रहा: ") + _clean(strengths[0], 200))
    if improve:
        parts.append(_t(lang, "The one thing I'd change: ", "एक चीज़ जो मैं बदलूँगी: ") + _clean(improve[0], 240))
    if data.get("resume_example"):
        parts.append(_clean(data.get("resume_example"), 300))
    parts.append(_t(lang, "Want to have another go with that, or shall we move on?", "क्या इसे फिर से आज़माना चाहेंगे, या आगे बढ़ें?"))
    return " ".join(p.strip() for p in parts if p and p.strip())


_ACTION_WORDS = ("i led", "i built", "i drove", "i ran", "i owned", "i designed", "i launched", "i created", "i decided",
                 "i worked", "i managed", "i analysed", "i analyzed", "i set up", "i wrote", "i convinced", "i proposed",
                 "मैंने")
_RESULT_WORDS = ("result", "increase", "increased", "reduced", "improved", "grew", "saved", "impact", "outcome", "launched",
                 "conversion", "revenue", "retention", "नतीजा", "बढ़", "कम किया", "सुधार")
_CONTEXT_WORDS = ("when i", "at my", "in my last", "in my previous", "our team", "the project", "we had", "situation",
                  "जब मैं", "मेरी पिछली", "प्रोजेक्ट")


def _t(lang, en, hi):
    return hi if lang == "hi" else en


_RESUME_VERBS = re.compile(r"\b(led|built|launched|drove|grew|increased|reduced|owned|designed|shipped|managed|created|improved|"
                           r"delivered|scaled|ran|started|cut|saved)\b", re.I)


def resume_highlight(resume: str, question: str = "") -> str:
    """The resume line most worth telling as a story: an action with a result, ideally on topic."""
    lines = [re.sub(r"^[\s•\-–*·●▪]+", "", l).strip() for l in re.split(r"[\n\r]+|(?<=[.!?])\s+(?=[A-Z])", resume or "")]
    lines = [l for l in lines if 30 <= len(l) <= 260]
    if not lines:
        return ""
    q_words = {w for w in re.findall(r"[a-z]{4,}", (question or "").lower())}

    def score(line):
        low = line.lower()
        return ((2 if re.search(r"\d", line) else 0) + (2 if _RESUME_VERBS.search(line) else 0)
                + sum(1 for w in q_words if w in low))
    best = max(lines, key=score)
    return best if score(best) >= 2 else ""


def local_grade(question: str, hints: str, answer: str, company: str = "", lang: str = "en", resume: str = "") -> dict:
    a = (answer or "").lower()
    words = len(a.split())
    has_numbers = bool(re.search(r"\d|percent|lakh|crore|million|प्रतिशत|लाख|करोड़", a))
    has_action = any(w in a for w in _ACTION_WORDS) or len(re.findall(r"\bi\b", a)) >= 3
    has_result = any(w in a for w in _RESULT_WORDS)
    has_context = any(w in a for w in _CONTEXT_WORDS)
    mentions_company = bool(company) and company.lower() in a

    strengths, improve = [], []
    if not words:
        empty = _t(lang, "I didn't get an answer that time. Give it a go, even a rough one helps!",
                   "इस बार कोई जवाब नहीं मिला। कोशिश कीजिए, कच्चा जवाब भी मदद करता है!")
        return {"score": 1, "verdict": empty, "strengths": [], "improve": [], "better_answer": "",
                "resume_example": "", "spoken": empty, "source": "local"}

    score = 1
    if words >= 25:
        score += 1
    else:
        improve.append(_t(lang, "Say a bit more. Aim for 45 to 90 seconds when you speak it.",
                          "थोड़ा और बताइए। बोलते समय 45 से 90 सेकंड का लक्ष्य रखें।"))
    if words >= 60:
        score += 0.5
    if has_context:
        score += 0.5
        strengths.append(_t(lang, "You set the scene clearly.", "आपने स्थिति साफ़ तौर पर बताई।"))
    else:
        improve.append(_t(lang, "Open with the situation: where you were and what was at stake.",
                          "शुरुआत स्थिति से करें: आप कहाँ थे और क्या दाँव पर था।"))
    if has_action:
        score += 1
        strengths.append(_t(lang, "You focused on what you did yourself.", "आपने अपने खुद के काम पर ध्यान दिया।"))
    else:
        improve.append(_t(lang, 'Say what YOU did, with "I" rather than "we".', 'बताइए कि आपने खुद क्या किया, "हमने" की जगह "मैंने" कहें।'))
    if has_result or has_numbers:
        score += 1
        if has_numbers:
            strengths.append(_t(lang, "Concrete numbers make it believable.", "ठोस आंकड़ों से जवाब भरोसेमंद लगता है।"))
    else:
        improve.append(_t(lang, "End with the result, ideally a number (%, time saved, revenue).",
                          "नतीजे के साथ खत्म करें, बेहतर हो कि कोई आंकड़ा हो (%, बचाया समय, रेवेन्यू)।"))
    if mentions_company:
        score += 0.5

    score = max(1, min(5, int(round(score))))
    verdict = {
        1: _t(lang, "A start! Let's build this out.", "शुरुआत अच्छी है! इसे और बढ़ाते हैं।"),
        2: _t(lang, "You're on the right track. A bit more structure will help.", "आप सही दिशा में हैं। थोड़ा और ढांचा मदद करेगा।"),
        3: _t(lang, "Solid answer. A few tweaks will make it stand out.", "अच्छा जवाब। कुछ बदलाव इसे और बेहतर बना देंगे।"),
        4: _t(lang, "Strong answer! Nearly interview-ready.", "बहुत बढ़िया जवाब! लगभग इंटरव्यू के लिए तैयार।"),
        5: _t(lang, "Excellent! That's a confident, complete answer.", "शानदार! यह एक भरोसेमंद, पूरा जवाब है।"),
    }[score]
    highlight = resume_highlight(resume, question)
    resume_example = (_t(lang, f'From your resume, this would make a strong story: "{highlight}"',
                         f'आपके रिज़्यूमे से यह एक अच्छी कहानी बन सकती है: "{highlight}"') if highlight else "")
    spoken = [verdict]
    if strengths:
        spoken.append(strengths[0])
    if improve:
        spoken.append(_t(lang, "The main thing I'd work on: ", "सबसे ज़रूरी सुधार: ") + improve[0])
    if highlight:
        spoken.append(_t(lang, f"For example, you could tell the story from your resume where you {_story_hook(highlight)}. "
                               "Start with the situation, say what you did, and finish with that result.",
                         f"जैसे, आप अपने रिज़्यूमे का यह अनुभव बता सकते हैं: {highlight}। पहले स्थिति बताइए, फिर आपने क्या किया, और आखिर में नतीजा।"))
    spoken.append(_t(lang, "Want to give it another go, or shall we move on?", "क्या फिर से कोशिश करना चाहेंगे, या आगे बढ़ें?"))
    # Without a model there's no rewrite, so show the coach notes on what a strong answer covers
    return {"score": score, "verdict": verdict, "strengths": strengths[:2], "improve": improve[:3],
            "better_answer": (hints or "")[:800], "resume_example": resume_example,
            "spoken": " ".join(spoken), "source": "local"}


def _story_hook(line: str) -> str:
    """'Led the payments revamp, cutting drop-off 20%' -> 'led the payments revamp, cutting drop-off 20%'."""
    line = line.rstrip(".")
    return line[:1].lower() + line[1:] if line[:2] != line[:2].upper() else line
