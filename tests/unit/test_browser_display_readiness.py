"""Native mapping checks use only bounded read-only synthetic-window inspection."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from scripts import browser_display_readiness as display

TITLE = "potbot-rendered-" + "a" * 32


def test_logical_title_is_insufficient_until_its_native_window_is_viewable(monkeypatch):
    calls = []
    outputs = [
        f' 0x100 "{TITLE} - Chromium": ()',
        "Map State: IsUnMapped",
        f' 0x100 "{TITLE} - Chromium": ()',
        "Map State: IsViewable",
    ]

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=outputs.pop(0).encode())

    monkeypatch.setattr(display.subprocess, "run", run)
    monkeypatch.setattr(display.time, "sleep", lambda _: None)
    result = display.wait_for_mapped_test_window(TITLE)
    assert result["map_state"] == "IsViewable" and len(result["title_sha256"]) == 64
    assert [command for command, _ in calls] == [
        ["xwininfo", "-root", "-tree"],
        ["xwininfo", "-id", "0x100"],
        ["xwininfo", "-root", "-tree"],
        ["xwininfo", "-id", "0x100"],
    ]
    assert all(0 < options["timeout"] <= 1.0 and options["check"] for _, options in calls)


def test_another_browser_window_cannot_supply_readiness(monkeypatch):
    monkeypatch.setattr(display, "_query", lambda *_: f'0x1 "{TITLE}other - Chromium": ()')
    ticks = iter((0, 0, 4, 4))
    monkeypatch.setattr(display.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(display.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="did not become viewable"):
        display.wait_for_mapped_test_window(TITLE)


def test_multiple_matching_windows_are_refused(monkeypatch):
    monkeypatch.setattr(
        display, "_query", lambda *_: f'0x1 "{TITLE}": ()\n0x2 "{TITLE} - Chromium": ()'
    )
    with pytest.raises(RuntimeError, match="ambiguous"):
        display.wait_for_mapped_test_window(TITLE)


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("xwininfo", 1)])
def test_missing_or_hung_native_dependency_fails_instead_of_skipping(monkeypatch, failure):
    def unavailable(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(display.subprocess, "run", unavailable)
    with pytest.raises(type(failure)):
        display.wait_for_mapped_test_window(TITLE)


def test_unbounded_native_output_is_refused(monkeypatch):
    monkeypatch.setattr(
        display.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=b"x" * 65537)
    )
    with pytest.raises(RuntimeError, match="exceeded"):
        display.wait_for_mapped_test_window(TITLE)


@pytest.mark.parametrize("title", ["", "arbitrary real browser", TITLE + "; cmd", TITLE.upper()])
def test_only_fixed_synthetic_title_shape_can_reach_native_inspection(title, monkeypatch):
    monkeypatch.setattr(
        display.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("unexpected native query")
    )
    with pytest.raises(ValueError):
        display.wait_for_mapped_test_window(title)
