"""Two kinds of signed tokens, separated by audience.

* **Session tokens** (`aud=gateway`) identify the human on the REST/WebSocket
  edge.
* **Caller-context tokens** (`aud=mcp-tools`) are minted by the agent runtime
  for *one* tool call and travel in the MCP request's `_meta`. They carry the
  human's identity down to the tool server so that authorization is evaluated
  against the person who asked, never against the agent's own identity.

  Each token is bound to a single tool name and tool-call id and expires in
  seconds, so a leaked or replayed token cannot be used to invoke a different
  tool or the same tool with a different idempotency key.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import jwt

ALG = "HS256"
AUD_GATEWAY = "gateway"
AUD_TOOLS = "mcp-tools"
CALLER_META_KEY = "agentplatform.dev/caller-context"
CALLER_TTL_SECONDS = 60


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: str
    org_id: str


@dataclass(frozen=True)
class CallerContext:
    """Who a tool call is being made on behalf of."""

    user_id: str
    org_id: str
    session_id: str
    tool: str
    call_id: str


def issue_session_token(secret: str, principal: Principal, ttl: int = 12 * 3600) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": principal.user_id, "org": principal.org_id, "aud": AUD_GATEWAY, "iat": now, "exp": now + ttl},
        secret,
        algorithm=ALG,
    )


def verify_session_token(secret: str, token: str) -> Principal:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALG], audience=AUD_GATEWAY)
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid session token: {exc}") from exc
    return Principal(user_id=claims["sub"], org_id=claims["org"])


def mint_caller_context(secret: str, ctx: CallerContext, ttl: int = CALLER_TTL_SECONDS) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": ctx.user_id,
            "org": ctx.org_id,
            "sid": ctx.session_id,
            "tool": ctx.tool,
            "call": ctx.call_id,
            "aud": AUD_TOOLS,
            "iat": now,
            "exp": now + ttl,
        },
        secret,
        algorithm=ALG,
    )


def verify_caller_context(secret: str, token: str, *, expected_tool: str) -> CallerContext:
    try:
        claims = jwt.decode(token, secret, algorithms=[ALG], audience=AUD_TOOLS)
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid caller context: {exc}") from exc
    if claims.get("tool") != expected_tool:
        raise AuthError("caller context was issued for a different tool")
    return CallerContext(
        user_id=claims["sub"],
        org_id=claims["org"],
        session_id=claims.get("sid", ""),
        tool=claims["tool"],
        call_id=claims.get("call", ""),
    )
