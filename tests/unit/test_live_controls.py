"""Live configuration boundaries, reviewed identities and real owned processes."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest

from potbot.controller.node_controls import authenticate_intent
from potbot.desktop_live_settings import ConfigurationDraft, project_live
from potbot.desktop_settings_client import NativeSettingsError
from potbot.desktop_settings_state import RevocationJournal
from potbot.domain.configuration import resolve_configuration
from potbot.domain.errors import InvalidInputError, ScopeDeniedError, StateMismatchError
from potbot.domain.live_controls import EDITABLE, SCHEMA, configuration_review, node_review, proof
from potbot.domain.serialization import content_hash
from potbot.tools.node_supervisor import WorkerProcess, worker_environment

WORKSPACE, PRINCIPAL, NODE, INCARNATION = (str(uuid4()) for _ in range(4))


def config(values=None, revision=0):
    values = values or {}
    effective = resolve_configuration({"workspace": values}).settings
    return {
        "schema": SCHEMA,
        "workspaceId": WORKSPACE,
        "principalId": PRINCIPAL,
        "revision": revision,
        "values": values,
        "sha256": content_hash(values),
        "requestId": str(uuid4()) if revision else None,
        "scope": "workspace",
        "editableFields": list(EDITABLE),
        "apply": "next_admission",
        "canEdit": True,
        "effective": {key: effective[key] for key in EDITABLE},
        "managedElsewhere": ["secret_refs", "source_access"],
    }


def node_change(**updates):
    return {
        "requestId": str(uuid4()),
        "nodeId": NODE,
        "expectedGeneration": 1,
        "expectedRevision": 2,
        "expectedIncarnation": INCARNATION,
        "action": "restart",
        "confirm": True,
        **updates,
    }


@pytest.mark.parametrize(
    "values",
    [
        {"secret_refs": {"x": "secret://providers/x"}},
        {"source_access": ["/private/source"]},
        {"max_packet_tokens": True},
        {"max_packet_tokens": 0},
        {"allowed_tools": ["../../private"]},
        {"reasoning_models": ["/private/model"]},
        {"coding_models": ["x", "x"]},
    ],
)
def test_configuration_edits_keep_native_security_owners_and_types(values):
    review = {
        "requestId": str(uuid4()),
        "expectedRevision": 0,
        "expectedSha256": content_hash({}),
        "values": values,
        "confirm": True,
    }
    with pytest.raises(InvalidInputError):
        configuration_review(review)


def test_configuration_projection_is_scoped_and_drops_private_fields():
    data = config({"max_packet_tokens": 4096}, 1)
    data["nativeDsn"] = "PRIVATE-ADMIN-DSN"
    result = project_live("configuration", data, WORKSPACE, PRINCIPAL)
    assert result["values"] == {"max_packet_tokens": 4096}
    assert "PRIVATE" not in json.dumps(result)
    with pytest.raises(NativeSettingsError, match="authorization"):
        project_live("configuration", data, str(uuid4()), PRINCIPAL)
    data["sha256"] = "0" * 64
    with pytest.raises(NativeSettingsError, match="invalid scoped"):
        project_live("configuration", data, WORKSPACE, PRINCIPAL)


def test_configuration_rebase_preserves_other_edits_and_inheritance():
    draft = ConfigurationDraft(config({"max_packet_tokens": 2048}, 1))
    draft.draft = {}  # Explicitly restore inheritance, not the old value.
    latest = config({"max_packet_tokens": 4096, "coding_models": ["fixture/model"]}, 2)
    draft.observe(latest)
    assert draft.rebase() == ["max_packet_tokens"]
    assert draft.draft == {"coding_models": ["fixture/model"]}
    assert draft.base["revision"] == 2


@pytest.mark.parametrize(
    "update",
    [
        {"expectedGeneration": True},
        {"expectedRevision": 0},
        {"expectedIncarnation": "bad"},
        {"action": "reboot"},
        {"action": []},
        {"confirm": False},
        {"command": ["shutdown"]},
    ],
)
def test_node_review_cannot_select_commands_or_relax_exact_bindings(update):
    with pytest.raises(InvalidInputError):
        node_review(node_change(**update))


def test_native_control_authentication_rejects_forged_or_changed_journal_rows():
    from uuid import UUID

    workspace, principal = UUID(WORKSPACE), UUID(PRINCIPAL)
    review = node_change()
    signed = proof(workspace, principal, review, "native-setup-key")
    row = {
        "principal_id": principal,
        "idempotency_key": review["requestId"],
        "result": {
            "workspaceId": WORKSPACE,
            "principalId": PRINCIPAL,
            "review": review,
            "proof": signed,
        },
    }
    assert authenticate_intent(row, workspace, principal, "native-setup-key") == review
    forged = copy.deepcopy(row)
    forged["result"]["review"]["action"] = "drain"
    with pytest.raises(ScopeDeniedError):
        authenticate_intent(forged, workspace, principal, "native-setup-key")
    with pytest.raises(ScopeDeniedError):
        authenticate_intent(row, workspace, principal, "rotated-key")


def test_live_review_journal_recovers_original_and_can_retain_a_server_identity(tmp_path):
    journal = RevocationJournal(tmp_path / "private" / "live")
    original = node_change()
    first = journal.store(WORKSPACE, PRINCIPAL, original, validator=node_review)
    retry = {**original, "requestId": str(uuid4())}
    assert journal.store(WORKSPACE, PRINCIPAL, retry, validator=node_review) == first
    assert journal.store(WORKSPACE, PRINCIPAL, retry, validator=node_review, specific=True) == retry
    assert (
        journal.store(WORKSPACE, str(uuid4()), retry, validator=node_review)["requestId"]
        == retry["requestId"]
    )


def test_native_worker_environment_does_not_pass_admin_or_control_secrets():
    result = worker_environment(
        {
            "PATH": "fixture-path",
            "POTBOT_ADMIN_DSN": "private-admin",
            "POTBOT_SETUP_TOKEN": "private-setup",
            "COPIED_SECRET": "private-admin",
            "OPENAI_API_KEY": "existing-provider",
        },
        "restricted-app",
        "private-admin",
        "private-setup",
    )
    assert result == {
        "PATH": "fixture-path",
        "POTBOT_DSN": "restricted-app",
        "OPENAI_API_KEY": "existing-provider",
    }


def test_real_owned_worker_requires_a_startup_frame_and_confirms_exit():
    worker_id = "fixture-worker"
    code = "import json,time;print(json.dumps({'data':{'worker_id':'fixture-worker','status':'listening'}}),flush=True);time.sleep(60)"
    child = WorkerProcess([sys.executable, "-c", code], worker_id, env=dict(os.environ))
    try:
        child.wait_ready()
        assert child.running()
        process_id = child.process.pid
    finally:
        child.stop()
    assert child.process.pid == process_id and child.process.poll() is not None


def test_real_failed_startup_is_never_reported_ready():
    child = WorkerProcess(
        [sys.executable, "-c", "print('not a readiness frame')"],
        "fixture-worker",
        env=dict(os.environ),
    )
    with pytest.raises(StateMismatchError):
        child.wait_ready()
    assert child.process.poll() is not None


def test_new_native_module_imports_without_qt_and_cli_help_uses_no_credentials():
    code = """
