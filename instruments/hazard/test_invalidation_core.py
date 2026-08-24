import hashlib
import unittest

from invalidation_core import (
    ChangeBlock,
    classify_change_overlap,
    intervals_overlap,
    is_exact_inverse_patch,
    normalize_windows_path,
    parse_numbered_read_window,
    parse_structured_patch,
    transform_intervals_through_changes,
    union_intervals,
)


class WindowsPathTests(unittest.TestCase):
    def test_normalizes_drive_case_separators_and_parents(self):
        self.assertEqual(
            normalize_windows_path(r"C:/Users/JOSH/Project/../File.PY"),
            r"c:\users\josh\file.py",
        )

    def test_resolves_relative_path_against_transcript_cwd(self):
        self.assertEqual(
            normalize_windows_path(r"src/../lib/A.JS", r"D:\Work\Repo"),
            r"d:\work\repo\lib\a.js",
        )

    def test_normalizes_extended_unc_prefix(self):
        self.assertEqual(
            normalize_windows_path(r"\\?\UNC\Server\Share\A\..\B.TXT"),
            r"\\server\share\b.txt",
        )


class NumberedReadTests(unittest.TestCase):
    def test_parses_tab_and_arrow_renderings_and_ignores_wrapper_text(self):
        visible = (
            "wrapper before\n"
            "   41\N{RIGHTWARDS ARROW}alpha\n"
            "42\t\n"
            "43\t  indented\n"
            "<system-reminder>not file content</system-reminder>\n"
        )
        window = parse_numbered_read_window(
            visible, expected_start=41, expected_num_lines=3
        )
        self.assertEqual(window.interval, (41, 44))
        self.assertEqual(window.lines, ("alpha", "", "  indented"))
        self.assertEqual(
            window.signatures[0], hashlib.sha256(b"alpha").hexdigest()
        )

    def test_rejects_gap_in_visible_line_numbers(self):
        with self.assertRaisesRegex(ValueError, "not consecutive"):
            parse_numbered_read_window("8\tone\n10\ttwo")

    def test_rejects_structured_metadata_mismatch(self):
        with self.assertRaisesRegex(ValueError, "length mismatch"):
            parse_numbered_read_window(
                "8\tone\n9\ttwo", expected_start=8, expected_num_lines=3
            )


class StructuredPatchTests(unittest.TestCase):
    def test_extracts_exact_blocks_without_context(self):
        patch = [
            {
                "oldStart": 10,
                "oldLines": 6,
                "newStart": 10,
                "newLines": 6,
                "lines": [
                    " keep",
                    "-old-a",
                    "-old-b",
                    "+new-a",
                    " keep2",
                    "+inserted",
                    " tail",
                    " end",
                ],
            }
        ]
        blocks = parse_structured_patch(patch)
        self.assertEqual(len(blocks), 2)

        replacement, insertion = blocks
        self.assertEqual(replacement.old_interval, (11, 13))
        self.assertEqual(replacement.new_interval, (11, 12))
        self.assertEqual(replacement.old_lines, ("old-a", "old-b"))
        self.assertEqual(replacement.new_lines, ("new-a",))
        self.assertNotIn("keep", replacement.old_lines)
        self.assertEqual(
            replacement.old_signatures[0], hashlib.sha256(b"old-a").hexdigest()
        )

        self.assertTrue(insertion.is_insertion)
        self.assertEqual(insertion.old_interval, (14, 14))
        self.assertEqual(insertion.new_interval, (13, 14))
        self.assertEqual(insertion.new_lines, ("inserted",))

    def test_attaches_no_newline_markers_to_each_changed_side(self):
        blocks = parse_structured_patch(
            [
                {
                    "oldStart": 1,
                    "oldLines": 1,
                    "newStart": 1,
                    "newLines": 1,
                    "lines": [
                        "-before",
                        "\\ No newline at end of file",
                        "+after",
                        "\\ No newline at end of file",
                    ],
                }
            ]
        )
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].old_no_newline)
        self.assertTrue(blocks[0].new_no_newline)

    def test_rejects_declared_hunk_count_mismatch(self):
        with self.assertRaisesRegex(ValueError, "count mismatch"):
            parse_structured_patch(
                [
                    {
                        "oldStart": 1,
                        "oldLines": 2,
                        "newStart": 1,
                        "newLines": 1,
                        "lines": ["-old", "+new"],
                    }
                ]
            )


