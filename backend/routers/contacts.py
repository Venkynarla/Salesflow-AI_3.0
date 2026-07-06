"""Contacts API routes — owner-scoped for multi-user isolation, plus lead/client
categorization, duplicate detection, and hold support.

Visibility rules mirror campaigns.py:
- Admins see every contact.
- Logged-in members see only contacts they (or their campaigns) own.
- Guests (no login) see only "unowned" contacts — the shared legacy/demo pool.
"""

import csv
import io
import re
from collections import defaultdict
from datetime import datetime
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.models.database import get_db, SessionLocal
from backend.models.db import Activity, Contact, ContactStatus, STEP_TYPES, LIST_TYPES, SkippedImport, User
from backend.services.auth import get_user_from_token, is_admin
from backend.services.pipeline import (
    complete_step,
    create_batch_job,
    generate_step_content,
    get_batch_job,
    get_progress,
    next_action_for_contact,
    pipeline_overview,
    run_batch_pipeline,
    run_email_draft_for_contact,
    run_full_pipeline_for_contact,
    run_send_email_for_contact,
    run_enrichment_for_contact,
    run_smart_step_for_contact,
)

router = APIRouter(prefix="/contacts", tags=["contacts"])


# ── Ownership helpers (mirrors campaigns.py) ────────────────────────────────

def _current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> Optional[User]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    user = get_user_from_token(db, authorization.split(" ", 1)[1].strip())
    if user and not user.is_active:
        raise HTTPException(403, "Your account has been restricted. Contact an admin.")
    return user


def _visible_query(db: Session, user: Optional[User]):
    q = db.query(Contact)
    if user and is_admin(user):
        return q
    if user:
        return q.filter(Contact.owner_id == user.id)
    return q.filter(Contact.owner_id.is_(None))


def _owned_contact(db: Session, contact_id: int, user: Optional[User]) -> Contact:
    c = db.query(Contact).filter(Contact.id == contact_id).first()
    if not c:
        raise HTTPException(404, "Contact not found")
    if user and is_admin(user):
        return c
    owner_ok = (user and c.owner_id == user.id) or (not user and c.owner_id is None)
    if not owner_ok:
        raise HTTPException(403, "You don't have access to this contact")
    return c


def _owned_ids(db: Session, ids: List[int], user: Optional[User]) -> List[int]:
    """Filters a requested id list down to only the ones the current user may act on."""
    if user and is_admin(user):
        return ids
    q = db.query(Contact.id).filter(Contact.id.in_(ids))
    q = q.filter(Contact.owner_id == user.id) if user else q.filter(Contact.owner_id.is_(None))
    return [r[0] for r in q.all()]


class ContactOut(BaseModel):
    id: int
    name: str
    email: str
    linkedin_url: Optional[str]
    company: Optional[str]
    job_title: Optional[str]
    status: str
    email_subject: Optional[str]
    email_body: Optional[str]
    followup_count: int
    next_followup_at: Optional[datetime]
    email_sent_at: Optional[datetime]
    error_message: Optional[str]
    created_at: datetime
    campaign_id: Optional[int]
    linkedin_headline: Optional[str] = None
    linkedin_summary: Optional[str] = None
    linkedin_experience: Optional[str] = None
    linkedin_skills: Optional[str] = None
    list_type: str = "prospect"
    on_hold: bool = False
    owner_id: Optional[int] = None

    class Config:
        from_attributes = True


class ContactUpdate(BaseModel):
    email_subject: Optional[str] = None
    email_body: Optional[str] = None
    status: Optional[str] = None
    name: Optional[str] = None
    company: Optional[str] = None
    job_title: Optional[str] = None
    linkedin_url: Optional[str] = None
    campaign_id: Optional[int] = None
    # Manual/automatic enrichment fields. These are editable in the UI and fed to AI drafting.
    linkedin_headline: Optional[str] = None
    linkedin_summary: Optional[str] = None
    linkedin_experience: Optional[str] = None
    linkedin_skills: Optional[str] = None
    list_type: Optional[str] = None


class ContactCreate(BaseModel):
    name: str
    email: str
    linkedin_url: Optional[str] = None
    company: Optional[str] = None
    job_title: Optional[str] = None
    campaign_id: Optional[int] = None
    # Optional manual About/context at creation time
    linkedin_summary: Optional[str] = None
    linkedin_headline: Optional[str] = None
    linkedin_experience: Optional[str] = None
    linkedin_skills: Optional[str] = None
    list_type: Optional[str] = None


