from __future__ import annotations

import collections
import unittest

from invalidation import (
    Operation,
    attach_rework_outcomes,
    build_response_episodes,
    build_strict_hazard_pairs,
    logical_lines,
    patch_matches_preimage,
    summarize_hazard_pairs,
    summarize_episodes,
    transform_anchor,
)
from invalidation_core import ChangeBlock


PATH = r"c:\repo\src\a.py"
CUTOFF = 1_000.0


def operation(
    tool_id: str,
    tool: str,
    actor: str,
    call_ts: float,
    result_ts: float,
    *,
    read_interval: tuple[int, int] | None = None,
    patch: tuple[ChangeBlock, ...] = (),
    patch_status: str | None = None,
    read_lines: tuple[str, ...] | None = None,
    original_lines: tuple[str, ...] | None = None,
) -> Operation:
    if patch_status is None:
        patch_status = "exact" if patch else "missing"
    return Operation(
        tool_id=tool_id,
        tool=tool,
        session="session",
        actor=actor,
        legacy_agent=actor,
        explicit_agent=actor != "MAIN",
        call_ts=call_ts,
        result_ts=result_ts,
        path=PATH,
        legacy_path=PATH,
        success=True,
        read_interval=read_interval,
        read_source="visible_result_fallback" if read_interval else "none",
        read_lines=(
            read_lines
            if read_lines is not None
            else (
                tuple("line" for _ in range(read_interval[1] - read_interval[0]))
                if read_interval else None
            )
        ),
        patch=patch,
        patch_status=patch_status,
        original_file_status="nonempty_string",
        original_file_lines=(
            original_lines if original_lines is not None else tuple("line" for _ in range(100))
        ),
        origin_created_ns=0,
        origin_rel="fixture.jsonl",
        call_uuid=tool_id + "-message",
    )


