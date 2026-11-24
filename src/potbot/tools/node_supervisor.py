"""Native supervisor for one registered node's existing Potbot job worker.

Run on the node host. Native administrator credentials stay in this process;
the child and GUI use the ordinary restricted application principal.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from potbot.controller.node_controls import authenticate_intent, lock_node, pending, unsettled
from potbot.domain.auth import WorkspaceContext
from potbot.domain.errors import ScopeDeniedError, StateMismatchError
from potbot.domain.live_controls import worker_identity
from potbot.persistence.database import admin_connection


class Worker(Protocol):
    def wait_ready(self) -> None: ...
    def running(self) -> bool: ...
    def stop(self) -> None: ...


class WorkerProcess:
    """A real owned process; readiness comes from its existing CLI startup frame."""

    def __init__(self, argv: Sequence[str], worker_id: str, *, env: dict[str, str]) -> None:
        self.ready = threading.Event()
        self.process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )

        def read() -> None:
            assert self.process.stdout is not None
            while True:
                line = self.process.stdout.readline(16_385)
                if not line:
                    return
                if len(line) > 16_384:
                    # Drain long output without retaining it or treating it as readiness.
                    while line and not line.endswith(b"\n"):
                        line = self.process.stdout.readline(16_385)
                    continue
                try:
                    value = json.loads(line)
                    data = value.get("data", value.get("result", {}))
                    if data.get("worker_id") == worker_id and data.get("status") == "listening":
                        self.ready.set()
                except (ValueError, AttributeError, TypeError):
                    pass

        self.reader = threading.Thread(target=read, name="potbot-node-startup", daemon=True)
        self.reader.start()

    def wait_ready(self) -> None:
        deadline = time.monotonic() + 15
        while not self.ready.wait(0.05):
            if self.process.poll() is not None or time.monotonic() >= deadline:
                self.stop()
                raise StateMismatchError("The replacement worker did not confirm readiness")
        if not self.running():
            raise StateMismatchError("The worker exited during its startup handshake")

    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> None:
        if self.running():
            try:
                self.process.send_signal(
                    getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
                    if os.name == "nt"
                    else signal.SIGINT
                )
            except OSError:
                if os.name != "nt":
                    raise
                # A Windows service has no console for CTRL_BREAK. The queue
                # and holds have already drained before a normal stop.
                self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise StateMismatchError("Worker exit is unconfirmed; restart is fenced") from None
        self.reader.join(timeout=1)
        if self.reader.is_alive():
            raise StateMismatchError("The worker output stream remains owned; inspect descendants")
        if self.process.stdout is not None:
            self.process.stdout.close()


class NativeNodeSupervisor:
    def __init__(
        self,
        admin_dsn: str,
        schema: str,
        context: WorkspaceContext,
        node_id: UUID,
        generation: int,
        profile_sha256: str,
        setup_token: str,
        factory: Callable[[str], Worker],
    ) -> None:
        self.admin_dsn, self.schema, self.context = admin_dsn, schema, context
        self.node_id, self.generation, self.profile_sha256 = node_id, generation, profile_sha256
        self.setup_token, self.factory = setup_token, factory
        self.incarnation = uuid4()
        self.child: Worker | None = None

    def _transaction(self) -> Any:
        return admin_connection(self.admin_dsn, self.schema)

    def _registration(self, connection: Any) -> None:
        found = connection.execute(
            "SELECT 1 FROM remote_nodes n JOIN remote_workspace_owners o ON "
            "(o.workspace_id,o.principal_id)=(n.workspace_id,n.owner_principal_id) "
            "JOIN workspace_memberships m ON (m.workspace_id,m.principal_id)="
            "(o.workspace_id,o.principal_id) WHERE n.workspace_id=%s AND n.id=%s "
            "AND n.generation=%s AND o.principal_id=%s",
            (self.context.workspace_id, self.node_id, self.generation, self.context.principal_id),
        ).fetchone()
        if not found:
            raise ScopeDeniedError("The native node registration or owner changed")

    def _runtime(self, connection: Any) -> dict[str, Any]:
        lock_node(connection, self.context.workspace_id, self.node_id)
        self._registration(connection)
        row = connection.execute(
            "SELECT * FROM node_runtime_controls WHERE workspace_id=%s AND node_id=%s FOR UPDATE",
            (self.context.workspace_id, self.node_id),
        ).fetchone()
        if (
            row is None
            or row["incarnation"] != self.incarnation
            or row["profile_sha256"] != self.profile_sha256
        ):
            raise StateMismatchError("The native supervisor no longer owns this incarnation")
        return dict(row)

    def _event(
        self,
        connection: Any,
        runtime: dict[str, Any],
        stage: str,
        reason: str,
        request_id: UUID | None = None,
    ) -> None:
        identity = request_id or runtime["request_id"]
        if identity is None:
            return
        if (
            stage != "accepted"
            and connection.execute(
                "SELECT 1 FROM node_control_events WHERE workspace_id=%s AND request_id=%s "
                "AND stage IN ('applied','rejected','indeterminate')",
                (self.context.workspace_id, identity),
            ).fetchone()
        ):
            return
        connection.execute(
            "INSERT INTO node_control_events "
            "(workspace_id,node_id,request_id,stage,reason,incarnation,revision) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                self.context.workspace_id,
                self.node_id,
                identity,
                stage,
                reason,
                runtime["incarnation"],
                runtime["revision"],
            ),
        )

    def _change(
        self, connection: Any, phase: str, desired: str, *, request_id: UUID | None = None
    ) -> dict[str, Any]:
        row = connection.execute(
            "UPDATE node_runtime_controls SET phase=%s,desired=%s,revision=revision+1,"
            "request_id=%s,heartbeat_at=clock_timestamp() WHERE workspace_id=%s AND node_id=%s "
            "AND incarnation=%s RETURNING *",
            (phase, desired, request_id, self.context.workspace_id, self.node_id, self.incarnation),
        ).fetchone()
        if row is None:
            raise StateMismatchError("The node incarnation changed")
        return dict(row)

    def start(self) -> None:
        with self._transaction() as connection:
            lock_node(connection, self.context.workspace_id, self.node_id)
            self._registration(connection)
            old = connection.execute(
                "SELECT phase,revision FROM node_runtime_controls WHERE workspace_id=%s AND node_id=%s FOR UPDATE",
                (self.context.workspace_id, self.node_id),
            ).fetchone()
            if old is not None and old["phase"] != "stopped":
                raise StateMismatchError(
                    "A prior native supervisor requires independent reconciliation"
                )
            connection.execute(
                "INSERT INTO node_runtime_controls "
                "(workspace_id,node_id,principal_id,registration_generation,revision,desired,phase,"
                "incarnation,worker_id,profile_sha256) VALUES(%s,%s,%s,%s,%s,'active','starting',%s,%s,%s) "
                "ON CONFLICT(workspace_id,node_id) DO UPDATE SET principal_id=EXCLUDED.principal_id,"
                "registration_generation=EXCLUDED.registration_generation,revision=EXCLUDED.revision,"
                "desired='active',phase='starting',incarnation=EXCLUDED.incarnation,"
                "worker_id=EXCLUDED.worker_id,profile_sha256=EXCLUDED.profile_sha256,"
                "heartbeat_at=clock_timestamp(),request_id=NULL",
                (
                    self.context.workspace_id,
                    self.node_id,
                    self.context.principal_id,
                    self.generation,
                    (old["revision"] + 1) if old else 1,
                    self.incarnation,
                    worker_identity(self.node_id, self.incarnation),
                    self.profile_sha256,
                ),
            )
        self._launch(None)

    def _launch(self, request_id: UUID | None) -> None:
        try:
            self.child = self.factory(worker_identity(self.node_id, self.incarnation))
            self.child.wait_ready()
            if not self.child.running():
                raise StateMismatchError("The worker did not become ready")
        except Exception:
            with self._transaction() as connection:
                self._runtime(connection)
                row = self._change(connection, "indeterminate", "drain", request_id=request_id)
                self._event(connection, row, "indeterminate", "worker_start_unconfirmed")
            raise
        with self._transaction() as connection:
            self._runtime(connection)
            row = self._change(connection, "ready", "active", request_id=request_id)
            if request_id:
                self._event(connection, row, "applied", "replacement_worker_ready")

    def tick(self) -> None:
        restart: UUID | None = None
        with self._transaction() as connection:
            runtime = self._runtime(connection)
            if runtime["phase"] in {"indeterminate", "stopped"}:
                return
            if self.child is None or not self.child.running():
                row = self._change(
                    connection, "indeterminate", "drain", request_id=runtime["request_id"]
                )
                self._event(connection, row, "indeterminate", "worker_exit_unexpected")
                return
            connection.execute(
                "UPDATE node_runtime_controls SET heartbeat_at=clock_timestamp() "
                "WHERE workspace_id=%s AND node_id=%s",
                (self.context.workspace_id, self.node_id),
            )
            for intent in pending(connection, self.context.workspace_id, self.node_id):
                try:
                    review = authenticate_intent(
                        intent,
                        self.context.workspace_id,
                        self.context.principal_id,
                        self.setup_token,
                    )
                    if (
                        review["nodeId"] != str(self.node_id)
                        or review["expectedGeneration"] != self.generation
                        or review["expectedRevision"] != runtime["revision"]
                        or review["expectedIncarnation"] != str(self.incarnation)
                        or runtime["phase"] not in {"ready", "drained"}
                    ):
                        raise StateMismatchError("The node review is stale")
                except Exception:
                    try:
                        identity = UUID(intent["idempotency_key"])
                    except (ValueError, TypeError):
                        continue
                    self._event(
                        connection, runtime, "rejected", "review_invalid_or_stale", identity
                    )
                    continue
                runtime = self._change(
                    connection, "draining", review["action"], request_id=UUID(review["requestId"])
                )
                self._event(connection, runtime, "accepted", "admissions_fenced")
            if runtime["phase"] == "draining" and not unsettled(
                connection, self.context.workspace_id, self.node_id, self.incarnation
            ):
                if runtime["desired"] == "drain":
                    row = self._change(
                        connection, "drained", "drain", request_id=runtime["request_id"]
                    )
                    self._event(connection, row, "applied", "jobs_and_holds_settled")
                else:
                    self._change(
                        connection, "restarting", "restart", request_id=runtime["request_id"]
                    )
                    restart = runtime["request_id"]
        if restart is not None:
            self._restart(restart)

    def _restart(self, request_id: UUID) -> None:
        assert self.child is not None
        try:
            self.child.stop()
            if self.child.running():
                raise StateMismatchError("The prior worker remains alive")
        except Exception:
            with self._transaction() as connection:
                row = self._runtime(connection)
                row = self._change(connection, "indeterminate", "drain", request_id=request_id)
                self._event(connection, row, "indeterminate", "worker_exit_unconfirmed")
            return
        with self._transaction() as connection:
            self._runtime(connection)
            self.incarnation = uuid4()
            connection.execute(
                "UPDATE node_runtime_controls SET incarnation=%s,worker_id=%s,phase='starting',"
                "revision=revision+1,heartbeat_at=clock_timestamp() WHERE workspace_id=%s AND node_id=%s",
                (
                    self.incarnation,
                    worker_identity(self.node_id, self.incarnation),
                    self.context.workspace_id,
                    self.node_id,
                ),
            )
        self._launch(request_id)

    def begin_shutdown(self) -> None:
        with self._transaction() as connection:
            runtime = self._runtime(connection)
            self._event(connection, runtime, "rejected", "native_operator_shutdown")
            self._change(connection, "draining", "drain")

    def close(self) -> bool:
        with self._transaction() as connection:
            runtime = self._runtime(connection)
            if unsettled(connection, self.context.workspace_id, self.node_id, self.incarnation):
                connection.execute(
                    "UPDATE node_runtime_controls SET heartbeat_at=clock_timestamp() "
                    "WHERE workspace_id=%s AND node_id=%s",
                    (self.context.workspace_id, self.node_id),
                )
                return False
            self._change(connection, "drained", "drain", request_id=runtime["request_id"])
        if self.child:
            self.child.stop()
        with self._transaction() as connection:
            runtime = self._runtime(connection)
            self._change(connection, "stopped", "drain", request_id=runtime["request_id"])
        return True


def worker_environment(
    environment: dict[str, str], application_dsn: str, admin_dsn: str, setup_token: str
) -> dict[str, str]:
    """Only the parent resolves administrator credentials and the control MAC key."""
    result = {
        key: value
        for key, value in environment.items()
        if key not in {"POTBOT_ADMIN_DSN", "POTBOT_SETUP_TOKEN"}
        and value not in {admin_dsn, setup_token}
    }
    result["POTBOT_DSN"] = application_dsn
    return result


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import hashlib
    import socket
    import sys

    from potbot.credentials import CredentialStore, credential_profile, resolve_postgres_dsn
    from potbot.domain.errors import DomainError, InvalidInputError
    from potbot.domain.serialization import content_hash
    from potbot.models.hosted_policy import HostedPolicy
    from potbot.persistence.database import Database

    parser = argparse.ArgumentParser(prog="potbot remote-workspace supervise-node")
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--owner-principal-id", required=True, type=UUID)
    parser.add_argument("--node-id", required=True, type=UUID)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--provider-policy", required=True, type=Path)
    parser.add_argument("--credential-profile", default=None)
    parser.add_argument("--schema", default="potbot")
    parser.add_argument("--confirm", required=True, action="store_true")
    args = parser.parse_args(argv)
    try:
        store = CredentialStore(args.credential_profile or credential_profile())
        admin_dsn = resolve_postgres_dsn("admin", store=store, interactive=False)
        application_dsn = resolve_postgres_dsn(store=store, interactive=False)
        setup_token = os.environ.get("POTBOT_SETUP_TOKEN") or store.get("api.setup_token")
        if not setup_token:
            raise InvalidInputError("The native setup capability is required")
        context = WorkspaceContext(args.workspace_id, args.owner_principal_id)
        with Database(application_dsn, schema=args.schema).transaction(context):
            pass
        policy = args.provider_policy.resolve(strict=True)
        HostedPolicy.read(policy)
        with policy.open("rb") as stream:
            content = stream.read(65_537)
        if len(content) > 65_536:
            raise InvalidInputError("The native worker policy exceeds its byte allowance")
        policy_sha256 = hashlib.sha256(content).hexdigest()
        with admin_connection(admin_dsn, args.schema) as connection:
            row = connection.execute(
                "SELECT registration FROM remote_nodes WHERE workspace_id=%s AND id=%s",
                (args.workspace_id, args.node_id),
            ).fetchone()
            if (
                not row
                or row["registration"]["hostname"].casefold() != socket.gethostname().casefold()
            ):
                raise StateMismatchError("Run the native supervisor on the registered node host")
        profile = content_hash(
            {
                "schema": "potbot-node-worker/v1",
                "node": str(args.node_id),
                "workspace": str(args.workspace_id),
                "generation": args.generation,
                "policySha256": policy_sha256,
                "credentialProfile": store.profile,
            }
        )
        environment = worker_environment(dict(os.environ), application_dsn, admin_dsn, setup_token)
        environment["POTBOT_CREDENTIAL_PROFILE"] = store.profile

        def launch(worker_id: str) -> Worker:
            with policy.open("rb") as stream:
                current = stream.read(65_537)
            if hashlib.sha256(current).hexdigest() != policy_sha256:
                raise StateMismatchError("The reviewed native worker policy changed")
            return WorkerProcess(
                [
                    sys.executable,
                    "-m",
                    "potbot.api.jobs_cli",
                    "worker",
                    "--workspace-id",
                    str(context.workspace_id),
                    "--principal-id",
                    str(context.principal_id),
                    "--schema",
                    args.schema,
                    "--provider-policy",
                    str(policy),
                    "--worker-id",
                    worker_id,
                ],
                worker_id,
                env=environment,
            )

        supervisor = NativeNodeSupervisor(
            admin_dsn,
            args.schema,
            context,
            args.node_id,
            args.generation,
            profile,
            setup_token,
            launch,
        )
        supervisor.start()
        print(json.dumps({"nodeId": str(args.node_id), "status": "supervising"}), flush=True)
        try:
            while True:
                supervisor.tick()
                time.sleep(0.5)
        except KeyboardInterrupt:
            supervisor.begin_shutdown()
            while not supervisor.close():
                try:
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    pass  # Uncertain work is fenced; it is never reported stopped.
        return 0
    except DomainError as error:
        print(json.dumps({"error": error.to_dict()}), flush=True)
    except Exception:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "capability_unavailable",
                        "message": "Native node supervision unavailable; inspect its protected state.",
                    }
                }
            ),
            flush=True,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
