"""Small helper CLI used by a proctor (or this README's setup steps) to enroll
a student into an exam session and obtain the short-lived token the Student
Agent needs to connect. This is *not* run by the student — it calls the
server's admin-authenticated REST API.

Usage:
    python register_student.py --server http://localhost:8000 --admin-key CHANGE_ME_ADMIN_KEY \
        --session-id exam_101 --student-id student_1 --student-name "Alex Kim"
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Enroll a student into an exam session")
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--admin-key", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--student-id", required=True)
    parser.add_argument("--student-name", required=True)
    args = parser.parse_args()

    body = json.dumps(
        {"session_id": args.session_id, "student_id": args.student_id, "display_name": args.student_name}
    ).encode()
    url = f"{args.server.rstrip('/')}/api/sessions/{args.session_id}/students"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"X-Admin-Api-Key": args.admin_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(f"Enrollment failed: HTTP {exc.code} {exc.read().decode()}", file=sys.stderr)
        sys.exit(1)

    print("Enrollment successful. Launch the Student Agent with:\n")
    print(
        f"  python main.py --server ws://{urllib.parse.urlsplit(args.server).netloc} "
        f"--http-server {args.server} --session-id {args.session_id} "
        f"--student-id {args.student_id} --token {payload['token']}"
    )
    print(f"\nToken expires at: {payload['expires_at']}")


if __name__ == "__main__":
    import urllib.parse

    main()
