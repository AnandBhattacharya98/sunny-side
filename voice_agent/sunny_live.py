"""Real-time Sunny: the board assistant and the Quiz Mode coach as LiveKit voice agents.

The browser that started the session stays in charge of the board. Whenever Sunny needs to do
something (look something up, move a job, grade a quiz answer), she asks the browser over LiveKit
RPC, and the browser runs it through the same website endpoints the typed chat uses. So board
commands, confirmations and quiz grading behave exactly like the non-live versions, and the agent
needs no access to the website or its database.

RPC methods the browser registers (payloads and replies are JSON):
  sunny.ask      {text}            -> {reply, needs_confirmation, board_changed, opened_quiz}
  sunny.confirm  {}                -> {reply, board_changed}
  sunny.cancel   {}                -> {reply}
  quiz.show      {index}           -> {ok}
  quiz.grade     {index, answer}   -> {ok, score, spoken, error?}
  quiz.finish    {}                -> {ok, summary}
"""
import json
import logging
import os

from livekit import rtc
from livekit.agents import Agent, AgentSession, RunContext, function_tool, get_job_context, inference
from livekit.agents.beta.tools import EndCallTool

logger = logging.getLogger("sunny-live")

LANG_NAMES = {"en": "English", "hi": "Hindi", "ta": "Tamil", "te": "Telugu", "bn": "Bengali", "mr": "Marathi",
              "kn": "Kannada", "es": "Spanish", "fr": "French", "de": "German", "ar": "Arabic"}
# Deepgram Nova 3 "multi" handles these (and Hindi-English mixing); Whisper covers the rest
NOVA_LANGS = {"en", "hi", "es", "fr", "de"}
SUNNY_VOICE = os.getenv("SUNNY_TTS_VOICE", "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc").strip()  # same voice as the site
SUNNY_TTS_MODEL = os.getenv("SUNNY_TTS_MODEL", "cartesia/sonic-3").strip()


def live_details(data: dict) -> dict:
    """The parts of the job metadata a live session uses, cleaned up."""
    lang = data.get("lang") if data.get("lang") in LANG_NAMES else "en"
    out = {
        "mode": data.get("mode") if data.get("mode") in ("sunny", "quiz") else "sunny",
        "lang": lang,
        "identity": str(data.get("identity") or ""),
        "candidate_name": " ".join(str(data.get("candidate_name") or "").split())[:60],
        "resumed": bool(data.get("resumed")),
        "board": [b for b in (data.get("board") or []) if isinstance(b, dict)][:40],
        "title": str(data.get("title") or "")[:120],
        "company": str(data.get("company") or "")[:80],
        "resume": str(data.get("resume") or "")[:1200],
        "questions": [q for q in (data.get("questions") or []) if isinstance(q, dict) and q.get("q")][:15],
    }
    try:
        out["start"] = max(0, min(int(data.get("start") or 0), max(len(out["questions"]) - 1, 0)))
    except (TypeError, ValueError):
        out["start"] = 0
    return out


def stt_for(lang: str, model_override: str = ""):
    if model_override:
        return inference.STT(model=model_override, language="multi" if lang in NOVA_LANGS else lang)
    if lang in NOVA_LANGS:
        return inference.STT(model="deepgram/nova-3", language="multi")
    return inference.STT(model="cartesia/ink-whisper", language=lang)


def tts_for(lang: str):
    return inference.TTS(model=SUNNY_TTS_MODEL, voice=SUNNY_VOICE, language=lang)


async def ask_browser(identity: str, method: str, payload: dict, timeout: float = 30.0) -> dict:
    """Runs one of the browser's RPC methods. Returns {} if the browser can't be reached."""
    room = get_job_context().room
    if not identity or identity not in room.remote_participants:
        identity = next((p.identity for p in room.remote_participants.values()
                         if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT), "")
    if not identity:
        return {}
    try:
        raw = await room.local_participant.perform_rpc(destination_identity=identity, method=method,
                                                       payload=json.dumps(payload), response_timeout=timeout)
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("browser rpc %s failed: %s", method, e)
        return {}


def _lang_rule(lang: str) -> str:
    name = LANG_NAMES.get(lang, "English")
    if lang == "en":
        return "Speak English. If the user switches language, follow them."
    return f"Speak {name}. If the user switches language, follow them. Job titles and company names stay as written."


# ── Sunny, the board assistant ──────────────────────────────────────────────

