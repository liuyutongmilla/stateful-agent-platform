"""Drives graph runs as background tasks and turns graph output into channel events.

A run is decoupled from any WebSocket: it publishes to the hub and keeps going
if the client disconnects. If the run itself is cut short (cancel, crash,
deploy), the last checkpoint is still on disk and `resume()` continues from it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from typing import Any

from langgraph.types import Command

from ..gateway.hub import Hub
from .graph import recursion_limit
from .sessions import RESUMABLE, SessionStore
from .state import STAGES

log = logging.getLogger("agent.runtime")


class RunConflict(Exception):
    """The requested operation does not fit the session's current run state."""


class AgentRuntime:
    def __init__(self, graph: Any, hub: Hub, sessions: SessionStore, *, max_iterations: int) -> None:
        self.graph = graph
        self.hub = hub
        self.sessions = sessions
        self.max_iterations = max_iterations
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._handled_ops: OrderedDict[tuple[str, str], None] = OrderedDict()

    def _config(self, session_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": recursion_limit(self.max_iterations)}

    def is_running(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    def first_time(self, session_id: str, op_id: Any) -> bool:
        """Record a client command id; False if this command was already handled (a retry).

        A failed command stays recorded too: its error already told the client the outcome,
        and replaying it later against changed state would be a different command.
        Commands without an id are never deduplicated.
        """
        if not isinstance(op_id, str) or not op_id:
            return True
        key = (session_id, op_id)
        if key in self._handled_ops:
            return False
        self._handled_ops[key] = None
        while len(self._handled_ops) > 20_000:
            self._handled_ops.popitem(last=False)
        return True

    # ------------------------------------------------------------- operations
    async def send(self, session: dict[str, Any], text: str) -> None:
        sid = session["id"]
        if self.is_running(sid):
            raise RunConflict("a run is already in progress for this session")
        state = await self.graph.aget_state(self._config(sid))
        if state.next:
            raise RunConflict("this session has an unfinished run; resume it (or answer its approval) first")
        run_id = uuid.uuid4().hex[:12]
        message = {"role": "user", "id": f"{run_id}:user", "content": text}
        if not state.values.get("messages"):
            await self.sessions.set_title(sid, text)
        self.hub.publish(sid, "user_message", {"id": message["id"], "text": text})
        self._spawn(
            sid,
            {
                "messages": [message],
                "run_id": run_id,
                "user_id": session["user_id"],
                "org_id": session["org_id"],
                "session_id": sid,
            },
        )

    async def resume(self, session: dict[str, Any]) -> None:
        sid = session["id"]
        if self.is_running(sid):
            raise RunConflict("a run is already in progress for this session")
        state = await self.graph.aget_state(self._config(sid))
        if not state.next:
            raise RunConflict("nothing to resume")
        if _pending_approval(state):
            raise RunConflict("this run is waiting for an approval decision, not a resume")
        self.hub.publish(sid, "resumed", {"from_stage": _stage_of(state), "next": list(state.next)})
        self._spawn(sid, None)

    async def approve(self, session: dict[str, Any], approved: bool) -> None:
        sid = session["id"]
        if self.is_running(sid):
            raise RunConflict("a run is already in progress for this session")
        state = await self.graph.aget_state(self._config(sid))
        if not _pending_approval(state):
            raise RunConflict("no approval is pending")
        self.hub.publish(sid, "approval_resolved", {"approved": approved})
        self._spawn(sid, Command(resume={"approved": approved}))

    async def cancel(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def shutdown(self) -> None:
        for sid in list(self._tasks):
            await self.cancel(sid)

    # ---------------------------------------------------------------- snapshot
    async def snapshot(self, session: dict[str, Any]) -> dict[str, Any]:
        """Full client-visible state of a session, rebuilt from its latest checkpoint."""
        state = await self.graph.aget_state(self._config(session["id"]))
        values = state.values
        status = "running" if self.is_running(session["id"]) else session["status"]
        return {
            "status": status,
            "stage": _stage_of(state),
            "stages": list(STAGES),
            "iterations": values.get("iterations", 0),
            "max_iterations": self.max_iterations,
            "plan": values.get("plan", []),
            "allowed_tools": values.get("allowed_tools", []),
            "messages": values.get("messages", []),
            "pending_approval": _pending_approval(state),
            "resumable": bool(state.next) and status in RESUMABLE,
        }

    # ------------------------------------------------------------------ driver
    def _spawn(self, session_id: str, graph_input: Any) -> None:
        task = asyncio.create_task(self._drive(session_id, graph_input), name=f"run:{session_id}")
        self._tasks[session_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(session_id, None) if self._tasks.get(session_id) is t else None)

    async def _set_status(self, session_id: str, status: str, **extra: Any) -> None:
        await self.sessions.set_status(session_id, status)
        self.hub.publish(session_id, "status", {"status": status, **extra})

    async def _drive(self, session_id: str, graph_input: Any) -> None:
        config = self._config(session_id)
        try:
            await self._set_status(session_id, "running")
            async for mode, chunk in self.graph.astream(
                graph_input,
                config,
                stream_mode=["custom", "updates"],
                durability="sync",  # checkpoint is on disk before the next node starts
            ):
                if mode == "custom":
                    self.hub.publish(session_id, chunk["type"], chunk["data"], chunk.get("ts"))
                elif "__interrupt__" in chunk:
                    for intr in chunk["__interrupt__"]:
                        self.hub.publish(session_id, "approval_required", intr.value)
            state = await self.graph.aget_state(config)
            await self._set_status(session_id, "awaiting_approval" if _pending_approval(state) else "idle")
        except asyncio.CancelledError:
            # Shielded so the status write survives the cancellation that got us here.
            await asyncio.shield(self._set_status(session_id, "interrupted", resumable=True))
            raise
        except Exception as exc:  # noqa: BLE001 - a run must never take the server down
            log.exception("run failed for session %s", session_id)
            self.hub.publish(session_id, "error", {"message": f"{type(exc).__name__}: {exc}"})
            await self._set_status(session_id, "error", resumable=True)


def _pending_approval(state: Any) -> dict[str, Any] | None:
    for task in state.tasks:
        for intr in task.interrupts:
            if isinstance(intr.value, dict) and intr.value.get("kind") == "approval":
                return intr.value
    return None


def _stage_of(state: Any) -> str:
    """The workflow stage a session is in (or was in when it stopped)."""
    if state.next:
        node = state.next[0]
        return "execute" if node in ("reason", "act") else node
    return state.values.get("stage", "")
