"""Focused tests for the frozen posture co-change radius adapter.

Run from the repository root with
``python -m unittest instruments.posture.tests.test_radius -v``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from instruments.posture.radius import FrozenCochangeRadius

# The adapter intentionally imports the directly executable replay module from
# its own directory.  Import these symbols through that same loaded module so
# this test also exercises backward compatibility of the legacy ranker.
from replay import (  # type: ignore[import-not-found]
    ReplayState,
    cochange_query,
    collect_cochange_histories,
    rank_cochange_histories,
    score_cochange_histories,
)


REQUIRED_LOG_ARGUMENTS = [
    "--first-parent",
    "--reverse",
    "--root",
    "--diff-merges=first-parent",
    "--find-renames=50%",
    "-l0",
    "--name-status",
    "-z",
]


def modified(*paths: str) -> list[dict[str, str]]:
    return [{"status": "M", "raw_status": "M", "path": path} for path in paths]


def renamed(old_path: str, new_path: str) -> dict[str, str]:
    return {
        "status": "R",
        "raw_status": "R100",
        "old_path": old_path,
        "new_path": new_path,
    }


class StreamFixture:
    def __init__(
        self,
        root: Path,
        *,
        initial_files: list[str],
        changes: list[list[dict[str, str]]],
    ) -> None:
        self.stream = root / "fixture.jsonl.gz"
        self.metadata = root / "fixture.meta.json"
        head = f"{len(changes) + 1:040x}"
        initial_tree = f"{0:040x}"
        header = {
            "type": "header",
            "schema_version": 1,
            "source_head_sha": head,
            "initial_tree_sha": initial_tree,
            "initial_files": initial_files,
            "git_log_arguments": REQUIRED_LOG_ARGUMENTS,
        }
        commits: list[dict[str, object]] = []
        previous = initial_tree
        for index, commit_changes in enumerate(changes):
            sha = f"{index + 1:040x}"
            commits.append(
                {
                    "type": "commit",
                    "index": index,
                    "sha": sha,
                    "parents": [previous],
                    "timestamp": index,
                    "changes": commit_changes,
                }
            )
            previous = sha
        # Use the stream's final record as its declared source head.
        if commits:
            head = str(commits[-1]["sha"])
            header["source_head_sha"] = head
        with gzip.open(self.stream, "wt", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(header, sort_keys=True) + "\n")
            for commit in commits:
                handle.write(json.dumps(commit, sort_keys=True) + "\n")
        stream_hash = hashlib.sha256(self.stream.read_bytes()).hexdigest()
        self.metadata.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "ok",
                    "source_head_sha": head,
                    "commit_count": len(commits),
                    "stream_sha256": stream_hash,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


class FrozenRadiusTests(unittest.TestCase):
    def test_cutoff_excludes_future_cochange_and_scores_are_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StreamFixture(
                Path(directory),
                initial_files=["a.py", "b.py", "c.py", "future.py"],
                changes=[
                    modified("a.py", "b.py"),
                    modified("a.py", "b.py"),
                    modified("a.py", "c.py"),
                    modified("a.py", "future.py"),
                ],
            )
            radius = FrozenCochangeRadius.from_stream(
                fixture.stream,
                cutoff_index=3,
                expected_cutoff_sha=f"{4:040x}",
                top_k=3,
                threshold=0.0,
                threshold_inclusive=False,
                decayed=False,
            )

            selected = radius.radius_for("a.py")
            self.assertEqual(
                [(item.path, item.score) for item in selected.candidates],
                [("b.py", 2 / 3), ("c.py", 1 / 3)],
            )
            self.assertNotIn("future.py", [item.path for item in selected.candidates])
            self.assertEqual(radius.provenance["freeze"]["history_commit_count"], 3)
            self.assertEqual(radius.provenance["freeze"]["first_excluded_sha"], f"{4:040x}")

    def test_threshold_boundary_policy_and_top_k_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StreamFixture(
                Path(directory),
                initial_files=["a", "b", "c"],
                changes=[modified("a", "b"), modified("a", "c")],
            )
            strict = FrozenCochangeRadius.from_stream(
                fixture.stream,
                cutoff_index=2,
                top_k=2,
                threshold=0.5,
                threshold_inclusive=False,
                decayed=False,
            )
            inclusive = FrozenCochangeRadius.from_stream(
                fixture.stream,
                cutoff_index=2,
                top_k=1,
                threshold=0.5,
                threshold_inclusive=True,
                decayed=False,
            )

            self.assertEqual(strict.radius_for("a").candidates, ())
            self.assertEqual(
                [(item.path, item.score) for item in inclusive.radius_for("a").candidates],
                [("b", 0.5)],
            )
            self.assertEqual(
                strict.provenance["radius"]["threshold_comparison"], ">"
            )
            self.assertEqual(
                inclusive.provenance["radius"]["threshold_comparison"], ">="
            )

    def test_rename_keeps_identity_and_new_path_is_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StreamFixture(
                Path(directory),
                initial_files=["old.py", "peer.py"],
                changes=[
                    modified("old.py", "peer.py"),
                    [renamed("old.py", "new.py"), *modified("peer.py")],
                ],
            )
            radius = FrozenCochangeRadius.from_stream(
                fixture.stream,
                cutoff_index=2,
                top_k=5,
                threshold=0.0,
                threshold_inclusive=False,
                decayed=False,
            )

            self.assertEqual(
                [(item.path, item.score) for item in radius.radius_for("new.py").candidates],
                [("peer.py", 1.0)],
            )
            with self.assertRaises(KeyError):
                radius.radius_for("old.py")

    def test_multi_seed_union_retains_contributions_and_omits_direct_seeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StreamFixture(
                Path(directory),
                initial_files=["a", "b", "shared"],
                changes=[
                    modified("a", "b", "shared"),
                    modified("a", "shared"),
                ],
            )
            radius = FrozenCochangeRadius.from_stream(
                fixture.stream,
                cutoff_index=2,
                top_k=5,
                threshold=0.0,
                threshold_inclusive=False,
                decayed=False,
            )

            query = radius.query(["a", "b", "a"])
            self.assertEqual(query.seeds, ("a", "b"))
            self.assertEqual([item.path for item in query.union], ["shared"])
            self.assertEqual(
                query.union[0].seed_scores,
                (("a", 1.0), ("b", 1.0)),
            )

    def test_provenance_is_hashed_defensive_and_atomically_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = StreamFixture(
                root,
                initial_files=["a", "b"],
                changes=[modified("a", "b")],
            )
            radius = FrozenCochangeRadius.from_stream(
                fixture.stream,
                cutoff_index=1,
                top_k=1,
                threshold=0.1,
                threshold_inclusive=True,
            )
            first = radius.provenance
            first["freeze"]["cutoff_index"] = 999
            self.assertEqual(radius.provenance["freeze"]["cutoff_index"], 1)

            output = root / "provenance.json"
            radius.write_provenance(output)
            persisted = json.loads(output.read_text(encoding="utf-8"))
            recorded_hash = persisted.pop("provenance_sha256")
            canonical = json.dumps(
                persisted,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            self.assertEqual(recorded_hash, hashlib.sha256(canonical).hexdigest())

    def test_metadata_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StreamFixture(
                Path(directory),
                initial_files=["a", "b"],
                changes=[modified("a", "b")],
            )
            metadata = json.loads(fixture.metadata.read_text(encoding="utf-8"))
            metadata["stream_sha256"] = "0" * 64
            fixture.metadata.write_text(json.dumps(metadata), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "SHA-256"):
                FrozenCochangeRadius.from_stream(
                    fixture.stream,
                    cutoff_index=1,
                    top_k=1,
                    threshold=0.0,
                    threshold_inclusive=False,
                )


class ReplayBackwardCompatibilityTests(unittest.TestCase):
    def test_legacy_ranking_uses_the_exposed_score_formula_unchanged(self) -> None:
        for decayed in (False, True):
            with self.subTest(decayed=decayed):
                state = ReplayState(["a", "b", "c"])
                state.fold(0, state.resolve_changes(modified("a", "b")))
                state.fold(1, state.resolve_changes(modified("a", "c")))
                seed = state.path_to_id["a"]
                seed_history, histories = collect_cochange_histories(state, seed, 2)
                scores = score_cochange_histories(
                    state,
                    seed_history,
                    histories,
                    2,
                    decayed=decayed,
                )
                ranked = rank_cochange_histories(
                    state,
                    seed_history,
                    histories,
                    2,
                    decayed=decayed,
                )

                self.assertEqual(ranked, cochange_query(state, seed, 2, decayed=decayed))
                self.assertEqual(set(ranked.ids), set(scores))


if __name__ == "__main__":
    unittest.main()
