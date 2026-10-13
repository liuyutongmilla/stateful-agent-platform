"""Model interface plus two implementations.

`MockLLM` is a deterministic, offline stand-in that drives real tool calls
through the same code paths a hosted model would. It is what the test suite
and load test use, so their results say nothing about a real model's latency.

`LangChainLLM` adapts any LangChain chat model (Anthropic, OpenAI, ...).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .state import current_run_messages
from .tools import ToolSpec

Purpose = Literal["plan", "act"]
OnToken = Callable[[str], None]


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class LLMResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLM(Protocol):
    async def complete(
        self, purpose: Purpose, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec], on_token: OnToken
    ) -> LLMResult: ...


# --------------------------------------------------------------------------- mock


def _call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{uuid.uuid4().hex[:12]}", name=name, args=args)


def _bullets(rows: list[dict[str, Any]], *keys: str) -> str:
    return "\n".join("- " + " · ".join(str(r.get(k)) for k in keys if r.get(k) is not None) for r in rows) or "- (none)"


def _render(name: str, data: Any) -> str:
    """Turn one successful tool payload into a short human-readable line or list."""
    if not isinstance(data, dict):
        return str(data)
    if name == "list_users":
        return "Users:\n" + _bullets(data["users"], "id", "name", "status", "roles")
    if name == "list_services":
        return "Services:\n" + _bullets(data["services"], "name", "status", "replicas")
    if name == "get_service_status":
        return (
            f"{data['name']} is {data['status']}: {data['replicas']} replicas, "
            f"{data['error_rate_pct']}% errors, p95 {data['p95_latency_ms']} ms."
        )
    if name == "restart_service":
        return f"Restarted {data['name']}; it now reports {data['status']}."
    if name == "scale_service":
        return f"Scaled {data['name']} to {data['replicas']} replicas."
    if name == "list_feature_flags":
        return "Feature flags:\n" + "\n".join(f"- {f['key']}: {'on' if f['enabled'] else 'off'}" for f in data["flags"])
    if name == "set_feature_flag":
        return f"Flag {data['key']} is now {'on' if data['enabled'] else 'off'}."
    if name == "list_incidents":
        return "Incidents:\n" + _bullets(data["incidents"], "id", "severity", "title", "status")
    if name in ("open_incident", "resolve_incident"):
        return f"Incident {data['id']} ({data['severity']}) is {data['status']}: {data['title']}."
    if name == "query_audit_log":
        return "Audit log:\n" + _bullets(data["entries"], "at", "actor", "tool", "resource", "decision")
    if name == "list_api_keys":
        return "API keys:\n" + _bullets(data["api_keys"], "id", "label", "last_rotated")
    if name == "rotate_api_key":
        return f"Rotated {data['id']}; new key prefix is {data['prefix']}."
    if name == "get_usage_report":
        return (
            f"Usage for {data['period']}: {data['active_seats']} seats, {data['api_requests']:,} API requests, "
            f"estimated ${data['estimated_cost_usd']:,.2f}."
        )
    if name == "whoami":
        return (
            f"You are {data['user']} in {data['organization']} with role(s) {', '.join(data['roles']) or 'none'}. "
            f"You can use {len(data['allowed_tools'])} tools: {', '.join(data['allowed_tools'])}."
        )
    if name in ("suspend_user", "reactivate_user", "invite_user", "get_user"):
        return f"User {data['id']} ({data['name']}) is {data['status']}; roles: {', '.join(data['roles'])}."
    if name == "reset_user_mfa":
        return f"MFA enrollment cleared for {data['user_id']}."
    if name in ("grant_role", "revoke_role"):
        return f"{data['user_id']} now has role(s): {', '.join(data['roles']) or 'none'}."
    return json.dumps(data)


def _summarize(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for r in results:
        if r["status"] == "ok":
            try:
                lines.append(_render(r["name"], json.loads(r["content"])))
            except (json.JSONDecodeError, KeyError, TypeError):
                lines.append(r["content"])
        elif r["status"] == "denied":
            detail = json.loads(r["content"]).get("detail", "")
            lines.append(f"I could not run {r['name']}: permission denied ({detail}). I did not retry.")
        elif r["status"] == "rejected":
            lines.append(f"I did not run {r['name']} because you declined the approval.")
        else:
            detail = json.loads(r["content"]).get("detail", r["content"])
            lines.append(f"{r['name']} failed: {detail}")
    return "\n\n".join(lines)


CAPABILITIES = (
    "I can help administer your organization: users and roles, feature flags, service health "
    "and restarts, incidents, API keys, usage, and the audit log. Try \"run a health check\", "
    "\"list users\", \"restart search-api\", or \"what can I do?\"."
)

# (pattern, plan steps, builder) - first match wins, so specific intents come first.
_Intent = tuple[re.Pattern[str], list[str], Callable[[re.Match[str]], list[ToolCall]]]
_INTENTS: list[_Intent] = [
    (re.compile(r"restart (?:the )?([\w-]+)"), ["Restart the service", "Report its new status"],
     lambda m: [_call("restart_service", service=m[1], reason="requested by operator")]),
    (re.compile(r"scale (?:the )?([\w-]+) to (\d+)"), ["Scale the service", "Confirm replica count"],
     lambda m: [_call("scale_service", service=m[1], replicas=int(m[2]))]),
    (re.compile(r"reactivate (\w+)"), ["Reactivate the user"], lambda m: [_call("reactivate_user", user_id=m[1])]),
    (re.compile(r"suspend (\w+)"), ["Suspend the user", "Confirm the new status"],
     lambda m: [_call("suspend_user", user_id=m[1], reason="requested by administrator")]),
    (re.compile(r"reset mfa for (\w+)"), ["Reset MFA enrollment"], lambda m: [_call("reset_user_mfa", user_id=m[1])]),
    (re.compile(r"rotate (?:api key )?(key_\w+)"), ["Rotate the API key"], lambda m: [_call("rotate_api_key", key_id=m[1])]),
    (re.compile(r"grant (\w+)(?: role)? to (\w+)"), ["Grant the role"], lambda m: [_call("grant_role", user_id=m[2], role=m[1])]),
    (re.compile(r"revoke (\w+)(?: role)? from (\w+)"), ["Revoke the role"], lambda m: [_call("revoke_role", user_id=m[2], role=m[1])]),
    (re.compile(r"(enable|disable) (?:the )?(?:flag )?([\w-]+)"), ["Update the feature flag"],
     lambda m: [_call("set_feature_flag", key=m[2], enabled=m[1] == "enable")]),
    (re.compile(r"open (?:an? )?(sev[1-4]) incident(?: for ([\w-]+))?:? ?(.*)"), ["Open the incident"],
     lambda m: [_call("open_incident", title=m[3] or "Untitled incident", severity=m[1], **({"service": m[2]} if m[2] else {}))]),
    (re.compile(r"status of ([\w-]+)"), ["Fetch service status"], lambda m: [_call("get_service_status", service=m[1])]),
    (re.compile(r"denied"), ["Search the audit log for denied attempts"], lambda m: [_call("query_audit_log", decision="denied")]),
    (re.compile(r"audit"), ["Read the audit log"], lambda m: [_call("query_audit_log")]),
    (re.compile(r"users?\b"), ["List users"], lambda m: [_call("list_users")]),
    (re.compile(r"flags?\b"), ["List feature flags"], lambda m: [_call("list_feature_flags")]),
    (re.compile(r"incidents?\b"), ["List incidents"], lambda m: [_call("list_incidents")]),
    (re.compile(r"api keys?|keys\b"), ["List API keys"], lambda m: [_call("list_api_keys")]),
    (re.compile(r"usage|billing|cost"), ["Fetch the usage report"], lambda m: [_call("get_usage_report")]),
    (re.compile(r"who ?am ?i|what can i do|permissions"), ["Look up my roles and tools"], lambda m: [_call("whoami")]),
    (re.compile(r"services?\b"), ["List services"], lambda m: [_call("list_services")]),
]
_HEALTH = re.compile(r"health ?check|triage")
_LOOP = re.compile(r"loop forever")
_STREAM = re.compile(r"stream (\d+)")


class MockLLM:
    def __init__(self, token_delay_ms: int = 15) -> None:
        self._delay = token_delay_ms / 1000

    async def _stream(self, text: str, on_token: OnToken) -> None:
        for piece in re.findall(r"\S+\s*", text):
            if self._delay:
                await asyncio.sleep(self._delay)
            on_token(piece)

    async def complete(
        self, purpose: Purpose, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec], on_token: OnToken
    ) -> LLMResult:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        text = user.lower().strip()
        if purpose == "plan":
            return LLMResult(text=json.dumps(self._plan(text)))
        available = {t.name for t in tools}
        results = [m for m in current_run_messages(messages) if m["role"] == "tool"]
        step = self._act(text, results)
        if isinstance(step, list):
            missing = sorted({c.name for c in step} - available)
            if missing:
                step = (
                    f"I can't do that for you: {', '.join(missing)} is not among the tools your role permits. "
                    "Ask an organization owner if you need this access."
                )
            else:
                return LLMResult(tool_calls=step)
        await self._stream(step, on_token)
        return LLMResult(text=step)

    def _plan(self, text: str) -> list[str]:
        if _HEALTH.search(text):
            return ["List services", "Inspect unhealthy services and open incidents", "Summarize and recommend"]
        if _LOOP.search(text):
            return ["Keep calling tools until the iteration limit stops the loop"]
        if _STREAM.search(text):
            return ["Answer directly"]
        for pattern, plan, _ in _INTENTS:
            if pattern.search(text):
                return plan
        return ["Answer directly"]

    def _act(self, text: str, results: list[dict[str, Any]]) -> list[ToolCall] | str:
        if m := _STREAM.search(text):
            return " ".join(f"tok{i}" for i in range(int(m[1])))
        if _LOOP.search(text):
            return [_call("whoami")]
        if _HEALTH.search(text):
            return self._health(results)
        if results:
            return _summarize(results)
        for pattern, _, build in _INTENTS:
            if m := pattern.search(text):
                return build(m)
        return CAPABILITIES

    @staticmethod
    def _health(results: list[dict[str, Any]]) -> list[ToolCall] | str:
        if not results:
            return [_call("list_services")]
        if len(results) == 1 and results[0]["status"] == "ok":
            services = json.loads(results[0]["content"])["services"]
            unhealthy = [s["name"] for s in services if s["status"] != "healthy"]
            if not services:
                return "You don't have visibility into any services."
            if not unhealthy:
                return _summarize(results) + "\n\nAll services are healthy. No action needed."
            return [*[_call("get_service_status", service=n) for n in unhealthy], _call("list_incidents", status="open")]
        summary = _summarize(results)
        degraded = [json.loads(r["content"])["name"] for r in results if r["name"] == "get_service_status" and r["status"] == "ok"]
        if degraded:
            summary += (
                f"\n\nRecommendation: {', '.join(degraded)} needs attention. "
                f"Say \"restart {degraded[0]}\" and I'll do a rolling restart (it will ask for your approval)."
            )
        return summary


# ---------------------------------------------------------------------- langchain


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b if isinstance(b, str) else b.get("text", "") if isinstance(b, dict) and b.get("type") == "text" else ""
            for b in content
        )
    return ""


def to_langchain_messages(system: str, messages: list[dict[str, Any]]) -> list[Any]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    out: list[Any] = [SystemMessage(content=system)]
    for m in messages:
        if m["role"] == "user":
            out.append(HumanMessage(content=m["content"]))
        elif m["role"] == "assistant":
            out.append(
                AIMessage(
                    content=m.get("content", ""),
                    tool_calls=[{"id": c["id"], "name": c["name"], "args": c["args"]} for c in m.get("tool_calls", [])],
                )
            )
        else:
            out.append(
                ToolMessage(
                    content=m["content"],
                    tool_call_id=m["tool_call_id"],
                    name=m["name"],
                    status="success" if m["status"] == "ok" else "error",
                )
            )
    return out


class LangChainLLM:
    """Adapter over any LangChain chat model that supports `bind_tools` and `astream`."""

    def __init__(self, model: Any) -> None:
        self._model = model

    async def complete(
        self, purpose: Purpose, system: str, messages: list[dict[str, Any]], tools: list[ToolSpec], on_token: OnToken
    ) -> LLMResult:
        model = self._model
        if purpose == "act" and tools:
            model = model.bind_tools(
                [
                    {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                    for t in tools
                ]
            )
        acc: Any = None
        pieces: list[str] = []
        async for chunk in model.astream(to_langchain_messages(system, messages)):
            if piece := _text_of(chunk.content):
                pieces.append(piece)
                if purpose == "act":
                    on_token(piece)
            acc = chunk if acc is None else acc + chunk
        if acc is None:
            return LLMResult()
        calls = [
            ToolCall(id=c.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=c["name"], args=c.get("args") or {})
            for c in getattr(acc, "tool_calls", [])
        ]
        return LLMResult(text="".join(pieces), tool_calls=calls)


def make_llm(provider: str, model: str, mock_token_delay_ms: int) -> LLM:
    if provider == "mock":
        return MockLLM(mock_token_delay_ms)
    from langchain.chat_models import init_chat_model  # optional dependency

    if not model and provider != "anthropic":
        raise ValueError("set LLM_MODEL for this provider")
    return LangChainLLM(init_chat_model(model or "claude-sonnet-5-5", model_provider=provider))
