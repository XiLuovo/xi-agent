from __future__ import annotations

from decimal import Decimal


RATES = {
    "standard": Decimal("0.00"),
    "member": Decimal("0.10"),
    "vip": Decimal("0.20"),
}


def discount_rate(customer_tier: str) -> Decimal:
    try:
        return RATES[customer_tier]
    except KeyError as exc:
        raise ValueError(f"unknown customer tier: {customer_tier}") from exc
