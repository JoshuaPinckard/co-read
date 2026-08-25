import unittest

from instruments.hazard.invalidation_core import ChangeBlock
from instruments.oscillation.analyze import (
    Operation,
    PairEdge,
    apply_patch_lines,
    build_pairs_and_sequences,
    classify_file,
    import_order_only_patch,
    oscillation_subtype,
    oscillation_witness_cause_partitions,
    pair_classification,
    patch_mechanical_kind,
    patches_contact,
    read_matches_preimage,
    reversal_read_category,
    sequence_classification,
    volatile_metadata_only_contact,
    whitespace_only_patch,
)


TEST_PATH = r"c:\repo\src\example.py"


def change(
    start,
    old_lines,
    new_lines,
    *,
    new_start=None,
):
    return ChangeBlock(
        old_start=start,
        new_start=start if new_start is None else new_start,
        old_lines=tuple(old_lines),
        new_lines=tuple(new_lines),
    )


def write_operation(
    tool_id,
    agent,
    pre_lines,
    patch,
    *,
    call_ts,
    result_ts,
    path=TEST_PATH,
):
    patch = tuple(patch)
    pre_lines = tuple(pre_lines)
    return Operation(
        tool_id=tool_id,
        tool="Edit",
        agent=agent,
        explicit_agent=True,
        sessions=(f"session-{agent}",),
        call_ts=call_ts,
        result_ts=result_ts,
        path=path,
        success=True,
        patch=patch,
        pre_lines=pre_lines,
        post_lines=apply_patch_lines(pre_lines, patch),
        read_interval=None,
        read_lines=None,
        metadata_status="exact_write",
        duplicate_occurrences=1,
    )


def read_operation(
    tool_id,
    agent,
    interval,
    lines,
    *,
    call_ts,
    result_ts,
    path=TEST_PATH,
):
    return Operation(
        tool_id=tool_id,
        tool="Read",
        agent=agent,
        explicit_agent=True,
        sessions=(f"session-{agent}",),
        call_ts=call_ts,
        result_ts=result_ts,
        path=path,
        success=True,
        patch=None,
        pre_lines=None,
        post_lines=None,
        read_interval=interval,
        read_lines=tuple(lines) if lines is not None else None,
        metadata_status="exact_read",
        duplicate_occurrences=1,
    )


def pair_edge(
    left,
    right,
    classification,
    *,
    score,
    contact_pairs=((0, 0),),
    contact_mechanical_kinds=None,
):
    inverse = {
        "score": score,
        "remove_fraction": score,
        "restore_fraction": score,
    }
    return PairEdge(
        path=left.path or TEST_PATH,
        left_position=0,
        right_position=1,
        left=left,
        right=right,
        classification=classification,
        inverse=inverse,
        line_inverse=dict(inverse),
        latency_seconds=right.call_ts - left.result_ts,
        mechanical_kinds=(),
        command_causes=(),
        generated=False,
        wholesale=False,
        contact_pairs=tuple(contact_pairs),
        contact_mechanical_kinds=(
            tuple(() for _ in contact_pairs)
            if contact_mechanical_kinds is None
            else tuple(tuple(kinds) for kinds in contact_mechanical_kinds)
        ),
    )


class ApplyPatchLinesTests(unittest.TestCase):
    def test_applies_replacement_insertion_and_deletion(self):
        preimage = ("alpha", "bravo", "charlie")
        cases = (
            (
                "replacement",
                change(2, ("bravo",), ("BRAVO",)),
                ("alpha", "BRAVO", "charlie"),
            ),
            (
                "insertion",
                change(2, (), ("inserted",)),
                ("alpha", "inserted", "bravo", "charlie"),
            ),
            (
                "deletion",
                change(2, ("bravo",), ()),
                ("alpha", "charlie"),
            ),
        )
        for label, patch, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(apply_patch_lines(preimage, (patch,)), expected)


class ContactTests(unittest.TestCase):
    def test_primary_overlap_excludes_boundary_anchors(self):
        forward = change(10, ("old-a", "old-b"), ("new-a", "new-b"))
        anchors = {
            "left_boundary": change(10, (), ("insert-left",)),
            "interior": change(11, (), ("insert-inside",)),
            "right_boundary": change(12, (), ("insert-right",)),
        }

        self.assertFalse(patches_contact((forward,), (anchors["left_boundary"],)))
        self.assertTrue(patches_contact((forward,), (anchors["interior"],)))
        self.assertFalse(patches_contact((forward,), (anchors["right_boundary"],)))

        for label, anchor in anchors.items():
            with self.subTest(label=label):
                self.assertTrue(
                    patches_contact(
                        (forward,), (anchor,), include_boundary_anchors=True
                    )
                )


