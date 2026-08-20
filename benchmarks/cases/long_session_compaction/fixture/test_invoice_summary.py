import unittest

from invoice_summary import summarize_lines


class InvoiceSummaryTests(unittest.TestCase):
    def test_subtotal_includes_quantity_for_each_line(self) -> None:
        lines = [
            {"unit_price": 12, "quantity": 3},
            {"unit_price": 5, "quantity": 2},
        ]
        self.assertEqual(
            summarize_lines(lines),
            {"subtotal": 46, "line_count": 2},
        )

    def test_empty_invoice_has_zero_subtotal(self) -> None:
        self.assertEqual(
            summarize_lines([]),
            {"subtotal": 0, "line_count": 0},
        )


if __name__ == "__main__":
    unittest.main()
