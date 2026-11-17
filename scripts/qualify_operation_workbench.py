#!/usr/bin/env python3
"""Opt-in check/run acceptance against an already configured loopback worker API.

The operator supplies one existing catalogue operation and exact JobSpec. This
command submits one check and at most one explicitly approved run, then reads
only those owned IDs. It never claims a queue job, starts a worker, provisions a
runtime or retries a mutation. Hosted requests additionally require paid-call
and disclosure approval. A private, exclusive JSONL journal records intent and
IDs before each next action, including ambiguous submission outcomes.

Success proves the observed workbench lifecycle and retained result, not blanket
production qualification, model correctness or an independent native-host audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from potbot.domain.auth import WorkspaceContext
from potbot.domain.jobs import CHECK_JOB, COG_JOB, TERMINAL_JOB_STATES
from potbot.domain.operation_workbench import (
    SCHEMA as WORKBENCH_SCHEMA,
)
from potbot.domain.operation_workbench import (
    canonical_uuid,
    operation_id,
    parse_check_report,
    parse_request,
    report_time,
    request_digest,
)
from potbot.domain.serialization import canonical_json, content_hash
from potbot.evaluation.sandbox_worker_acceptance import read_private, unique_json

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "potbot-operation-workbench-qualification/v1"
SOURCES = (
    "scripts/qualify_operation_workbench.py",
    "src/potbot/controller/operation_workbench.py",
    "src/potbot/controller/operation_checks.py",
    "src/potbot/controller/job_runner.py",
    "src/potbot/controller/jobs.py",
    "src/potbot/controller/native_cog_dispatch.py",
    "src/potbot/models/hosted_gateway.py",
    "src/potbot/domain/operation_workbench.py",
    "src/potbot/api/ui_operation_workbench.py",
    "src/potbot/persistence/operation_workbench_schema.py",
)


def source_identity(root: Path = ROOT) -> dict[str, Any]:
    """Bind the client checkout without claiming the remote worker's source."""

    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", *args], cwd=root, timeout=10, stderr=subprocess.DEVNULL
        )

    commit = git("rev-parse", "HEAD").decode("ascii").strip()
    if re.fullmatch(r"[a-f0-9]{40}", commit) is None:
        raise ValueError("Committed client source is required")
    hashes = {}
    for name in SOURCES:
        path = root / name
        committed = git("show", f"{commit}:{name}")
        if path.resolve(strict=True) != path or path.read_bytes() != committed:
            raise ValueError("Qualification client differs from its committed source")
        hashes[name] = hashlib.sha256(committed).hexdigest()
    return {
        "clientCommit": commit,
        "clientSources": hashes,
        "clientSourcesSha256": content_hash(hashes),
        "workerSourceAttested": False,
    }


def loopback_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parts.port is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError("An explicit loopback HTTP API origin is required")
    return f"http://{parts.netloc}"


class Journal:
    """Append-only private receipt, created before the first HTTP mutation."""

    def __init__(self, path: Path) -> None:
        parent = path.parent
        if not path.is_absolute() or parent.resolve(strict=True) != parent:
            raise ValueError("A real private report directory is required")
        info = parent.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("The report directory must be owned and private")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, "w", encoding="utf-8")
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def __call__(self, value: dict[str, Any]) -> None:
        raw = canonical_json(value)
        if len(raw.encode()) > 65536:
            raise ValueError("Qualification receipt exceeds its bound")
        self.stream.write(raw + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())

    def close(self) -> None:
        self.stream.close()