import importlib.abc,sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self,name,*args):
        if name.split('.')[0] in {'PySide6','fastapi','starlette','pydantic'}:
            raise ImportError('Optional GUI/web runtime unavailable')
sys.meta_path.insert(0,NoQt())
import potbot.desktop_live_settings
from potbot.desktop_settings_client import project_preferences
assert project_preferences({'revision':1,'settings':{'theme':'dark'}})['settings']['theme']=='dark'
assert not any(name.split('.')[0] in {'PySide6','fastapi','starlette','pydantic'} for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], capture_output=True, check=True, timeout=10)
    output = subprocess.run(
        [sys.executable, "-m", "potbot.tools.node_supervisor", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert "--node-id" in output.stdout and "--confirm" in output.stdout


@pytest.mark.parametrize(
    "lineage,shipped_digest",
    [
        ("main", "036d853f162e17a82ecdd00ab8bcb19cd7436efbc18fb0a9a50678b656f0ad25"),
        ("phase3-preview", "1eb015d7e9c12b826e75e6e1c74ef484b8281fc003801e5b8c0466d42162006d"),
    ],
)
def test_live_migration_preserves_every_shipped_checksum_in_both_lineages(lineage, shipped_digest):
    from potbot.persistence.schema import application_migrations

    migrations = application_migrations(lineage=lineage)
    assert [item.version for item in migrations] == list(range(1, 60))
    assert migrations[-1].name == "live_controls_schema"
    # Digests taken from main 0956d071's complete shipped definitions. A new
    # feature must not rewrite a migration which an operator already applied.
    assert content_hash([item.checksum for item in migrations[:-1]]) == shipped_digest
