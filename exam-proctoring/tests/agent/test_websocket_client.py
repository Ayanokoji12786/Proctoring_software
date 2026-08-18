"""WebSocketClient: durable local queue, and reconnection delivering queued events
without ever losing one (a real local WebSocket server is used for the
reconnect test — no external network access required).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
import websockets


def _sample_event(agent_config, event_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"):
    from events.schemas import EventPayload, EventType, Severity

    return EventPayload(
        event_id=event_id,
        student_id=agent_config.student_id,
        session_id=agent_config.session_id,
        timestamp=dt.datetime.now(dt.timezone.utc),
        event_type=EventType.WINDOW_FOCUS_CHANGED,
        severity=Severity.YELLOW,
        metadata={},
    )


def test_enqueue_persists_event_to_disk(agent_config):
    from networking.websocket_client import WebSocketClient

    client = WebSocketClient(agent_config)
    event = _sample_event(agent_config)
    client.enqueue_event(event)

    assert client.queued_count == 1
    contents = open(agent_config.local_queue_path).read()
    saved = json.loads(contents.strip())
    assert saved["event_id"] == event.event_id


def test_queue_reloads_from_disk_after_restart(agent_config):
    from networking.websocket_client import WebSocketClient

    first_client = WebSocketClient(agent_config)
    event = _sample_event(agent_config)
    first_client.enqueue_event(event)

    # simulate the agent process restarting: a brand-new client reads the same queue file
    second_client = WebSocketClient(agent_config)
    assert second_client.queued_count == 1


@pytest.mark.asyncio
async def test_event_is_resent_after_reconnect_and_eventually_acked(agent_config):
    from events.schemas import EventPayload
    from networking.websocket_client import WebSocketClient

    received_event_ids: list[str] = []
    connection_count = 0

    async def handler(ws):
        nonlocal connection_count
        connection_count += 1
        this_connection = connection_count
        async for raw in ws:
            data = json.loads(raw)
            if data["type"] == "AUTH":
                await ws.send(json.dumps({"type": "AUTH_OK", "session_id": data["session_id"], "student_id": data["student_id"]}))
            elif data["type"] == "HEARTBEAT":
                await ws.send(json.dumps({"type": "HEARTBEAT_ACK", "timestamp": "now"}))
            elif data["type"] == "EVENT":
                received_event_ids.append(data["payload"]["event_id"])
                if this_connection == 1:
                    # Simulate the connection dying right after the event arrives, before an
                    # ACK is sent, forcing the client to resend once it reconnects.
                    await ws.close()
                    return
                await ws.send(json.dumps({"type": "EVENT_ACK", "event_id": data["payload"]["event_id"], "duplicate": False}))

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    agent_config.server_ws_url = f"ws://127.0.0.1:{port}"
    agent_config.reconnect_initial_backoff_seconds = 0.05
    agent_config.reconnect_max_backoff_seconds = 0.1
    agent_config.heartbeat_interval_seconds = 30
    agent_config.connection_timeout_seconds = 3

    client = WebSocketClient(agent_config)
    run_task = asyncio.ensure_future(client.run())

    try:
        await asyncio.sleep(0.2)  # let the first AUTH complete
        event = _sample_event(agent_config)
        client.enqueue_event(event)

        for _ in range(50):  # poll up to ~2.5s for the resend+ack round trip
            if client.queued_count == 0:
                break
            await asyncio.sleep(0.05)

        assert client.queued_count == 0, "event should have been acknowledged after reconnecting"
        assert received_event_ids.count(event.event_id) >= 2, "server should have seen the event both before and after the reconnect"
        assert connection_count >= 2, "client should have reconnected at least once"
    finally:
        await client.stop(graceful=False)
        run_task.cancel()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_auth_failure_does_not_retry_forever(agent_config):
    from networking.websocket_client import ConnectionStatus, WebSocketClient

    async def handler(ws):
        async for raw in ws:
            data = json.loads(raw)
            if data["type"] == "AUTH":
                await ws.send(json.dumps({"type": "AUTH_FAILED", "reason": "bad token"}))
                await ws.close()
                return

    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    agent_config.server_ws_url = f"ws://127.0.0.1:{port}"

    client = WebSocketClient(agent_config)
    try:
        await asyncio.wait_for(client.run(), timeout=3)  # run() returns promptly on auth failure, not retrying
    finally:
        server.close()
        await server.wait_closed()

    assert client.status == ConnectionStatus.AUTH_FAILED
