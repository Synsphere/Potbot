"""Native draft/recovery state; never a grant authority or an automatic retry."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from potbot.desktop_settings_client import NativeSettingsError, project_preferences
from potbot.domain.serialization import content_hash


def validate_review(value: Any) -> dict[str, Any]:
    try:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "requestId",
                "grantId",
                "expectedRevision",
                "expectedGeneration",
                "grantSha256",
                "confirm",
            }
            or value["confirm"] is not True
        ):
            raise ValueError
        for key in ("requestId", "grantId"):
            identity = UUID(value[key])
            if str(identity) != value[key] or identity.int == 0:
                raise ValueError
        for key in ("expectedRevision", "expectedGeneration"):
            if type(value[key]) is not int or not 1 <= value[key] < 2**53:
                raise ValueError
        if not isinstance(value["grantSha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", value["grantSha256"]
        ):
            raise ValueError
        return dict(value)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise NativeSettingsError("configuration") from None


@dataclass
class PreferenceDraft:
    base: dict[str, Any]
    draft: dict[str, Any] = field(default_factory=dict)
    latest: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.base = project_preferences(self.base)
        self.draft = dict(self.base["settings"])

    @property
    def changed(self) -> list[str]:
        return sorted(
            key
            for key in self.base["settings"].keys() | self.draft.keys()
            if self.base["settings"].get(key) != self.draft.get(key)
        )

    def edit(self, key: str, value: Any) -> None:
        self.draft = project_preferences({"revision": 0, "settings": {**self.draft, key: value}})[
            "settings"
        ]

    def observe(self, saved: dict[str, Any]) -> None:
        saved = project_preferences(saved)
        if self.changed and saved != self.base:
            self.latest = saved
        elif not self.changed:
            self.base = saved
            self.draft = dict(saved["settings"])
            self.latest = None

    def rebase(self) -> list[str]:
        if self.latest is None:
            return []
        changed = self.changed
        conflicts = [
            key
            for key in changed
            if self.base["settings"].get(key) != self.latest["settings"].get(key)
            and self.draft.get(key) != self.latest["settings"].get(key)
        ]
        merged = dict(self.latest["settings"])
        for key in changed:
            if key in self.draft:
                merged[key] = self.draft[key]
            else:
                merged.pop(key, None)
        self.base = self.latest
        self.latest = None
        self.draft = merged
        return conflicts

    def accept(self, saved: dict[str, Any]) -> None:
        self.base = project_preferences(saved)
        self.draft = dict(self.base["settings"])
        self.latest = None


class RevocationJournal:
    """A bounded private immutable review, persisted BEFORE an explicit POST.

    A restart can recover the original identity, including a lost server reply.
    Reading this file never submits a request or claims native completion.
    """

    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            from potbot.dependencies import project_root

            directory = project_root() / ".potbot" / "desktop" / "revocations"
        self.directory = directory

    def prepare(
        self,
        workspace_id: str,
        principal_id: str,
        grant: dict[str, Any],
        requests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            for identity in (workspace_id, principal_id):
                if str(UUID(identity)) != identity or UUID(identity).int == 0:
                    raise ValueError
            pending = next(
                (
                    row["requestId"]
                    for row in requests
                    if row["grantId"] == grant["id"]
                    and row["expectedRevision"] == grant["revision"]
                    and row["state"] == "requested"
                ),
                None,
            )
            body = validate_review(
                {
                    "requestId": pending or str(uuid4()),
                    "grantId": grant["id"],
                    "expectedRevision": grant["revision"],
                    "expectedGeneration": grant["revocationGeneration"],
                    "grantSha256": grant["sha256"],
                    "confirm": True,
                }
            )
            key = content_hash(
                {
                    "workspace": workspace_id,
                    "principal": principal_id,
                    **{k: v for k, v in body.items() if k != "requestId"},
                }
            )
            # Check parents before creating children; a symlink may not redirect writes.
            for path in (self.directory.parent.parent, self.directory.parent, self.directory):
                if not path.exists() and not path.is_symlink():
                    path.mkdir(mode=0o700)
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError
            target = self.directory / (key + ".json")
            if not target.exists():
                with os.scandir(self.directory) as entries:
                    if sum(1 for _, _entry in zip(range(101), entries, strict=False)) >= 100:
                        raise ValueError
                record = {"workspaceId": workspace_id, "principalId": principal_id, "review": body}
                try:
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(json.dumps(record, sort_keys=True).encode())
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    pass
            info = target.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise ValueError
            descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError
            saved = json.loads(raw)
            retained = validate_review(saved["review"])
            if (
                saved["workspaceId"] != workspace_id
                or saved["principalId"] != principal_id
                or any(retained[k] != body[k] for k in body if k != "requestId")
            ):
                raise ValueError
            return retained
        except Exception:
            raise NativeSettingsError("journal") from None
