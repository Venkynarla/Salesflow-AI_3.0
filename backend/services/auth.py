"""
Lightweight auth service.

No external auth deps required — uses hashlib PBKDF2 for password hashing and
a random urlsafe token stored in the `sessions` table for bearer auth.

Design goal: the app must keep working with ZERO users registered (legacy /
single-user mode). Endpoints that accept an optional `Authorization: Bearer`
header will attach owner_id when a valid session is present, and silently
skip owner filtering otherwise.
"""

import hashlib
import os
import secrets
from typing import Optional

from fastapi import Header
from sqlalchemy.orm import Session as DBSession

from backend.models.db import User, Session as SessionModel


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


def create_session(db: DBSession, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    db.add(SessionModel(token=token, user_id=user_id))
    db.commit()
    return token


def get_user_from_token(db: DBSession, token: str) -> Optional[User]:
    if not token:
        return None
    sess = db.query(SessionModel).filter(SessionModel.token == token).first()
    if not sess:
        return None
    return db.query(User).filter(User.id == sess.user_id).first()


def is_admin(user: Optional[User]) -> bool:
    return bool(user and user.role == "admin")


def log_audit(db: DBSession, user_id: Optional[int], event_type: str, detail: str = "") -> None:
    """Records a platform-wide audit event (login, campaign created, contacts
    uploaded, etc.) — separate from the per-contact Activity log, so admin
    activity monitoring actually sees everything a user does, not just
    pipeline steps run against a specific contact."""
    from backend.models.db import AuditLog
    try:
        db.add(AuditLog(user_id=user_id, event_type=event_type, detail=detail[:500] if detail else None))
        db.commit()
    except Exception:
        db.rollback()
