"""
Interactive interview practice: grades a spoken or typed answer to one prep question.

Uses Gemini when a key is available and falls back to simple local checks (length, a
STAR-shaped story, concrete numbers), so practice still works without any API key.
Everything returned is plain text the page escapes before showing.
"""
import json
import re

import random

import voice_engine as ve
import livekit_llm

MAX_QUESTION_CHARS = 500
MAX_HINT_CHARS = 1500
MAX_ANSWER_CHARS = 3000
MAX_RESUME_CHARS = 6000


def _clean(text, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit]


MAX_AVOID = 12


def _question_kind(question: str) -> str:
    """"story" for behavioural questions (tell me about a time...), "view" for everything else."""
    q = (question or "").lower()
    return "story" if re.search(
        r"tell me about (a|an|the|your|one|some)\b|a time (when|you|that)|describe (a|an|the) (time|situation|project|moment)|"
        r"give (me |us )?an example|walk (me|us) through (a|an|the|your) (time|project|launch)|how did you|have you ever|"
        r"your (biggest|proudest|hardest|toughest)|बताइए जब|एक समय", q) else "view"


def grade_answer(question: str, hints: str, answer: str, title: str = "", company: str = "",
                 api_key: str = None, lang: str = "en", resume: str = "", avoid=None) -> dict:
    """Returns {"score": 1-5, "verdict", "strengths": [...], "improve": [...], "better_answer",
    "resume_example", "spoken", "source"}. `spoken` is what Sunny says aloud: a short,
    conversational walk-through of the feedback that ends by asking what to do next.
    `avoid` lists sentences Sunny already said in this practice, so she doesn't repeat herself."""
    question = _clean(question, MAX_QUESTION_CHARS)
    hints = _clean(hints, MAX_HINT_CHARS)
    answer = _clean(answer, MAX_ANSWER_CHARS)
    resume = str(resume or "").strip()[:MAX_RESUME_CHARS]
    lang = lang if lang in ve.LANGS else "en"
    avoid = [_clean(a, 300) for a in (avoid or []) if isinstance(a, str) and a.strip()][-MAX_AVOID:]
    if answer:
        prompt = _grading_prompt(question, hints, answer, title, company, lang, resume, avoid)
        if api_key:
            try:
                return _gemini_grade(prompt, api_key, lang)
            except (ve.GeminiError, ValueError, KeyError, TypeError) as e:
                print(f"Quiz feedback via Gemini failed: {e}")
        if livekit_llm.is_configured():
            try:
                return _parse_grade(livekit_llm.complete(prompt, temperature=0.7), lang)
            except (livekit_llm.LiveKitLLMError, ValueError, KeyError, TypeError) as e:
                print(f"Quiz feedback via LiveKit failed, using local checks: {e}")
    return local_grade(question, hints, answer, company, lang, resume, avoid)


def _grading_prompt(question, hints, answer, title, company, lang, resume="", avoid=()) -> str:
    language = "Hindi (Devanagari script)" if lang == "hi" else ve.LANG_NAMES.get(lang, "English")
    resume_block = (f"CANDIDATE RESUME (their real experience; the only source for examples): {json.dumps(resume)}"
                    if resume else "CANDIDATE RESUME: not available")
    if _question_kind(question) == "story":
        kind_rule = ("This is a behavioural question: a strong answer is one real story told as situation, task, "
                     "action and result, with a measurable outcome.")
    else:
        kind_rule = ("This is NOT a 'tell me about a time' question. Do NOT coach STAR, 'say I instead of we' or "
                     "'end with a number' unless it truly helps. Judge the substance: did they answer what was asked, "
                     "cover the points in the coach notes, show real knowledge of the company and market, and structure "
                     "their thinking clearly? The model answer should be an expert answer to this exact question.")
    avoid_block = ("Sunny already said these lines earlier in this practice. Do not repeat them or reuse their "
                   f"openings; word everything freshly: {json.dumps(list(avoid))}" if avoid else "")
    return f"""You are Sunny, a warm, honest interview coach talking with a candidate who is practising out loud for a {title or 'job'} interview at {company or 'a company'}.
Grade their answer to the question below on a 1-5 scale (1 = off-topic or empty, 3 = decent but generic, 5 = specific, insightful and well structured).
Be encouraging but concrete and detailed, like a good mentor in a one-on-one. Every point must be about THIS question and what they actually said: quote or name their words. Generic interview tips are not allowed. Write everything in {language}, speaking to the candidate as "you".
{kind_rule}

QUESTION: {json.dumps(question)}
COACH NOTES (what a strong answer covers): {json.dumps(hints)}
CANDIDATE ANSWER (a speech transcript; ignore filler words and transcription slips): {json.dumps(answer)}
{resume_block}
{avoid_block}

Use their resume only where an experience genuinely helps answer THIS question (for example, as evidence for a point they make). If nothing fits, leave resume_example empty rather than forcing it.
Never invent employers, numbers or projects that are not in their answer or resume.

Return only JSON:
{{"score": 1-5,
  "verdict": "one friendly sentence summing up how this answer went, specific to it",
  "strengths": ["2-3 specific points about what worked, each one full sentence naming what they said"],
  "improve": ["2-3 specific, actionable changes for this question, each one or two sentences saying what to add and why it matters to the interviewer"],
  "resume_example": "1-2 sentences naming the resume experience that would strengthen this answer and how to use it (empty string if none fits)",
  "better_answer": "a 5-8 sentence model answer to this exact question in first person, built from their answer and, where it fits, their resume",
  "spoken": "what you say out loud right after they finish: 4-6 natural, conversational sentences, like a person talking, not reading a list. React to what they actually said, name one thing that worked, explain the most important improvement for this question, and mention the resume experience if one fits. No headings, no score numbers. End with a short, naturally worded question about whether to try again or move on."}}"""


