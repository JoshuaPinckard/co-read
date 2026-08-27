from __future__ import annotations

import unittest

from instruments.arms.shim.gitops import ScratchRepository


class HarvestRegionTests(unittest.TestCase):
    def test_disjoint_same_line_regions_are_composed(self) -> None:
        scratch = object.__new__(ScratchRepository)
        output, decisions = scratch._harvest_bytes(
            base=b"abcdef",
            left=b"aBcdeF",
            right=b"abcDef",
            answer=b"aBcDeF",
        )
        self.assertEqual(output, b"aBcDeF")
        self.assertTrue(all(not row["contested"] for row in decisions))

    def test_answer_selects_only_a_produced_contested_region(self) -> None:
        scratch = object.__new__(ScratchRepository)
        output, decisions = scratch._harvest_bytes(
            base=b"abcdef",
            left=b"aXXdef",
            right=b"aYYdef",
            answer=b"aYYdef",
        )
        self.assertEqual(output, b"aYYdef")
        self.assertEqual(decisions[0]["selected_from"], "B")


if __name__ == "__main__":
    unittest.main()

