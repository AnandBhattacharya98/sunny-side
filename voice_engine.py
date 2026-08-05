import os
import re
import json
import difflib
from datetime import datetime
from db import get_conn, DB_PATH
from ai_engine import get_fallback_gemini_key, _call_gemini, ANTHROPIC_API_KEY, GEMINI_API_KEY

def classify_intent_and_slot(transcript: str, jobs_snapshot: list[dict], chat_history: list[dict] = None, api_key: str = None) -> dict:
    """
    Classifies a spoken transcript into a structured intent and slot dictionary.
    Falls back to a local rules-based parsing engine if no API keys are available.
    """
    transcript_clean = transcript.strip().lower()
    if not transcript_clean:
        return {"intent": None, "slots": {}, "ambiguous": False}

    key_to_use = api_key or get_fallback_gemini_key()
    
    # Try using Gemini if API key exists
    if key_to_use:
        try:
            res = _gemini_classify(transcript, jobs_snapshot, chat_history, key_to_use)
            if res and res.get("intent"):
                return res
        except Exception as e:
            print(f"Gemini voice classification failed: {e}. Falling back to local rules.")
            
    # Fallback to local parsing engine
    return _local_classify(transcript_clean, jobs_snapshot, chat_history)


def _gemini_classify(transcript: str, jobs_snapshot: list[dict], chat_history: list[dict], api_key: str) -> dict:
    history_str = ""
    if chat_history:
        history_str = "\n    CONVERSATION HISTORY:\n"
        for turn in chat_history:
            role = "User" if turn.get("role") == "user" else "Assistant"
            text = turn.get("text", "")
            history_str += f"    - {role}: \"{text}\"\n"

    prompt = f"""
    You are the voice assistant router for the Job Hunter Board.
    Analyze the user's spoken transcript, slot-fill any references to jobs in the provided snapshot, and return a JSON object.
    You must use the CONVERSATION HISTORY (if present) to resolve context, pronouns (like "it", "that", "that one", "first one"), and references from previous turns.
    {history_str}
    
    SUPPORTED INTENTS:
    - "pipeline_stats": stats queries (e.g. "How is my pipeline?", "what is my average score?")
    - "column_count": jobs in a stage (e.g. "how many jobs am I shortlisted for?"). Required slot: "column" (must be one of: "whatsapp", "new", "shortlisted", "interviewing", "applied", "offer", "rejected")
    - "job_lookup": query details about a job (e.g. "tell me about the Swiggy job"). Required slot: "job_id"
    - "job_fit": why a job scored what it did (e.g. "why did the CRED job get an 8?"). Required slot: "job_id"
    - "job_status": status check (e.g. "Did I apply to Zepto yet?"). Required slot: "job_id"
    - "email_lookup": emails from a company (e.g. "Any replies from Razorpay?"). Required slot: "company" (string) or "job_id"
    - "email_count": count of synced emails (e.g. "how many emails do I have?")
    - "last_sync": last scraping check time (e.g. "When did I last refresh?")
    - "top_matches": top scoring jobs (e.g. "What are my top 3 jobs right now?")
    - "cover_letter_status": check cover letter (e.g. "Do I have a cover letter for Meesho?"). Required slot: "job_id"
    - "move_job": move a card (e.g. "Move the Razorpay job to applied"). Required slots: "job_id", "status" (must be one of: "whatsapp", "new", "shortlisted", "interviewing", "applied", "offer", "rejected")
    - "thumbs_up": like a job (e.g. "I like the Groww job"). Required slot: "job_id"
    - "thumbs_down": dislike/thumbs down a job (e.g. "Not interested in the PhonePe job"). Required slot: "job_id"
    - "trigger_refresh": check for new jobs (e.g. "refresh my listings")
    - "regenerate_cover_letter": refresh cover letter (e.g. "regenerate the cover letter for Swiggy"). Required slot: "job_id"
    - "send_email": email cover letter (e.g. "send the application to Razorpay"). Required slot: "job_id"
    - "archive_job": remove/archive a job (e.g. "remove the BrowserStack job"). Required slot: "job_id"
    - "quiz_mode": open the quiz tab/mode for a job card (e.g. "quiz me on this", "open quiz for Swiggy", "practice interview"). Required slot: "job_id"
    
    JOBS SNAPSHOT (last 50 active jobs):
    {json.dumps(jobs_snapshot)}
    
    TRANSCRIPT:
    "{transcript}"
    
    Resolve references like "the Swiggy job" or "Razorpay" to the closest matching job_id from the JOBS SNAPSHOT.
    If a reference is ambiguous (e.g. two jobs match Swiggy), resolve to the best candidate but flag it by setting "ambiguous": true.
    
    Return EXACTLY a JSON object with this structure. Do not add markdown fences:
    {{
      "intent": "intent_name" or null if unrecognized,
      "slots": {{
         "job_id": "resolved_job_id" or null,
         "column": "resolved_column" or null,
         "status": "resolved_status_column" or null,
         "company": "resolved_company" or null
      }},
      "ambiguous": true/false
    }}
    """
    res_text = _call_gemini(prompt, response_json=True, api_key=api_key)
    if res_text.startswith("```"):
        res_text = re.sub(r"^```(?:json)?\n|```$", "", res_text, flags=re.MULTILINE)
    return json.loads(res_text.strip())


