from __future__ import annotations

import unittest
from pathlib import Path

from instruments.unification import predictor


def read_event(
    tool_id: str,
    call: float,
    result: float,
    *,
    file_id: int | None,
    path: str = "a",
    agent: str = "agent",
) -> predictor.PreparedRead:
    return predictor.PreparedRead(
        agent=agent,
        session="session",
        tool_use_id=tool_id,
        call_timestamp=call,
        result_timestamp=result,
        availability_timestamp=max(call, result),
        path_key=predictor.git_path_key(path),
        copied_prefix_identity=False,
        file_id=file_id,
    )


class TemporalReadTests(unittest.TestCase):
    def test_read_stream_must_match_requested_repository(self) -> None:
        header = {"target_repository": r"C:\repo"}
        predictor.assert_read_stream_repository(header, Path(r"C:\repo"))
        with self.assertRaisesRegex(ValueError, "does not match"):
            predictor.assert_read_stream_repository(header, Path(r"C:\other"))

    def test_unmapped_read_preserves_transitive_task_boundary(self) -> None:
        events = [
            read_event("one", 0.0, 1.0, file_id=1),
            read_event("bridge", 250.0, 251.0, file_id=None),
            read_event("two", 500.0, 501.0, file_id=2),
        ]
        incidences, diagnostics = predictor.build_pair_incidences(events)
        self.assertEqual(diagnostics["task_count"], 1)
        self.assertEqual(diagnostics["informative_task_count"], 1)
        self.assertEqual(
            incidences,
            [predictor.PairIncidence(501.0, 1, 2, 0)],
        )

    def test_task_splits_only_on_gap_strictly_greater_than_300(self) -> None:
        events = [
            read_event("one", 0.0, 0.1, file_id=1),
            read_event("two", 300.0, 300.1, file_id=2),
            read_event("three", 600.1, 600.2, file_id=3),
        ]
        incidences, diagnostics = predictor.build_pair_incidences(events)
        self.assertEqual(diagnostics["task_count"], 2)
        self.assertEqual([(item.left_id, item.right_id) for item in incidences], [(1, 2)])

    def test_pair_availability_waits_for_both_successful_results(self) -> None:
        events = [
            read_event("one", 10.0, 12.0, file_id=1),
            read_event("two", 11.0, 70.0, file_id=2),
        ]
        incidences, _ = predictor.build_pair_incidences(events)
        self.assertEqual(incidences[0].availability_timestamp, 70.0)
        timestamps = [item.availability_timestamp for item in incidences]
        self.assertEqual(predictor.strict_pair_prefix(incidences, timestamps, 70.0), 0)
        self.assertEqual(predictor.strict_pair_prefix(incidences, timestamps, 70.001), 1)

    def test_temporal_path_mapping_never_uses_future_addition(self) -> None:
        header = {"initial_files": ["a"]}
        commits = [
            {
                "sha": "commit-0",
                "changes": [{"status": "A", "path": "b"}],
            }
        ]
        dates = {"commit-0": predictor.CommitDates(10, 10)}
        events = [
            read_event("before", 4.0, 5.0, file_id=None, path="b"),
            read_event("after", 14.0, 15.0, file_id=None, path="b"),
        ]
        mapped, diagnostics = predictor.map_reads_at_availability(
            events, header, commits, dates
        )
        self.assertIsNone(mapped[0].file_id)
        self.assertIsNotNone(mapped[1].file_id)
        self.assertEqual(diagnostics["mapped_events"], 1)
        self.assertEqual(diagnostics["unmapped_path_absent_from_live_tree"], 1)


class RankingTests(unittest.TestCase):
    def test_coread_adjacency_counts_prefix_and_filters_nonlive_ids(self) -> None:
        incidences = [
            predictor.PairIncidence(1.0, 1, 2, 0),
            predictor.PairIncidence(2.0, 1, 2, 1),
            predictor.PairIncidence(3.0, 1, 3, 2),
        ]
        adjacency = predictor.coread_adjacency(incidences, 3, {1, 2})
        self.assertEqual(adjacency, {1: {2: 2}, 2: {1: 2}})

    def test_rrf_uses_complete_positive_rankings(self) -> None:
        _, replay = predictor.replay_modules()
        state = replay.ReplayState(["a", "b", "c", "d", "e"])
        ranking_one = [(5.0, 1), (4.0, 2), (3.0, 3)]
        ranking_two = [(5.0, 4), (4.0, 2), (3.0, 3)]
        fused = predictor.reciprocal_rank_fusion(
            state, replay, (ranking_one, ranking_two)
        )
        self.assertEqual(fused.ids[0], 2)
        self.assertIn(3, fused.ids)

    def test_ordered_scores_uses_current_path_bytes_for_ties(self) -> None:
        _, replay = predictor.replay_modules()
        state = replay.ReplayState(["z", "a", "m"])
        scores = {
            state.path_to_id["z"]: 2.0,
            state.path_to_id["a"]: 1.0,
            state.path_to_id["m"]: 1.0,
        }
        ordered = predictor.ordered_scores(state, scores)
        self.assertEqual(
            [state.id_to_path[file_id] for _, file_id in ordered],
            ["z", "a", "m"],
        )


class DependenceTests(unittest.TestCase):
    def test_bootstrap_moves_whole_commits_and_is_deterministic(self) -> None:
        def metrics(p1: int, p10: int, r10: float, r20: float) -> dict[str, float | int]:
            return {
                "p1_hits": p1,
                "p10_hits": p10,
                "r10_sum": r10,
                "r20_sum": r20,
                "empty_queries": 0,
            }

        commits = [
            {
                "query_count": 2,
                "models": {
                    model: metrics(2 if model == "coread" else 0, 2, 1.0, 1.0)
                    for model in predictor.MODEL_KEYS
                },
            },
            {
                "query_count": 5,
                "models": {
                    model: metrics(0, 0, 0.0, 0.0)
                    for model in predictor.MODEL_KEYS
                },
            },
        ]
        models = {
            model: predictor.aggregate_sample(commits, [0, 1], model)
            for model in predictor.MODEL_KEYS
        }
        first = predictor.commit_bootstrap(commits, models, 50)
        second = predictor.commit_bootstrap(commits, models, 50)
        self.assertEqual(first, second)
        self.assertEqual(first["resampling_unit"].split(";")[0], "eligible commit")
        interval = first["comparisons"]["coread_minus_cochange"]["p_at_1"]
        self.assertGreater(interval["point_delta"], 0.0)


if __name__ == "__main__":
    unittest.main()
