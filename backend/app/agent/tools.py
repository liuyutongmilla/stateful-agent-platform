"""Bridge between the agent runtime and the MCP tool server.

The runtime never calls a tool "as itself". Each call mints a short-lived
caller-context token for the human behind the session and attaches it to the
MCP request `_meta`, where the server verifies it and runs the SpiceDB check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mcp import Client
from mcp.shared.exceptions import MCPError

from ..auth import CALLER_META_KEY, CallerContext, mint_caller_context
from ..mcp_server.server import PERMISSION_META_KEY

TOOL_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    destructive: bool = False
    read_only: bool = True
    permission: str = ""


@dataclass
class ToolOutcome:
    status: str  # ok | denied | error
    content: str  # what the model sees
    data: Any = field(default=None)  # parsed JSON payload when status == ok


def _extract_error(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start >= 0:
        try:
            parsed = json.loads(text[start:])
            if isinstance(parsed, dict) and "error" in parsed:
                return parsed
        except json.JSONDecodeError:
            pass
    return {"error": "tool_error", "detail": text}


class ToolGateway:
    def __init__(self, client: Client, secret: str) -> None:
        self._client = client
        self._secret = secret
        self._specs: list[ToolSpec] | None = None

    async def specs(self) -> list[ToolSpec]:
        if self._specs is None:
            specs: list[ToolSpec] = []
            cursor: str | None = None
            while True:
                page = await self._client.list_tools(cursor=cursor)
                for t in page.tools:
                    ann = t.annotations
                    specs.append(
                        ToolSpec(
                            name=t.name,
                            description=t.description or "",
                            input_schema=t.input_schema,
                            destructive=bool(ann and ann.destructive_hint),
                            read_only=bool(ann and ann.read_only_hint),
                            permission=(t.meta or {}).get(PERMISSION_META_KEY, ""),
                        )
                    )
                cursor = page.next_cursor
                if not cursor:
                    break
            self._specs = specs
        return self._specs

    async def call(self, name: str, args: dict[str, Any], caller: CallerContext) -> ToolOutcome:
        token = mint_caller_context(self._secret, caller)
        try:
            result = await self._client.call_tool(
                name, args, read_timeout_seconds=TOOL_TIMEOUT_SECONDS, meta={CALLER_META_KEY: token}
            )
        except MCPError as exc:
            return ToolOutcome("error", json.dumps({"error": "tool_error", "detail": str(exc)}))
        text = "\n".join(getattr(c, "text", "") for c in result.content if getattr(c, "type", "") == "text")
        if result.is_error:
            err = _extract_error(text)
            status = "denied" if err["error"] in ("permission_denied", "unauthenticated") else "error"
            return ToolOutcome(status, json.dumps(err))
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
        return ToolOutcome("ok", json.dumps(data, separators=(",", ":")) if not isinstance(data, str) else data, data)
