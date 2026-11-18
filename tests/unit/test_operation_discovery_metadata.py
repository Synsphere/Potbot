from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from potbot.cogs.contracts import CogSignature
from potbot.cogs.registry import builtin_cog_snapshot
from potbot.controller import operation_discovery_metadata as metadata
from potbot.controller.task_discovery_ranking import rank_task_candidates
from potbot.controller.task_discovery_sources import _operation_candidate
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.serialization import canonical_json, content_hash
from potbot.domain.syn import SynObject, SynRef, SynSnapshot, SynType
from potbot.persistence.artifacts import ArtifactIntegrityError, LocalArtifactBackend
from potbot.workspace_sandbox import WorkspaceSandbox

REFERENCE = "syn://cog/retained-file-inspection"
INPUT = "synny://type/potbot-data-text"
OUTPUT = "synny://type/potbot-data-document-json"
ACTOR = WorkspaceContext(uuid4(), uuid4())


def cog_snapshot(
    *,
    description: str | None = "Inspect retained repository files and source paths.",
    name: str = "Opaque capability",
    input_type: str = INPUT,
    output_type: str = OUTPUT,
    reference: str = REFERENCE,
    extra_types: tuple[SynType, ...] = (),
    definition: str | None = "Inspect an immutable repository file.",
) -> SynSnapshot:
    concrete = "syn://type/" + SynRef(reference).slug + "-cog"
    signature = CogSignature(
        SynRef(reference), concrete, input_type, output_type, "potbot.cogs.fixture.inspect", 1, True
    )
    payload = {
        "id": reference,
        "typeRef": concrete,
        "name": name,
        "schemaVersion": 1,
        "inputTypeRef": input_type,
        "outputTypeRef": output_type,
        "implementationBindingHash": signature.binding_hash,
        "handlerRef": signature.handler_ref,
        "implementationRevision": 1,
        "requiresControllerScope": True,
    }
    if description is not None:
        payload["description"] = description
    return SynSnapshot(
        "explicit metadata fixture",
        "extend_builtin",
        (
            SynType(
                SynRef(concrete), "Concrete fixture", (SynRef("syn://type/cog"),), definition, ()
            ),
            *extra_types,
        ),
        (SynObject(SynRef(reference), SynRef(concrete), canonical_json(payload)),),
    )


def operation(*, reference: str = REFERENCE, kind: str = "cog") -> dict[str, Any]:
    return {
        "id": content_hash(reference),
        "kind": kind,
        "reference": reference,
        "name": "Names do not create authority",
        "state": "source_only",
        "contextObjectId": None,
        "version": None,
        "fingerprintSha256": None,
        "updateAvailable": None,
        "sources": [],
        "release": None,
        "reasons": ["source_metadata_only"],
    }


def with_payload(snapshot: SynSnapshot, **changes: Any) -> SynSnapshot:
    entry = snapshot.objects[0]
    payload = json.loads(entry.payload_json)
    payload.update(changes)
    return replace(snapshot, objects=(replace(entry, payload_json=canonical_json(payload)),))


def test_verified_cog_declarations_supply_exact_ports_but_not_installation_or_effects() -> None:
    value = metadata.project_cog_metadata(cog_snapshot(), REFERENCE)
    assert value["description"] == "Inspect retained repository files and source paths."
    assert value["capabilities"] == {
        "classes": [],
        "inputTypes": [INPUT],
        "outputTypes": [OUTPUT],
        "effects": [],
        "scopes": [],
        "platforms": [],
    }
    assert value["requiredTaskCapability"] == "cog.execute:syn://thing/retained-file-inspection"
    candidate = _operation_candidate(ACTOR, operation(), metadata=value)
    assert not candidate["installed"]
    assert "effects_unknown" in candidate["reasonCodes"]
    assert "canonical_scopes_unknown" in candidate["reasonCodes"]
    assert "canonical_ports_unknown" not in candidate["reasonCodes"]
    assert "task_capability_unknown" not in candidate["reasonCodes"]


def test_alias_ports_are_preserved_as_one_canonical_identity() -> None:
    value = metadata.project_cog_metadata(
        cog_snapshot(input_type="syn://type/potbot-data-text"), REFERENCE
    )
    assert value["capabilities"]["inputTypes"] == [INPUT]


