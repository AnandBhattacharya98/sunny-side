"""Sunny-Side practice call: a LiveKit voice agent that plays a recruiter doing a first-round screen.

The website (/calls) mints a room token that dispatches this agent by name, with the candidate's
name, target role and a resume excerpt as job metadata. The browser talks to it over WebRTC.

Pipeline for every turn:  your voice -> STT -> LLM -> TTS -> you
plus VAD (is anyone speaking?) and a turn detector (have they finished their thought?).

Every setting you'll experiment with is read from the environment (.env.local locally,
`lk agent update-secrets` on LiveKit Cloud), so you can change ONE thing and hear the difference.

Run locally:     python agent.py dev      (then start a practice call on the Calls page)
Talk in a shell: python agent.py console
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, JobProcess, cli, inference, room_io
from livekit.plugins import silero

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


def call_details(raw: str | None) -> dict:
    """The job metadata the website attached: candidate_name, role, company, resume."""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: " ".join(str(data.get(k) or "").split()) for k in ("candidate_name", "role", "company", "resume")}


def instructions_for(details: dict) -> str:
    lines = [PROMPT, "", "# This call"]
    lines.append(f"- Candidate: {details['candidate_name'] or 'unknown; ask their name'}")
    lines.append(f"- Role: {details['role'] or 'not given; ask what role they are interviewing for'}")
    if details["company"]:
        lines.append(f"- Company you recruit for: {details['company']}")
    if details["resume"]:
        lines.append(f"- Resume excerpt (use it for one tailored question, don't read it out): {details['resume'][:1500]}")
    return "\n".join(lines)


class Recruiter(Agent):
    def __init__(self, details: dict) -> None:
        super().__init__(instructions=instructions_for(details))
        self.details = details

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions="Greet the candidate by name if you know it, introduce yourself as Maya, the recruiter, "
            "say this is a quick first-round chat about the role, and ask if now is a good time."
        )


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
    if not NOISE_CANCELLATION:
        return None
    try:
        from livekit.plugins import noise_cancellation
    except ImportError:
        logger.warning("livekit-plugins-noise-cancellation isn't installed; running without it")
        return None
    return noise_cancellation.BVC()


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}
    details = call_details(ctx.job.metadata)
    logger.info(
        "settings: stt=%s llm=%s tts=%s voice=%s turn=%s endpointing=%.2f-%.2fs "
        "interruptions=%s (min %.2fs, %d words) noise_cancellation=%s preemptive=%s role=%r",
        STT_MODEL, LLM_MODEL, TTS_MODEL, TTS_VOICE or "default", TURN_DETECTION,
        MIN_ENDPOINTING_DELAY, MAX_ENDPOINTING_DELAY, ALLOW_INTERRUPTIONS,
        MIN_INTERRUPTION_DURATION, MIN_INTERRUPTION_WORDS, NOISE_CANCELLATION, PREEMPTIVE_GENERATION,
        details["role"],
    )

    session = AgentSession(
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

    nc = build_noise_cancellation()
    await session.start(
        agent=Recruiter(details),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(noise_cancellation=nc) if nc else True,
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
