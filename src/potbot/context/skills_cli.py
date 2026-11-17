"""Import, review and manage portable skill releases through the existing owners."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from potbot.api.jobs_cli import add_scope_arguments

_MUTATIONS = frozenset(
    {
        "import-zip",
        "import-directory",
        "import-github",
        "install",
        "activate",
        "disable",
        "rollback",
        "revoke",
    }
)


def download_skill_resource(
    service: Any,
    context: Any,
    identity: UUID,
    path: str,
    output: Path,
    *,
    release_sha256: str | None = None,
) -> dict[str, Any]:
    """Download a fully verified resource; never overwrite an existing local file."""
    from potbot.context.skill_packages import MAX_FILE_BYTES, validate_resource_path
    from potbot.domain.errors import InvalidInputError

    validate_resource_path(path)
    result = bytearray()
    expected: dict[str, Any] | None = None
    while True:
        if release_sha256 is None:
            value = service.package_resource(
                context, identity, path, offset=len(result), length=65536
            )
        else:
            value = service.resource(
                context, identity, release_sha256, path, offset=len(result), length=65536
            )
        resource = value.get("resource")
        if (
            not isinstance(resource, dict)
            or resource.get("path") != path
            or type(resource.get("size")) is not int
            or not 0 <= resource["size"] <= MAX_FILE_BYTES
            or not isinstance(resource.get("sha256"), str)
            or len(resource["sha256"]) != 64
            or value.get("workspaceId") != str(context.workspace_id)
            or value.get("principalId") != str(context.principal_id)
            or type(value.get("offset")) is not int
            or value["offset"] != len(result)
            or not isinstance(value.get("dataBase64"), str)
        ):
            raise InvalidInputError("Skill resource download binding is inconsistent")
        if expected is not None and resource != expected:
            raise InvalidInputError("Skill resource changed while downloading")
        expected = resource
        try:
            chunk = base64.b64decode(value["dataBase64"], validate=True)
        except ValueError:
            raise InvalidInputError("Invalid skill resource encoding") from None
        if len(chunk) != min(65536, resource["size"] - len(result)):
            raise InvalidInputError("Skill resource response exceeds its range")
        result.extend(chunk)
        next_offset = value.get("nextOffset")
        if next_offset is None:
            if (
                len(result) != resource["size"]
                or hashlib.sha256(result).hexdigest() != resource["sha256"]
            ):
                raise InvalidInputError(
                    "Skill resource download failed complete integrity verification"
                )
            break
        if type(next_offset) is not int or next_offset != len(result) or not chunk:
            raise InvalidInputError("Skill resource download did not make bounded progress")
    target = output.absolute()
    # Bytes are completely verified before any destination is opened. Exclusive
    # creation also rejects existing files and symlink destinations.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(result)
            stream.flush()
        os.fsync(fd)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        os.close(fd)
    return {"path": str(target), "sha256": expected["sha256"], "bytes": len(result)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="potbot skills", description=__doc__)
    add_scope_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "packages"):
        command = commands.add_parser(
            name,
            help="list installed skills" if name == "list" else "list private imported packages",
        )
        command.add_argument("--limit", type=int, default=25)
        command.add_argument("--after")
    for name in ("import-zip", "import-directory"):
        command = commands.add_parser(
            name, help="acquire immutable data for review; does not install or execute"
        )
        command.add_argument("source", type=Path)
        command.add_argument("--key", required=True, help="reuse after a lost response")
        if name == "import-zip":
            command.add_argument("--path", default="", help="exact subdirectory inside archive")
    github = commands.add_parser(
        "import-github", help="acquire a public GitHub package at an exact commit"
    )
    github.add_argument("repository", help="canonical https://github.com/owner/repository")
    github.add_argument("--commit", required=True, help="full 40-character commit hash")
    github.add_argument("--path", default="", help="exact skill directory inside repository")
    github.add_argument("--key", required=True)
    package = commands.add_parser(
        "package", help="inspect a private package and bounded SKILL.md preview"
    )
    package.add_argument("package_id", type=UUID)
    plan = commands.add_parser(
        "plan", help="review exact source, resources, requirements and update diff"
    )
    plan.add_argument("package_id", type=UUID)
    plan.add_argument("--skill", type=UUID)
    install = commands.add_parser("install", help="approve a reviewed release; it remains disabled")
    install.add_argument("package_id", type=UUID)
    install.add_argument("--skill", type=UUID)
    install.add_argument("--plan", required=True, help="exact reviewed plan SHA-256")
    install.add_argument(
        "--revision", required=True, type=int, help="reviewed current revision; 0 for a new skill"
    )
    install.add_argument("--key", required=True)
    install.add_argument("--confirm", required=True, action="store_true")
    inspect = commands.add_parser(
        "inspect", help="inspect the recorded skill head and approval validity"
    )
    inspect.add_argument("skill_id", type=UUID)
    history = commands.add_parser(
        "history", help="read immutable lifecycle decisions and retained releases"
    )
    history.add_argument("skill_id", type=UUID)
    history.add_argument("--limit", type=int, default=25)
    history.add_argument("--after")
    for name in ("activate", "disable", "rollback", "revoke"):
        command = commands.add_parser(
            name, help="explicitly change a skill's controlled availability"
        )
        command.add_argument("skill_id", type=UUID)
        command.add_argument("--revision", type=int, required=True)
        command.add_argument("--key", required=True)
        command.add_argument("--confirm", required=True, action="store_true")
        if name == "rollback":
            command.add_argument(
                "--release",
                required=True,
                help="retained release SHA-256; rollback leaves it disabled",
            )
    for name in ("read", "package-read", "download", "package-download"):
        command = commands.add_parser(
            name, help="read an exact resource; downloaded bytes are fully verified"
        )
        command.add_argument(
            "identity", type=UUID, help="package ID" if name.startswith("package-") else "skill ID"
        )
        command.add_argument("--path", required=True, help="exact resource path from manifest")
        if not name.startswith("package-"):
            command.add_argument("--release", required=True)
        if name.endswith("download"):
            command.add_argument("--output", required=True, type=Path)
        else:
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--length", type=int, default=65536)
    args = parser.parse_args(argv)

    # Argument help and unrelated commands need neither PostgreSQL nor YAML or a vault.
    from potbot.api.jobs_cli import database_context
    from potbot.api.wire import OperationSemantics, error_response

    try:
        from potbot.api.operations_cli import _setup_token
        from potbot.context.skill_acquisition import SkillAcquisition
        from potbot.context.skill_packages import (
            bundle_skill_directory,
            fetch_github_skill,
            parse_skill_zip,
            read_skill_archive,
        )

        database, context = database_context(args)
        service = SkillAcquisition(database)
        token = _setup_token() if args.command in _MUTATIONS else None
        if args.command == "list":
            result = service.list(context, limit=args.limit, after=args.after)
        elif args.command == "packages":
            result = service.packages(context, limit=args.limit, after=args.after)
        elif args.command == "import-zip":
            raw = read_skill_archive(args.source)
            source = {
                "kind": "zip",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "subdirectory": args.path,
            }
            result = service.acquire(
                context, source, args.key, lambda: parse_skill_zip(raw, subdirectory=args.path)
            )
        elif args.command == "import-directory":
            raw = bundle_skill_directory(args.source)
            package_value = parse_skill_zip(raw)
            source = {"kind": "directory", "releaseSha256": package_value.release_sha256}
            result = service.acquire(context, source, args.key, lambda: package_value)
        elif args.command == "import-github":
            source = {
                "kind": "github",
                "repository": args.repository,
                "commit": args.commit,
                "subdirectory": args.path,
            }
            result = service.acquire(
                context,
                source,
                args.key,
                lambda: fetch_github_skill(args.repository, args.commit, args.path),
            )
        elif args.command == "package":
            result = service.package(context, args.package_id)
        elif args.command == "plan":
            result = service.plan(context, args.package_id, skill_id=args.skill)
        elif args.command == "install":
            assert token is not None
            result = service.install(
                context,
                args.package_id,
                args.plan,
                args.revision,
                args.key,
                skill_id=args.skill,
                setup_token=token,
            )
        elif args.command == "inspect":
            result = service.inspect(context, args.skill_id)
        elif args.command == "history":
            result = service.history(context, args.skill_id, limit=args.limit, after=args.after)
        elif args.command in {"activate", "disable", "rollback", "revoke"}:
            assert token is not None
            result = service.control(
                context,
                args.skill_id,
                args.command,
                args.revision,
                args.key,
                release_sha256=getattr(args, "release", None),
                setup_token=token,
            )
        elif args.command.endswith("download"):
            result = download_skill_resource(
                service,
                context,
                args.identity,
                args.path,
                args.output,
                release_sha256=getattr(args, "release", None),
            )
        elif args.command == "package-read":
            result = service.package_resource(
                context, args.identity, args.path, offset=args.offset, length=args.length
            )
        else:
            result = service.resource(
                context,
                args.identity,
                args.release,
                args.path,
                offset=args.offset,
                length=args.length,
            )
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except Exception as error:
        response = error_response(
            error,
            semantics=OperationSemantics(
                read_only=args.command not in _MUTATIONS, idempotency_key=args.command in _MUTATIONS
            ),
        )
        print(json.dumps(response.body, sort_keys=True, ensure_ascii=True))
        return 1