def qualify(
    client: Any,
    actor: WorkspaceContext,
    identity: str,
    request: object,
    *,
    key: str,
    source: dict[str, Any],
    journal: Callable[[dict[str, Any]], None],
    confirm_run: bool,
    allow_paid: bool = False,
    confirm_disclosure: bool = False,
    timeout_seconds: int = 300,
    poll_seconds: float = 1.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Exercise only the exact requested operation; never drive a queue worker."""
    spec = parse_request(request)
    identity = operation_id(identity)
    if (
        not confirm_run
        or (spec.job_type == "model.generate" and not (allow_paid and confirm_disclosure))
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,180}", key) is None
        or type(timeout_seconds) is not int
        or not 5 <= timeout_seconds <= 900
        or not 0.1 <= poll_seconds <= 5
    ):
        raise ValueError("Explicit bounded run approval and a stable request key are required")
    digest = request_digest(spec.to_dict())
    state: dict[str, Any] = {
        "schema": SCHEMA,
        "workspaceId": str(actor.workspace_id),
        "principalId": str(actor.principal_id),
        "operationId": identity,
        "jobType": spec.job_type,
        "episodeId": str(spec.episode_id),
        "requestSha256": digest,
        "checkKey": key + ":check",
        "runKey": key + ":run",
        "checkJobId": None,
        "runJobId": None,
        "startedAt": datetime.now(UTC).isoformat(),
        "source": source,
        "qualifiedSmoke": False,
        "qualifiedForProduction": False,
        "automaticRetries": 0,
    }
    base = f"/v1/ui/operations/{identity}"
    deadline = clock() + timeout_seconds

    def record(phase: str) -> None:
        state["phase"] = phase
        journal(json.loads(canonical_json(state)))

    def exchange(method: str, suffix: str, payload: object = None, attempt: str | None = None):
        if clock() >= deadline:
            raise TimeoutError("The bounded observation window ended")
        options: dict[str, Any] = {}
        if attempt is not None:
            options.update(json=payload, headers={"Idempotency-Key": attempt})
        with client.stream(method, base + suffix, **options) as response:
            if response.status_code not in ({202} if method == "POST" else {200}):
                raise ValueError("The workbench API refused the exact request")
            raw = bytearray()
            for part in response.iter_bytes():
                if clock() >= deadline:
                    raise TimeoutError("The bounded observation window ended")
                raw.extend(part)
                if len(raw) > 1_048_576:
                    raise ValueError("Workbench response exceeds its bound")
        value = unique_json(bytes(raw), maximum=1_048_576)
        if (
            value.get("schema") != WORKBENCH_SCHEMA
            or value.get("workspaceId") != str(actor.workspace_id)
            or value.get("principalId") != str(actor.principal_id)
            or value.get("operationId") != identity
        ):
            raise ValueError("Workbench response scope changed")
        return value

    def owned(value: dict[str, Any], kind: str, expected: str | None = None) -> dict[str, Any]:
        job = value.get("job")
        if (
            not isinstance(job, dict)
            or job.get("job_type") != kind
            or job.get("owner_principal_id") != str(actor.principal_id)
            or job.get("episode_id") != str(spec.episode_id)
            or (expected is not None and job.get("id") != expected)
            or job.get("state") not in {*TERMINAL_JOB_STATES, "queued", "running", "cancelling"}
            or (
                kind != CHECK_JOB
                and (
                    job.get("operation_check_id") != state["checkJobId"]
                    or job.get("selected_models") != spec.selected_models
                )
            )
        ):
            raise ValueError("Workbench job identity changed")
        canonical_uuid(job.get("id"))
        canonical_uuid(job.get("contract_revision_id"))
        return job

    def wait(job: dict[str, Any], kind: str) -> dict[str, Any]:
        while job["state"] not in TERMINAL_JOB_STATES:
            if clock() >= deadline:
                raise TimeoutError("The bounded observation window ended")
            sleep(poll_seconds)
            job = owned(exchange("GET", f"/jobs/{job['id']}"), kind, job["id"])
        return job

    record("intent_recorded")
    try:
        contract = exchange("GET", "/contract")
        if contract.get("supported") is not True or contract.get("jobType") != spec.job_type:
            raise ValueError("The selected catalogue entry has no matching job route")
        record("submitting_check")
        checked = owned(exchange("POST", "/checks", spec.to_dict(), state["checkKey"]), CHECK_JOB)
        state["checkJobId"] = checked["id"]
        record("observing_check")
        checked = wait(checked, CHECK_JOB)
        state["checkState"] = checked["state"]
        if checked["state"] != "succeeded" or checked.get("dispatched_at") is not None:
            record("check_failed")
            return state
        report = parse_check_report((checked.get("result") or {}).get("operation_check"))
        if (
            report["operationId"] != identity
            or report["bindings"]["requestSha256"] != digest
            or report["bindings"]["contractRevisionId"] != checked["contract_revision_id"]
            or not report_time(report["checkedAt"])
            <= datetime.now(UTC)
            < report_time(report["expiresAt"])
        ):
            raise ValueError("The retained check is stale or differs from this request")
        state["check"] = report
        if not report["eligible"]:
            record("check_blocked")
            return state
        required = {"policySha256"}
        if spec.job_type == COG_JOB:
            required |= {
                "profileSha256",
                "installationSha256",
                "repositorySha256",
                "grantGeneration",
                "grantSha256",
                "runtimeSha256",
                "nativeSha256",
            }
        if not required <= report["bindings"].keys():
            raise ValueError("The worker omitted a required execution binding")
        record("submitting_run")
        run = owned(
            exchange(
                "POST",
                "/runs",
                {
                    "checkJobId": checked["id"],
                    "request": spec.to_dict(),
                    "confirm": True,
                },
                state["runKey"],
            ),
            spec.job_type,
        )
        state["runJobId"] = run["id"]
        record("observing_run")
        run = wait(run, spec.job_type)
        state["runState"] = run["state"]
        if run["state"] != "succeeded" or run.get("dispatched_at") is None:
            record("run_failed")
            return state
        result = exchange("GET", f"/jobs/{run['id']}/result")
        terminal = owned(result, spec.job_type, run["id"])
        if (
            terminal["state"] != "succeeded"
            or terminal["contract_revision_id"] != checked["contract_revision_id"]
        ):
            raise ValueError("Retained execution outcome changed")
        output = result.get("output")
        if not isinstance(output, str) or result.get("truncated") is not False:
            raise ValueError("Complete retained execution output is required")
        evidence: dict[str, Any] = {"outputSha256": hashlib.sha256(output.encode()).hexdigest()}
        if spec.job_type == COG_JOB:
            artifact, text = result.get("artifact"), result.get("artifactText")
            if (
                not isinstance(artifact, dict)
                or not isinstance(text, str)
                or result.get("artifactTruncated") is not False
                or artifact.get("bytes") != len(text.encode())
                or artifact.get("sha256") != hashlib.sha256(text.encode()).hexdigest()
                or artifact.get("id") != (terminal.get("result") or {}).get("artifact_id")
            ):
                raise ValueError("Native artifact bytes failed their retained digest")
            canonical_uuid(artifact.get("id"))
            evidence["artifact"] = {key: artifact[key] for key in ("id", "sha256", "bytes")}
        elif not output or (terminal.get("result") or {}).get("completed_models") != 1:
            raise ValueError("One completed hosted result is required")
        cursor, event_types = 0, {}
        for _ in range(128):
            page = exchange("GET", f"/jobs/{run['id']}/events?after={cursor}&limit=8")
            rows = page.get("items")
            if (
                not isinstance(rows, list)
                or len(rows) > 8
                or type(page.get("has_more")) is not bool
            ):
                raise ValueError("Invalid retained event page")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or row.get("job_id") != run["id"]
                    or type(row.get("sequence")) is not int
                    or row["sequence"] <= cursor
                    or not isinstance(row.get("event_type"), str)
                    or re.fullmatch(r"job\.[a-z_]+", row["event_type"]) is None
                ):
                    raise ValueError("Retained event ownership or order changed")
                cursor = row["sequence"]
                kind = row["event_type"]
                event_types[kind] = event_types.get(kind, 0) + 1
            if page.get("next_cursor") != cursor or (page["has_more"] and not rows):
                raise ValueError("Retained event cursor changed")
            if not page["has_more"]:
                break
        else:
            raise ValueError("Retained events exceeded the bounded observation window")
        if not event_types.get("job.running") or not event_types.get("job.succeeded"):
            raise ValueError("Durable execution lifecycle events are missing")
        state.update(result=evidence, eventTypes=event_types, qualifiedSmoke=True)
        record("complete")
    except Exception as error:
        # No exception text, endpoint response, prompt, output or credential enters receipts.
        state["qualifiedSmoke"] = False
        state["failureClass"] = type(error).__name__
        previous = state["phase"]
        state["stoppedDuring"] = previous
        state["phase"] = (
            "submission_uncertain" if previous.startswith("submitting_") else "observation_failed"
        )
        try:
            journal(json.loads(canonical_json(state)))
        except OSError:
            pass  # Known IDs remain available on stdout if the private disk fails.
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True, type=loopback_url)
    parser.add_argument("--origin", required=True, help="already allowed operator UI origin")
    parser.add_argument("--workspace-id", required=True, type=UUID)
    parser.add_argument("--principal-id", required=True, type=UUID)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--input", required=True, type=Path, help="private exact JobSpec JSON")
    parser.add_argument("--key", required=True, help="stable qualification request key")
    parser.add_argument("--report", required=True, type=Path, help="new private JSONL journal")
    parser.add_argument("--confirm-run", action="store_true", required=True)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--confirm-prompt-disclosure", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    journal = None
    try:
        import httpx

        from potbot.service_credentials import ServiceCredentials

        origin = urlsplit(args.origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
            or len(args.origin) > 2048
        ):
            raise ValueError("An exact configured UI origin is required")
        request = unique_json(read_private(args.input.absolute()), maximum=16384)
        source = source_identity()
        credentials = ServiceCredentials()
        bearer = credentials.resolve("secret://api/access-token")
        setup = credentials.store.get("api.setup_token")
        if not isinstance(setup, str) or re.fullmatch(r"[!-~]{32,4096}", setup) is None:
            raise ValueError("Current native operator setup capability is required")
        journal = Journal(args.report.absolute())
        with httpx.Client(
            base_url=args.api_url,
            headers={
                "Authorization": "Bearer " + bearer,
                "X-Potbot-Setup-Token": setup,
                "Origin": args.origin,
                "Accept": "application/json",
            },
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            result = qualify(
                client,
                WorkspaceContext(args.workspace_id, args.principal_id),
                args.operation_id,
                request,
                key=args.key,
                source=source,
                journal=journal,
                confirm_run=args.confirm_run,
                allow_paid=args.allow_paid,
                confirm_disclosure=args.confirm_prompt_disclosure,
                timeout_seconds=args.timeout_seconds,
            )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["qualifiedSmoke"] else 1
    except Exception as error:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "qualifiedSmoke": False,
                    "failureClass": type(error).__name__,
                    "phase": "preflight_failed" if journal is None else "observation_failed",
                }
            )
        )
        return 1
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
