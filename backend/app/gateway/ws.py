"""Multiplexed WebSocket endpoint.

Wire protocol (JSON text frames)

client -> server
    {"op":"auth","token":"..."}                       must be the first frame
    {"op":"subscribe","ch":SID,"last_seq":N,"epoch":E} attach to a session, replaying frames after N
    {"op":"unsubscribe","ch":SID}
    {"op":"send","ch":SID,"id":OP,"text":"..."}        start a run
    {"op":"approve","ch":SID,"id":OP,"approved":bool}  answer a destructive-tool approval
    {"op":"resume","ch":SID,"id":OP}                   continue an interrupted run from its checkpoint
    {"op":"cancel","ch":SID,"id":OP}
    {"op":"ping","t":123}

    The four commands carry a client-chosen `id`. The server answers each with
    an `ack` (or an `error`) echoing it, and remembers handled ids, so a client
    that lost its connection before seeing the ack can simply resend: the
    command takes effect at most once.

server -> client
    {"op":"ready","user":..,"org":..,"epoch":E}
    {"op":"subscribed","ch":SID,"epoch":E,"seq":N,"head":H,"mode":"replay"|"snapshot","snapshot":{...}?}
        frames N+1..H follow immediately (catch-up), then live frames
    {"op":"ack","ch":SID,"id":OP}
    {"op":"error","ch":SID?,"id":OP?,"code":..,"message":..}
    {"op":"pong","t":123}
    {"ch":SID,"seq":N,"ts":MS,"type":T,"data":{...}}   session events, gap-free per channel
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..agent.runtime import AgentRuntime, RunConflict
from ..agent.sessions import SessionStore
from ..auth import AuthError, Principal, verify_session_token
from .hub import Connection, Hub

log = logging.getLogger("gateway.ws")
router = APIRouter()

AUTH_TIMEOUT_SECONDS = 5
MAX_TEXT_CHARS = 8000
CLOSE_POLICY_VIOLATION = 1008
CLOSE_TRY_AGAIN_LATER = 1013
COMMANDS = ("send", "approve", "resume", "cancel")


def _ctl(op: str, **fields: Any) -> str:
    return json.dumps({"op": op, **fields}, separators=(",", ":"))


async def _writer(ws: WebSocket, conn: Connection) -> None:
    while True:
        await ws.send_text(await conn.queue.get())


class _Handler:
    def __init__(self, ws: WebSocket, conn: Connection, principal: Principal) -> None:
        self.ws, self.conn, self.principal = ws, conn, principal
        self.hub: Hub = ws.app.state.hub
        self.runtime: AgentRuntime = ws.app.state.runtime
        self.sessions: SessionStore = ws.app.state.sessions

    async def _owned(self, sid: Any) -> dict[str, Any]:
        """Load a session, treating someone else's exactly like one that does not exist."""
        session = await self.sessions.get(sid) if isinstance(sid, str) else None
        if session is None or session["user_id"] != self.principal.user_id:
            raise LookupError("session not found")
        return session

    async def handle(self, msg: dict[str, Any]) -> None:
        op, sid, op_id = msg.get("op"), msg.get("ch"), msg.get("id")
        if op == "ping":
            self.conn.enqueue(_ctl("pong", t=msg.get("t")))
            return
        try:
            session = await self._owned(sid)
            if op == "subscribe":
                await self._subscribe(session, msg)
            elif op == "unsubscribe":
                self.hub.unsubscribe(self.conn, sid)
            elif op in COMMANDS:
                if not self.runtime.first_time(sid, op_id):
                    self.conn.enqueue(_ctl("ack", ch=sid, id=op_id, duplicate=True))
                    return
                await self._command(op, session, msg)
                self.conn.enqueue(_ctl("ack", ch=sid, id=op_id))
            else:
                raise ValueError(f"unknown op {op!r}")
        except LookupError as exc:
            self.conn.enqueue(_ctl("error", ch=sid, id=op_id, code="not_found", message=str(exc)))
        except RunConflict as exc:
            self.conn.enqueue(_ctl("error", ch=sid, id=op_id, code="conflict", message=str(exc)))
        except ValueError as exc:
            self.conn.enqueue(_ctl("error", ch=sid, id=op_id, code="bad_request", message=str(exc)))

    async def _command(self, op: str, session: dict[str, Any], msg: dict[str, Any]) -> None:
        if op == "send":
            text = msg.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                raise ValueError(f"text must be 1-{MAX_TEXT_CHARS} characters")
            await self.runtime.send(session, text.strip())
        elif op == "approve":
            await self.runtime.approve(session, bool(msg.get("approved")))
        elif op == "resume":
            await self.runtime.resume(session)
        else:
            await self.runtime.cancel(session["id"])

    async def _subscribe(self, session: dict[str, Any], msg: dict[str, Any]) -> None:
        sid = session["id"]
        channel = self.hub.channel(sid)
        last_seq = msg.get("last_seq")
        frames = None
        if isinstance(last_seq, int) and msg.get("epoch") == self.hub.epoch:
            frames = channel.replay_after(last_seq)
        if frames is not None:
            # No await between reading the buffer and joining the channel, so no frame can slip through.
            self.conn.enqueue(_ctl("subscribed", ch=sid, epoch=self.hub.epoch, seq=last_seq, head=channel.seq, mode="replay"))
            for frame in frames:
                self.conn.enqueue(frame)
            self.hub.subscribe(self.conn, sid)
            return
        # Client is new, too far behind, or from before a restart: send full state.
        # Frames published while the snapshot is being built are replayed after it;
        # client reducers are idempotent by message/tool-call id, so overlap is harmless.
        seq_before = channel.seq
        snapshot = await self.runtime.snapshot(session)
        self.conn.enqueue(
            _ctl("subscribed", ch=sid, epoch=self.hub.epoch, seq=seq_before, head=channel.seq, mode="snapshot", snapshot=snapshot)
        )
        for frame in channel.replay_after(seq_before) or []:
            self.conn.enqueue(frame)
        self.hub.subscribe(self.conn, sid)


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    settings = ws.app.state.settings
    hub: Hub = ws.app.state.hub
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_SECONDS))
        if first.get("op") != "auth":
            raise AuthError("first frame must be auth")
        principal = verify_session_token(settings.secret, str(first.get("token", "")))
    except (AuthError, asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect, AttributeError):
        with contextlib.suppress(Exception):
            await ws.close(CLOSE_POLICY_VIOLATION, "authentication required")
        return

    conn = Connection(settings.send_queue)
    handler = _Handler(ws, conn, principal)
    conn.enqueue(_ctl("ready", user=principal.user_id, org=principal.org_id, epoch=hub.epoch))
    writer = asyncio.create_task(_writer(ws, conn))
    overflow = asyncio.create_task(conn.overflowed.wait())

    async def reader() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    raise ValueError
            except ValueError:
                conn.enqueue(_ctl("error", code="bad_request", message="frames must be JSON objects"))
                continue
            await handler.handle(msg)

    read = asyncio.create_task(reader())
    try:
        done, _ = await asyncio.wait({read, writer, overflow}, return_when=asyncio.FIRST_COMPLETED)
        if overflow in done:
            with contextlib.suppress(Exception):
                await ws.close(CLOSE_TRY_AGAIN_LATER, "client too slow; reconnect to catch up")
        for task in done:
            if task is not overflow and (exc := task.exception()) and not isinstance(exc, (WebSocketDisconnect, RuntimeError)):
                log.warning("websocket task failed: %r", exc)
    finally:
        # Runs keep going server-side; only this connection's subscriptions end.
        hub.drop(conn)
        for task in (read, writer, overflow):
            task.cancel()
        await asyncio.gather(read, writer, overflow, return_exceptions=True)
