from __future__ import annotations

import collections
import unittest

import numpy as np

from instruments.unification import analyze, extract_reads


class TranscriptExtractionTests(unittest.TestCase):
    def test_windows_containment_is_not_string_prefix(self) -> None:
        root = extract_reads.normalise_absolute(r"C:\repo", None)
        assert root is not None
        self.assertEqual(
            extract_reads.relative_to_repository(r"C:\repo\src\A.py", None, root),
            "src/a.py",
        )
        self.assertIsNone(
            extract_reads.relative_to_repository(r"C:\repository\src\A.py", None, root)
        )

    def test_conservative_success_detection(self) -> None:
        numbered = {"content": "1\thello\n2\tworld"}
        oversize = {
            "content": "File content (300KB) exceeds maximum allowed size (256KB). "
            "Use offset and limit parameters to read specific portions of the file."
        }
        image = {"content": [{"type": "image", "source": {}}]}
        metadata = {"content": "", "is_error": False}
        self.assertTrue(extract_reads.successful_result(numbered, None))
        self.assertFalse(extract_reads.successful_result(oversize, None))
        self.assertTrue(extract_reads.successful_result(image, None))
        self.assertTrue(
            extract_reads.successful_result(metadata, {"file": {"filePath": r"C:\repo\a.py"}})
        )

    def test_global_tool_id_duplicate_uses_earliest_created_fallback_session(self) -> None:
        diagnostics: collections.Counter[str] = collections.Counter()
        later = {
            "session": "later",
            "agent": "later",
            "explicit_agent": False,
            "ts": 10.0,
            "cwd": r"C:\repo",
            "input_path": r"C:\repo\a.py",
            "source_created_ns": 20,
            "order": ("later", 1, 0),
            "occurrences": 1,
            "fallback_identity_from_copied_prefix": False,
        }
        earlier = {**later, "session": "earlier", "agent": "earlier", "source_created_ns": 10}
        calls: dict[str, dict[str, object]] = {}
        extract_reads.merge_call(calls, "tool-1", later, diagnostics)
        extract_reads.merge_call(calls, "tool-1", earlier, diagnostics)
        self.assertEqual(calls["tool-1"]["session"], "earlier")
        self.assertFalse(calls["tool-1"].get("conflict", False))
        self.assertTrue(calls["tool-1"]["fallback_identity_from_copied_prefix"])


class MatrixTests(unittest.TestCase):
    def test_inactivity_window_is_transitive_and_splits_on_strictly_greater_gap(self) -> None:
        events = [
            {"agent": "a", "timestamp": 0.0, "session": "s", "tool_use_id": "1", "label": ("git", 1)},
            {"agent": "a", "timestamp": 60.0, "session": "s", "tool_use_id": "2", "label": ("git", 2)},
            {"agent": "a", "timestamp": 120.0, "session": "s", "tool_use_id": "3", "label": ("git", 3)},
            {"agent": "a", "timestamp": 181.0, "session": "s", "tool_use_id": "4", "label": ("git", 1)},
        ]
        tasks = analyze.build_task_windows(events, 60)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["end"] - tasks[0]["start"], 120.0)

    def test_read_matrix_counts_each_file_once_and_preserves_first_order(self) -> None:
        tasks = [
            {
                "agent": "a",
                "start": 0.0,
                "end": 3.0,
                "event_count": 4,
                "files": {("git", 1): 0.0, ("git", 2): 2.0, ("git", 3): 2.0},
            }
        ]
        matrix, directed, marginals, coverage = analyze.read_counts(tasks, {1: 0, 2: 1, 3: 2})
        np.testing.assert_array_equal(
            matrix,
            np.asarray([[0, 1, 1], [1, 0, 1], [1, 1, 0]], dtype=np.int64),
        )
        self.assertEqual(directed[0, 1], 1)
        self.assertEqual(directed[0, 2], 1)
        self.assertEqual(directed[1, 2] + directed[2, 1], 0)
        np.testing.assert_array_equal(marginals, np.asarray([1, 1, 1]))
        self.assertEqual(coverage["direction_timestamp_tie_incidences"], 1)

    def test_primary_rank_universe_excludes_double_zeros_but_keeps_one_sided_edges(self) -> None:
        read = np.asarray([[0, 2, 0], [2, 0, 0], [0, 0, 0]], dtype=np.int64)
        change = np.asarray([[0, 0, 0], [0, 0, 3], [0, 3, 0]], dtype=np.int64)
        vectors, coverage = analyze.matrix_vectors(
            read,
            change,
            np.asarray([1, 1, 0]),
            np.asarray([0, 1, 1]),
            2,
            2,
        )
        np.testing.assert_array_equal(vectors["read"], np.asarray([2.0, 0.0]))
        np.testing.assert_array_equal(vectors["change"], np.asarray([0.0, 3.0]))
        self.assertEqual(coverage["union_nonzero_pair_support"], 2)
        self.assertEqual(coverage["double_zero_pair_coordinates_excluded_from_primary_correlation"], 1)

    def test_top_k_uses_only_positive_neighbors(self) -> None:
        matrix = np.asarray([[0, 4, 0], [4, 0, 1], [0, 1, 0]], dtype=np.int64)
        tops, diagnostics = analyze.top_positive(matrix, ["a", "b", "c"], k=10)
        self.assertEqual(tops, [{1}, {0, 2}, {1}])
        self.assertEqual(diagnostics["shorter_than_k_seeds"], 3)


if __name__ == "__main__":
    unittest.main()
