"""Administrative tools exposed over the Model Context Protocol.

Every tool goes through `admin_tool`, which is the single enforcement point:

1. **Authenticate the caller.** The request's `_meta` must carry a caller
   context token signed by the agent runtime, bound to this tool name.
   Missing/invalid/expired/mismatched tokens fail closed.
2. **Authorize against SpiceDB** as the *human* (`user:<id>`), on the tenant
   organization or on the specific service named in the arguments.
3. **Audit** the decision (allowed and denied alike).
4. **Deduplicate** mutating calls by tool-call id, so an agent workflow that
   is resumed from a checkpoint cannot apply the same side effect twice.

The agent has no standing privileges of its own: with no caller context it
can do nothing, and with one it can do exactly what that person can.
"""

from __future__ import annotations

import contextvars
import functools
import json
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from ..admin.store import AdminStore, NotFound
from ..auth import CALLER_META_KEY, AuthError, CallerContext, verify_caller_context
from ..authz import Authorizer, ObjectRef, rel

PERMISSION_META_KEY = "agentplatform.dev/permission"
GRANTABLE_ROLES = ("admin", "operator", "auditor", "member")
ORG_ROLES = ("owner", *GRANTABLE_ROLES)

Role = Literal["admin", "operator", "auditor", "member"]
Severity = Literal["sev1", "sev2", "sev3", "sev4"]

_caller: contextvars.ContextVar[CallerContext] = contextvars.ContextVar("caller")


@dataclass(frozen=True)
class ToolPolicy:
    permission: str
    scope: Literal["organization", "service"]
    resource_arg: str | None
    mutating: bool
    destructive: bool


def _fail(code: str, detail: str, **extra: Any) -> ToolError:
    return ToolError(json.dumps({"error": code, "detail": detail, **extra}))


