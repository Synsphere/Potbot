"""Operator recipe confinement and deterministic reviewed build identities."""

from __future__ import annotations

import json
import os
from uuid import UUID, uuid4

import pytest

from potbot.domain.errors import InvalidInputError, ScopeDeniedError
from potbot.tools.tool_acquisition import ToolRecipe, load_tool_recipe
from potbot.tools.tool_stage_s10 import command_digest


def recipe_document(*, slug="fixture-tool"):
    return {
        "schema": "potbot-tool-acquisition-recipe/v1",
        "slug": slug,
        "name": "Fixture executable",
        "description": "Compile an exact offline source using the qualified rootfs",
        "repository": "https://github.com/fixture/tool.git",
        "build_argv": ["/usr/bin/python3", "-I", "-B", "{checkout}/build.py", "{tool}/static/tool"],
        "timeout_ms": 30000,
        "cleanup_paths": ["build-cache"],
        "lockfile_paths": ["requirements.lock"],
        "artifact_paths": ["static/tool"],
        "tests": [
            {
                "name": name,
                "kind": name,
                "argv": ["/usr/bin/python3", "{checkout}/" + name + ".py"],
                "timeout_ms": 1000,
            }
            for name in ("smoke", "unit")
        ],
    }


def test_reviewed_command_is_exact_per_operation_and_independent_of_template():
    recipe = ToolRecipe.from_dict(recipe_document())
    operation = UUID("00000000-0000-4000-8000-000000000001")
    rendered = recipe.render(operation_id=operation, agent_directory="agent-one")
    root = "/home/agent-one/builds/00000000000040008000000000000001"
    assert rendered == {
        "agent_directory": "agent-one",
        "build_root": root,
        "build_argv": [
            "/usr/bin/python3",
            "-I",
            "-B",
            root + "/checkout/build.py",
            root + "/static/tool",
        ],
        "command_sha256": command_digest(
            ("/usr/bin/python3", "-I", "-B", root + "/checkout/build.py", root + "/static/tool")
        ),
    }
    assert rendered["command_sha256"] != command_digest(recipe.build_argv)
    assert (
        recipe.render(operation_id=uuid4(), agent_directory="agent-one")["command_sha256"]
        != rendered["command_sha256"]
    )
    assert ToolRecipe.from_dict(recipe.to_dict()).digest == recipe.digest


@pytest.mark.parametrize(
    "argv",
    [
        ["python3", "build.py"],
        ["/home/agent/compiler", "build.py"],
        ["/usr/local/tools/compiler/static/compiler", "build.py"],
        ["{checkout}/build.py"],
        ["/usr/bin/python3", "{outside}/build.py"],
        ["/usr/bin/python3", "{checkout/build.py"],
        ["/usr/bin/python3", "build.py\n--network"],
        ["/usr/bin/python3", "x" * 4097],
    ],
)
def test_unbounded_or_mutable_builder_identity_is_not_publishable(argv):
    document = recipe_document()
    document["build_argv"] = argv
    with pytest.raises(InvalidInputError):
        ToolRecipe.from_dict(document)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "https://github.com/fixture/tool.git?token=private"),
        ("repository", "https://user:private@github.com/fixture/tool.git"),
        ("cleanup_paths", ["../outside"]),
        ("cleanup_paths", ["build-cache", "build-cache/nested"]),
        ("artifact_paths", ["checkout/run.py"]),
        ("artifact_paths", ["static/../../escape"]),
        ("description", "\ud800"),
        ("timeout_ms", True),
    ],
)
def test_source_paths_and_recipe_data_cannot_change_authority(field, value):
    document = recipe_document()
    document[field] = value
    with pytest.raises((InvalidInputError, ScopeDeniedError)):
        ToolRecipe.from_dict(document)


def test_real_smoke_and_unit_suite_is_required_before_any_build_can_be_reviewed():
    document = recipe_document()
    document["tests"][1]["kind"] = "smoke"
    with pytest.raises(InvalidInputError):
        ToolRecipe.from_dict(document)
    document["tests"][1]["kind"] = []
    with pytest.raises(InvalidInputError):
        ToolRecipe.from_dict(document)


def test_private_recipe_reader_rejects_links_shared_permissions_and_duplicate_fields(tmp_path):
    path = tmp_path / "operator-recipe.json"
    path.write_text(json.dumps(recipe_document()), encoding="utf-8")
    path.chmod(0o600)
    expected = load_tool_recipe(path)
    assert expected.slug == "fixture-tool"
    path.chmod(0o644)
    with pytest.raises(ScopeDeniedError):
        load_tool_recipe(path)
    path.chmod(0o600)
    linked = tmp_path / "redirected.json"
    linked.symlink_to(path)
    with pytest.raises(ScopeDeniedError):
        load_tool_recipe(linked)
    hard = tmp_path / "alias.json"
    os.link(path, hard)
    with pytest.raises(ScopeDeniedError):
        load_tool_recipe(path)
    hard.unlink()
    path.write_text('{"schema":"first","schema":"second"}', encoding="utf-8")
    with pytest.raises(InvalidInputError):
        load_tool_recipe(path)


def test_recipe_file_accepts_unicode_without_changing_the_canonical_manifest(tmp_path):
    document = recipe_document()
    document["name"] = "🌱" * 120
    document["description"] = "é" * 4096
    recipe = ToolRecipe.from_dict(document)
    path = tmp_path / "unicode.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    assert load_tool_recipe(path).digest == recipe.digest
