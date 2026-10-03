"""Independent checks cover both the shared recipe and strict chunking."""
import unittest

from more_itertools import chunked, take


class SharedChunkContract(unittest.TestCase):
    def test_take_consumption(self):
        source = iter(range(7))
        self.assertEqual(take(3, source), [0, 1, 2])
        self.assertEqual(next(source), 3)
        self.assertEqual(take(0, source), [])
        self.assertEqual(next(source), 4)
        self.assertEqual(take(None, source), [5, 6])
        self.assertEqual(take(10, iter([1, 2])), [1, 2])

    def test_chunk_sizes(self):
        self.assertEqual(list(chunked(iter(range(7)), 3)), [[0, 1, 2], [3, 4, 5], [6]])
        self.assertEqual(list(chunked(range(6), 3, strict=True)), [[0, 1, 2], [3, 4, 5]])
        self.assertEqual(list(chunked(range(3), 0)), [])
        self.assertEqual(list(chunked(range(3), None)), [[0, 1, 2]])

    def test_strict_rejects_trailing_before_yield(self):
        iterator = chunked(range(7), 3, strict=True)
        self.assertEqual(next(iterator), [0, 1, 2])
        self.assertEqual(next(iterator), [3, 4, 5])
        with self.assertRaises(ValueError):
            next(iterator)

    def test_invalid_sizes(self):
        with self.assertRaises(ValueError):
            chunked([1], -1)
        with self.assertRaises(ValueError):
            take(-1, [1])
        with self.assertRaises(ValueError):
            chunked([1], None, strict=True)
