import unittest

from profile_service import get_profile
from profile_view import render_profile


class ProfileContractTests(unittest.TestCase):
    def test_provider_and_consumer_share_the_same_contract(self) -> None:
        profile = get_profile(7)

        self.assertIs(type(profile["active"]), bool)
        self.assertEqual(render_profile(7), "Ada Lovelace (7): active")


if __name__ == "__main__":
    unittest.main()