def test_only_the_exact_installed_pure_signature_can_supply_read_only_effect() -> None:
    snapshot = builtin_cog_snapshot()
    ref = str(snapshot.objects[0].ref)
    assert metadata.project_cog_metadata(snapshot, ref)["capabilities"]["effects"] == ["read_only"]
    entry = snapshot.objects[0]
    payload = json.loads(entry.payload_json)
    signature = CogSignature(
        entry.ref,
        str(entry.type_ref),
        payload["inputTypeRef"],
        payload["outputTypeRef"],
        "potbot.cogs.fixture.unreviewed",
        1,
        False,
    )
    payload.update(
        handlerRef=signature.handler_ref, implementationBindingHash=signature.binding_hash
    )
    replaced = replace(snapshot, objects=(replace(entry, payload_json=canonical_json(payload)),))
    assert metadata.project_cog_metadata(replaced, ref)["capabilities"]["effects"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"implementationBindingHash": "0" * 64},
        {"inputTypeRef": "synny://type/potbot-data-record"},
        {"outputTypeRef": "synny://type/potbot-data-text"},
        {"implementationRevision": True},
        {"requiresControllerScope": "true"},
        {"handlerRef": "os.system"},
    ],
)
def test_inconsistent_or_unsupported_signature_cannot_publish_typed_metadata(changes) -> None:
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        metadata.project_cog_metadata(with_payload(cog_snapshot(), **changes), REFERENCE)


def test_unregistered_port_and_non_cog_concrete_type_cannot_publish_metadata() -> None:
    with pytest.raises(ScopeDeniedError, match="port"):
        metadata.project_cog_metadata(cog_snapshot(input_type="syn://type/absent-port"), REFERENCE)
    snapshot = cog_snapshot()
    record = replace(snapshot.types[0], parents=(SynRef("syn://type/thing"),))
    with pytest.raises(ScopeDeniedError, match="subtype"):
        metadata.project_cog_metadata(replace(snapshot, types=(record,)), REFERENCE)


def test_missing_full_signature_stays_unknown() -> None:
    snapshot = cog_snapshot()
    entry = snapshot.objects[0]
    payload = json.loads(entry.payload_json)
    del payload["implementationBindingHash"]
    with pytest.raises(InvalidInputError, match="signature"):
        metadata.project_cog_metadata(
            replace(snapshot, objects=(replace(entry, payload_json=canonical_json(payload)),)),
            REFERENCE,
        )


def test_concrete_type_definition_is_only_description_fallback() -> None:
    value = metadata.project_cog_metadata(cog_snapshot(description=None), REFERENCE)
    assert value["description"] == "Inspect an immutable repository file."
    value = metadata.project_cog_metadata(
        cog_snapshot(description=None, definition=None, name="inspect retained repository files"),
        REFERENCE,
    )
    assert value["description"] == ""
    assert "operation_description_unavailable" in value["reasonCodes"]
    candidate = _operation_candidate(ACTOR, operation(), metadata=value)
    ranked = rank_task_candidates(
        [candidate],
        "inspect retained repository files",
        None,
        task_capabilities=[value["requiredTaskCapability"]],
    )
    assert ranked["noSuitableCandidate"]


def test_payload_authority_claims_do_not_supply_classes_effects_scopes_or_permissions() -> None:
    snapshot = with_payload(
        cog_snapshot(),
        capabilities={"classes": ["repository_inspection"]},
        effectClass="read_only",
        scopes=["all"],
        requiredTaskCapability="everything",
        installed=True,
        qualified=True,
        executable=True,
    )
    value = metadata.project_cog_metadata(snapshot, REFERENCE)
    assert value["capabilities"]["classes"] == value["capabilities"]["effects"] == []
    assert value["capabilities"]["scopes"] == []
    assert value["requiredTaskCapability"] == "cog.execute:syn://thing/retained-file-inspection"