def _gemini_grade(prompt, api_key, lang) -> dict:
    res = ve.gemini_generate(api_key, [{"text": prompt}],
                             generation_config={"responseMimeType": "application/json", "temperature": 0.7},
                             timeout=20)
    return _parse_grade(ve._first_part(res).get("text", ""), lang)


def _parse_grade(text: str, lang: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.S)
        text = m.group(0) if m else text
    data = json.loads(text)
    score = int(round(float(data.get("score", 3))))
    return {
        "score": max(1, min(5, score)),
        "verdict": _clean(data.get("verdict"), 200),
        "strengths": [_clean(s, 240) for s in (data.get("strengths") or [])[:2] if _clean(s, 240)],
        "improve": [_clean(s, 260) for s in (data.get("improve") or [])[:3] if _clean(s, 260)],
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
_STRUCTURE_WORDS = ("first", "second", "third", "secondly", "another", "also", "finally", "on the other hand", "number one",
                    "पहला", "दूसरा", "तीसरा")
_REASON_WORDS = ("because", "since", "which means", "so that", "due to", "driven by", "क्योंकि", "इसलिए")
_STOP = set("""about above after again against also among another answer anything around because been before being
below between both bring could does doing down during each even every from further have having here how into just
like make many more most much must need only other over own same should some such than that their them then there
these they this those through under until very want what when where which while whom will with would your you're
yours think really thing things kind able actually basically right well also something someone question tell
describe explain talk share give example company role interview strong cover covers answer answers mention""".split())


def _t(lang, en, hi):
    return hi if lang == "hi" else en


def _pick(lang, options, avoid=(), rng=random):
    """One of several (en, hi) phrasings, preferring one Sunny hasn't said recently."""
    said = " ".join(avoid).lower()
    fresh = [o for o in options if _t(lang, *o).lower()[:40] not in said]
    return _t(lang, *rng.choice(fresh or options))


def _keywords(text: str) -> set:
    return {w[:6] for w in re.findall(r"[a-z][a-z'-]{3,}", (text or "").lower()) if w not in _STOP}


def _hint_points(hints: str) -> list:
    """Coach notes split into the separate points a strong answer covers."""
    parts = re.split(r"[.;\n]|,\s*(?:and\s+)?|\s+and\s+(?=[a-z]+\s)", hints or "")
    points = []
    for p in parts:
        p = re.sub(r"^\W+|\W+$", "", p).strip()
        if len(_keywords(p)) >= 1 and 8 <= len(p) <= 160:
            points.append(p)
    return points[:5]


def _proper_nouns(answer: str, company: str = "") -> list:
    """Names the candidate brought in (competitors, products), e.g. ['Paytm']."""
    skip = {(company or "").lower(), "i", "the", "and", "so", "but", "we", "my", "our", "it", "that", "this", "kindly",
            "user", "users", "also", "first", "second", "then", "they"}
    seen = []
    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z]{2,}|[A-Z]{2,})\b", answer or ""):
        w = m.group(1)
        if w.lower() not in skip and w not in seen:
            seen.append(w)
    return seen[:3]


_RESUME_VERBS = re.compile(r"\b(led|built|launched|drove|driving|grew|increased|reduced|owned|designed|shipped|managed|created|"
                           r"improved|delivered|scaled|ran|started|cut|saved|accelerated)\b", re.I)


