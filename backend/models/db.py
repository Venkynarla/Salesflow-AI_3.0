from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, DateTime,
    Boolean, ForeignKey, Enum as SAEnum
)
from sqlalchemy.orm import relationship, declarative_base
import enum

Base = declarative_base()


class ContactStatus(str, enum.Enum):
    pending      = "pending"       # just uploaded
    enriching    = "enriching"     # LinkedIn fetch in progress
    enriched     = "enriched"      # profile data fetched
    drafting     = "drafting"      # AI writing email
    draft_ready  = "draft_ready"   # email draft ready for review
    approved     = "approved"      # human approved, queued to send
    sent         = "sent"          # initial email sent
    followed_up  = "followed_up"   # at least one follow-up sent
    replied      = "replied"       # prospect replied — stop sequence
    bounced      = "bounced"       # email bounced
    error        = "error"         # something went wrong


# ── Multi-touch pipeline definition ─────────────────────────────────────────
# Fixed catalogue of step "types" that can be arranged into a campaign sequence.
STEP_TYPES = {
    "email":             {"label": "Intro Email",       "kind": "email"},
    "followup_1":        {"label": "Follow-up 1",        "kind": "email"},
    "linkedin_connect":  {"label": "LinkedIn Connect",   "kind": "linkedin"},
    "followup_email_2":  {"label": "Follow-up Email 2",  "kind": "email"},
    "linkedin_message":  {"label": "LinkedIn Message",   "kind": "linkedin"},
    "cold_call":         {"label": "Cold Call Script",   "kind": "call"},
    "followup_email_3":  {"label": "Follow-up Email 3",  "kind": "email"},
    "followup_email_4":  {"label": "Follow-up Email 4",  "kind": "email"},
}

DEFAULT_PIPELINE_STEPS = [
    "email", "followup_1", "linkedin_connect", "followup_email_2",
    "linkedin_message", "cold_call", "followup_email_3", "followup_email_4",
]

LIST_TYPES = {
    "prospect":       "Prospect (pipeline)",
    "lead":           "Lead",
    "client":         "Client",
    "not_interested": "Not Interested",
}


class Contact(Base):
    __tablename__ = "contacts"

    id            = Column(Integer, primary_key=True, index=True)
    campaign_id   = Column(Integer, ForeignKey("campaigns.id"), nullable=True)
    owner_id      = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    # JSON string: {step_key: {status, subject, body, script, generated_at, sent_at}}
    pipeline_state = Column(Text, nullable=True)

    # lead | client | not_interested | prospect (default pipeline contact)
    list_type     = Column(String(30), default="prospect", index=True)
    on_hold       = Column(Boolean, default=False, index=True)  # duplicates put on hold get no pipeline actions

    # Basic info from uploaded CSV
    name          = Column(String(200), nullable=False)
    email         = Column(String(200), nullable=False, unique=True, index=True)
    linkedin_url  = Column(String(500), nullable=True)
    company       = Column(String(200), nullable=True)
    job_title     = Column(String(200), nullable=True)

    # Enriched data (from LinkedIn scrape)
    linkedin_headline    = Column(Text, nullable=True)
    linkedin_summary     = Column(Text, nullable=True)
    linkedin_experience  = Column(Text, nullable=True)  # JSON string
    linkedin_skills      = Column(Text, nullable=True)  # comma-separated
    enriched_at          = Column(DateTime, nullable=True)

    # Email drafts & sending
    email_subject        = Column(Text, nullable=True)
    email_body           = Column(Text, nullable=True)
    email_sent_at        = Column(DateTime, nullable=True)
    followup_count       = Column(Integer, default=0)
    last_followup_at     = Column(DateTime, nullable=True)
    next_followup_at     = Column(DateTime, nullable=True)

    # Status
    status        = Column(SAEnum(ContactStatus), default=ContactStatus.pending, index=True)
    error_message = Column(Text, nullable=True)

    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    campaign      = relationship("Campaign", back_populates="contacts")
    activities    = relationship("Activity", back_populates="contact", cascade="all, delete-orphan")


class Campaign(Base):
    __tablename__ = "campaigns"

    id            = Column(Integer, primary_key=True, index=True)
    owner_id      = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    name          = Column(String(200), nullable=False)
    description   = Column(Text, nullable=True)

    # JSON list of step keys, e.g. ["email","followup_1","linkedin_connect",...]
    pipeline_steps = Column(Text, nullable=True)

    # Your product / offer context (fed to AI for personalization)
    your_name         = Column(String(200), nullable=True)
    your_company      = Column(String(200), nullable=True)
    your_role         = Column(String(200), nullable=True)
    value_proposition = Column(Text, nullable=True)   # what you're selling / offering

    # Follow-up schedule (days after initial send)
    followup_days     = Column(String(50), default="3,7,14")  # e.g. "3,7,14"
    max_followups     = Column(Integer, default=3)

    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    contacts      = relationship("Contact", back_populates="campaign", cascade="all, delete-orphan")


class Activity(Base):
    __tablename__ = "activities"

    id            = Column(Integer, primary_key=True, index=True)
    contact_id    = Column(Integer, ForeignKey("contacts.id"), nullable=False)
    activity_type = Column(String(50), nullable=False)   # enriched | email_sent | followup_sent | replied | error
    detail        = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    contact       = relationship("Contact", back_populates="activities")


class User(Base):
    """Lightweight user account for multi-user / team setups.

    The app works perfectly well with zero users created (legacy single-user
    mode — everything is visible to everyone). Once a user registers and logs
    in, new campaigns/contacts they create are tagged with their owner_id and
    the UI can filter to "My data" vs "Everyone".
    """
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(200), nullable=False)
    email         = Column(String(200), nullable=False, unique=True, index=True)
    password_hash = Column(String(300), nullable=False)
    role          = Column(String(30), default="member")   # admin | member
    is_active     = Column(Boolean, default=True)           # False = restricted / login blocked
    created_at    = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)


class SkippedImport(Base):
    """Records every row skipped during a CSV/Excel upload, with the reason,
    so nothing silently disappears."""
    __tablename__ = "skipped_imports"

    id            = Column(Integer, primary_key=True, index=True)
    owner_id      = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    attempted_name  = Column(String(200), nullable=True)
    attempted_email = Column(String(200), nullable=True)
    list_name       = Column(String(200), nullable=True)   # campaign name or list_type at time of upload
    reason          = Column(String(300), nullable=False)
    created_at      = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    """Platform-wide activity log — captures every user action, not just the
    contact-tied ones in Activity (login, campaign created, contacts added/
    uploaded, etc.) so the admin activity monitor actually sees everything."""
    __tablename__ = "audit_logs"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event_type    = Column(String(50), nullable=False, index=True)  # login | register | campaign_created | contact_created | contacts_uploaded | contact_deleted | campaign_deleted
    detail        = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, index=True)


class Session(Base):
    """Simple bearer-token session, persisted so logins survive a restart."""
    __tablename__ = "sessions"

    id            = Column(Integer, primary_key=True, index=True)
    token         = Column(String(64), nullable=False, unique=True, index=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)
