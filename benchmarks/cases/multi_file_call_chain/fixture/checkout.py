from __future__ import annotations

from typing import Sequence

from catalog import product_price
from pricing import calculate_total


def build_checkout_summary(
    skus: Sequence[str],
    customer_tier: str,
) -> str:
    prices = [product_price(sku) for sku in skus]
    total = calculate_total(prices, customer_tier)
    return f"{len(skus)} items / {total:.2f}"
