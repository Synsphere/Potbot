"""Standard-library guest client for the dedicated context-RPC worker profile.

Image path: /opt/potbot/context_client.py. No Potbot/psycopg imports, credentials,
network access or writable root files. Reserve stdout for protocol; log to stderr.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import struct
import sys
import threading
from typing import Any, BinaryIO
from uuid import UUID, uuid4

SCHEMA = "potbot-context-rpc/v1"
MAX_FRAME_BYTES = 2 * 1024 * 1024


class ContextRPCError(RuntimeError):
    def __init__(self, code: str, request_id: str) -> None:
        self.code, self.request_id = code, request_id
        super().__init__(f"Context operation failed ({code}); request {request_id}")


def _exact(stream: BinaryIO, length: int) -> bytes:
    parts = bytearray()
    while len(parts) < length:
        chunk = stream.read(length - len(parts))
        if not chunk:
            raise EOFError("Context channel closed")
        parts.extend(chunk)
    return bytes(parts)


class ContextClient:
    def __init__(self, reader: BinaryIO | None = None, writer: BinaryIO | None = None) -> None:
        self.reader = reader if reader is not None else sys.stdin.buffer
        self.writer = writer if writer is not None else sys.stdout.buffer
        self._lock = threading.Lock()

    def _exchange(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request["request_id"]
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError("Use a canonical request UUID")
        raw = json.dumps(
            request, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if not 1 <= len(raw) <= MAX_FRAME_BYTES:
            raise ValueError("Context request exceeds frame allowance")
        with self._lock:
            try:
                self.writer.write(struct.pack("!I", len(raw)) + raw)
                self.writer.flush()
                size = struct.unpack("!I", _exact(self.reader, 4))[0]
                if not 1 <= size <= MAX_FRAME_BYTES:
                    raise ValueError("Invalid context response frame")
                reply = json.loads(_exact(self.reader, size).decode("utf-8"))
                if (
                    not isinstance(reply, dict)
                    or reply.get("schema") != SCHEMA
                    or type(reply.get("ok")) is not bool
                    or not (
                        reply.get("request_id") == request_id
                        or (reply["ok"] is False and reply.get("request_id") is None)
                    )
                ):
                    raise ValueError("Invalid context response")
            except (OSError, EOFError, UnicodeError, ValueError, RecursionError) as exc:
                # Never auto-retry a lost response with a different UUID.
                raise ContextRPCError("indeterminate", request_id) from exc
        if reply["ok"] is not True:
            error = reply.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or len(code) > 64:
                code = "internal_error"
            raise ContextRPCError(code, request_id)
        result = reply.get("result")
        if not isinstance(result, dict) or (
            request["method"] not in {"skills.list", "objects.list"}
            and type(result.get("revision")) is not int
        ):
            raise ContextRPCError("invalid_response", request_id)
        if "content" in result and (
            not isinstance(result["content"], str)
            or hashlib.sha256(result["content"].encode("utf-8")).hexdigest()
            != result.get("content_sha256", result.get("sha256"))
        ):
            raise ContextRPCError("integrity_error", request_id)
        if request["method"] in {"skills.load", "skills.read"}:
            for field in ("skill_id", "release_sha256"):
                if field in request and result.get(field) != request[field]:
                    raise ContextRPCError("integrity_error", request_id)
        if request["method"] == "skills.load" and "manifest" in result:
            try:
                manifest = result["manifest"]
                manifest_raw = json.dumps(
                    manifest,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("schema") != "potbot-skill-package/v1"
                    or manifest.get("content_sha256") != result.get("content_sha256")
                    or hashlib.sha256(manifest_raw).hexdigest() != result["release_sha256"]
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
                raise ContextRPCError("integrity_error", request_id) from None
        if request["method"] == "skills.read":
            self._verify_resource(request, result)
        if "context" in result:
            documents = result["context"]
            if not isinstance(documents, dict) or set(documents) != {
                "AGENTS.md",
                "MODEL.md",
                "MEMORY.md",
            }:
                raise ContextRPCError("invalid_response", request_id)
            for doc in documents.values():
                if (
                    not isinstance(doc, dict)
                    or not isinstance(doc.get("content"), str)
                    or hashlib.sha256(doc["content"].encode("utf-8")).hexdigest()
                    != doc.get("sha256")
                ):
                    raise ContextRPCError("integrity_error", request_id)
        return result

    @staticmethod
    def _verify_resource(request: dict[str, Any], result: dict[str, Any]) -> None:
        """A range stays bound to its selected package, path and byte offset."""
        try:
            data = base64.b64decode(result["data"], validate=True)
            size, offset, next_offset = result["size"], result["offset"], result["next_offset"]
            digest = result["resource_sha256"]
            if (
                result["encoding"] != "base64"
                or result["resource_path"] != request["resource_path"]
                or type(size) is not int
                or size < 0
                or type(offset) is not int
                or offset != request["offset"]
                or not 0 <= offset <= size
                or type(next_offset) is not int
                or next_offset != offset + len(data)
                or next_offset > size
                or len(data) != min(request["max_bytes"], size - offset)
                or type(result["eof"]) is not bool
                or result["eof"] != (next_offset == size)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
                or hashlib.sha256(data).hexdigest() != result["range_sha256"]
                or (
                    offset == 0
                    and next_offset == size
                    and hashlib.sha256(data).hexdigest() != digest
                )
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise ContextRPCError("integrity_error", request["request_id"]) from None

    def read(
        self,
        scope: str,
        role: str = "MODEL.md",
        *,
        skill_id: str | None = None,
        object_id: str | None = None,
    ) -> dict[str, Any]:
        request = {
            "schema": SCHEMA,
            "request_id": str(uuid4()),
            "method": "context.read",
            "scope": scope,
            "role": role,
        }
        if skill_id is not None:
            request["skill_id"] = skill_id
        if object_id is not None:
            request["object_id"] = object_id
        return self._exchange(request)

    def update(
        self,
        scope: str,
        role: str,
        content: str,
        *,
        expected_revision: int,
        request_id: str,
        reason: str = "",
        skill_id: str | None = None,
        object_id: str | None = None,
    ) -> dict[str, Any]:
        """Retain request_id BEFORE sending; reuse only for the identical retry."""
        request = {
            "schema": SCHEMA,
            "request_id": request_id,
            "method": "context.update",
            "scope": scope,
            "role": role,
            "content": content,
            "expected_revision": expected_revision,
            "reason": reason,
        }
        if skill_id is not None:
            request["skill_id"] = skill_id
        if object_id is not None:
            request["object_id"] = object_id
        return self._exchange(request)

    def create_skill(
        self, name: str, content: str, *, request_id: str, reason: str = ""
    ) -> dict[str, Any]:
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": request_id,
                "method": "skills.create",
                "name": name,
                "content": content,
                "reason": reason,
            }
        )

    def save_skill(
        self,
        skill_id: str,
        content: str,
        *,
        expected_revision: int,
        request_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": request_id,
                "method": "skills.save",
                "skill_id": skill_id,
                "content": content,
                "expected_revision": expected_revision,
                "reason": reason,
            }
        )

    def publish_skill(
        self, skill_id: str, *, expected_revision: int, request_id: str, reason: str = ""
    ) -> dict[str, Any]:
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": request_id,
                "method": "skills.publish",
                "skill_id": skill_id,
                "expected_revision": expected_revision,
                "reason": reason,
            }
        )

    def archive_skill(
        self, skill_id: str, *, expected_revision: int, request_id: str, reason: str = ""
    ) -> dict[str, Any]:
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": request_id,
                "method": "skills.archive",
                "skill_id": skill_id,
                "expected_revision": expected_revision,
                "reason": reason,
            }
        )

    def list_skills(self, *, limit: int = 25, after: str | None = None) -> dict[str, Any]:
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": str(uuid4()),
                "method": "skills.list",
                "limit": limit,
                "after": after,
            }
        )

    def load_skill(self, skill_id: str, release_sha256: str) -> dict[str, Any]:
        """Load selected text, not execute it or promote it to system policy."""
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": str(uuid4()),
                "method": "skills.load",
                "skill_id": skill_id,
                "release_sha256": release_sha256,
            }
        )

    def read_skill_resource(
        self,
        skill_id: str,
        release_sha256: str,
        resource_path: str,
        *,
        offset: int = 0,
        max_bytes: int = 65_536,
    ) -> dict[str, Any]:
        """Read one exact package resource as data; never execute package content."""
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": str(uuid4()),
                "method": "skills.read",
                "skill_id": skill_id,
                "release_sha256": release_sha256,
                "resource_path": resource_path,
                "offset": offset,
                "max_bytes": max_bytes,
            }
        )

    def list_objects(self, *, limit: int = 25, after: str | None = None) -> dict[str, Any]:
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": str(uuid4()),
                "method": "objects.list",
                "limit": limit,
                "after": after,
            }
        )

    def work_input(self) -> dict[str, Any]:
        """Only available in an installed context-capable typed Cog worker."""
        result = self._exchange(
            {"schema": SCHEMA, "request_id": str(uuid4()), "method": "work.input"}
        )
        work = result.get("work")
        if not isinstance(work, dict):
            raise ContextRPCError("invalid_response", "work.input")
        return work

    def finish_work(self, type_ref: str, value: Any, *, request_id: str) -> dict[str, Any]:
        """Persist a final result; do not mutate context after this succeeds."""
        return self._exchange(
            {
                "schema": SCHEMA,
                "request_id": request_id,
                "method": "work.result",
                "result": {"schema_version": 1, "type_ref": type_ref, "value": value},
            }
        )
