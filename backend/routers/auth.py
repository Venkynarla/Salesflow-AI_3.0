"""Auth API routes — lightweight multi-user support with admin controls."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.models.database import get_db
from backend.models.db import User
from backend.services.auth import (
    create_session,
    get_user_from_token,
    hash_password,
    is_admin,
    log_audit,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterIn(BaseModel):
    name: str
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class RoleIn(BaseModel):
    role: str  # "admin" | "member"


class ActiveIn(BaseModel):
    is_active: bool


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    is_active: bool
    created_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AuthOut(BaseModel):
    token: str
    user: UserOut


def current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> Optional[User]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return get_user_from_token(db, authorization.split(" ", 1)[1].strip())


def require_user(user: Optional[User] = Depends(current_user)) -> User:
    if not user:
        raise HTTPException(401, "Sign in required")
    if not user.is_active:
        raise HTTPException(403, "Your account has been restricted. Contact an admin.")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not is_admin(user):
        raise HTTPException(403, "Admin access required")
    return user


@router.post("/register", response_model=AuthOut)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "Valid email is required")
    if len(payload.password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "An account with this email already exists")
    is_first_user = db.query(User).count() == 0
    user = User(
        name=payload.name.strip() or email.split("@")[0],
        email=email,
        password_hash=hash_password(payload.password),
        role="admin" if is_first_user else "member",
        is_active=True,
        last_login_at=datetime.utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_session(db, user.id)
    log_audit(db, user.id, "register", f"{user.name} registered" + (" (became first admin)" if is_first_user else ""))
    return {"token": token, "user": user}


@router.post("/login", response_model=AuthOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "Your account has been restricted. Contact an admin.")
    user.last_login_at = datetime.utcnow()
    db.commit()
    token = create_session(db, user.id)
    log_audit(db, user.id, "login", f"{user.name} logged in")
    return {"token": token, "user": user}


@router.get("/me", response_model=Optional[UserOut])
def me(user: Optional[User] = Depends(current_user)):
    return user


# ── Admin-only user management ──────────────────────────────────────────────

@router.get("/users", response_model=List[UserOut])
def list_users(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    return db.query(User).order_by(User.name).all()


@router.post("/users/{user_id}/role", response_model=UserOut)
def set_user_role(user_id: int, payload: RoleIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    if payload.role not in ("admin", "member"):
        raise HTTPException(400, "role must be 'admin' or 'member'")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == admin.id and payload.role != "admin":
        admins_left = db.query(User).filter(User.role == "admin", User.id != admin.id).count()
        if admins_left == 0:
            raise HTTPException(400, "You are the only admin — promote someone else first")
    target.role = payload.role
    db.commit()
    db.refresh(target)
    return target


@router.post("/users/{user_id}/active", response_model=UserOut)
def set_user_active(user_id: int, payload: ActiveIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == admin.id and not payload.is_active:
        raise HTTPException(400, "You can't restrict your own account")
    target.is_active = payload.is_active
    db.commit()
    db.refresh(target)
    return target


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    from backend.models.db import Contact, Campaign
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "User not found")
    if target.id == admin.id:
        raise HTTPException(400, "You can't delete your own account while logged in as it")
    db.query(Contact).filter(Contact.owner_id == user_id).update({"owner_id": None})
    db.query(Campaign).filter(Campaign.owner_id == user_id).update({"owner_id": None})
    db.delete(target)
    db.commit()
    return {"message": "User deleted. Their campaigns/contacts remain but become unowned."}


@router.get("/activity")
def admin_activity_feed(limit: int = 300, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Admin-only: a unified, live feed of what every user has been doing —
    logins, campaigns created, contacts added/uploaded, plus every pipeline
    action (enrich, draft, send, step completions, etc.) — across the whole
    platform, with a per-user action-count summary."""
    from backend.models.db import Activity, AuditLog, Contact

    users_by_id = {u.id: u for u in db.query(User).all()}

    def label_for(user_id):
        u = users_by_id.get(user_id)
        return u.name if u else "Guest / unassigned"

    feed = []
    per_user_counts: dict = {}

    # Platform-wide events: login, register, campaign_created, contacts_uploaded, etc.
    audit_rows = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()
    for a in audit_rows:
        owner_label = label_for(a.user_id)
        per_user_counts[owner_label] = per_user_counts.get(owner_label, 0) + 1
        feed.append({
            "id": f"audit-{a.id}",
            "activity_type": a.event_type,
            "detail": a.detail,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "contact_id": None,
            "contact_name": None,
            "user": owner_label,
        })

    # Per-contact pipeline events: enriched, draft_ready, email_sent, step_generated, etc.
    contact_rows = (
        db.query(Activity, Contact)
        .join(Contact, Activity.contact_id == Contact.id)
        .order_by(Activity.created_at.desc())
        .limit(limit)
        .all()
    )
    for activity, contact in contact_rows:
        owner_label = label_for(contact.owner_id)
        per_user_counts[owner_label] = per_user_counts.get(owner_label, 0) + 1
        feed.append({
            "id": f"activity-{activity.id}",
            "activity_type": activity.activity_type,
            "detail": activity.detail,
            "created_at": activity.created_at.isoformat() if activity.created_at else None,
            "contact_id": contact.id,
            "contact_name": contact.name,
            "user": owner_label,
        })

    feed.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return {"feed": feed[:limit], "per_user_counts": per_user_counts}


