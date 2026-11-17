"""Verify bounded owned artifact ranges before publishing a local result file."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools.temporary import TemporaryDirectory

MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
RANGE_BYTES = 262144


class ArtifactReader(Protocol):
    def artifact(
        self,
        context: WorkspaceContext,
        identity: str,
        job_id: UUID,
        *,
        offset: int = 0,
        length: int = RANGE_BYTES,
    ) -> dict[str, Any]: ...


def download_result(
    reader: ArtifactReader,
    actor: WorkspaceContext,
    operation_id: str,
    job_id: UUID,
    destination: Path,
) -> dict[str, Any]:
    """One explicit download; an existing file is never replaced."""
    target = destination.absolute()
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise InvalidInputError("Choose a new result filename in an existing directory")
    expected: dict[str, Any] | None = None
    offset, digest = 0, hashlib.sha256()
    with TemporaryDirectory(prefix=".potbot-result-", dir=target.parent) as temporary:
        staged = Path(temporary) / "result"
        with staged.open("xb") as stream:
            os.chmod(staged, 0o600)
            while True:
                page = reader.artifact(
                    actor, operation_id, job_id, offset=offset, length=RANGE_BYTES
                )
                metadata = page.get("artifact")
                if (
                    page.get("schema") != "potbot-operation-workbench/v1"
                    or page.get("workspaceId") != str(actor.workspace_id)
                    or page.get("principalId") != str(actor.principal_id)
                    or page.get("operationId") != operation_id
                    or type(page.get("offset")) is not int
                    or page["offset"] != offset
                    or not isinstance(metadata, dict)
                    or set(metadata) != {"id", "sha256", "bytes", "mediaType"}
                    or type(metadata.get("bytes")) is not int
                    or not 0 <= metadata["bytes"] <= MAX_DOWNLOAD_BYTES
                    or not isinstance(metadata.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])
                    or not isinstance(metadata.get("id"), str)
                    or not isinstance(metadata.get("mediaType"), str)
                    or not 1 <= len(metadata["mediaType"]) <= 256
                    or not isinstance(page.get("dataBase64"), str)
                    or len(page["dataBase64"]) > 4 * ((RANGE_BYTES + 2) // 3)
                ):
                    raise StateMismatchError("Operation result range has an invalid identity")
                try:
                    if str(UUID(metadata["id"])) != metadata["id"]:
                        raise ValueError
                    data = base64.b64decode(page["dataBase64"], validate=True)
                except (ValueError, TypeError, binascii.Error):
                    raise StateMismatchError("Operation result range is malformed") from None
                if expected is None:
                    expected = dict(metadata)
                if metadata != expected or len(data) != min(
                    RANGE_BYTES, expected["bytes"] - offset
                ):
                    raise StateMismatchError("Operation result changed or its range is incomplete")
                next_offset = offset + len(data)
                cursor = page.get("nextOffset")
                if (cursor is not None and type(cursor) is not int) or cursor != (
                    next_offset if next_offset < expected["bytes"] else None
                ):
                    raise StateMismatchError("Operation result range cursor is inconsistent")
                stream.write(data)
                digest.update(data)
                offset = next_offset
                if page["nextOffset"] is None:
                    break
            if offset != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
                raise StateMismatchError("Operation result failed its final integrity check")
            stream.flush()
            os.fsync(stream.fileno())
        # Both names are in the same directory tree. Linking is atomic and
        # refuses an existing destination, including one created mid-download.
        os.link(staged, target)
    return {
        "artifactId": expected["id"],
        "sha256": expected["sha256"],
        "bytes": expected["bytes"],
        "path": str(target),
    }
