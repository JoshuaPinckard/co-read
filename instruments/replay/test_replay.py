"""Focused invariants for the replay protocol.

Run from the repository root with
`python -m unittest instruments.replay.test_replay -v`.
"""

from __future__ import annotations

import io
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import parse_log
from replay import (
    RankedResult,
    ReplayState,
    cochange_query,
    new_model_accumulator,
    path_query,
    popularity_query,
    random_query,
    score_prediction,
)


def changes(*items: tuple[str, ...]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in items:
        if item[0] == "R":
            result.append({"status": "R", "old_path": item[1], "new_path": item[2]})
        else:
            result.append({"status": item[0], "path": item[1]})
    return result


class ReplayInvariantTests(unittest.TestCase):
    def test_query_precedes_fold_and_additions_are_not_ground_truth(self) -> None:
        state = ReplayState(["a.py", "b.py"])
        resolved = state.resolve_changes(changes(("M", "a.py"), ("M", "b.py"), ("A", "new.py")))
        eligible = [change.file_id for change in resolved if change.status != "A"]
        self.assertEqual(len(eligible), 2)
        self.assertEqual(cochange_query(state, eligible[0], 0, decayed=False).ids, ())
        state.fold(0, resolved)
        with self.assertRaises(AssertionError):
            cochange_query(state, eligible[0], 0, decayed=False)
        self.assertIn(eligible[1], cochange_query(state, eligible[0], 1, decayed=False).ids)

    def test_rename_is_one_identity_and_history_follows_it(self) -> None:
        state = ReplayState(["old.py", "peer.py"])
        old_id = state.path_to_id["old.py"]
        peer_id = state.path_to_id["peer.py"]
        state.fold(0, state.resolve_changes(changes(("M", "old.py"), ("M", "peer.py"))))
        resolved = state.resolve_changes(changes(("R", "old.py", "new.py"), ("M", "peer.py")))
        self.assertEqual(resolved[0].file_id, old_id)
        self.assertEqual(state.id_to_path[old_id], "old.py")
        self.assertIn(peer_id, cochange_query(state, old_id, 1, decayed=False).ids)
        state.fold(1, resolved)
        self.assertNotIn("old.py", state.path_to_id)
        self.assertEqual(state.path_to_id["new.py"], old_id)
        self.assertIn(peer_id, cochange_query(state, old_id, 2, decayed=False).ids)
        self.assertNotIn(old_id, state.adjacency[old_id])

    def test_delete_then_readd_gets_new_identity(self) -> None:
        state = ReplayState(["x.py"])
        old_id = state.path_to_id["x.py"]
        state.fold(0, state.resolve_changes(changes(("D", "x.py"))))
        resolved = state.resolve_changes(changes(("A", "x.py")))
        new_id = resolved[0].file_id
        self.assertNotEqual(old_id, new_id)
        state.fold(1, resolved)
        self.assertEqual(state.path_to_id["x.py"], new_id)
        self.assertEqual(len(state.file_history[new_id]), 1)
        self.assertEqual(state.readded_path_identity_count, 1)

    def test_factorized_and_materialized_cliques_rank_identically(self) -> None:
        paths = [f"src/f{index:03d}.py" for index in range(70)]
        materialized = ReplayState(paths, pair_materialize_max_files=100)
        hybrid = ReplayState(paths, pair_materialize_max_files=64)
        all_modified = changes(*(("M", path) for path in paths))
        materialized.fold(0, materialized.resolve_changes(all_modified))
        hybrid.fold(0, hybrid.resolve_changes(all_modified))
        two_modified = changes(("M", paths[0]), ("M", paths[1]))
        materialized.fold(1, materialized.resolve_changes(two_modified))
        hybrid.fold(1, hybrid.resolve_changes(two_modified))
        for decayed in (False, True):
            left = cochange_query(materialized, materialized.path_to_id[paths[0]], 2, decayed=decayed)
            right = cochange_query(hybrid, hybrid.path_to_id[paths[0]], 2, decayed=decayed)
            self.assertEqual(left, right)

    def test_randomized_factor_boundary_matches_materialized_oracle(self) -> None:
        paths = [f"src/f{index:03d}.py" for index in range(80)]
        materialized = ReplayState(paths, pair_materialize_max_files=1_000)
        hybrid = ReplayState(paths, pair_materialize_max_files=64)
        rng = random.Random(20260823)

        for commit_index, size in enumerate((2, 63, 64, 65, 80, 3, 70, 5, 79, 2)):
            touched = rng.sample(paths, size)
            for seed_path in rng.sample(paths, 5):
                for decayed in (False, True):
                    left = cochange_query(
                        materialized,
                        materialized.path_to_id[seed_path],
                        commit_index,
                        decayed=decayed,
                    )
                    right = cochange_query(
                        hybrid,
                        hybrid.path_to_id[seed_path],
                        commit_index,
                        decayed=decayed,
                    )
                    self.assertEqual(left, right)

            current_changes = changes(*(("M", path) for path in touched))
            materialized.fold(commit_index, materialized.resolve_changes(current_changes))
            hybrid.fold(commit_index, hybrid.resolve_changes(current_changes))

    def test_popularity_is_one_global_order_with_seed_only_filtered(self) -> None:
        state = ReplayState(["a", "b", "c", "d"])
        state.fold(0, state.resolve_changes(changes(("M", "a"), ("M", "b"))))
        state.fold(1, state.resolve_changes(changes(("M", "a"))))
        ids = state.path_to_id
        without_c = popularity_query(state, ids["c"], 2).ids
        without_b = popularity_query(state, ids["b"], 2).ids
        self.assertEqual(without_c, (ids["a"], ids["b"], ids["d"]))
        self.assertEqual(without_b, (ids["a"], ids["c"], ids["d"]))

    def test_random_is_deterministic_and_uses_only_live_precommit_files(self) -> None:
        state = ReplayState(["a", "b", "c", "gone"])
        state.fold(0, state.resolve_changes(changes(("D", "gone"))))
        seed = state.path_to_id["a"]
        first = random_query(state, seed, 1, "repo")
        second = random_query(state, seed, 1, "repo")
        self.assertEqual(first, second)
        self.assertEqual(set(first.ids), {state.path_to_id["b"], state.path_to_id["c"]})

    def test_path_score_uses_prefix_depth_before_basename_similarity(self) -> None:
        paths = [
            "src/main/java/x/Foo.java",
            "src/main/java/x/Unrelated.java",
            "src/test/java/x/Foo.java",
        ]
        state = ReplayState(paths)
        seed = state.path_to_id[paths[0]]
        ranked = path_query(state, seed, 0)
        self.assertEqual(ranked.ids[0], state.path_to_id[paths[1]])
        self.assertEqual(ranked.ids[1], state.path_to_id[paths[2]])

    def test_precision_at_ten_has_fixed_denominator(self) -> None:
        accumulator = new_model_accumulator()
        contribution = score_prediction(
            accumulator,
            RankedResult((2,), False, False, False),
            {2},
            100,
        )
        self.assertEqual(contribution["p10_hits"], 1)
        self.assertEqual(accumulator["p10_hits"] / (10 * accumulator["queries"]), 0.1)


class ExtractionParserTests(unittest.TestCase):
    def test_nul_parser_preserves_tabs_newlines_and_rename(self) -> None:
        sha = b"a" * 40
        payload = (
            b"\0COMMIT\0"
            + sha
            + b"\0"
            + b"123\0\0\nM\0a\tb\0R100\0old\nname\0new\nname\0"
        )
        parsed = list(parse_log(io.BytesIO(payload)))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["changes"][0]["path"], "a\tb")
        self.assertEqual(parsed[0]["changes"][1]["old_path"], "old\nname")
        self.assertEqual(parsed[0]["changes"][1]["new_path"], "new\nname")


if __name__ == "__main__":
    unittest.main()