@router.get("/users-overview")
def users_overview(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Admin-only dashboard data (item 2/3): every user, plus how many
    campaigns/contacts they've created, so admins can see platform-wide usage
    at a glance and drill into any one user."""
    from backend.models.db import Campaign, Contact

    users = db.query(User).order_by(User.name).all()
    out = []
    for u in users:
        campaign_count = db.query(Campaign).filter(Campaign.owner_id == u.id).count()
        contact_count = db.query(Contact).filter(Contact.owner_id == u.id).count()
        out.append({
            "id": u.id, "name": u.name, "email": u.email, "role": u.role,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "campaign_count": campaign_count,
            "contact_count": contact_count,
        })
    return {"users": out}


@router.get("/users/{user_id}/dashboard")
def user_dashboard(user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    """Admin-only drill-down (item 3): click a user's name to see everything
    they own — their campaigns, their contacts and stats, their recent
    activity — same shape as the main dashboard but scoped to this one user."""
    from backend.models.db import Activity, AuditLog, Campaign, Contact, ContactStatus, SkippedImport

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(404, "User not found")

    campaigns = db.query(Campaign).filter(Campaign.owner_id == user_id).order_by(Campaign.created_at.desc()).all()
    contacts_q = db.query(Contact).filter(Contact.owner_id == user_id)
    total = contacts_q.count()
    by_status = {s.value: contacts_q.filter(Contact.status == s).count() for s in ContactStatus}
    skipped_count = db.query(SkippedImport).filter(SkippedImport.owner_id == user_id).count()

    audit_rows = db.query(AuditLog).filter(AuditLog.user_id == user_id).order_by(AuditLog.created_at.desc()).limit(50).all()
    contact_activity_rows = (
        db.query(Activity)
        .join(Contact, Activity.contact_id == Contact.id)
        .filter(Contact.owner_id == user_id)
        .order_by(Activity.created_at.desc())
        .limit(50)
        .all()
    )
    recent = [
        {"type": a.event_type, "detail": a.detail, "created_at": a.created_at.isoformat() if a.created_at else None}
        for a in audit_rows
    ] + [
        {"type": a.activity_type, "detail": a.detail, "created_at": a.created_at.isoformat() if a.created_at else None}
        for a in contact_activity_rows
    ]
    recent.sort(key=lambda x: x["created_at"] or "", reverse=True)

    return {
        "user": {
            "id": target.id, "name": target.name, "email": target.email, "role": target.role,
            "is_active": target.is_active,
            "created_at": target.created_at.isoformat() if target.created_at else None,
            "last_login_at": target.last_login_at.isoformat() if target.last_login_at else None,
        },
        "stats": {"total": total, **by_status, "skipped": skipped_count},
        "campaigns": [{"id": c.id, "name": c.name, "contact_count": len(c.contacts), "created_at": c.created_at.isoformat()} for c in campaigns],
        "recent_activity": recent[:50],
    }