class ResponseEpisodeTests(unittest.TestCase):
    def test_overlapping_opening_write_then_reread_is_one_episode(self) -> None:
        changed = ChangeBlock(12, 12, ("old",), ("new",))
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("w1", "Edit", "MAIN", 4, 5, patch=(changed,)),
            operation("r2", "Read", "reader", 8, 9, read_interval=(12, 13)),
        ]
        diagnostics: collections.Counter[str] = collections.Counter()
        episodes = build_response_episodes(operations, CUTOFF, diagnostics)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["region_class"], "region_overlapping")
        self.assertEqual(episodes[0]["opening_region_class"], "region_overlapping")
        self.assertEqual(episodes[0]["foreign_write_count"], 1)
        self.assertEqual(episodes[0]["response"], "reread_first")

    def test_second_foreign_write_is_competing_not_merged(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("w1", "Edit", "MAIN", 4, 5, patch=(ChangeBlock(50, 50, ("x",), ("y",)),)),
            operation("w2", "Edit", "other", 6, 7, patch=(ChangeBlock(12, 12, ("a",), ("b",)),)),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["opening_region_class"], "file_only")
        self.assertEqual(episodes[0]["region_class"], "file_only")
        self.assertEqual(episodes[0]["response"], "competing_foreign_write")

    def test_file_only_episode_can_have_relevant_reader_edit_and_rework(self) -> None:
        foreign = ChangeBlock(50, 50, ("far",), ("changed",))
        index = ChangeBlock(12, 12, ("before",), ("reader-change",))
        reedit = ChangeBlock(12, 12, ("reader-change",), ("reader-change-2",))
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("w1", "Edit", "MAIN", 4, 5, patch=(foreign,)),
            operation("e1", "Edit", "reader", 6, 7, patch=(index,)),
            operation("e2", "Edit", "reader", 8, 9, patch=(reedit,)),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["region_class"], "file_only")
        self.assertEqual(episodes[0]["response"], "relevant_edit")
        attach_rework_outcomes(
            episodes,
            operations,
            {"session": 20.0},
            snapshot_epoch=10_000.0,
        )
        self.assertTrue(episodes[0]["rework"])
        self.assertTrue(episodes[0]["reedited_by_reader"])
        summary = summarize_episodes(episodes)
        self.assertEqual(
            summary["contingency"]["file_only"]["rework_followed"], 1
        )

    def test_unlocalized_reader_edit_is_not_clean(self) -> None:
        foreign = ChangeBlock(12, 12, ("old",), ("new",))
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("w1", "Edit", "MAIN", 4, 5, patch=(foreign,)),
            operation("e1", "Edit", "reader", 6, 7, patch_status="missing"),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        attach_rework_outcomes(
            episodes,
            operations,
            {"session": 1_000.0},
            snapshot_epoch=10_000.0,
        )
        self.assertEqual(episodes[0]["response"], "edit_unlocalized")
        self.assertIsNone(episodes[0]["rework"])
        self.assertIsNone(episodes[0]["clean"])

    def test_read_that_completes_after_write_invocation_is_not_a_hazard(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 5, read_interval=(10, 20)),
            operation(
                "w1",
                "Edit",
                "MAIN",
                4,
                6,
                patch=(ChangeBlock(12, 12, ("old",), ("new",)),),
            ),
        ]
        diagnostics: collections.Counter[str] = collections.Counter()
        episodes = build_response_episodes(operations, CUTOFF, diagnostics)
        self.assertEqual(episodes, [])
        self.assertEqual(diagnostics["read_write_interval_order_ambiguous"], 1)

    def test_reader_edit_before_foreign_write_is_retained_as_unknown_chain(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("e1", "Edit", "reader", 3, 4, patch_status="missing"),
            operation("w1", "Edit", "MAIN", 5, 6, patch=(ChangeBlock(12, 12, ("x",), ("y",)),)),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["opening_region_class"], "region_overlapping")
        self.assertEqual(episodes[0]["region_class"], "unknown")
        self.assertEqual(
            episodes[0]["opening_preimage_validation"], "intervening_reader_write"
        )

    def test_preopening_deletion_anchor_remains_a_dependency(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(12, 13)),
            operation(
                "e1", "Edit", "reader", 3, 4,
                patch=(ChangeBlock(12, 12, ("line",), ()),),
            ),
            operation(
                "w1", "Edit", "MAIN", 5, 6,
                patch=(ChangeBlock(12, 12, (), ("foreign",)),),
            ),
            operation(
                "e2", "Edit", "reader", 7, 8,
                patch=(ChangeBlock(12, 12, ("foreign",), ("repair",)),),
            ),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["opening_region_class"], "region_overlapping")
        self.assertEqual(episodes[0]["response"], "relevant_edit")
        self.assertEqual(
            episodes[0]["opening_preimage_validation"], "intervening_reader_write"
        )

    def test_nonoverlap_reread_does_not_expand_stale_footprint(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("w1", "Edit", "MAIN", 3, 4, patch=(ChangeBlock(12, 12, ("x",), ("y",)),)),
            operation("r2", "Read", "reader", 5, 6, read_interval=(50, 60)),
            operation("e1", "Edit", "reader", 7, 8, patch=(ChangeBlock(52, 52, ("a",), ("b",)),)),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        self.assertEqual(episodes[0]["response"], "no_relevant_response")
        self.assertEqual(episodes[0]["nonoverlap_reread_count"], 1)

    def test_deletion_anchor_reread_is_observed(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("w1", "Edit", "MAIN", 3, 4, patch=(ChangeBlock(12, 12, ("gone",), ()),)),
            operation("r2", "Read", "reader", 5, 6, read_interval=(12, 13)),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        self.assertEqual(episodes[0]["response"], "reread_first")

    def test_concurrent_later_write_is_not_rework(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("w1", "Edit", "MAIN", 3, 4, patch=(ChangeBlock(50, 50, ("far",), ("changed",)),)),
            operation("e1", "Edit", "reader", 5, 7, patch=(ChangeBlock(12, 12, ("before",), ("reader-change",)),)),
            operation("w2", "Edit", "MAIN", 6, 8, patch=(ChangeBlock(12, 12, ("reader-change",), ("other",)),)),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        attach_rework_outcomes(
            episodes, operations, {"session": 100.0}, snapshot_epoch=10_000.0
        )
        self.assertIsNone(episodes[0]["rework"])
        self.assertFalse(episodes[0]["followup_chain_known"])

    def test_earlier_competing_call_censors_episode_even_if_it_finishes_later(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation(
                "w1", "Edit", "MAIN", 3, 10,
                patch=(ChangeBlock(12, 12, ("line",), ("changed",)),),
            ),
            operation(
                "w2", "Edit", "other", 11, 20,
                patch=(ChangeBlock(50, 50, ("line",), ("other",)),),
            ),
            operation("r2", "Read", "reader", 12, 13, read_interval=(12, 13)),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["response"], "competing_foreign_write")

    def test_overlapping_followup_writes_make_rework_chain_unknown(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation(
                "w1", "Edit", "MAIN", 3, 4,
                patch=(ChangeBlock(50, 50, ("line",), ("changed",)),),
            ),
            operation(
                "e1", "Edit", "reader", 5, 6,
                patch=(ChangeBlock(12, 12, ("line",), ("reader-change",)),),
            ),
            operation(
                "w2", "Edit", "MAIN", 7, 10,
                patch=(ChangeBlock(50, 50, ("changed",), ("other",)),),
            ),
            operation(
                "e2", "Edit", "reader", 8, 9,
                patch=(ChangeBlock(12, 12, ("reader-change",), ("reader-change-2",)),),
            ),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        attach_rework_outcomes(
            episodes, operations, {"session": 100.0}, snapshot_epoch=10_000.0
        )
        self.assertIsNone(episodes[0]["rework"])
        self.assertFalse(episodes[0]["followup_chain_known"])

    def test_central_outcome_requires_edit_of_foreign_changed_footprint(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation(
                "w1", "Edit", "MAIN", 3, 4,
                patch=(ChangeBlock(12, 12, ("line",), ("foreign",)),),
            ),
            operation(
                "e1", "Edit", "reader", 5, 6,
                patch=(ChangeBlock(15, 15, ("line",), ("reader-change",)),),
            ),
            operation(
                "e2", "Edit", "reader", 7, 8,
                patch=(ChangeBlock(15, 15, ("reader-change",), ("again",)),),
            ),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        attach_rework_outcomes(
            episodes, operations, {"session": 100.0}, snapshot_epoch=10_000.0
        )
        summary = summarize_episodes(episodes)
        self.assertEqual(
            summary["contingency"]["region_overlapping"]["rework_followed"], 1
        )
        self.assertEqual(
            summary["confirmed_invalidation_proxy"]["eligible_region_overlap_outcomes"],
            0,
        )

    def test_deletion_index_can_be_exactly_reverted_at_anchor(self) -> None:
        deletion = ChangeBlock(12, 12, ("reader-change",), ())
        inverse = ChangeBlock(12, 12, (), ("reader-change",))
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("w1", "Edit", "MAIN", 3, 4, patch=(ChangeBlock(50, 50, ("far",), ("changed",)),)),
            operation("e1", "Edit", "reader", 5, 6, patch=(deletion,)),
            operation("e2", "Edit", "reader", 7, 8, patch=(inverse,)),
        ]
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        attach_rework_outcomes(
            episodes, operations, {"session": 100.0}, snapshot_epoch=10_000.0
        )
        self.assertTrue(episodes[0]["rework"])
        self.assertTrue(episodes[0]["reverted"])


class PreimageValidationTests(unittest.TestCase):
    def test_logical_lines_preserve_final_empty_line(self) -> None:
        self.assertEqual(logical_lines("a\nb\n"), ("a", "b", ""))

    def test_patch_old_side_is_checked_against_preimage(self) -> None:
        self.assertTrue(
            patch_matches_preimage(
                [ChangeBlock(2, 2, ("b",), ("B",))], ("a", "b", "c")
            )
        )
        self.assertFalse(
            patch_matches_preimage(
                [ChangeBlock(2, 2, ("wrong",), ("B",))], ("a", "b", "c")
            )
        )

    def test_anchor_transform_uses_original_coordinates_for_later_blocks(self) -> None:
        changes = (
            ChangeBlock(2, 2, (), ("inserted",)),
            ChangeBlock(11, 12, ("deleted",), ()),
        )
        self.assertEqual(transform_anchor(10, changes), 11)


class HazardPairTests(unittest.TestCase):
    def test_pair_overlap_unit_is_not_response_episode_unit(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation("r2", "Read", "reader", 2.1, 2.2, read_interval=(40, 50)),
            operation(
                "w1", "Edit", "MAIN", 4, 5,
                patch=(ChangeBlock(12, 12, ("line",), ("changed",)),),
            ),
        ]
        pairs = build_strict_hazard_pairs(operations, CUTOFF, collections.Counter())
        episodes = build_response_episodes(operations, CUTOFF, collections.Counter())
        self.assertEqual(len(pairs), 2)
        self.assertEqual(len(episodes), 1)
        summary = summarize_hazard_pairs(pairs)
        measurement = summary["offset_only_overlap_measurement"]
        self.assertEqual(measurement["region_overlapping"], 1)
        self.assertEqual(measurement["file_only"], 1)

    def test_pair_requires_read_result_before_write_call(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 5, read_interval=(10, 20)),
            operation(
                "w1", "Edit", "MAIN", 4, 6,
                patch=(ChangeBlock(12, 12, ("line",), ("changed",)),),
            ),
        ]
        diagnostics: collections.Counter[str] = collections.Counter()
        pairs = build_strict_hazard_pairs(operations, CUTOFF, diagnostics)
        self.assertEqual(pairs, [])
        self.assertEqual(diagnostics["strict_pairs_excluded_for_overlapping_calls"], 1)

    def test_followup_timeline_extends_beyond_endpoint_cutoff(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation(
                "w1", "Edit", "MAIN", 3, 4,
                patch=(ChangeBlock(12, 12, ("line",), ("changed",)),),
            ),
            operation(
                "w2", "Edit", "other", CUTOFF + 1, CUTOFF + 2,
                patch=(ChangeBlock(50, 50, ("line",), ("other",)),),
            ),
        ]
        pairs = build_strict_hazard_pairs(operations, CUTOFF, collections.Counter())
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["response"], "competing_foreign_write")

    def test_earlier_competing_call_censors_reader_action(self) -> None:
        operations = [
            operation("r1", "Read", "reader", 1, 2, read_interval=(10, 20)),
            operation(
                "w1", "Edit", "MAIN", 3, 4,
                patch=(ChangeBlock(12, 12, ("line",), ("changed",)),),
            ),
            operation(
                "w2", "Edit", "other", 5, 10,
                patch=(ChangeBlock(50, 50, ("line",), ("other",)),),
            ),
            operation("e1", "Edit", "reader", 6, 7, patch_status="missing"),
        ]
        pairs = build_strict_hazard_pairs(operations, CUTOFF, collections.Counter())
        self.assertEqual(pairs[0]["response"], "competing_foreign_write")
        self.assertTrue(pairs[0]["reader_write_after_opening_any"])


if __name__ == "__main__":
    unittest.main()
