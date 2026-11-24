"""Native, user-scoped PostgreSQL install and recovery safety boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot import native_postgres
from potbot.domain.errors import CapabilityUnavailableError, InvalidInputError


def test_native_release_artifacts_are_pinned_and_windows_compatible() -> None:
    assert native_postgres.MAMBA_VERSION == "2.9.0-0"
    assert len(native_postgres.MAMBA_RELEASES["win-64"]) == 64
    assert native_postgres.PORT > 1024
    assert "docker" not in native_postgres.provision_if_needed.__code__.co_names


def test_native_package_process_environment_is_confined(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PGDATA", "not-the-owned-cluster")
    monkeypatch.setenv("CONDA_PREFIX", "not-the-owned-packages")
    monkeypatch.setenv("MAMBA_ROOT_PREFIX", "not-the-owned-root")
    env = native_postgres._package_environment(tmp_path)
    assert "PGDATA" not in env and "CONDA_PREFIX" not in env
    assert env["MAMBA_ROOT_PREFIX"].startswith(str(tmp_path))
    assert env["PATH"].startswith(str(tmp_path / ".potbot" / "native-postgres" / "packages"))


def test_external_operator_database_is_never_adopted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("POTBOT_ADMIN_DSN", "postgresql://external:test@localhost/unrelated")
    monkeypatch.setattr(native_postgres, "is_managed", lambda _root: False)
    monkeypatch.setattr(
        native_postgres, "_ensure_packages", lambda _root: pytest.fail("no install")
    )
    monkeypatch.setattr(
        native_postgres, "CredentialStore", lambda _profile: SimpleNamespace(get=lambda _key: None)
    )
    assert native_postgres.provision_if_needed(tmp_path) is False


def test_existing_owned_cluster_is_restarted_without_reinstallation(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(native_postgres, "is_managed", lambda _root: True)
    monkeypatch.setattr(native_postgres, "ensure_running", calls.append)
    monkeypatch.setattr(
        native_postgres, "_ensure_packages", lambda _root: pytest.fail("no install")
    )
    monkeypatch.setattr(
        native_postgres, "CredentialStore", lambda _profile: SimpleNamespace(get=lambda _key: None)
    )
    assert native_postgres.provision_if_needed(tmp_path)
    assert calls == [tmp_path]


def test_broken_marker_fails_closed_without_modifying_cluster(tmp_path: Path) -> None:
    parent, prefix, data, marker = native_postgres._paths(tmp_path)
    data.mkdir(parents=True)
    (data / "PG_VERSION").write_text("16\n")
    marker.write_text(json.dumps({"schema": "unknown", "port": 55436}), encoding="utf-8")
    with pytest.raises(InvalidInputError, match="differs from"):
        native_postgres._manifest(tmp_path)
    assert (data / "PG_VERSION").is_file()


def test_cluster_creation_refuses_an_existing_data_directory(monkeypatch, tmp_path: Path) -> None:
    parent, _prefix, data, _marker = native_postgres._paths(tmp_path)
    data.mkdir(parents=True)
    monkeypatch.setattr(
        native_postgres, "_ensure_packages", lambda _root: pytest.fail("no install")
    )
    with pytest.raises(InvalidInputError, match="already exists"):
        native_postgres._create_cluster(tmp_path)
    assert data.is_dir()


def test_unknown_postgres_listener_blocks_native_start(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        native_postgres,
        "_tools",
        lambda _prefix: (Path("initdb"), Path("pg_ctl"), Path("pg_config")),
    )
    monkeypatch.setattr(
        native_postgres.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=3)
    )
    monkeypatch.setattr(native_postgres, "_port_in_use", lambda: True)
    monkeypatch.setattr(
        native_postgres, "_command", lambda *a, **k: pytest.fail("should not start")
    )
    with pytest.raises(CapabilityUnavailableError, match="another process"):
        native_postgres._start_cluster(tmp_path, "secret")


def test_managed_password_requires_exact_original_vault_identity(monkeypatch) -> None:
    value = "postgresql://potbot_admin:password@another-host:55436/potbot"
    monkeypatch.setattr(
        native_postgres, "CredentialStore", lambda _profile: SimpleNamespace(get=lambda _key: value)
    )
    with pytest.raises(InvalidInputError, match="differs from"):
        native_postgres._password_from_vault()
