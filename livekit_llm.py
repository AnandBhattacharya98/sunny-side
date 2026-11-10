"""Text generation through LiveKit Inference (https://docs.livekit.io/agents/models/llm/).

Uses the same LiveKit Cloud keys and gateway as Sunny's voice (see livekit_tts.py). The gateway
speaks the OpenAI chat completions API, so this is one small HTTPS call with no extra SDK. It lets
quiz feedback be written by a model even when nobody has added a Gemini key.
"""
import os
import json

import requests

import livekit_tts

DEFAULT_MODEL = "openai/gpt-4.1-mini"
TIMEOUT = 25


class LiveKitLLMError(Exception):
    pass


def is_configured() -> bool:
    return livekit_tts.is_configured()


def model() -> str:
    return (os.getenv("LIVEKIT_LLM_MODEL") or "").strip() or DEFAULT_MODEL


def _parse_stream(resp) -> str:
    """Join the text of an SSE chat completions stream."""
    out = []
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if chunk.get("error"):
            raise LiveKitLLMError(str(chunk["error"])[:300])
        for choice in chunk.get("choices") or []:
            piece = (choice.get("delta") or {}).get("content") or (choice.get("message") or {}).get("content")
            if piece:
                out.append(piece)
    return "".join(out)


def complete(prompt: str, temperature: float = 0.7, max_tokens: int = 1400, system: str = "") -> str:
    """Return the model's reply to `prompt`. Raises LiveKitLLMError."""
    key, secret = livekit_tts.credentials()
    if not (key and secret):
        raise LiveKitLLMError("LiveKit isn't configured")
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": model(), "messages": messages, "temperature": temperature,
            "max_tokens": max_tokens, "stream": True}
    url = livekit_tts.inference_url().rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {livekit_tts.access_token(key, secret)}",
               "Content-Type": "application/json", "User-Agent": "sunny-side"}
    try:
        with requests.post(url, json=body, headers=headers, timeout=TIMEOUT, stream=True) as resp:
            if resp.status_code >= 400:
                raise LiveKitLLMError(f"LiveKit Inference LLM returned HTTP {resp.status_code}: {resp.text[:200]}")
            if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
                text = _parse_stream(resp)
            else:
                data = resp.json()
                text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except LiveKitLLMError:
        raise
    except Exception as e:  # never echo headers: they carry the token
        raise LiveKitLLMError(f"LiveKit Inference LLM failed: {type(e).__name__}") from e
    if not text.strip():
        raise LiveKitLLMError("LiveKit Inference LLM returned no text")
    return text