class BulkContactAction(BaseModel):
    ids: List[int]
    campaign_id: Optional[int] = None


class BulkIds(BaseModel):
    ids: List[int] = []


class BulkHold(BaseModel):
    ids: List[int] = []
    hold: bool = True


class StepCompleteIn(BaseModel):
    send: bool = True


class StepEditIn(BaseModel):
    subject: Optional[str] = None
    body: str


class ActivityOut(BaseModel):
    id: int
    activity_type: str
    detail: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("/", response_model=ContactOut)
def create_contact(payload: ContactCreate, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email is required")
    existing = db.query(Contact).filter(Contact.email == email).first()
    if existing:
        raise HTTPException(400, "A contact with this email already exists")
    list_type = payload.list_type if (payload.list_type in LIST_TYPES and user and is_admin(user)) else "prospect"
    contact = Contact(
        name=payload.name.strip() or email.split("@")[0],
        email=email,
        linkedin_url=payload.linkedin_url,
        company=payload.company,
        job_title=payload.job_title,
        campaign_id=payload.campaign_id,
        linkedin_summary=payload.linkedin_summary,
        linkedin_headline=payload.linkedin_headline,
        linkedin_experience=payload.linkedin_experience,
        linkedin_skills=payload.linkedin_skills,
        enriched_at=datetime.utcnow() if (payload.linkedin_summary or payload.linkedin_headline or payload.linkedin_experience) else None,
        status=ContactStatus.enriched if (payload.linkedin_summary or payload.linkedin_headline or payload.linkedin_experience) else ContactStatus.pending,
        list_type=list_type,
        owner_id=user.id if user else None,
    )
    db.add(contact)
    db.commit()
    db.refresh(contact)
    from backend.services.auth import log_audit
    log_audit(db, user.id if user else None, "contact_created", f"Added contact '{contact.name}' ({contact.email})")
    return contact


@router.post("/upload")
async def upload_contacts(
    file: UploadFile = File(...),
    campaign_id: Optional[int] = Form(None),
    list_type: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(_current_user),
):
    """Upload a CSV or Excel file of contacts. Only admins can tag an upload as
    Leads / Clients / Not Interested — everyone else gets the standard pipeline
    ("prospect") category."""
    content = await file.read()

    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content))
        elif file.filename.endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(content))
        else:
            raise HTTPException(400, "Only CSV or Excel files are supported")
    except Exception as e:
        raise HTTPException(400, f"Could not parse file: {e}")

    df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

    if "email" not in df.columns:
        raise HTTPException(400, f"Missing required column: email. Found: {list(df.columns)}")

    effective_list_type = list_type if (list_type in LIST_TYPES and user and is_admin(user)) else "prospect"
    list_name_label = None
    if campaign_id:
        from backend.models.db import Campaign
        camp = db.query(Campaign).filter(Campaign.id == campaign_id).first()
        list_name_label = camp.name if camp else f"Campaign #{campaign_id}"
    else:
        list_name_label = LIST_TYPES.get(effective_list_type, effective_list_type)

    created = 0
    skipped = 0

    def _record_skip(name: str, email: str, reason: str):
        db.add(SkippedImport(
            owner_id=user.id if user else None,
            attempted_name=name or None,
            attempted_email=email or None,
            list_name=list_name_label,
            reason=reason,
        ))

    for _, row in df.iterrows():
        raw_email = row.get("email", "")
        email = "" if pd.isna(raw_email) else str(raw_email).strip().lower()
        raw_name = row.get("name", "")
        name = "" if pd.isna(raw_name) else str(raw_name).strip()

        if not email or "@" not in email or email in ("nan", "none"):
            _record_skip(name, email, "Missing or invalid email address")
            skipped += 1
            continue

        try:
            existing = db.query(Contact).filter(Contact.email == email).first()
            if existing:
                # Allow upload to add existing contact to selected campaign instead of dropping it silently
                if campaign_id and existing.campaign_id != campaign_id:
                    existing.campaign_id = campaign_id
                    existing.updated_at = datetime.utcnow()
                    _record_skip(name or existing.name, email, f"Already existed as a contact — added to '{list_name_label}' instead of creating a duplicate")
                else:
                    _record_skip(name or existing.name, email, "A contact with this email already exists")
                skipped += 1
                continue

            contact = Contact(
                email=email,
                name=name or email.split("@")[0],
                linkedin_url=str(row.get("linkedin_url", row.get("linkedin", "")) or "").strip() or None,
                company=str(row.get("company", "") or "").strip() or None,
                job_title=str(row.get("job_title", row.get("title", row.get("designation", ""))) or "").strip() or None,
                campaign_id=campaign_id,
                status=ContactStatus.pending,
                list_type=effective_list_type,
                owner_id=user.id if user else None,
            )
            db.add(contact)
            created += 1
        except Exception as e:
            _record_skip(name, email, f"Unexpected error while importing this row: {e}")
            skipped += 1
            continue

    db.commit()
    from backend.services.auth import log_audit
    log_audit(
        db, user.id if user else None, "contacts_uploaded",
        f"Uploaded '{file.filename}' → {created} created, {skipped} skipped (list: {list_name_label})",
    )
    return {"created": created, "skipped": skipped, "list_type": effective_list_type}


