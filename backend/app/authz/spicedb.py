"""SpiceDB-backed authorizer (gRPC, via the official `authzed` client)."""

from __future__ import annotations

import logging

import grpc
from authzed.api.v1 import (
    AsyncClient,
    CheckBulkPermissionsRequest,
    CheckBulkPermissionsRequestItem,
    CheckPermissionRequest,
    CheckPermissionResponse,
    Consistency,
    LookupResourcesRequest,
    ObjectReference,
    RelationshipUpdate,
    SubjectReference,
    WriteRelationshipsRequest,
    WriteSchemaRequest,
)
from authzed.api.v1 import Relationship as PbRelationship
from grpcutil import bearer_token_credentials, insecure_bearer_token_credentials

from .base import ObjectRef, Relationship

# Admin actions must never be authorized against a stale snapshot (e.g. right
# after a role is revoked), so every check is fully consistent. This trades
# some latency for correctness; a ZedToken-per-session scheme would be the
# next step if check latency mattered.
_FULL = Consistency(fully_consistent=True)
log = logging.getLogger("authz.spicedb")


def _obj(o: ObjectRef) -> ObjectReference:
    return ObjectReference(object_type=o.type, object_id=o.id)


def _subj(o: ObjectRef) -> SubjectReference:
    return SubjectReference(object=_obj(o))


def _pb(r: Relationship) -> PbRelationship:
    return PbRelationship(resource=_obj(r.resource), relation=r.relation, subject=_subj(r.subject))


class SpiceDBAuthorizer:
    def __init__(self, endpoint: str, token: str, *, insecure: bool = True) -> None:
        creds = insecure_bearer_token_credentials(token) if insecure else bearer_token_credentials(token)
        self._client = AsyncClient(endpoint, creds)

    async def write_schema(self, schema: str) -> None:
        await self._client.WriteSchema(WriteSchemaRequest(schema=schema))

    async def _update(self, op: int, relationships: list[Relationship]) -> None:
        if not relationships:
            return
        await self._client.WriteRelationships(
            WriteRelationshipsRequest(
                updates=[RelationshipUpdate(operation=op, relationship=_pb(r)) for r in relationships]
            )
        )

    async def write(self, relationships: list[Relationship]) -> None:
        await self._update(RelationshipUpdate.Operation.OPERATION_TOUCH, relationships)

    async def delete(self, relationships: list[Relationship]) -> None:
        await self._update(RelationshipUpdate.Operation.OPERATION_DELETE, relationships)

    async def check(self, resource: ObjectRef, permission: str, subject: ObjectRef) -> bool:
        try:
            resp = await self._client.CheckPermission(
                CheckPermissionRequest(
                    resource=_obj(resource), permission=permission, subject=_subj(subject), consistency=_FULL
                )
            )
        except grpc.aio.AioRpcError as exc:
            # Asking about a type or permission the schema does not define is a deny.
            # Anything else (SpiceDB down, bad credentials) must surface, not be mistaken for "no".
            if exc.code() in (grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.INVALID_ARGUMENT):
                log.warning("check on undefined schema element %s#%s: %s", resource, permission, exc.details())
                return False
            raise
        return resp.permissionship == CheckPermissionResponse.PERMISSIONSHIP_HAS_PERMISSION

    async def check_many(self, checks: list[tuple[ObjectRef, str]], subject: ObjectRef) -> list[bool]:
        if not checks:
            return []
        resp = await self._client.CheckBulkPermissions(
            CheckBulkPermissionsRequest(
                consistency=_FULL,
                items=[
                    CheckBulkPermissionsRequestItem(resource=_obj(res), permission=perm, subject=_subj(subject))
                    for res, perm in checks
                ],
            )
        )
        out: list[bool] = []
        for pair in resp.pairs:
            # A per-item error (e.g. unknown permission) is a deny, not a crash.
            ok = (
                pair.HasField("item")
                and pair.item.permissionship == CheckPermissionResponse.PERMISSIONSHIP_HAS_PERMISSION
            )
            out.append(ok)
        return out

    async def lookup_resources(self, resource_type: str, permission: str, subject: ObjectRef) -> list[str]:
        stream = self._client.LookupResources(
            LookupResourcesRequest(
                resource_object_type=resource_type, permission=permission, subject=_subj(subject), consistency=_FULL
            )
        )
        return sorted([r.resource_object_id async for r in stream])

    async def close(self) -> None:
        channel = getattr(self._client, "_channel", None)
        if isinstance(channel, grpc.aio.Channel):
            await channel.close()
