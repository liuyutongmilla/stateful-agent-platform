"""Runs against a real SpiceDB. Skipped when no SpiceDB binary is available.

The point: prove the gRPC client works, and that the in-memory evaluator used
by the rest of the suite gives the same answers as SpiceDB for this schema.
"""

from __future__ import annotations

from dataclasses import replace

from app.authz import InMemoryAuthorizer, ObjectRef, load_schema, make_authorizer, rel
from app.authz.memory import parse_schema
from app.authz.seed import DEMO_USERS, SERVICES, seed_relationships
from app.config import get_settings
from tests.conftest import SPICEDB_KEY

USERS = [*DEMO_USERS, "mallory"]


def settings_for(endpoint: str):
    return replace(get_settings(), spicedb_endpoint=endpoint, spicedb_token=SPICEDB_KEY, spicedb_insecure=True)


async def memory_backend() -> InMemoryAuthorizer:
    m = InMemoryAuthorizer()
    await m.write_schema(load_schema())
    await m.write(seed_relationships())
    return m


async def test_in_memory_evaluator_agrees_with_spicedb_on_every_check(spicedb_endpoint):
    spice = await make_authorizer(settings_for(spicedb_endpoint))
    memory = await memory_backend()
    schema = parse_schema(load_schema())
    resources = [ObjectRef("organization", o) for o in SERVICES] + [ObjectRef("service", s) for ss in SERVICES.values() for s in ss]
    compared = 0
    try:
        for resource in resources:
            names = [*schema[resource.type]["permissions"], *schema[resource.type]["relations"]]
            names = [n for n in names if n != "org"]  # `org` takes an organization subject, not a user
            for who in USERS:
                subject = ObjectRef("user", who)
                expected = await memory.check_many([(resource, n) for n in names], subject)
                actual = await spice.check_many([(resource, n) for n in names], subject)
                assert actual == expected, (str(resource), who, dict(zip(names, zip(expected, actual, strict=True), strict=True)))
                # single checks take a different RPC; spot-check they agree too
                assert await spice.check(resource, names[0], subject) == expected[0]
                compared += len(names)
        for who in USERS:
            subject = ObjectRef("user", who)
            for permission in ("view", "operate"):
                assert await spice.lookup_resources("service", permission, subject) == await memory.lookup_resources(
                    "service", permission, subject
                )
    finally:
        await spice.close()
    assert compared > 300


async def test_relationship_writes_and_deletes_are_visible_to_the_next_check(spicedb_endpoint):
    spice = await make_authorizer(settings_for(spicedb_endpoint))
    acme, dave, grant = ObjectRef("organization", "acme"), ObjectRef("user", "dave"), rel("organization:acme", "admin", "user:dave")
    try:
        assert not await spice.check(acme, "manage_users", dave)
        await spice.write([grant])
        assert await spice.check(acme, "manage_users", dave)  # fully consistent: no stale read
        await spice.write([grant])  # TOUCH: writing twice is not an error
        await spice.delete([grant])
        assert not await spice.check(acme, "manage_users", dave)
        # undefined permissions and types deny rather than raise, same as the in-memory backend
        assert not await spice.check(acme, "no_such_permission", dave)
        assert not await spice.check(ObjectRef("spaceship", "x"), "view", dave)
    finally:
        await spice.close()


async def test_agent_tool_calls_are_authorized_by_spicedb_end_to_end(spicedb_endpoint, make_platform):
    p = await make_platform(spicedb_endpoint=spicedb_endpoint, spicedb_token=SPICEDB_KEY)
    assert type(p.app.state.admin_server.authz).__name__ == "SpiceDBAuthorizer"

    dave, sid = await p.session("dave")
    await dave.send(sid, "restart api-gateway")
    await dave.wait_status(sid, "awaiting_approval")
    await dave.approve(sid, True)
    await dave.wait_status(sid, "idle")
    assert dave.of_type(sid, "tool_result")[0]["data"]["status"] == "denied"

    # the owner grants dave a role through the agent; SpiceDB then lets him in
    alice, a_sid = await p.session("alice")
    await alice.send(a_sid, "grant operator to dave")
    await alice.wait_status(a_sid, "idle")
    assert alice.of_type(a_sid, "tool_result")[0]["data"]["status"] == "ok"

    dave2, sid2 = await p.session("dave")
    await dave2.send(sid2, "restart api-gateway")
    await dave2.wait_status(sid2, "awaiting_approval")
    await dave2.approve(sid2, True)
    await dave2.wait_status(sid2, "idle")
    assert dave2.of_type(sid2, "tool_result")[0]["data"]["status"] == "ok"
    assert "operator" in dave2.of_type(sid2, "tools")[0]["data"]["roles"]
