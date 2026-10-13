"""In-memory evaluator for the subset of the SpiceDB schema language we use.

This exists so the platform runs (and unit tests pass) without a SpiceDB
container. It parses the *same* `schema.zed` that is written to SpiceDB, so
there is one source of truth; `tests/test_spicedb_parity.py` checks both
backends agree when a real SpiceDB is available.

Supported: `relation`, and `permission` expressions built from unions (`+`)
of relations, other permissions, and arrows (`relation->permission`).
Not supported (and rejected at parse time): intersections, exclusions,
caveats, wildcards, subject relations.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .base import ObjectRef, Relationship

_DEF = re.compile(r"definition\s+(\w+)\s*\{(.*?)\}", re.S)
_REL = re.compile(r"relation\s+(\w+)\s*:\s*([\w\s|]+?)\s*$", re.M)
_PERM = re.compile(r"permission\s+(\w+)\s*=\s*(.+?)\s*$", re.M)
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)


class SchemaError(ValueError):
    pass


def parse_schema(schema: str) -> dict[str, dict]:
    schema = _COMMENT.sub("", schema)
    defs: dict[str, dict] = {}
    for name, body in _DEF.findall(schema):
        relations = {r: [t.strip() for t in types.split("|")] for r, types in _REL.findall(body)}
        permissions: dict[str, list[tuple[str, str | None]]] = {}
        for perm, expr in _PERM.findall(body):
            if re.search(r"[&\-](?!>)|\(|\)|\*", expr):
                raise SchemaError(f"unsupported operator in {name}#{perm}: {expr!r}")
            terms: list[tuple[str, str | None]] = []
            for term in (t.strip() for t in expr.split("+")):
                if "->" in term:
                    left, right = (p.strip() for p in term.split("->", 1))
                    terms.append((left, right))
                else:
                    terms.append((term, None))
            permissions[perm] = terms
        defs[name] = {"relations": relations, "permissions": permissions}
    if not defs:
        raise SchemaError("schema has no definitions")
    return defs


class InMemoryAuthorizer:
    def __init__(self) -> None:
        self._defs: dict[str, dict] = {}
        # (resource_type, resource_id, relation) -> {subject}
        self._tuples: dict[tuple[str, str, str], set[ObjectRef]] = defaultdict(set)

    async def write_schema(self, schema: str) -> None:
        self._defs = parse_schema(schema)

    async def write(self, relationships: list[Relationship]) -> None:
        for r in relationships:
            d = self._defs.get(r.resource.type)
            if d is None or r.relation not in d["relations"]:
                raise SchemaError(f"unknown relation {r.resource.type}#{r.relation}")
            if r.subject.type not in d["relations"][r.relation]:
                raise SchemaError(f"{r.subject.type} is not an allowed subject for {r.resource.type}#{r.relation}")
            self._tuples[(r.resource.type, r.resource.id, r.relation)].add(r.subject)

    async def delete(self, relationships: list[Relationship]) -> None:
        for r in relationships:
            self._tuples[(r.resource.type, r.resource.id, r.relation)].discard(r.subject)

    def _eval(self, resource: ObjectRef, name: str, subject: ObjectRef, depth: int = 0) -> bool:
        if depth > 16:
            raise SchemaError("permission evaluation too deep (cycle?)")
        d = self._defs.get(resource.type)
        if d is None:
            return False
        if name in d["relations"]:
            return subject in self._tuples.get((resource.type, resource.id, name), ())
        terms = d["permissions"].get(name)
        if terms is None:
            return False
        for left, arrow in terms:
            if arrow is None:
                if self._eval(resource, left, subject, depth + 1):
                    return True
            else:
                for parent in self._tuples.get((resource.type, resource.id, left), ()):
                    if self._eval(parent, arrow, subject, depth + 1):
                        return True
        return False

    async def check(self, resource: ObjectRef, permission: str, subject: ObjectRef) -> bool:
        return self._eval(resource, permission, subject)

    async def check_many(self, checks: list[tuple[ObjectRef, str]], subject: ObjectRef) -> list[bool]:
        return [self._eval(res, perm, subject) for res, perm in checks]

    async def lookup_resources(self, resource_type: str, permission: str, subject: ObjectRef) -> list[str]:
        ids = {rid for (rt, rid, _rel) in self._tuples if rt == resource_type}
        return sorted(i for i in ids if self._eval(ObjectRef(resource_type, i), permission, subject))

    async def close(self) -> None:
        return None
