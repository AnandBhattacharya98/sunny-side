"""Career Switch: suggests realistic career pivots away from the role a user is already targeting.

Gemini reads the resume and proposes paths when a key is available. Without one, a built-in map of
adjacent careers is used, keyed on the user's current role family."""
import hashlib
import json
import re
import threading
import time

from ai_engine import _call_gemini, _title_similarity, _title_tokens

# Each family lists the words that identify it and the careers people from it most often move into.
# "query" is what we search job boards for; "carry" are skills that transfer; "gaps" are what to learn.
ROLE_FAMILIES = {
    "product": {
        "match": ["product manager", "product owner", "apm", "product lead", "product management"],
        "paths": [
            {"role": "UX Researcher", "query": "UX Researcher", "why": "You already talk to users and turn what they say into decisions.",
             "carry": ["User interviews", "Problem framing", "Prioritisation"], "gaps": ["Research methods", "Usability testing"],
             "first_step": "Run and write up one usability study on a product you use."},
            {"role": "Product Marketing Manager", "query": "Product Marketing Manager", "why": "You know how products create value; PMMs tell that story to the market.",
             "carry": ["Positioning", "Customer insight", "Launches"], "gaps": ["Messaging", "Go-to-market campaigns"],
             "first_step": "Rewrite the launch page for a feature you shipped."},
            {"role": "Program Manager", "query": "Program Manager", "why": "Coordinating teams to ship is most of the job.",
             "carry": ["Roadmapping", "Stakeholder management", "Delivery"], "gaps": ["Formal planning tools", "Risk tracking"],
             "first_step": "Get familiar with a planning tool like Jira Advanced Roadmaps or MS Project."},
            {"role": "Management Consultant", "query": "Strategy Consultant", "why": "Structured problem solving and business cases carry straight over.",
             "carry": ["Business cases", "Analysis", "Executive communication"], "gaps": ["Case interviews", "Industry depth"],
             "first_step": "Practise five case interviews."},
        ],
    },
    "engineering": {
        "match": ["software engineer", "developer", "sde", "backend", "frontend", "full stack", "fullstack", "programmer", "devops"],
        "paths": [
            {"role": "Product Manager", "query": "Technical Product Manager", "why": "Engineers who move to PM bring credibility with the team and sharper trade-off calls.",
             "carry": ["Technical judgement", "Shipping", "Systems thinking"], "gaps": ["User research", "Prioritisation frameworks"],
             "first_step": "Write a one-page spec for a feature your team should build next."},
            {"role": "Solutions Engineer", "query": "Solutions Engineer", "why": "You get to stay technical while working with customers.",
             "carry": ["Debugging", "APIs and integrations", "Explaining systems"], "gaps": ["Demos", "Sales process"],
             "first_step": "Record a 5 minute demo of a tool you built."},
            {"role": "Developer Advocate", "query": "Developer Advocate", "why": "Writing, talks and sample code are the job, and you already think like the audience.",
             "carry": ["Coding", "Teaching teammates", "Docs"], "gaps": ["Public speaking", "Content writing"],
             "first_step": "Publish one technical blog post."},
            {"role": "Data Engineer", "query": "Data Engineer", "why": "Most of the work is backend engineering pointed at data pipelines.",
             "carry": ["Python or SQL", "Distributed systems", "Testing"], "gaps": ["Spark or dbt", "Data modelling"],
             "first_step": "Build a small pipeline with dbt on a public dataset."},
        ],
    },
    "data": {
        "match": ["data analyst", "data scientist", "business analyst", "analytics", "bi analyst", "machine learning", "ml engineer"],
        "paths": [
            {"role": "Product Manager", "query": "Product Manager", "why": "You already find what moves the numbers; PMs decide what to build from it.",
             "carry": ["Metrics", "Experimentation", "SQL"], "gaps": ["Roadmapping", "Working with design"],
             "first_step": "Turn one analysis you did into a product proposal."},
            {"role": "Growth Marketing Manager", "query": "Growth Marketing Manager", "why": "Growth teams are run on funnels, experiments and attribution.",
             "carry": ["A/B testing", "Funnel analysis", "Dashboards"], "gaps": ["Paid channels", "Copywriting"],
             "first_step": "Run a small paid campaign with a fixed budget and report the results."},
            {"role": "Strategy and Operations", "query": "Strategy and Operations", "why": "BizOps teams want people who can model a business and find the levers.",
             "carry": ["Modelling", "Forecasting", "Storytelling with data"], "gaps": ["Operating cadence", "Cross-team projects"],
             "first_step": "Build a driver model for a business you know well."},
            {"role": "Risk Analyst", "query": "Risk Analyst", "why": "Fintech and lending risk teams hire for the same statistics skills.",
             "carry": ["Statistics", "SQL", "Modelling"], "gaps": ["Credit or fraud domain", "Regulation basics"],
             "first_step": "Read up on credit scorecards and build one on a public dataset."},
        ],
    },
    "design": {
        "match": ["designer", "ux designer", "ui designer", "ui/ux", "product design", "visual design", "graphic design"],
        "paths": [
            {"role": "Product Manager", "query": "Product Manager", "why": "Designers already own the user problem; PM adds the business side.",
             "carry": ["User empathy", "Prototyping", "Collaboration"], "gaps": ["Metrics", "Prioritisation"],
             "first_step": "Pair with a PM on one roadmap decision."},
            {"role": "UX Researcher", "query": "UX Researcher", "why": "Research is the part of design many designers love most.",
             "carry": ["Interviews", "Synthesis", "Usability tests"], "gaps": ["Quantitative methods", "Survey design"],
             "first_step": "Run a mixed-methods study with a survey and five interviews."},
            {"role": "Front-end Developer", "query": "Frontend Developer", "why": "You already think in components and states.",
             "carry": ["Design systems", "Layout", "Accessibility"], "gaps": ["JavaScript", "A framework like React"],
             "first_step": "Build one of your own designs in React."},
            {"role": "Content Designer", "query": "Content Designer", "why": "Words are part of the interface, and teams hire for it on its own.",
             "carry": ["Information architecture", "User flows", "Clarity"], "gaps": ["UX writing style guides"],
             "first_step": "Rewrite the error messages in an app you use."},
        ],
    },
    "marketing": {
        "match": ["marketing", "brand", "seo", "content marketing", "social media", "growth"],
        "paths": [
            {"role": "Product Marketing Manager", "query": "Product Marketing Manager", "why": "A natural step closer to product, with the same storytelling skills.",
             "carry": ["Messaging", "Campaigns", "Market research"], "gaps": ["Product depth", "Sales enablement"],
             "first_step": "Write a competitive battlecard for your current product."},
            {"role": "Product Manager", "query": "Product Manager", "why": "You know the customer and the market, which is half of PM.",
             "carry": ["Customer insight", "Experimentation", "Launches"], "gaps": ["Working with engineering", "Specs"],
             "first_step": "Write a spec for a feature your customers keep asking for."},
            {"role": "Customer Success Manager", "query": "Customer Success Manager", "why": "Retention and expansion use the same lifecycle thinking.",
             "carry": ["Lifecycle", "Communication", "Data-driven decisions"], "gaps": ["Account management", "Renewals"],
             "first_step": "Shadow a CSM on three customer calls."},
            {"role": "Marketing Analyst", "query": "Marketing Analyst", "why": "If you liked the numbers part of campaigns, make it the job.",
             "carry": ["Attribution", "Reporting", "Testing"], "gaps": ["SQL", "Statistics"],
             "first_step": "Learn enough SQL to rebuild your main campaign report."},
        ],
    },
    "sales": {
        "match": ["sales", "account executive", "business development", "bdr", "sdr", "account manager"],
        "paths": [
            {"role": "Customer Success Manager", "query": "Customer Success Manager", "why": "Same relationships, with less quota pressure and more long-term work.",
             "carry": ["Relationship building", "Negotiation", "Product knowledge"], "gaps": ["Onboarding programs", "Health scores"],
             "first_step": "Map the onboarding journey of your last five customers."},
            {"role": "Partnerships Manager", "query": "Partnerships Manager", "why": "Deal-making at the company level instead of per customer.",
             "carry": ["Negotiation", "Pipeline management", "Networking"], "gaps": ["Partner programs", "Co-marketing"],
             "first_step": "Draft a partner proposal for a company that sells to your customers."},
            {"role": "Sales Operations", "query": "Sales Operations", "why": "You know where the process breaks; ops fixes it with data and tools.",
             "carry": ["CRM", "Forecasting", "Process"], "gaps": ["Salesforce admin", "Analytics"],
             "first_step": "Get the Salesforce Administrator certification."},
            {"role": "Product Marketing Manager", "query": "Product Marketing Manager", "why": "You've heard every objection, which is what PMMs write for.",
             "carry": ["Objection handling", "Pitching", "Customer insight"], "gaps": ["Messaging frameworks", "Launch planning"],
             "first_step": "Turn your best pitch into a one-page messaging doc."},
        ],
    },
    "support": {
        "match": ["customer success", "customer support", "support", "customer service", "client servicing", "helpdesk"],
        "paths": [
            {"role": "Account Manager", "query": "Account Manager", "why": "You already keep customers happy; account management adds growth.",
             "carry": ["Customer empathy", "Problem solving", "Product knowledge"], "gaps": ["Upselling", "Negotiation"],
             "first_step": "Find one upsell opportunity in your accounts and pitch it."},
            {"role": "Product Operations", "query": "Product Operations", "why": "You know exactly where the product hurts customers.",
             "carry": ["Feedback triage", "Process", "Tooling"], "gaps": ["Product analytics", "Roadmap process"],
             "first_step": "Build a monthly report of the top customer issues for the product team."},
            {"role": "Implementation Consultant", "query": "Implementation Consultant", "why": "Set-up and onboarding projects use your product knowledge directly.",
             "carry": ["Onboarding", "Training", "Troubleshooting"], "gaps": ["Project management", "Integrations"],
             "first_step": "Own one customer onboarding end to end."},
            {"role": "QA Analyst", "query": "QA Analyst", "why": "You already find and reproduce bugs better than most.",
             "carry": ["Reproducing issues", "Attention to detail", "Writing clear reports"], "gaps": ["Test automation", "Test planning"],
             "first_step": "Learn the basics of Playwright or Selenium."},
        ],
    },
    "operations": {
        "match": ["operations", "ops manager", "supply chain", "logistics", "procurement"],
        "paths": [
            {"role": "Program Manager", "query": "Program Manager", "why": "Running complex operations is program management by another name.",
             "carry": ["Execution", "Process design", "Cross-team coordination"], "gaps": ["Tech program tooling"],
             "first_step": "Write up one process you improved as a case study with numbers."},
            {"role": "Product Manager", "query": "Product Manager", "why": "Ops-heavy companies (logistics, quick commerce) want PMs who know the floor.",
             "carry": ["Process knowledge", "Metrics", "Stakeholders"], "gaps": ["Specs", "Working with engineers"],
             "first_step": "Spec a tool that would remove one manual step in your team."},
            {"role": "Business Analyst", "query": "Business Analyst", "why": "You know the processes; BAs document and improve them with data.",
             "carry": ["Process mapping", "Reporting", "Requirements"], "gaps": ["SQL", "BI tools"],
             "first_step": "Build a dashboard of your team's key metrics."},
            {"role": "Strategy and Operations", "query": "Strategy and Operations", "why": "A step up into planning and company-level priorities.",
             "carry": ["Execution", "Problem solving", "Analysis"], "gaps": ["Financial modelling"],
             "first_step": "Take a short course on financial modelling."},
        ],
    },
    "finance": {
        "match": ["finance", "accountant", "accounting", "chartered accountant", "auditor", "financial analyst", "investment"],
        "paths": [
            {"role": "FP&A to Strategy", "query": "Strategy Manager", "why": "Strategy teams need people who understand the numbers behind decisions.",
             "carry": ["Financial modelling", "Budgeting", "Analysis"], "gaps": ["Market research", "Presenting to leadership"],
             "first_step": "Write a one-page investment memo for a new product line."},
            {"role": "Product Manager (Fintech)", "query": "Product Manager Fintech", "why": "Fintech products need domain experts in payments, lending and compliance.",
             "carry": ["Domain knowledge", "Compliance", "Analysis"], "gaps": ["Product discovery", "Agile"],
             "first_step": "Map the user journey for a payments or lending product."},
            {"role": "Data Analyst", "query": "Data Analyst", "why": "Excel modelling skills move straight into SQL and BI.",
             "carry": ["Modelling", "Reporting", "Attention to detail"], "gaps": ["SQL", "Python basics"],
             "first_step": "Rebuild a monthly finance report in SQL."},
            {"role": "Risk and Compliance", "query": "Risk Manager", "why": "Audit and controls experience is the core of risk roles.",
             "carry": ["Controls", "Regulation", "Audit"], "gaps": ["Risk frameworks"],
             "first_step": "Read the RBI or your regulator's latest risk guidelines."},
        ],
    },
    "hr": {
        "match": ["recruiter", "talent acquisition", "hr", "human resources", "people partner", "people operations"],
        "paths": [
            {"role": "People Analytics", "query": "People Analytics", "why": "HR decisions are increasingly data-led, and you know the context.",
             "carry": ["HR processes", "Surveys", "Reporting"], "gaps": ["SQL", "Statistics"],
             "first_step": "Analyse your team's hiring funnel and share the findings."},
            {"role": "Account Executive", "query": "Account Executive", "why": "Recruiting is sales: pipeline, pitching and closing.",
             "carry": ["Pipeline management", "Persuasion", "Negotiation"], "gaps": ["Product demos", "Quota planning"],
             "first_step": "Shadow a sales rep for a day."},
            {"role": "Learning and Development", "query": "Learning and Development", "why": "Training and onboarding are a natural next step.",
             "carry": ["Onboarding", "Communication", "Coaching"], "gaps": ["Instructional design"],
             "first_step": "Design a two-hour onboarding module."},
            {"role": "Customer Success Manager", "query": "Customer Success Manager", "why": "Managing relationships and expectations is the same skill set.",
             "carry": ["Relationships", "Communication", "Organisation"], "gaps": ["SaaS metrics", "Product depth"],
             "first_step": "Learn the basics of SaaS churn and retention metrics."},
        ],
    },
    "project": {
        "match": ["project manager", "program manager", "scrum master", "delivery manager", "pmo"],
        "paths": [
            {"role": "Product Manager", "query": "Product Manager", "why": "Move from how things get built to what gets built.",
             "carry": ["Delivery", "Stakeholders", "Planning"], "gaps": ["Discovery", "Product metrics"],
             "first_step": "Run one discovery interview series with users."},
            {"role": "Operations Manager", "query": "Operations Manager", "why": "Running teams and processes day to day.",
             "carry": ["Process", "People coordination", "Reporting"], "gaps": ["P&L ownership"],
             "first_step": "Own a recurring process and its metrics."},
            {"role": "Management Consultant", "query": "Management Consultant", "why": "Consultancies staff delivery-minded people on transformations.",
             "carry": ["Structured planning", "Client management", "Risk"], "gaps": ["Case interviews"],
             "first_step": "Practise five case interviews."},
            {"role": "Customer Success Manager", "query": "Customer Success Manager", "why": "Enterprise CS is project management with customers.",
             "carry": ["Planning", "Communication", "Escalations"], "gaps": ["Commercial skills"],
             "first_step": "Lead one customer rollout project."},
        ],
    },
    "teaching": {
        "match": ["teacher", "lecturer", "professor", "tutor", "trainer", "educator", "faculty"],
        "paths": [
            {"role": "Instructional Designer", "query": "Instructional Designer", "why": "You already design learning; edtech companies hire for it.",
             "carry": ["Curriculum design", "Assessment", "Clear explanation"], "gaps": ["E-learning tools"],
             "first_step": "Build one short course in a tool like Articulate or Notion."},
            {"role": "Learning and Development", "query": "Learning and Development Specialist", "why": "Corporate training uses the same skills with adult learners.",
             "carry": ["Facilitation", "Coaching", "Planning"], "gaps": ["Business context"],
             "first_step": "Run a workshop for a local company or community group."},
            {"role": "Customer Success Manager", "query": "Customer Success Manager", "why": "Onboarding and training customers is a big part of CS.",
             "carry": ["Teaching", "Patience", "Communication"], "gaps": ["SaaS metrics"],
             "first_step": "Learn a SaaS product well enough to teach it."},
            {"role": "Content Writer", "query": "Content Writer", "why": "Explaining hard things simply is what good content does.",
             "carry": ["Writing", "Research", "Clarity"], "gaps": ["SEO basics"],
             "first_step": "Publish three explainers in your subject."},
        ],
    },
    "writing": {
        "match": ["writer", "content", "editor", "copywriter", "journalist"],
        "paths": [
            {"role": "Content Designer / UX Writer", "query": "UX Writer", "why": "Product teams pay for clear, short words in the interface.",
             "carry": ["Clarity", "Editing", "Audience"], "gaps": ["UX basics", "Figma"],
             "first_step": "Rewrite the onboarding of an app you use."},
            {"role": "Product Marketing Manager", "query": "Product Marketing Manager", "why": "Messaging and storytelling are the core of PMM.",
             "carry": ["Storytelling", "Research", "Copy"], "gaps": ["Go-to-market", "Product depth"],
             "first_step": "Write a launch blog for a product you like."},
            {"role": "Technical Writer", "query": "Technical Writer", "why": "Docs teams hire writers who can learn technical topics quickly.",
             "carry": ["Writing", "Research", "Structure"], "gaps": ["APIs", "Docs tooling"],
             "first_step": "Write a getting-started guide for an open source project."},
            {"role": "Communications Manager", "query": "Communications Manager", "why": "Internal and external comms need strong writers.",
             "carry": ["Writing", "Editing", "Deadlines"], "gaps": ["PR", "Stakeholder management"],
             "first_step": "Draft a press release for a recent launch you admire."},
        ],
    },
}

