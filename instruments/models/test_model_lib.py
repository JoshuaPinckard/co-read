from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from model_lib import (  # noqa: E402
    aligned_claim,
    aligned_overblock_probability,
    contiguous_disjoint_probability,
    nearest_rank,
    order_stat_extremes,
    quantile_reconstruction,
)


def brute_disjoint(width1: int, width2: int, size: int) -> float:
    total = 0
    disjoint = 0
    for start1 in range(size - width1 + 1):
        for start2 in range(size - width2 + 1):
            total += 1
            if start1 + width1 <= start2 or start2 + width2 <= start1:
                disjoint += 1
    return disjoint / total


def brute_overblock(width1: int, width2: int, size: int, granularity: int) -> float:
    total = 0
    overblocked = 0
    for start1 in range(size - width1 + 1):
        exact1 = (start1, start1 + width1)
        claim1 = aligned_claim(*exact1, size, granularity)
        for start2 in range(size - width2 + 1):
            total += 1
            exact2 = (start2, start2 + width2)
            exact_disjoint = exact1[1] <= exact2[0] or exact2[1] <= exact1[0]
            claim2 = aligned_claim(*exact2, size, granularity)
            claims_overlap = max(claim1[0], claim2[0]) < min(claim1[1], claim2[1])
            overblocked += int(exact_disjoint and claims_overlap)
    return overblocked / total


class CollisionFormulaTests(unittest.TestCase):
    def test_contiguous_formula_matches_enumeration(self) -> None:
        for size in range(2, 18):
            for width1 in range(1, size + 1):
                for width2 in range(1, size + 1):
                    expected = brute_disjoint(width1, width2, size)
                    actual = contiguous_disjoint_probability(width1, width2, size)
                    self.assertAlmostEqual(expected, actual, places=15)

    def test_aligned_overblock_matches_enumeration(self) -> None:
        for size in range(2, 22):
            for width1 in range(1, size + 1):
                for width2 in range(1, size + 1):
                    for granularity in (2, 3, 4, 7, 32):
                        expected = brute_overblock(width1, width2, size, granularity)
                        actual = aligned_overblock_probability(width1, width2, size, granularity)
                        self.assertAlmostEqual(
                            expected,
                            actual,
                            places=15,
                            msg=(size, width1, width2, granularity),
                        )


class SummaryReconstructionTests(unittest.TestCase):
    def test_extremes_reproduce_nearest_ranks(self) -> None:
        n = 1354
        low, high = order_stat_extremes(n, 9, 56, 200, 452, 1)
        for sample in (low, high):
            self.assertEqual(sample[nearest_rank(0.50, n) - 1], 9)
            self.assertEqual(sample[nearest_rank(0.90, n) - 1], 56)
            self.assertEqual(sample[nearest_rank(0.99, n) - 1], 200)
            self.assertEqual(sample[-1], 452)
            self.assertTrue(all(sample[index] <= sample[index + 1] for index in range(n - 1)))

    def test_reconstruction_reproduces_nearest_ranks(self) -> None:
        n = 405
        sample = quantile_reconstruction(n, 25.755, 3519.388, 133320.741, 278233.521, 0)
        self.assertTrue(math.isclose(sample[nearest_rank(0.50, n) - 1], 25.755))
        self.assertTrue(math.isclose(sample[nearest_rank(0.90, n) - 1], 3519.388))
        self.assertTrue(math.isclose(sample[nearest_rank(0.99, n) - 1], 133320.741))
        self.assertTrue(math.isclose(sample[-1], 278233.521))


if __name__ == "__main__":
    unittest.main()
