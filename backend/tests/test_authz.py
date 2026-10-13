"""The authorization model itself, evaluated by the in-memory backend."""

from __future__ import annotations

import pytest

from app.authz import InMemoryAuthorizer, ObjectRef, load_schema, rel
from app.authz.memory import SchemaError, parse_schema
from app.authz.seed import seed_relationships

ACME = ObjectRef("organization", "acme")


@pytest.fixture
async def authz():
    a = InMemoryAuthorizer()
    await a.write_schema(load_schema())
    await a.write(seed_relationships())
    return a


def user(name: str) -> ObjectRef:
    return ObjectRef("user", name)


# (user, permission, expected) - one row per interesting cell of the role matrix.
MATRIX = [
    ("alice", "manage_roles", True),  # owner
    ("alice", "view_billing", True),
    ("bob", "operate_services", True),  # operator
    ("bob", "manage_flags", True),
    ("bob", "manage_users", False),
    ("bob", "view_audit", False),
    ("bob", "view_billing", False),
    ("carol", "view_audit", True),  # auditor: read-only everywhere
    ("carol", "view_users", True),
    ("carol", "manage_flags", False),
    ("carol", "operate_services", False),
    ("dave", "access", True),  # plain member
    ("dave", "view_users", False),
    ("dave", "view_services", False),
    ("erin", "access", False),  # owner of a different org
    ("erin", "manage_roles", False),
    ("mallory", "access", False),  # unknown user
]


@pytest.mark.parametrize(("who", "permission", "expected"), MATRIX)
async def test_role_matrix(authz, who, permission, expected):
    assert await authz.check(ACME, permission, user(who)) is expected


async def test_service_permissions_flow_through_the_org_arrow(authz):
    gateway, search = ObjectRef("service", "api-gateway"), ObjectRef("service", "search-api")
    assert await authz.check(gateway, "operate", user("bob"))  # operator, via org->operate_services
    assert await authz.check(gateway, "view", user("carol"))
    assert not await authz.check(gateway, "operate", user("carol"))
    # dave has no org-level service rights but maintains exactly one service
    assert await authz.check(search, "operate", user("dave"))
    assert not await authz.check(gateway, "operate", user("dave"))
    assert not await authz.check(gateway, "view", user("dave"))
    # another tenant's owner gets nothing here
    assert not await authz.check(search, "view", user("erin"))


async def test_lookup_resources(authz):
    assert await authz.lookup_resources("service", "operate", user("dave")) == ["search-api"]
    assert await authz.lookup_resources("service", "operate", user("erin")) == ["storefront"]
    assert len(await authz.lookup_resources("service", "view", user("carol"))) == 4
    assert await authz.lookup_resources("service", "operate", user("carol")) == []


async def test_grant_and_revoke_take_effect_immediately(authz):
    r = rel("organization:acme", "admin", "user:dave")
    assert not await authz.check(ACME, "manage_users", user("dave"))
    await authz.write([r])
    assert await authz.check(ACME, "manage_users", user("dave"))
    await authz.delete([r])
    assert not await authz.check(ACME, "manage_users", user("dave"))


async def test_unknown_permission_or_type_is_a_deny_not_an_error(authz):
    assert not await authz.check(ACME, "no_such_permission", user("alice"))
    assert not await authz.check(ObjectRef("spaceship", "x"), "view", user("alice"))


async def test_write_rejects_relations_outside_the_schema(authz):
    with pytest.raises(SchemaError):
        await authz.write([rel("organization:acme", "superuser", "user:dave")])
    with pytest.raises(SchemaError):
        await authz.write([rel("organization:acme", "owner", "service:notifier")])


def test_parser_refuses_operators_it_cannot_evaluate():
    with pytest.raises(SchemaError):
        parse_schema("definition a { relation x: user\n relation y: user\n permission p = x & y }")
    with pytest.raises(SchemaError):
        parse_schema("definition a { relation x: user\n relation y: user\n permission p = x - y }")
