"""Deterministic PostgreSQL test partitions and complete CI coverage receipts.

These receipts check collection/execution accounting inside one CI source run;
they do not independently authenticate a runner or qualify production runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

SELECTOR = "postgres and not sandbox"
SCHEMA = "potbot.postgres-test-shard/1"
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def nodeids_digest(nodeids: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(nodeids), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _nodeids(value: object) -> list[str]:
    _require(isinstance(value, list) and len(value) <= 25000, "Invalid test node collection")
    assert isinstance(value, list)
    _require(
        all(
            isinstance(node, str)
            and 1 <= len(node) <= 4096
            and not any(char in node for char in "\n\r\0")
            for node in value
        ),
        "Invalid test node identity",
    )
    _require(value == sorted(set(value)), "Test identities must be sorted and unique")
    return value


@dataclass(frozen=True, slots=True)
class PostgresShard:
    index: int
    count: int

    def __post_init__(self) -> None:
        _require(
            type(self.index) is int
            and type(self.count) is int
            and 1 <= self.count <= 16
            and 0 <= self.index < self.count,
            "PostgreSQL shard must select one index in a bounded partition",
        )

    @classmethod
    def parse(cls, value: str) -> PostgresShard:
        _require(
            isinstance(value, str) and re.fullmatch(r"[0-9]{1,2}/[0-9]{1,2}", value) is not None,
            "Use zero-based PostgreSQL shard index/count",
        )
        index, count = map(int, value.split("/"))
        shard = cls(index, count)
        _require(value == f"{index}/{count}", "Shard syntax must be canonical")
        return shard

    def includes(self, nodeid: str) -> bool:
        # Independent of process hash seeds, collection order and host pathname.
        return (
            int.from_bytes(hashlib.sha256(nodeid.encode()).digest()[:8], "big") % self.count
            == self.index
        )

    def selected(self, nodeids: list[str]) -> list[str]:
        checked = _nodeids(sorted(nodeids))
        return [node for node in checked if self.includes(node)]


def collection_receipt(
    shard: PostgresShard, nodeids: list[str], source_commit: str
) -> dict[str, Any]:
    _require(
        isinstance(source_commit, str) and _REVISION.fullmatch(source_commit) is not None,
        "Shard evidence requires its exact source commit",
    )
    collected = _nodeids(sorted(nodeids))
    _require(bool(collected), "No PostgreSQL tests were collected")
    selected = shard.selected(collected)
    return {
        "schema": SCHEMA,
        "source_commit": source_commit,
        "selector": SELECTOR,
        "index": shard.index,
        "count": shard.count,
        "collection_count": len(collected),
        "collection_sha256": nodeids_digest(collected),
        "selected_nodeids": selected,
        "selected_sha256": nodeids_digest(selected),
        "completed": False,
        "pytest_exit_code": None,
        "terminal_outcomes": [],
    }


def verify_postgres_shards(reports: list[dict[str, Any]], source_commit: str) -> dict[str, Any]:
    """Refuse lost, overlapping, incomplete, filtered, failed or foreign partitions."""
    _require(
        isinstance(source_commit, str) and _REVISION.fullmatch(source_commit) is not None,
        "Coverage gate requires the expected source commit",
    )
    _require(isinstance(reports, list) and 1 <= len(reports) <= 16, "Missing shard receipts")
    fields = {
        "schema",
        "source_commit",
        "selector",
        "index",
        "count",
        "collection_count",
        "collection_sha256",
        "selected_nodeids",
        "selected_sha256",
        "completed",
        "pytest_exit_code",
        "terminal_outcomes",
    }
    indices: set[int] = set()
    collected_identity: tuple[int, int, str] | None = None
    union: set[str] = set()
    totals = {"passed": 0, "skipped": 0}
    for report in reports:
        _require(isinstance(report, dict) and set(report) == fields, "Invalid shard receipt fields")
        _require(
            report["schema"] == SCHEMA
            and report["source_commit"] == source_commit
            and report["selector"] == SELECTOR,
            "Shard belongs to another source or selector",
        )
        shard = PostgresShard(report["index"], report["count"])
        _require(shard.index not in indices, "Duplicate PostgreSQL shard index")
        indices.add(shard.index)
        _require(
            type(report["collection_count"]) is int
            and 1 <= report["collection_count"] <= 25000
            and isinstance(report["collection_sha256"], str)
            and _DIGEST.fullmatch(report["collection_sha256"]) is not None,
            "Invalid full PostgreSQL collection identity",
        )
        identity = (shard.count, report["collection_count"], report["collection_sha256"])
        _require(
            collected_identity is None or identity == collected_identity,
            "Shard collections differ; coverage cannot be inferred",
        )
        collected_identity = identity
        selected = _nodeids(report["selected_nodeids"])
        _require(
            nodeids_digest(selected) == report["selected_sha256"]
            and all(shard.includes(node) for node in selected),
            "Shard selection differs from its partition",
        )
        _require(not union.intersection(selected), "Overlapping PostgreSQL test selections")
        union.update(selected)
        _require(
            report["completed"] is True
            and type(report["pytest_exit_code"]) is int
            and report["pytest_exit_code"] == 0,
            "PostgreSQL shard did not complete successfully",
        )
        outcomes = report["terminal_outcomes"]
        _require(
            isinstance(outcomes, list) and len(outcomes) == len(selected),
            "Shard lacks terminal outcomes for its selected tests",
        )
        terminal: set[str] = set()
        for item in outcomes:
            _require(
                isinstance(item, dict)
                and set(item) == {"nodeid", "outcome"}
                and isinstance(item["nodeid"], str)
                and item["nodeid"] in selected
                and item["nodeid"] not in terminal
                and item["outcome"] in {"passed", "skipped"},
                "Invalid, duplicated or failed terminal test outcome",
            )
            terminal.add(item["nodeid"])
            totals[item["outcome"]] += 1
        _require(terminal == set(selected), "Selected PostgreSQL tests were not all accounted for")
    assert collected_identity is not None
    count, total, digest = collected_identity
    _require(indices == set(range(count)), "Missing PostgreSQL partition")
    _require(
        len(union) == total and nodeids_digest(sorted(union)) == digest,
        "PostgreSQL shard union differs from the full collection",
    )
    return {
        "schema": "potbot.postgres-shard-coverage/1",
        "source_commit": source_commit,
        "selector": SELECTOR,
        "partitions": count,
        "collected_tests": total,
        "accounted_tests": len(union),
        "collection_sha256": digest,
        "terminal_outcomes": totals,
        "coverage_complete": True,
        "runtime_qualified": False,
    }
