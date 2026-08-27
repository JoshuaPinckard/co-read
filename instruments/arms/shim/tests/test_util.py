from __future__ import annotations

import unittest

from instruments.arms.shim.util import byte_regions, regions_overlap


class ByteRegionTests(unittest.TestCase):
    def test_repeated_region_rewrites_keep_flanking_content_anchor(self) -> None:
        first = byte_regions(b"prefix AAAA suffix\n", b"prefix BBBB suffix\n")
        second = byte_regions(b"prefix BBBB suffix\n", b"prefix CCCC suffix\n")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]["content_anchor"], second[0]["content_anchor"])

    def test_disjoint_same_line_edits_remain_disjoint(self) -> None:
        base = b"abcdef\n"
        left = byte_regions(base, b"aBcdeF\n")
        right = byte_regions(base, b"abcDef\n")

        self.assertEqual(
            [(region["old_start"], region["old_end"]) for region in left],
            [(1, 2), (5, 6)],
        )
        self.assertEqual(
            [(region["old_start"], region["old_end"]) for region in right],
            [(3, 4)],
        )
        self.assertFalse(
            any(regions_overlap(a, b) for a in left for b in right)
        )

    def test_insertion_at_half_open_region_end_does_not_overlap(self) -> None:
        changed = {"old_start": 2, "old_end": 5}
        at_end = {"old_start": 5, "old_end": 5}
        inside = {"old_start": 4, "old_end": 4}

        self.assertFalse(regions_overlap(changed, at_end))
        self.assertFalse(regions_overlap(at_end, changed))
        self.assertTrue(regions_overlap(changed, inside))
        self.assertTrue(regions_overlap(inside, changed))


if __name__ == "__main__":
    unittest.main()