@router.get("/", response_model=List[ContactOut])
def list_contacts(
    status: Optional[str] = None,
    campaign_id: Optional[int] = None,
    list_type: Optional[str] = None,
    on_hold: Optional[bool] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(_current_user),
):
    q = _visible_query(db, user)
    if status:
        q = q.filter(Contact.status == status)
    if campaign_id:
        q = q.filter(Contact.campaign_id == campaign_id)
    if list_type:
        q = q.filter(Contact.list_type == list_type)
    if on_hold is not None:
        q = q.filter(Contact.on_hold == on_hold)
    if search:
        q = q.filter(
            (Contact.name.ilike(f"%{search}%"))
            | (Contact.email.ilike(f"%{search}%"))
            | (Contact.company.ilike(f"%{search}%"))
        )
    return q.order_by(Contact.created_at.desc()).offset(skip).limit(limit).all()


@router.get("/stats")
def contact_stats(db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    base = _visible_query(db, user)
    total = base.count()
    stats = {}
    for status in ContactStatus:
        stats[status.value] = _visible_query(db, user).filter(Contact.status == status).count()
    by_list_type = {lt: _visible_query(db, user).filter(Contact.list_type == lt).count() for lt in LIST_TYPES}
    on_hold_count = _visible_query(db, user).filter(Contact.on_hold == True).count()  # noqa: E712
    return {"total": total, **stats, "by_list_type": by_list_type, "on_hold": on_hold_count}


@router.get("/skipped")
def list_skipped_imports(
    search: Optional[str] = None,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(_current_user),
):
    """Every contact row that got skipped during an upload, with the reason —
    so nothing silently disappears (item 4). Admins can search/filter by user;
    everyone else only ever sees their own."""
    q = db.query(SkippedImport)
    if user and is_admin(user):
        if user_id:
            q = q.filter(SkippedImport.owner_id == user_id)
    elif user:
        q = q.filter(SkippedImport.owner_id == user.id)
    else:
        q = q.filter(SkippedImport.owner_id.is_(None))
    if search:
        q = q.filter(
            (SkippedImport.attempted_name.ilike(f"%{search}%"))
            | (SkippedImport.attempted_email.ilike(f"%{search}%"))
            | (SkippedImport.list_name.ilike(f"%{search}%"))
        )
    rows = q.order_by(SkippedImport.created_at.desc()).limit(1000).all()
    users_by_id = {u.id: u for u in db.query(User).all()}
    return [{
        "id": r.id, "name": r.attempted_name, "email": r.attempted_email,
        "list_name": r.list_name, "reason": r.reason,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "owner_id": r.owner_id,
        "owner_name": (users_by_id.get(r.owner_id).name if r.owner_id and users_by_id.get(r.owner_id) else "Guest / unassigned"),
    } for r in rows]


@router.get("/skipped/by-user")
def skipped_by_user(db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Admin-only-ish summary: how many skipped rows belong to each user, so
    it's easy to see at a glance (item 5)."""
    q = db.query(SkippedImport)
    if not (user and is_admin(user)):
        q = q.filter(SkippedImport.owner_id == (user.id if user else None))
    rows = q.all()
    users_by_id = {u.id: u for u in db.query(User).all()}
    counts: dict = {}
    for r in rows:
        label = users_by_id.get(r.owner_id).name if r.owner_id and users_by_id.get(r.owner_id) else "Guest / unassigned"
        counts[label] = counts.get(label, 0) + 1
    return {"counts": counts}


class SkippedBulkIds(BaseModel):
    ids: List[int] = []


@router.post("/skipped/bulk/delete")
def bulk_delete_skipped(payload: SkippedBulkIds, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    if not payload.ids:
        raise HTTPException(400, "Select at least one row")
    q = db.query(SkippedImport).filter(SkippedImport.id.in_(payload.ids))
    if not (user and is_admin(user)):
        q = q.filter(SkippedImport.owner_id == (user.id if user else None))
    deleted = q.delete(synchronize_session=False)
    db.commit()
    return {"message": f"Dismissed {deleted} skipped rows", "deleted": deleted}


@router.post("/skipped/bulk/add-to-contacts")
def bulk_add_skipped_to_contacts(payload: SkippedBulkIds, campaign_id: Optional[int] = None, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Attempts to actually create a contact from each selected skipped row.
    Rows skipped for a fixable reason (nothing wrong, just already existed at
    upload time) succeed; rows with an unusable email are reported back so you
    know exactly why they still can't be added."""
    q = db.query(SkippedImport).filter(SkippedImport.id.in_(payload.ids))
    if not (user and is_admin(user)):
        q = q.filter(SkippedImport.owner_id == (user.id if user else None))
    rows = q.all()
    if not rows:
        raise HTTPException(400, "No accessible skipped rows in the selection")

    added, failed = [], []
    for row in rows:
        email = (row.attempted_email or "").strip().lower()
        if not email or "@" not in email:
            failed.append({"id": row.id, "reason": "No valid email on file for this row"})
            continue
        existing = db.query(Contact).filter(Contact.email == email).first()
        if existing:
            failed.append({"id": row.id, "reason": f"Contact #{existing.id} already exists with this email"})
            continue
        contact = Contact(
            email=email,
            name=row.attempted_name or email.split("@")[0],
            campaign_id=campaign_id,
            status=ContactStatus.pending,
            list_type="prospect",
            owner_id=row.owner_id,
        )
        db.add(contact)
        db.delete(row)
        added.append(row.id)

    db.commit()
    return {"message": f"Added {len(added)} contacts, {len(failed)} could not be added", "added": added, "failed": failed}


@router.delete("/skipped/{skipped_id}")
def dismiss_skipped_import(skipped_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    row = db.query(SkippedImport).filter(SkippedImport.id == skipped_id).first()
    if not row:
        raise HTTPException(404, "Not found")
    if not (user and is_admin(user)):
        owner_ok = (user and row.owner_id == user.id) or (not user and row.owner_id is None)
        if not owner_ok:
            raise HTTPException(403, "You don't have access to this record")
    db.delete(row)
    db.commit()
    return {"message": "Dismissed"}


@router.get("/duplicates")
def find_duplicates(db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Groups visible contacts that look like genuine duplicates of each other.

    A name match alone (e.g. two different people both named "Praveen") is NOT
    enough — that produced false positives. We now require corroborating
    evidence: the same LinkedIn URL (strong signal on its own), OR the same
    full normalized name AND same company AND a closely-matching email local
    part (so "praveen-3.kumar@novartis.com" and "praveen.dass@novartis.com" —
    same first name, same company, but clearly different surnames in the
    email — are correctly treated as different people).
    """
    contacts = _visible_query(db, user).all()
    groups = _compute_duplicate_groups(contacts)

    all_dup_ids = sorted({i for g in groups for i in g["ids"]})
    return {"groups": groups, "total_contacts_in_duplicates": len(all_dup_ids), "ids": all_dup_ids}


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _email_local(email: str) -> str:
    return re.split(r"[.\-_+]", email.split("@")[0].lower())


def _email_similarity(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a.split("@")[0].lower(), b.split("@")[0].lower()).ratio()


def _compute_duplicate_groups(contacts: List[Contact]) -> List[dict]:
    from itertools import combinations

    li_groups: dict = defaultdict(list)
    name_company_groups: dict = defaultdict(list)
    for c in contacts:
        if c.linkedin_url:
            li_groups[_norm(c.linkedin_url)].append(c)
        key_name = _norm(c.name)
        if key_name and len(key_name) > 2:
            name_company_groups[(key_name, _norm(c.company))].append(c)

    seen_id_sets = set()
    result_groups = []

    # Strong signal: identical LinkedIn URL.
    for key, members in li_groups.items():
        if len(members) < 2:
            continue
        ids = tuple(sorted(m.id for m in members))
        if ids in seen_id_sets:
            continue
        seen_id_sets.add(ids)
        result_groups.append({
            "match_on": "linkedin_url", "sample": members[0].name,
            "count": len(members), "ids": list(ids),
        })

    # Weaker signal: same name + same company — only counts if email local
    # parts also closely match (corroborating evidence it's the same person).
    for (name_key, company_key), members in name_company_groups.items():
        if len(members) < 2:
            continue
        confirmed_pairs = set()
        for a, b in combinations(members, 2):
            if _email_similarity(a.email, b.email) >= 0.82:
                confirmed_pairs.add(a.id)
                confirmed_pairs.add(b.id)
        if len(confirmed_pairs) < 2:
            continue
        ids = tuple(sorted(confirmed_pairs))
        if ids in seen_id_sets:
            continue
        seen_id_sets.add(ids)
        result_groups.append({
            "match_on": "name_company_and_similar_email", "sample": members[0].name,
            "count": len(ids), "ids": list(ids),
        })

    return result_groups


@router.post("/bulk/hold")
def bulk_hold(payload: BulkHold, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Put contacts on hold (e.g. duplicates you're reviewing) — held contacts
    are skipped by every pipeline / bulk-send action until released."""
    ids = _owned_ids(db, payload.ids, user)
    if not ids:
        raise HTTPException(400, "No accessible contacts in the selection")
    updated = db.query(Contact).filter(Contact.id.in_(ids)).update(
        {Contact.on_hold: payload.hold, Contact.updated_at: datetime.utcnow()},
        synchronize_session=False,
    )
    db.commit()
    return {"message": f"{'Held' if payload.hold else 'Released'} {updated} contacts", "updated": updated}


@router.get("/export.csv")
def export_contacts_csv(status: Optional[str] = None, campaign_id: Optional[int] = None, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Download contacts with enrichment and ALL generated pipeline content (intro
    email, follow-ups, LinkedIn connect note/message, cold call script) as CSV."""
    q = _visible_query(db, user)
    if status:
        q = q.filter(Contact.status == ContactStatus(status))
    if campaign_id:
        q = q.filter(Contact.campaign_id == campaign_id)
    contacts = q.order_by(Contact.created_at.desc()).all()

    step_keys = list(STEP_TYPES.keys())
    output = io.StringIO()
    writer = csv.writer(output)
    header = [
        "id", "name", "email", "company", "job_title", "linkedin_url", "campaign_id", "status",
        "list_type", "on_hold",
        "linkedin_headline", "linkedin_summary", "linkedin_experience", "linkedin_skills",
        "followup_count", "email_sent_at", "next_followup_at", "error_message",
    ]
    for step in step_keys:
        header += [f"{step}_subject", f"{step}_body", f"{step}_status"]
    writer.writerow(header)

    for c in contacts:
        overview = {item["step"]: item for item in pipeline_overview(c)}
        row = [
            c.id, c.name, c.email, c.company or "", c.job_title or "", c.linkedin_url or "", c.campaign_id or "",
            c.status.value if hasattr(c.status, "value") else c.status,
            c.list_type or "prospect", c.on_hold,
            c.linkedin_headline or "", c.linkedin_summary or "", c.linkedin_experience or "", c.linkedin_skills or "",
            c.followup_count or 0, c.email_sent_at or "", c.next_followup_at or "", c.error_message or "",
        ]
        for step in step_keys:
            item = overview.get(step, {})
            row += [item.get("subject") or "", item.get("body") or "", item.get("status") or "pending"]
        writer.writerow(row)

    output.seek(0)
    filename = f"salesflow_contacts_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/next-steps")
def contacts_next_steps(db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    contacts = _visible_query(db, user).filter(Contact.status != ContactStatus.replied, Contact.on_hold == False).all()  # noqa: E712
    return {str(c.id): next_action_for_contact(c) for c in contacts}


@router.get("/{contact_id}", response_model=ContactOut)
def get_contact(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    return _owned_contact(db, contact_id, user)


@router.get("/{contact_id}/activities", response_model=List[ActivityOut])
def get_activities(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    _owned_contact(db, contact_id, user)
    return (
        db.query(Activity)
        .filter(Activity.contact_id == contact_id)
        .order_by(Activity.created_at.desc())
        .all()
    )


@router.get("/{contact_id}/progress")
def contact_progress(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    _owned_contact(db, contact_id, user)
    return get_progress(contact_id)


@router.patch("/{contact_id}", response_model=ContactOut)
def update_contact(contact_id: int, payload: ContactUpdate, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)

    data = payload.dict(exclude_unset=True)
    if "list_type" in data and not (user and is_admin(user)):
        data.pop("list_type")  # only admins may re-categorize a contact
    for field, value in data.items():
        if field == "status" and value:
            setattr(c, field, ContactStatus(value))
        else:
            setattr(c, field, value)

    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return c


@router.post("/{contact_id}/enrich")
async def enrich_contact(contact_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    _owned_contact(db, contact_id, user)
    background_tasks.add_task(run_enrichment_for_contact, contact_id, True)
    return {"message": "Enrichment started", "contact_id": contact_id, "progress_url": f"/api/contacts/{contact_id}/progress"}


@router.post("/{contact_id}/draft")
async def draft_email(contact_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    _owned_contact(db, contact_id, user)
    background_tasks.add_task(run_email_draft_for_contact, contact_id)
    return {"message": "Draft generation started", "contact_id": contact_id, "progress_url": f"/api/contacts/{contact_id}/progress"}


@router.post("/{contact_id}/approve-and-send")
async def approve_and_send(contact_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    if not c.email_subject or not c.email_body:
        raise HTTPException(400, "No email draft — run Draft first")

    c.status = ContactStatus.approved
    c.updated_at = datetime.utcnow()
    db.commit()
    background_tasks.add_task(run_send_email_for_contact, contact_id)
    return {"message": "Email queued for sending"}


@router.post("/bulk/pipeline")
async def bulk_pipeline(
    background_tasks: BackgroundTasks,
    campaign_id: Optional[int] = None,
    auto_send: bool = False,
    db: Session = Depends(get_db),
    user: Optional[User] = Depends(_current_user),
):
    q = _visible_query(db, user).filter(
        Contact.status.in_([ContactStatus.pending, ContactStatus.enriched, ContactStatus.error]),
        Contact.on_hold == False,  # noqa: E712
    )
    if campaign_id:
        q = q.filter(Contact.campaign_id == campaign_id)
    contacts = q.all()

    for contact in contacts:
        background_tasks.add_task(run_full_pipeline_for_contact, contact.id, auto_send)

    return {"message": f"Pipeline started for {len(contacts)} contacts"}


@router.post("/{contact_id}/mark-replied")
def mark_replied(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    c.status = ContactStatus.replied
    c.next_followup_at = None
    c.updated_at = datetime.utcnow()
    db.commit()
    return {"message": "Marked as replied"}


@router.post("/bulk/campaign")
def bulk_campaign(payload: BulkContactAction, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    if not payload.ids:
        raise HTTPException(400, "Select at least one contact")
    ids = _owned_ids(db, payload.ids, user)
    if not ids:
        raise HTTPException(403, "No accessible contacts in the selection")
    updated = db.query(Contact).filter(Contact.id.in_(ids)).update(
        {Contact.campaign_id: payload.campaign_id, Contact.updated_at: datetime.utcnow()},
        synchronize_session=False,
    )
    db.commit()
    return {"message": f"Updated campaign for {updated} contacts", "updated": updated}


@router.post("/bulk/delete")
def bulk_delete(payload: BulkContactAction, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    if not payload.ids:
        raise HTTPException(400, "Select at least one contact")
    ids = _owned_ids(db, payload.ids, user)
    if not ids:
        raise HTTPException(403, "No accessible contacts in the selection")
    deleted = db.query(Contact).filter(Contact.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    from backend.services.auth import log_audit
    log_audit(db, user.id if user else None, "contacts_deleted", f"Bulk-deleted {deleted} contacts")
    return {"message": f"Deleted {deleted} contacts", "deleted": deleted}


@router.post("/{contact_id}/reset-drafting")
def reset_stuck_drafting(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    if c.status in (ContactStatus.drafting, ContactStatus.enriching):
        c.status = ContactStatus.enriched if (c.linkedin_headline or c.linkedin_summary or c.linkedin_experience) else ContactStatus.pending
    c.error_message = None
    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return {"message": "Contact reset. You can regenerate the draft now.", "status": c.status.value}


@router.delete("/{contact_id}")
def delete_contact(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    db.delete(c)
    db.commit()
    return {"message": "Deleted"}


@router.post("/{contact_id}/campaign/{campaign_id}")
def add_contact_to_campaign(contact_id: int, campaign_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    c.campaign_id = campaign_id
    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return {"message": "Contact added to campaign", "campaign_id": campaign_id}


@router.delete("/{contact_id}/campaign")
def remove_contact_from_campaign(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    c.campaign_id = None
    c.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(c)
    return {"message": "Contact removed from campaign"}


# ── Multi-step pipeline (email → followup-1 → LinkedIn connect → ... ) ─────

@router.get("/{contact_id}/pipeline")
def get_contact_pipeline(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    return {"steps": pipeline_overview(c), "next_action": next_action_for_contact(c)}


@router.patch("/{contact_id}/pipeline/{step}")
def edit_pipeline_step(contact_id: int, step: str, payload: StepEditIn, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Lets the contact's owner (or an admin) hand-edit a generated step's
    subject/body without re-running AI generation (item 4)."""
    c = _owned_contact(db, contact_id, user)
    if step not in STEP_TYPES or step == "email":
        raise HTTPException(400, "Use PATCH /contacts/{id} with email_subject/email_body for the intro email step")
    from backend.services.pipeline import load_pipeline_state, save_pipeline_state
    state = load_pipeline_state(c)
    entry = state.get(step, {"status": "pending"})
    if entry.get("status") not in ("generated", "sent", "done"):
        raise HTTPException(400, "Generate this step first before editing it")
    entry["subject"] = payload.subject
    entry["body"] = payload.body
    state[step] = entry
    save_pipeline_state(db, c, state)
    return {"message": "Step content updated", "step": step, "content": entry}


@router.post("/{contact_id}/pipeline/{step}/generate")
async def generate_pipeline_step(contact_id: int, step: str, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    if step not in STEP_TYPES or step == "email":
        raise HTTPException(400, "Use /enrich or /draft for the intro email step")
    try:
        entry = await generate_step_content(db, c, step)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"message": "Generated", "step": step, "content": entry}


@router.post("/{contact_id}/pipeline/{step}/complete")
async def complete_pipeline_step(contact_id: int, step: str, payload: StepCompleteIn, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    if step not in STEP_TYPES or step == "email":
        raise HTTPException(400, "Use /approve-and-send for the intro email step")
    try:
        entry = await complete_step(db, c, step, send_email_now=payload.send)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"message": "Step completed", "step": step, "content": entry}


@router.post("/{contact_id}/pipeline/smart")
async def run_smart_step(contact_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Auto-detect what the next pipeline step is and execute the generation
    part of it (item 14/15/16 — smart pipeline progression)."""
    c = _owned_contact(db, contact_id, user)
    action = next_action_for_contact(c)
    background_tasks.add_task(run_smart_step_for_contact, contact_id)
    return {"message": f"Running: {action['label']}", "next_action": action}


# ── Bulk / batch pipeline with 1/N live progress ────────────────────────────

@router.post("/bulk/run-pipeline")
def bulk_run_pipeline(payload: BulkIds, background_tasks: BackgroundTasks, campaign_id: Optional[int] = None, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Runs the *smart* pipeline (auto-detects each contact's next step) across
    a set of contacts, tracked as a batch job so the UI can show 1/100, 2/100...
    Contacts on hold are always skipped."""
    if payload.ids:
        ids = _owned_ids(db, payload.ids, user)
        contacts = db.query(Contact).filter(Contact.id.in_(ids), Contact.on_hold == False).all()  # noqa: E712
    else:
        q = _visible_query(db, user).filter(Contact.status != ContactStatus.replied, Contact.on_hold == False)  # noqa: E712
        if campaign_id:
            q = q.filter(Contact.campaign_id == campaign_id)
        contacts = q.all()

    if not contacts:
        raise HTTPException(400, "No contacts to run the pipeline on (contacts on hold are skipped)")

    ids = [c.id for c in contacts]
    job_id = create_batch_job(ids)
    background_tasks.add_task(run_batch_pipeline, job_id, ids)
    return {"message": f"Pipeline started for {len(ids)} contacts", "job_id": job_id, "total": len(ids)}


@router.get("/pipeline/jobs/{job_id}")
def get_pipeline_job(job_id: str):
    job = get_batch_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/bulk/send-emails")
def bulk_send_emails(payload: BulkIds, background_tasks: BackgroundTasks, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    """Bulk-send: sends whatever email is ready for each selected contact —
    the intro email if it's draft_ready, OR any generated follow-up email step
    (follow-up 1/2/3/4) that's ready to go out. Held contacts are skipped."""
    q = _visible_query(db, user).filter(Contact.on_hold == False)  # noqa: E712
    if payload.ids:
        ids = _owned_ids(db, payload.ids, user)
        q = q.filter(Contact.id.in_(ids))
    else:
        q = q.filter(Contact.status != ContactStatus.replied)
    candidates = q.all()

    to_send_intro = []
    to_send_step = []  # (contact_id, step_key)
    for c in candidates:
        if c.status == ContactStatus.draft_ready:
            to_send_intro.append(c.id)
            continue
        action = next_action_for_contact(c)
        if action["action"] == "needs_review" and action["step"] and STEP_TYPES.get(action["step"], {}).get("kind") == "email":
            to_send_step.append((c.id, action["step"]))

    if not to_send_intro and not to_send_step:
        raise HTTPException(400, "No emails ready to send in this selection — generate or draft one first")

    if to_send_intro:
        db.query(Contact).filter(Contact.id.in_(to_send_intro)).update(
            {Contact.status: ContactStatus.approved, Contact.updated_at: datetime.utcnow()},
            synchronize_session=False,
        )
        db.commit()

    all_ids = to_send_intro + [cid for cid, _ in to_send_step]
    job_id = create_batch_job(all_ids)

    async def _send_all(job_id: str, intro_ids: List[int], step_pairs: List[tuple]):
        job = get_batch_job(job_id)
        for cid in intro_ids:
            await run_send_email_for_contact(cid)
            job["completed"] += 1
        db2 = SessionLocal()
        try:
            for cid, step in step_pairs:
                c2 = db2.query(Contact).filter(Contact.id == cid).first()
                if c2:
                    try:
                        await complete_step(db2, c2, step, send_email_now=True)
                    except Exception:
                        pass
                job["completed"] += 1
        finally:
            db2.close()
        job["status"] = "completed"
        job["finished_at"] = datetime.utcnow().isoformat()

    background_tasks.add_task(_send_all, job_id, to_send_intro, to_send_step)
    return {"message": f"Sending {len(all_ids)} emails", "job_id": job_id, "total": len(all_ids)}


# ── Dashboard notifications: batch counts of what's pending at each step ───

@router.get("/dashboard/notifications")
def dashboard_notifications(db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    contacts = _visible_query(db, user).filter(Contact.status != ContactStatus.replied, Contact.on_hold == False).all()  # noqa: E712
    counts: dict = {}
    for step_key in STEP_TYPES:
        counts[step_key] = 0
    for c in contacts:
        action = next_action_for_contact(c)
        if action["action"] in ("generate", "needs_review", "enrich_and_draft"):
            key = action["step"] or "email"
            counts[key] = counts.get(key, 0) + 1
    items = []
    for step, n in counts.items():
        meta = STEP_TYPES.get(step, {"label": step.replace("_", " ").title()})
        items.append({"step": step, "label": meta["label"], "count": n})
    return {"items": items}


# ── Download all generated content for one contact ─────────────────────────

@router.get("/{contact_id}/download")
def download_contact_content(contact_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = _owned_contact(db, contact_id, user)
    lines = [f"SalesFlow AI — Outreach content for {c.name} ({c.email})", "=" * 60, ""]
    for item in pipeline_overview(c):
        lines.append(f"## {item['label']} [{item['status']}]")
        if item.get("subject"):
            lines.append(f"Subject: {item['subject']}")
        lines.append(item.get("body") or "(not generated yet)")
        lines.append("")
    content = "\n".join(lines)
    filename = f"{c.name.replace(' ', '_')}_outreach_content.txt"
    return StreamingResponse(
        iter([content]),
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
