from __future__ import annotations

from profile_repository import fetch_user


def get_profile(user_id: int) -> dict[str, object]:
    record = fetch_user(user_id)
    return {
        "user_id": record["user_id"],
        "display_name": record["full_name"],
        "active": "yes" if record["enabled"] else "no",
    }
