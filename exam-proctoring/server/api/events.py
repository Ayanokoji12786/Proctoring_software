"""Query endpoints for the event timeline / dashboard filtering."""
from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from auth.tokens import require_admin_api_key
from database.db import get_db
from models.models import Event

router = APIRouter(prefix="/api/events", tags=["events"], dependencies=[Depends(require_admin_api_key)])


@router.get("")
def query_events(
    session_id: str,
    student_id: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = Query(default=200, le=1000),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = db.query(Event).filter(Event.session_id == session_id)
    if student_id:
        query = query.filter(Event.student_id == student_id)
    if event_type:
        query = query.filter(Event.event_type == event_type)
    if severity:
        query = query.filter(Event.severity == severity)
    if since:
        query = query.filter(Event.timestamp >= since)
    if until:
        query = query.filter(Event.timestamp <= until)

    rows = query.order_by(Event.timestamp.desc()).limit(limit).all()
    return [
        {
            "event_id": e.id,
            "session_id": e.session_id,
            "student_id": e.student_id,
            "event_type": e.event_type,
            "severity": e.severity,
            "timestamp": e.timestamp,
            "metadata": json.loads(e.metadata_json or "{}"),
            "evidence_id": e.evidence_id,
        }
        for e in rows
    ]
