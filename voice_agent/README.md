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

## Real-time Sunny (board chat and Quiz Mode)

The same agent also plays Sunny when the board's mic is tapped, and the Quiz Mode coach when practice starts. The website sends `mode: "sunny"` (with a board snapshot) or `mode: "quiz"` (with the questions) in the job metadata; the code is in [`sunny_live.py`](sunny_live.py).

Sunny never touches the database. When she needs something done, such as moving a job, explaining a score or grading an answer, she asks the browser over LiveKit RPC, and the browser runs it through the same endpoints the typed chat uses. Confirmations, quiz scoring and the summary all behave as they do without live mode. Board changes show up after the live chat ends, because reloading the board would hang up the call.

Sunny speaks with the site's voice (`SUNNY_TTS_VOICE`, Cartesia Sonic 3) in the user's chosen language. Speech-to-text is Deepgram Nova 3 `multi` for English, Hindi, Spanish, French and German, and Cartesia Ink Whisper for the others (`SUNNY_STT_MODEL` overrides it). The quiz coach waits longer before replying (`QUIZ_MIN_ENDPOINTING_DELAY` 1.0 s, `QUIZ_MAX_ENDPOINTING_DELAY` 5.0 s), so thinking pauses don't cut an answer off.

## Mock interviews

On the Calls page, **Mock interview** sends Maya in as a hiring manager for one job on the user's board (metadata `mode: "interview"`, code in [`interviewer.py`](interviewer.py)). She plans 4 or 7 main questions from the posting, its key requirements and the resume, asks at most one follow-up per answer, gives no feedback during the call, and hangs up after closing.

The browser records the call (your mic and Maya mixed) into IndexedDB on that device, with timed captions. When it ends, the website scores every main answer from the transcript (Gemini, then LiveKit's LLM), writes a 1 to 5 score with what worked, what to sharpen and a stronger answer, and adds the overall score to the job's Timeline. In the call log, **Show report and replay** shows the report, plays the recording from each answer, and lets you click any transcript line to jump there. Recordings never leave the browser.

## Phone practice (LiveKit SIP)

The same recruiter can take a real phone call, two ways. Both log the call on the Calls page when it ends: the agent posts the transcript to the website, signed with a shared secret.

**1. Shared secret (needed for both).** Make a long random string (`python -c "import secrets; print(secrets.token_urlsafe(32))"`) and set it as `SUNNY_AGENT_SECRET` on Render and on the agent (`lk agent update-secrets --secrets SUNNY_AGENT_SECRET=... --secrets SUNNY_SITE_URL=https://pm-job-hunter-qfva.onrender.com`).

**2a. Dial in.** In LiveKit Cloud, open Telephony, buy a phone number, then create a dispatch rule for it:

```json
{
  "dispatch_rule": {
    "name": "Sunny-Side practice line",
    "rule": { "dispatchRuleIndividual": { "roomPrefix": "phone-in-" } },
    "roomConfig": { "agents": [{ "agentName": "sunny-screener" }] }
  }
}
```

(`lk sip dispatch create rule.json` does the same from the CLI.) Set `LIVEKIT_SIP_NUMBER` on Render to the number as you want it shown. On the Calls page, **Get my code** gives a six-digit code that works once for ten minutes; the caller types it on the keypad or says it, and gets three tries per call. The room prefix matters: the agent treats `phone-in-` rooms as dial-in calls.

**2b. Call me.** Create an outbound trunk with a SIP provider that can call your country (Twilio, Telnyx, Plivo and Exotel all work with LiveKit; LiveKit's own numbers are US only), then `lk sip outbound create trunk.json`. Set the trunk id (`ST_...`) as `LIVEKIT_SIP_OUTBOUND_TRUNK_ID` on Render. The user types their number on the Calls page, the server starts a room with the agent, and the agent rings them. It's limited to three calls an hour per user and to the country codes in `PHONE_PRACTICE_COUNTRIES` (default `+91,+1,+44`), because every call costs money.

On a phone call the agent uses LiveKit's telephony noise cancellation and can hang up after saying goodbye.

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
