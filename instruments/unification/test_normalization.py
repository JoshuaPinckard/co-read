from __future__ import annotations

import unittest

import numpy as np

from instruments.unification import analyze as base
from instruments.unification import normalization


class ScoreFormulaTests(unittest.TestCase):
    def test_confidence_is_directional_and_outgoing_top_matches_raw(self) -> None:
        pair = np.asarray(
            [
                [0, 4, 2],
                [4, 0, 1],
                [2, 1, 0],
            ],
            dtype=np.int64,
        )
        marginals = np.asarray([10, 4, 2], dtype=np.int64)
        confidence = normalization.confidence_scores(pair, marginals)
        self.assertEqual(confidence[0, 1], 0.4)
        self.assertEqual(confidence[1, 0], 1.0)
        raw_top, _ = normalization.top_k_mask(pair, pair > 0, k=1)
        confidence_top, _ = normalization.top_k_mask(confidence, pair > 0, k=1)
        np.testing.assert_array_equal(raw_top, confidence_top)

    def test_pmi_uses_unit_probabilities_and_keeps_negative_supported_edges(self) -> None:
        pair = np.asarray(
            [
                [0, 1, 2],
                [1, 0, 0],
                [2, 0, 0],
            ],
            dtype=np.int64,
        )
        marginals = np.asarray([8, 8, 2], dtype=np.int64)
        pmi = normalization.pmi_scores(pair, marginals, 10, normalized=False)
        self.assertAlmostEqual(pmi[0, 1], np.log(10 / 64))
        self.assertAlmostEqual(pmi[0, 2], np.log(20 / 16))
        selected, _ = normalization.top_k_mask(pmi, pair > 0, k=2)
        self.assertTrue(selected[0, 1])
        self.assertTrue(selected[0, 2])
        self.assertFalse(selected[1, 2])

    def test_npmi_declares_degenerate_joint_probability_one_as_zero(self) -> None:
        pair = np.asarray([[0, 5], [5, 0]], dtype=np.int64)
        marginals = np.asarray([5, 5], dtype=np.int64)
        npmi = normalization.pmi_scores(pair, marginals, 5, normalized=True)
        self.assertEqual(npmi[0, 1], 0.0)

    def test_pmi_union_spearman_keeps_one_sided_edges(self) -> None:
        read_support = np.asarray(
            [[False, True, False], [True, False, False], [False, False, False]]
        )
        change_support = np.asarray(
            [[False, False, False], [False, False, True], [False, True, False]]
        )
        read = np.asarray([[0, -2, 0], [-2, 0, 0], [0, 0, 0]], dtype=float)
        change = np.asarray([[0, 0, 0], [0, 0, 3], [0, 3, 0]], dtype=float)
        result = normalization.union_spearman(
            read,
            change,
            read_support,
            change_support,
            variant="pmi",
        )
        self.assertEqual(result["pair_coordinates"], 2)
        self.assertAlmostEqual(result["spearman"], -1.0)


class IncidenceAdapterTests(unittest.TestCase):
    def test_read_incidence_reconstructs_existing_raw_builder(self) -> None:
        tasks = [
            {
                "agent": "z",
                "start": 20.0,
                "end": 21.0,
                "event_count": 2,
                "files": {("git", 1): 20.0, ("git", 2): 21.0},
            },
            {
                "agent": "a",
                "start": 10.0,
                "end": 11.0,
                "event_count": 1,
                "files": {("raw", "outside"): 10.0},
            },
            {
                "agent": "a",
                "start": 0.0,
                "end": 1.0,
                "event_count": 2,
                "files": {("git", 1): 0.0, ("git", 3): 1.0},
            },
        ]
        index = {1: 0, 2: 1, 3: 2}
        existing, _, marginals, _ = base.read_counts(tasks, index)
        incidence = normalization.read_incidence(tasks, index, half_life=2)
        rebuilt, rebuilt_marginals = normalization.incidence_counts(incidence.matrix)
        np.testing.assert_array_equal(rebuilt, existing)
        np.testing.assert_array_equal(rebuilt_marginals, marginals)
        self.assertEqual(incidence.global_unit_count, 3)
        self.assertEqual(incidence.included_unit_count, 2)
        # The excluded middle window still advances the global decay clock.
        np.testing.assert_array_equal(incidence.global_indices, np.asarray([0, 2]))
        np.testing.assert_allclose(
            incidence.weights,
            normalization.exponential_weights(np.asarray([3, 1]), 2),
        )

    def test_weighted_confidence_divides_both_terms_by_same_decay_clock(self) -> None:
        matrix = np.asarray([[1, 1, 0], [1, 0, 1]], dtype=bool)
        weights = np.asarray([0.25, 1.0])
        pair, marginals = normalization.weighted_incidence_counts(matrix, weights)
        scores = normalization.confidence_scores(pair, marginals)
        self.assertAlmostEqual(scores[0, 1], 0.25 / 1.25)
        self.assertAlmostEqual(scores[0, 2], 1.0 / 1.25)
        self.assertAlmostEqual(scores[1, 0], 1.0)


