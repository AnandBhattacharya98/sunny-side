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


def transcribe_pcm(pcm: bytes, sample_rate: int, lang: str = "en") -> str:
    """Transcribe 16-bit mono PCM. Raises LiveKitSTTError."""
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
    deadline = time.monotonic() + TIMEOUT
    token = livekit_tts.access_token(key, secret)
    try:
        ws = livekit_tts._connect(_ws_url(model), [f"Authorization: Bearer {token}"], TIMEOUT)
    except Exception as e:
        raise LiveKitSTTError(f"Couldn't reach LiveKit Inference: {type(e).__name__}: {e}") from e

    finals: list[str] = []
    try:
        ws.send(json.dumps(session))
        step = sample_rate * 2 * FRAME_MS // 1000
        for i in range(0, len(pcm), step):
            ws.send(json.dumps({"type": "input_audio", "audio": base64.b64encode(pcm[i:i + step]).decode("ascii")}))
        ws.send(json.dumps({"type": "session.finalize"}))
        # Providers can transcribe slower than we upload, so a final for the first words can come
        # well before the rest. Stop soon after the gateway confirms the finalize; failing that,
        # once there's text and a quiet gap after the audio could have been fully heard.
        heard_by = time.monotonic() + len(pcm) / (2 * sample_rate)
        finalized = False
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                if finals:
                    break
                raise LiveKitSTTError("LiveKit Inference timed out")
            if finalized:
                wait = max(TRAILING_WAIT, heard_by - now)
            elif finals:
                wait = max(IDLE_WAIT, heard_by - now)
            else:
                wait = remaining
            ws.settimeout(min(remaining, wait))
            try:
                raw = ws.recv()
            except Exception as e:
                if (finalized or finals) and _is_timeout(e):
                    break  # nothing more is coming
                raise
            if not raw:
                break
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "final_transcript":
                text = (msg.get("transcript") or "").strip()
                if text:
                    finals.append(text)
            elif kind == "session.finalized":
                finalized = True
            elif kind == "session.closed":
                break
            elif kind == "error":
                raise LiveKitSTTError(f"LiveKit Inference error: {str(msg.get('message') or msg.get('code') or msg)[:300]}")
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
    return " ".join(finals).strip()
