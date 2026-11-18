"""One-shot native host orchestration through the existing S9–S12 owners.

The HTTP service only persists decisions. This worker independently loads the
operator recipe, checks the signed claim, revalidates exact source/runtime and
predecessor evidence, and delegates all execution and release to their owners.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from potbot.domain.errors import (
    DomainError,
    InvalidInputError,
    ScopeDeniedError,
    StateMismatchError,
)
from potbot.domain.serialization import content_hash
from potbot.tools.shared_tool_release_markers import read_hold
from potbot.tools.shared_tool_store import SharedToolDefinition
from potbot.tools.shared_tool_upgrades import ToolTestSuite, stage_name
from potbot.tools.tool_acquisition import ToolRecipe, load_tool_recipe
from potbot.tools.tool_stage_s10 import ToolBuildPlan, build_and_stage, command_digest

if TYPE_CHECKING:
    from potbot.domain.auth import WorkspaceContext
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.ai_workspace_operator import AiWorkspaceOperator
    from potbot.tools.shared_tool_upgrades import SharedToolUpgradeManager
    from potbot.tools.tool_acquisition import ToolAcquisitionService
    from potbot.tools.tool_approval_worker_s11 import ToolApprovalWorker
    from potbot.tools.tool_sources import GitToolSourceStore


def recipe_suite(recipe: ToolRecipe) -> ToolTestSuite:
    return recipe.tests


def private_directory(path: Path) -> Path:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_dir()
        or path.resolve(strict=True) != path
        or path.stat().st_mode & 0o077
        or path.stat().st_uid != os.geteuid()
    ):
        raise ScopeDeniedError(
            "Tool controller storage must be an operator-owned physical 0700 directory"
        )
    return path


def retain_recipe(recipe: ToolRecipe, directory: Path) -> Path:
    """Retain an immutable host copy alongside its independently signed publication."""
    private_directory(directory)
    path = directory / (recipe.digest + ".json")
    if path.exists() or path.is_symlink():
        if load_tool_recipe(path).digest != recipe.digest:
            raise StateMismatchError("Retained operator recipe differs from publication")
        return path
    data = json.dumps(
        recipe.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as writer:
        writer.write(data)
        writer.flush()
        os.fsync(writer.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def _artifact_hash(root: Path, relative: str) -> str:
    path = root / relative
    if not path.is_file() or path.is_symlink() or path.resolve(strict=True) != path:
        raise StateMismatchError("Reviewed tool artifact is absent or redirected")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4 * 1024**3:
            raise StateMismatchError("Reviewed tool artifact is outside storage policy")
        value = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, 1024**2):
            size += len(chunk)
            if size > 4 * 1024**3:
                raise StateMismatchError("Reviewed tool artifact exceeds byte allowance")
            value.update(chunk)
        after = os.fstat(fd)
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) or size != before.st_size:
            raise StateMismatchError("Reviewed tool artifact changed during readback")
        return value.hexdigest()
    finally:
        os.close(fd)


class ToolAcquisitionWorker:
    def __init__(
        self,
        *,
        service: ToolAcquisitionService,
        context: WorkspaceContext,
        agent_id: UUID,
        setup_token: str,
        sources: GitToolSourceStore,
        recipe_dir: Path,
        evidence_dir: Path,
        manager: SharedToolUpgradeManager,
        homes: AgentHomeRegistry,
        operator: AiWorkspaceOperator,
        promotion: ToolApprovalWorker,
    ) -> None:
        self.service, self.context, self.agent_id = service, context, agent_id
        self.setup_token, self.sources = setup_token, sources
        self.recipe_dir, self.evidence_dir = (
            private_directory(recipe_dir),
            private_directory(evidence_dir),
        )
        self.manager, self.homes, self.operator, self.promotion = (
            manager,
            homes,
            operator,
            promotion,
        )
        if context.workspace_id != operator.workspace_id or operator.runtime != manager.runtime:
            raise ScopeDeniedError("Tool acquisition worker runtime/workspace mismatch")
        if not isinstance(setup_token, str) or len(setup_token) < 32:
            raise ScopeDeniedError("Tool acquisition worker requires its native setup capability")
        for private in (self.recipe_dir, self.evidence_dir, sources.root):
            for public in (manager.runtime.home, manager.store.root):
                if (
                    private == public
                    or private.is_relative_to(public)
                    or public.is_relative_to(private)
                ):
                    raise ScopeDeniedError(
                        "Tool controller receipts must be outside guest-visible storage"
                    )

    def _recipe(self, claim: dict[str, Any]) -> ToolRecipe:
        if claim["agent_id"] != self.agent_id or not isinstance(claim["id"], UUID):
            raise ScopeDeniedError("Tool acquisition claim belongs to another enrolled agent")
        expected = ToolRecipe.from_dict(claim["recipe"])
        if expected.digest != claim["recipe_sha256"]:
            raise StateMismatchError("Tool acquisition recipe digest changed")
        actual = load_tool_recipe(self.recipe_dir / (expected.digest + ".json"))
        if actual.digest != expected.digest or actual.to_dict() != expected.to_dict():
            raise ScopeDeniedError("Tool acquisition recipe differs from its private operator copy")
        return actual

    def _execution(self, claim: dict[str, Any], recipe: ToolRecipe) -> dict[str, Any]:
        home = self.homes.lookup(self.context.workspace_id, self.agent_id)
        if home is None:
            raise ScopeDeniedError("Tool acquisition requires a registered agent home")
        definition = self.manager.acquisition_predecessor(
            workspace_id=self.context.workspace_id,
            agent_id=self.agent_id,
            slug=recipe.slug,
        )
        if definition is not None and definition.repository != recipe.repository:
            raise ScopeDeniedError("Tool acquisition cannot replace a different source publisher")
        if definition is not None and definition.commit == claim["new_commit"]:
            raise StateMismatchError("Tool acquisition source already matches the installed commit")
        rendered = recipe.render(operation_id=claim["id"], agent_directory=home.directory)
        argv = tuple(rendered["build_argv"])
        runtime = self.manager.runtime
        return {
            "agent_directory": home.directory,
            "build_root": f"/home/{home.directory}/builds/{claim['id'].hex}",
            "build_argv": list(argv),
            "command_sha256": command_digest(argv),
            "image_digest": runtime.image_digest,
            "qualification_sha256": runtime.qualification_sha256,
            "lockfiles": [
                list(item) for item in self.sources.lockfiles(claim["id"], recipe.lockfile_paths)
            ],
            "old_definition": (
                json.loads(json.dumps(definition.to_dict())) if definition is not None else None
            ),
        }

    def _source(self, claim: dict[str, Any], recipe: ToolRecipe) -> dict[str, Any]:
        receipt = self.sources.acquire(claim["id"], recipe.repository, claim["new_commit"])
        execution = self._execution(claim, recipe)
        return self.service.complete_source(self.context, claim, receipt.to_dict(), execution)

    def _build_tree(self, claim: dict[str, Any], execution: dict[str, Any]) -> Path:
        home = self.manager.runtime.home / str(execution["agent_directory"])
        if home.is_symlink() or not home.is_dir() or home.resolve(strict=True) != home:
            raise ScopeDeniedError("Tool acquisition agent home must be physical")
        identity = home.stat()
        builds = home / "builds"
        if not builds.exists():
            builds.mkdir(mode=0o700)
            os.chown(builds, identity.st_uid, identity.st_gid, follow_symlinks=False)
        if builds.is_symlink() or not builds.is_dir() or builds.resolve(strict=True) != builds:
            raise ScopeDeniedError("Tool build parent is redirected")
        if builds.stat().st_uid != identity.st_uid or builds.stat().st_mode & 0o022:
            raise ScopeDeniedError("Tool build parent differs from qualified home ownership")
        destination = builds / str(claim["id"].hex)
        self.sources.materialize(
            claim["id"], destination, host_owner=(identity.st_uid, identity.st_gid)
        )
        self.sources.verify_materialized(claim["id"], destination)
        return destination

    def _build(self, claim: dict[str, Any], recipe: ToolRecipe) -> dict[str, Any]:
        retained = self.sources.inspect(claim["id"]).to_dict()
        if retained != claim["source_receipt"]:
            raise StateMismatchError("Retained source differs from approved build evidence")
        execution = self._execution(claim, recipe)
        if execution != claim["execution"]:
            raise StateMismatchError(
                "Current recipe, source, predecessor or runtime changed after review"
            )
        tree = (
            self.manager.runtime.home
            / str(execution["agent_directory"])
            / "builds"
            / str(claim["id"].hex)
        )
        prior = execution["old_definition"]
        plan = ToolBuildPlan(
            self.context.workspace_id,
            self.agent_id,
            claim["id"],
            recipe.slug,
            tuple(execution["build_argv"]),
            execution["command_sha256"],
            prior["commit"] if prior is not None else None,
            claim["new_commit"],
            recipe.timeout_ms,
        )
        staging = build_and_stage(
            plan=plan,
            operator=self.operator,
            store=self.manager.store,
            homes=self.homes,
            evidence_dir=self.evidence_dir,
            built_tree=tree,
            authorize_attempt=lambda: self.service.validate_claim(self.context, claim),
            prepare_tree=lambda: self._build_tree(claim, execution),
        )
        staged = self.manager.store.root / stage_name(recipe.slug, claim["id"])
        replacement = SharedToolDefinition(
            recipe.slug,
            recipe.repository,
            claim["new_commit"],
            recipe.cleanup_paths,
            tuple((path, digest) for path, digest in execution["lockfiles"]),
            False,
            tuple(
                (relative, _artifact_hash(staged, relative)) for relative in recipe.artifact_paths
            ),
        )
        self.manager.prepare(
            workspace_id=self.context.workspace_id,
            agent_id=self.agent_id,
            operation_id=claim["id"],
            expected_old_commit=prior["commit"] if prior is not None else None,
            replacement=replacement,
            suite=recipe_suite(recipe),
            authorize_attempt=lambda: self.service.validate_claim(self.context, claim),
        )
        tested = self.manager.inspect(
            workspace_id=self.context.workspace_id,
            agent_id=self.agent_id,
            operation_id=claim["id"],
        )
        return self.service.complete_build(self.context, claim, staging, tested)

    def _save_action_pins(self, claim: dict[str, Any], proposal: dict[str, object]) -> None:
        if proposal.get("requires_operator_runtime_repin") is not True:
            raise StateMismatchError("Tool lifecycle pin proposal is incomplete")
        target = (
            self.promotion.proposal_dir
            / f"acquisition-{claim['id'].hex}-{claim['worker_token'].hex}.json"
        )
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as writer:
            writer.write(
                (
                    json.dumps(
                        {
                            "schema": "potbot-tool-acquisition-repin/v1",
                            "operationId": str(claim["id"]),
                            "proposal": proposal,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            )
            writer.flush()
            os.fsync(writer.fileno())
        directory = os.open(
            self.promotion.proposal_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _action(self, claim: dict[str, Any], recipe: ToolRecipe) -> dict[str, Any]:
        action, operation = claim["action"], claim["id"]
        authority = self.promotion.release_authority()
        result: dict[str, Any]

        def authorize_attempt() -> None:
            self.service.validate_claim(self.context, claim)

        def authorize_commit(connection: Any) -> None:
            self.service.validate_claim(self.context, claim, connection=connection)

        if action == "repin":
            result = authority.record_repin(
                operation=operation,
                authorize_attempt=authorize_attempt,
                authorize_commit=authorize_commit,
            )
        elif action == "postflight":
            self.operator.bind_tool_release_authority(authority)
            result = authority.postflight(
                operation=operation,
                suite=recipe_suite(recipe),
                authorize_attempt=authorize_attempt,
                authorize_commit=authorize_commit,
            )
        elif action == "rollback":
            result = self.manager.rollback(
                workspace_id=self.context.workspace_id,
                agent_id=self.agent_id,
                operation_id=operation,
                slug=recipe.slug,
                expected_current_commit=claim["new_commit"],
                confirm=True,
                authorize_attempt=authorize_attempt,
            )
            proposal = self.manager.runtime_entrypoint_updates(
                workspace_id=self.context.workspace_id,
                agent_id=self.agent_id,
                operation_id=operation,
            )
            self._save_action_pins(claim, proposal)
        elif action == "reconcile":
            with self.manager.store.ledger._connect() as conn:
                journal = conn.execute(
                    "SELECT workspace_id,agent_id,slug,state FROM trusted_tool_upgrades WHERE operation_id=?",
                    (str(operation),),
                ).fetchone()
            if journal is not None and (
                journal["workspace_id"] != str(self.context.workspace_id)
                or journal["agent_id"] != str(self.agent_id)
                or journal["slug"] != recipe.slug
            ):
                raise ScopeDeniedError("Tool reconciliation journal belongs to another operation")
            if (
                claim.get("abandon_only") is True
                or journal is None
                or journal["state"] in {"ready", "aborted"}
            ):
                # This acquires the same exclusive lease as dispatch. It does
                # not infer that an uncertain child stopped or restart a build.
                result = self.manager.abandon_candidate(
                    workspace_id=self.context.workspace_id,
                    agent_id=self.agent_id,
                    operation_id=operation,
                    slug=recipe.slug,
                    confirm=True,
                    authorize_attempt=authorize_attempt,
                )
            else:
                if journal["state"] in {"switching", "rolling_back", "indeterminate"}:
                    result = self.manager.reconcile(
                        workspace_id=self.context.workspace_id,
                        agent_id=self.agent_id,
                        operation_id=operation,
                        slug=recipe.slug,
                        confirm=True,
                        authorize_attempt=authorize_attempt,
                    )
                else:
                    result = self.manager.inspect(
                        workspace_id=self.context.workspace_id,
                        agent_id=self.agent_id,
                        operation_id=operation,
                    )
                if result.get("state") in {"active", "rolled_back"}:
                    recorded = self.service.inspect(self.context, operation)["operation"]
                    hold = read_hold(self.manager.store.root, recipe.slug)
                    matches = hold is not None and recorded["releaseBindingSha256"] == content_hash(
                        hold
                    )
                    phase = recorded["releaseState"]
                    if matches and phase in {"repinned", "released", "withdrawn"}:
                        result = authority.record_repin(
                            operation=operation,
                            authorize_attempt=authorize_attempt,
                            authorize_commit=authorize_commit,
                        )
                    elif matches and phase in {"failed", "indeterminate"}:
                        result = authority.read_held(
                            operation=operation,
                            authorize_attempt=authorize_attempt,
                            authorize_commit=authorize_commit,
                        )
                    elif result.get("state") == "active":
                        result = authority.reconcile_activation(
                            operation=operation,
                            confirm=True,
                            authorize_attempt=authorize_attempt,
                            authorize_commit=authorize_commit,
                        )
                if result.get("state") != "aborted" and result.get("releaseState") not in {
                    "repinned",
                    "released",
                    "withdrawn",
                    "failed",
                    "indeterminate",
                }:
                    proposal = self.manager.runtime_entrypoint_updates(
                        workspace_id=self.context.workspace_id,
                        agent_id=self.agent_id,
                        operation_id=operation,
                    )
                    self._save_action_pins(claim, proposal)
        else:
            raise InvalidInputError("Unknown tool acquisition host action")
        return self.service.complete_action(self.context, claim, dict(result))

    def run_once(self, kind: str, *, operation_id: UUID | None = None) -> dict[str, Any]:
        if kind == "promote":
            return self.promotion.run_once(operation_id=operation_id)
        if kind not in {"source", "build", "action"}:
            raise InvalidInputError("Tool acquisition worker requires an explicit phase")
        claim = self.service.claim(
            self.context,
            self.agent_id,
            kind,
            setup_token=self.setup_token,
            operation_id=operation_id,
        )
        if claim is None:
            return {"state": "idle"}
        try:
            recipe = self._recipe(claim)
            if kind == "source":
                return self._source(claim, recipe)
            if kind == "build":
                return self._build(claim, recipe)
            return self._action(claim, recipe)
        except Exception as exc:
            code = exc.code.value if isinstance(exc, DomainError) else "internal_error"
            # Build/action exceptions may follow real process or filesystem effects.
            # A host must reconcile them; the service never reschedules the claim.
            return self.service.fail(self.context, claim, code, uncertain=kind != "source")