# Used when the role doesn't match any family: broad, beginner-friendly moves.
GENERAL_PATHS = [
    {"role": "Customer Success Manager", "query": "Customer Success Manager", "why": "Valued for people skills over a specific background, and open to career changers.",
     "carry": ["Communication", "Problem solving"], "gaps": ["SaaS basics"], "first_step": "Learn how SaaS companies measure retention."},
    {"role": "Business Analyst", "query": "Business Analyst", "why": "Every industry needs people who can turn data into decisions.",
     "carry": ["Domain knowledge", "Analysis"], "gaps": ["SQL", "Excel modelling"], "first_step": "Take a short SQL course."},
    {"role": "Project Coordinator", "query": "Project Coordinator", "why": "Organisation and follow-through transfer from almost any job.",
     "carry": ["Organisation", "Communication"], "gaps": ["Project tools"], "first_step": "Get comfortable with Jira or Asana."},
    {"role": "Operations Associate", "query": "Operations Associate", "why": "Fast-growing companies hire generalists who get things done.",
     "carry": ["Execution", "Reliability"], "gaps": ["Spreadsheets", "Process mapping"], "first_step": "Map and improve one process you do today."},
]

MAX_PATHS = 5
_CACHE_TTL = 6 * 3600
_cache = {}
_cache_lock = threading.Lock()


