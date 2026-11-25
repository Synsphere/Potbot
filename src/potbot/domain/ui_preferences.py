"""Pure shared preference validation for native clients and HTTP adapters."""

from __future__ import annotations

from typing import Any

from potbot.domain.errors import InvalidInputError

# Preferences are deliberately non-secret appearance settings, never executable
# configuration, credentials, endpoints or access-control overrides.
PREFERENCES = {
    "theme": {"system", "dark", "light"},
    "graphDisplay": {"3d", "list"},
}
BOOL_PREFERENCES = frozenset(
    ("reducedMotion", "showGraphDiagnostics", "orbAutoHide", "downloadNotifications")
)


def validate_preferences(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 12:
        raise InvalidInputError("Preferences must be an object of approved UI settings")
    if set(value) - set(PREFERENCES) - BOOL_PREFERENCES - {"xpraMaxFps"}:
        raise InvalidInputError("Unknown preference; private and executable settings are forbidden")
    result: dict[str, Any] = {}
    for key, selection in value.items():
        if key in PREFERENCES:
            if type(selection) is not str or selection not in PREFERENCES[key]:
                raise InvalidInputError("Invalid enumerated UI preference")
        elif key in BOOL_PREFERENCES:
            if type(selection) is not bool:
                raise InvalidInputError("UI preference must be a boolean")
        elif key == "xpraMaxFps":
            if type(selection) is not int or selection not in {10, 15, 24, 30, 60}:
                raise InvalidInputError("Unsupported Xpra render frame limit")
        result[key] = selection
    return result
