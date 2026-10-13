"""A small protocol-complete client, shared by the test suite and the load test.

It does what the browser client does: one socket, many sessions, per-channel
sequence tracking, duplicate suppression, and resubscribe-with-last_seq on
reconnect. It also records integrity counters and token delivery latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import httpx
import websockets


class PlatformClient:
    def __init__(self, base_url: str, user: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.token = ""
        self.epoch: str | None = None
        self.ws: Any = None
        self.events: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.modes: dict[str, list[str]] = defaultdict(list)  # how each subscribe was answered
        self.arrivals: list[str] = []  # session id of every event, in arrival order
        self.errors: list[dict[str, Any]] = []
        self.last_seq: dict[str, int] = {}
        self.subscribed: set[str] = set()
        self.duplicates = 0
        self.gaps = 0
        self.token_latency_ms: list[float] = []  # live delivery only
        self.replayed_tokens = 0  # tokens received as catch-up after a reconnect
        self.reconnects = 0
        self._head: dict[str, int] = {}
        self._unacked: dict[str, str] = {}  # command id -> frame, resent after a reconnect
        self._reader: asyncio.Task[None] | None = None
        self._changed = asyncio.Condition()
        self._acks: dict[str, asyncio.Event] = defaultdict(asyncio.Event)

    # ------------------------------------------------------------------ REST
    async def login(self) -> None:
        async with httpx.AsyncClient() as http:
            r = await http.post(f"{self.base_url}/api/login", json={"user": self.user})
            r.raise_for_status()
            self.token = r.json()["token"]

    async def create_session(self) -> str:
        async with httpx.AsyncClient() as http:
            r = await http.post(f"{self.base_url}/api/sessions", headers={"Authorization": f"Bearer {self.token}"})
            r.raise_for_status()
            return r.json()["id"]

    # ------------------------------------------------------------- transport
    async def connect(self) -> None:
        if not self.token:
            await self.login()
        url = self.base_url.replace("http", "ws", 1) + "/ws"
        self.ws = await websockets.connect(url, max_size=None, max_queue=None)
        await self.ws.send(json.dumps({"op": "auth", "token": self.token}))
        ready = json.loads(await self.ws.recv())
        if ready.get("op") != "ready":
            raise RuntimeError(f"handshake failed: {ready}")
        self._server_epoch = ready["epoch"]
        self._reader = asyncio.create_task(self._read())

    async def drop(self) -> None:
        """Kill the socket abruptly, as a network failure would."""
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self.ws:
            self.ws.transport.abort()
            self.ws = None

    async def reconnect(self) -> None:
        self.reconnects += 1
        await self.connect()
        for sid in list(self.subscribed):
            await self.subscribe(sid)
        for frame in list(self._unacked.values()):  # commands whose fate we never learned
            await self.ws.send(frame)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self.ws:
            await self.ws.close()

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                now = time.time() * 1000
                msg = json.loads(raw)
                if "op" in msg:
                    self._control(msg)
                else:
                    self._event(msg, now)
                async with self._changed:
                    self._changed.notify_all()
        except websockets.ConnectionClosed:
            pass

    def _control(self, msg: dict[str, Any]) -> None:
        if msg["op"] == "subscribed":
            sid = msg["ch"]
            self.epoch = msg["epoch"]
            self.modes[sid].append(msg["mode"])
            self._head[sid] = msg.get("head", 0)
            if msg["mode"] == "snapshot":
                self.snapshots[sid] = msg["snapshot"]
                self.last_seq[sid] = msg["seq"]
            self._acks[sid].set()
        elif msg["op"] == "ack":
            self._unacked.pop(msg.get("id"), None)
        elif msg["op"] == "error":
            self._unacked.pop(msg.get("id"), None)
            self.errors.append(msg)

    def _event(self, msg: dict[str, Any], now: float) -> None:
        sid, seq = msg["ch"], msg["seq"]
        last = self.last_seq.get(sid, 0)
        if seq <= last:
            self.duplicates += 1
            return
        if seq != last + 1:
            self.gaps += 1
        self.last_seq[sid] = seq
        if msg["type"] == "token":
            if seq <= self._head.get(sid, 0):
                self.replayed_tokens += 1  # was produced while we were disconnected; not a delivery-latency sample
            else:
                self.token_latency_ms.append(now - msg["ts"])
        self.events[sid].append(msg)
        self.arrivals.append(sid)

    # ------------------------------------------------------------------- ops
    async def _op(self, op: str, **fields: Any) -> None:
        await self.ws.send(json.dumps({"op": op, **fields}))

    async def subscribe(self, sid: str) -> None:
        self.subscribed.add(sid)
        self._acks[sid].clear()
        await self._op("subscribe", ch=sid, last_seq=self.last_seq.get(sid), epoch=self.epoch)
        await asyncio.wait_for(self._acks[sid].wait(), 10)

    async def _command(self, op: str, **fields: Any) -> None:
        """Send a command that must take effect exactly once, even across a dropped connection."""
        op_id = uuid.uuid4().hex
        frame = json.dumps({"op": op, "id": op_id, **fields})
        self._unacked[op_id] = frame
        with contextlib.suppress(Exception):  # if the socket is down, reconnect() delivers it
            await self.ws.send(frame)

    async def send(self, sid: str, text: str) -> None:
        await self._command("send", ch=sid, text=text)

    async def approve(self, sid: str, approved: bool) -> None:
        await self._command("approve", ch=sid, approved=approved)

    async def resume(self, sid: str) -> None:
        await self._command("resume", ch=sid)

    async def cancel(self, sid: str) -> None:
        await self._command("cancel", ch=sid)

    # --------------------------------------------------------------- waiting
    def of_type(self, sid: str, type_: str) -> list[dict[str, Any]]:
        return [e for e in self.events[sid] if e["type"] == type_]

    async def wait_for(self, predicate: Callable[[], Any], timeout: float = 15) -> Any:
        async def loop() -> Any:
            async with self._changed:
                while not (value := predicate()):
                    await self._changed.wait()
                return value

        return await asyncio.wait_for(loop(), timeout)

    async def wait_status(self, sid: str, status: str, timeout: float = 15, *, after: int = 0) -> dict[str, Any]:
        """Wait for a status event; `after` skips events up to that seq (to wait for the *next* idle)."""
        return await self.wait_for(
            lambda: next(
                (e for e in self.events[sid] if e["type"] == "status" and e["data"]["status"] == status and e["seq"] > after),
                None,
            ),
            timeout,
        )

    async def wait_error(self, timeout: float = 5) -> dict[str, Any]:
        return await self.wait_for(lambda: self.errors[-1] if self.errors else None, timeout)

    def text(self, sid: str) -> str:
        """All streamed assistant text for a session, in order."""
        return "".join(e["data"]["text"] for e in self.of_type(sid, "token"))
