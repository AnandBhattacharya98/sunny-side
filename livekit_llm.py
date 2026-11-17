"""Text generation through LiveKit Inference (https://docs.livekit.io/agents/models/llm/).

Uses the same LiveKit Cloud keys and gateway as Sunny's voice (see livekit_tts.py). The gateway
speaks the OpenAI chat completions API, so this is one small HTTPS call with no extra SDK. It lets
quiz feedback be written by a model even when nobody has added a Gemini key.
"""
import os
import json
import re

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
    # Read bytes and decode as UTF-8 ourselves: the stream's Content-Type has no charset, so requests would
    # fall back to ISO-8859-1 and turn "user's" (curly apostrophe) into "userâ€\x99s".
    for line in resp.iter_lines():
        raw = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
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


def repair_mojibake(text):
    """Undoes UTF-8 text that was decoded as ISO-8859-1 or cp1252 ("â€\x99" back to "’"). Each suspicious run is
    converted on its own and kept as it is unless it turns into valid UTF-8, so correct text is never changed."""
    if not isinstance(text, str) or not re.search(r"[\u00c2-\u00f4]", text):
        return text

    def fix(m):
        run = m.group(0)
        for enc in ("latin-1", "cp1252"):
            try:
                return run.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
        return run

    # cp1252 maps 0x80-0x9f to symbols like € and ™, so widen the run to those before fixing
    return re.sub(r"[\u00c2-\u00f4][\u0080-\u00bf\u2010-\u203a\u20ac\u2122\u0152\u0153\u0160\u0161\u0178\u017d\u017e\u0192\u02c6\u02dc]+",
                  fix, text)


def repair_json_text(raw: str) -> str:
    """repair_mojibake over every string inside a stored JSON value; returns the input when it isn't JSON."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw

    def walk(v):
        if isinstance(v, str):
            return repair_mojibake(v)
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v

    fixed = walk(data)
    return raw if fixed == data else json.dumps(fixed)


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
