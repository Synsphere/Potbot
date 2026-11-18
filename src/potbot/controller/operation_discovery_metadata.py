"""Verified descriptive contracts for the existing operation discovery owner.

Imported Cog declarations remain source assertions. This reader verifies their
sealed artifacts and exact signatures; it neither imports handlers nor inspects
native worker configuration. The ordinary worker still proves installation,
task admission, exact ports and execution policy before dispatch.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.cogs.contracts import CogSignature
from potbot.cogs.registry import INSTALLED_COGS
from potbot.domain.auth import WorkspaceContext
from potbot.domain.discovery import digest_value, text_value
from potbot.domain.errors import DomainError, InvalidInputError, ScopeDeniedError
from potbot.domain.syn import SynRef, SynSnapshot, type_identity
from potbot.persistence.artifacts import ArtifactStore, LocalArtifactBackend
from potbot.persistence.database import Database
from potbot.semantics.catalog import (
    builtin_types,
    canonical_type_ref,
    catalog_fingerprint,
    extension_types,
    merge_types,
    type_ancestors,
)
from potbot.semantics.service import SynService
from potbot.workspace_sandbox import POLICY_VERSION, WorkspaceSandbox

SCHEMA = "potbot-operation-discovery-metadata/v1"
MAX_SNAPSHOT_SOURCES = 32
MAX_METADATA_BYTES = 32 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
_FIELDS = ("classes", "inputTypes", "outputTypes", "effects", "scopes", "platforms")
_SIGNATURE_FIELDS = frozenset(
    {
        "inputTypeRef",
        "outputTypeRef",
        "handlerRef",
        "implementationRevision",
        "requiresControllerScope",
        "implementationBindingHash",
    }
)
_MODEL_DESCRIPTION = (
    "Generate a text response from an explicit prompt through the configured hosted model "
    "generation operation. Supports reasoning and coding request roles. The worker checks "
    "the selected provider, model, disclosure policy, credentials and task budget before use."
)


def _empty(reason: str, *, provenance: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "source": "unavailable",
        "description": "",
        "capabilities": {key: [] for key in _FIELDS},
        "requiredTaskCapability": None,
        "provenance": provenance or [],
        "reasonCodes": [reason],
    }


def _description(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.encode("utf-8")) > 64 * 1024:
        raise InvalidInputError("The sealed operation description is invalid")
    # Descriptive source text is plain, bounded display data, never instructions.
    return text_value(" ".join(value.split())[:4096], "Operation description", 4096, empty=True)


def project_cog_metadata(snapshot: SynSnapshot, reference: str) -> dict[str, Any]:
    """Project one declared Cog from already verified source bytes.

    The signature's hash checks the declared binding, not installation. A native
    worker must compare these ports with its actual selected operation. No class,
    scope or platform is guessed from a name, description or arbitrary payload.
    """
    ref = SynRef(reference)
    if ref.kind != "cog":
        raise InvalidInputError("Operation metadata requires an exact Cog reference")
    entries = [entry for entry in snapshot.objects if entry.ref == ref]
    if len(entries) != 1:
        raise ScopeDeniedError("The selected sealed snapshot does not contain this Cog")
    entry = entries[0]
    types = merge_types(snapshot)
    declared = {type_identity(record.ref): record for record in types}
    closure = type_ancestors(types)
    cog_type = str(canonical_type_ref(entry.type_ref))
    if "syn://type/cog" not in closure.get(cog_type, ()) and cog_type != "syn://type/cog":
        raise ScopeDeniedError("The declared operation is not a canonical Cog subtype")
    payload = json.loads(entry.payload_json)
    if not _SIGNATURE_FIELDS <= payload.keys():
        raise InvalidInputError("The sealed Cog lacks an exact typed signature")
    signature = CogSignature(
        ref,
        str(entry.type_ref),
        payload["inputTypeRef"],
        payload["outputTypeRef"],
        payload["handlerRef"],
        payload["implementationRevision"],
        payload["requiresControllerScope"],
    )
    if digest_value(payload["implementationBindingHash"]) != signature.binding_hash:
        raise ScopeDeniedError("The sealed Cog signature binding is inconsistent")
    if any(
        type_identity(port) not in declared
        for port in (signature.input_type_ref, signature.output_type_ref)
    ):
        raise ScopeDeniedError("A sealed Cog port is absent from its canonical type catalogue")
    # Explicit object description takes precedence. An absent description may
    # use the exact concrete type's definition, never the object/type name.
    description = _description(
        payload.get("description", declared[type_identity(entry.type_ref)].definition)
    )
    # These four locally installed transforms are pure parsing/scope checks.
    # An arbitrary source cannot obtain this effect claim by copying their name.
    pure = signature in INSTALLED_COGS
    capabilities = {
        "classes": [],
        "inputTypes": [str(canonical_type_ref(signature.input_type_ref))],
        "outputTypes": [str(canonical_type_ref(signature.output_type_ref))],
        "effects": ["read_only"] if pure else [],
        "scopes": [],
        "platforms": [],
    }
    return {
        "schema": SCHEMA,
        "source": "sealed_syn_declaration",
        "description": description,
        "capabilities": capabilities,
        "requiredTaskCapability": "cog.execute:"
        + reference.replace("syn://cog/", "syn://thing/", 1),
        "provenance": [],
        "reasonCodes": [
            "syn_cog_declaration_verified",
            "native_worker_check_required",
            *(["installed_pure_signature_known"] if pure else []),
            *([] if description else ["operation_description_unavailable"]),
        ],
    }


def hosted_generation_metadata() -> dict[str, Any]:
    """Describe the installed text generation route, without rating any model.

    This describes the prompt/text values handled by the existing model owner.
    Provider selection, paid-call effects, credentials and disclosure approval
    remain separate worker checks. No literal task capability is invented.
    """
    registered = {
        str(canonical_type_ref(record.ref)) for record in (*builtin_types(), *extension_types())
    }
    text_type = str(canonical_type_ref("synny://type/potbot-data-text"))
    ports = [text_type] if text_type in registered else []
    return {
        "schema": SCHEMA,
        "source": "installed_model_generation_contract",
        "description": _MODEL_DESCRIPTION,
        "capabilities": {
            "classes": [],
            "inputTypes": ports,
            "outputTypes": ports,
            "effects": [],
            "scopes": [],
            "platforms": ["remote"],
        },
        "requiredTaskCapability": None,
        "provenance": [
            {
                "owner": "model.generate",
                "contract": "potbot.models.providers.contracts.ProviderRequest",
            }
        ],
        "reasonCodes": ["hosted_generation_contract", "hosted_worker_check_required"],
    }


class OperationDiscoveryMetadata:
    """One bounded catalogue read, reusing immutable Syn/artifact owners.

    ``artifacts`` is an internal dependency seam, never request data. Production
    resolves the already registered workspace artifact root, or the exact
    operator-configured ``POTBOT_SYN_ARTIFACT_ROOT`` beneath that root, without
    creating directories or loading native worker profiles. Missing or invalid
    configuration leaves metadata explicitly unavailable. No network, model or
    process is invoked.
    """

    def __init__(
        self,
        database: Database,
        context: WorkspaceContext,
        *,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        if artifacts is not None and artifacts.db is not database:
            raise InvalidInputError("Operation metadata and artifacts must share a database")
        self.database, self.context, self.artifacts = database, context, artifacts
        self._snapshots: dict[str, tuple[SynSnapshot, dict[str, Any]]] = {}
        self._attempted_snapshots: set[str] = set()
        self._source_bytes = 0
        self._artifact_unavailable = False

    def _store(self) -> ArtifactStore:
        if self._artifact_unavailable:
            raise ScopeDeniedError("The workspace artifact owner is unavailable")
        if self.artifacts is not None:
            return self.artifacts
        try:
            with self.database.transaction(self.context) as conn:
                conn.execute("SET TRANSACTION READ ONLY")
                row = conn.execute(
                    "SELECT root_path,root_key,policy_version FROM workspace_sandboxes WHERE workspace_id=%s",
                    (self.context.workspace_id,),
                ).fetchone()
            if row is None or row["policy_version"] != POLICY_VERSION:
                raise ScopeDeniedError("The workspace artifact owner is unavailable")
            sandbox = WorkspaceSandbox.existing(row["root_path"])
            boundary = sandbox.path("artifacts")
            if (
                sandbox.key != row["root_key"]
                or not boundary.is_dir()
                or boundary.resolve(strict=True) != boundary
            ):
                raise ScopeDeniedError("The workspace artifact root is unavailable")
            configured = os.environ.get("POTBOT_SYN_ARTIFACT_ROOT")
            if configured is not None and (
                not configured or len(configured) > 4096 or any(ord(c) < 32 for c in configured)
            ):
                raise ScopeDeniedError("The configured Syn artifact root is unavailable")
            root = Path(configured) if configured is not None else boundary
            if (
                not root.is_absolute()
                or not root.is_relative_to(boundary)
                or not root.is_dir()
                or root.resolve(strict=True) != root
            ):
                raise ScopeDeniedError("The configured Syn artifact root is unavailable")
            self.artifacts = ArtifactStore(
                self.database, LocalArtifactBackend(root, create_root=False)
            )
        except (DomainError, OSError, ValueError, TypeError):
            self._artifact_unavailable = True
            raise ScopeDeniedError("The workspace artifact owner is unavailable") from None
        return self.artifacts

    def _snapshot(self, identity: str, expected_source: str) -> tuple[SynSnapshot, dict[str, Any]]:
        if identity in self._snapshots:
            source, provenance = self._snapshots[identity]
            if source.fingerprint != expected_source:
                raise ScopeDeniedError("The catalogue has conflicting source pins")
            return source, provenance
        if identity in self._attempted_snapshots:
            raise ScopeDeniedError("The sealed operation snapshot is unavailable")
        if len(self._attempted_snapshots) >= MAX_SNAPSHOT_SOURCES:
            raise ScopeDeniedError("The operation metadata snapshot allowance is exhausted")
        # Failed lookups and budget denials consume a distinct source attempt;
        # a corrupted shared artifact must not be reread for every candidate.
        self._attempted_snapshots.add(identity)
        artifacts = self._store()
        with self.database.transaction(self.context) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            retained = SynService._snapshot(conn, self.context, UUID(identity))
            record = artifacts._metadata(conn, self.context, retained["artifact_id"])
            byte_length = record["byte_length"]
            if type(byte_length) is not int or byte_length < 0:
                raise ScopeDeniedError("The sealed operation artifact length is invalid")
            if self._source_bytes + byte_length > MAX_SNAPSHOT_BYTES:
                raise ScopeDeniedError("The operation metadata byte allowance is exhausted")
            # Charge the immutable declared length before integrity verification.
            # A failed verified read cannot refund work already attempted.
            self._source_bytes += byte_length
            raw = artifacts.read_in_transaction(conn, self.context, retained["artifact_id"])
        if hashlib.sha256(raw).hexdigest() != retained["content_hash"]:
            raise ScopeDeniedError("The sealed operation artifact changed")
        effective = json.loads(raw)
        source = SynSnapshot.from_dict(effective["source"])
        types = merge_types(source)
        if (
            source.fingerprint != expected_source
            or source.fingerprint != retained["source_hash"]
            or catalog_fingerprint(types) != retained["catalog_hash"]
            or effective["catalog"]["types"] != [record.to_dict() for record in types]
            or effective["catalog"]["fingerprint"] != retained["catalog_hash"]
        ):
            raise ScopeDeniedError("The sealed operation catalogue is inconsistent")
        provenance = {
            "snapshotId": identity,
            "sourceSha256": source.fingerprint,
            "snapshotSha256": retained["content_hash"],
            "catalogSha256": retained["catalog_hash"],
        }
        self._snapshots[identity] = (source, provenance)
        return source, provenance

    def for_operation(self, item: dict[str, Any]) -> dict[str, Any] | None:
        if item["kind"] == "model" and item["reference"].startswith("model:"):
            return hosted_generation_metadata()
        if item["kind"] != "cog" or not item["reference"].startswith("syn://cog/"):
            return None
        sources = [row for row in item["sources"] if row["family"] == "syn_snapshot"]
        if not sources:
            return _empty("syn_cog_metadata_unavailable")
        if len(sources) > MAX_SNAPSHOT_SOURCES:
            return _empty("syn_cog_metadata_source_limit")
        projections: list[dict[str, Any]] = []
        provenance = []
        try:
            for row in sorted(sources, key=lambda value: value["recordId"]):
                identity = str(UUID(row["recordId"]))
                expected = digest_value(row["sha256"])
                snapshot, retained = self._snapshot(identity, expected)
                projected = project_cog_metadata(snapshot, item["reference"])
                # Independently verify the public owner record and its exact
                # membership in the complete retained snapshot, not only a hash
                # supplied alongside arbitrary mutable metadata.
                resolved = SynService(self.database, self._store()).resolve(
                    self.context, UUID(identity), item["reference"]
                )
                declared = next(
                    entry for entry in snapshot.objects if str(entry.ref) == item["reference"]
                )
                if (
                    resolved["scope"]["snapshot_id"] != identity
                    or resolved["snapshot"]["source_hash"] != expected
                    or resolved["snapshot"]["content_hash"] != retained["snapshotSha256"]
                    or resolved["snapshot"]["catalog_hash"] != retained["catalogSha256"]
                    or resolved["payload_json"] != declared.payload_json
                    or resolved["semantic_type_ref"] != str(declared.type_ref)
                ):
                    raise ScopeDeniedError(
                        "The resolved operation differs from its sealed snapshot"
                    )
                projections.append(projected)
                provenance.append(
                    {
                        **retained,
                        "objectSha256": resolved["artifact_digest"],
                        "sourceContentSha256": resolved["source_content_hash"],
                    }
                )
        except (DomainError, OSError, ValueError, TypeError, KeyError, StopIteration, UnicodeError):
            return _empty("syn_cog_metadata_unavailable")
        if any(projection != projections[0] for projection in projections[1:]):
            return _empty("syn_cog_metadata_conflicting", provenance=provenance)
        result = {**projections[0], "provenance": provenance}
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
            return _empty("syn_cog_metadata_source_limit")
        return result
