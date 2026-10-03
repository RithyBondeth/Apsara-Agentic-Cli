"""Independent contract checks; upstream revision is pinned by the suite."""
import unittest

from boltons.iterutils import chunked, chunked_iter


class ChunkContract(unittest.TestCase):
    def test_size_trailing_and_count(self):
        self.assertEqual(chunked(range(7), 3), [[0, 1, 2], [3, 4, 5], [6]])
        self.assertEqual(chunked(range(7), 3, count=2), [[0, 1, 2], [3, 4, 5]])
        self.assertEqual(chunked(range(7), 3, fill=None), [[0, 1, 2], [3, 4, 5], [6, None, None]])
        self.assertEqual(chunked([], 3), [])

    def test_generator_consumption(self):
        source = iter(range(8))
        chunks = chunked_iter(source, 3)
        self.assertEqual(next(chunks), [0, 1, 2])
        self.assertEqual(next(source), 3)
        self.assertEqual(list(chunks), [[4, 5, 6], [7]])

    def test_string_and_bytes(self):
        self.assertEqual(chunked("abcdefg", 3), ["abc", "def", "g"])
        self.assertEqual(chunked(b"abcdefg", 3), [b"abc", b"def", b"g"])

    def test_invalid_size(self):
        for size in (0, -1):
            with self.assertRaises(ValueError):
                chunked([1], size)
