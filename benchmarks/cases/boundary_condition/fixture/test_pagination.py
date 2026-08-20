import unittest

from pagination import page_count


class PaginationTests(unittest.TestCase):
    def test_empty_exact_and_partial_page_boundaries(self) -> None:
        self.assertEqual(page_count(0, 10), 0)
        self.assertEqual(page_count(20, 10), 2)
        self.assertEqual(page_count(21, 10), 3)


if __name__ == "__main__":
    unittest.main()
