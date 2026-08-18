from __future__ import annotations

from fastapi import APIRouter

from websocket.manager import manager

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "connected_students": len(manager.student_sockets),
        "connected_admins": len(manager.admin_sockets),
    }
