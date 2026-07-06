"""
Pipeline service:
LinkedIn enrichment -> NVIDIA AI draft -> send -> follow-up scheduling.

Important fixes:
- Background tasks open their own DB session by contact_id.
- Progress is stored in memory and exposed via /contacts/{id}/progress.
- Enrich can automatically generate an email draft.
- No legacy AI dependency is used.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from backend.models.database import SessionLocal
from backend.models.db import (
    Activity, Campaign, Contact, ContactStatus,
    STEP_TYPES, DEFAULT_PIPELINE_STEPS,
)
from backend.services.ai_generator import (
    generate_followup_email,
    generate_initial_email,
    generate_linkedin_connect_note,
    generate_linkedin_message,
    generate_coldcall_script,
)
from backend.services.email_sender import send_email
from backend.services.enrichment import build_enrichment_context, enrich_linkedin_profile

logger = logging.getLogger(__name__)

_PROGRESS: dict[int, dict] = {}
_BATCH_JOBS: dict[str, dict] = {}


def set_progress(contact_id: int, percent: int, step: str, status: str = "running") -> None:
    percent = max(0, min(100, int(percent)))
    _PROGRESS[int(contact_id)] = {
        "contact_id": int(contact_id),
        "percent": percent,
        "step": step,
        "status": status,
        "updated_at": datetime.utcnow().isoformat(),
    }


def get_progress(contact_id: int) -> dict:
    return _PROGRESS.get(
        int(contact_id),
        {
            "contact_id": int(contact_id),
            "percent": 0,
            "step": "Not started",
            "status": "idle",
            "updated_at": datetime.utcnow().isoformat(),
        },
    )


def _log_activity(db: Session, contact_id: int, activity_type: str, detail: str = "") -> None:
    try:
        db.add(Activity(contact_id=contact_id, activity_type=activity_type, detail=detail))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to log activity for contact %s", contact_id)


def _set_status(db: Session, contact: Contact, status: ContactStatus, error: Optional[str] = None) -> None:
    contact.status = status
    contact.error_message = error
    contact.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(contact)


def _sender_context(contact: Contact) -> dict:
    campaign: Optional[Campaign] = contact.campaign
    return {
        "your_name": campaign.your_name if campaign and campaign.your_name else "Venkat",
        "your_company": campaign.your_company if campaign and campaign.your_company else "Innominds",
        "your_role": campaign.your_role if campaign and campaign.your_role else "",
        "value_proposition": (
            campaign.value_proposition
            if campaign and campaign.value_proposition
            else "AI, data, cloud, automation, platform engineering, and digital product engineering services"
        ),
    }


def _profile_dict(contact: Contact) -> dict:
    return {
        "headline": contact.linkedin_headline or "",
        "summary": contact.linkedin_summary or "",
        "experience": contact.linkedin_experience or "",
        "skills": contact.linkedin_skills or "",
    }


def _contact_dict(contact: Contact) -> dict:
    return {
        "name": contact.name,
        "company": contact.company or "",
        "job_title": contact.job_title or "",
        "email": contact.email,
        "linkedin_url": contact.linkedin_url or "",
    }


async def run_enrichment(db: Session, contact: Contact, auto_draft: bool = True) -> None:
    """Fetch LinkedIn profile data, save it, and optionally generate an email draft."""
    contact_id = contact.id
    set_progress(contact_id, 5, "Starting enrichment")
    _set_status(db, contact, ContactStatus.enriching)

    try:
        profile = {"headline": "", "summary": "", "experience": "", "skills": ""}

        if contact.linkedin_url:
            set_progress(contact_id, 20, "Opening LinkedIn profile")
            profile = await enrich_linkedin_profile(contact.linkedin_url)
            set_progress(contact_id, 55, "Reading profile details")
        else:
            set_progress(contact_id, 45, "No LinkedIn URL. Using uploaded contact details.")

        contact.linkedin_headline = profile.get("headline") or contact.linkedin_headline
        contact.linkedin_summary = profile.get("summary") or contact.linkedin_summary
        contact.linkedin_experience = profile.get("experience") or contact.linkedin_experience
        contact.linkedin_skills = profile.get("skills") or contact.linkedin_skills
        contact.enriched_at = datetime.utcnow()
        contact.status = ContactStatus.enriched
        contact.error_message = None
        contact.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(contact)

        set_progress(contact_id, 70, "Enrichment saved")
        _log_activity(db, contact.id, "enriched", f"Headline: {contact.linkedin_headline or 'n/a'}")

        if auto_draft:
            await run_email_draft(db, contact, start_percent=72, end_percent=100)
        else:
            set_progress(contact_id, 100, "Enrichment completed", "completed")

    except Exception as e:
        logger.exception("Enrichment failed for contact %s", contact_id)
        db.rollback()
        _set_status(db, contact, ContactStatus.error, str(e))
        _log_activity(db, contact_id, "error", f"Enrichment failed: {e}")
        set_progress(contact_id, 100, f"Error: {e}", "error")


async def run_email_draft(
    db: Session,
    contact: Contact,
    start_percent: int = 10,
    end_percent: int = 100,
) -> None:
    """Generate or regenerate a personalised email draft."""
    contact_id = contact.id
    set_progress(contact_id, start_percent, "Preparing AI prompt")
    _set_status(db, contact, ContactStatus.drafting)

    try:
        profile_data = _profile_dict(contact)
        prospect_context = build_enrichment_context(_contact_dict(contact), profile_data)
        sender_context = _sender_context(contact)

        set_progress(contact_id, min(end_percent - 20, 85), "Generating draft with NVIDIA API")
        draft = await generate_initial_email(prospect_context, sender_context)

        contact.email_subject = draft.get("subject") or f"Quick thought, {contact.name}"
        contact.email_body = draft.get("body") or ""
        contact.status = ContactStatus.draft_ready
        contact.error_message = None
        contact.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(contact)

        _log_activity(db, contact.id, "draft_ready", f"Subject: {contact.email_subject}")
        set_progress(contact_id, end_percent, "Draft ready", "completed")

    except Exception as e:
        logger.exception("Draft generation failed for contact %s", contact_id)
        db.rollback()
        _set_status(db, contact, ContactStatus.error, str(e))
        _log_activity(db, contact_id, "error", f"Draft failed: {e}")
        set_progress(contact_id, 100, f"Error: {e}", "error")


async def run_send_email(db: Session, contact: Contact) -> None:
    """Send the approved email draft."""
    if not contact.email_subject or not contact.email_body:
        _set_status(db, contact, ContactStatus.error, "No email draft to send")
        return

    try:
        success = await send_email(
            to_email=contact.email,
            to_name=contact.name,
            subject=contact.email_subject,
            body=contact.email_body,
        )

        if success:
            contact.email_sent_at = datetime.utcnow()
            campaign = contact.campaign
            if campaign and campaign.followup_days:
                days = [int(d.strip()) for d in campaign.followup_days.split(",") if d.strip().isdigit()]
                if days:
                    contact.next_followup_at = datetime.utcnow() + timedelta(days=days[0])
            _set_status(db, contact, ContactStatus.sent)
            _log_activity(db, contact.id, "email_sent", contact.email_subject)
        else:
            _set_status(db, contact, ContactStatus.bounced, "SMTP send failed")
    except Exception as e:
        logger.exception("Send failed for contact %s", contact.id)
        _set_status(db, contact, ContactStatus.error, str(e))


async def run_followup(db: Session, contact: Contact) -> None:
    """Generate and send the next follow-up."""
    try:
        campaign = contact.campaign
        if not campaign:
            return

        prospect_context = build_enrichment_context(_contact_dict(contact), _profile_dict(contact))
        sender_context = _sender_context(contact)
        followup_number = (contact.followup_count or 0) + 1

        draft = await generate_followup_email(
            prospect_context=prospect_context,
            sender_context=sender_context,
            original_subject=contact.email_subject or "Quick thought",
            followup_number=followup_number,
        )

        success = await send_email(
            to_email=contact.email,
            to_name=contact.name,
            subject=draft["subject"],
            body=draft["body"],
        )

        if success:
            contact.followup_count = followup_number
            contact.last_followup_at = datetime.utcnow()

            days = [int(d.strip()) for d in (campaign.followup_days or "").split(",") if d.strip().isdigit()]
            if followup_number < (campaign.max_followups or 0) and followup_number < len(days):
                contact.next_followup_at = datetime.utcnow() + timedelta(days=days[followup_number])
            else:
                contact.next_followup_at = None

            contact.status = ContactStatus.followed_up
            contact.updated_at = datetime.utcnow()
            db.commit()
            _log_activity(db, contact.id, "followup_sent", f"Follow-up #{followup_number}: {draft['subject']}")
    except Exception as e:
        logger.exception("Follow-up failed for contact %s", contact.id)
        _set_status(db, contact, ContactStatus.error, str(e))


async def run_full_pipeline(db: Session, contact: Contact, auto_send: bool = False) -> None:
    """Run enrich -> draft -> optional send."""
    await run_enrichment(db, contact, auto_draft=True)
    db.refresh(contact)

    if auto_send and contact.status == ContactStatus.draft_ready:
        contact.status = ContactStatus.approved
        db.commit()
        await run_send_email(db, contact)


async def run_enrichment_for_contact(contact_id: int, auto_draft: bool = True) -> None:
    """Background-safe enrichment entrypoint."""
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.id == contact_id).first()
        if not contact:
            set_progress(contact_id, 100, "Contact not found", "error")
            return
        await run_enrichment(db, contact, auto_draft=auto_draft)
    finally:
        db.close()


async def run_email_draft_for_contact(contact_id: int) -> None:
    """Background-safe drafting entrypoint."""
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.id == contact_id).first()
        if not contact:
            set_progress(contact_id, 100, "Contact not found", "error")
            return
        await run_email_draft(db, contact)
    finally:
        db.close()


async def run_full_pipeline_for_contact(contact_id: int, auto_send: bool = False) -> None:
    """Background-safe full pipeline entrypoint."""
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.id == contact_id).first()
        if not contact:
            set_progress(contact_id, 100, "Contact not found", "error")
            return
        await run_full_pipeline(db, contact, auto_send=auto_send)
    finally:
        db.close()


async def run_send_email_for_contact(contact_id: int) -> None:
    """Background-safe send entrypoint."""
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.id == contact_id).first()
        if contact:
            await run_send_email(db, contact)
    finally:
        db.close()


async def process_due_followups(db: Session) -> None:
    """Scheduler tick: process due follow-ups."""
    now = datetime.utcnow()
    due = (
        db.query(Contact)
        .filter(
            Contact.next_followup_at.isnot(None),
            Contact.next_followup_at <= now,
            Contact.status.in_([ContactStatus.sent, ContactStatus.followed_up]),
        )
        .all()
    )

    logger.info("Scheduler: %s contacts due for follow-up", len(due))
    for contact in due:
        await run_followup(db, contact)


# ══════════════════════════════════════════════════════════════════════════
# Multi-step pipeline engine (email → followup-1 → LinkedIn connect →
# followup email-2 → LinkedIn message → cold call → followup email-3 → -4)
# ══════════════════════════════════════════════════════════════════════════

def get_campaign_steps(contact: Contact) -> list[str]:
    campaign: Optional[Campaign] = contact.campaign
    if campaign and campaign.pipeline_steps:
        try:
            steps = json.loads(campaign.pipeline_steps)
            if isinstance(steps, list) and steps:
                return [s for s in steps if s in STEP_TYPES]
        except Exception:
            pass
    return list(DEFAULT_PIPELINE_STEPS)


def load_pipeline_state(contact: Contact) -> dict:
    state = {}
    if contact.pipeline_state:
        try:
            state = json.loads(contact.pipeline_state)
        except Exception:
            state = {}
    for step in get_campaign_steps(contact):
        if step == "email":
            continue  # tracked via contact.status instead
        if step not in state:
            state[step] = {"status": "pending", "subject": None, "body": None, "generated_at": None, "sent_at": None}
    return state


def save_pipeline_state(db: Session, contact: Contact, state: dict) -> None:
    contact.pipeline_state = json.dumps(state)
    contact.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(contact)


def pipeline_overview(contact: Contact) -> list[dict]:
    """Full step-by-step status list for the UI stepper."""
    steps = get_campaign_steps(contact)
    state = load_pipeline_state(contact)
    out = []
    for step in steps:
        meta = STEP_TYPES.get(step, {"label": step, "kind": "email"})
        if step == "email":
            status_map = {
                ContactStatus.pending: "pending", ContactStatus.enriching: "in_progress",
                ContactStatus.enriched: "pending", ContactStatus.drafting: "in_progress",
                ContactStatus.draft_ready: "generated", ContactStatus.approved: "generated",
                ContactStatus.sent: "done", ContactStatus.followed_up: "done",
                ContactStatus.replied: "done", ContactStatus.bounced: "generated",
                ContactStatus.error: "pending",
            }
            out.append({
                "step": step, "label": meta["label"], "kind": meta["kind"],
                "status": status_map.get(contact.status, "pending"),
                "subject": contact.email_subject, "body": contact.email_body,
                "sent_at": contact.email_sent_at.isoformat() if contact.email_sent_at else None,
            })
        else:
            s = state.get(step, {"status": "pending"})
            out.append({
                "step": step, "label": meta["label"], "kind": meta["kind"],
                "status": s.get("status", "pending"),
                "subject": s.get("subject"), "body": s.get("body"),
                "sent_at": s.get("sent_at"),
            })
    return out


def next_action_for_contact(contact: Contact) -> dict:
    """Determine the single next actionable step — powers the 'smart' pipeline
    button (auto-detects what to do next) and the bulk 'Run pipeline on
    selected' action.
    """
    overview = pipeline_overview(contact)
    for item in overview:
        if item["status"] == "done":
            continue
        if item["step"] == "email":
            if contact.status in (ContactStatus.pending, ContactStatus.error, ContactStatus.enriched):
                return {"step": "email", "action": "enrich_and_draft", "label": "Enrich + Draft intro email"}
            if contact.status in (ContactStatus.enriching, ContactStatus.drafting):
                return {"step": "email", "action": "in_progress", "label": "Working…"}
            if contact.status in (ContactStatus.draft_ready, ContactStatus.approved, ContactStatus.bounced):
                return {"step": "email", "action": "needs_review", "label": "Review & send intro email"}
        else:
            if item["status"] == "pending":
                return {"step": item["step"], "action": "generate", "label": f"Generate {item['label']}"}
            if item["status"] == "generated":
                kind = item["kind"]
                verb = "Send" if kind == "email" else ("Mark connected" if item["step"] == "linkedin_connect" else "Mark done")
                return {"step": item["step"], "action": "needs_review", "label": f"{verb}: {item['label']}"}
    return {"step": None, "action": "complete", "label": "Pipeline complete"}


async def generate_step_content(db: Session, contact: Contact, step: str) -> dict:
    """Generate AI content for a single non-email-primary pipeline step."""
    profile_data = _profile_dict(contact)
    prospect_context = build_enrichment_context(_contact_dict(contact), profile_data)
    sender_context = _sender_context(contact)
    meta = STEP_TYPES.get(step)
    if not meta:
        raise ValueError(f"Unknown step: {step}")

    if step == "linkedin_connect":
        draft = await generate_linkedin_connect_note(prospect_context, sender_context)
    elif step == "linkedin_message":
        draft = await generate_linkedin_message(prospect_context, sender_context)
    elif step == "cold_call":
        draft = await generate_coldcall_script(prospect_context, sender_context)
    elif meta["kind"] == "email":
        steps = get_campaign_steps(contact)
        followup_number = max(1, [s for s in steps if STEP_TYPES.get(s, {}).get("kind") == "email"].index(step))
        draft = await generate_followup_email(
            prospect_context=prospect_context,
            sender_context=sender_context,
            original_subject=contact.email_subject or "Quick thought",
            followup_number=followup_number,
        )
    else:
        raise ValueError(f"No generator for step: {step}")

    state = load_pipeline_state(contact)
    state[step] = {
        "status": "generated",
        "subject": draft.get("subject"),
        "body": draft.get("body"),
        "generated_at": datetime.utcnow().isoformat(),
        "sent_at": None,
    }
    save_pipeline_state(db, contact, state)
    _log_activity(db, contact.id, "step_generated", f"{STEP_TYPES[step]['label']}: {draft.get('subject','')}")
    return state[step]


async def complete_step(db: Session, contact: Contact, step: str, send_email_now: bool = True) -> dict:
    """Mark a step complete. For email-kind steps this actually sends the email."""
    state = load_pipeline_state(contact)
    entry = state.get(step)
    if not entry or entry.get("status") != "generated":
        raise ValueError("Generate this step's content first")

    meta = STEP_TYPES[step]
    if meta["kind"] == "email" and send_email_now:
        success = await send_email(
            to_email=contact.email, to_name=contact.name,
            subject=entry["subject"], body=entry["body"],
        )
        if not success:
            raise RuntimeError("Email send failed")

    entry["status"] = "sent" if meta["kind"] == "email" else "done"
    entry["sent_at"] = datetime.utcnow().isoformat()
    state[step] = entry
    save_pipeline_state(db, contact, state)

    contact.status = ContactStatus.followed_up
    contact.followup_count = (contact.followup_count or 0) + 1
    contact.last_followup_at = datetime.utcnow()
    contact.updated_at = datetime.utcnow()
    db.commit()

    _log_activity(db, contact.id, "step_completed", f"{meta['label']} marked {entry['status']}")
    return entry


async def run_smart_step_for_contact(contact_id: int) -> dict:
    """Background-safe: figure out the next step for a contact and execute the
    generation part of it (never auto-sends — sending/marking-done stays a
    human action from the UI, except the very first intro email which follows
    the existing enrich→draft flow).
    """
    db = SessionLocal()
    try:
        contact = db.query(Contact).filter(Contact.id == contact_id).first()
        if not contact:
            return {"contact_id": contact_id, "result": "not_found"}
        action = next_action_for_contact(contact)
        if action["action"] == "enrich_and_draft":
            await run_enrichment(db, contact, auto_draft=True)
            return {"contact_id": contact_id, "result": "email_drafted"}
        if action["action"] == "generate":
            await generate_step_content(db, contact, action["step"])
            return {"contact_id": contact_id, "result": f"generated:{action['step']}"}
        return {"contact_id": contact_id, "result": action["action"]}
    except Exception as e:
        logger.exception("Smart step failed for contact %s", contact_id)
        return {"contact_id": contact_id, "result": "error", "detail": str(e)}
    finally:
        db.close()


def create_batch_job(contact_ids: list[int]) -> str:
    job_id = uuid.uuid4().hex[:12]
    _BATCH_JOBS[job_id] = {
        "id": job_id, "total": len(contact_ids), "completed": 0,
        "status": "running", "started_at": datetime.utcnow().isoformat(),
        "finished_at": None, "current": None, "results": [],
    }
    return job_id


def get_batch_job(job_id: str) -> Optional[dict]:
    return _BATCH_JOBS.get(job_id)


async def run_batch_pipeline(job_id: str, contact_ids: list[int]) -> None:
    job = _BATCH_JOBS.get(job_id)
    if not job:
        return
    db = SessionLocal()
    try:
        for cid in contact_ids:
            c = db.query(Contact).filter(Contact.id == cid).first()
            job["current"] = c.name if c else str(cid)
            result = await run_smart_step_for_contact(cid)
            job["completed"] += 1
            job["results"].append(result)
        job["status"] = "completed"
    except Exception as e:
        logger.exception("Batch pipeline job %s failed", job_id)
        job["status"] = "error"
    finally:
        job["finished_at"] = datetime.utcnow().isoformat()
        job["current"] = None
        db.close()
