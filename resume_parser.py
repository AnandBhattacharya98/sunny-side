import re
import json
from ai_engine import _call_gemini, get_fallback_gemini_key

def parse_resume(raw_text: str, api_key: str = None) -> dict:
    """
    Main entry point for parsing resume text.
    First tries to use Gemini if a key is available, falls back to local rule-based parsing.
    """
    if not raw_text or not raw_text.strip():
        return {
            "skills": [],
            "titles": [],
            "years_experience": 0.0,
            "domains": [],
            "tools": [],
            "education": [],
            "seniority": "associate"
        }

    key_to_use = api_key or get_fallback_gemini_key()
    if key_to_use:
        try:
            return parse_resume_ai(raw_text, key_to_use)
        except Exception as e:
            print(f"AI parsing failed, falling back to local: {e}")
            # Fallback to local parsing on AI failure
            
    return parse_resume_local(raw_text)

def parse_resume_ai(raw_text: str, api_key: str) -> dict:
    prompt = f"""
    You are an expert resume parsing AI. Extract the following structured fields from the raw resume text:
    - skills: list of technical and soft skills (e.g. Python, SQL, Project Roadmapping, User Research)
    - titles: list of professional job titles held (e.g. AI Associate Product Manager, Software Engineer)
    - years_experience: float representing total years of professional experience (e.g. 2.5)
    - domains: list of industry domains (e.g. fintech, conversational AI, BFSI, SaaS)
    - tools: list of software tools, frameworks, and platforms (e.g. Retell, Deepgram, Jira, Git, Figma)
    - education: list of objects with "degree" and "school" keys (e.g. [{{"degree": "MBA & MS IT Mgmt", "school": "UT Dallas"}}] )
    - seniority: single string representation of overall career level ("intern", "associate", "mid", "senior", "director", "executive")
    
    Return a JSON object conforming exactly to this schema. DO NOT wrap with markdown blocks.
    
    Raw Resume Text:
    {raw_text}
    """
    raw_res = _call_gemini(prompt, response_json=True, api_key=api_key)
    # Strip potential markdown fences
    if raw_res.startswith("```"):
        raw_res = re.sub(r"^```(?:json)?\n|```$", "", raw_res, flags=re.MULTILINE)
    return json.loads(raw_res.strip())

def parse_resume_local(raw_text: str) -> dict:
    # 1. Years of experience detection
    exp_matches = re.findall(r"(\d+(?:\.\d+)?)\+?\s*(?:years?|yrs?)\b", raw_text, re.IGNORECASE)
    years_exp = 0.0
    if exp_matches:
        try:
            years_exp = max(float(x) for x in exp_matches)
        except ValueError:
            pass

    # 2. Skills and tools keyword mapping
    skills_lexicon = [
        "python", "sql", "javascript", "java", "c++", "c#", "go", "rust", "ruby", "php", "typescript",
        "product roadmapping", "product management", "user research", "agile", "scrum", "kanban", "jira", "confluence",
        "stakeholder management", "go-to-market", "gtm", "market research", "ab testing", "a/b testing", "data analytics",
        "tableau", "power bi", "looker", "mixpanel", "amplitude", "figma", "sketch", "wireframing", "prototyping",
        "machine learning", "deep learning", "nlp", "llm", "conversational ai", "prompt engineering", "retell", "deepgram",
        "bfsi", "fintech", "saas", "edtech", "healthcare", "e-commerce", "retail", "cloud computing", "aws", "gcp", "azure",
        "docker", "kubernetes", "git", "github", "ci/cd", "devops"
    ]
    
    extracted_skills = []
    extracted_tools = []
    extracted_domains = []
    
    tools_list = ["jira", "confluence", "tableau", "power bi", "looker", "mixpanel", "amplitude", "figma", "sketch", "retell", "deepgram", "docker", "kubernetes", "git", "github"]
    domains_list = ["bfsi", "fintech", "saas", "edtech", "healthcare", "e-commerce", "retail"]
    
    for word in skills_lexicon:
        pattern = rf"\b{re.escape(word)}\b"
        if re.search(pattern, raw_text, re.IGNORECASE):
            name = word.upper() if len(word) <= 3 else word.title()
            if word in tools_list:
                extracted_tools.append(name)
            elif word in domains_list:
                extracted_domains.append(name)
            else:
                extracted_skills.append(name)

    # 3. Title detection from first 15 lines
    lines = [line.strip() for line in raw_text.split("\n") if line.strip()][:15]
    detected_titles = []
    title_keywords = [
        "product manager", "pm", "product lead", "director of product", "associate product manager", "apm",
        "software engineer", "developer", "data scientist", "data analyst", "system administrator",
        "scrum master", "product owner", "project manager", "program manager", "intern"
    ]
    for line in lines:
        for keyword in title_keywords:
            if re.search(rf"\b{re.escape(keyword)}\b", line, re.IGNORECASE):
                if len(line) < 60:
                    detected_titles.append(line)
                    break

    # 4. Seniority detection
    seniority = "associate"
    lower_text = raw_text.lower()
    if any(k in lower_text for k in ["intern", "apprenticeship"]):
        seniority = "intern"
    elif any(k in lower_text for k in ["director", "head of", "vp", "vice president"]):
        seniority = "director"
    elif any(k in lower_text for k in ["senior", "sr.", "sr manager", "lead"]):
        seniority = "senior"
    elif any(k in lower_text for k in ["principal", "staff"]):
        seniority = "senior"
    elif years_exp >= 5.0:
        seniority = "senior"
    elif years_exp >= 2.0:
        seniority = "mid"

    return {
        "skills": extracted_skills,
        "titles": detected_titles,
        "years_experience": years_exp,
        "domains": extracted_domains,
        "tools": extracted_tools,
        "education": [],
        "seniority": seniority
    }
