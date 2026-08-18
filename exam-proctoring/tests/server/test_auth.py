"""Auth: token creation/verification and admin API key gating."""
from __future__ import annotations

import datetime as dt

import jwt
import pytest


def test_token_roundtrip_succeeds():
    from auth.tokens import create_student_token, verify_student_token

    token, expires_at, jti = create_student_token("exam_1", "student_1")
    claims = verify_student_token(token, "exam_1", "student_1")

    assert claims["sub"] == "student_1"
    assert claims["session_id"] == "exam_1"
    assert claims["jti"] == jti
    assert expires_at > dt.datetime.now(dt.timezone.utc)


def test_token_rejects_wrong_student_id():
    from auth.tokens import TokenError, create_student_token, verify_student_token

    token, _, _ = create_student_token("exam_1", "student_1")
    with pytest.raises(TokenError):
        verify_student_token(token, "exam_1", "student_2")


def test_token_rejects_wrong_session_id():
    from auth.tokens import TokenError, create_student_token, verify_student_token

    token, _, _ = create_student_token("exam_1", "student_1")
    with pytest.raises(TokenError):
        verify_student_token(token, "exam_2", "student_1")


def test_token_rejects_expired_token():
    from auth.tokens import TokenError, verify_student_token
    from config import settings

    now = dt.datetime.now(dt.timezone.utc)
    expired_claims = {
        "sub": "student_1",
        "session_id": "exam_1",
        "jti": "x",
        "iat": now - dt.timedelta(hours=2),
        "exp": now - dt.timedelta(hours=1),
        "scope": "student",
    }
    expired_token = jwt.encode(expired_claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(TokenError):
        verify_student_token(expired_token, "exam_1", "student_1")


def test_token_rejects_tampered_signature():
    from auth.tokens import TokenError, create_student_token, verify_student_token

    token, _, _ = create_student_token("exam_1", "student_1")
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    with pytest.raises(TokenError):
        verify_student_token(tampered, "exam_1", "student_1")


def test_admin_rest_endpoint_requires_api_key(client):
    resp = client.post("/api/sessions", json={"name": "No Auth"})
    assert resp.status_code == 401


def test_admin_rest_endpoint_accepts_valid_key(client, admin_headers):
    resp = client.post("/api/sessions", json={"name": "Has Auth", "session_id": "exam_auth_test"}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "exam_auth_test"


@pytest.mark.parametrize(
    "bad_id",
    ["../../../etc/passwd", "a/b", "a\\b", "..", "with space", "", "x" * 65],
)
def test_ids_that_could_escape_the_storage_path_are_rejected(client, admin_headers, bad_id):
    """session_id/student_id become directory names under the evidence store."""
    client.post("/api/sessions", json={"name": "Safe", "session_id": "exam_safe"}, headers=admin_headers)
    resp = client.post(
        "/api/sessions/exam_safe/students",
        json={"session_id": "exam_safe", "student_id": bad_id, "display_name": "X"},
        headers=admin_headers,
    )
    assert resp.status_code == 422, f"expected rejection for student_id={bad_id!r}"


def test_ordinary_ids_are_still_accepted(client, admin_headers):
    client.post("/api/sessions", json={"name": "Safe2", "session_id": "exam_safe2"}, headers=admin_headers)
    resp = client.post(
        "/api/sessions/exam_safe2/students",
        json={"session_id": "exam_safe2", "student_id": "student_1.a-b", "display_name": "Alex Kim"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
