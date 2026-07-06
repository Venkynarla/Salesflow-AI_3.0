"""
Sales Automation Platform — FastAPI entry point.
Run with: uvicorn main:app --reload --port 8000
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from backend.models.database import init_db, SessionLocal
from backend.routers import contacts, campaigns, auth
from backend.services.pipeline import process_due_followups


# ── Scheduler ──────────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()


async def followup_job():
    """Scheduled job: check and send due follow-ups."""
    db = SessionLocal()
    try:
        await process_due_followups(db)
    finally:
        db.close()


# ── App lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    logger.info("Database initialised")

    scheduler.add_job(followup_job, "interval", hours=1, id="followup_scheduler")
    scheduler.start()
    logger.info("Follow-up scheduler started (runs every hour)")

    yield

    # Shutdown
    scheduler.shutdown()
    logger.info("Scheduler stopped")


# ── FastAPI app ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Inside Sales Automation Platform",
    description="Automated LinkedIn enrichment → NVIDIA AI email personalisation → scheduled outreach",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(contacts.router, prefix="/api")
app.include_router(campaigns.router, prefix="/api")
app.include_router(auth.router, prefix="/api")


# ── Health check ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "sales-automation"}


# ── Serve frontend ─────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    index = os.path.join("frontend", "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "Frontend not found — place index.html in /frontend/"}
