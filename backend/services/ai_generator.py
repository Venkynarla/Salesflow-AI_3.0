"""
AI email generation service.
Uses NVIDIA's OpenAI-compatible API.

Drafting goal:
- Use manual About/enrichment first when provided.
- Infer only precise, title-relevant pain points.
- Write a tight outbound email around pain points, not generic service dumping.
"""

import json
import logging
import os
import re
from typing import Any, Dict

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
logger = logging.getLogger(__name__)

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
MODEL = os.getenv("NVIDIA_MODEL", "meta/llama-3.1-70b-instruct")

client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=NVIDIA_API_KEY or "not-configured",
)

ANTI_SPAM_RULES = """
Write like a real person emailing from their laptop, not like marketing copy.
Never use these words/phrases or close variants: free, guarantee, guaranteed, act now, limited time,
click here, buy now, risk-free, 100%, amazing, revolutionary, cutting-edge, game-changer, game changer,
synergy, world-class, best-in-class, unlock, supercharge, don't miss out, exclusive offer, transform your business.
Do not use exclamation marks. Do not write in ALL CAPS anywhere, including the subject line.
Do not open with "I hope this email finds you well" or any close variant.
Sentences should be short and plain, the way a busy professional actually writes email.
"""

INITIAL_SYSTEM = """
You are a senior enterprise B2B outreach strategist for Innominds.
Your job is NOT to write a generic sales email. Your job is to identify the prospect's most likely business/technical pain points from their title, company, and enrichment context, then write a short email around only those pain points.
""" + ANTI_SPAM_RULES + """
Strict rules:
1. Do NOT return JSON.
2. Return exactly this format:
SUBJECT: <short human subject, max 7 words>

BODY:
Hi <First Name>,

<paragraph 1: one specific observation from manual/LinkedIn context or title/company. No flattery.>

<paragraph 2: 2-3 precise pain points this person likely owns. Connect to Innominds only through those pain points.>

<paragraph 3: soft 15-minute CTA.>

Best,
<sender name>
3. Do not mention every Innominds service. Mention only the service angle that matches the pain points.
4. No buzzwords unless they are grounded in the prospect's role/context.
5. Do not use phrases like "I hope you're doing well", "game-changer", "cutting-edge", "synergy", "transform your business", "revolutionize".
6. Keep body under 140 words.
7. If enrichment is empty, use job title + company to infer conservative role-based pain points. Do not pretend you saw LinkedIn details.
8. Pain points must be valid, practical, and senior-friendly.
"""

def _clean_body(body: str, subject: str = "") -> str:
    body = (body or "").strip()
    body = body.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    if body.startswith("{") and "body" in body:
        try:
            data = json.loads(body)
            subject = data.get("subject", subject)
            body = data.get("body", body)
        except Exception:
            pass
    body = re.sub(r"^BODY\s*:\s*", "", body, flags=re.I).strip()
    body = re.sub(r"^Subject\s*:\s*.*?\n+", "", body, flags=re.I | re.S).strip()
    if subject:
        body = body.replace(subject, "").strip()
    body = body.replace("\\n", "\n")
    lines = [line.strip() for line in body.splitlines()]
    cleaned, blank = [], False
    for line in lines:
        if not line:
            if not blank:
                cleaned.append("")
            blank = True
        else:
            cleaned.append(line)
            blank = False
    return "\n".join(cleaned).strip()


