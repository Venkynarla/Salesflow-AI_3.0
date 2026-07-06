"""Campaigns API routes — owner-scoped for multi-user isolation.

Visibility rules:
- Admins see every campaign.
- Logged-in members see only campaigns they created.
- Guests (no login) see only "unowned" campaigns (owner_id is null) — the
  shared legacy/demo pool, so the app still works fully without accounts.
"""

import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.models.database import get_db
from backend.models.db import Campaign, DEFAULT_PIPELINE_STEPS, STEP_TYPES, User
from backend.services.auth import get_user_from_token, is_admin

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


def _current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> Optional[User]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    user = get_user_from_token(db, authorization.split(" ", 1)[1].strip())
    if user and not user.is_active:
        raise HTTPException(403, "Your account has been restricted. Contact an admin.")
    return user


def _visible_query(db: Session, user: Optional[User]):
    q = db.query(Campaign)
    if user and is_admin(user):
        return q
    if user:
        return q.filter(Campaign.owner_id == user.id)
    return q.filter(Campaign.owner_id.is_(None))


def _check_access(c: Campaign, user: Optional[User]):
    if user and is_admin(user):
        return
    owner_ok = (user and c.owner_id == user.id) or (not user and c.owner_id is None)
    if not owner_ok:
        raise HTTPException(403, "You don't have access to this campaign")


class CampaignCreate(BaseModel):
    name: str
    description: Optional[str] = None
    your_name: Optional[str] = None
    your_company: Optional[str] = None
    your_role: Optional[str] = None
    value_proposition: Optional[str] = None
    followup_days: str = "3,7,14"
    max_followups: int = 3
    pipeline_steps: Optional[List[str]] = None


class CampaignOut(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    your_name: Optional[str] = None
    your_company: Optional[str] = None
    your_role: Optional[str] = None
    value_proposition: Optional[str] = None
    followup_days: str
    max_followups: int
    is_active: bool
    created_at: datetime
    contact_count: Optional[int] = 0
    pipeline_steps: List[str] = []
    owner_name: Optional[str] = None
    owner_id: Optional[int] = None

    class Config:
        from_attributes = True


def _to_out(c: Campaign, db: Optional[Session] = None) -> CampaignOut:
    try:
        steps = json.loads(c.pipeline_steps) if c.pipeline_steps else list(DEFAULT_PIPELINE_STEPS)
        if not isinstance(steps, list) or not steps:
            steps = list(DEFAULT_PIPELINE_STEPS)
    except Exception:
        steps = list(DEFAULT_PIPELINE_STEPS)
    owner_name = None
    if c.owner_id and db is not None:
        owner = db.query(User).filter(User.id == c.owner_id).first()
        owner_name = owner.name if owner else None
    return CampaignOut(
        id=c.id, name=c.name, description=c.description,
        your_name=c.your_name, your_company=c.your_company, your_role=c.your_role,
        value_proposition=c.value_proposition, followup_days=c.followup_days,
        max_followups=c.max_followups, is_active=c.is_active, created_at=c.created_at,
        contact_count=len(c.contacts), pipeline_steps=steps, owner_name=owner_name, owner_id=c.owner_id,
    )


@router.get("/step-catalogue")
def step_catalogue():
    """Available step types for the visual campaign builder (drag-and-drop palette)."""
    return {"steps": [{"key": k, **v} for k, v in STEP_TYPES.items()], "default_sequence": DEFAULT_PIPELINE_STEPS}


@router.get("/", response_model=List[CampaignOut])
def list_campaigns(db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    campaigns = _visible_query(db, user).order_by(Campaign.created_at.desc()).all()
    return [_to_out(c, db) for c in campaigns]


@router.post("/", response_model=CampaignOut)
def create_campaign(payload: CampaignCreate, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    data = payload.dict(exclude={"pipeline_steps"})
    steps = payload.pipeline_steps or list(DEFAULT_PIPELINE_STEPS)
    steps = [s for s in steps if s in STEP_TYPES] or list(DEFAULT_PIPELINE_STEPS)
    campaign = Campaign(**data, pipeline_steps=json.dumps(steps), owner_id=user.id if user else None)
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    from backend.services.auth import log_audit
    log_audit(db, user.id if user else None, "campaign_created", f"Created campaign '{campaign.name}'")
    return _to_out(campaign, db)


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not c:
        raise HTTPException(404, "Campaign not found")
    _check_access(c, user)
    return _to_out(c, db)


@router.patch("/{campaign_id}", response_model=CampaignOut)
def update_campaign(campaign_id: int, payload: CampaignCreate, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not c:
        raise HTTPException(404, "Campaign not found")
    _check_access(c, user)
    data = payload.dict(exclude_unset=True, exclude={"pipeline_steps"})
    for field, value in data.items():
        setattr(c, field, value)
    if payload.pipeline_steps is not None:
        steps = [s for s in payload.pipeline_steps if s in STEP_TYPES] or list(DEFAULT_PIPELINE_STEPS)
        c.pipeline_steps = json.dumps(steps)
    db.commit()
    db.refresh(c)
    return _to_out(c, db)


@router.delete("/{campaign_id}")
def delete_campaign(campaign_id: int, db: Session = Depends(get_db), user: Optional[User] = Depends(_current_user)):
    c = db.query(Campaign).filter(Campaign.id == campaign_id).first()
    if not c:
        raise HTTPException(404, "Campaign not found")
    _check_access(c, user)
    name = c.name
    db.delete(c)
    db.commit()
    from backend.services.auth import log_audit
    log_audit(db, user.id if user else None, "campaign_deleted", f"Deleted campaign '{name}'")
    return {"message": "Deleted"}
