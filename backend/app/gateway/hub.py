"""Per-session event channels with sequence numbers and bounded replay.

One WebSocket carries many sessions. Each session is a *channel* with its own
monotonically increasing `seq`; the hub keeps the last N frames per channel so
a client that reconnects with `last_seq` gets exactly the frames it missed.
Agent runs publish here regardless of whether anyone is connected, which is
what lets a run survive a dropped connection.

Frames are serialized once and the same string is queued to every subscriber.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import OrderedDict, deque
from typing import Any

MAX_IDLE_CHANNELS = 2000


class Connection:
    """Outbound side of one WebSocket: a bounded queue drained by a single writer task."""

    def __init__(self, max_queue: int) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)
        self.overflowed = asyncio.Event()
        self.channels: set[str] = set()

    def enqueue(self, frame: str) -> None:
        if self.overflowed.is_set():
            return
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            # A consumer this far behind is cut loose instead of growing memory
            # without bound; it reconnects and catches up from the replay buffer.
            self.overflowed.set()


class Channel:
    def __init__(self, session_id: str, buffer: int) -> None:
        self.session_id = session_id
        self.seq = 0
        self.frames: deque[tuple[int, str]] = deque(maxlen=buffer)
        self.subscribers: set[Connection] = set()

    def publish(self, type_: str, data: dict[str, Any], ts: float | None = None) -> int:
        self.seq += 1
        frame = json.dumps(
            {"ch": self.session_id, "seq": self.seq, "ts": ts or time.time() * 1000, "type": type_, "data": data},
            separators=(",", ":"),
        )
        self.frames.append((self.seq, frame))
        for conn in self.subscribers:
            conn.enqueue(frame)
        return self.seq

    def replay_after(self, last_seq: int) -> list[str] | None:
        """Frames with seq > last_seq, or None if the buffer no longer reaches back that far."""
        if last_seq > self.seq:
            return None
        if last_seq == self.seq:
            return []
        oldest = self.frames[0][0] if self.frames else self.seq + 1
        if last_seq + 1 < oldest:
            return None
        return [frame for seq, frame in self.frames if seq > last_seq]


class Hub:
    def __init__(self, buffer: int = 4096) -> None:
        # Changes on every process start; tells clients their seq numbers are stale.
        self.epoch = uuid.uuid4().hex[:12]
        self._buffer = buffer
        self._channels: OrderedDict[str, Channel] = OrderedDict()

    def channel(self, session_id: str) -> Channel:
        ch = self._channels.get(session_id)
        if ch is None:
            ch = self._channels[session_id] = Channel(session_id, self._buffer)
            self._evict()
        else:
            self._channels.move_to_end(session_id)
        return ch

    def _evict(self) -> None:
        if len(self._channels) <= MAX_IDLE_CHANNELS:
            return
        for sid in [s for s, c in self._channels.items() if not c.subscribers][: len(self._channels) - MAX_IDLE_CHANNELS]:
            del self._channels[sid]

    def publish(self, session_id: str, type_: str, data: dict[str, Any], ts: float | None = None) -> int:
        return self.channel(session_id).publish(type_, data, ts)

    def subscribe(self, conn: Connection, session_id: str) -> Channel:
        ch = self.channel(session_id)
        ch.subscribers.add(conn)
        conn.channels.add(session_id)
        return ch

    def unsubscribe(self, conn: Connection, session_id: str) -> None:
        conn.channels.discard(session_id)
        if ch := self._channels.get(session_id):
            ch.subscribers.discard(conn)

    def drop(self, conn: Connection) -> None:
        for sid in list(conn.channels):
            self.unsubscribe(conn, sid)