SUNNY_PROMPT = """You are Sunny, the cheerful, warm voice assistant inside Sunny-Side, a job-search board.
You're talking out loud in real time, so sound like a friend on a call: short sentences, at most two or
three per turn, no lists, markdown, emojis or URLs. Say numbers naturally.

What you can do:
- Answer simple questions about the user's board yourself, using the board snapshot below
  (titles, companies, columns and match scores out of 10). Example: "what are my top matches?".
- For anything that changes the board, or needs more than the snapshot (moving a job, archiving,
  explaining a score, what's new today, follow-ups, emails, interview prep, starting a quiz,
  cover letters, what to do next), call board_request with the user's request in their own words.
  The website does it and tells you what to say. Say that reply naturally; you may shorten it,
  but never change facts, names, numbers or what was done.
- If board_request says it needs a yes or no, ask the user exactly that and wait. When they agree,
  call confirm_action; when they decline or change their mind, call cancel_action.
  Never call confirm_action unless the user clearly said yes to that question.
- If the user is done ("bye", "that's all", "stop"), say a short goodbye and call end_call.

Never invent jobs, scores, dates or companies. If you don't know, use board_request."""


def board_text(board: list[dict]) -> str:
    if not board:
        return "The board is empty right now."
    lines = []
    for b in board:
        score = b.get("score")
        lines.append(f"- {b.get('title') or 'Untitled'} at {b.get('company') or 'unknown company'}"
                     f" ({b.get('column') or 'Inbox'}{f', match {score}' if isinstance(score, (int, float)) else ''})")
    return "\n".join(lines)


class SunnyLive(Agent):
    def __init__(self, details: dict) -> None:
        name = details["candidate_name"]
        instructions = "\n\n".join([
            SUNNY_PROMPT,
            "# Language\n" + _lang_rule(details["lang"]),
            "# The user\n" + (f"Their name is {name}." if name else "You don't know their name."),
            "# Board snapshot (when the session started)\n" + board_text(details["board"]),
        ])
        super().__init__(instructions=instructions, tools=[EndCallTool(ignore_on_enter=True, delete_room=True)])
        self.details = details
        self.board_stale = False

    async def on_enter(self) -> None:
        if self.details["resumed"]:
            return
        self.session.generate_reply(instructions="Say hi as Sunny in one short, friendly sentence"
                                    + (f" using their first name, {self.details['candidate_name'].split()[0]}"
                                       if self.details["candidate_name"] else "")
                                    + ", and ask what they'd like to do.")

    async def _browser(self, method: str, payload: dict) -> dict:
        return await ask_browser(self.details["identity"], method, payload)

    def _after(self, res: dict) -> str:
        notes = []
        if res.get("board_changed"):
            self.board_stale = True
            notes.append("The board changed; the snapshot above is now out of date, so use board_request for board questions.")
        if res.get("opened_quiz"):
            notes.append("The quiz is opening on screen and this chat is ending. Say one encouraging line, then stop.")
        return " ".join(notes)

    @function_tool
    async def board_request(self, context: RunContext, request: str) -> str:
        """Ask Sunny-Side to do or look up something on the user's job board.

        Args:
            request: What the user asked, in their own words, e.g. "move the Swiggy job to applied".
        """
        res = await self._browser("sunny.ask", {"text": request[:500]})
        if not res:
            return "The website didn't answer. Say sorry, it couldn't reach the board just now, and offer to try again."
        reply = res.get("reply") or "Done."
        if res.get("needs_confirmation"):
            return f"Website reply: {reply}\nThis needs the user's yes or no before anything changes. Ask them."
        return f"Website reply: {reply}\n{self._after(res)}".strip()

    @function_tool
    async def confirm_action(self, context: RunContext) -> str:
        """The user said yes to the pending change. Only call this right after they clearly agreed."""
        res = await self._browser("sunny.confirm", {})
        if not res:
            return "The website didn't answer, so nothing changed. Tell the user and offer to try again."
        return f"Website reply: {res.get('reply') or 'Done.'}\n{self._after(res)}".strip()

    @function_tool
    async def cancel_action(self, context: RunContext) -> str:
        """The user said no to the pending change, or changed their mind."""
        res = await self._browser("sunny.cancel", {})
        return f"Website reply: {res.get('reply') or 'Okay, cancelled.'}"


# ── Quiz Mode coach ─────────────────────────────────────────────────────────

