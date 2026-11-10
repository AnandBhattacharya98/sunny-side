"""Sunny-Side practice call: a LiveKit voice agent that plays a recruiter doing a first-round screen.

Three ways a call starts:
  * Browser: the website (/calls) mints a room token that dispatches this agent by name, with the
    candidate's name, target role and a resume excerpt as job metadata. The browser talks over WebRTC.
  * Call me: the website dispatches the agent with `dial_to` and `trunk_id` in the metadata, and the
    agent rings the user's phone through LiveKit SIP.
  * Dial in: a LiveKit SIP dispatch rule sends a call to the practice number here, in a room named
    phone-in-...; the agent asks for the one-time code from the Calls page to learn who is calling.
Phone calls are logged by the agent posting the transcript to the website (see sunny_site.py).

Pipeline for every turn:  your voice -> STT -> LLM -> TTS -> you
plus VAD (is anyone speaking?) and a turn detector (have they finished their thought?).

Every setting you'll experiment with is read from the environment (.env.local locally,
`lk agent update-secrets` on LiveKit Cloud), so you can change ONE thing and hear the difference.

Run locally:     python agent.py dev      (then start a practice call on the Calls page)
Talk in a shell: python agent.py console
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import (Agent, AgentServer, AgentSession, JobContext, JobProcess, RunContext, cli,
                            function_tool, get_job_context, inference, room_io)
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import silero

import sunny_site

load_dotenv(".env.local")
load_dotenv()

logger = logging.getLogger("practice-recruiter")

AGENT_NAME = "sunny-screener"  # the website dispatches this name; keep in sync with LIVEKIT_AGENT_NAME
PROMPT = (Path(__file__).parent / "prompt.md").read_text()


def env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


# ---------------------------------------------------------------------------
# The knobs. Defaults are a sensible starting point; change them in the environment.
# ---------------------------------------------------------------------------
STT_MODEL = env("STT_MODEL", "deepgram/nova-3")
STT_LANGUAGE = env("STT_LANGUAGE", "multi")  # "multi" handles Hindi/English mixing
LLM_MODEL = env("LLM_MODEL", "openai/gpt-4.1-mini")
TTS_MODEL = env("TTS_MODEL", "cartesia/sonic-3")
TTS_VOICE = env("TTS_VOICE", "")  # a Cartesia voice id from https://play.cartesia.ai/voices

# How the agent decides you've finished talking: model | vad | stt
TURN_DETECTION = env("TURN_DETECTION", "model")
MIN_ENDPOINTING_DELAY = float(env("MIN_ENDPOINTING_DELAY", "0.5"))
MAX_ENDPOINTING_DELAY = float(env("MAX_ENDPOINTING_DELAY", "3.0"))

# Interruption (barge-in)
ALLOW_INTERRUPTIONS = env("ALLOW_INTERRUPTIONS", "true").lower() == "true"
MIN_INTERRUPTION_DURATION = float(env("MIN_INTERRUPTION_DURATION", "0.5"))
MIN_INTERRUPTION_WORDS = int(env("MIN_INTERRUPTION_WORDS", "0"))

NOISE_CANCELLATION = env("NOISE_CANCELLATION", "true").lower() == "true"
PREEMPTIVE_GENERATION = env("PREEMPTIVE_GENERATION", "true").lower() == "true"


DETAIL_KEYS = ("candidate_name", "role", "company", "resume")
PHONE_IN_PREFIX = "phone-in-"
CALLEE_IDENTITY = "phone-candidate"
MAX_CODE_TRIES = 3


def call_details(raw: str | None) -> dict:
    """The job metadata the website attached: candidate_name, role, company, resume, and for
    phone calls dial_to, trunk_id and call_ref."""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {k: " ".join(str(data.get(k) or "").split()) for k in DETAIL_KEYS}
    for k in ("dial_to", "trunk_id", "call_ref"):
        out[k] = str(data.get(k) or "").strip()
    return out


def instructions_for(details: dict) -> str:
    lines = [PROMPT, "", "# This call"]
    lines.append(f"- Candidate: {details['candidate_name'] or 'unknown; ask their name'}")
    lines.append(f"- Role: {details['role'] or 'not given; ask what role they are interviewing for'}")
    if details["company"]:
        lines.append(f"- Company you recruit for: {details['company']}")
    if details["resume"]:
        lines.append(f"- Resume excerpt (use it for one tailored question, don't read it out): {details['resume'][:1500]}")
    return "\n".join(lines)


GREETING = ("Greet the candidate by name if you know it, introduce yourself as Maya, the recruiter, "
            "say this is a quick first-round chat about the role, and ask if now is a good time.")


class Recruiter(Agent):
    def __init__(self, details: dict, *, greet: bool = True, phone: bool = False) -> None:
        tools = [EndCallTool(extra_description="Use it after you've closed the screen and said goodbye.",
                             ignore_on_enter=True)] if phone else []
        super().__init__(instructions=instructions_for(details), tools=tools)
        self.details = details
        self.greet = greet

    async def on_enter(self) -> None:
        if self.greet:
            await self.session.generate_reply(instructions=GREETING)


class PhoneCall:
    """What a phone call needs to report itself to the website when it ends."""

    def __init__(self, room: str, call_ref: str = "") -> None:
        self.room = room
        self.call_ref = call_ref
        self.started = 0.0
        self.history_from = 0  # skip the code check; only the screen goes in the transcript

    def begin(self, session: AgentSession) -> None:
        self.started = time.monotonic()
        self.history_from = len(session.history.items)

    async def report(self, session: AgentSession) -> None:
        if not self.call_ref:
            return
        transcript = []
        for item in session.history.items[self.history_from:]:
            if getattr(item, "type", "") != "message" or item.role not in ("assistant", "user"):
                continue
            text = (item.text_content or "").strip()
            if text:
                transcript.append({"role": "agent" if item.role == "assistant" else "user", "text": text})
        duration = int(time.monotonic() - self.started) if self.started else 0
        status, _ = await sunny_site.post("/api/agent/calls/end", {
            "call_ref": self.call_ref, "room": self.room, "duration_seconds": duration, "transcript": transcript,
        })
        logger.info("phone call logged on the website: status=%s turns=%d", status, len(transcript))


CODE_DESK_PROMPT = """You answer the Sunny-Side practice phone line. Before the practice screen can start, the caller
must give the six-digit code shown on their Calls page. They can type it on the keypad or say it.
When they say digits, call check_code with exactly the digits they said. Keep every turn to one short
sentence. Don't talk about anything else; if they don't have a code, tell them to open the Calls page
in Sunny-Side and press Get my code, then call back. Never read a code back aloud."""


class CodeDesk(Agent):
    """Asks a dial-in caller for their one-time code, then hands the call to the recruiter."""

    def __init__(self, call: PhoneCall, caller: str) -> None:
        super().__init__(instructions=CODE_DESK_PROMPT, tools=[EndCallTool()])
        self.call = call
        self.caller = caller
        self.tries = 0
        self.checking = False
        self.done = False

    async def on_enter(self) -> None:
        self.session.say("Hi, this is Sunny-Side phone practice. Please type or say the six digit code "
                         "from your Calls page.")

    @function_tool
    async def check_code(self, context: RunContext, code: str) -> str | Agent:
        """Check the six-digit practice code the caller said.

        Args:
            code: Just the digits the caller said, for example "482913".
        """
        return await self.try_code(code)

    async def try_code(self, code: str) -> str | Agent:
        digits = re.sub(r"\D", "", code or "")
        if self.done or self.checking:
            return "Already checking a code. Wait a moment."
        if len(digits) != 6:
            return "That isn't six digits. Ask the caller for all six digits of the code."
        self.checking = True
        try:
            status, data = await sunny_site.post("/api/agent/phone/claim",
                                                 {"code": digits, "room": self.call.room, "caller": self.caller})
        finally:
            self.checking = False
        if status == 200 and data.get("ok"):
            self.done = True
            self.call.call_ref = data.get("call_ref") or ""
            self.call.begin(self.session)
            details = {k: " ".join(str((data.get("details") or {}).get(k) or "").split()) for k in DETAIL_KEYS}
            return Recruiter(details, phone=True)
        self.tries += 1
        if status == 0:
            return "The Sunny-Side website can't be reached right now. Apologise and ask them to try again later, then end the call."
        if self.tries >= MAX_CODE_TRIES or not data.get("retry", True):
            return "That code didn't work and there are no tries left. Say sorry, then end the call."
        return "That code didn't work. Ask them to check the code on their Calls page and try again."

    async def keypad_code(self, digits: str) -> None:
        """A code typed on the keypad: same check, but nothing is mid-turn to hand the result to."""
        self.session.say("One moment.")
        result = await self.try_code(digits)
        if isinstance(result, Agent):
            self.session.update_agent(result)
        else:
            self.session.generate_reply(instructions=result)


server = AgentServer()


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


def build_turn_detection():
    if TURN_DETECTION == "model":
        return inference.TurnDetector()
    if TURN_DETECTION in ("vad", "stt"):
        return TURN_DETECTION
    raise ValueError(f"TURN_DETECTION must be model, vad or stt, not {TURN_DETECTION!r}")


def build_noise_cancellation():
    """Telephony-tuned noise cancellation for phone callers, the regular model for browsers."""
    if not NOISE_CANCELLATION:
        return None
    try:
        from livekit.plugins import noise_cancellation
    except ImportError:
        logger.warning("livekit-plugins-noise-cancellation isn't installed; running without it")
        return None

    def pick(params):  # room_io NoiseCancellationParams: .participant, .track
        if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return noise_cancellation.BVCTelephony()
        return noise_cancellation.BVC()

    return pick


def build_session(ctx: JobContext) -> AgentSession:
    return AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=inference.STT(model=STT_MODEL, language=STT_LANGUAGE),
        llm=inference.LLM(model=LLM_MODEL),
        tts=inference.TTS(model=TTS_MODEL, voice=TTS_VOICE) if TTS_VOICE else inference.TTS(model=TTS_MODEL),
        turn_handling={
            "turn_detection": build_turn_detection(),
            "endpointing": {"min_delay": MIN_ENDPOINTING_DELAY, "max_delay": MAX_ENDPOINTING_DELAY},
            "interruption": {
                "enabled": ALLOW_INTERRUPTIONS,
                "min_duration": MIN_INTERRUPTION_DURATION,
                "min_words": MIN_INTERRUPTION_WORDS,
            },
            "preemptive_generation": {"enabled": PREEMPTIVE_GENERATION},
        },
    )


def room_options(identity: str | None = None) -> room_io.RoomOptions:
    nc = build_noise_cancellation()
    opts = {"audio_input": room_io.AudioInputOptions(noise_cancellation=nc) if nc else True}
    if identity:
        opts["participant_identity"] = identity
    return room_io.RoomOptions(**opts)


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}
    details = call_details(ctx.job.metadata)
    mode = "call-me" if details["dial_to"] else "dial-in" if ctx.room.name.startswith(PHONE_IN_PREFIX) else "browser"
    logger.info(
        "settings: mode=%s stt=%s llm=%s tts=%s voice=%s turn=%s endpointing=%.2f-%.2fs "
        "interruptions=%s (min %.2fs, %d words) noise_cancellation=%s preemptive=%s role=%r",
        mode, STT_MODEL, LLM_MODEL, TTS_MODEL, TTS_VOICE or "default", TURN_DETECTION,
        MIN_ENDPOINTING_DELAY, MAX_ENDPOINTING_DELAY, ALLOW_INTERRUPTIONS,
        MIN_INTERRUPTION_DURATION, MIN_INTERRUPTION_WORDS, NOISE_CANCELLATION, PREEMPTIVE_GENERATION,
        details["role"],
    )
    session = build_session(ctx)

    if mode == "browser":
        await session.start(agent=Recruiter(details), room=ctx.room, room_options=room_options())
    elif mode == "call-me":
        await ring_candidate(ctx, session, details)
    else:
        await answer_dial_in(ctx, session)


async def ring_candidate(ctx: JobContext, session: AgentSession, details: dict) -> None:
    """Call me: ring the user's phone, and start the screen once they pick up."""
    call = PhoneCall(ctx.room.name, details["call_ref"])
    ctx.add_shutdown_callback(lambda: call.report(session))
    started = asyncio.create_task(session.start(
        agent=Recruiter(details, greet=False, phone=True), room=ctx.room, room_options=room_options(CALLEE_IDENTITY)))
    try:
        await ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(
            room_name=ctx.room.name,
            sip_trunk_id=details["trunk_id"],
            sip_call_to=details["dial_to"],
            participant_identity=CALLEE_IDENTITY,
            participant_name="Candidate",
            wait_until_answered=True,
        ))
    except Exception as e:  # busy, no answer, bad trunk: logged as a missed call
        logger.info("outbound call not answered: %s", e)
        started.cancel()
        ctx.shutdown("not answered")
        return
    await started
    call.begin(session)
    session.generate_reply(instructions=GREETING)


async def answer_dial_in(ctx: JobContext, session: AgentSession) -> None:
    """Dial in: ask for the one-time code, then hand over to the recruiter."""
    await ctx.connect()
    caller = await ctx.wait_for_participant(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
    call = PhoneCall(ctx.room.name)
    ctx.add_shutdown_callback(lambda: call.report(session))
    desk = CodeDesk(call, caller.attributes.get("sip.phoneNumber", ""))

    keyed: list[str] = []

    def on_dtmf(ev: rtc.SipDTMF) -> None:
        if desk.done or ev.participant is None or ev.participant.identity != caller.identity:
            return
        if ev.digit == "#" or ev.digit == "*":
            digits, keyed[:] = "".join(keyed), []
        else:
            keyed.append(ev.digit)
            if len(keyed) < 6:
                return
            digits, keyed[:] = "".join(keyed), []
        if digits:
            asyncio.create_task(desk.keypad_code(digits))

    ctx.room.on("sip_dtmf_received", on_dtmf)
    await session.start(agent=desk, room=ctx.room, room_options=room_options(caller.identity))


if __name__ == "__main__":
    cli.run_app(server)