def test_metadata_description_change_is_in_owner_pin_but_names_never_add_relevance() -> None:
    item = operation()
    first = _operation_candidate(
        ACTOR, item, metadata=metadata.project_cog_metadata(cog_snapshot(), REFERENCE)
    )
    second = _operation_candidate(
        ACTOR,
        item,
        metadata=metadata.project_cog_metadata(
            cog_snapshot(description="Convert documents."), REFERENCE
        ),
    )
    assert first["ownerSha256"] != second["ownerSha256"]
    selected = rank_task_candidates(
        [first],
        "inspect repository files",
        None,
        task_capabilities=[first["requiredTaskCapability"]],
    )
    assert selected["items"][0]["typeFit"] == "not_requested"
    assert selected["items"][0]["taskPermission"] == "permitted"
    assert selected["items"][0]["executable"] is False
    denied = rank_task_candidates([first], "inspect repository files", None, task_capabilities=[])
    assert denied["noSuitableCandidate"]
    assert denied["excluded"][0]["reasonCodes"] == ["task_capability_denied"]


def test_model_description_describes_existing_route_without_promising_provider_eligibility() -> (
    None
):
    value = metadata.hosted_generation_metadata()
    assert value["capabilities"]["inputTypes"] == value["capabilities"]["outputTypes"] == [INPUT]
    assert value["requiredTaskCapability"] is None
    assert value["capabilities"]["effects"] == value["capabilities"]["classes"] == []
    candidate = _operation_candidate(
        ACTOR, operation(kind="model", reference="model:opaque"), metadata=value
    )
    ranked = rank_task_candidates(
        [candidate], "Generate a text response", None, task_capabilities=[]
    )
    assert not ranked["noSuitableCandidate"]
    assert ranked["items"][0]["taskPermission"] == "unknown"
    assert not ranked["items"][0]["installed"] and not ranked["items"][0]["executable"]
    assert rank_task_candidates(
        [candidate], "Generate a text response", {"requireLocal": True}, task_capabilities=[]
    )["noSuitableCandidate"]
    assert rank_task_candidates(
        [candidate],
        "Generate a text response",
        {"permittedEffects": ["read_only"]},
        task_capabilities=[],
    )["noSuitableCandidate"]


