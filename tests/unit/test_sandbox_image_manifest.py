"""System image identity manifests contain build facts, not runtime attestation."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "sandbox_image_manifest.py"
    spec = importlib.util.spec_from_file_location("sandbox_image_manifest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _qualifier():
    path = Path(__file__).resolve().parents[2] / "scripts" / "qualify_published_sandbox_image.py"
    spec = importlib.util.spec_from_file_location("qualify_published_sandbox_image", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_immutable_image_manifest_no_false_qualification() -> None:
    make = _module().manifest
    entry = make("ghcr.io/example/potbot-sandbox", "a" * 64, "b" * 40, "linux/arm64")
    assert entry["image_ref"] == "ghcr.io/example/potbot-sandbox@sha256:" + "a" * 64
    assert entry["guest_home"] == "/home"
    assert entry["guest_writable_persistent_mounts"] == ["/home", "/usr/local/tools"]
    assert entry["guest_shared_tool_writable_mount"] == "/usr/local/tools"
    assert entry["guest_writable_ephemeral_mounts"] == ["/var/ai"]
    assert entry["guest_shared_model_readonly_mount"] == "/opt/potbot/models"
    assert entry["rootfs_read_only_required"] is True
    assert entry["runtime_qualified"] is False
    assert entry["signature_verified_by_host"] is False
    assert len(entry["manifest_sha256"]) == 64
    for args in (
        ("../../bad", "a" * 64, "b" * 40, "linux/amd64"),
        ("ghcr.io/example/repo", "bad", "b" * 40, "linux/amd64"),
        ("ghcr.io/example/repo", "a" * 64, "bad", "linux/amd64"),
        ("ghcr.io/example/repo", "a" * 64, "b" * 40, "windows/amd64"),
    ):
        with pytest.raises(ValueError):
            make(*args)


def test_shipped_python_bytes_use_git_lf_canonicalization(tmp_path: Path) -> None:
    source = tmp_path / "worker_probe.py"
    source.write_bytes(b"first\r\nsecond\r\n")
    assert _qualifier()._canonical_source_bytes(source) == b"first\nsecond\n"

    source.write_bytes(b"first\rsecond\n")
    assert _qualifier()._canonical_source_bytes(source) == b"first\rsecond\n"


def test_non_python_shipped_bytes_are_not_rewritten(tmp_path: Path) -> None:
    source = tmp_path / "payload.bin"
    payload = b"first\r\nsecond\r\n"
    source.write_bytes(payload)
    assert _qualifier()._canonical_source_bytes(source) == payload


def test_published_image_failure_summary_names_workspace_failures() -> None:
    qualifier = _qualifier()
    summary = qualifier._failure_summary(
        {"qualified": True},
        {
            "qualified_smoke": False,
            "tests": {
                "network_is_denied": {"passed": False},
                "home_is_writable": {"passed": True},
                "etc_is_read_only": {"passed": False},
            },
        },
        None,
        None,
    )
    assert summary == {
        "stage": "workspace-smoke",
        "failed_tests": ["etc_is_read_only", "network_is_denied"],
    }


def test_published_image_failure_summary_names_operator_checks() -> None:
    qualifier = _qualifier()
    summary = qualifier._failure_summary(
        {"qualified": True},
        {"qualified_smoke": True},
        {
            "qualified": False,
            "checks": {
                "host": {"passed": True},
                "taskstats": {"passed": False},
            },
        },
        None,
    )
    assert summary == {
        "stage": "operator-gate",
        "failed_checks": ["taskstats"],
    }


def test_published_qualifier_verifies_preprovisioned_scratch_without_chown() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "scripts/qualify_published_sandbox_image.py").read_text(
        encoding="utf-8"
    )
    assert "os.chown(tools" not in source
    assert "expected_layout = {" in source
    assert "tools: (os.geteuid(), 69, 0o2775)" in source
    assert "qualification scratch ownership or mode differs" in source
    assert "(home / \".tmp\").mkdir(mode=0o700)" in source


def test_published_qualifier_uses_scoped_taskstats_preflight_when_launcher_is_active(
    monkeypatch,
) -> None:
    qualifier = _qualifier()
    calls = []
    monkeypatch.setenv(
        "POTBOT_OPERATOR_SCOPED_LAUNCHER",
        str(qualifier.DEFAULT_SOCKET),
    )
    monkeypatch.setattr(
        qualifier,
        "qualify_scoped_worker",
        lambda endpoint: calls.append(endpoint),
    )
    monkeypatch.setattr(
        qualifier,
        "qualify_accounting",
        lambda: (_ for _ in ()).throw(AssertionError("raw TASKSTATS must not run")),
    )

    result = qualifier._accounting_gate()
    assert result == {
        "schema": 1,
        "qualified": True,
        "source": "scoped-launcher-preflight",
    }
    assert calls == [qualifier.DEFAULT_SOCKET]


def test_published_qualifier_keeps_direct_taskstats_for_native_linux(monkeypatch) -> None:
    qualifier = _qualifier()
    monkeypatch.delenv("POTBOT_OPERATOR_SCOPED_LAUNCHER", raising=False)
    expected = {
        "schema": 1,
        "qualified": True,
        "source": "linux-generic-netlink-taskstats",
    }
    monkeypatch.setattr(qualifier, "qualify_accounting", lambda: expected)
    monkeypatch.setattr(
        qualifier,
        "qualify_scoped_worker",
        lambda endpoint: (_ for _ in ()).throw(
            AssertionError("scoped launcher must not run")
        ),
    )

    assert qualifier._accounting_gate() == expected


def test_published_qualifier_reports_scoped_taskstats_failure_without_throwing(
    monkeypatch,
) -> None:
    qualifier = _qualifier()
    monkeypatch.setenv(
        "POTBOT_OPERATOR_SCOPED_LAUNCHER",
        str(qualifier.DEFAULT_SOCKET),
    )

    def fail(endpoint):
        raise PermissionError("private details must not escape")

    monkeypatch.setattr(qualifier, "qualify_scoped_worker", fail)
    result = qualifier._accounting_gate()
    assert result == {
        "schema": 1,
        "qualified": False,
        "source": "scoped-launcher-preflight",
        "error": "PermissionError",
    }


def test_taskstats_failure_summary_retains_only_safe_category_fields() -> None:
    qualifier = _qualifier()
    summary = qualifier._failure_summary(
        {
            "qualified": False,
            "source": "scoped-launcher-preflight",
            "error": "CapabilityUnavailableError",
            "private": "/must/not/escape",
        },
        {"qualified_smoke": False},
        None,
        None,
    )
    assert summary == {
        "stage": "taskstats",
        "source": "scoped-launcher-preflight",
        "error": "CapabilityUnavailableError",
    }
