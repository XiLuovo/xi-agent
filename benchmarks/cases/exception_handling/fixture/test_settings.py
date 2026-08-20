import unittest

from settings import SettingsError, parse_retry_limit


class SettingsTests(unittest.TestCase):
    def test_retry_limit_error_contract(self) -> None:
        with self.assertRaisesRegex(SettingsError, "^missing retry_limit$"):
            parse_retry_limit({})
        with self.assertRaisesRegex(
            SettingsError,
            "^retry_limit must be an integer$",
        ):
            parse_retry_limit({"retry_limit": "many"})
        with self.assertRaisesRegex(
            SettingsError,
            "^retry_limit must be non-negative$",
        ):
            parse_retry_limit({"retry_limit": -1})
        self.assertEqual(parse_retry_limit({"retry_limit": "3"}), 3)


if __name__ == "__main__":
    unittest.main()
