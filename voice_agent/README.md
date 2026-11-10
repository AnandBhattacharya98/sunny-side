# Practice recruiter agent

The LiveKit voice agent behind the **Practice recruiter screen** on the Calls page. It plays Maya, a recruiter doing a three-minute first-round screen, using the candidate's name, target role and a resume excerpt that the website sends with the call. The prompt is in [`prompt.md`](prompt.md).

## How a practice call works

```
Calls page ──POST /api/calls/practice/start──► Sunny-Side (Render)
    │                                              │ mints a LiveKit room token that also
    │◄──────── url + token ────────────────────────┘ dispatches agent "sunny-screener"
    │
    └──WebRTC──► LiveKit Cloud room ◄── this agent (LiveKit Cloud or your laptop)
                     mic ─► noise cancellation ─► VAD ─► STT ─► turn detector ─► LLM ─► TTS ─► speaker
    │
    └──POST /api/calls/practice/end (duration + transcript)──► saved to the call log, summary written by Gemini
```

- **VAD** (Silero) hears that you're speaking, which drives interruptions.
- **STT** turns your audio into text as you speak; the captions on the page come from it.
- **Turn detector** looks at your words and pauses to decide whether you've finished.
- **LLM** reads the prompt, the call details and the conversation, and writes the reply.
- **TTS** speaks the reply, starting on the first sentence while the LLM is still writing.

STT, LLM and TTS go through LiveKit Inference, so the LiveKit keys Sunny already uses are the only keys needed.

## Run it

The website side needs `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` on Render (the same project as Sunny's voice). Then run the agent one of two ways.

**On your laptop** (good for experimenting):

```bash
cd voice_agent
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env.local      # paste the same LIVEKIT_URL / KEY / SECRET as Render
python -m livekit.agents download-files
python agent.py dev
```

Leave it running and start a practice call on the Calls page. The live site dispatches to whichever agent named `sunny-screener` is connected to the project.

**On LiveKit Cloud** (always on): install the [LiveKit CLI](https://docs.livekit.io/home/cli/), then from this folder run `lk cloud auth` and `lk agent create`. It builds the `Dockerfile` here. Change settings later with `lk agent update-secrets`.

You can also talk to it in a terminal with `python agent.py console`.

## Settings to experiment with

Change one at a time, restart, have the same short conversation, and write what happened in [`EXPERIMENTS.md`](EXPERIMENTS.md). The agent logs the active settings at the start of each call.

| Setting | Default | What it does |
|---|---|---|
| `TTS_VOICE` | Cartesia default | Which voice speaks. Paste a voice id from play.cartesia.ai. |
| `TURN_DETECTION` | `model` | How the agent decides you've finished: `model` (words + pauses), `vad` (silence only), `stt` (the STT provider's end of sentence). |
| `MIN_ENDPOINTING_DELAY` | `0.5` | Seconds of silence before it may reply. Lower is snappier and cuts in more. |
| `MAX_ENDPOINTING_DELAY` | `3.0` | Longest it waits when the turn detector thinks you're not done. |
| `ALLOW_INTERRUPTIONS` | `true` | Whether you can talk over the agent to stop it. |
| `MIN_INTERRUPTION_DURATION` | `0.5` | Seconds of your speech needed to stop it. |
| `MIN_INTERRUPTION_WORDS` | `0` | Words needed to stop it; `2` ignores "hmm" and "ok". |
| `NOISE_CANCELLATION` | `true` | LiveKit Cloud noise cancellation on your mic. |
| `PREEMPTIVE_GENERATION` | `true` | Start the reply before your turn is confirmed, to cut latency. |
| `STT_MODEL`, `STT_LANGUAGE`, `LLM_MODEL`, `TTS_MODEL` | see `.env.example` | Swap providers. `STT_LANGUAGE=multi` handles Hindi-English mixing. |

## Secrets

`LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` live only in `.env.local` (git-ignored), Render's environment and LiveKit Cloud secrets. Never commit them or show them in a screenshot or recording.