def _resume_lines(resume: str) -> list:
    """Resume bullets as whole sentences: lines a PDF broke mid-sentence are joined back up."""
    raw = [l.strip() for l in re.split(r"[\n\r]+", resume or "")]
    joined = []
    for line in raw:
        if not line:
            continue
        bullet = bool(re.match(r"^[•\-–*·●▪]", line))
        line = re.sub(r"^[\s•\-–*·●▪]+", "", line).strip()
        if joined and not bullet and not re.search(r"[.!?:;]$", joined[-1]) and re.match(r"^[a-z0-9%(&]", line):
            joined[-1] += " " + line
        else:
            joined.append(line)
    out = []
    for line in joined:
        out += [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line)]
    return [l for l in out if 30 <= len(l) <= 260 and not re.search(r"\b(by|to|and|of|the|with|for|a|an|in)$", l, re.I)]


def resume_highlight(resume: str, question: str = "", hints: str = "", need_topic: bool = False) -> str:
    """The resume line most worth bringing up for this question: an action with a result that is on topic.
    With need_topic, a line only counts when it shares words with the question or coach notes."""
    lines = _resume_lines(resume)
    if not lines:
        return ""
    topic = _keywords(question) | _keywords(hints)

    def overlap(line):
        return len(_keywords(line) & topic)

    def score(line):
        return (2 if re.search(r"\d", line) else 0) + (2 if _RESUME_VERBS.search(line) else 0) + 2 * overlap(line)
    best = max(lines, key=score)
    if need_topic and not overlap(best):
        return ""
    return best if score(best) >= 2 else ""


