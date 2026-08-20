import unittest

from checkout import build_checkout_summary


class CheckoutTests(unittest.TestCase):
    def test_vip_discount_reaches_public_checkout_summary(self) -> None:
        summary = build_checkout_summary(["keyboard", "mouse"], "vip")

        self.assertEqual(summary, "2 items / 80.00")


if __name__ == "__main__":
    unittest.main()
