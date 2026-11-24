"""Read-only native-window readiness for an isolated synthetic X11 test display.

Document visibility/focus alone does not establish that the native browser window
is mapped. Never import this test helper in the production worker or use it to
focus, resize, capture, restart or retry a browser command.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import time


def _query(arguments: list[str], remaining: float) -> str:
    result = subprocess.run(
        ["xwininfo", *arguments],
        check=True,
        capture_output=True,
        timeout=min(1.0, remaining),
    )
    if len(result.stdout) > 65536:
        raise RuntimeError("Synthetic display inventory exceeded its bound")
    return result.stdout.decode("utf-8", errors="replace")


def wait_for_mapped_test_window(title: str) -> dict[str, object]:
    """Require exactly one independently mapped native window within three seconds."""
    if re.fullmatch(r"potbot-rendered-[0-9a-f]{32}", title) is None:
        raise ValueError("Native readiness is restricted to uniquely named synthetic windows")
    deadline = time.monotonic() + 3.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Synthetic browser native window did not become viewable")
        tree = _query(["-root", "-tree"], remaining)
        matches = {
            match[1]
            for line in tree.splitlines()
            if (match := re.match(r'\s*(0x[0-9a-fA-F]+) "' + re.escape(title) + r'(?:"| - )', line))
        }
        if len(matches) > 1:
            raise RuntimeError("Synthetic browser native window is ambiguous")
        if matches:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Synthetic browser native window did not become viewable")
            state = _query(["-id", next(iter(matches))], remaining)
            if re.search(r"^\s*Map State: IsViewable\s*$", state, re.MULTILINE):
                return {
                    "map_state": "IsViewable",
                    "title_sha256": hashlib.sha256(title.encode()).hexdigest(),
                }
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
