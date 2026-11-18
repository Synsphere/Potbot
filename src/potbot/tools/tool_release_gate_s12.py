"""S12: PostgreSQL-authoritative postflight release of exact shared-tool bytes.

The host filesystem journal may hold a tool, never release it. This authority
rechecks signed operator approval, live runtime pins and installed source before
admission. Interrupted postflight is not replayed. Rollback uses S9's existing
fenced exchange and requires fresh pin/readback/postflight for the restored bits.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from potbot.api.ui_tool_upgrades import _approval_mac
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.serialization import content_hash
from potbot.tools.shared_tool_release_markers import (
    ToolMaintenanceProbe,
    canonical,
    read_hold,
)
from potbot.tools.shared_tool_upgrades import SandboxedToolTestExecutor, ToolTestSuite

if TYPE_CHECKING:
    from potbot.api.ui_tool_upgrades import UiToolUpgradeStore
    from potbot.domain.auth import WorkspaceContext
    from potbot.persistence.database import Database
    from potbot.tools.agent_homes import AgentHomeRegistry
    from potbot.tools.shared_tool_upgrades import SharedToolUpgradeManager, TestExecutor
    from potbot.tools.shared_workspace_isolation import SharedWorkspaceRuntime
    from potbot.tools.workspace_concurrency import WorkspaceLeaseLedger


class ToolReleaseAuthority:
    """Host-owned authority; never supplied by an API payload or model result."""

    def __init__(
        self,
        *,
        approvals: UiToolUpgradeStore,
        manager: SharedToolUpgradeManager,
        context: WorkspaceContext,
        agent_id: UUID | None,
        setup_token: str,
    ) -> None:
        if (
            (agent_id is not None and not isinstance(agent_id, UUID))
            or not isinstance(setup_token, str)
            or (
                not 32 <= len(setup_token) <= 4096
                or not all(33 <= ord(c) <= 126 for c in setup_token)
            )
        ):
            raise ScopeDeniedError("S12 requires the operator's independent setup capability")
        self.approvals, self.manager, self.context = approvals, manager, context
        self.agent_id, self._setup_token = agent_id, setup_token

    def _record(
        self,
        conn: Any,
        operation: UUID,
        *,
        lock: bool = False,
        binding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM ui_tool_upgrade_approvals WHERE workspace_id=%s AND id=%s"
            + (" FOR UPDATE" if lock else ""),
            (self.context.workspace_id, operation),
        ).fetchone()
        if row is None or (self.agent_id is not None and row["agent_id"] != self.agent_id):
            raise ScopeDeniedError("S12 release is not owned by this workspace Agent")
        decision = conn.execute(
            "SELECT revision FROM ui_tool_upgrade_audit WHERE workspace_id=%s "
            "AND upgrade_id=%s AND action='approved' ORDER BY revision LIMIT 2",
            (self.context.workspace_id, operation),
        ).fetchall()
        if (
            len(decision) != 1
            or row["approval_principal_id"] is None
            or (
                not isinstance(row["approval_mac"], str)
                or not hmac.compare_digest(
                    row["approval_mac"],
                    _approval_mac(
                        self._setup_token,
                        row,
                        row["approval_principal_id"],
                        decision[0]["revision"],
                    ),
                )
            )
        ):
            raise ScopeDeniedError("S12 release requires the original signed operator decision")
        result = dict(row)
        if binding is not None:
            self._approved_binding(result, binding)
        return result

    def _approved_binding(self, row: dict[str, Any], hold: dict[str, Any]) -> None:
        """An operation ID alone cannot join signed approval to installed bits."""
        _, old, new = self.manager._transaction(
            self.context.workspace_id,
            UUID(hold["agent_id"]),
            UUID(hold["operation_id"]),
            hold["slug"],
            allowed={"active"} if hold["direction"] == "activate" else {"rolled_back"},
        )
        expected = {
            "workspace_id": hold["workspace_id"],
            "id": hold["operation_id"],
            "agent_id": hold["agent_id"],
            "slug": hold["slug"],
            "old_commit": old.commit if old is not None else None,
            "new_commit": new.commit,
            "evidence_sha256": hold["test_evidence_sha256"],
            "suite_sha256": hold["suite_sha256"],
        }
        target = new if hold["direction"] in {"activate", "withdraw"} else old
        if (
            any(
                (str(row[field]) if row[field] is not None else None) != value
                for field, value in expected.items()
            )
            or target is None
            or (hold["direction"] == "withdraw" and old is not None)
            or hold["commit"] != target.commit
        ):
            raise ScopeDeniedError("S12 installed binding differs from signed operator approval")

    def _mac(self, row: dict[str, Any], binding: dict[str, Any], evidence: dict[str, Any]) -> str:
        value = {
            "schema": "potbot-s12-released/1",
            "workspace": str(self.context.workspace_id),
            "operation": str(row["id"]),
            "agent": str(row["agent_id"]),
            "approval_mac": row["approval_mac"],
            "generation": row["release_generation"],
            "binding": binding,
            "evidence": evidence,
        }
        return hmac.new(
            self._setup_token.encode("ascii"), canonical(value).encode(), hashlib.sha256
        ).hexdigest()

    def _readback(
        self,
        hold: dict[str, Any],
        runtime: SharedWorkspaceRuntime,
        *,
        require_pins: bool = True,
    ) -> None:
        manager = self.manager
        workspace = self.context.workspace_id
        agent = self.agent_id or UUID(hold["agent_id"])
        if (
            hold["workspace_id"] != str(workspace)
            or hold["agent_id"] != str(agent)
            or runtime.shared_tools != manager.store.root
            or hold["image_digest"] != runtime.image_digest
            or hold["qualification_sha256"] != runtime.qualification_sha256
            or read_hold(manager.store.root, hold["slug"]) != hold
        ):
            raise ScopeDeniedError("S12 release binding differs from the selected runtime")
        manager._actor(workspace, agent)
        row, old, new = manager._transaction(
            workspace,
            agent,
            UUID(hold["operation_id"]),
            hold["slug"],
            allowed={"active"} if hold["direction"] == "activate" else {"rolled_back"},
        )
        if hold["direction"] == "withdraw":
            if old is not None:
                raise StateMismatchError("S12 withdrawal requires an absent predecessor")
            manager._require_absent(workspace, hold["slug"])
            if (
                content_hash(new.to_dict()) != hold["definition_sha256"]
                or row["evidence_sha256"] != hold["test_evidence_sha256"]
                or row["suite_sha256"] != hold["suite_sha256"]
                or content_hash(
                    manager._verify_at(manager._location(new.slug, UUID(row["operation_id"])), new)
                )
                != hold["identity_sha256"]
                or (
                    require_pins
                    and any(
                        path.startswith(f"/usr/local/tools/{hold['slug']}/")
                        for path, _ in runtime.tool_entrypoints
                    )
                )
            ):
                raise StateMismatchError("S12 withdrawal bytes or retired executable pins changed")
            return
        definition = new if hold["direction"] == "activate" else old
        if definition is None:
            raise StateMismatchError("S12 absent predecessor cannot authorize a restored release")
        registered = manager.store.definition(workspace, hold["slug"])
        if (
            registered != definition
            or content_hash(definition.to_dict()) != hold["definition_sha256"]
            or row["evidence_sha256"] != hold["test_evidence_sha256"]
            or row["suite_sha256"] != hold["suite_sha256"]
            or content_hash(manager._verify_at(manager.store.root / hold["slug"], definition))
            != hold["identity_sha256"]
        ):
            raise StateMismatchError("S12 installed source or artifact readback changed")
        prefix = f"/usr/local/tools/{hold['slug']}/"
        actual_pins = {
            path: digest for path, digest in runtime.tool_entrypoints if path.startswith(prefix)
        }
        if require_pins and (
            len(actual_pins) != sum(path.startswith(prefix) for path, _ in runtime.tool_entrypoints)
            or actual_pins != hold["entrypoints"]
        ):
            raise StateMismatchError("S12 independently loaded executable pins do not match")

    def admits(self, hold: dict[str, Any], runtime: SharedWorkspaceRuntime) -> bool:
        """Read-only launch check; a missing row, changed revision or DB outage denies."""
        if hold["direction"] == "withdraw":
            raise StateMismatchError(
                "S12 withdrawal is an absence receipt, never execution authority"
            )
        self._readback(hold, runtime)
        with self.approvals.database.transaction(self.context) as conn:
            row = self._record(conn, UUID(hold["operation_id"]), binding=hold)
        if (
            row["release_phase"] != "released"
            or str(row["agent_id"]) != hold["agent_id"]
            or row["state"] != ("active" if hold["direction"] == "activate" else "rolled_back")
            or row["release_binding"] != hold
            or not isinstance(row["release_evidence"], dict)
            or not isinstance(row["release_mac"], str)
            or not hmac.compare_digest(
                row["release_mac"], self._mac(row, hold, row["release_evidence"])
            )
        ):
            raise StateMismatchError("S12 tool has no matching completed postflight release")
        return True

    def record_repin(
        self,
        *,
        operation: UUID,
        authorize_attempt: Callable[[], None] | None = None,
        authorize_commit: Callable[[Any], None] | None = None,
    ) -> dict[str, object]:
        """Independent host readback; this prepares postflight, never releases a tool."""
        if self.agent_id is None:
            raise ScopeDeniedError("Installed S12 admission reader cannot release a tool")
        manager = self.manager
        inspected = manager.inspect(
            workspace_id=self.context.workspace_id, agent_id=self.agent_id, operation_id=operation
        )
        slug = str(inspected["slug"])
        hold = read_hold(manager.store.root, slug)
        if hold is None or hold["operation_id"] != str(operation):
            raise StateMismatchError("S12 activation has no matching durable host hold")
        guard = manager._guard(
            self.context.workspace_id,
            self.agent_id,
            slug,
            "s12-repin-" + uuid4().hex,
            "tool.release_repin",
        )
        try:
            if authorize_attempt is not None:
                authorize_attempt()
            self._readback(hold, manager.runtime)
            if authorize_attempt is not None:
                authorize_attempt()
            guard.ensure_live()
            with self.approvals.database.transaction(self.context) as conn:
                row = self._record(conn, operation, lock=True, binding=hold)
                if authorize_commit is not None:
                    authorize_commit(conn)
                if hold["direction"] == "withdraw":
                    return self._record_withdrawal(conn, operation, row, hold)
                if row["release_binding"] == hold and row["release_phase"] in {
                    "repinned",
                    "released",
                }:
                    return {
                        "id": str(operation),
                        "state": row["state"],
                        "releaseState": row["release_phase"],
                    }
                if hold["direction"] == "activate" and row["state"] != "awaiting_repin":
                    raise StateMismatchError("S12 activation must be reconciled before repin")
                if row["release_binding"] == hold and row["release_phase"] in {
                    "testing",
                    "indeterminate",
                    "failed",
                }:
                    raise StateMismatchError(
                        "S12 postflight is held; reconcile or roll back without replay"
                    )
                updated = conn.execute(
                    "UPDATE ui_tool_upgrade_approvals SET state='awaiting_repin',worker_token=NULL,"
                    "release_phase='repinned',release_binding=%s::jsonb,release_evidence=NULL,"
                    "release_mac=NULL,postflight_token=NULL,release_generation=release_generation+1,"
                    "revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                    (canonical(hold), self.context.workspace_id, operation),
                ).fetchone()
                self.approvals._audit(
                    conn,
                    self.context,
                    operation,
                    "release_repinned",
                    row["state"],
                    "awaiting_repin",
                    updated["revision"],
                    content_hash(hold),
                )
            return {
                "id": str(operation),
                "state": "awaiting_postflight",
                "releaseState": "repinned",
            }
        finally:
            try:
                guard.finish(evidence_sha256=content_hash(hold))
            except StateMismatchError:
                guard.abandon()

    def _record_withdrawal(
        self,
        conn: Any,
        operation: UUID,
        row: dict[str, Any],
        hold: dict[str, Any],
    ) -> dict[str, object]:
        """A signed verified absence closes rollback without running a fake test.

        The owning per-tool lease and exact readback are held by record_repin.
        Retaining the denial marker prevents an unregistered replacement from
        becoming visible under the previously admitted executable pathname.
        """
        if row["release_phase"] == "withdrawn":
            if (
                row["state"] != "rolled_back"
                or row["release_binding"] != hold
                or not isinstance(row["release_evidence"], dict)
                or not isinstance(row["release_mac"], str)
                or not hmac.compare_digest(
                    row["release_mac"], self._mac(row, hold, row["release_evidence"])
                )
            ):
                raise StateMismatchError("S12 retained withdrawal evidence changed")
            return {"id": str(operation), "state": "rolled_back", "releaseState": "withdrawn"}
        evidence = {
            "schema": "potbot-tool-withdrawal/1",
            "binding_sha256": content_hash(hold),
            "installation_state": "absent",
            "retained_identity_sha256": hold["identity_sha256"],
            "executable_pins_removed": True,
        }
        next_row = dict(row)
        next_row["release_generation"] += 1
        updated = conn.execute(
            "UPDATE ui_tool_upgrade_approvals SET state='rolled_back',worker_token=NULL,"
            "release_phase='withdrawn',release_binding=%s::jsonb,release_evidence=%s::jsonb,"
            "release_mac=%s,postflight_token=NULL,release_generation=release_generation+1,"
            "revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
            (
                canonical(hold),
                canonical(evidence),
                self._mac(next_row, hold, evidence),
                self.context.workspace_id,
                operation,
            ),
        ).fetchone()
        self.approvals._audit(
            conn,
            self.context,
            operation,
            "withdrawn",
            row["state"],
            "rolled_back",
            updated["revision"],
            content_hash(evidence),
        )
        return {"id": str(operation), "state": "rolled_back", "releaseState": "withdrawn"}

    def read_held(
        self,
        *,
        operation: UUID,
        authorize_attempt: Callable[[], None] | None = None,
        authorize_commit: Callable[[Any], None] | None = None,
    ) -> dict[str, object]:
        """Acknowledge an unchanged failed/uncertain release without replaying it.

        This is only a verified held-state observation after a separately
        authorized host-action recovery. It does not reset postflight, remove
        its failure evidence, assert process termination, or require new pins.
        A live/uncertain host lease still blocks the readback guard.
        """
        if self.agent_id is None:
            raise ScopeDeniedError("Installed admission readers cannot reconcile held releases")
        manager = self.manager
        inspected = manager.inspect(
            workspace_id=self.context.workspace_id,
            agent_id=self.agent_id,
            operation_id=operation,
        )
        hold = read_hold(manager.store.root, str(inspected["slug"]))
        if hold is None or hold["operation_id"] != str(operation):
            raise StateMismatchError("S12 held-state readback has no matching private hold")
        guard = manager._guard(
            self.context.workspace_id,
            self.agent_id,
            hold["slug"],
            "s12-held-read-" + uuid4().hex,
            "tool.release_read",
        )
        try:
            if authorize_attempt is not None:
                authorize_attempt()
            self._readback(hold, manager.runtime, require_pins=False)
            guard.ensure_live()
            with self.approvals.database.transaction(self.context) as conn:
                row = self._record(conn, operation, lock=True, binding=hold)
                if authorize_commit is not None:
                    authorize_commit(conn)
                if (
                    row["release_binding"] != hold
                    or row["release_phase"] not in {"failed", "indeterminate"}
                    or row["postflight_token"] is not None
                ):
                    raise StateMismatchError(
                        "S12 held-state readback differs from retained failure"
                    )
                return {
                    "id": str(operation),
                    "state": row["state"],
                    "releaseState": row["release_phase"],
                }
        finally:
            try:
                guard.finish(evidence_sha256=content_hash(hold))
            except StateMismatchError:
                guard.abandon()
                raise

    def reconcile_activation(
        self,
        *,
        operation: UUID,
        confirm: bool,
        authorize_attempt: Callable[[], None] | None = None,
        authorize_commit: Callable[[Any], None] | None = None,
    ) -> dict[str, object]:
        """Acknowledge a verified S9 outcome after an interrupted S11 response.

        The host must first reconcile any uncertain filesystem/worker lease via
        its existing operator. This never reruns activation or postflight.
        """
        if confirm is not True or self.agent_id is None:
            raise ScopeDeniedError("S12 recovery requires an explicit verified-outcome decision")
        inspected = self.manager.inspect(
            workspace_id=self.context.workspace_id, agent_id=self.agent_id, operation_id=operation
        )
        hold = read_hold(self.manager.store.root, str(inspected["slug"]))
        if (
            hold is None
            or hold["operation_id"] != str(operation)
            or hold["direction"] != "activate"
        ):
            raise StateMismatchError("S12 recovery has no matching host hold")
        guard = self.manager._guard(
            self.context.workspace_id,
            self.agent_id,
            hold["slug"],
            "s12-reconcile-" + uuid4().hex,
            "tool.release_reconcile",
        )
        try:
            if authorize_attempt is not None:
                authorize_attempt()
            # Recovering an interrupted activation only restores the repin
            # handoff. Exact bytes and signed approval are still checked here;
            # record_repin and postflight retain strict executable-pin checks.
            self._readback(hold, self.manager.runtime, require_pins=False)
            if authorize_attempt is not None:
                authorize_attempt()
            guard.ensure_live()
            with self.approvals.database.transaction(self.context) as conn:
                row = self._record(conn, operation, lock=True, binding=hold)
                if authorize_commit is not None:
                    authorize_commit(conn)
                if row["release_phase"] != "none" or row["state"] not in {
                    "approved",
                    "executing",
                    "indeterminate",
                    "awaiting_repin",
                }:
                    raise StateMismatchError("S12 recovery cannot replay an attempted postflight")
                changed = conn.execute(
                    "UPDATE ui_tool_upgrade_approvals SET state='awaiting_repin',worker_token=NULL,"
                    "revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                    (self.context.workspace_id, operation),
                ).fetchone()
                self.approvals._audit(
                    conn,
                    self.context,
                    operation,
                    "reconciled",
                    row["state"],
                    "awaiting_repin",
                    changed["revision"],
                    content_hash(hold),
                )
            return {"id": str(operation), "state": "awaiting_repin", "releaseState": "none"}
        finally:
            try:
                guard.finish(evidence_sha256=content_hash(hold))
            except StateMismatchError:
                guard.abandon()

    def postflight(
        self,
        *,
        operation: UUID,
        suite: ToolTestSuite,
        executor: TestExecutor | None = None,
        authorize_attempt: Callable[[], None] | None = None,
        authorize_commit: Callable[[Any], None] | None = None,
    ) -> dict[str, object]:
        """Execute the approved suite once under the existing exclusive tool lease.

        Custom executors are trusted qualification fixtures; production uses the
        existing sandbox supervisor. A crashed or uncertain attempt cannot rerun.
        """
        if not isinstance(suite, ToolTestSuite):
            raise ScopeDeniedError("S12 postflight requires the operator's pinned test suite")
        if self.agent_id is None:
            raise ScopeDeniedError("Installed S12 admission reader cannot run postflight")
        manager, context = self.manager, self.context
        with self.approvals.database.transaction(context) as conn:
            initial = self._record(conn, operation)
        hold = initial["release_binding"]
        if not isinstance(hold, dict) or suite.digest != hold["suite_sha256"]:
            raise StateMismatchError("S12 postflight suite differs from the approved tests")
        if hold["direction"] == "withdraw":
            raise StateMismatchError("S12 an absent installation has no executable postflight")
        self._approved_binding(initial, hold)
        if initial["release_phase"] == "released":
            self.admits(hold, manager.runtime)
            return {"id": str(operation), "state": initial["state"], "releaseState": "released"}
        guard = manager._guard(
            context.workspace_id,
            self.agent_id,
            hold["slug"],
            "s12-postflight-" + uuid4().hex,
            "tool.postflight",
        )
        token = uuid4()
        generation = initial["release_generation"]
        claimed = False
        uncertain = False
        lease_finished = False
        results: list[dict[str, object]] = []
        try:
            if authorize_attempt is not None:
                authorize_attempt()
            self._readback(hold, manager.runtime)
            guard.ensure_live()
            with self.approvals.database.transaction(context) as conn:
                row = self._record(conn, operation, lock=True, binding=hold)
                if authorize_commit is not None:
                    authorize_commit(conn)
                if (
                    row["release_phase"] != "repinned"
                    or row["release_binding"] != hold
                    or row["release_generation"] != generation
                ):
                    raise StateMismatchError("S12 postflight is already attempted or not repinned")
                changed = conn.execute(
                    "UPDATE ui_tool_upgrade_approvals SET release_phase='testing',postflight_token=%s,"
                    "revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                    (token, context.workspace_id, operation),
                ).fetchone()
                self.approvals._audit(
                    conn,
                    context,
                    operation,
                    "postflight_started",
                    row["release_phase"],
                    "testing",
                    changed["revision"],
                    content_hash(hold),
                )
            claimed = True
            home = manager.homes.lookup(context.workspace_id, self.agent_id)
            if home is None:
                raise ScopeDeniedError("S12 postflight Agent home is unavailable")
            probe = ToolMaintenanceProbe(guard, hold["slug"], content_hash(hold))
            selected = executor or SandboxedToolTestExecutor(
                replace(manager.runtime, tool_maintenance_probe=probe), monitor=manager.ai_monitor
            )
            for case in suite.cases:
                if authorize_attempt is not None:
                    authorize_attempt()
                guard.ensure_live()
                self._readback(hold, manager.runtime)
                uncertain = True
                result = selected.run(
                    case=case,
                    argv=case.render(slug=hold["slug"], stage=hold["slug"], agent=home.directory),
                    workspace_id=context.workspace_id,
                    agent_id=self.agent_id,
                    agent_dir=manager.runtime.home / home.directory,
                    uid=home.uid,
                    gid=home.gid,
                    cancelled=lambda: guard.fenced,
                )
                if not isinstance(result, dict):
                    raise StateMismatchError("S12 postflight returned malformed evidence")
                if result.get("stopped") is True:
                    uncertain = False
                elapsed = result.get("elapsed_ms")
                output_digest = result.get("output_sha256")
                if (
                    result.get("name") != case.name
                    or result.get("kind") != case.kind
                    or result.get("passed") is not True
                    or result.get("dispatched") is not True
                    or result.get("stopped") is not True
                    or result.get("outcome") != "completed"
                    or result.get("exit_code") != 0
                    or not isinstance(elapsed, int)
                    or isinstance(elapsed, bool)
                    or not 0 <= elapsed <= case.timeout_ms
                    or not isinstance(result.get("isolation_identity"), str)
                    or not result["isolation_identity"]
                    or not isinstance(output_digest, str)
                    or re.fullmatch(r"[a-f0-9]{64}", output_digest) is None
                ):
                    raise StateMismatchError(
                        "S12 postflight did not return complete passing evidence"
                    )
                results.append(
                    {
                        key: result[key]
                        for key in (
                            "name",
                            "kind",
                            "outcome",
                            "exit_code",
                            "elapsed_ms",
                            "dispatched",
                            "stopped",
                            "isolation_identity",
                            "output_sha256",
                        )
                    }
                )
            self._readback(hold, manager.runtime)
            if authorize_attempt is not None:
                authorize_attempt()
            guard.ensure_live()
            evidence = {
                "schema": "potbot-tool-postflight/1",
                "binding_sha256": content_hash(hold),
                "suite_sha256": suite.digest,
                "cases": results,
            }
            # Both durable stores must fail closed after a crash. Finalize the
            # host lease before authorizing release in PG; a failure between
            # these writes leaves PG in testing and cannot authorize replay.
            # A concurrent later swap changes the hold and is independently
            # rejected at admission, even if this older receipt then commits.
            uncertain = True
            guard.finish(evidence_sha256=content_hash(evidence))
            lease_finished = True
            uncertain = False
            with self.approvals.database.transaction(context) as conn:
                row = self._record(conn, operation, lock=True, binding=hold)
                if authorize_commit is not None:
                    authorize_commit(conn)
                if (
                    row["release_phase"] != "testing"
                    or row["postflight_token"] != token
                    or (row["release_binding"] != hold)
                    or row["release_generation"] != generation
                ):
                    raise StateMismatchError("S12 postflight owner was fenced")
                state = "active" if hold["direction"] == "activate" else "rolled_back"
                updated = conn.execute(
                    "UPDATE ui_tool_upgrade_approvals SET state=%s,release_phase='released',"
                    "release_evidence=%s::jsonb,release_mac=%s,postflight_token=NULL,"
                    "revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s RETURNING *",
                    (
                        state,
                        canonical(evidence),
                        self._mac(row, hold, evidence),
                        context.workspace_id,
                        operation,
                    ),
                ).fetchone()
                self.approvals._audit(
                    conn,
                    context,
                    operation,
                    "released" if state == "active" else "rollback_released",
                    "testing",
                    state,
                    updated["revision"],
                    content_hash(evidence),
                )
            claimed = False
            return {
                "id": str(operation),
                "state": state,
                "releaseState": "released",
                "postflightSha256": content_hash(evidence),
            }
        except BaseException:
            if claimed:
                # If this write also fails, 'testing' retains its token and still
                # blocks dispatch/retry after restart. No successful fallback.
                phase = "indeterminate" if uncertain or guard.fenced else "failed"
                with self.approvals.database.transaction(context) as conn:
                    changed = conn.execute(
                        "UPDATE ui_tool_upgrade_approvals SET release_phase=%s,postflight_token=NULL,"
                        "revision=revision+1,updated_at=clock_timestamp() WHERE workspace_id=%s AND id=%s "
                        "AND release_phase='testing' AND postflight_token=%s RETURNING *",
                        (phase, context.workspace_id, operation, token),
                    ).fetchone()
                    if changed is not None:
                        self.approvals._audit(
                            conn,
                            context,
                            operation,
                            "postflight_" + phase,
                            "testing",
                            phase,
                            changed["revision"],
                            content_hash(hold),
                        )
            raise
        finally:
            if not lease_finished:
                try:
                    guard.finish(
                        evidence_sha256=content_hash({"binding": hold, "cases": results}),
                        uncertain=uncertain,
                    )
                except StateMismatchError:
                    guard.abandon()


def bind_installed_tool_releases(
    database: Database,
    context: WorkspaceContext,
    runtime: SharedWorkspaceRuntime,
    ledger: WorkspaceLeaseLedger,
    homes: AgentHomeRegistry,
) -> SharedWorkspaceRuntime:
    """Bind the installed service's existing protected credential and PG context.

    An unavailable credential leaves held tools masked. It does not make them
    legacy tools or prevent unrelated admitted image programs from running.
    """
    from potbot.api.ui_tool_upgrades import UiToolUpgradeStore
    from potbot.credentials import CredentialStore
    from potbot.tools.shared_tool_release_markers import DIRECTORY
    from potbot.tools.shared_tool_store import SharedToolStore
    from potbot.tools.shared_tool_upgrades import SharedToolUpgradeManager

    if runtime.shared_tools is None or not (runtime.shared_tools / DIRECTORY).exists():
        return runtime
    try:
        token = CredentialStore().get("api.setup_token")
        if not isinstance(token, str) or (
            not 32 <= len(token) <= 4096 or not all(33 <= ord(c) <= 126 for c in token)
        ):
            raise ScopeDeniedError("S12 setup credential unavailable")
    except Exception:
        logging.getLogger(__name__).warning(
            "S12 setup credential unavailable; changed tools remain held"
        )
        return runtime

    def authorized(workspace: UUID, agent: UUID) -> bool:
        return workspace == context.workspace_id and homes.lookup(workspace, agent) is not None

    store = SharedToolStore(ledger=ledger, shared_root=runtime.shared_tools, authorize=authorized)
    manager = SharedToolUpgradeManager(
        store=store,
        runtime=runtime,
        homes=homes,
        authorize=authorized,
    )
    authority = ToolReleaseAuthority(
        approvals=UiToolUpgradeStore(database),
        manager=manager,
        context=context,
        agent_id=None,
        setup_token=token,
    )
    selected = replace(runtime, tool_release_authority=authority)
    manager.runtime = selected
    return selected
