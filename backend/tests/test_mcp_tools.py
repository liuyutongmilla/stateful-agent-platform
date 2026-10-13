"""The MCP tool surface: authentication of caller context and SpiceDB-style enforcement."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from mcp import Client

from app.auth import CALLER_META_KEY, CallerContext, mint_caller_context
from app.authz import make_authorizer
from app.config import get_settings
from app.mcp_server import PERMISSION_META_KEY, build_admin_server
from tests.conftest import SPICEDB_KEY

SECRET = "test-secret-0123456789abcdef-0123456789"


class Harness:
    def __init__(self, client: Client, server) -> None:
        self.client, self.server, self._n = client, server, 0

    async def call(self, user: str, org: str, tool: str, args: dict | None = None, *, call_id: str | None = None):
        self._n += 1
        ctx = CallerContext(user, org, "sess-1", tool, call_id or f"c{self._n}")
        result = await self.client.call_tool(tool, args or {}, meta={CALLER_META_KEY: mint_caller_context(SECRET, ctx)})
        text = result.content[0].text
        payload = json.loads(text[text.find("{") :])
        return result.is_error, payload


@pytest.fixture(params=["memory", "spicedb"])
async def h(request):
    """Every test in this file runs against both authorization backends (SpiceDB when available)."""
    settings = replace(get_settings(), spicedb_endpoint="", secret=SECRET)
    if request.param == "spicedb":
        endpoint = request.getfixturevalue("spicedb_endpoint")
        settings = replace(settings, spicedb_endpoint=endpoint, spicedb_token=SPICEDB_KEY)
    server = build_admin_server(await make_authorizer(settings), None, SECRET)
    ready, done, box = asyncio.Event(), asyncio.Event(), {}

    async def hold_connection() -> None:
        # The MCP client must be opened and closed by the same task.
        async with Client(server.mcp) as client:
            box["h"] = Harness(client, server)
            ready.set()
            await done.wait()

    task = asyncio.create_task(hold_connection())
    await ready.wait()
    yield box["h"]
    done.set()
    await task


async def test_exposes_at_least_15_tools_each_mapped_to_a_permission(h):
    tools = (await h.client.list_tools()).tools
    assert len(tools) >= 15
    assert len({t.name for t in tools}) == len(tools)
    for t in tools:
        assert t.description
        assert "#" in t.meta[PERMISSION_META_KEY], f"{t.name} has no declared permission"
    assert set(h.server.policies) == {t.name for t in tools}


async def test_calls_without_caller_context_fail_closed(h):
    for tool in h.server.policies:
        # arguments are irrelevant: authentication happens before anything else
        result = await h.client.call_tool(tool, _minimal_args(tool))
        assert result.is_error and "unauthenticated" in result.content[0].text, tool


def _minimal_args(tool: str) -> dict:
    """Schema-valid arguments that target objects which do not exist, so an *allowed* call is still harmless."""
    return {
        "get_user": {"user_id": "ghost"},
        "invite_user": {"user_id": "bob", "name": "dup", "email": "dup@x"},  # already exists -> invalid_argument
        "suspend_user": {"user_id": "ghost", "reason": "r"},
        "reactivate_user": {"user_id": "ghost"},
        "reset_user_mfa": {"user_id": "ghost"},
        "grant_role": {"user_id": "ghost", "role": "admin"},
        "revoke_role": {"user_id": "ghost", "role": "operator"},
        "set_feature_flag": {"key": "ghost", "enabled": True},
        "get_service_status": {"service": "ghost"},
        "restart_service": {"service": "ghost", "reason": "r"},
        "scale_service": {"service": "ghost", "replicas": 3},
        "open_incident": {"title": "t", "severity": "sev3", "service": "ghost"},
        "resolve_incident": {"incident_id": "ghost", "resolution": "r"},
        "rotate_api_key": {"key_id": "ghost"},
    }.get(tool, {})


async def test_caller_context_is_bound_to_one_tool(h):
    token = mint_caller_context(SECRET, CallerContext("alice", "acme", "s", "list_feature_flags", "c"))
    result = await h.client.call_tool("list_users", {}, meta={CALLER_META_KEY: token})
    assert result.is_error and "different tool" in result.content[0].text


async def test_forged_and_expired_caller_contexts_are_rejected(h):
    forged = mint_caller_context("some-other-secret-0123456789abcdef-xyz", CallerContext("alice", "acme", "s", "list_users", "c"))
    expired = mint_caller_context(SECRET, CallerContext("alice", "acme", "s", "list_users", "c"), ttl=-5)
    for token in (forged, expired, "garbage"):
        result = await h.client.call_tool("list_users", {}, meta={CALLER_META_KEY: token})
        assert result.is_error and "unauthenticated" in result.content[0].text


async def test_every_org_tool_is_denied_to_a_user_without_the_permission(h):
    """dave (plain member) should be refused by every tool that needs more than membership."""
    for tool, policy in h.server.policies.items():
        if policy.scope != "organization" or policy.permission in ("access", "view_incidents"):
            continue
        is_error, payload = await h.call("dave", "acme", tool, _minimal_args(tool))
        assert is_error and payload["error"] == "permission_denied", tool


async def test_denied_mutations_have_no_effect(h):
    is_error, _ = await h.call("carol", "acme", "set_feature_flag", {"key": "new-checkout", "enabled": True})
    assert is_error
    _, flags = await h.call("alice", "acme", "list_feature_flags")
    assert next(f for f in flags["flags"] if f["key"] == "new-checkout")["enabled"] is False


async def test_service_tools_are_authorized_per_service(h):
    ok_err, ok = await h.call("dave", "acme", "restart_service", {"service": "search-api", "reason": "r"})
    assert not ok_err and ok["status"] == "healthy"
    is_error, payload = await h.call("dave", "acme", "restart_service", {"service": "api-gateway", "reason": "r"})
    assert is_error and payload["error"] == "permission_denied"
    _, visible = await h.call("dave", "acme", "list_services")
    assert [s["name"] for s in visible["services"]] == ["search-api"]


async def test_tenants_are_isolated(h):
    is_error, payload = await h.call("erin", "globex", "restart_service", {"service": "api-gateway", "reason": "r"})
    assert is_error and payload["error"] == "permission_denied"
    _, users = await h.call("erin", "globex", "list_users")
    assert [u["id"] for u in users["users"]] == ["erin"]
    # a token claiming the wrong org gets nothing: erin holds no relation on acme
    is_error, payload = await h.call("erin", "acme", "list_users")
    assert is_error and payload["error"] == "permission_denied"


async def test_whoami_reports_exactly_the_callable_tools(h):
    for who in ("alice", "bob", "carol", "dave"):
        _, me = await h.call(who, "acme", "whoami")
        for tool, policy in h.server.policies.items():
            if policy.scope != "organization":
                continue
            is_error, payload = await h.call(who, "acme", tool, _minimal_args(tool))
            denied = is_error and payload["error"] == "permission_denied"
            assert denied == (tool not in me["allowed_tools"]), (who, tool)


async def test_grant_role_changes_what_spicedb_allows_and_owner_is_not_grantable(h):
    assert (await h.call("dave", "acme", "list_users"))[0]
    assert not (await h.call("alice", "acme", "grant_role", {"user_id": "dave", "role": "auditor"}))[0]
    assert not (await h.call("dave", "acme", "list_users"))[0]
    assert not (await h.call("alice", "acme", "revoke_role", {"user_id": "dave", "role": "auditor"}))[0]
    assert (await h.call("dave", "acme", "list_users"))[0]
    # privilege escalation guards: only owners manage roles, and "owner" is rejected by the schema
    is_error, payload = await h.call("bob", "acme", "grant_role", {"user_id": "bob", "role": "admin"})
    assert is_error and payload["error"] == "permission_denied"
    result = await h.client.call_tool(
        "grant_role",
        {"user_id": "dave", "role": "owner"},
        meta={CALLER_META_KEY: mint_caller_context(SECRET, CallerContext("alice", "acme", "s", "grant_role", "cx"))},
    )
    assert result.is_error


async def test_mutating_calls_are_idempotent_per_tool_call_id(h):
    args = {"title": "Disk full", "severity": "sev2"}
    _, first = await h.call("bob", "acme", "open_incident", args, call_id="same-call")
    _, again = await h.call("bob", "acme", "open_incident", args, call_id="same-call")
    assert first == again
    _, listing = await h.call("bob", "acme", "list_incidents")
    assert sum(1 for i in listing["incidents"] if i["title"] == "Disk full") == 1
    _, other = await h.call("bob", "acme", "open_incident", args, call_id="different-call")
    assert other["id"] != first["id"]


async def test_allowed_and_denied_attempts_are_audited(h):
    await h.call("dave", "acme", "rotate_api_key", {"key_id": "key_ci"})
    await h.call("alice", "acme", "rotate_api_key", {"key_id": "key_ci"})
    _, log = await h.call("carol", "acme", "query_audit_log", {"tool_name": "rotate_api_key"})
    decisions = {(e["actor"], e["decision"]) for e in log["entries"]}
    assert decisions == {("dave", "denied"), ("alice", "allowed")}
    assert all(e["session"] == "sess-1" and e["permission"] == "manage_keys" for e in log["entries"])


async def test_bad_arguments_and_missing_objects_are_tool_errors(h):
    is_error, payload = await h.call("alice", "acme", "scale_service", {"service": "notifier", "replicas": 500})
    assert is_error and payload["error"] == "invalid_argument"
    is_error, payload = await h.call("alice", "acme", "get_user", {"user_id": "nobody"})
    assert is_error and payload["error"] == "not_found"
    is_error, payload = await h.call("alice", "acme", "suspend_user", {"user_id": "alice", "reason": "oops"})
    assert is_error and payload["error"] == "invalid_argument"
