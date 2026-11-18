"""Common start.* entry point: run the app by default, preserve advanced CLI."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments in (["start"], ["stop"], ["restart"], ["status"], ["logs"], ["ui-password"], ["change-password"]):
        from potbot.oneclick import main as oneclick_main

        return oneclick_main(arguments)
    # Explicit administrative/diagnostic arguments retain the original CLI,
    # without triggering GUI downloads or PostgreSQL prompting.
    from potbot.cli import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
