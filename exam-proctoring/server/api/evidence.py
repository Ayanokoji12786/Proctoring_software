"""Evidence snapshot upload/retrieval.

Snapshots are single still images (never continuous video), uploaded only
when the agent's local event policy allows it (see student-agent config).
Stored on disk with a retention window; a background task in main.py purges
expired files.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from auth.tokens import TokenError, require_admin_api_key, verify_student_token
from config import settings
from database.db import get_db
from models.models import Event, Evidence

logger = logging.getLogger("proctoring.api.evidence")
router = APIRouter(prefix="/api/evidence", tags=["evidence"])

# Magic-byte signatures. The multipart Content-Type is chosen by the client and
# is therefore a claim, not a fact; storing and later re-serving whatever bytes
# arrive under an image/* type is how a non-image ends up being served as one.
_MAGIC_BYTES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}


def _looks_like_declared_image(content_type: str, data: bytes) -> bool:
    return any(data.startswith(sig) for sig in _MAGIC_BYTES.get(content_type, ()))


@router.post("")
async def upload_evidence(
    session_id: str = Form(...),
    student_id: str = Form(...),
    token: str = Form(...),
    event_id: str | None = Form(default=None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    try:
        verify_student_token(token, session_id, student_id)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    if file.content_type not in settings.evidence_allowed_content_types_tuple:
        raise HTTPException(status_code=415, detail=f"unsupported content type: {file.content_type}")

    contents = await file.read(settings.evidence_max_bytes + 1)
    if len(contents) > settings.evidence_max_bytes:
        raise HTTPException(status_code=413, detail="evidence file too large")

    if not _looks_like_declared_image(file.content_type, contents):
        logger.warning("rejected evidence upload for student_id=%s: content does not match declared type", student_id)
        raise HTTPException(status_code=415, detail="file content does not match its declared image type")

    evidence_id = str(uuid.uuid4())
    ext = ".jpg" if file.content_type == "image/jpeg" else ".png"
    student_dir = os.path.join(settings.evidence_storage_dir, session_id, student_id)
    os.makedirs(student_dir, exist_ok=True)
    file_path = os.path.join(student_dir, f"{evidence_id}{ext}")
    with open(file_path, "wb") as f:
        f.write(contents)

    now = dt.datetime.now(dt.timezone.utc)
    expires_at = now + dt.timedelta(hours=settings.evidence_retention_hours)
    evidence = Evidence(
        id=evidence_id,
        event_id=event_id,
        student_id=student_id,
        session_id=session_id,
        file_path=file_path,
        content_type=file.content_type,
        captured_at=now,
        expires_at=expires_at,
    )
    db.add(evidence)
    # Flush so the evidence row exists before the event UPDATE below references
    # it - Event.evidence_id is a real (now-enforced) foreign key.
    db.flush()

    # Close the loop back to the event. The agent creates the event first and
    # uploads the snapshot after (it needs the event_id to name it), so
    # Event.evidence_id is NULL at insert time and stays that way unless it is
    # backfilled here - leaving the column permanently empty and the event
    # timeline unable to show which snapshot belongs to which event.
    #
    # The event is matched on (id, student_id, session_id), not id alone: this
    # endpoint is authenticated as a *student*, so accepting a bare event_id
    # would let one student attach their upload to another student's event.
    if event_id:
        event = (
            db.query(Event)
            .filter_by(id=event_id, student_id=student_id, session_id=session_id)
            .one_or_none()
        )
        if event is not None:
            event.evidence_id = evidence_id
        else:
            logger.warning(
                "evidence %s references event_id=%s that does not belong to student_id=%s",
                evidence_id, event_id, student_id,
            )

    db.commit()

    logger.info("evidence stored evidence_id=%s student_id=%s bytes=%d", evidence_id, student_id, len(contents))
    return {"evidence_id": evidence_id, "expires_at": expires_at}


@router.get("/{evidence_id}", dependencies=[Depends(require_admin_api_key)])
def get_evidence(evidence_id: str, db: Session = Depends(get_db)) -> FileResponse:
    evidence = db.get(Evidence, evidence_id)
    if evidence is None or evidence.deleted:
        raise HTTPException(status_code=404, detail="evidence not found or deleted")
    if not os.path.exists(evidence.file_path):
        raise HTTPException(status_code=404, detail="evidence file missing on disk")
    return FileResponse(evidence.file_path, media_type=evidence.content_type)


@router.get("", dependencies=[Depends(require_admin_api_key)])
def list_evidence(session_id: str, student_id: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    query = db.query(Evidence).filter_by(session_id=session_id, deleted=False)
    if student_id:
        query = query.filter_by(student_id=student_id)
    return [
        {
            "evidence_id": e.id,
            "event_id": e.event_id,
            "student_id": e.student_id,
            "captured_at": e.captured_at,
            "expires_at": e.expires_at,
        }
        for e in query.order_by(Evidence.captured_at.desc()).all()
    ]


def purge_expired_evidence() -> int:
    """Deletes expired evidence files from disk and marks DB rows deleted. Returns count purged."""
    from database.db import session_scope

    purged = 0
    now = dt.datetime.now(dt.timezone.utc)
    with session_scope() as db:
        expired = db.query(Evidence).filter(Evidence.expires_at < now, Evidence.deleted.is_(False)).all()
        for evidence in expired:
            try:
                if os.path.exists(evidence.file_path):
                    os.remove(evidence.file_path)
            except OSError as exc:
                logger.warning("failed to delete evidence file %s: %s", evidence.file_path, exc)
            evidence.deleted = True
            purged += 1
    if purged:
        logger.info("purged %d expired evidence file(s)", purged)
    return purged