def detect_family(role_text: str) -> str | None:
    """Best family for a role or resume snippet, by the longest matching phrase."""
    text = f" {(role_text or '').lower()} "
    best, best_len = None, 0
    for name, fam in ROLE_FAMILIES.items():
        for phrase in fam["match"]:
            if re.search(rf"\b{re.escape(phrase)}\b", text) and len(phrase) > best_len:
                best, best_len = name, len(phrase)
    return best


# Words that name a different kind of work. "Product Marketing Manager" is a switch from "Product Manager",
# while "Senior Product Manager, Payments" is not.
_FUNCTION_WORDS = {
    "marketing", "design", "designer", "research", "researcher", "analyst", "analytics", "engineer", "engineering",
    "developer", "scientist", "data", "sales", "operations", "ops", "support", "success", "finance", "accountant",
    "recruiter", "hr", "people", "consultant", "writer", "content", "program", "project", "account", "partnerships",
    "qa", "test", "security", "legal", "advocate", "solutions", "strategy", "teacher", "trainer",
}


def is_current_track(title: str, current_role: str, path_query: str = "") -> bool:
    """True when a posting is basically the role the user already targets, so it isn't a switch.
    With a path_query, a posting that looks more like the current role than the new path is dropped too."""
    if not current_role:
        return False
    cur, tok = _title_tokens(current_role), _title_tokens(title)
    if cur and cur <= tok and not ((tok - cur) & _FUNCTION_WORDS):
        return True
    return bool(path_query) and _title_similarity(title, current_role) > _title_similarity(title, path_query)


