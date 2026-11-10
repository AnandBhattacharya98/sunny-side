"""Mock interview: Maya runs a role-specific interview built from the job posting and the resume.

The website sends mode "interview" with the role, company, a posting excerpt, the resume excerpt and
how many main questions to ask. Maya interviews; she doesn't coach during the call. The browser
records the call and posts the transcript, and the website scores each answer afterwards.
"""
from livekit.agents import Agent
from livekit.agents.beta.tools import EndCallTool

import sunny_live

INTERVIEW_PROMPT = """You are Maya, an experienced hiring manager running a realistic mock interview over a voice
call. The candidate is practising for a real interview for the role below, so be a real interviewer:
warm but professional, curious, and a little probing. This is a call, not a chat: short sentences,
one question at a time, no lists, markdown or emojis.

Plan the interview from the job posting and the resume:
- Ask exactly {questions} main questions, then close. Mix them: one opener about their background
  for this role, behavioural questions ("tell me about a time..."), and role-specific questions drawn
  from the posting's actual responsibilities and requirements. Tie at least one to something on their resume.
- After an answer, ask at most one short follow-up when it was vague, had no example or no result
  ("What was the outcome?", "What did you do, specifically?"). Then move on.
- Don't give feedback or scores during the interview; a neutral "Thanks" or "Got it" is fine.
- If they ask to repeat or rephrase, do it. If they want a moment, wait.
- If they ask how they did, say they'll get a detailed, scored report on the Calls page right after.

Close: invite their questions for you and answer briefly and generically, thank them, say goodbye,
then call end_call. If they want to stop early, close politely and call end_call.
Never promise a job, an interview or a salary."""


def interview_details(data: dict) -> dict:
    clip = lambda k, n: " ".join(str(data.get(k) or "").split())[:n]
    try:
        questions = max(2, min(int(data.get("questions") or 4), 10))
    except (TypeError, ValueError):
        questions = 4
    return {"lang": data.get("lang") if data.get("lang") in sunny_live.LANG_NAMES else "en",
            "identity": str(data.get("identity") or ""), "candidate_name": clip("candidate_name", 60),
            "role": clip("role", 120), "company": clip("company", 80), "posting": clip("posting", 2500),
            "key_reqs": clip("key_reqs", 600), "resume": str(data.get("resume") or "")[:1500],
            "questions": questions}


def instructions_for(d: dict) -> str:
    parts = [INTERVIEW_PROMPT.format(questions=d["questions"]),
             "# Language\n" + sunny_live._lang_rule(d["lang"]),
             "# This interview",
             f"- Candidate: {d['candidate_name'] or 'unknown; ask their name'}",
             f"- Role: {d['role'] or 'not given; ask which role'}" + (f" at {d['company']}" if d["company"] else "")]
    if d["key_reqs"]:
        parts.append(f"- Key requirements: {d['key_reqs']}")
    if d["posting"]:
        parts.append(f"- Job posting (excerpt): {d['posting']}")
    if d["resume"]:
        parts.append(f"- Resume excerpt (draw on it, don't read it out): {d['resume']}")
    return "\n".join(parts)


class Interviewer(Agent):
    def __init__(self, details: dict) -> None:
        super().__init__(instructions=instructions_for(details),
                         tools=[EndCallTool(ignore_on_enter=True, delete_room=True)])
        self.details = details

    async def on_enter(self) -> None:
        d = self.details
        self.session.generate_reply(instructions=(
            "Greet the candidate by first name if you know it, introduce yourself as Maya, the hiring manager"
            + (f" at {d['company']}" if d["company"] else "")
            + f", say this is an interview for the {d['role'] or 'role'} and will take about "
            + f"{d['questions'] * 2} minutes, and ask if they're ready."))