class NoDatabase:
    def transaction(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("This projection must not access a database or runtime")


def test_unrelated_and_hosted_owners_do_not_probe_native_configuration(monkeypatch) -> None:
    monkeypatch.setenv("POTBOT_NATIVE_COG_CONFIG", "/untrusted/worker/config")
    reader = metadata.OperationDiscoveryMetadata(NoDatabase(), ACTOR)  # type: ignore[arg-type]
    assert reader.for_operation(operation(kind="skill", reference="skill:opaque")) is None
    assert reader.for_operation(operation(kind="tool", reference="syn://thing/opaque")) is None
    assert (
        reader.for_operation(operation(kind="model", reference="model:opaque"))["source"]
        == "installed_model_generation_contract"
    )
    assert reader.for_operation(operation())["reasonCodes"] == ["syn_cog_metadata_unavailable"]


def reader_fixture(monkeypatch, snapshots: list[SynSnapshot]):
    identities = [str(uuid4()) for _ in snapshots]
    by_id = dict(zip(identities, snapshots, strict=True))
    reader = metadata.OperationDiscoveryMetadata(NoDatabase(), ACTOR)  # type: ignore[arg-type]
    item = operation()
    item["sources"] = [
        {
            "family": "syn_snapshot",
            "recordId": identity,
            "revision": None,
            "sha256": snapshot.fingerprint,
        }
        for identity, snapshot in by_id.items()
    ]
    reader._snapshot = lambda identity, expected: (
        by_id[identity],
        {
            "snapshotId": identity,
            "sourceSha256": expected,
            "snapshotSha256": expected,
            "catalogSha256": expected,
        },
    )
    reader._store = lambda: object()

    class Syn:
        def __init__(self, *args: Any) -> None:
            pass

        def resolve(self, context: Any, identity: UUID, reference: str) -> dict[str, Any]:
            source = by_id[str(identity)]
            entry = source.objects[0]
            return {
                "scope": {"snapshot_id": str(identity)},
                "snapshot": {
                    "source_hash": source.fingerprint,
                    "content_hash": source.fingerprint,
                    "catalog_hash": source.fingerprint,
                },
                "payload_json": entry.payload_json,
                "semantic_type_ref": str(entry.type_ref),
                "artifact_digest": content_hash(entry.payload_json),
                "source_content_hash": content_hash(entry.to_dict()),
            }

    monkeypatch.setattr(metadata, "SynService", Syn)
    return reader, item, Syn


def test_equivalent_namespaces_share_metadata_with_both_source_pins(monkeypatch) -> None:
    reader, item, _ = reader_fixture(
        monkeypatch, [cog_snapshot(), cog_snapshot(name="Different display name")]
    )
    value = reader.for_operation(item)
    assert value["source"] == "sealed_syn_declaration"
    assert len(value["provenance"]) == 2
    assert value["capabilities"]["inputTypes"] == [INPUT]


def test_conflicting_sealed_namespaces_do_not_select_arbitrary_port_or_description(
    monkeypatch,
) -> None:
    reader, item, _ = reader_fixture(
        monkeypatch, [cog_snapshot(), cog_snapshot(description="A different operation.")]
    )
    value = reader.for_operation(item)
    assert value["reasonCodes"] == ["syn_cog_metadata_conflicting"]
    assert value["description"] == "" and not any(value["capabilities"].values())
    assert len(value["provenance"]) == 2


def test_mutable_resolved_record_cannot_override_complete_immutable_source(monkeypatch) -> None:
    reader, item, owner = reader_fixture(monkeypatch, [cog_snapshot()])
    original = owner.resolve

    def forged(self, *args):
        result = original(self, *args)
        value = json.loads(result["payload_json"])
        value["description"] = "fabricated mutable metadata"
        result["payload_json"] = canonical_json(value)
        return result

    monkeypatch.setattr(owner, "resolve", forged)
    value = reader.for_operation(item)
    assert value["reasonCodes"] == ["syn_cog_metadata_unavailable"]
    assert value["description"] == ""


def test_source_bound_rejects_incomplete_projection_before_reading_owners() -> None:
    reader = metadata.OperationDiscoveryMetadata(NoDatabase(), ACTOR)  # type: ignore[arg-type]
    item = operation()
    item["sources"] = [
        {"family": "syn_snapshot", "recordId": str(uuid4()), "revision": None, "sha256": "a" * 64}
        for _ in range(33)
    ]
    assert reader.for_operation(item)["reasonCodes"] == ["syn_cog_metadata_source_limit"]


def snapshot_read_fixture(monkeypatch):
    source = cog_snapshot()
    types = metadata.merge_types(source)
    catalogue_hash = metadata.catalog_fingerprint(types)
    raw = canonical_json(
        {
            "source": source.to_dict(),
            "catalog": {
                "fingerprint": catalogue_hash,
                "types": [record.to_dict() for record in types],
            },
        }
    ).encode("utf-8")

    class Database:
        @contextmanager
        def transaction(self, actor):
            assert actor == ACTOR
            yield self

        def execute(self, statement):
            assert statement == "SET TRANSACTION READ ONLY"

    database = Database()

    class Artifacts:
        def __init__(self):
            self.db = database
            self.metadata_reads = []
            self.reads = []
            self.fail_integrity = False

        def _metadata(self, connection, context, artifact_id):
            assert connection is database and context == ACTOR
            self.metadata_reads.append(artifact_id)
            return {"byte_length": len(raw)}

        def read_in_transaction(self, connection, context, artifact_id):
            assert connection is database and context == ACTOR
            self.reads.append(artifact_id)
            if self.fail_integrity:
                raise ArtifactIntegrityError("The complete artifact failed integrity verification")
            return raw

    def retained_snapshot(connection, context, identity):
        assert connection is database and context == ACTOR
        return {
            "artifact_id": identity,
            "content_hash": metadata.hashlib.sha256(raw).hexdigest(),
            "source_hash": source.fingerprint,
            "catalog_hash": catalogue_hash,
        }

    artifacts = Artifacts()
    monkeypatch.setattr(metadata.SynService, "_snapshot", staticmethod(retained_snapshot))
    reader = metadata.OperationDiscoveryMetadata(database, ACTOR, artifacts=artifacts)
    return reader, source, artifacts, len(raw)


def test_failed_snapshot_integrity_is_not_reread_or_refunded(monkeypatch) -> None:
    reader, source, artifacts, byte_length = snapshot_read_fixture(monkeypatch)
    monkeypatch.setattr(metadata, "MAX_SNAPSHOT_BYTES", byte_length)
    artifacts.fail_integrity = True
    first, second = str(uuid4()), str(uuid4())
    with pytest.raises(ArtifactIntegrityError):
        reader._snapshot(first, source.fingerprint)
    with pytest.raises(ScopeDeniedError, match="snapshot is unavailable"):
        reader._snapshot(first, source.fingerprint)
    with pytest.raises(ScopeDeniedError, match="byte allowance"):
        reader._snapshot(second, source.fingerprint)
    with pytest.raises(ScopeDeniedError, match="snapshot is unavailable"):
        reader._snapshot(second, source.fingerprint)
    assert artifacts.reads == [UUID(first)]
    assert artifacts.metadata_reads == [UUID(first), UUID(second)]
    assert reader._source_bytes == byte_length


def test_budget_denied_snapshot_attempts_are_cached_and_count_toward_source_limit(
    monkeypatch,
) -> None:
    reader, source, artifacts, _ = snapshot_read_fixture(monkeypatch)
    monkeypatch.setattr(metadata, "MAX_SNAPSHOT_BYTES", 0)
    attempted = [str(uuid4()) for _ in range(metadata.MAX_SNAPSHOT_SOURCES)]
    for identity in attempted:
        with pytest.raises(ScopeDeniedError, match="byte allowance"):
            reader._snapshot(identity, source.fingerprint)
    with pytest.raises(ScopeDeniedError, match="snapshot is unavailable"):
        reader._snapshot(attempted[0], source.fingerprint)
    with pytest.raises(ScopeDeniedError, match="snapshot allowance"):
        reader._snapshot(str(uuid4()), source.fingerprint)
    assert artifacts.metadata_reads == [UUID(identity) for identity in attempted]
    assert artifacts.reads == []


def test_failed_distinct_snapshot_integrity_attempts_count_toward_source_limit(
    monkeypatch,
) -> None:
    reader, source, artifacts, byte_length = snapshot_read_fixture(monkeypatch)
    artifacts.fail_integrity = True
    monkeypatch.setattr(metadata, "MAX_SNAPSHOT_BYTES", byte_length * metadata.MAX_SNAPSHOT_SOURCES)
    attempted = [str(uuid4()) for _ in range(metadata.MAX_SNAPSHOT_SOURCES)]
    for identity in attempted:
        with pytest.raises(ArtifactIntegrityError):
            reader._snapshot(identity, source.fingerprint)
    with pytest.raises(ScopeDeniedError, match="snapshot allowance"):
        reader._snapshot(str(uuid4()), source.fingerprint)
    assert artifacts.metadata_reads == artifacts.reads == [UUID(identity) for identity in attempted]


def test_verified_snapshot_is_cached_without_double_charging(monkeypatch) -> None:
    reader, source, artifacts, byte_length = snapshot_read_fixture(monkeypatch)
    monkeypatch.setattr(metadata, "MAX_SNAPSHOT_BYTES", byte_length)
    identity = str(uuid4())
    first = reader._snapshot(identity, source.fingerprint)
    assert first[0] == source
    assert first[1]["snapshotId"] == identity
    assert first[1]["sourceSha256"] == source.fingerprint
    assert reader._snapshot(identity, source.fingerprint) == first
    with pytest.raises(ScopeDeniedError, match="conflicting source pins"):
        reader._snapshot(identity, "0" * 64)
    with pytest.raises(ScopeDeniedError, match="byte allowance"):
        reader._snapshot(str(uuid4()), source.fingerprint)
    assert artifacts.reads == [UUID(identity)]
    assert reader._source_bytes == byte_length


def test_public_projection_does_not_mutate_catalogue_source_records(monkeypatch) -> None:
    reader, item, _ = reader_fixture(monkeypatch, [cog_snapshot()])
    retained = copy.deepcopy(item)
    value = reader.for_operation(item)
    _operation_candidate(ACTOR, item, metadata=value)
    assert item == retained


def test_description_bounds_preserve_plain_text_and_reject_controls() -> None:
    value = metadata.project_cog_metadata(
        cog_snapshot(description="Read\n  retained\tfiles"), REFERENCE
    )
    assert value["description"] == "Read retained files"
    with pytest.raises(InvalidInputError):
        metadata.project_cog_metadata(cog_snapshot(description="not\x00plain"), REFERENCE)
    value = metadata.project_cog_metadata(cog_snapshot(description="a" * 5000), REFERENCE)
    assert len(value["description"]) == 4096


class RootDatabase:
    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self.sandbox = sandbox

    @contextmanager
    def transaction(self, actor):
        assert actor == ACTOR
        yield self

    def execute(self, statement, arguments=None):
        assert statement == "SET TRANSACTION READ ONLY" or (
            statement.startswith(
                "SELECT root_path,root_key,policy_version FROM workspace_sandboxes"
            )
            and arguments == (ACTOR.workspace_id,)
        )
        return self

    def fetchone(self):
        return {
            "root_path": str(self.sandbox.root),
            "root_key": self.sandbox.key,
            "policy_version": 1,
        }


def test_explicit_syn_artifact_subdirectory_is_reachable_without_probe_or_creation(
    tmp_path, monkeypatch
) -> None:
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    configured = sandbox.path("artifacts") / "objects"
    configured.mkdir()
    monkeypatch.setenv("POTBOT_SYN_ARTIFACT_ROOT", str(configured))
    before = sorted(str(path.relative_to(sandbox.root)) for path in sandbox.root.rglob("*"))
    owner = metadata.OperationDiscoveryMetadata(RootDatabase(sandbox), ACTOR)._store()
    assert owner.backend.root == configured
    assert sorted(str(path.relative_to(sandbox.root)) for path in sandbox.root.rglob("*")) == before


@pytest.mark.parametrize(
    "choice", ["outside", "relative", "missing", "symlink", "traversal", "empty", "file"]
)
def test_invalid_syn_artifact_override_never_falls_back_or_creates_directories(
    tmp_path, monkeypatch, choice
) -> None:
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    outside = tmp_path / "outside"
    outside.mkdir()
    boundary = sandbox.path("artifacts")
    link = boundary / "redirected"
    link.symlink_to(outside, target_is_directory=True)
    plain = boundary / "file"
    plain.write_text("not a directory")
    selected = {
        "outside": str(outside),
        "relative": "artifacts/objects",
        "missing": str(boundary / "missing"),
        "symlink": str(link),
        "traversal": str(boundary / ".." / "artifacts"),
        "empty": "",
        "file": str(plain),
    }[choice]
    monkeypatch.setenv("POTBOT_SYN_ARTIFACT_ROOT", selected)
    before = sorted(path.name for path in boundary.iterdir())
    reader = metadata.OperationDiscoveryMetadata(RootDatabase(sandbox), ACTOR)
    with pytest.raises(ScopeDeniedError, match="artifact owner is unavailable"):
        reader._store()
    assert sorted(path.name for path in boundary.iterdir()) == before
    assert not (boundary / "missing").exists()


def test_missing_override_uses_only_the_registered_artifact_root(tmp_path, monkeypatch) -> None:
    sandbox = WorkspaceSandbox.prepare(tmp_path / "workspace")
    monkeypatch.delenv("POTBOT_SYN_ARTIFACT_ROOT", raising=False)
    owner = metadata.OperationDiscoveryMetadata(RootDatabase(sandbox), ACTOR)._store()
    assert owner.backend.root == sandbox.path("artifacts")


def test_existing_artifact_backend_can_disable_root_creation_without_changing_default(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "not-created"
    with pytest.raises(ArtifactIntegrityError):
        LocalArtifactBackend(missing, create_root=False)
    assert not missing.exists()
    created = LocalArtifactBackend(missing)
    assert created.root.is_dir()
    assert LocalArtifactBackend(missing, create_root=False).root == created.root
    with pytest.raises(InvalidInputError, match="boolean"):
        LocalArtifactBackend(tmp_path / "bad", create_root=0)  # type: ignore[arg-type]