def local_grade(question: str, hints: str, answer: str, company: str = "", lang: str = "en", resume: str = "",
                avoid=(), rng=random) -> dict:
    """Feedback without a model: checks the answer against this question's own coach notes,
    and words it differently from what Sunny said on earlier questions."""
    avoid = list(avoid or ())
    a = (answer or "").lower()
    words = len(a.split())
    if not words:
        empty = _t(lang, "I didn't get an answer that time. Give it a go, even a rough one helps!",
                   "इस बार कोई जवाब नहीं मिला। कोशिश कीजिए, कच्चा जवाब भी मदद करता है!")
        return {"score": 1, "verdict": empty, "strengths": [], "improve": [], "better_answer": "",
                "resume_example": "", "spoken": empty, "source": "local"}
    kind = _question_kind(question)
    a_keys = _keywords(answer)
    points = _hint_points(hints)
    has_numbers = bool(re.search(r"\d|percent|lakh|crore|million|billion|प्रतिशत|लाख|करोड़", a))
    names = _proper_nouns(answer, company)

    def covers(point):
        if _keywords(point) & a_keys:
            return True
        return bool(names) and bool(re.search(r"compet|rival|player|alternative", point, re.I))
    covered = [p for p in points if covers(p)]
    missing = [p for p in points if p not in covered]
    on_topic = bool((_keywords(question) | _keywords(hints)) & a_keys) or bool(covered)
    mentions_company = bool(company) and company.lower() in a
    strengths, improve = [], []

    if kind == "story":
        has_action = any(w in a for w in _ACTION_WORDS) or len(re.findall(r"\bi\b", a)) >= 3
        has_result = any(w in a for w in _RESULT_WORDS)
        has_context = any(w in a for w in _CONTEXT_WORDS)
        score = 1 + (words >= 25) + 0.5 * (words >= 60) + 0.5 * has_context + has_action + (has_result or has_numbers)
        if has_context:
            strengths.append(_pick(lang, [("You set the scene clearly, so the story is easy to follow.", "आपने स्थिति साफ़ बताई, इसलिए कहानी समझना आसान है।"),
                                          ("Starting with the situation gave your story good footing.", "स्थिति से शुरुआत करने से कहानी मज़बूत बनी।")], avoid, rng))
        else:
            improve.append(_pick(lang, [("Open with the situation: where you were and what was at stake.", "शुरुआत स्थिति से करें: आप कहाँ थे और क्या दाँव पर था।"),
                                        ("Give the interviewer a line of context first, like the team, the product and the problem.", "पहले एक लाइन का संदर्भ दें: टीम, प्रोडक्ट और समस्या।")], avoid, rng))
        if has_action:
            strengths.append(_pick(lang, [("You made your own part clear.", "आपने अपना योगदान साफ़ बताया।"),
                                          ("I could tell exactly what you did yourself.", "साफ़ पता चला कि आपने खुद क्या किया।")], avoid, rng))
        else:
            improve.append(_pick(lang, [('Say what YOU did, with "I" rather than "we".', 'बताइए कि आपने खुद क्या किया, "हमने" की जगह "मैंने" कहें।'),
                                        ("Spell out the decisions you personally made; right now the team gets the credit.", "जो फ़ैसले आपने खुद लिए वो बताइए; अभी सारा श्रेय टीम को जा रहा है।")], avoid, rng))
        if has_result or has_numbers:
            if has_numbers:
                strengths.append(_pick(lang, [("The numbers make the result believable.", "आंकड़ों से नतीजा भरोसेमंद लगता है।"),
                                              ("Putting a number on the outcome really lands.", "नतीजे में आंकड़ा देने से असर पड़ता है।")], avoid, rng))
        else:
            improve.append(_pick(lang, [("Land it with the result, ideally a number like a percentage or time saved.", "नतीजे के साथ खत्म करें, बेहतर हो कि कोई आंकड़ा हो।"),
                                        ("The story stops before the payoff. What changed because of your work?", "कहानी नतीजे से पहले रुक जाती है। आपके काम से क्या बदला?")], avoid, rng))
    else:
        has_structure = sum(w in a for w in _STRUCTURE_WORDS) >= 2
        has_reason = any(w in a for w in _REASON_WORDS)
        score = (1 + 0.5 * (words >= 30) + 0.5 * (words >= 70) + 0.5 * has_structure + 0.5 * has_reason
                 + 0.5 * bool(names or has_numbers))
        score += 1.5 * len(covered) / len(points) if points else 1 * on_topic
        if names:
            strengths.append(_t(lang, f"Bringing in {', '.join(names)} made it concrete, not generic.",
                                f"{', '.join(names)} का ज़िक्र करने से जवाब ठोस बना।"))
        if covered:
            strengths.append(_t(lang, f"You touched on a key point: {covered[0][:1].lower() + covered[0][1:]}.",
                                f"आपने एक अहम बात छुई: {covered[0]}।"))
        if has_reason:
            strengths.append(_t(lang, "You explained the why, not just the what.", "आपने सिर्फ़ क्या नहीं, क्यों भी बताया।"))
        for p in missing[:2]:
            low = p[:1].lower() + p[1:]
            improve.append(_pick(lang, [(f"A strong answer would also {low}.", f"एक मज़बूत जवाब में यह भी होगा: {p}।"),
                                        (f"You could add a part where you {low}.", f"आप यह हिस्सा जोड़ सकते हैं: {p}।"),
                                        (f"It's missing a bit where you {low}.", f"इसमें यह कमी है: {p}।")], avoid, rng))
        if not has_structure:
            improve.append(_pick(lang, [("Name your points up front, like 'I see three challenges', then take them one at a time.", "पहले बताइए कितने पॉइंट हैं, जैसे 'मुझे तीन चुनौतियाँ दिखती हैं', फिर एक-एक करके।"),
                                        ("Number your ideas as you go (first, second...) so the interviewer can follow.", "अपने पॉइंट गिनते हुए बोलिए (पहला, दूसरा...), ताकि सुनने वाला समझ सके।")], avoid, rng))
        if not (names or has_numbers):
            improve.append(_pick(lang, [("Back it up with something specific: a competitor, a figure or a recent move in the market.", "कुछ ठोस जोड़ें: कोई प्रतियोगी, कोई आंकड़ा, या बाज़ार का कोई हालिया बदलाव।"),
                                        ("One concrete fact would make this land, such as market share or a rival's recent launch.", "एक ठोस तथ्य इसे मज़बूत बना देगा, जैसे मार्केट शेयर या किसी प्रतियोगी का नया लॉन्च।")], avoid, rng))
        if company and not mentions_company and len(improve) < 3:
            improve.append(_t(lang, f"Tie it back to {company} specifically, and say what you'd do about it in this role.",
                              f"इसे खास तौर पर {company} से जोड़ें, और बताइए इस रोल में आप क्या करेंगे।"))
    if words < 25:
        improve.insert(0, _pick(lang, [("Say a bit more. Aim for 45 to 90 seconds when you speak it.", "थोड़ा और बताइए। बोलते समय 45 से 90 सेकंड का लक्ष्य रखें।"),
                                       ("That was quite short, so give it another minute of detail.", "जवाब छोटा था, एक मिनट और डिटेल दीजिए।")], avoid, rng))
    if not on_topic:
        improve.insert(0, _t(lang, "Make sure you answer the question that was asked; most of this drifted away from it.",
                             "पक्का करें कि आप पूछे गए सवाल का जवाब दें; जवाब उससे भटक गया।"))

    score = max(1, min(5, int(round(score))))
    verdicts = {
        1: [("A start! Let's build this out.", "शुरुआत अच्छी है! इसे और बढ़ाते हैं।"),
            ("Okay, there's a seed of an answer here. Let's grow it.", "ठीक है, जवाब की शुरुआत है। इसे बढ़ाते हैं।")],
        2: [("You're on the right track. A bit more depth will help.", "आप सही दिशा में हैं। थोड़ी और गहराई मदद करेगी।"),
            ("There's something here, but it needs more shape.", "इसमें दम है, पर इसे और आकार चाहिए।"),
            ("Decent instinct. Let's make it sharper.", "सोच अच्छी है। इसे और धारदार बनाते हैं।")],
        3: [("Solid answer. A few tweaks will make it stand out.", "अच्छा जवाब। कुछ बदलाव इसे और बेहतर बना देंगे।"),
            ("Good, that would hold up in a real interview.", "अच्छा, असली इंटरव्यू में यह चल जाएगा।"),
            ("Nice, you covered the core of it.", "बढ़िया, आपने मूल बात कवर कर ली।")],
        4: [("Strong answer! Nearly interview-ready.", "बहुत बढ़िया जवाब! लगभग इंटरव्यू के लिए तैयार।"),
            ("That was really good.", "यह सच में अच्छा था।")],
        5: [("Excellent! That's a confident, complete answer.", "शानदार! यह एक भरोसेमंद, पूरा जवाब है।"),
            ("Brilliant, I'd be impressed hearing that.", "शानदार, यह सुनकर कोई भी प्रभावित होगा।")],
    }
    verdict = _pick(lang, verdicts[score], avoid, rng)

    highlight = resume_highlight(resume, question, hints, need_topic=(kind != "story"))
    if highlight:
        resume_example = (_t(lang, f'From your resume, this would make a strong story: "{highlight}"',
                             f'आपके रिज़्यूमे से यह एक अच्छी कहानी बन सकती है: "{highlight}"') if kind == "story" else
                          _t(lang, f'You could back a point up with your own experience: "{highlight}"',
                             f'आप अपने अनुभव से बात मज़बूत कर सकते हैं: "{highlight}"'))
    else:
        resume_example = ""

    spoken = [verdict]
    if strengths:
        spoken.append(strengths[0])
    if improve:
        lead = _pick(lang, [("The main thing I'd work on: ", "सबसे ज़रूरी सुधार: "), ("If I had to pick one change: ", "अगर एक बदलाव चुनूँ तो: "),
                            ("Next time, try this: ", "अगली बार यह आज़माइए: "), ("What would lift it most: ", "जो इसे सबसे ज़्यादा बेहतर करेगा: ")], avoid, rng)
        spoken.append(lead + (improve[0][:1].lower() + improve[0][1:] if lang == "en" else improve[0]))
    if len(improve) > 1:
        spoken.append(_t(lang, "And one more: ", "और एक बात: ") + (improve[1][:1].lower() + improve[1][1:] if lang == "en" else improve[1]))
    if highlight:
        if kind == "story":
            spoken.append(_t(lang, f"For example, you could tell the story from your resume where you {_story_hook(highlight)}.",
                             f"जैसे, आप अपने रिज़्यूमे का यह अनुभव बता सकते हैं: {highlight}।"))
        else:
            spoken.append(_t(lang, f"Your own experience could help here too, where you {_story_hook(highlight)}.",
                             f"यहाँ आपका अपना अनुभव भी काम आएगा: {highlight}।"))
    spoken.append(_pick(lang, [("Want to give it another go, or shall we move on?", "क्या फिर से कोशिश करना चाहेंगे, या आगे बढ़ें?"),
                               ("Do you want to try that one again, or go to the next question?", "इसे फिर से आज़माएँ, या अगले सवाल पर चलें?"),
                               ("Shall we have another shot at it, or keep going?", "एक बार और कोशिश करें, या आगे बढ़ें?"),
                               ("Try again with that in mind, or next question?", "यह ध्यान में रखकर फिर से कोशिश, या अगला सवाल?")], avoid, rng))
    # Without a model there's no rewrite, so show the coach notes on what a strong answer covers
    return {"score": score, "verdict": verdict, "strengths": strengths[:2], "improve": improve[:3],
            "better_answer": (hints or "")[:800], "resume_example": resume_example,
            "spoken": " ".join(spoken), "source": "local"}


def _story_hook(line: str) -> str:
    """'Led the payments revamp, cutting drop-off 20%' -> 'led the payments revamp, cutting drop-off 20%'."""
    line = line.rstrip(".")
    return line[:1].lower() + line[1:] if line[:2] != line[:2].upper() else line
