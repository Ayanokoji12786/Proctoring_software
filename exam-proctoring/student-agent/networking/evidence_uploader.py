"""Uploads a single evidence snapshot image over HTTPS (multipart) when the
local event policy calls for it. Never streams continuous video; at most one
compressed still image per qualifying event.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import AgentConfig

logger = logging.getLogger("agent.networking.evidence")


async def upload_snapshot(config: AgentConfig, event_id: str, jpeg_bytes: bytes) -> Optional[str]:
    url = f"{config.server_http_url.rstrip('/')}/api/evidence"
    files = {"file": (f"{event_id}.jpg", jpeg_bytes, "image/jpeg")}
    data = {
        "session_id": config.session_id,
        "student_id": config.student_id,
        "token": config.token,
        "event_id": event_id,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, data=data, files=files)
            response.raise_for_status()
            payload = response.json()
            logger.info("evidence uploaded event_id=%s evidence_id=%s", event_id, payload.get("evidence_id"))
            return payload.get("evidence_id")
    except httpx.HTTPError as exc:
        logger.warning("evidence upload failed for event_id=%s: %s", event_id, exc)
        return None
