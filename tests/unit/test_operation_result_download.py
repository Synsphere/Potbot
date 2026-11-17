"""A download publishes only a complete, same-owner, digest-verified artifact."""

from __future__ import annotations

import base64
import hashlib
import os
from uuid import uuid4

import pytest

from potbot.api.operation_result_download import MAX_DOWNLOAD_BYTES, RANGE_BYTES, download_result
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, StateMismatchError


class Reader:
    def __init__(self, content, transform=None):
        self.content, self.transform = content, transform
        self.actor = WorkspaceContext(uuid4(), uuid4())
        self.operation, self.job, self.artifact_id = "a" * 64, uuid4(), uuid4()
        self.calls = []

    def artifact(self, actor, operation, job, *, offset, length):
        assert (actor, operation, job) == (self.actor, self.operation, self.job)
        assert length == RANGE_BYTES
        self.calls.append(offset)
        data = self.content[offset : offset + length]
        page = {
            "schema": "potbot-operation-workbench/v1",
            "workspaceId": str(actor.workspace_id),
            "principalId": str(actor.principal_id),
            "operationId": operation,
            "artifact": {
                "id": str(self.artifact_id),
                "sha256": hashlib.sha256(self.content).hexdigest(),
                "bytes": len(self.content),
                "mediaType": "application/json",
            },
            "offset": offset,
            "dataBase64": base64.b64encode(data).decode(),
            "nextOffset": offset + len(data) if offset + len(data) < len(self.content) else None,
        }
        if self.transform:
            self.transform(page)
        return page

    def download(self, path):
        return download_result(self, self.actor, self.operation, self.job, path)


@pytest.mark.parametrize("content", (b"", b"hello", b"a" * (RANGE_BYTES * 2 + 7)))
def test_complete_integrity_verified_download(content, tmp_path):
    reader = Reader(content)
    output = tmp_path / "result.json"
    result = reader.download(output)
    assert output.read_bytes() == content
    assert result == {
        "artifactId": str(reader.artifact_id),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "path": str(output),
    }
    assert len(reader.calls) == max(1, (len(content) + RANGE_BYTES - 1) // RANGE_BYTES)
    assert list(tmp_path.iterdir()) == [output]
    if os.name == "posix":
        assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("field", ("schema", "workspaceId", "principalId", "operationId", "offset"))
def test_wrong_scope_or_cursor_does_not_publish(field, tmp_path):
    reader = Reader(b"abc", lambda page: page.update({field: "foreign"}))
    output = tmp_path / "result.json"
    with pytest.raises(StateMismatchError):
        reader.download(output)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "update",
    (
        {"bytes": MAX_DOWNLOAD_BYTES + 1},
        {"bytes": True},
        {"bytes": -1},
        {"id": "not-a-uuid"},
        {"sha256": "A" * 64},
        {"mediaType": None},
        {"storage_locator": "private-path"},
    ),
)
def test_invalid_metadata_does_not_publish(update, tmp_path):
    reader = Reader(b"abc", lambda page: page["artifact"].update(update))
    with pytest.raises(StateMismatchError):
        reader.download(tmp_path / "result.json")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("data", ("%%%", "YQ==", base64.b64encode(b"bad").decode()))
def test_malformed_short_or_corrupted_range_does_not_publish(data, tmp_path):
    reader = Reader(b"abc", lambda page: page.update(dataBase64=data))
    with pytest.raises(StateMismatchError):
        reader.download(tmp_path / "result.json")
    assert not list(tmp_path.iterdir())


def test_changed_artifact_between_ranges_does_not_publish(tmp_path):
    def changed(page):
        if page["offset"]:
            page["artifact"]["id"] = str(uuid4())

    reader = Reader(b"a" * (RANGE_BYTES + 1), changed)
    with pytest.raises(StateMismatchError):
        reader.download(tmp_path / "result.json")
    assert reader.calls == [0, RANGE_BYTES]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cursor", (0, 2, True, "3"))
def test_inconsistent_range_cursor_does_not_publish(cursor, tmp_path):
    reader = Reader(b"abc", lambda page: page.update(nextOffset=cursor))
    with pytest.raises(StateMismatchError):
        reader.download(tmp_path / "result.json")
    assert not list(tmp_path.iterdir())


def test_existing_file_is_preserved_without_reading(tmp_path):
    output = tmp_path / "result.json"
    output.write_bytes(b"existing")
    reader = Reader(b"new")
    with pytest.raises(InvalidInputError):
        reader.download(output)
    assert output.read_bytes() == b"existing" and not reader.calls


def test_file_created_during_download_is_preserved(tmp_path):
    output = tmp_path / "result.json"

    def race(_page):
        output.write_bytes(b"created-by-another-process")

    reader = Reader(b"new", race)
    with pytest.raises(FileExistsError):
        reader.download(output)
    assert output.read_bytes() == b"created-by-another-process"
    assert list(tmp_path.iterdir()) == [output]
