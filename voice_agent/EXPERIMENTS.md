# Setting experiments

Change one setting in `.env.local`, restart the agent (`python agent.py dev`), have the same short
conversation each time, and fill in a row. The agent logs its active settings when each call starts,
so you can check which values were live.

Use the same script every time so the runs are comparable, for example:
1. Answer the greeting with "yes, go ahead".
2. Say "I need around five lakh... for my sister's wedding" with a real pause at the "...".
3. While the bot is answering, say "hmm" once, then later talk over it with "sorry, actually it's six lakh".
4. Play some background noise (a fan, a TV, music) for one answer.

## Baseline (defaults)

| Setting | Value |
|---|---|
| Voice | default |
| Turn detection | model, endpointing 0.5 to 3.0 s |
| Interruptions | on, 0.5 s, 0 words |
| Noise cancellation | on |
| Preemptive generation | on |

Notes on baseline:

## Experiment 1: Voice

| | |
|---|---|
| What I changed | `TTS_VOICE` from default to `...` |
| What I expected | |
| What happened | |

## Experiment 2: Turn detection

Suggested: `TURN_DETECTION=vad` with `MIN_ENDPOINTING_DELAY=0.3`. Pause mid-sentence at "five lakh...".

| | |
|---|---|
| What I changed | |
| What I expected | (with `vad`, it should cut in during the pause because only silence counts) |
| What happened | |

## Experiment 3: Interruption

Suggested: `ALLOW_INTERRUPTIONS=false`, then separately `MIN_INTERRUPTION_WORDS=2`.

| | |
|---|---|
| What I changed | |
| What I expected | (off: it keeps talking over "sorry, actually..."; 2 words: "hmm" no longer stops it, a real sentence does) |
| What happened | |

## Optional extras

- `NOISE_CANCELLATION=false` with a TV on: does the bot hear phantom words or reply to the TV?
- `PREEMPTIVE_GENERATION=false`: does the reply start noticeably later?
