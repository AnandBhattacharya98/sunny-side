"""Sunny's voice through LiveKit Inference text-to-speech (https://docs.livekit.io/agents/models/tts/).

LiveKit Inference gives one gateway to many TTS models (Cartesia, Inworld, Rime, Deepgram, ...)
billed through a LiveKit Cloud project, so no separate provider key is needed. The livekit-agents
SDK is ~400 MB and built for realtime rooms, so this module speaks the gateway's small WebSocket
protocol directly and returns a WAV the browser can play, like the Gemini voice does.

It is switched on by setting LIVEKIT_API_KEY and LIVEKIT_API_SECRET. When it isn't configured,
or a request fails, callers fall back to Gemini and then to the browser's own voice.
"""
import os
import re
import json
import time
import hmac
import base64
import hashlib
import logging
from urllib.parse import urlencode

log = logging.getLogger("voice.livekit")

DEFAULT_INFERENCE_URL = "https://agent-gateway.livekit.cloud/v1"
# Cartesia Sonic 3 speaks all of Sunny's languages (en hi ta te bn mr kn es fr de ar).
DEFAULT_MODEL = "cartesia/sonic-3"
DEFAULT_VOICE = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"  # Jacqueline, the voice in LiveKit's docs
SAMPLE_RATE = 24000
TIMEOUT = 15  # seconds for the whole synthesis


class LiveKitTTSError(Exception):
    pass


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return default


def credentials() -> tuple[str, str]:
    return (_env("LIVEKIT_INFERENCE_API_KEY", "LIVEKIT_API_KEY"),
            _env("LIVEKIT_INFERENCE_API_SECRET", "LIVEKIT_API_SECRET"))


def is_configured() -> bool:
    key, secret = credentials()
    return bool(key and secret)


def model_and_voice() -> tuple[str, str]:
    """LIVEKIT_TTS_MODEL may carry the voice too, as "provider/model:voice"."""
    model = _env("LIVEKIT_TTS_MODEL", default=DEFAULT_MODEL)
    voice = _env("LIVEKIT_TTS_VOICE")
    if ":" in model:
        model, _, inline_voice = model.partition(":")
        voice = voice or inline_voice
    if not voice and model == DEFAULT_MODEL:
        voice = DEFAULT_VOICE
    return model, voice


def inference_url() -> str:
    url = _env("LIVEKIT_INFERENCE_URL")
    if url:
        return url
    if ".staging.livekit.cloud" in _env("LIVEKIT_URL"):
        return "https://agent-gateway.staging.livekit.cloud/v1"
    return DEFAULT_INFERENCE_URL


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def access_token(api_key: str, api_secret: str, ttl: int = 600) -> str:
    """A LiveKit JWT with the inference grant, the same claims the agents SDK mints."""
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"iss": api_key, "sub": "sunny-side", "nbf": now - 10, "exp": now + ttl,
              "inference": {"perform": True}}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    sig = hmac.new(api_secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


def _ws_url(model: str) -> str:
    base = inference_url().rstrip("/")
    if base.startswith(("http://", "https://")):
        base = base.replace("http", "ws", 1)
    return f"{base}/tts?{urlencode({'model': model})}"


def _connect(url: str, headers: list[str], timeout: float):
    import websocket  # websocket-client; imported here so the app runs without it
    return websocket.create_connection(url, header=headers, timeout=timeout)


def stream_pcm(text: str, lang: str = "en"):
    """Yield 16-bit mono PCM chunks (at SAMPLE_RATE) as LiveKit produces them.

    Raises LiveKitTTSError, before the first chunk if the request can't start at all.
    """
    key, secret = credentials()
    if not (key and secret):
        raise LiveKitTTSError("LiveKit isn't configured")
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        raise LiveKitTTSError("No text to speak")
    model, voice = model_and_voice()
    language = (lang or "en").split("-")[0].lower()

    generation = {"model": model, "language": language}
    if voice:
        generation["voice"] = voice
    session = {"type": "session.create", "sample_rate": str(SAMPLE_RATE), "encoding": "pcm_s16le",
               "extra": {}, **generation, "connection": {"timeout": TIMEOUT, "retries": 0}}

    deadline = time.monotonic() + TIMEOUT
    try:
        ws = _connect(_ws_url(model), [f"Authorization: Bearer {access_token(key, secret)}"], TIMEOUT)
    except Exception as e:  # handshake errors carry the HTTP status, never our secret
        raise LiveKitTTSError(f"Couldn't reach LiveKit Inference: {type(e).__name__}: {e}") from e
    got_audio = False
    try:
        ws.send(json.dumps(session))
        ws.send(json.dumps({"type": "input_transcript", "transcript": text + " ",
                            "generation_config": generation, "extra": {}}))
        ws.send(json.dumps({"type": "session.flush"}))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LiveKitTTSError("LiveKit Inference timed out")
            ws.settimeout(remaining)
            raw = ws.recv()
            if not raw:
                raise LiveKitTTSError("LiveKit Inference closed the connection")
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "output_audio" and msg.get("audio"):
                got_audio = True
                yield base64.b64decode(msg["audio"])
            elif kind == "done":
                break
            elif kind == "error":
                raise LiveKitTTSError(f"LiveKit Inference error: {str(msg.get('message') or msg)[:300]}")
    except LiveKitTTSError:
        raise
    except GeneratorExit:
        raise
    except Exception as e:
        raise LiveKitTTSError(f"LiveKit Inference failed: {type(e).__name__}: {e}") from e
    finally:
        try:
            ws.close()
        except Exception:
            pass
    if not got_audio:
        raise LiveKitTTSError("LiveKit Inference returned no audio")


def synthesize_pcm(text: str, lang: str = "en") -> tuple[bytes, int]:
    """Speak `text` and return (16-bit mono PCM, sample rate). Raises LiveKitTTSError."""
    return b"".join(stream_pcm(text, lang)), SAMPLE_RATE