def _parse_model_output(text: str) -> Dict[str, str]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Empty model response")
    raw = re.sub(r"^```(?:json|text)?", "", raw, flags=re.I).strip()
    raw = re.sub(r"```$", "", raw).strip()
    # Some model responses escape newlines as literal backslash-n text instead of
    # real newline characters. Normalize BEFORE running line-based regexes below,
    # otherwise SUBJECT's greedy match swallows the entire BODY on one "line".
    raw = raw.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            subject = str(data.get("subject", "Relevant pain points")).strip()
            body = _clean_body(str(data.get("body", "")).strip(), subject)
            return {"subject": subject, "body": body}
        except Exception:
            pass
    subject = "Relevant pain points"
    body = raw
    # Subject stops at the next real newline, a blank line, or a "BODY:" marker —
    # never swallows the rest of the message.
    m = re.search(r"SUBJECT\s*:\s*(.+?)(?:\n\s*\n|\n\s*BODY\s*:|\n|$)", raw, flags=re.I | re.S)
    if m:
        subject = m.group(1).strip().strip('"')
    m = re.search(r"BODY\s*:\s*(.*)", raw, flags=re.I | re.S)
    if m:
        body = m.group(1).strip()
    # Hard safety net: whatever the regex above captured, a subject line must never
    # contain a newline or leak into the greeting line (e.g. "...Novartis\n\nHi Raghava,").
    subject = subject.split("\n")[0].strip().strip('"').strip("'").strip()
    subject = re.sub(r"\s{2,}", " ", subject)
    return {"subject": subject, "body": _clean_body(body, subject)}


def _first_name_from_context(prospect_context: str) -> str:
    m = re.search(r"Name:\s*([^\n]+)", prospect_context or "")
    return m.group(1).strip().split()[0] if m else "there"


def _extract_line(prospect_context: str, label: str) -> str:
    m = re.search(rf"^{re.escape(label)}:\s*(.*)$", prospect_context or "", flags=re.I | re.M)
    return m.group(1).strip() if m else ""


def _role_pain_points(title: str, company: str, context: str) -> str:
    t = (title or "").lower()
    ctx = (context or "").lower()
    if any(x in t+ctx for x in ["pharmacovigilance", "safety", "pv"]):
        return "case intake triage, safety data quality, signal workflow visibility, inspection-ready automation"
    if any(x in t+ctx for x in ["data", "analytics", "bi", "insights", "rwe", "real world"]):
        return "trusted data pipelines, dashboard adoption, self-service analytics, lineage, observability, and AI-ready data foundations"
    if any(x in t+ctx for x in ["manufacturing", "quality", "operations", "supply", "plant", "mes", "scada"]):
        return "deviation reduction, batch visibility, predictive quality, MES/SCADA workflow friction, and shopfloor data integration"
    if any(x in t+ctx for x in ["digital", "product", "platform", "engineering", "technology", "cloud"]):
        return "platform reliability, cloud modernization, GenAI delivery, product engineering velocity, and governed automation"
    if any(x in t+ctx for x in ["clinical", "medical", "regulatory", "r&d", "research"]):
        return "document-heavy workflows, evidence synthesis, compliant GenAI assist, and fragmented clinical/scientific data"
    return "manual workflows, fragmented systems, data quality, AI adoption risk, and delivery velocity"


def _fallback_initial(sender_context: Dict[str, Any], prospect_context: str) -> Dict[str, str]:
    sender_name = sender_context.get("your_name") or os.getenv("EMAIL_FROM_NAME", "Venkat")
    sender_company = sender_context.get("your_company") or "Innominds"
    first = _first_name_from_context(prospect_context)
    title = _extract_line(prospect_context, "Title")
    company = _extract_line(prospect_context, "Company")
    pains = _role_pain_points(title, company, prospect_context)
    role_line = f"your role as {title}" if title else "your current charter"
    body = (
        f"Hi {first},\n\n"
        f"Noticed {role_line}{' at ' + company if company else ''}, and thought the pain points around {pains} may be relevant.\n\n"
        f"At {sender_company}, we typically help teams remove execution friction in exactly those areas — with focused AI, data, cloud, automation, and engineering work rather than broad consulting noise.\n\n"
        "Would a short 15-minute exchange make sense to compare notes?\n\n"
        f"Best,\n{sender_name}"
    )
    return {"subject": "Relevant pain points", "body": body}


