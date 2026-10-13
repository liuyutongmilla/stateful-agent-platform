"""The admin domain the tools operate on.

Deliberately an in-memory store with seed data: the interesting parts of this
project are the agent runtime, transport and authorization, not CRUD
persistence. Every method takes the tenant (`org`) explicitly so a tool can
never touch another tenant's data by accident.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict
from typing import Any

from ..authz.seed import DEMO_USERS, SERVICES


class NotFound(KeyError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AdminStore:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.flags: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.services: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.incidents: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.api_keys: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.audit: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._seed()

    def _seed(self) -> None:
        for uid, u in DEMO_USERS.items():
            self.users[u["org"]][uid] = {
                "id": uid,
                "name": u["name"],
                "email": f"{uid}@{u['org']}.example",
                "status": "active",
                "mfa_enrolled": uid != "dave",
                "roles": [u["role"]],
                "created_at": "2026-01-15T09:00:00Z",
            }
        health = {
            "api-gateway": ("healthy", 6, 0.2, 84),
            "search-api": ("degraded", 3, 4.7, 912),
            "billing-worker": ("healthy", 2, 0.0, 130),
            "notifier": ("healthy", 2, 0.1, 61),
            "storefront": ("healthy", 4, 0.3, 140),
        }
        for org, names in SERVICES.items():
            for name in names:
                status, replicas, err, p95 = health[name]
                self.services[org][name] = {
                    "name": name,
                    "status": status,
                    "replicas": replicas,
                    "version": "2026.10.2",
                    "error_rate_pct": err,
                    "p95_latency_ms": p95,
                    "last_restart": "2026-10-08T03:12:00Z",
                }
        for key, enabled, desc in [
            ("new-checkout", False, "Redesigned checkout flow"),
            ("semantic-search", True, "Embedding-based search ranking"),
            ("bulk-export", False, "CSV export for workspace admins"),
        ]:
            self.flags["acme"][key] = {"key": key, "enabled": enabled, "description": desc, "updated_by": "system"}
        self.flags["globex"]["dark-mode"] = {
            "key": "dark-mode",
            "enabled": True,
            "description": "Dark theme",
            "updated_by": "system",
        }
        for kid, label in [("key_ci", "CI deploy pipeline"), ("key_analytics", "Analytics export")]:
            self.api_keys["acme"][kid] = {
                "id": kid,
                "label": label,
                "prefix": "sk_" + secrets.token_hex(3),
                "created_at": "2026-06-01T00:00:00Z",
                "last_rotated": "2026-06-01T00:00:00Z",
            }
        self.incidents["acme"].append(
            {
                "id": "INC-1001",
                "title": "Elevated search latency",
                "severity": "sev3",
                "service": "search-api",
                "status": "open",
                "opened_by": "bob",
                "opened_at": "2026-10-11T22:40:00Z",
            }
        )

    # --- users -------------------------------------------------------------
    def list_users(self, org: str, status: str | None = None) -> list[dict[str, Any]]:
        return [u for u in self.users[org].values() if status in (None, u["status"])]

    def get_user(self, org: str, user_id: str) -> dict[str, Any]:
        try:
            return self.users[org][user_id]
        except KeyError:
            raise NotFound(f"user {user_id!r} not found") from None

    def create_user(self, org: str, user_id: str, name: str, email: str) -> dict[str, Any]:
        if user_id in self.users[org]:
            raise ValueError(f"user {user_id!r} already exists")
        user = {
            "id": user_id,
            "name": name,
            "email": email,
            "status": "invited",
            "mfa_enrolled": False,
            "roles": ["member"],
            "created_at": _now(),
        }
        self.users[org][user_id] = user
        return user

    # --- services ----------------------------------------------------------
    def get_service(self, org: str, name: str) -> dict[str, Any]:
        try:
            return self.services[org][name]
        except KeyError:
            raise NotFound(f"service {name!r} not found") from None

    def restart_service(self, org: str, name: str) -> dict[str, Any]:
        svc = self.get_service(org, name)
        svc.update(status="healthy", error_rate_pct=0.1, p95_latency_ms=min(svc["p95_latency_ms"], 150), last_restart=_now())
        return svc

    # --- flags / keys / incidents -------------------------------------------
    def get_flag(self, org: str, key: str) -> dict[str, Any]:
        try:
            return self.flags[org][key]
        except KeyError:
            raise NotFound(f"feature flag {key!r} not found") from None

    def get_api_key(self, org: str, key_id: str) -> dict[str, Any]:
        try:
            return self.api_keys[org][key_id]
        except KeyError:
            raise NotFound(f"api key {key_id!r} not found") from None

    def rotate_api_key(self, org: str, key_id: str) -> dict[str, Any]:
        key = self.get_api_key(org, key_id)
        key.update(prefix="sk_" + secrets.token_hex(3), last_rotated=_now())
        return key

    def open_incident(self, org: str, title: str, severity: str, service: str | None, opened_by: str) -> dict[str, Any]:
        inc = {
            "id": f"INC-{1001 + len(self.incidents[org])}",
            "title": title,
            "severity": severity,
            "service": service,
            "status": "open",
            "opened_by": opened_by,
            "opened_at": _now(),
        }
        self.incidents[org].append(inc)
        return inc

    def usage_report(self, org: str) -> dict[str, Any]:
        seats = len([u for u in self.users[org].values() if u["status"] != "suspended"])
        return {
            "period": "2026-10",
            "active_seats": seats,
            "api_requests": 18_420_113 if org == "acme" else 2_104_877,
            "compute_hours": 6_312 if org == "acme" else 804,
            "estimated_cost_usd": 14_870.25 if org == "acme" else 1_930.10,
        }

    # --- audit -------------------------------------------------------------
    def record_audit(self, org: str, entry: dict[str, Any]) -> None:
        self.audit[org].append({"at": _now(), **entry})
        del self.audit[org][:-5000]

    def query_audit(self, org: str, *, actor: str | None, tool: str | None, decision: str | None, limit: int) -> list[dict[str, Any]]:
        rows = [
            e
            for e in reversed(self.audit[org])
            if actor in (None, e["actor"]) and tool in (None, e["tool"]) and decision in (None, e["decision"])
        ]
        return rows[: max(1, min(limit, 200))]
