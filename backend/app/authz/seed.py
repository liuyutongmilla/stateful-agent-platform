"""Demo tenants. Two orgs so cross-tenant isolation is testable."""

from __future__ import annotations

from .base import Relationship, rel

DEMO_USERS: dict[str, dict[str, str]] = {
    "alice": {"org": "acme", "role": "owner", "name": "Alice Chen"},
    "bob": {"org": "acme", "role": "operator", "name": "Bob Okafor"},
    "carol": {"org": "acme", "role": "auditor", "name": "Carol Diaz"},
    "dave": {"org": "acme", "role": "member", "name": "Dave Kim"},
    "erin": {"org": "globex", "role": "owner", "name": "Erin Novak"},
}

SERVICES: dict[str, list[str]] = {
    "acme": ["api-gateway", "search-api", "billing-worker", "notifier"],
    "globex": ["storefront"],
}

# dave is a plain member of acme but maintains one service.
MAINTAINERS = {"search-api": ["dave"]}


def seed_relationships() -> list[Relationship]:
    out = [rel(f"organization:{u['org']}", u["role"], f"user:{uid}") for uid, u in DEMO_USERS.items()]
    for org, services in SERVICES.items():
        out += [rel(f"service:{s}", "org", f"organization:{org}") for s in services]
    for service, users in MAINTAINERS.items():
        out += [rel(f"service:{service}", "maintainer", f"user:{u}") for u in users]
    return out
