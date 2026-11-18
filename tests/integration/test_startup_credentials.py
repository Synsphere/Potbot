"""First-run native-vault credential contracts used by the one-click wizard.

The database credential lifecycle remains separately testable; opening the GUI
is never a prerequisite for validating a PostgreSQL connection.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import pytest

from potbot import credentials
from potbot.domain.errors import InvalidInputError, SourceUnavailableError

from .conftest import AppEnvironment

pytestmark = pytest.mark.postgres


class ContractVault:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[service, username] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.values[service, username]


def test_first_use_saves_verified_connection_and_reuses_it(app_env: AppEnvironment, monkeypatch) -> None:
    vault = ContractVault()
    prompts: list[str] = []
    dsn = app_env.db.dsn
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    monkeypatch.setenv(credentials.PROFILE_ENV, "startup-integration")
    monkeypatch.setattr(credentials, "native_backend", lambda: vault)
    monkeypatch.setattr(credentials, "_interactive_allowed", lambda *_: True)
    def entered(label: str) -> str:
        prompts.append(label)
        return dsn
    monkeypatch.setattr(credentials, "_hidden_prompt", entered)
    assert credentials.resolve_postgres_dsn(validator=credentials.validate_connection) == dsn
    assert len(prompts) == 1
    assert vault.values == {
        (credentials.SERVICE_NAME, "startup-integration:postgres.application"): dsn,
    }
    monkeypatch.setattr(credentials, "_interactive_allowed", lambda *_: False)
    assert credentials.resolve_postgres_dsn() == dsn
    assert len(prompts) == 1
    monkeypatch.setenv(credentials.PROFILE_ENV, "missing-headless")
    with pytest.raises(InvalidInputError):
        credentials.resolve_postgres_dsn()
    monkeypatch.setenv("POTBOT_DSN", dsn)
    assert credentials.resolve_postgres_dsn() == dsn
    assert len(vault.values) == 1


def test_failed_connection_never_persists_url_or_password(app_env: AppEnvironment, monkeypatch, capsys) -> None:
    vault = ContractVault()
    original = urlsplit(app_env.db.dsn)
    dsn = urlunsplit(original._replace(path="/potbot_test_startup_nonexistent_database"))
    monkeypatch.delenv("POTBOT_DSN", raising=False)
    monkeypatch.setenv(credentials.PROFILE_ENV, "startup-failure-integration")
    monkeypatch.setattr(credentials, "native_backend", lambda: vault)
    monkeypatch.setattr(credentials, "_interactive_allowed", lambda *_: True)
    monkeypatch.setattr(credentials, "_hidden_prompt", lambda _: dsn)
    with pytest.raises(SourceUnavailableError):
        credentials.resolve_postgres_dsn(validator=credentials.validate_connection)
    assert vault.values == {}
    captured = capsys.readouterr()
    assert dsn not in captured.err + captured.out
    assert original.password not in captured.err + captured.out