async def generate_initial_email(prospect_context: str, sender_context: Dict[str, Any], original_subject: str | None = None) -> Dict[str, str]:
    if not NVIDIA_API_KEY:
        logger.warning("NVIDIA_API_KEY is missing. Returning fallback email.")
        return _fallback_initial(sender_context, prospect_context)
    title = _extract_line(prospect_context, "Title")
    company = _extract_line(prospect_context, "Company")
    inferred_pains = _role_pain_points(title, company, prospect_context)
    user_prompt = f"""
Prospect context. Manual About/enrichment may have been entered by the user and should be treated as the strongest signal:
{prospect_context}

Conservative role-based pain point hints:
{inferred_pains}

Sender context:
Name: {sender_context.get('your_name', '')}
Company: {sender_context.get('your_company', '')}
Role: {sender_context.get('your_role', '')}
Value proposition: {sender_context.get('value_proposition', '')}

Write a precise email that talks about ONLY the most likely pain points. Do not list services. Do not fabricate achievements. If manual/enrichment context exists, ground paragraph 1 in it. If not, clearly base the note on the title/company only.
""".strip()
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": INITIAL_SYSTEM}, {"role": "user", "content": user_prompt}],
            temperature=0.35,
            max_tokens=600,
        )
        result = _parse_model_output(response.choices[0].message.content or "")
        if not result.get("body"):
            raise ValueError("Model returned empty body")
        return result
    except Exception as e:
        logger.exception("NVIDIA email generation failed: %s", e)
        return _fallback_initial(sender_context, prospect_context)


FOLLOWUP_ANGLES = {
    1: "time_slots",
    2: "use_case",
    3: "fresh_angle",
    4: "breakup",
}


def _next_week_slots() -> str:
    import datetime as _dt
    today = _dt.date.today()
    days_ahead = (7 - today.weekday()) % 7 or 7  # next Monday
    next_monday = today + _dt.timedelta(days=days_ahead)
    tue = next_monday + _dt.timedelta(days=1)
    thu = next_monday + _dt.timedelta(days=3)
    return f"{tue.strftime('%A')} or {thu.strftime('%A')} next week"


def _followup_system(angle: str) -> str:
    base = "You are writing a brief, plain-text B2B follow-up email in reply to an earlier note." + ANTI_SPAM_RULES
    angle_instructions = {
        "time_slots": (
            "This follow-up should simply offer two concrete time windows to talk, tied to the same pain point as before. "
            "Keep it to 2-3 sentences total. Do not repeat the full pitch."
        ),
        "use_case": (
            "This follow-up should mention one short, generic (not overly specific/fabricated) example of the kind of work "
            "the sender's company does for similar roles/industries, tied to the same pain point. No fake client names or numbers. "
            "Keep it to 3-4 sentences."
        ),
        "fresh_angle": (
            "This follow-up should approach the same pain point from a different angle than a typical check-in — for example, "
            "a short relevant question, or a one-line observation about their role/industry. Keep it brief and low-pressure."
        ),
        "breakup": (
            "This is a final, low-pressure follow-up. Acknowledge you haven't heard back, leave the door open with no guilt-tripping, "
            "and say you won't keep following up. Keep it to 2 sentences plus a sign-off."
        ),
    }
    return base + "\n" + angle_instructions.get(angle, angle_instructions["fresh_angle"]) + """
Return exactly this format:
SUBJECT: Re: <original subject>

BODY:
Hi <First Name>,

<the follow-up body per the instructions above>

Best,
<sender name>
"""


