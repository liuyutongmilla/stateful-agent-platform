"""Graph state. Everything here is plain JSON-able data so checkpoints stay portable."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

STAGES = ("intake", "plan", "execute", "review", "finalize")


class AgentState(TypedDict, total=False):
    # Conversation, append-only. Shapes:
    #   {"role": "user", "id", "content"}
    #   {"role": "assistant", "id", "content", "tool_calls": [{"id", "name", "args"}]}
    #   {"role": "tool", "tool_call_id", "name", "content", "status": ok|denied|rejected|error}
    messages: Annotated[list[dict[str, Any]], operator.add]

    # Who the agent is acting for. Set from the authenticated session, never by the model.
    user_id: str
    org_id: str
    session_id: str

    # Per-run bookkeeping (reset by `intake` at the start of every run).
    run_id: str
    stage: str
    allowed_tools: list[str]
    plan: list[str]
    iterations: int
    limit_reached: bool
    review: dict[str, Any]
    final: str


def current_run_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Messages belonging to the run in progress: everything after the last user turn."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] == "user":
            return messages[i + 1 :]
    return messages
