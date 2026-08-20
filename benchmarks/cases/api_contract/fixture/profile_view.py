from __future__ import annotations

from profile_service import get_profile


def render_profile(user_id: int) -> str:
    profile = get_profile(user_id)
    status = "active" if profile["active"] else "inactive"
    return f"{profile['name']} ({profile['id']}): {status}"