def _fallback_followup(sender_context: Dict[str, Any], prospect_context: str, original_subject: str, angle: str) -> Dict[str, str]:
    first = _first_name_from_context(prospect_context)
    title = _extract_line(prospect_context, "Title")
    company = _extract_line(prospect_context, "Company")
    pains = _role_pain_points(title, company, prospect_context)
    sender_name = sender_context.get("your_name") or ""
    sender_company = sender_context.get("your_company") or ""
    subject = f"Re: {original_subject or 'Relevant pain points'}"
    if angle == "time_slots":
        slots = _next_week_slots()
        body = (
            f"Hi {first},\n\nFollowing up on my note below. Would {slots} work for a short call, "
            f"whichever is easier on your calendar?\n\nBest,\n{sender_name}"
        )
    elif angle == "use_case":
        body = (
            f"Hi {first},\n\nWanted to add one data point: we've done similar work for other "
            f"{title.split(',')[0] if title else 'teams'} dealing with {pains}. "
            f"Happy to share what that looked like if useful.\n\nBest,\n{sender_name}"
        )
    elif angle == "breakup":
        body = (
            f"Hi {first},\n\nHaven't heard back, so I'll leave this here for now. "
            f"If {pains} becomes a priority later, feel free to reach out.\n\nBest,\n{sender_name}"
        )
    else:
        body = (
            f"Hi {first},\n\nCircling back in case this got buried — still think {pains} is worth "
            f"a quick conversation whenever it's convenient.\n\nBest,\n{sender_name}"
        )
    return {"subject": subject, "body": body}


async def generate_followup_email(prospect_context: str, sender_context: Dict[str, Any], original_subject: str, followup_number: int) -> Dict[str, str]:
    angle = FOLLOWUP_ANGLES.get(followup_number, "fresh_angle")
    if not NVIDIA_API_KEY:
        return _fallback_followup(sender_context, prospect_context, original_subject, angle)
    extra = f"Suggested time windows if the angle is time_slots: {_next_week_slots()}." if angle == "time_slots" else ""
    user_prompt = f"""
Prospect context:
{prospect_context}

Sender: {sender_context.get('your_name', '')} at {sender_context.get('your_company', '')}
Original subject: {original_subject}
Follow-up number: {followup_number}
Angle for this follow-up: {angle}
{extra}

Write the follow-up now. Use SUBJECT/BODY format only.
""".strip()
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": _followup_system(angle)}, {"role": "user", "content": user_prompt}],
            temperature=0.5,
            max_tokens=380,
        )
        result = _parse_model_output(response.choices[0].message.content or "")
        if not result.get("subject", "").lower().startswith("re:"):
            result["subject"] = f"Re: {original_subject or result.get('subject','Relevant pain points')}"
        return result
    except Exception as e:
        logger.exception("NVIDIA follow-up generation failed: %s", e)
        return _fallback_followup(sender_context, prospect_context, original_subject, angle)


LINKEDIN_CONNECT_SYSTEM = """
You write LinkedIn connection request notes. LinkedIn limits these to 300 characters.
Return exactly this format, nothing else:
NOTE: <connection note, under 300 characters, personal, references one specific pain point or observation, no hard pitch, ends with a light reason to connect>
"""

LINKEDIN_MESSAGE_SYSTEM = ANTI_SPAM_RULES + """
You write a LinkedIn direct message sent AFTER a connection has been accepted.
Return exactly this format:
SUBJECT: LinkedIn message

BODY:
Hi <First Name>,

<2-3 short sentences: thank them for connecting, reference the same pain point thread as the email sequence, soft CTA for a quick call>

<sign-off with sender name>
"""

COLDCALL_SYSTEM = ANTI_SPAM_RULES + """
You write a cold call talk-track / script for an SDR to use on the phone.
Return exactly this format:
SUBJECT: Cold call script — <prospect name>

BODY:
OPENER: <8-12 second opener that states who you are and why you're calling>

HOOK: <one sentence tied to their specific pain point>

IF INTERESTED: <one or two lines to move to booking 15 minutes>

IF OBJECTION ("not interested" / "send me an email"): <one line rebuttal that respects their time>

VOICEMAIL (if no answer): <short voicemail script, under 20 seconds>
"""


