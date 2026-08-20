from __future__ import annotations

from decimal import Decimal


PRICES = {
    "keyboard": Decimal("80.00"),
    "mouse": Decimal("20.00"),
}


def product_price(sku: str) -> Decimal:
    return PRICES[sku]