class PairClassificationTests(unittest.TestCase):
    def test_exact_reversal_requires_and_recognizes_full_restoration(self):
        base = ("head", "old value", "tail")
        left = write_operation(
            "write-a",
            "agent-a",
            base,
            (change(2, ("old value",), ("new value",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        right = write_operation(
            "write-b",
            "agent-b",
            left.post_lines,
            (change(2, ("new value",), ("old value",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        classification, inverse, _ = pair_classification(left, right)

        self.assertEqual(classification, "exact_reversal")
        self.assertEqual(right.post_lines, left.pre_lines)
        self.assertEqual(inverse["score"], 1.0)

    def test_partial_reversal_obeys_reported_thresholds(self):
        left = write_operation(
            "write-a",
            "agent-a",
            ("old1 old2 old3 old4",),
            (change(1, ("old1 old2 old3 old4",), ("new1 new2 new3 new4",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        right = write_operation(
            "write-b",
            "agent-b",
            left.post_lines,
            (change(1, ("new1 new2 new3 new4",), ("old1 old2 old3 other",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        for threshold, expected in (
            (0.50, "partial_reversal"),
            (0.75, "partial_reversal"),
            (0.90, "independent_coediting"),
        ):
            with self.subTest(threshold=threshold):
                classification, inverse, _ = pair_classification(
                    left, right, threshold=threshold
                )
                self.assertEqual(classification, expected)
                self.assertAlmostEqual(float(inverse["score"]), 0.75)

    def test_one_to_two_to_three_is_not_a_partial_reversal(self):
        left = write_operation(
            "write-a",
            "agent-a",
            ("value = 1",),
            (change(1, ("value = 1",), ("value = 2",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        right = write_operation(
            "write-b",
            "agent-b",
            left.post_lines,
            (change(1, ("value = 2",), ("value = 3",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        classification, inverse, _ = pair_classification(left, right)

        self.assertEqual(classification, "independent_coediting")
        self.assertEqual(inverse["remove_fraction"], 1.0)
        self.assertEqual(inverse["restore_fraction"], 0.0)
        self.assertEqual(inverse["score"], 0.0)


class MechanicalCauseTests(unittest.TestCase):
    def test_detects_whitespace_only_changes(self):
        patch = (change(1, ("answer = 1",), ("answer  =  1",)),)

        self.assertTrue(whitespace_only_patch(patch))
        self.assertEqual(patch_mechanical_kind(patch), "whitespace_only")

    def test_detects_import_reordering_but_not_general_line_reordering(self):
        imports = (
            change(
                1,
                ('import zebra from "zebra";', 'import alpha from "alpha";'),
                ('import alpha from "alpha";', 'import zebra from "zebra";'),
            ),
        )
        ordinary_lines = (
            change(1, ("run_zebra();", "run_alpha();"), ("run_alpha();", "run_zebra();")),
        )

        self.assertTrue(import_order_only_patch(imports))
        self.assertEqual(patch_mechanical_kind(imports), "import_order_only")
        self.assertFalse(import_order_only_patch(ordinary_lines))
        self.assertIsNone(patch_mechanical_kind(ordinary_lines))

    def test_volatile_metadata_contact_rejects_substantive_lines(self):
        timestamp_forward = (
            change(
                1,
                ("updated: 2026-08-24T10:00:00Z",),
                ("updated: 2026-08-24T11:00:00Z",),
            ),
        )
        timestamp_following = (
            change(
                1,
                ("updated: 2026-08-24T11:00:00Z",),
                ("updated: 2026-08-24T12:00:00Z",),
            ),
        )
        substantive_forward = (
            change(1, ("status: pending",), ("status: active",)),
        )
        substantive_following = (
            change(1, ("status: active",), ("status: complete",)),
        )

        self.assertTrue(
            volatile_metadata_only_contact(timestamp_forward, timestamp_following)
        )
        self.assertFalse(
            volatile_metadata_only_contact(
                substantive_forward, substantive_following
            )
        )


class FileClassificationTests(unittest.TestCase):
    def test_markdown_beneath_memory_directory_is_coordination_markdown(self):
        self.assertEqual(
            classify_file(r"c:\repo\.claude\memory\project-notes.md"),
            "coordination_markdown",
        )


class SequenceClassificationTests(unittest.TestCase):
    def make_writes(self, agents):
        return [
            write_operation(
                f"write-{index}",
                agent,
                (f"old-{index}",),
                (change(1, (f"old-{index}",), (f"new-{index}",)),),
                call_ts=float(index * 2 + 1),
                result_ts=float(index * 2 + 2),
            )
            for index, agent in enumerate(agents)
        ]

    def test_aba_sequences_are_oscillations_with_distinct_subtypes(self):
        cases = (
            (("exact_reversal", "exact_reversal"), (1.0, 1.0), "exact_cycle"),
            (
                ("partial_reversal", "partial_reversal"),
                (0.75, 0.75),
                "reversal_reapplication",
            ),
            (
                ("independent_coediting", "independent_coediting"),
                (0.0, 0.0),
                "ABA_only",
            ),
        )
        for classifications, scores, expected_subtype in cases:
            with self.subTest(expected_subtype=expected_subtype):
                writes = self.make_writes(("agent-a", "agent-b", "agent-a"))
                edges = [
                    pair_edge(
                        writes[index],
                        writes[index + 1],
                        classifications[index],
                        score=scores[index],
                    )
                    for index in range(2)
                ]
                self.assertEqual(sequence_classification(writes, edges), "oscillation")
                self.assertEqual(
                    oscillation_subtype(writes, edges), expected_subtype
                )

    def test_non_repeating_agents_do_not_form_an_oscillation(self):
        writes = self.make_writes(("agent-a", "agent-b", "agent-c"))
        edges = [
            pair_edge(writes[0], writes[1], "exact_reversal", score=1.0),
            pair_edge(writes[1], writes[2], "independent_coediting", score=0.0),
        ]

        self.assertEqual(sequence_classification(writes, edges), "exact_reversal")
        self.assertIsNone(oscillation_subtype(writes, edges))

    def test_aba_requires_one_region_to_persist_across_both_edges(self):
        writes = self.make_writes(("agent-a", "agent-b", "agent-a"))
        edges = [
            pair_edge(
                writes[0],
                writes[1],
                "independent_coediting",
                score=0.0,
                contact_pairs=((0, 0),),
            ),
            pair_edge(
                writes[1],
                writes[2],
                "independent_coediting",
                score=0.0,
                contact_pairs=((1, 0),),
            ),
        ]

        self.assertEqual(
            sequence_classification(writes, edges), "independent_coediting"
        )
        self.assertIsNone(oscillation_subtype(writes, edges))

    def test_mechanical_witness_ignores_nonpersistent_boundary_contact(self):
        writes = self.make_writes(("agent-a", "agent-b", "agent-a"))
        edges = [
            pair_edge(
                writes[0],
                writes[1],
                "independent_coediting",
                score=0.0,
                contact_pairs=((0, 0), (0, 1)),
                contact_mechanical_kinds=(
                    ("volatile_metadata_only_overlap",),
                    (),
                ),
            ),
            pair_edge(
                writes[1],
                writes[2],
                "independent_coediting",
                score=0.0,
                contact_pairs=((0, 0),),
                contact_mechanical_kinds=(("volatile_metadata_only_overlap",),),
            ),
        ]

        self.assertEqual(sequence_classification(writes, edges), "oscillation")
        self.assertEqual(
            oscillation_witness_cause_partitions(TEST_PATH, writes, edges),
            ("definite_mechanical_only",),
        )


class PopulationEligibilityTests(unittest.TestCase):
    def test_same_agent_adjacency_is_excluded(self):
        left = write_operation(
            "write-a1",
            "agent-a",
            ("old",),
            (change(1, ("old",), ("new",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        right = write_operation(
            "write-a2",
            "agent-a",
            left.post_lines,
            (change(1, ("new",), ("old",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences((left, right), ())

        self.assertEqual(pairs, [])
        self.assertEqual(sequences, [])
        self.assertEqual(attrition["same_agent_adjacent_pairs"], 1)
        self.assertEqual(attrition.get("eligible_overlapping_pairs_D_pair", 0), 0)

    def test_broken_preimage_continuity_is_excluded(self):
        left = write_operation(
            "write-a",
            "agent-a",
            ("old",),
            (change(1, ("old",), ("new",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        stale_right = write_operation(
            "write-b",
            "agent-b",
            ("stale",),
            (change(1, ("stale",), ("other",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences(
            (left, stale_right), ()
        )

        self.assertEqual(pairs, [])
        self.assertEqual(sequences, [])
        self.assertEqual(attrition["local_state_continuity_breaks"], 1)
        self.assertEqual(attrition.get("eligible_overlapping_pairs_D_pair", 0), 0)

    def test_unrelated_disjoint_change_does_not_break_local_continuity(self):
        left = write_operation(
            "write-a",
            "agent-a",
            ("old", "unchanged"),
            (change(1, ("old",), ("new",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        right = write_operation(
            "write-b",
            "agent-b",
            left.post_lines,
            (
                change(1, ("new",), ("old",)),
                change(2, ("unchanged",), ("unrelated",)),
            ),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences((left, right), ())

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].classification, "exact_reversal")
        self.assertEqual(len(sequences), 1)
        self.assertEqual(attrition["eligible_overlapping_pairs_D_pair"], 1)

    def test_exact_reversal_survives_coordinate_shift_before_changed_region(self):
        left = write_operation(
            "write-a",
            "agent-a",
            ("header", "context", "old", "tail"),
            (change(3, ("old",), ("new",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        shifted_right = write_operation(
            "write-b",
            "agent-b",
            ("unrelated insertion", "header", "context", "new", "tail"),
            (change(4, ("new",), ("old",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences(
            (left, shifted_right), ()
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].classification, "exact_reversal")
        self.assertEqual(attrition["eligible_overlapping_pairs_D_pair"], 1)
        self.assertEqual(len(sequences), 1)
        self.assertEqual(sequences[0].classification, "exact_reversal")

    def test_unrelated_deletion_before_region_recovers_exact_reversal(self):
        left = write_operation(
            "write-a-delete-shift",
            "agent-a",
            ("head", "removed elsewhere", "context", "old", "tail"),
            (change(4, ("old",), ("new",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        shifted_right = write_operation(
            "write-b-delete-shift",
            "agent-b",
            ("head", "context", "new", "tail"),
            (change(3, ("new",), ("old",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences(
            (left, shifted_right), ()
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].classification, "exact_reversal")
        self.assertEqual(len(sequences), 1)
        self.assertEqual(attrition["shifted_coordinate_contacts_recovered"], 1)
        self.assertEqual(attrition["eligible_overlapping_pairs_D_pair"], 1)

    def test_disjoint_earlier_b_hunk_can_shift_exact_inverse_new_coordinate(self):
        left = write_operation(
            "write-a-multihunk",
            "agent-a",
            ("head", "context-a", "context-b", "old", "tail"),
            (change(4, ("old",), ("new",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        multihunk_right = write_operation(
            "write-b-multihunk",
            "agent-b",
            left.post_lines,
            (
                change(2, (), ("unrelated insertion",)),
                change(4, ("new",), ("old",), new_start=5),
            ),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences(
            (left, multihunk_right), ()
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].classification, "exact_reversal")
        self.assertEqual(len(sequences), 1)
        self.assertEqual(attrition["eligible_overlapping_pairs_D_pair"], 1)

    def test_intervening_mutation_inside_changed_content_is_rejected(self):
        left = write_operation(
            "write-a-mutated-region",
            "agent-a",
            ("head", "old-a", "old-b", "tail"),
            (change(2, ("old-a", "old-b"), ("new-a", "new-b")),),
            call_ts=1.0,
            result_ts=2.0,
        )
        mutated_right = write_operation(
            "write-b-mutated-region",
            "agent-b",
            ("head", "new-a", "intervening mutation", "tail"),
            (
                change(
                    2,
                    ("new-a", "intervening mutation"),
                    ("old-a", "old-b"),
                ),
            ),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences(
            (left, mutated_right), ()
        )

        self.assertEqual(pairs, [])
        self.assertEqual(sequences, [])
        self.assertEqual(attrition["contacted_region_alignment_failures"], 1)
        self.assertEqual(attrition.get("eligible_overlapping_pairs_D_pair", 0), 0)

    def test_shifted_deletion_anchor_with_stable_neighbors_is_recovered(self):
        deletion = write_operation(
            "write-a-delete-anchor",
            "agent-a",
            ("head", "before", "removed", "after", "tail"),
            (change(3, ("removed",), ()),),
            call_ts=1.0,
            result_ts=2.0,
        )
        shifted_inverse = write_operation(
            "write-b-delete-anchor",
            "agent-b",
            ("unrelated insertion", "head", "before", "after", "tail"),
            (change(4, (), ("removed",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences(
            (deletion, shifted_inverse), ()
        )

        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].classification, "exact_reversal")
        self.assertEqual(len(sequences), 1)
        self.assertEqual(attrition["shifted_coordinate_contacts_recovered"], 1)
        self.assertEqual(attrition["eligible_overlapping_pairs_D_pair"], 1)

    def test_mutation_exactly_at_deletion_anchor_is_rejected(self):
        deletion = write_operation(
            "write-a-mutated-anchor",
            "agent-a",
            ("head", "before", "removed", "after", "tail"),
            (change(3, ("removed",), ()),),
            call_ts=1.0,
            result_ts=2.0,
        )
        ambiguous_inverse = write_operation(
            "write-b-mutated-anchor",
            "agent-b",
            ("head", "before", "intervening mutation", "after", "tail"),
            (change(3, (), ("removed",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences(
            (deletion, ambiguous_inverse), ()
        )

        self.assertEqual(pairs, [])
        self.assertEqual(sequences, [])
        self.assertEqual(attrition["contacted_region_alignment_failures"], 1)
        self.assertEqual(attrition.get("eligible_overlapping_pairs_D_pair", 0), 0)

    def test_repeated_line_ambiguous_alignment_is_rejected(self):
        left = write_operation(
            "write-a-repeated-line",
            "agent-a",
            ("old", "same", "tail"),
            (change(1, ("old",), ("same",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        ambiguous_right = write_operation(
            "write-b-repeated-line",
            "agent-b",
            ("same", "tail"),
            (change(1, ("same",), ("old",)),),
            call_ts=3.0,
            result_ts=4.0,
        )

        pairs, sequences, attrition = build_pairs_and_sequences(
            (left, ambiguous_right), ()
        )

        self.assertEqual(pairs, [])
        self.assertEqual(sequences, [])
        self.assertEqual(attrition["contacted_region_alignment_failures"], 1)
        self.assertEqual(attrition.get("eligible_overlapping_pairs_D_pair", 0), 0)

    def test_boundary_sensitivity_can_complete_an_aba_sequence(self):
        first_a = write_operation(
            "write-a1",
            "agent-a",
            ("head", "old", "tail"),
            (change(2, ("old",), ("new",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        write_b = write_operation(
            "write-b",
            "agent-b",
            first_a.post_lines,
            (change(3, (), ("marker",)),),
            call_ts=3.0,
            result_ts=4.0,
        )
        second_a = write_operation(
            "write-a2",
            "agent-a",
            write_b.post_lines,
            (change(3, ("marker",), ()),),
            call_ts=5.0,
            result_ts=6.0,
        )

        self.assertFalse(patches_contact(first_a.patch, write_b.patch))
        self.assertTrue(
            patches_contact(
                first_a.patch,
                write_b.patch,
                include_boundary_anchors=True,
            )
        )
        self.assertTrue(patches_contact(write_b.patch, second_a.patch))

        primary_pairs, primary_sequences, _ = build_pairs_and_sequences(
            (first_a, write_b, second_a), ()
        )
        self.assertEqual(len(primary_pairs), 1)
        self.assertEqual(primary_pairs[0].left.tool_id, "write-b")
        self.assertFalse(
            any(
                sequence.classification == "oscillation"
                for sequence in primary_sequences
            )
        )

        boundary_pairs, boundary_sequences, _ = build_pairs_and_sequences(
            (first_a, write_b, second_a),
            (),
            include_boundary_anchors=True,
        )
        self.assertEqual(len(boundary_pairs), 2)
        self.assertEqual(len(boundary_sequences), 1)
        self.assertEqual(
            [write.agent for write in boundary_sequences[0].writes],
            ["agent-a", "agent-b", "agent-a"],
        )
        self.assertEqual(boundary_sequences[0].classification, "oscillation")


class ReadEvidenceTests(unittest.TestCase):
    def make_reversal_edge(self):
        left = write_operation(
            "write-a",
            "agent-a",
            ("head", "old", "tail"),
            (change(2, ("old",), ("new",)),),
            call_ts=1.0,
            result_ts=2.0,
        )
        right = write_operation(
            "write-b",
            "agent-b",
            left.post_lines,
            (change(2, ("new",), ("old",)),),
            call_ts=4.0,
            result_ts=5.0,
        )
        return pair_edge(left, right, "exact_reversal", score=1.0)

    def test_verified_read_must_match_the_reverter_preimage_slice(self):
        edge = self.make_reversal_edge()
        matching = read_operation(
            "read-match",
            "agent-b",
            (2, 3),
            ("new",),
            call_ts=2.5,
            result_ts=3.0,
        )
        mismatching = read_operation(
            "read-mismatch",
            "agent-b",
            (2, 3),
            ("stale",),
            call_ts=2.5,
            result_ts=3.0,
        )

        self.assertTrue(read_matches_preimage(matching, edge.right))
        self.assertFalse(read_matches_preimage(mismatching, edge.right))
        self.assertEqual(
            reversal_read_category(
                edge, {(TEST_PATH, "agent-b"): (matching,)}
            ),
            "post_A_verified_region_read",
        )
        self.assertEqual(
            reversal_read_category(
                edge, {(TEST_PATH, "agent-b"): (mismatching,)}
            ),
            "post_A_offset_only_region_read",
        )


if __name__ == "__main__":
    unittest.main()