async def _generate_simple(system_prompt: str, user_prompt: str, fallback: Dict[str, str], max_tokens: int = 400) -> Dict[str, str]:
    if not NVIDIA_API_KEY:
        return fallback
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.35,
            max_tokens=max_tokens,
        )
        result = _parse_model_output(response.choices[0].message.content or "")
        if not result.get("body"):
            raise ValueError("empty body")
        return result
    except Exception as e:
        logger.exception("NVIDIA generation failed: %s", e)
        return fallback


async def generate_linkedin_connect_note(prospect_context: str, sender_context: Dict[str, Any]) -> Dict[str, str]:
    first = _first_name_from_context(prospect_context)
    sender_name = sender_context.get("your_name") or "there"
    fallback_note = f"Hi {first}, enjoyed learning about your work — would love to connect and swap notes on how teams like yours are tackling delivery bottlenecks. — {sender_name}"
    user_prompt = f"Prospect context:\n{prospect_context}\n\nSender: {sender_name} at {sender_context.get('your_company','')}\nWrite the connection note now."
    if not NVIDIA_API_KEY:
        return {"subject": "LinkedIn connection note", "body": fallback_note[:300]}
    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": LINKEDIN_CONNECT_SYSTEM}, {"role": "user", "content": user_prompt}],
            temperature=0.4,
            max_tokens=180,
        )
        raw = (response.choices[0].message.content or "").strip()
        m = re.search(r"NOTE\s*:\s*(.*)", raw, flags=re.I | re.S)
        note = (m.group(1).strip() if m else raw).strip('"')
        return {"subject": "LinkedIn connection note", "body": note[:300]}
    except Exception as e:
        logger.exception("NVIDIA LinkedIn note generation failed: %s", e)
        return {"subject": "LinkedIn connection note", "body": fallback_note[:300]}


async def generate_linkedin_message(prospect_context: str, sender_context: Dict[str, Any]) -> Dict[str, str]:
    first = _first_name_from_context(prospect_context)
    sender_name = sender_context.get("your_name") or ""
    fallback = {
        "subject": "LinkedIn message",
        "body": f"Hi {first},\n\nThanks for connecting! Circling back on the note I sent by email — happy to keep it to 15 minutes if useful.\n\nBest,\n{sender_name}",
    }
    user_prompt = f"Prospect context:\n{prospect_context}\n\nSender: {sender_name} at {sender_context.get('your_company','')}\nValue proposition: {sender_context.get('value_proposition','')}"
    return await _generate_simple(LINKEDIN_MESSAGE_SYSTEM, user_prompt, fallback, max_tokens=320)


async def generate_coldcall_script(prospect_context: str, sender_context: Dict[str, Any]) -> Dict[str, str]:
    first = _first_name_from_context(prospect_context)
    title = _extract_line(prospect_context, "Title")
    company = _extract_line(prospect_context, "Company")
    pains = _role_pain_points(title, company, prospect_context)
    sender_name = sender_context.get("your_name") or ""
    sender_company = sender_context.get("your_company") or ""
    fallback = {
        "subject": f"Cold call script — {first}",
        "body": (
            f"OPENER: Hi {first}, this is {sender_name} from {sender_company} — I'll be quick, is now a bad time?\n\n"
            f"HOOK: I work with {title or 'teams like yours'} on {pains}.\n\n"
            "IF INTERESTED: Would it be worth 15 minutes this week to see if it's relevant?\n\n"
            "IF OBJECTION: Totally understand — mind if I send one email so you have it when it's useful?\n\n"
            f"VOICEMAIL: Hi {first}, {sender_name} from {sender_company}, following up on a note about {pains}. I'll send an email too — talk soon."
        ),
    }
    user_prompt = f"Prospect context:\n{prospect_context}\n\nPain point hints: {pains}\nSender: {sender_name} at {sender_company}"
    return await _generate_simple(COLDCALL_SYSTEM, user_prompt, fallback, max_tokens=420)
