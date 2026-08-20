from decimal import Decimal
import unittest

from order_total import calculate_order_total


class OrderTotalTests(unittest.TestCase):
    def test_total_includes_each_items_quantity(self) -> None:
        items = [
            {"unit_price": "19.90", "quantity": 2},
            {"unit_price": "5.50", "quantity": 3},
        ]

        self.assertEqual(calculate_order_total(items), Decimal("56.30"))


if __name__ == "__main__":
    unittest.main()
