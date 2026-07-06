from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from backend.models.db import Base
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./sales_automation.db")
# Supabase (and some other providers) hand out URLs with the old "postgres://"
# scheme, which SQLAlchemy 2.x no longer recognizes — normalize it.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Columns added after the initial release — auto-added to existing DBs so upgrading
# doesn't require dropping data. (SQLite/Postgres both support simple ADD COLUMN.)
_NEW_COLUMNS = {
    "contacts": [
        ("owner_id", "INTEGER"),
        ("pipeline_state", "TEXT"),
        ("list_type", "VARCHAR(30) DEFAULT 'prospect'"),
        ("on_hold", "BOOLEAN DEFAULT FALSE"),
    ],
    "campaigns": [
        ("owner_id", "INTEGER"),
        ("pipeline_steps", "TEXT"),
    ],
    "users": [
        ("is_active", "BOOLEAN DEFAULT TRUE"),
        ("last_login_at", "TIMESTAMP"),
    ],
}


def _run_migrations():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in _NEW_COLUMNS.items():
            if table not in existing_tables:
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table)}
            for col_name, col_type in columns:
                if col_name not in existing_cols:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"))


def init_db():
    Base.metadata.create_all(bind=engine)
    try:
        _run_migrations()
    except Exception:
        # Non-fatal — worst case, a fresh table already has the column from create_all.
        pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
