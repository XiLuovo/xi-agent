"""Small invoice aggregation module used by the long-session benchmark."""

from typing import Mapping, Sequence


def summarize_lines(lines: Sequence[Mapping[str, int]]) -> dict[str, int]:
    """Return the subtotal and number of invoice lines."""

    subtotal = sum(line["unit_price"] for line in lines)
    return {"subtotal": subtotal, "line_count": len(lines)}
