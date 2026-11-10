"""LiveKit Inference TTS for Sunny's voice. Run with: python -m pytest tests/

No network: the gateway WebSocket is replaced by a fake that speaks the same protocol."""
import base64
import hashlib
import hmac
import json

import pytest

import livekit_tts
import voice_engine as ve
import dashboard
from db import get_conn, DB_PATH


class FakeWS:
    def __init__(self, replies):
        self.sent = []
        self.replies = list(replies)
        self.closed = False

    def send(self, data):
        self.sent.append(json.loads(data))

    def settimeout(self, t):
        pass

    def recv(self):
        return json.dumps(self.replies.pop(0))

    def close(self):
        self.closed = True


def audio_msg(pcm):
    return {"type": "output_audio", "audio": base64.b64encode(pcm).decode(), "session_id": "s1"}


@pytest.fixture
def livekit(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIkey123")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-xyz")
    for name in ("LIVEKIT_TTS_MODEL", "LIVEKIT_TTS_VOICE", "LIVEKIT_INFERENCE_URL", "LIVEKIT_URL",
                 "LIVEKIT_INFERENCE_API_KEY", "LIVEKIT_INFERENCE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ve, "_livekit_down_until", 0.0)
    ve._tts_cache._data.clear()
    state = {"replies": [audio_msg(b"\x01\x00" * 10), audio_msg(b"\x02\x00" * 5), {"type": "done"}], "calls": []}

    def connect(url, headers, timeout):
        ws = FakeWS(state["replies"])
        state["calls"].append({"url": url, "headers": headers, "ws": ws})
        return ws
    monkeypatch.setattr(livekit_tts, "_connect", connect)
    return state


def test_not_configured_without_keys(monkeypatch):
    for name in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "LIVEKIT_INFERENCE_API_KEY", "LIVEKIT_INFERENCE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert not livekit_tts.is_configured()
    with pytest.raises(livekit_tts.LiveKitTTSError):
        livekit_tts.synthesize_pcm("hi")


def test_access_token_is_signed_with_inference_grant():
    token = livekit_tts.access_token("APIkey123", "secret-xyz")
    head, body, sig = token.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    expected = hmac.new(b"secret-xyz", f"{head}.{body}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(pad(sig)) == expected
    claims = json.loads(base64.urlsafe_b64decode(pad(body)))
    assert claims["iss"] == "APIkey123" and claims["inference"] == {"perform": True}
    assert claims["exp"] > claims["nbf"]


def test_model_and_voice_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("LIVEKIT_TTS_MODEL", raising=False)
    monkeypatch.delenv("LIVEKIT_TTS_VOICE", raising=False)
    assert livekit_tts.model_and_voice() == ("cartesia/sonic-3", livekit_tts.DEFAULT_VOICE)
    monkeypatch.setenv("LIVEKIT_TTS_MODEL", "inworld/inworld-tts-2:Ashley")
    assert livekit_tts.model_and_voice() == ("inworld/inworld-tts-2", "Ashley")
    monkeypatch.setenv("LIVEKIT_TTS_VOICE", "Olivia")
    assert livekit_tts.model_and_voice() == ("inworld/inworld-tts-2", "Olivia")


def test_synthesize_speaks_the_gateway_protocol(livekit):
    pcm, rate = livekit_tts.synthesize_pcm("Hello   there!", lang="hi")
    assert pcm == b"\x01\x00" * 10 + b"\x02\x00" * 5 and rate == 24000
    call = livekit["calls"][0]
    assert call["url"] == "wss://agent-gateway.livekit.cloud/v1/tts?model=cartesia%2Fsonic-3"
    assert call["headers"][0].startswith("Authorization: Bearer ")
    create, transcript, flush = call["ws"].sent
    assert create["type"] == "session.create" and create["encoding"] == "pcm_s16le"
    assert create["voice"] == livekit_tts.DEFAULT_VOICE and create["language"] == "hi"
    assert transcript["type"] == "input_transcript" and transcript["transcript"] == "Hello there! "
    assert flush == {"type": "session.flush"}
    assert call["ws"].closed


def test_gateway_error_raises(livekit):
    livekit["replies"][:] = [{"type": "error", "message": "voice not found"}]
    with pytest.raises(livekit_tts.LiveKitTTSError, match="voice not found"):
        livekit_tts.synthesize_pcm("hi")


def test_speak_prefers_livekit_and_caches(livekit, monkeypatch):
    monkeypatch.setattr(ve, "gemini_generate", lambda *a, **k: pytest.fail("Gemini shouldn't be called"))
    wav, provider = ve.speak("Okay, cancelled.", "gemini-key", lang="en")
    assert provider == "livekit" and wav[:4] == b"RIFF"
    livekit["replies"][:] = []  # a second call would fail if it reached the gateway
    assert ve.speak("Okay, cancelled.", "gemini-key", lang="en") == (wav, "livekit")
    assert len(livekit["calls"]) == 1


def test_speak_falls_back_to_gemini_when_livekit_fails(livekit, monkeypatch):
    livekit["replies"][:] = [{"type": "error", "message": "quota"}]
    monkeypatch.setattr(ve, "synthesize_speech", lambda text, key: b"RIFFgemini")
    assert ve.speak("Hi there", "gemini-key") == (b"RIFFgemini", "gemini")
    # LiveKit is skipped for a minute after a failure, so the next reply isn't held up
    assert ve.speak("Hi again", "gemini-key") == (b"RIFFgemini", "gemini")
    assert len(livekit["calls"]) == 1


@pytest.fixture
def client():
    ve.rate_limiter.reset()
    conn = get_conn(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (id, username, password_hash, name) VALUES (301, 'lk301', 'x', 'LK')")
    conn.commit()
    conn.close()
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        with c.session_transaction() as s:
            s["user_id"] = 301
        yield c


def test_synthesize_route_uses_livekit_without_a_gemini_key(livekit, client, monkeypatch):
    monkeypatch.setattr(dashboard, "_voice_api_key", lambda conn, uid: None)
    res = client.post("/api/voice/synthesize", json={"text": "Namaste", "lang": "hi"})
    data = res.get_json()
    assert res.status_code == 200 and data["provider"] == "livekit"
    assert base64.b64decode(data["audio_base64"])[:4] == b"RIFF"
    assert livekit["calls"][0]["ws"].sent[0]["language"] == "hi"
    welcome = client.get("/api/voice/welcome?lang=en").get_json()
    assert welcome["server_tts"] is True and welcome["server_voice"] is False


def test_synthesize_route_without_any_provider(client, monkeypatch):
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    monkeypatch.setattr(dashboard, "_voice_api_key", lambda conn, uid: None)
    res = client.post("/api/voice/synthesize", json={"text": "Hello"})
    assert res.status_code == 400 and res.get_json()["provider_fallback"] is True
