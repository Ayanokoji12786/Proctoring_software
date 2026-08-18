"""REST endpoints for exam session + student enrollment management (proctor/admin use)."""
from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth.tokens import create_student_token, require_admin_api_key
from database.db import get_db
from models.models import ExamSession, Student, StudentSession
from schemas.schemas import (
    CreateSessionRequest,
    CreateSessionResponse,
    EnrollRequest,
    EnrollResponse,
    StudentStatusOut,
)
from websocket.manager import manager

router = APIRouter(prefix="/api", tags=["sessions"], dependencies=[Depends(require_admin_api_key)])


@router.post("/sessions", response_model=CreateSessionResponse)
def create_session(req: CreateSessionRequest, db: Session = Depends(get_db)) -> CreateSessionResponse:
    session_id = req.session_id or f"exam_{uuid.uuid4().hex[:8]}"
    if db.get(ExamSession, session_id) is not None:
        raise HTTPException(status_code=409, detail="session_id already exists")
    exam_session = ExamSession(session_id=session_id, name=req.name)
    db.add(exam_session)
    db.commit()
    db.refresh(exam_session)
    return CreateSessionResponse(session_id=exam_session.session_id, name=exam_session.name, created_at=exam_session.created_at)


@router.post("/sessions/{session_id}/end")
async def end_session(session_id: str, db: Session = Depends(get_db)) -> dict:
    exam_session = db.get(ExamSession, session_id)
    if exam_session is None:
        raise HTTPException(status_code=404, detail="session not found")
    exam_session.ended_at = dt.datetime.now(dt.timezone.utc)

    # Mark every enrollment offline so the DB doesn't keep claiming students are
    # connected to an exam that is over.
    for student_session in db.query(StudentSession).filter_by(session_id=session_id, status="online").all():
        student_session.status = "offline"
        student_session.left_at = exam_session.ended_at
    db.commit()

    # Ending the exam has to actually stop monitoring: rejecting *new*
    # connections isn't enough while already-connected agents keep streaming.
    disconnected = await manager.close_session(session_id)

    return {"session_id": session_id, "ended_at": exam_session.ended_at, "agents_disconnected": disconnected}


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)) -> list[dict]:
    sessions = db.query(ExamSession).order_by(ExamSession.created_at.desc()).all()
    return [
        {"session_id": s.session_id, "name": s.name, "created_at": s.created_at, "ended_at": s.ended_at}
        for s in sessions
    ]


@router.post("/sessions/{session_id}/students", response_model=EnrollResponse)
def enroll_student(session_id: str, req: EnrollRequest, db: Session = Depends(get_db)) -> EnrollResponse:
    if req.session_id != session_id:
        raise HTTPException(status_code=400, detail="session_id mismatch between path and body")

    exam_session = db.get(ExamSession, session_id)
    if exam_session is None:
        raise HTTPException(status_code=404, detail="session not found")

    student = db.get(Student, req.student_id)
    if student is None:
        student = Student(student_id=req.student_id, display_name=req.display_name)
        db.add(student)
    else:
        student.display_name = req.display_name

    student_session = (
        db.query(StudentSession)
        .filter_by(session_id=session_id, student_id=req.student_id)
        .one_or_none()
    )
    token, expires_at, jti = create_student_token(session_id, req.student_id)
    if student_session is None:
        student_session = StudentSession(session_id=session_id, student_id=req.student_id, token_jti=jti)
        db.add(student_session)
    else:
        student_session.token_jti = jti

    db.commit()
    return EnrollResponse(session_id=session_id, student_id=req.student_id, token=token, expires_at=expires_at)


@router.get("/sessions/{session_id}/students", response_model=list[StudentStatusOut])
def list_students(session_id: str, db: Session = Depends(get_db)) -> list[StudentStatusOut]:
    exam_session = db.get(ExamSession, session_id)
    if exam_session is None:
        raise HTTPException(status_code=404, detail="session not found")

    results: list[StudentStatusOut] = []
    for student_session in db.query(StudentSession).filter_by(session_id=session_id).all():
        student = db.get(Student, student_session.student_id)
        live_state = manager.get_state(student_session.student_id, session_id)
        if live_state:
            results.append(
                StudentStatusOut(
                    student_id=student_session.student_id,
                    display_name=student.display_name if student else student_session.student_id,
                    session_id=session_id,
                    status=live_state.status,
                    current_state=live_state.current_state,
                    overall_severity=live_state.overall_severity,
                    alert_count=live_state.alert_count,
                    last_event_type=live_state.last_event_type,
                    last_event_at=live_state.last_event_at,
                    last_heartbeat=live_state.last_heartbeat,
                )
            )
        else:
            results.append(
                StudentStatusOut(
                    student_id=student_session.student_id,
                    display_name=student.display_name if student else student_session.student_id,
                    session_id=session_id,
                    status=student_session.status,
                    current_state="UNKNOWN",
                    overall_severity="green",
                    alert_count=0,
                    last_heartbeat=student_session.last_heartbeat,
                )
            )
    return results
