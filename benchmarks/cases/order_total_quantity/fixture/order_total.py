from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping


CENT = Decimal("0.01")


def calculate_order_total(items: Iterable[Mapping[str, object]]) -> Decimal:
    """Return the sum of all order lines, rounded to currency precision."""

    total = sum(
        (Decimal(str(item["unit_price"])) for item in items),
        start=Decimal("0"),
    )
    return total.quantize(CENT, rounding=ROUND_HALF_UP)
