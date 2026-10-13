"""Session registry: who owns which conversation thread, and its run status."""

from __future__ import annotations

import time
import uuid
from typing import Any

import aiosqlite

# idle               nothing running; ready for a new message
# running            a run is in flight
# awaiting_approval  paused at a human-approval interrupt (persisted in the checkpoint)
# interrupted        the run stopped early (cancel, crash, restart); resumable
# error              the run raised; resumable (retries the failed node)
RESUMABLE = ("interrupted", "error")


class SessionStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   id TEXT PRIMARY KEY, user_id TEXT NOT NULL, org_id TEXT NOT NULL, title TEXT NOT NULL,
                   status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"""
        )
        # Anything "running" when the process died was cut off mid-flight.
        await self._conn.execute("UPDATE sessions SET status='interrupted' WHERE status='running'")
        await self._conn.commit()

    async def create(self, user_id: str, org_id: str, title: str = "New session") -> dict[str, Any]:
        now = time.time()
        row = {"id": uuid.uuid4().hex, "user_id": user_id, "org_id": org_id, "title": title, "status": "idle", "created_at": now, "updated_at": now}
        await self._conn.execute(
            "INSERT INTO sessions VALUES (:id, :user_id, :org_id, :title, :status, :created_at, :updated_at)", row
        )
        await self._conn.commit()
        return row

    async def get(self, session_id: str) -> dict[str, Any] | None:
        async with self._conn.execute(
            "SELECT id, user_id, org_id, title, status, created_at, updated_at FROM sessions WHERE id=?", (session_id,)
        ) as cur:
            r = await cur.fetchone()
        return dict(zip(("id", "user_id", "org_id", "title", "status", "created_at", "updated_at"), r, strict=True)) if r else None

    async def list_for(self, user_id: str) -> list[dict[str, Any]]:
        async with self._conn.execute(
            "SELECT id, title, status, created_at, updated_at FROM sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(zip(("id", "title", "status", "created_at", "updated_at"), r, strict=True)) for r in rows]

    async def set_status(self, session_id: str, status: str) -> None:
        await self._conn.execute("UPDATE sessions SET status=?, updated_at=? WHERE id=?", (status, time.time(), session_id))
        await self._conn.commit()

    async def set_title(self, session_id: str, title: str) -> None:
        await self._conn.execute("UPDATE sessions SET title=? WHERE id=?", (title[:80], session_id))
        await self._conn.commit()
