"""WebSocket client: AUTH handshake, event delivery with a durable local queue,
heartbeats, and reconnect with exponential backoff.

Reliability contract (spec sections 10 & 16): events are appended to an
in-memory + on-disk queue as soon as they're created. They are only removed
from the queue once the server ACKs them by event_id. If the connection
drops, on reconnect every still-queued event is resent; the server
deduplicates by event_id so no duplicate events are recorded downstream.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from config import AgentConfig
from events.schemas import EventPayload

logger = logging.getLogger("agent.websocket_client")


class ConnectionStatus:
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTH_FAILED = "auth_failed"
    STOPPED = "stopped"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class WebSocketClient:
    def __init__(
        self,
        config: AgentConfig,
        on_status_change: Optional[Callable[[str], None]] = None,
        on_event_acked: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config = config
        self._ws = None
        self._status = ConnectionStatus.DISCONNECTED
        self._queue_path = Path(config.local_queue_path)
        self._pending: dict[str, EventPayload] = {}
        self._sent_this_connection: set[str] = set()
        self._send_signal = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._on_status_change = on_status_change or (lambda s: None)
        self._on_event_acked = on_event_acked or (lambda eid: None)
        self._load_persisted_queue()

    @property
    def status(self) -> str:
        return self._status

    @property
    def queued_count(self) -> int:
        return len(self._pending)

    # ---------------- durable local queue ----------------

    def _load_persisted_queue(self) -> None:
        if not self._queue_path.exists():
            return
        loaded = 0
        for line in self._queue_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = EventPayload.model_validate(json.loads(line))
                self._pending[payload.event_id] = payload
                loaded += 1
            except Exception:
                logger.warning("dropping corrupt queued event line on load")
        if loaded:
            logger.info("resumed %d unsent event(s) from local queue", loaded)

    def _persist_queue(self) -> None:
        try:
            self._queue_path.write_text("".join(p.model_dump_json() + "\n" for p in self._pending.values()))
        except OSError:
            logger.exception("failed to persist local event queue to disk")

    def enqueue_event(self, event: EventPayload) -> None:
        self._pending[event.event_id] = event
        self._persist_queue()
        self._send_signal.set()

    # ---------------- lifecycle ----------------

    def _set_status(self, status: str) -> None:
        if status != self._status:
            self._status = status
            self._on_status_change(status)

    async def stop(self, graceful: bool = True) -> None:
        self._stop_event.set()
        if self._ws is not None and graceful:
            try:
                await self._ws.send(json.dumps({"type": "END_EXAM"}))
            except Exception:
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._set_status(ConnectionStatus.STOPPED)

    async def run(self) -> None:
        """Main connection loop: connect, auth, run send/heartbeat/receive tasks, reconnect on drop."""
        backoff = self.config.reconnect_initial_backoff_seconds
        ws_url = f"{self.config.server_ws_url.rstrip('/')}/ws/student"

        while not self._stop_event.is_set():
            self._set_status(ConnectionStatus.CONNECTING)
            try:
                async with websockets.connect(ws_url, open_timeout=self.config.connection_timeout_seconds) as ws:
                    self._ws = ws
                    authenticated = await self._authenticate(ws)
                    if not authenticated:
                        self._set_status(ConnectionStatus.AUTH_FAILED)
                        return  # bad credentials: do not keep retrying

                    self._set_status(ConnectionStatus.CONNECTED)
                    backoff = self.config.reconnect_initial_backoff_seconds
                    self._sent_this_connection.clear()
                    self._send_signal.set()  # trigger an immediate flush of anything queued

                    # Run all three loops concurrently, but treat the FIRST one to finish
                    # (raise OR return - e.g. a clean server-side close ends the receiver
                    # loop without raising) as a signal the connection is gone, and tear
                    # down the rest immediately. Using gather() here would be a bug: it
                    # waits for every task to finish, so the sender/heartbeat loops (which
                    # run forever) would hang indefinitely after a clean disconnect and the
                    # client would never notice it needed to reconnect.
                    #
                    # The cleanup lives in `finally`, not just after `asyncio.wait`: if this
                    # whole run() coroutine is itself cancelled from outside (e.g. stop())
                    # while suspended at that await, the CancelledError propagates through
                    # immediately and skips any cleanup code that isn't in a finally block -
                    # leaving the sender/heartbeat sub-tasks orphaned (pending forever,
                    # logged by asyncio as "Task was destroyed but it is pending!").
                    tasks = [
                        asyncio.ensure_future(self._sender_loop(ws)),
                        asyncio.ensure_future(self._heartbeat_loop(ws)),
                        asyncio.ensure_future(self._receiver_loop(ws)),
                    ]
                    try:
                        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            exc = task.exception()
                            if exc is not None:
                                raise exc
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            except (ConnectionClosed, InvalidStatus, OSError, asyncio.TimeoutError) as exc:
                logger.warning("websocket connection lost (%s); reconnecting in %.1fs", exc, backoff)
            except Exception:
                logger.exception("unexpected error in websocket client loop")
            finally:
                self._ws = None
                if self._status != ConnectionStatus.AUTH_FAILED:
                    self._set_status(ConnectionStatus.DISCONNECTED)

            if self._stop_event.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.config.reconnect_max_backoff_seconds)

        self._set_status(ConnectionStatus.STOPPED)

    async def _authenticate(self, ws) -> bool:
        await ws.send(
            json.dumps(
                {
                    "type": "AUTH",
                    "session_id": self.config.session_id,
                    "student_id": self.config.student_id,
                    "token": self.config.token,
                }
            )
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=self.config.connection_timeout_seconds)
        data = json.loads(raw)
        if data.get("type") == "AUTH_OK":
            logger.info("authenticated with server session_id=%s", self.config.session_id)
            return True
        logger.error("authentication failed: %s", data.get("reason", "unknown reason"))
        return False

    async def _sender_loop(self, ws) -> None:
        while True:
            unsent = [e for eid, e in self._pending.items() if eid not in self._sent_this_connection]
            for event in unsent:
                await ws.send(json.dumps({"type": "EVENT", "payload": json.loads(event.model_dump_json())}))
                self._sent_this_connection.add(event.event_id)
                logger.debug("sent event %s (%s)", event.event_id, event.event_type.value)
            self._send_signal.clear()
            await self._send_signal.wait()

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await ws.send(json.dumps({"type": "HEARTBEAT", "student_id": self.config.student_id, "timestamp": _now_iso()}))
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    async def _receiver_loop(self, ws) -> None:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")
            if msg_type == "EVENT_ACK":
                event_id = data.get("event_id")
                if event_id and event_id in self._pending:
                    del self._pending[event_id]
                    self._sent_this_connection.discard(event_id)
                    self._persist_queue()
                    self._on_event_acked(event_id)
            elif msg_type == "HEARTBEAT_ACK":
                pass
            elif msg_type == "ERROR":
                logger.warning("server reported error: %s", data.get("reason"))
