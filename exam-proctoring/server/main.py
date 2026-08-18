"""FastAPI Server entrypoint.

Responsibilities (per architecture): authentication, student connection
registry, event validation, event broadcasting to the admin dashboard, and
exam session management.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import evidence, events, health, sessions
from config import settings
from database.db import init_db, session_scope
from models.models import StudentSession
from websocket import admin_ws, student_ws
from websocket.manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("proctoring.server")

HEARTBEAT_CHECK_INTERVAL = 5
EVIDENCE_CLEANUP_INTERVAL = 3600


async def _heartbeat_watchdog() -> None:
    """Marks students offline (and notifies the dashboard) if their heartbeat lapses."""
    while True:
        await asyncio.sleep(HEARTBEAT_CHECK_INTERVAL)
        try:
            for session_id, student_id in manager.offline_candidates(settings.heartbeat_timeout_seconds):
                logger.warning(
                    "student heartbeat timeout, marking offline: student_id=%s session_id=%s", student_id, session_id
                )
                state = manager.get_state(student_id, session_id)
                manager.student_sockets.pop((session_id, student_id), None)
                if state:
                    state.status = "offline"
                    await manager.broadcast_status(state)
                with session_scope() as db:
                    # Scoped to the timed-out enrollment only: a student enrolled
                    # in more than one exam must not be marked offline in all of
                    # them because one went quiet.
                    for ss in (
                        db.query(StudentSession)
                        .filter_by(student_id=student_id, session_id=session_id, status="online")
                        .all()
                    ):
                        ss.status = "offline"
                        ss.left_at = dt.datetime.now(dt.timezone.utc)
        except Exception:
            logger.exception("heartbeat watchdog iteration failed")


async def _evidence_cleanup_loop() -> None:
    from api.evidence import purge_expired_evidence

    while True:
        try:
            purge_expired_evidence()
        except Exception:
            logger.exception("evidence cleanup iteration failed")
        await asyncio.sleep(EVIDENCE_CLEANUP_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting proctoring server (env=%s)", settings.environment)
    init_db()
    watchdog_task = asyncio.create_task(_heartbeat_watchdog())
    cleanup_task = asyncio.create_task(_evidence_cleanup_loop())
    try:
        yield
    finally:
        watchdog_task.cancel()
        cleanup_task.cancel()
        logger.info("shutting down proctoring server")


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# `allow_credentials=True` combined with a wildcard origin is an invalid (and
# unsafe) CORS configuration: Starlette then reflects whatever Origin the
# caller sends, which is strictly weaker than the wildcard it looks like. This
# API authenticates with an explicit header rather than cookies, so credentialed
# cross-origin requests aren't needed at all when origins are wildcarded.
_wildcard_origins = "*" in settings.cors_allow_origins_list
if _wildcard_origins:
    logger.warning(
        "CORS is configured to allow all origins - set CORS_ALLOW_ORIGINS to your "
        "dashboard's origin before exposing this server publicly"
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins_list,
    allow_credentials=not _wildcard_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    # Evidence is served with a client-supplied content type; nosniff stops a
    # browser from re-interpreting a stored file as something executable.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.middleware("http")
async def _no_cache_dashboard_assets(request, call_next):
    """Force revalidation on every dashboard asset load.

    Without this, browsers may heuristically cache index.html/app.js/styles.css
    and keep serving a stale dashboard after this server (and its bundled
    static files) are redeployed/updated, since StaticFiles alone doesn't
    send a Cache-Control header forbidding that.
    """
    response = await call_next(request)
    if request.url.path.startswith("/dashboard"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response

app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(evidence.router)
app.include_router(events.router)
app.include_router(student_ws.router)
app.include_router(admin_ws.router)

try:
    app.mount("/dashboard", StaticFiles(directory="../dashboard", html=True), name="dashboard")
except RuntimeError:
    logger.warning("dashboard static directory not found; skipping static mount")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