QUIZ_PROMPT = """You are Sunny, an encouraging interview coach, running a spoken practice quiz in real time.
The questions are fixed and come from the tools; ask each one exactly as written, without the hints.

How each question goes:
1. Ask the question, then let the user answer. Stay quiet while they think. If they stop mid-thought,
   say only "mm-hm" or "take your time" and keep listening.
2. When their answer sounds finished, call submit_answer. Don't grade it yourself.
3. Say the feedback that submit_answer gives you, in your own warm voice but keeping every point.
   Then ask if they'd like to try that one again or move on.
4. "Again" or "retry": call retry_question. "Next", "move on", "okay": call next_question.
   "Repeat the question": say it again. "Give me a hint": give one short nudge from the hints.
   "Stop" or "I'm done": call finish_quiz.

Keep your own turns to one or two short sentences. No lists, markdown or emojis.
Never make up a score or feedback; it only comes from submit_answer."""


class QuizCoach(Agent):
    def __init__(self, details: dict) -> None:
        self.details = details
        self.qs = details["questions"]
        self.index = details["start"]
        self.answer_from = 0  # history position where answers to the current question start
        job = details["title"] + (f" at {details['company']}" if details["company"] else "")
        hints = "\n".join(f"Q{i + 1}: {q['q']}\n   hints: {q.get('hints') or 'none'}" for i, q in enumerate(self.qs))
        instructions = "\n\n".join([
            QUIZ_PROMPT,
            "# Language\n" + _lang_rule(details["lang"]),
            f"# The practice\nRole: {job or 'not given'}. {len(self.qs)} questions. "
            + (f"The candidate is {details['candidate_name']}." if details["candidate_name"] else ""),
            "# Questions and hints (for your reference; never read hints unless asked)\n" + hints,
        ])
        super().__init__(instructions=instructions, tools=[EndCallTool(ignore_on_enter=True, delete_room=True)])

    def _mark(self) -> None:
        self.answer_from = len(self.session.history.items)

    def _current(self) -> dict:
        return self.qs[self.index]

    def _answer_so_far(self) -> str:
        said = []
        for item in self.session.history.items[self.answer_from:]:
            if getattr(item, "type", "") == "message" and item.role == "user" and (item.text_content or "").strip():
                said.append(item.text_content.strip())
        return " ".join(said)

    async def _browser(self, method: str, payload: dict, timeout: float = 30.0) -> dict:
        return await ask_browser(self.details["identity"], method, payload, timeout)

    async def on_enter(self) -> None:
        self._mark()
        n = len(self.qs)
        first = "first question" if self.index == 0 else f"question {self.index + 1}"
        self.session.generate_reply(instructions=(
            f"In one short sentence, say you'll practise {n} question{'s' if n != 1 else ''} together out loud. "
            f"Then ask the {first} exactly as written: {self._current()['q']}"))

    @function_tool
    async def submit_answer(self, context: RunContext) -> str:
        """The user has finished answering the current question. Gets the feedback to say."""
        answer = self._answer_so_far()
        if len(answer.split()) < 3:
            return "They haven't really answered yet. Encourage them to give it a go, then keep listening."
        res = await self._browser("quiz.grade", {"index": self.index, "answer": answer[:3000]}, timeout=60.0)
        if not res or not res.get("ok"):
            return ("Grading didn't work just now (" + str(res.get("error") or "no reply") + "). Apologise briefly "
                    "and ask if they'd like to try again or move on.")
        self.answer_from = len(self.session.history.items)  # a retry starts a fresh answer
        return (f"Score: {res.get('score')} out of 5. Feedback to say: {res.get('spoken') or ''}\n"
                "After saying it, ask if they want to try again or move on.")

    @function_tool
    async def retry_question(self, context: RunContext) -> str:
        """The user wants another go at the current question."""
        await self._browser("quiz.show", {"index": self.index, "retry": True})
        self._mark()
        return f"Tell them to go ahead, then ask the question again: {self._current()['q']}"

    @function_tool
    async def next_question(self, context: RunContext) -> str:
        """Move on to the next question."""
        if self.index >= len(self.qs) - 1:
            return await self._finish()
        self.index += 1
        await self._browser("quiz.show", {"index": self.index})
        self._mark()
        return f"Ask question {self.index + 1} of {len(self.qs)} exactly as written: {self._current()['q']}"

    @function_tool
    async def finish_quiz(self, context: RunContext) -> str:
        """The user wants to stop, or the last question is done."""
        return await self._finish()

    async def _finish(self) -> str:
        res = await self._browser("quiz.finish", {})
        summary = res.get("summary") or ""
        return ("The practice is over. " + (f"Results: {summary}. " if summary else "")
                + "Sum it up in two warm sentences, say goodbye, then call end_call.")


def build_live_agent(details: dict) -> Agent:
    return QuizCoach(details) if details["mode"] == "quiz" and details["questions"] else SunnyLive(details)

