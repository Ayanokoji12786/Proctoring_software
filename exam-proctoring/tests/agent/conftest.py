"""Test fixtures for the Student Agent test suite.

Puts student-agent/ on sys.path, matching its flat (non-packaged) module
layout, and isolates each test's local event queue file in a tmp directory.
"""
from __future__ import annotations

import os
import sys

AGENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "student-agent"))
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

import pytest  # noqa: E402


@pytest.fixture()
def agent_config(tmp_path):
    from config import AgentConfig

    config = AgentConfig()
    config.session_id = "exam_test"
    config.student_id = "student_test"
    config.token = "test-token"
    config.local_queue_path = str(tmp_path / "queue.jsonl")
    config.default_cooldown_seconds = 0.05
    config.cooldown_seconds = {k: 0.05 for k in config.cooldown_seconds}
    return config
