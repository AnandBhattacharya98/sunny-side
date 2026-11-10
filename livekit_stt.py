"""Sunny's hearing through LiveKit Inference speech-to-text (https://docs.livekit.io/agents/models/stt/).

Uses the same LiveKit Cloud keys as the voice (see livekit_tts.py) and the same gateway, over its
streaming WebSocket. The gateway takes raw 16-bit PCM, so the board converts each recording to a
16 kHz mono WAV in the browser before uploading it; anything else goes to Gemini as before.
"""
import io
import json
import time
import wave
import base64
import logging
from urllib.parse import urlencode

import livekit_tts
from livekit_tts import LiveKitTTSError

log = logging.getLogger("voice.livekit")

# Deepgram Nova 3 is LiveKit's recommended English model and also covers these languages.
# Cartesia Ink Whisper (Whisper-based) covers the rest of Sunny's languages.
NOVA_LANGS = {"en", "hi", "es", "fr", "de"}
NOVA_MODEL = "deepgram/nova-3"
WHISPER_MODEL = "cartesia/ink-whisper"
TIMEOUT = 15            # seconds for the whole transcription
TRAILING_WAIT = 0.3     # seconds to wait for stray text once the gateway confirms the finalize
IDLE_WAIT = 1.5         # without that confirmation, how long a quiet gap must be before we stop
FRAME_MS = 50
SEND_SPEED = 2.0     # how much faster than real time the clip is sent
CREATE_WAIT = 3.0    # seconds to wait for session.created before sending audio


class LiveKitSTTError(LiveKitTTSError):
    pass


def is_configured() -> bool:
    return livekit_tts.is_configured()


def model_for(lang: str) -> str:
    override = livekit_tts._env("LIVEKIT_STT_MODEL")
    if override:
        return override
    return NOVA_MODEL if (lang or "en") in NOVA_LANGS else WHISPER_MODEL


def wav_to_pcm(data: bytes) -> tuple[bytes, int] | None:
    """(PCM, sample rate) for a 16-bit mono WAV, or None if the bytes aren't one."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getcomptype() != "NONE":
                return None
            return w.readframes(w.getnframes()), w.getframerate()
    except (wave.Error, EOFError):
        return None


def _is_timeout(e: Exception) -> bool:
    return isinstance(e, TimeoutError) or type(e).__name__ == "WebSocketTimeoutException"


def _ws_url(model: str) -> str:
    base = livekit_tts.inference_url().rstrip("/")
    if base.startswith(("http://", "https://")):
        base = base.replace("http", "ws", 1)
    return f"{base}/stt?{urlencode({'model': model})}"


def transcribe_pcm(pcm: bytes, sample_rate: int, lang: str = "en", stats: dict | None = None) -> str:
    """Transcribe 16-bit mono PCM. Raises LiveKitSTTError.

    `stats`, if given, is filled with counts that help explain a slow or partial transcript.
    """
    stats = stats if stats is not None else {}
    key, secret = livekit_tts.credentials()
    if not (key and secret):
        raise LiveKitSTTError("LiveKit isn't configured")
    if not pcm:
        raise LiveKitSTTError("No audio")
    model = model_for(lang)
    language = (lang or "en").split("-")[0].lower()
    session = {
        "type": "session.create",
        "model": model,
        "settings": {"sample_rate": str(sample_rate), "encoding": "pcm_s16le", "language": language, "extra": {}},
        "connection": {"timeout": TIMEOUT, "retries": 0},
    }
    began = time.monotonic()
    deadline = began + TIMEOUT
    token = livekit_tts.access_token(key, secret)
    try:
        ws = livekit_tts._connect(_ws_url(model), [f"Authorization: Bearer {token}"], TIMEOUT)
    except Exception as e:
        raise LiveKitSTTError(f"Couldn't reach LiveKit Inference: {type(e).__name__}: {e}") from e

    finals: list[str] = []
    state = {"created": False, "finalized": False, "closed": False, "interim": ""}
    stats.update(finals=0, interims=0)

    def handle(raw):
        if not raw or isinstance(raw, bytes):
            return
        msg = json.loads(raw)
        kind = msg.get("type")
        if kind == "session.created":
            state["created"] = True
        elif kind == "final_transcript":
            stats["finals"] += 1
            text = (msg.get("transcript") or "").strip()
            if text:
                finals.append(text)
            state["interim"] = ""
        elif kind in ("interim_transcript", "preflight_transcript"):
            stats["interims"] += 1
            state["interim"] = (msg.get("transcript") or "").strip()
        elif kind == "session.finalized":
            state["finalized"] = True
        elif kind == "session.closed":
            state["closed"] = True
        elif kind == "error":
            raise LiveKitSTTError(f"LiveKit Inference error: {str(msg.get('message') or msg.get('code') or msg)[:300]}")

    def drain(until: float):
        """Handle whatever the gateway sends until `until` (monotonic seconds)."""
        while not state["closed"]:
            wait = until - time.monotonic()
            if wait <= 0:
                return
            ws.settimeout(wait)
            try:
                handle(ws.recv())
            except Exception as e:
                if _is_timeout(e):
                    return
                raise

    try:
        ws.send(json.dumps(session))
        # Audio sent before the gateway has its provider connection ready can be lost, so wait
        # (briefly) for session.created before the first frame.
        created_by = time.monotonic() + CREATE_WAIT
        while not state["created"] and time.monotonic() < created_by and not state["closed"]:
            drain(min(created_by, time.monotonic() + 0.05))
        stats["created_ms"] = round((time.monotonic() - began) * 1000)
        # Send a little faster than real time: a burst of the whole clip can lose words.
        step = sample_rate * 2 * FRAME_MS // 1000
        sent_from = time.monotonic()
        for n, i in enumerate(range(0, len(pcm), step)):
            drain(sent_from + n * FRAME_MS / 1000 / SEND_SPEED)
            ws.send(json.dumps({"type": "input_audio", "audio": base64.b64encode(pcm[i:i + step]).decode("ascii")}))
        ws.send(json.dumps({"type": "session.finalize"}))
        # Providers work at about real time, so the last words can arrive well after the first
        # final. Stop soon after the gateway confirms the finalize; failing that, once there's
        # text and a quiet gap after the audio could have been fully heard.
        heard_by = sent_from + len(pcm) / (2 * sample_rate)
        while not state["closed"]:
            now = time.monotonic()
            if now >= deadline:
                if finals or state["interim"]:
                    break
                raise LiveKitSTTError("LiveKit Inference timed out")
            if state["finalized"]:
                wait = max(TRAILING_WAIT, heard_by - now)
            elif finals or state["interim"]:
                wait = max(IDLE_WAIT, heard_by - now)
            else:
                wait = deadline - now
            before = (stats["finals"], stats["interims"], state["finalized"])
            drain(min(deadline, now + wait))
            if (stats["finals"], stats["interims"], state["finalized"]) == before and (state["finalized"] or finals or state["interim"]):
                break  # a quiet gap: nothing more is coming
    except LiveKitSTTError:
        raise
    except Exception as e:
        raise LiveKitSTTError(f"LiveKit Inference failed: {type(e).__name__}: {e}") from e
    finally:
        try:
            ws.send(json.dumps({"type": "session.close"}))
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass
    if state["interim"]:  # words the provider heard but never finalized
        finals.append(state["interim"])
    return " ".join(finals).strip()
