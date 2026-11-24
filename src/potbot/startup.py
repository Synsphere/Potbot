"""Common start.* entry point: native desktop locally, preserve remote web and advanced CLI."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

from potbot.domain.errors import InvalidInputError


def _local_ui_mode(
    environment: Mapping[str, str] | None = None, platform_name: str | None = None
) -> str:
    environment = os.environ if environment is None else environment
    platform_name = sys.platform if platform_name is None else platform_name
    selected = environment.get("POTBOT_LOCAL_UI", "").strip().lower()
    if selected:
        if selected not in {"native", "web"}:
            raise InvalidInputError("POTBOT_LOCAL_UI must be either 'native' or 'web'.")
        return selected
    if platform_name.startswith("linux") and not (
        environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")
    ):
        return "web"
    return "native"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments == ["start"]:
        if _local_ui_mode() == "native":
            from potbot.desktop import main as desktop_main

            return desktop_main()
        from potbot.oneclick import main as oneclick_main

        return oneclick_main(arguments)
    if arguments in (
        ["stop"],
        ["restart"],
        ["status"],
        ["logs"],
        ["ui-password"],
        ["change-password"],
    ):
        from potbot.oneclick import main as oneclick_main

        return oneclick_main(arguments)
    from potbot.cli import main as cli_main

    return cli_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