class AdminToolServer:
    def __init__(self, authz: Authorizer, store: AdminStore, secret: str) -> None:
        self.authz = authz
        self.store = store
        self.secret = secret
        self.policies: dict[str, ToolPolicy] = {}
        self._idempotent: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self.mcp = MCPServer(
            "admin-tools",
            log_level="WARNING",  # denials are recorded in the audit log, not the process log
            instructions=(
                "Administrative tools for one tenant organization. Every call is authorized "
                "as the human the agent is acting for; a permission_denied error is final and "
                "must not be retried or worked around."
            ),
        )
        self._register()

    # ------------------------------------------------------------------ guard
    def _authenticate(self, ctx: Context, tool: str) -> CallerContext:
        meta = ctx.request_context.meta or {}
        token = meta.get(CALLER_META_KEY)
        if not isinstance(token, str):
            raise _fail("unauthenticated", "tool calls require a signed caller context")
        try:
            return verify_caller_context(self.secret, token, expected_tool=tool)
        except AuthError as exc:
            raise _fail("unauthenticated", str(exc)) from None

    def admin_tool(
        self,
        *,
        permission: str,
        scope: Literal["organization", "service"] = "organization",
        resource_arg: str | None = None,
        mutating: bool = False,
        destructive: bool = False,
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            name = fn.__name__
            policy = ToolPolicy(permission, scope, resource_arg, mutating, destructive)
            self.policies[name] = policy

            @functools.wraps(fn)
            async def guarded(*args: Any, **kwargs: Any) -> Any:
                caller = self._authenticate(kwargs["ctx"], name)
                if policy.scope == "service":
                    resource = ObjectRef("service", str(kwargs[policy.resource_arg or "service"]))
                else:
                    resource = ObjectRef("organization", caller.org_id)
                subject = ObjectRef("user", caller.user_id)
                allowed = await self.authz.check(resource, policy.permission, subject)
                self.store.record_audit(
                    caller.org_id,
                    {
                        "actor": caller.user_id,
                        "via": "agent",
                        "session": caller.session_id,
                        "tool": name,
                        "resource": str(resource),
                        "permission": policy.permission,
                        "decision": "allowed" if allowed else "denied",
                    },
                )
                if not allowed:
                    raise _fail(
                        "permission_denied",
                        f"user:{caller.user_id} lacks {resource.type}#{policy.permission} on {resource}",
                        tool=name,
                    )
                key = (caller.session_id, caller.call_id, name)
                if policy.mutating and caller.call_id and key in self._idempotent:
                    return self._idempotent[key]
                token = _caller.set(caller)
                try:
                    result = await fn(*args, **kwargs)
                except NotFound as exc:
                    raise _fail("not_found", str(exc.args[0])) from None
                except ValueError as exc:
                    raise _fail("invalid_argument", str(exc)) from None
                finally:
                    _caller.reset(token)
                if policy.mutating and caller.call_id:
                    self._idempotent[key] = result
                    while len(self._idempotent) > 10_000:
                        self._idempotent.popitem(last=False)
                return result

            self.mcp.tool(
                annotations=ToolAnnotations(
                    readOnlyHint=not mutating, destructiveHint=destructive, idempotentHint=True, openWorldHint=False
                ),
                meta={PERMISSION_META_KEY: f"{scope}#{permission}"},
            )(guarded)
            return guarded

        return decorator

    # ------------------------------------------------------------------ tools
    def _register(self) -> None:  # noqa: C901 - a flat list of small tools reads best in one place
        tool, store, authz = self.admin_tool, self.store, self.authz

        # ---- identity ------------------------------------------------------
        @tool(permission="access")
        async def whoami(ctx: Context) -> dict:
            """Describe the current caller: their roles and exactly which tools they may use."""
            c = _caller.get()
            subject, org = ObjectRef("user", c.user_id), ObjectRef("organization", c.org_id)
            roles = await authz.check_many([(org, r) for r in ORG_ROLES], subject)
            org_tools = [(n, p) for n, p in self.policies.items() if p.scope == "organization"]
            org_ok = await authz.check_many([(org, p.permission) for _, p in org_tools], subject)
            allowed = [n for (n, _), ok in zip(org_tools, org_ok, strict=True) if ok]
            own = set(store.services[c.org_id])
            reachable: dict[str, list[str]] = {}
            for name, p in self.policies.items():
                if p.scope != "service":
                    continue
                if p.permission not in reachable:
                    ids = await authz.lookup_resources("service", p.permission, subject)
                    reachable[p.permission] = [i for i in ids if i in own]
                if reachable[p.permission]:
                    allowed.append(name)
            return {
                "user": c.user_id,
                "organization": c.org_id,
                "roles": [r for r, ok in zip(ORG_ROLES, roles, strict=True) if ok],
                "allowed_tools": sorted(allowed),
                "operable_services": reachable.get("operate", []),
            }

        # ---- users ---------------------------------------------------------
        @tool(permission="view_users")
        async def list_users(ctx: Context, status: Literal["active", "invited", "suspended"] | None = None) -> dict:
            """List the organization's users, optionally filtered by status."""
            return {"users": store.list_users(_caller.get().org_id, status)}

        @tool(permission="view_users")
        async def get_user(user_id: str, ctx: Context) -> dict:
            """Get one user's profile, status, MFA enrollment and roles."""
            return store.get_user(_caller.get().org_id, user_id)

        @tool(permission="manage_users", mutating=True)
        async def invite_user(user_id: str, name: str, email: str, ctx: Context) -> dict:
            """Invite a new user to the organization as a plain member."""
            c = _caller.get()
            user = store.create_user(c.org_id, user_id, name, email)
            await authz.write([rel(f"organization:{c.org_id}", "member", f"user:{user_id}")])
            return user

        @tool(permission="manage_users", mutating=True, destructive=True)
        async def suspend_user(user_id: str, reason: str, ctx: Context) -> dict:
            """Suspend a user's account. They lose access until reactivated."""
            c = _caller.get()
            if user_id == c.user_id:
                raise ValueError("you cannot suspend your own account")
            user = store.get_user(c.org_id, user_id)
            user.update(status="suspended", suspended_reason=reason)
            return user

        @tool(permission="manage_users", mutating=True)
        async def reactivate_user(user_id: str, ctx: Context) -> dict:
            """Reactivate a suspended user."""
            user = store.get_user(_caller.get().org_id, user_id)
            user.update(status="active")
            user.pop("suspended_reason", None)
            return user

        @tool(permission="manage_users", mutating=True, destructive=True)
        async def reset_user_mfa(user_id: str, ctx: Context) -> dict:
            """Clear a user's MFA enrollment so they must re-enroll at next sign-in."""
            user = store.get_user(_caller.get().org_id, user_id)
            user.update(mfa_enrolled=False)
            return {"user_id": user_id, "mfa_enrolled": False}

        # ---- roles (these write SpiceDB relationships) ----------------------
        @tool(permission="manage_roles", mutating=True)
        async def grant_role(user_id: str, role: Role, ctx: Context) -> dict:
            """Grant an organization role to a user. The owner role cannot be granted through tools."""
            c = _caller.get()
            if role not in GRANTABLE_ROLES:
                raise ValueError(f"role must be one of {GRANTABLE_ROLES}")
            user = store.get_user(c.org_id, user_id)
            await authz.write([rel(f"organization:{c.org_id}", role, f"user:{user_id}")])
            if role not in user["roles"]:
                user["roles"].append(role)
            return {"user_id": user_id, "roles": user["roles"]}

        @tool(permission="manage_roles", mutating=True, destructive=True)
        async def revoke_role(user_id: str, role: Role, ctx: Context) -> dict:
            """Revoke an organization role from a user. The owner role cannot be revoked through tools."""
            c = _caller.get()
            if role not in GRANTABLE_ROLES:
                raise ValueError(f"role must be one of {GRANTABLE_ROLES}")
            user = store.get_user(c.org_id, user_id)
            await authz.delete([rel(f"organization:{c.org_id}", role, f"user:{user_id}")])
            user["roles"] = [r for r in user["roles"] if r != role]
            return {"user_id": user_id, "roles": user["roles"]}

        # ---- feature flags ---------------------------------------------------
        @tool(permission="view_flags")
        async def list_feature_flags(ctx: Context) -> dict:
            """List feature flags and whether each is enabled."""
            return {"flags": list(store.flags[_caller.get().org_id].values())}

        @tool(permission="manage_flags", mutating=True)
        async def set_feature_flag(key: str, enabled: bool, ctx: Context) -> dict:
            """Enable or disable a feature flag."""
            c = _caller.get()
            flag = store.get_flag(c.org_id, key)
            flag.update(enabled=enabled, updated_by=c.user_id)
            return flag

        # ---- services (authorized per service, not per org) ------------------
        @tool(permission="access")
        async def list_services(ctx: Context) -> dict:
            """List the services the caller is allowed to see, with health summary."""
            c = _caller.get()
            visible = set(await authz.lookup_resources("service", "view", ObjectRef("user", c.user_id)))
            return {"services": [s for n, s in store.services[c.org_id].items() if n in visible]}

        @tool(permission="view", scope="service", resource_arg="service")
        async def get_service_status(service: str, ctx: Context) -> dict:
            """Get detailed health for one service: status, replicas, error rate, p95 latency."""
            return store.get_service(_caller.get().org_id, service)

        @tool(permission="operate", scope="service", resource_arg="service", mutating=True, destructive=True)
        async def restart_service(service: str, reason: str, ctx: Context) -> dict:
            """Perform a rolling restart of one service."""
            return store.restart_service(_caller.get().org_id, service)

        @tool(permission="operate", scope="service", resource_arg="service", mutating=True)
        async def scale_service(service: str, replicas: int, ctx: Context) -> dict:
            """Set the replica count (1-50) for one service."""
            if not 1 <= replicas <= 50:
                raise ValueError("replicas must be between 1 and 50")
            svc = store.get_service(_caller.get().org_id, service)
            svc.update(replicas=replicas)
            return svc

        # ---- incidents -------------------------------------------------------
        @tool(permission="view_incidents")
        async def list_incidents(ctx: Context, status: Literal["open", "resolved"] | None = None) -> dict:
            """List incidents, optionally filtered by status."""
            rows = store.incidents[_caller.get().org_id]
            return {"incidents": [i for i in rows if status in (None, i["status"])]}

        @tool(permission="manage_incidents", mutating=True)
        async def open_incident(title: str, severity: Severity, ctx: Context, service: str | None = None) -> dict:
            """Open a new incident, optionally linked to a service."""
            c = _caller.get()
            if service is not None:
                store.get_service(c.org_id, service)
            return store.open_incident(c.org_id, title, severity, service, c.user_id)

        @tool(permission="manage_incidents", mutating=True)
        async def resolve_incident(incident_id: str, resolution: str, ctx: Context) -> dict:
            """Mark an incident as resolved with a resolution note."""
            for inc in store.incidents[_caller.get().org_id]:
                if inc["id"] == incident_id:
                    inc.update(status="resolved", resolution=resolution)
                    return inc
            raise NotFound(f"incident {incident_id!r} not found")

        # ---- audit / keys / billing ------------------------------------------
        @tool(permission="view_audit")
        async def query_audit_log(
            ctx: Context,
            actor: str | None = None,
            tool_name: str | None = None,
            decision: Literal["allowed", "denied"] | None = None,
            limit: int = 20,
        ) -> dict:
            """Search the audit log of tool invocations (newest first), including denied attempts."""
            rows = store.query_audit(_caller.get().org_id, actor=actor, tool=tool_name, decision=decision, limit=limit)
            return {"entries": rows}

        @tool(permission="view_keys")
        async def list_api_keys(ctx: Context) -> dict:
            """List API keys (prefix and metadata only; secrets are never returned)."""
            return {"api_keys": list(store.api_keys[_caller.get().org_id].values())}

        @tool(permission="manage_keys", mutating=True, destructive=True)
        async def rotate_api_key(key_id: str, ctx: Context) -> dict:
            """Rotate an API key. The old secret stops working immediately."""
            return store.rotate_api_key(_caller.get().org_id, key_id)

        @tool(permission="view_billing")
        async def get_usage_report(ctx: Context) -> dict:
            """Get this month's usage and estimated cost for the organization."""
            return store.usage_report(_caller.get().org_id)


def build_admin_server(authz: Authorizer, store: AdminStore | None, secret: str) -> AdminToolServer:
    return AdminToolServer(authz, store or AdminStore(), secret)
