from __future__ import annotations


def fetch_user(user_id: int) -> dict[str, object]:
    if user_id != 7:
        raise KeyError(user_id)
    return {
        "user_id": 7,
        "full_name": "Ada Lovelace",
        "enabled": 1,
    }
