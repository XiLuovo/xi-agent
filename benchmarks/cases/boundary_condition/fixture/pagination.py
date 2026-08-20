from __future__ import annotations


def page_count(total_items: int, page_size: int) -> int:
    """Return how many pages are needed for a collection."""

    if total_items < 0:
        raise ValueError("total_items must be non-negative")
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    return total_items // page_size + 1
