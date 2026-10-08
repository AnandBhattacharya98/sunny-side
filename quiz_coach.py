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


def _clean(text, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


def grade_answer(question: str, hints: str, answer: str, title: str = "", company: str = "",
                 api_key: str = None, lang: str = "en") -> dict:
    """Returns {"score": 1-5, "verdict", "strengths": [...], "improve": [...], "better_answer", "source"}."""
    question = _clean(question, MAX_QUESTION_CHARS)
    hints = _clean(hints, MAX_HINT_CHARS)
    answer = _clean(answer, MAX_ANSWER_CHARS)
    lang = lang if lang in ve.LANGS else "en"
    if api_key and answer:
        try:
            return _gemini_grade(question, hints, answer, title, company, api_key, lang)
        except (ve.GeminiError, ValueError, KeyError, TypeError) as e:
            print(f"Quiz feedback via Gemini failed, using local checks: {e}")
    return local_grade(question, hints, answer, company, lang)


def _gemini_grade(question, hints, answer, title, company, api_key, lang) -> dict:
    language = "Hindi (Devanagari script)" if lang == "hi" else "English"
    prompt = f"""You are a warm, honest interview coach. A candidate is practising for a {title or 'job'} interview at {company or 'a company'}.
Grade their answer to the question below on a 1-5 scale (1 = off-topic or empty, 3 = decent but generic, 5 = specific, structured, with measurable results).
Be encouraging but concrete. Write everything in {language}, speaking to the candidate as "you".

QUESTION: {json.dumps(question)}
COACH NOTES (what a strong answer covers): {json.dumps(hints)}
CANDIDATE ANSWER (may be a speech transcript with filler words): {json.dumps(answer)}

Return only JSON:
{{"score": 1-5,
  "verdict": "one short friendly sentence",
  "strengths": ["up to 2 short points about what worked"],
  "improve": ["up to 3 short, specific things to add or change"],
  "better_answer": "a 2-4 sentence stronger version built from THEIR answer, not invented facts"}}"""
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
        "better_answer": _clean(data.get("better_answer"), 800),
        "source": "ai",
    }


_ACTION_WORDS = ("i led", "i built", "i drove", "i ran", "i owned", "i designed", "i launched", "i created", "i decided",
                 "i worked", "i managed", "i analysed", "i analyzed", "i set up", "i wrote", "i convinced", "i proposed",
                 "मैंने")
_RESULT_WORDS = ("result", "increase", "increased", "reduced", "improved", "grew", "saved", "impact", "outcome", "launched",
                 "conversion", "revenue", "retention", "नतीजा", "बढ़", "कम किया", "सुधार")
_CONTEXT_WORDS = ("when i", "at my", "in my last", "in my previous", "our team", "the project", "we had", "situation",
                  "जब मैं", "मेरी पिछली", "प्रोजेक्ट")


def _t(lang, en, hi):
    return hi if lang == "hi" else en


def local_grade(question: str, hints: str, answer: str, company: str = "", lang: str = "en") -> dict:
    a = (answer or "").lower()
    words = len(a.split())
    has_numbers = bool(re.search(r"\d|percent|lakh|crore|million|प्रतिशत|लाख|करोड़", a))
    has_action = any(w in a for w in _ACTION_WORDS) or len(re.findall(r"\bi\b", a)) >= 3
    has_result = any(w in a for w in _RESULT_WORDS)
    has_context = any(w in a for w in _CONTEXT_WORDS)
    mentions_company = bool(company) and company.lower() in a

    strengths, improve = [], []
    if not words:
        return {"score": 1, "verdict": _t(lang, "I didn't get an answer that time. Give it a go, even a rough one helps!",
                                          "इस बार कोई जवाब नहीं मिला। कोशिश कीजिए, कच्चा जवाब भी मदद करता है!"),
                "strengths": [], "improve": [], "better_answer": "", "source": "local"}

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
    # Without a model there's no rewrite, so show the coach notes on what a strong answer covers
    return {"score": score, "verdict": verdict, "strengths": strengths[:2], "improve": improve[:3],
            "better_answer": (hints or "")[:800], "source": "local"}
