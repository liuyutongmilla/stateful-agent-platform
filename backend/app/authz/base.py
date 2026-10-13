"""Authorization interface shared by the SpiceDB client and the dev fallback."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Protocol


@dataclass(frozen=True)
class ObjectRef:
    type: str
    id: str

    def __str__(self) -> str:
        return f"{self.type}:{self.id}"


@dataclass(frozen=True)
class Relationship:
    resource: ObjectRef
    relation: str
    subject: ObjectRef

    def __str__(self) -> str:
        return f"{self.resource}#{self.relation}@{self.subject}"


def rel(resource: str, relation: str, subject: str) -> Relationship:
    """`rel("organization:acme", "owner", "user:alice")`"""
    rt, rid = resource.split(":", 1)
    st, sid = subject.split(":", 1)
    return Relationship(ObjectRef(rt, rid), relation, ObjectRef(st, sid))


class Authorizer(Protocol):
    async def write_schema(self, schema: str) -> None: ...

    async def write(self, relationships: list[Relationship]) -> None: ...

    async def delete(self, relationships: list[Relationship]) -> None: ...

    async def check(self, resource: ObjectRef, permission: str, subject: ObjectRef) -> bool: ...

    async def check_many(self, checks: list[tuple[ObjectRef, str]], subject: ObjectRef) -> list[bool]: ...

    async def lookup_resources(self, resource_type: str, permission: str, subject: ObjectRef) -> list[str]: ...

    async def close(self) -> None: ...


def load_schema() -> str:
    return resources.files("app.authz").joinpath("schema.zed").read_text()