def _local_classify(transcript: str, jobs_snapshot: list[dict], chat_history: list[dict] = None) -> dict:
    slots = {"job_id": None, "column": None, "status": None, "company": None}
    intent = None
    
    # 1. Resolve job reference if any
    matched_job = _resolve_job_locally(transcript, jobs_snapshot)
    
    # If no direct match in transcript, try resolving from previous turns (context)
    if not matched_job and chat_history:
        for turn in reversed(chat_history):
            turn_text = turn.get("text", "").lower()
            prev_match = _resolve_job_locally(turn_text, jobs_snapshot)
            if prev_match:
                matched_job = prev_match
                break
                
    if matched_job:
        slots["job_id"] = matched_job["job_id"]
        slots["company"] = matched_job["company"]
        
    # 2. Match intents
    if "refresh" in transcript or "check for new" in transcript or "sync" in transcript:
        intent = "trigger_refresh"
    elif "stats" in transcript or "average" in transcript or "pipeline" in transcript:
        intent = "pipeline_stats"
    elif "email" in transcript or "message" in transcript or "reply" in transcript or "replies" in transcript:
        if "count" in transcript or "how many" in transcript:
            intent = "email_count"
        else:
            intent = "email_lookup"
    elif "when did" in transcript or "last sync" in transcript or "last updated" in transcript or "last refresh" in transcript:
        intent = "last_sync"
    elif "top" in transcript or "best" in transcript or "recommend" in transcript or "match" in transcript:
        intent = "top_matches"
    elif "cover letter" in transcript or "letter" in transcript:
        if "regen" in transcript or "write" in transcript or "create" in transcript or "redo" in transcript:
            intent = "regenerate_cover_letter"
        elif "send" in transcript or "mail" in transcript or "apply" in transcript:
            intent = "send_email"
        else:
            intent = "cover_letter_status"
    elif "like" in transcript or "thumbs up" in transcript or "interested" in transcript:
        # Check if we are disliking
        if "not" in transcript or "don't" in transcript or "dislike" in transcript:
            intent = "thumbs_down"
        else:
            intent = "thumbs_up"
    elif "not interested" in transcript or "thumbs down" in transcript:
        intent = "thumbs_down"
    elif "quiz" in transcript or "practice" in transcript or "interview prep" in transcript or "coach" in transcript or "test me" in transcript or "ask me" in transcript:
        intent = "quiz_mode"
    elif "remove" in transcript or "delete" in transcript or "archive" in transcript:
        intent = "archive_job"
    elif "move" in transcript or "put" in transcript or "set" in transcript or "drag" in transcript:
        intent = "move_job"
        # Resolve destination status
        for col_name in ["whatsapp", "new", "shortlisted", "interviewing", "applied", "offer", "rejected"]:
            if col_name in transcript or col_name.replace("list", "") in transcript:
                slots["status"] = col_name
                break
            if "self import" in transcript and col_name == "whatsapp":
                slots["status"] = "whatsapp"
                break
            if "inbox" in transcript and col_name == "new":
                slots["status"] = "new"
                break
    elif "status" in transcript or "where is" in transcript:
        intent = "job_status"
    elif "why" in transcript or "fit" in transcript or "score" in transcript:
        intent = "job_fit"
    elif slots["job_id"]:
        intent = "job_lookup"
        
    # Check column counts
    if "how many" in transcript or "count" in transcript:
        for col_name in ["whatsapp", "new", "shortlisted", "interviewing", "applied", "offer", "rejected"]:
            if col_name in transcript or col_name.replace("list", "") in transcript:
                intent = "column_count"
                slots["column"] = col_name
                break
            if "self import" in transcript:
                intent = "column_count"
                slots["column"] = "whatsapp"
                break
            if "inbox" in transcript:
                intent = "column_count"
                slots["column"] = "new"
                break

    return {"intent": intent, "slots": slots, "ambiguous": False}


def _resolve_job_locally(transcript: str, jobs_snapshot: list[dict]) -> dict:
    if not jobs_snapshot:
        return None
        
    best_match = None
    max_score = 0
    
    for job in jobs_snapshot:
        company_lower = job["company"].lower()
        title_lower = job["title"].lower()
        
        score = 0
        # Exact company name match in transcript
        if company_lower in transcript:
            score += 10
            
        # Partial words overlap
        company_words = company_lower.split()
        for w in company_words:
            if len(w) > 2 and w in transcript:
                score += 3
                
        title_words = title_lower.replace("—", " ").replace("-", " ").split()
        for w in title_words:
            if len(w) > 3 and w in transcript:
                score += 2
                
        if score > max_score:
            max_score = score
            best_match = job
            
    if max_score >= 3:
        return best_match
    return None

def pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000, num_channels: int = 1, bits_per_sample: int = 16) -> bytes:
    import struct
    num_samples = len(pcm_data) // (bits_per_sample // 8)
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + len(pcm_data),
        b'WAVE',
        b'fmt ',
        16,
        1,  # PCM format
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b'data',
        len(pcm_data)
    )
    return header + pcm_data
