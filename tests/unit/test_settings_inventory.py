"""Real temporary files, static catalogue coverage and no-execution inventory."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from potbot.api.ui_resources import BOOL_PREFERENCES, PREFERENCES
from potbot.domain.configuration import DEFAULTS, LAYERS
from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.domain.settings_catalogue import catalogue, display_value
from potbot.tools import local_inventory
from potbot.tools.local_inventory import InventoryProfile, inspect_local


def profile(tmp_path: Path, **overrides):
    workspace = uuid4()
    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "packages").mkdir(exist_ok=True)
    value = {
        "schema": "potbot-local-inventory/v1",
        "workspace_id": str(workspace),
        "project_root": str(tmp_path),
        "node_label": "Explicit fixture node",
        "model_roots": ["models"],
        "tool_roots": ["packages"],
    } | overrides
    source = tmp_path / "inventory.json"
    source.write_text(json.dumps(value), encoding="utf-8")
    return source, workspace


def test_catalogue_covers_each_existing_setting_without_enabling_layer_writes():
    result = catalogue()
    assert result["layers"] == list(LAYERS)
    assert result["layerEditing"] is False
    configured = {row["id"] for row in result["descriptors"] if not row["editable"]}
    editable = {row["id"] for row in result["descriptors"] if row["editable"]}
    assert configured == {"configuration." + key for key in DEFAULTS}
    assert editable == {
        "preferences." + key for key in set(PREFERENCES) | BOOL_PREFERENCES | {"xpraMaxFps"}
    }
    assert len(result["features"]) == 29
    assert len({row["id"] for row in result["features"]}) == 29
    assert all(
        row["category"] in {c["id"] for c in result["categories"]}
        for row in result["features"] + result["descriptors"]
    )


def test_private_references_and_source_locations_are_counts_not_exported_values():
    secret = "secret://private/account-123"
    assert secret not in display_value("secret_refs", {"private": secret})
    assert "/private/source" not in display_value("source_access", ["/private/source"])
    assert "1 configured" == display_value("secret_refs", {"private": secret})


def test_models_are_stat_only_and_packages_are_headers_only(tmp_path, monkeypatch):
    source, workspace = profile(tmp_path)
    weights = tmp_path / "models" / "unknown.gguf"
    weights.write_bytes(b"PRIVATE WEIGHTS NOT A VERIFIED MODEL")
    (tmp_path / "models" / "unfinished.gguf.part").write_bytes(b"partial")
    (tmp_path / "models" / "never_import.py").write_text("raise RuntimeError('executed')")
    dist = tmp_path / "packages" / "example-1.2.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.3\nName: example\nVersion: 1.2\nSecret: NEVER EXPOSE\n\nBODY PRIVATE"
    )
    package = tmp_path / "packages" / "example"
    package.mkdir()
    (package / "__init__.py").write_text("raise RuntimeError('imported')")
    selected = InventoryProfile.read(source, workspace)

    def refused(*_args, **_kwargs):
        raise AssertionError("Inventory attempted execution, a model read or a connection")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(subprocess, "Popen", refused)
    original_open = os.open

    def metadata_only(path, flags, *args, **kwargs):
        assert Path(path).name == "METADATA"
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", metadata_only)
    models = inspect_local(selected, "models")
    tools = inspect_local(selected, "tools")
    assert models["status"] == tools["status"] == "observed"
    assert len(models["items"]) == 2
    found = next(row for row in models["items"] if row["name"] == "unknown.gguf")
    assert found["bytes"] == weights.stat().st_size
    assert found["verification"] == "not_checked" and found["qualification"] == "unknown"
    assert found["permission"] == "not_evaluated"
    assert (
        next(row for row in models["items"] if row["name"].endswith(".part"))["presence"]
        == "partial"
    )
    assert [(row["name"], row["version"]) for row in tools["items"]] == [("example", "1.2")]
    assert "PRIVATE" not in json.dumps(tools) and "NEVER EXPOSE" not in json.dumps(tools)
    assert str(tmp_path) not in json.dumps(models)
    assert tools["items"][0]["runtime"] == "unknown"


@pytest.mark.parametrize(
    "changes",
    [
        {"model_roots": ["../outside"]},
        {"model_roots": ["/models"]},
        {"model_roots": ["unrelated"]},
        {"tool_roots": ["../private"]},
        {"model_roots": ["models", "models"]},
        {"tool_roots": "packages"},
        {"project_root": "."},
        {"node_label": "bad\nlabel"},
        {"unexpected": "value"},
    ],
)
def test_profile_refuses_arbitrary_locations_and_unknown_fields(tmp_path, changes):
    source, workspace = profile(tmp_path, **changes)
    with pytest.raises(InvalidInputError):
        InventoryProfile.read(source, workspace)


def test_inventory_binding_cannot_be_relabelled_as_another_workspace(tmp_path):
    source, _workspace = profile(tmp_path)
    with pytest.raises(ScopeDeniedError):
        InventoryProfile.read(source, uuid4())


def test_unconfigured_inaccessible_empty_and_bounded_are_distinct(tmp_path, monkeypatch):
    assert inspect_local(None, "models")["status"] == "not_configured"
    source, workspace = profile(tmp_path)
    selected = InventoryProfile.read(source, workspace)
    assert inspect_local(selected, "models")["status"] == "observed"
    (tmp_path / "models").rmdir()
    missing = inspect_local(selected, "models")
    assert missing["status"] == "partial" and missing["unavailableRoots"] == 1
    (tmp_path / "models").mkdir()
    for index in range(4):
        (tmp_path / "models" / f"{index}.gguf").write_bytes(b"x")
    monkeypatch.setattr(local_inventory, "MAX_ENTRIES", 2)
    bounded = inspect_local(selected, "models")
    assert bounded["truncated"] and len(bounded["items"]) <= 2
    assert bounded["status"] == "partial"


def test_model_or_metadata_symlinks_never_escape_selected_roots(tmp_path):
    source, workspace = profile(tmp_path)
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret.gguf").write_bytes(b"private")
    try:
        (tmp_path / "models" / "escaped").symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name == "nt":
            pytest.skip("Windows host does not permit creating symlink fixtures")
        raise
    assert inspect_local(InventoryProfile.read(source, workspace), "models")["items"] == []


def test_tool_roots_do_not_recursively_scan_package_code_or_model_stores(tmp_path, monkeypatch):
    source, workspace = profile(tmp_path)
    nested = tmp_path / "packages" / "example"
    nested.mkdir()
    selected = InventoryProfile.read(source, workspace)
    original = os.scandir

    def no_package_walk(path):
        assert Path(path) != nested
        return original(path)

    monkeypatch.setattr(os, "scandir", no_package_walk)
    assert inspect_local(selected, "tools")["status"] == "observed"


def test_bad_metadata_does_not_export_arbitrary_file_body(tmp_path):
    source, workspace = profile(tmp_path)
    dist = tmp_path / "packages" / "bad.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: bad\nName: another\nVersion: secret token\n\nPRIVATE")
    assert inspect_local(InventoryProfile.read(source, workspace), "tools")["items"] == []


def test_added_computational_option_cannot_silently_miss_the_descriptor_registry(monkeypatch):
    monkeypatch.setitem(DEFAULTS, "new_unreviewed_option", True)
    with pytest.raises(RuntimeError, match="descriptor"):
        catalogue()


def test_overlapping_roots_do_not_duplicate_the_same_file(tmp_path):
    source, workspace = profile(tmp_path, model_roots=["models", "models/nested"])
    nested = tmp_path / "models" / "nested"
    nested.mkdir()
    (nested / "same.gguf").write_bytes(b"fixture")
    found = inspect_local(InventoryProfile.read(source, workspace), "models")
    assert len(found["items"]) == 1


def test_inventory_cli_uses_the_explicit_profile_without_administrative_credentials(
    tmp_path, monkeypatch, capsys
):
    from potbot import credentials
    from potbot.api.remote_workspaces_cli import main

    source, workspace = profile(tmp_path)

    def unavailable(*_args, **_kwargs):
        raise AssertionError("Native metadata inventory requested database administration")

    monkeypatch.setattr(credentials, "resolve_postgres_dsn", unavailable)
    assert (
        main(
            [
                "inventory",
                "--profile",
                str(source),
                "--workspace-id",
                str(workspace),
                "--kind",
                "models",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["result"]["status"] == "observed"


def test_native_apply_requires_explicit_confirmation_before_opening_database(monkeypatch):
    from potbot import credentials
    from potbot.api.remote_workspaces_cli import main

    def unavailable(*_args, **_kwargs):
        raise AssertionError("Parser opened administration without confirmation")

    monkeypatch.setattr(credentials, "resolve_postgres_dsn", unavailable)
    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "apply-revocation",
                "--workspace-id",
                str(uuid4()),
                "--owner-principal-id",
                str(uuid4()),
                "--request-id",
                str(uuid4()),
            ]
        )
    assert stopped.value.code == 2


def test_crlf_metadata_stops_at_the_header_boundary(tmp_path):
    source, workspace = profile(tmp_path)
    dist = tmp_path / "packages" / "headers.dist-info"
    dist.mkdir()
    metadata = dist / "METADATA"
    metadata.write_bytes(b"Name: valid\r\nVersion: 1.2\r\n\r\nName: PRIVATE-BODY\r\n")
    found = inspect_local(InventoryProfile.read(source, workspace), "tools")
    assert [(item["name"], item["version"]) for item in found["items"]] == [("valid", "1.2")]
    metadata.write_bytes(b"Metadata-Version: 2.3\r\n\r\nName: PRIVATE-BODY\r\nVersion: 1.0\r\n")
    found = inspect_local(InventoryProfile.read(source, workspace), "tools")
    assert found["items"] == [] and found["status"] == "partial"
