from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from discounts import discount_rate


CENT = Decimal("0.01")


def calculate_total(
    unit_prices: Iterable[Decimal],
    customer_tier: str,
) -> Decimal:
    subtotal = sum(unit_prices, start=Decimal("0"))
    rate = discount_rate("standard")
    return (subtotal * (Decimal("1") - rate)).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )
