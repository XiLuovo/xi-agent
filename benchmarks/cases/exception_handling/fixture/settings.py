from __future__ import annotations

from typing import Mapping


class SettingsError(ValueError):
    """Raised when application settings violate the public contract."""


def parse_retry_limit(raw: Mapping[str, object]) -> int:
    """Parse a required non-negative retry limit."""

    try:
        value = int(raw["retry_limit"])
    except (KeyError, TypeError, ValueError):
        return 0
    if value < 0:
        return 0
    return value
