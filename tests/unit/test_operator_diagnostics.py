"""Managed operator failures identify code without disclosing runtime data."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from potbot.domain.errors import InvalidInputError, StateMismatchError
from potbot.tools import operator_cli


def _raiser(filename: str, exception: Exception):
    namespace = {"failure": exception}
    # An ordinary traceback formatter could copy the source text or locals.
    exec(compile(
        "def fail(*args, **kwargs):\n"
        "    password = 'LOCAL-SECRET-DO-NOT-LOG'\n"
        "    raise failure\n", filename, "exec",
    ), namespace)
    return namespace["fail"]


def test_context_startup_reports_safe_type_stage_and_package_location(monkeypatch, capsys):
    from potbot.context import operator_bootstrap

    paths = SimpleNamespace(workspace_id="workspace")
    monkeypatch.setattr(operator_cli, "parse_manifest", lambda _: paths)
    filename = str(Path(operator_bootstrap.__file__))
    monkeypatch.setattr(
        operator_bootstrap, "default_context_manager",
        _raiser(filename, ModuleNotFoundError("postgresql://user:DSN-SECRET@host/database")),
    )
    assert operator_cli.main(["run", "--manifest", "/private/ARGUMENT-SECRET.json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    event = json.loads(output.err)
    assert event["operator"] == "failed"
    assert event["error_code"] == "internal_error"
    assert event["stage"] == "context-bootstrap"
    assert event["exception_type"] == "ModuleNotFoundError"
    assert event["frames"][-1] == {
        "file": "potbot/context/operator_bootstrap.py", "function": "fail", "line": 3,
    }
    assert not event["frames_truncated"]
    assert all(value not in output.err for value in (
        "DSN-SECRET", "LOCAL-SECRET", "ARGUMENT-SECRET", "postgresql://", filename,
    ))


@pytest.mark.parametrize("exception", [
    InvalidInputError("secret manifest path"), StateMismatchError("secret lease metadata"),
])
def test_all_domain_failures_keep_their_existing_error_code(monkeypatch, capsys, exception):
    monkeypatch.setattr(operator_cli, "parse_manifest", _raiser("/private/SECRET.py", exception))
    assert operator_cli.main(["--manifest", "/private/manifest.json"]) == 1
    event = json.loads(capsys.readouterr().err)
    assert event["error_code"] == exception.code.value
    assert event["stage"] == "manifest-validation"
    assert "secret" not in json.dumps(event).lower()
    assert all(frame["file"].startswith("potbot/") for frame in event["frames"])


def test_runtime_failure_never_calls_exception_string_or_repr(monkeypatch, capsys):
    class PrivateError(Exception):
        def __str__(self):
            pytest.fail("Exception messages must not be formatted")

        def __repr__(self):
            pytest.fail("Exception representations must not be formatted")

    monkeypatch.setattr(operator_cli, "parse_manifest", lambda _: SimpleNamespace())
    monkeypatch.setattr(operator_cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(
        operator_cli, "run_operator", _raiser("/private/SECRET.py", PrivateError("secret")),
    )
    assert operator_cli.main(["--manifest", "/manifest.json", "--without-context"]) == 1
    event = json.loads(capsys.readouterr().err)
    assert event["stage"] == "operator-runtime"
    assert event["exception_type"] == "PrivateError"
    assert "SECRET" not in json.dumps(event)


def test_frame_count_and_output_size_are_bounded():
    namespace = {}
    exec(compile(
        "def recurse(depth):\n"
        "    if depth: return recurse(depth - 1)\n"
        "    raise RuntimeError('SECRET')\n",
        str(Path(operator_cli.__file__)), "exec",
    ), namespace)
    try:
        namespace["recurse"](30)
    except RuntimeError as error:
        event = operator_cli._failure_event(error, "operator-runtime")
    assert len(event["frames"]) == 8
    assert event["frames_truncated"]
    assert len(json.dumps(event)) < 2048
    assert "SECRET" not in json.dumps(event)


def test_non_code_exception_type_metadata_is_not_published():
    exception = type("DSN=PRIVATE-TOKEN", (Exception,), {})()
    event = operator_cli._failure_event(exception, "operator-runtime")
    assert event["exception_type"] == "Exception"
    assert "PRIVATE-TOKEN" not in json.dumps(event)


def test_oversized_code_filename_cannot_overflow_the_supervisor_console_record():
    filename = str(Path(operator_cli.__file__).parent / ("SOURCE_SECRET_" * 1000 + ".py"))
    try:
        _raiser(filename, RuntimeError("secret"))()
    except RuntimeError as error:
        event = operator_cli._failure_event(error, "operator-runtime")
    assert event["frames"] == []
    assert len(json.dumps(event)) < 512
    assert "SOURCE_SECRET" not in json.dumps(event)


def test_success_event_and_exit_code_remain_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(operator_cli, "parse_manifest", lambda _: SimpleNamespace(workspace_id="ws"))
    monkeypatch.setattr(operator_cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(operator_cli, "run_operator", lambda *args, **kwargs: None)
    assert operator_cli.main(["--manifest", "/manifest.json", "--without-context"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"operator": "stopped", "workspace_id": "ws"}
    assert output.err == ""