class IntervalAndOverlapTests(unittest.TestCase):
    def test_union_merges_adjacent_windows(self):
        self.assertEqual(
            union_intervals([(8, 10), (2, 4), (4, 7), (9, 12)]),
            ((2, 7), (8, 12)),
        )
        self.assertFalse(intervals_overlap((1, 2), (2, 3)))
        self.assertTrue(intervals_overlap((1, 3), (2, 4)))

    def test_distinguishes_destructive_internal_and_boundary_contact(self):
        tracked = [(10, 13)]
        destructive = ChangeBlock(11, 11, ("old",), ("new",))
        internal = ChangeBlock(12, 12, (), ("inside",))
        left_edge = ChangeBlock(10, 10, (), ("before",))
        right_edge = ChangeBlock(13, 13, (), ("after",))

        result = classify_change_overlap(tracked, [destructive, internal])
        self.assertTrue(result.destructive)
        self.assertTrue(result.internal_insertion)
        self.assertFalse(result.boundary_insertion)
        self.assertTrue(result.strict)

        edges = classify_change_overlap(tracked, [left_edge, right_edge])
        self.assertFalse(edges.strict)
        self.assertTrue(edges.boundary_insertion)
        self.assertTrue(edges.boundary_sensitive)


class TransformTests(unittest.TestCase):
    @staticmethod
    def patch_with_insertion_and_replacement():
        return parse_structured_patch(
            [
                {
                    "oldStart": 1,
                    "oldLines": 8,
                    "newStart": 1,
                    "newLines": 9,
                    "lines": [
                        " old1",
                        " old2",
                        "+ins-a",
                        "+ins-b",
                        " old3",
                        " old4",
                        "-old5",
                        "-old6",
                        "+replacement",
                        " old7",
                        " old8",
                    ],
                }
            ]
        )

    def test_tracks_only_surviving_old_line_provenance(self):
        changes = self.patch_with_insertion_and_replacement()
        result = transform_intervals_through_changes([(2, 8)], changes)
        self.assertEqual(result.intervals, ((2, 3), (5, 7), (8, 9)))
        self.assertTrue(result.overlap.destructive)
        self.assertTrue(result.overlap.internal_insertion)

    def test_boundary_insertion_shifts_without_entering_tracked_region(self):
        insertion = ChangeBlock(2, 2, (), ("new",))
        result = transform_intervals_through_changes([(2, 4)], [insertion])
        self.assertEqual(result.intervals, ((3, 5),))
        self.assertFalse(result.overlap.strict)
        self.assertTrue(result.overlap.boundary_insertion)


class ExactInverseTests(unittest.TestCase):
    def test_recognizes_exact_multi_block_inverse(self):
        forward = TransformTests.patch_with_insertion_and_replacement()
        inverse = parse_structured_patch(
            [
                {
                    "oldStart": 3,
                    "oldLines": 2,
                    "newStart": 3,
                    "newLines": 0,
                    "lines": ["-ins-a", "-ins-b"],
                },
                {
                    "oldStart": 7,
                    "oldLines": 1,
                    "newStart": 5,
                    "newLines": 2,
                    "lines": ["-replacement", "+old5", "+old6"],
                },
            ]
        )
        self.assertTrue(is_exact_inverse_patch(forward, inverse))

        altered = list(inverse)
        altered[1] = ChangeBlock(7, 5, ("replacement",), ("old5", "changed"))
        self.assertFalse(is_exact_inverse_patch(forward, altered))

    def test_no_newline_state_must_also_be_inverted(self):
        forward = [
            ChangeBlock(
                1,
                1,
                ("old",),
                ("new",),
                old_no_newline=True,
                new_no_newline=False,
            )
        ]
        correct = [
            ChangeBlock(
                1,
                1,
                ("new",),
                ("old",),
                old_no_newline=False,
                new_no_newline=True,
            )
        ]
        wrong = [ChangeBlock(1, 1, ("new",), ("old",))]
        self.assertTrue(is_exact_inverse_patch(forward, correct))
        self.assertFalse(is_exact_inverse_patch(forward, wrong))


if __name__ == "__main__":
    unittest.main()
