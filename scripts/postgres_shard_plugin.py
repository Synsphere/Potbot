"""Explicit pytest plugin: partition the original PostgreSQL lane and retain proof."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from potbot.evaluation.postgres_test_shards import SELECTOR, PostgresShard, collection_receipt


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("potbot-postgres-partitions")
    group.addoption("--postgres-shard", default=None)
    group.addoption("--postgres-shard-report", type=Path, default=None)


def pytest_configure(config: pytest.Config) -> None:
    value = config.getoption("postgres_shard")
    destination = config.getoption("postgres_shard_report")
    if value is None and destination is None:
        return
    if value is None or destination is None:
        raise pytest.UsageError("PostgreSQL partition and report must be selected together")
    if config.option.markexpr != SELECTOR or config.option.keyword:
        raise pytest.UsageError(
            "PostgreSQL partitioning requires the original selector and no -k filter"
        )
    try:
        shard = PostgresShard.parse(value)
        revision = os.environ.get("GITHUB_SHA")
        if revision is None:
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (ValueError, OSError, subprocess.CalledProcessError):
        raise pytest.UsageError(
            "Cannot resolve PostgreSQL partition or exact source revision"
        ) from None
    if destination.exists() or destination.is_symlink():
        raise pytest.UsageError("Refusing to reuse an existing PostgreSQL shard receipt")
    config.pluginmanager.register(
        _ShardRun(shard, destination, revision), "potbot-postgres-shard-run"
    )


class _ShardRun:
    def __init__(self, shard: PostgresShard, destination: Path, revision: str) -> None:
        self.shard, self.destination, self.revision = shard, destination, revision
        self.receipt: dict | None = None
        self.last_bytes: bytes | None = None
        self.outcomes: list[dict[str, str]] = []

    def _write(self) -> None:
        assert self.receipt is not None
        payload = (json.dumps(self.receipt, sort_keys=True, indent=2) + "\n").encode()
        self.destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.last_bytes is None:
            descriptor = os.open(self.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
        else:
            if self.destination.is_symlink() or self.destination.read_bytes() != self.last_bytes:
                raise pytest.UsageError("PostgreSQL shard receipt changed outside its owner")
            descriptor, temporary = tempfile.mkstemp(
                prefix=".postgres-shard-", dir=self.destination.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                os.replace(temporary, self.destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        self.last_bytes = payload

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        try:
            self.receipt = collection_receipt(
                self.shard, [item.nodeid for item in items], self.revision
            )
        except ValueError as error:
            raise pytest.UsageError(str(error)) from None
        chosen = set(self.receipt["selected_nodeids"])
        deselected = [item for item in items if item.nodeid not in chosen]
        items[:] = [item for item in items if item.nodeid in chosen]
        config.hook.pytest_deselected(items=deselected)
        self._write()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call" or report.when == "setup" and not report.passed:
            self.outcomes.append({"nodeid": report.nodeid, "outcome": report.outcome})

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.receipt is not None:
            self.receipt.update(
                completed=True, pytest_exit_code=int(exitstatus), terminal_outcomes=self.outcomes
            )
            self._write()