class NullCalibrationTests(unittest.TestCase):
    def test_two_edge_switch_preserves_whole_unit_and_file_degrees(self) -> None:
        matrix = np.asarray(
            [
                [1, 1, 0, 0],
                [0, 1, 1, 0],
                [0, 0, 1, 1],
                [1, 0, 0, 1],
            ],
            dtype=bool,
        )
        chain = normalization.DegreePreservingSwapper(
            matrix,
            np.random.default_rng(7),
        )
        diagnostics = chain.advance(100)
        chain.assert_invariants()
        self.assertEqual(diagnostics["attempted"], 100)
        self.assertGreater(diagnostics["accepted"], 0)

    def test_independent_all_tie_expectation_matches_enumerable_case(self) -> None:
        # Two independent 1-subsets of three candidates agree with probability
        # 1/3; their Jaccard is one on agreement and zero otherwise.
        self.assertAlmostEqual(
            normalization.independent_tie_expected_jaccard(3, k=1),
            1 / 3,
        )

    def test_every_variant_gets_its_own_analytic_null_transform(self) -> None:
        read_matrix = np.asarray(
            [
                [1, 1, 0, 0],
                [1, 0, 1, 0],
                [0, 1, 0, 1],
                [0, 0, 1, 1],
            ],
            dtype=bool,
        )
        change_matrix = np.asarray(
            [
                [1, 0, 0, 1],
                [0, 1, 1, 0],
                [1, 1, 0, 0],
                [0, 0, 1, 1],
            ],
            dtype=bool,
        )
        weights = np.ones(4, dtype=float)
        indices = np.arange(4)
        read_data = normalization.IncidenceData(read_matrix, weights, indices, 4, 4)
        change_data = normalization.IncidenceData(change_matrix, weights, indices, 4, 4)
        read_pair, read_marginals = normalization.incidence_counts(read_matrix)
        change_pair, change_marginals = normalization.incidence_counts(change_matrix)
        null = normalization.analytic_popularity_null(
            read_pair,
            change_pair,
            read_marginals,
            change_marginals,
            read_data,
            change_data,
            np.ones(4, dtype=bool),
        )
        self.assertEqual(
            null["raw_pair_count"]["top10"]["mean_jaccard"],
            null["confidence"]["top10"]["seed_to_candidate"]["mean_jaccard"],
        )
        self.assertEqual(
            null["pmi"]["top10"]["mean_jaccard"],
            null["normalized_pmi"]["top10"]["mean_jaccard"],
        )
        self.assertIsNone(null["pmi"]["union_support_spearman"]["spearman"])

    def test_top_k_ties_follow_column_order_without_promoting_absent_edges(self) -> None:
        scores = np.zeros((4, 4), dtype=float)
        support = np.zeros((4, 4), dtype=bool)
        support[0, [1, 2, 3]] = True
        selected, diagnostics = normalization.top_k_mask(scores, support, k=2)
        np.testing.assert_array_equal(np.flatnonzero(selected[0]), np.asarray([1, 2]))
        self.assertEqual(diagnostics["k_boundary_tie_seeds"], 1)
        self.assertFalse(selected[0, 0])


if __name__ == "__main__":
    unittest.main()
