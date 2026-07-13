"""
Sales Automation Platform — FastAPI entry point.
Run with: uvicorn main:app --reload --port 8000
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
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

# Restrict CORS to known frontend origin(s). Set FRONTEND_ORIGINS in your
# environment (comma-separated) for production, e.g.:
#   FRONTEND_ORIGINS=https://salesflow-ai-3-0.onrender.com
_frontend_origins = os.getenv("FRONTEND_ORIGINS", "").split(",")
_frontend_origins = [o.strip() for o in _frontend_origins if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_frontend_origins or ["*"],
    allow_credentials=False,  # Bearer-token auth doesn't need cookies/credentials
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Security headers ──────────────────────────────────────────────────────────

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    response.headers["Strict-Transport-Security"] = (
        "max-age=63072000; includeSubDomains; preload"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    return response

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


# Path patterns that should never be treated as a frontend route — dotfiles,
# common config/backup/credential filenames, and version-control paths.
# Requesting any of these now returns a real 404 instead of your index.html,
# so scanners (and attackers) get an honest signal instead of a false "200 OK".
_BLOCKED_PATH_MARKERS = (
    ".env", ".git", ".aws", ".htpasswd", ".vscode", ".ds_store",
    "wp-config", "config.php", "web.config", "docker-compose",
    "backup.", "id_rsa", "phpinfo", "server-status", "composer.json",
    "package.json.bak",
)


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    lowered = full_path.lower()
    if any(marker in lowered for marker in _BLOCKED_PATH_MARKERS):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    index = os.path.join("frontend", "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return JSONResponse(
        status_code=404,
        content={"message": "Frontend not found — place index.html in /frontend/"},
    )