def _clean_path(p: dict) -> dict | None:
    if not isinstance(p, dict):
        return None
    role = str(p.get("role") or "").strip()[:80]
    if not role:
        return None

    def strs(v, n):
        return [str(x).strip()[:60] for x in (v if isinstance(v, list) else []) if str(x).strip()][:n]

    return {
        "role": role,
        "query": (str(p.get("query") or p.get("search_query") or role).strip())[:80],
        "why": str(p.get("why") or "").strip()[:300],
        "carry": strs(p.get("carry") or p.get("transferable_skills"), 4),
        "gaps": strs(p.get("gaps"), 3),
        "first_step": str(p.get("first_step") or "").strip()[:200],
    }


def local_paths(current_role: str, resume_text: str = "") -> list[dict]:
    family = detect_family(current_role) or detect_family((resume_text or "")[:600])
    paths = ROLE_FAMILIES[family]["paths"] if family else GENERAL_PATHS
    out = [dict(p) for p in paths if not is_current_track(p["query"], current_role)]
    return out[:MAX_PATHS]


def ai_paths(current_role: str, resume_text: str, api_key: str) -> list[dict] | None:
    prompt = f"""You are a career coach. The candidate below currently targets "{current_role or 'their current role'}".
Suggest {MAX_PATHS} realistic career switches into DIFFERENT roles, not a more senior or slightly renamed version of what they do now.
Favour roles where their existing skills transfer and that are hiring in India.

CANDIDATE RESUME:
{(resume_text or '(No resume provided.)')[:4000]}

Reply ONLY with JSON: a list of objects, each exactly
{{"role": "<target role>", "query": "<2-4 word job board search for it>", "why": "<one sentence on why this fits them>",
"carry": ["<skill they already have>", "..."], "gaps": ["<skill to build>", "..."], "first_step": "<one concrete thing to do this week>"}}"""
    try:
        data = json.loads(_call_gemini(prompt, response_json=True, api_key=api_key))
        if isinstance(data, dict):
            data = data.get("paths") or data.get("suggestions") or []
        paths = [c for c in (_clean_path(p) for p in data) if c and not is_current_track(c["query"], current_role)]
        return paths[:MAX_PATHS] or None
    except Exception as e:
        print(f"  [career switch AI fallback] {e}")
        return None


def suggest_paths(user_id: int, current_role: str, resume_text: str, api_key: str = "", refresh: bool = False) -> dict:
    """Career paths for a user. AI results are cached per user and resume so reloading the page is free."""
    key = hashlib.sha256(f"{user_id}|{current_role}|{resume_text}".encode()).hexdigest()
    now = time.time()
    if api_key and not refresh:
        with _cache_lock:
            hit = _cache.get(user_id)
            if hit and hit[0] == key and now - hit[1] < _CACHE_TTL:
                return hit[2]
    paths = ai_paths(current_role, resume_text, api_key) if api_key else None
    result = {"paths": paths, "mode": "ai"} if paths else {"paths": local_paths(current_role, resume_text), "mode": "local"}
    if result["mode"] == "ai":
        with _cache_lock:
            _cache[user_id] = (key, now, result)
    return result
