from __future__ import annotations

from ..config import Settings
from .base import Authorizer, ObjectRef, Relationship, load_schema, rel
from .memory import InMemoryAuthorizer
from .seed import seed_relationships

__all__ = ["Authorizer", "ObjectRef", "Relationship", "rel", "load_schema", "InMemoryAuthorizer", "make_authorizer"]


async def make_authorizer(settings: Settings, *, seed: bool = True) -> Authorizer:
    """SpiceDB when configured, otherwise the in-memory evaluator of the same schema."""
    if settings.spicedb_endpoint:
        from .spicedb import SpiceDBAuthorizer

        authz: Authorizer = SpiceDBAuthorizer(
            settings.spicedb_endpoint, settings.spicedb_token, insecure=settings.spicedb_insecure
        )
    else:
        authz = InMemoryAuthorizer()
    await authz.write_schema(load_schema())
    if seed:
        await authz.write(seed_relationships())
    return authz
