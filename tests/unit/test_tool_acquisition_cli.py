"""The host operator command exposes no execution dependencies to --help."""

import builtins
from uuid import uuid4

import pytest

from potbot.cli import main


@pytest.mark.parametrize(
    "command",
    [
        "recipes",
        "list",
        "publish-recipe",
        "request",
        "inspect",
        "events",
        "approve-build",
        "approve",
        "reject",
        "rollback",
        "reconcile",
        "repin",
        "postflight",
        "cancel",
        "run-once",
    ],
)
def test_help_does_not_open_vault_database_network_or_native_worker(command, monkeypatch, capsys):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith(
            (
                "potbot.credentials",
                "potbot.persistence.database",
                "potbot.tools.tool_acquisition_worker",
                "potbot.tools.tool_sources",
                "potbot.tools.ai_workspace_operator",
                "psycopg",
                "keyring",
                "fastapi",
            )
        ):
            raise AssertionError("Help loaded a native execution or secret dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    with pytest.raises(SystemExit) as result:
        main(
            [
                "tool-acquisition",
                "--workspace",
                str(uuid4()),
                "--principal",
                str(uuid4()),
                command,
                "--help",
            ]
        )
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_confirmation_and_review_digest_are_required_before_dependencies():
    with pytest.raises(SystemExit) as result:
        main(
            [
                "tool-acquisition",
                "--workspace",
                str(uuid4()),
                "--principal",
                str(uuid4()),
                "approve-build",
                str(uuid4()),
            ]
        )
    assert result.value.code == 2
