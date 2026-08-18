"""Evidence snapshot upload, listing, and retention purge."""
from __future__ import annotations

import datetime as dt
import io


def _enroll(client, admin_headers, session_id, student_id):
    client.post("/api/sessions", json={"name": session_id, "session_id": session_id}, headers=admin_headers)
    resp = client.post(
        f"/api/sessions/{session_id}/students",
        json={"session_id": session_id, "student_id": student_id, "display_name": "Evidence Student"},
        headers=admin_headers,
    )
    return resp.json()["token"]


_TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300030202020202"
    "03020202030303030406040404040408060605060909080a0a090809090a0c"
    "0f0c0a0b0e0b09090d110d0e0f101011100a0c12131210130f101010ffc900"
)


def test_evidence_upload_requires_valid_token(client, admin_headers):
    token = _enroll(client, admin_headers, "exam_evi_1", "e1")
    files = {"file": ("snap.jpg", io.BytesIO(_TINY_JPEG), "image/jpeg")}

    bad = client.post(
        "/api/evidence",
        data={"session_id": "exam_evi_1", "student_id": "e1", "token": "wrong-token"},
        files=files,
    )
    assert bad.status_code == 401


def test_evidence_upload_and_list(client, admin_headers):
    token = _enroll(client, admin_headers, "exam_evi_2", "e2")
    files = {"file": ("snap.jpg", io.BytesIO(_TINY_JPEG), "image/jpeg")}

    resp = client.post(
        "/api/evidence",
        data={"session_id": "exam_evi_2", "student_id": "e2", "token": token, "event_id": "evt-1"},
        files=files,
    )
    assert resp.status_code == 200
    evidence_id = resp.json()["evidence_id"]

    listing = client.get("/api/evidence", params={"session_id": "exam_evi_2", "student_id": "e2"}, headers=admin_headers)
    assert listing.status_code == 200
    assert any(e["evidence_id"] == evidence_id for e in listing.json())

    fetched = client.get(f"/api/evidence/{evidence_id}", headers=admin_headers)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/jpeg"


def test_evidence_rejects_oversized_file(client, admin_headers):
    from config import settings

    token = _enroll(client, admin_headers, "exam_evi_3", "e3")
    oversized = b"\xff" * (settings.evidence_max_bytes + 1024)
    files = {"file": ("snap.jpg", io.BytesIO(oversized), "image/jpeg")}

    resp = client.post(
        "/api/evidence",
        data={"session_id": "exam_evi_3", "student_id": "e3", "token": token},
        files=files,
    )
    assert resp.status_code == 413


def test_evidence_rejects_unsupported_content_type(client, admin_headers):
    token = _enroll(client, admin_headers, "exam_evi_4", "e4")
    files = {"file": ("snap.gif", io.BytesIO(b"GIF89a"), "image/gif")}

    resp = client.post(
        "/api/evidence",
        data={"session_id": "exam_evi_4", "student_id": "e4", "token": token},
        files=files,
    )
    assert resp.status_code == 415


def test_purge_expired_evidence_removes_only_expired_rows(client, admin_headers, tmp_path):
    from database.db import session_scope
    from models.models import Evidence
    from api.evidence import purge_expired_evidence

    now = dt.datetime.now(dt.timezone.utc)
    expired_path = tmp_path / "expired.jpg"
    expired_path.write_bytes(_TINY_JPEG)
    fresh_path = tmp_path / "fresh.jpg"
    fresh_path.write_bytes(_TINY_JPEG)

    with session_scope() as db:
        db.add(
            Evidence(
                id="expired-evidence-1",
                student_id="e5",
                session_id="exam_evi_5",
                file_path=str(expired_path),
                content_type="image/jpeg",
                captured_at=now - dt.timedelta(hours=100),
                expires_at=now - dt.timedelta(hours=1),
            )
        )
        db.add(
            Evidence(
                id="fresh-evidence-1",
                student_id="e5",
                session_id="exam_evi_5",
                file_path=str(fresh_path),
                content_type="image/jpeg",
                captured_at=now,
                expires_at=now + dt.timedelta(hours=1),
            )
        )

    purged_count = purge_expired_evidence()
    assert purged_count >= 1
    assert not expired_path.exists()
    assert fresh_path.exists()

    with session_scope() as db:
        expired_row = db.get(Evidence, "expired-evidence-1")
        fresh_row = db.get(Evidence, "fresh-evidence-1")
        assert expired_row.deleted is True
        assert fresh_row.deleted is False
