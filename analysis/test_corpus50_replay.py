"""Focused tests for the external Corpus-50 replay orchestration.

These tests never invoke Git, clone a repository, or write to the real replay
corpus/results.  Run from the repository root with::

    python -m unittest analysis.test_corpus50_replay -v
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from analysis import corpus50_replay as driver


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def member_raw(
    index: int,
    name: str,
    cohort: str,
    *,
    count: int = 500,
) -> dict[str, object]:
    return {
        "selection_order": index,
        "slug": name.replace("/", "__"),
        "name": name,
        "url": f"https://github.com/{name}.git",
        "cohort": cohort,
        "first_parent_commit_count": count,
        "selection_status": "selected",
    }


def incomplete_manifest(path: Path, counts: tuple[int, ...] = (800, 500)) -> driver.CorpusManifest:
    members = [
        member_raw(index, f"fixture/repo-{index}", "base", count=count)
        for index, count in enumerate(counts)
    ]
    write_json(
        path,
        {
            "rule_id": driver.RULE_ID,
            "seed": driver.RULE_SEED,
            "scope_name": driver.SCOPE_NAME,
            "listing_dates": {"base": "2026-08-22", "stress": "2026-08-23"},
            "disk_cap_bytes": 20 * driver.GIB,
            "members": members,
        },
    )
    return driver.CorpusManifest.load(path, allow_incomplete=True)


def production_manifest(path: Path) -> driver.CorpusManifest:
    raw_members: list[dict[str, object]] = []
    for name in driver.RETAINED_ANCHORS:
        raw_members.append(member_raw(len(raw_members), name, "retained_anchor", count=700))
    for index in range(35):
        raw_members.append(member_raw(len(raw_members), f"base/member-{index}", "base", count=900 - index))
    for index in range(5):
        raw_members.append(member_raw(len(raw_members), f"stress/member-{index}", "stress", count=600 + index))
    write_json(
        path,
        {
            "rule_id": driver.RULE_ID,
            "seed": driver.RULE_SEED,
            "scope_name": driver.SCOPE_NAME,
            "listing_dates": {"base": "2026-08-22", "stress": "2026-08-23"},
            "disk_cap_bytes": 20 * driver.GIB,
            "members": raw_members,
        },
    )
    return driver.CorpusManifest.load(path)


class ManifestTests(unittest.TestCase):
    def test_exact_membership_and_smallest_first_are_separate_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = production_manifest(Path(temporary) / "manifest.json")
            self.assertEqual(len(manifest.canonical_order), 50)
            self.assertEqual(manifest.canonical_order[0], "hashicorp__terraform-provider-random")
            additions = driver.choose_members(manifest, {}, group="additions", requested_slugs=[])
            self.assertEqual(len(additions), 40)
            counts = [member.first_parent_commit_count for member in additions]
            self.assertEqual(counts, sorted(counts))
            # Execution sorting must not mutate the manifest's canonical order.
            self.assertNotEqual([member.slug for member in additions], list(manifest.canonical_order[10:]))

    def test_production_manifest_rejects_wrong_realised_cohort_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            manifest = production_manifest(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["members"].pop()
            write_json(path, raw)
            with self.assertRaises(driver.ManifestError):
                driver.CorpusManifest.load(path)
            self.assertEqual(len(manifest.members), 50)

    def test_one_based_selection_order_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            raw = [member_raw(1, "fixture/a", "base"), member_raw(2, "fixture/b", "base")]
            write_json(path, {"rule_id": driver.RULE_ID, "scope_name": driver.SCOPE_NAME, "members": raw})
            manifest = driver.CorpusManifest.load(path, allow_incomplete=True)
            self.assertEqual([member.selection_order for member in manifest.members], [0, 1])


class PersistenceTests(unittest.TestCase):
    def test_sync_corpus_preserves_unrelated_records_and_installs_50_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = production_manifest(root / "manifest.json")
            paths = driver.HarnessPaths(
                corpus=root / "CORPUS.json",
                clones=root / "clones",
                streams=root / "streams",
                results=root / "results",
            )
            write_json(
                paths.corpus,
                {
                    "repository_order": ["old_static_order"],
                    "repositories": {"not_selected_but_preserved": {"status": "ok"}},
                    "foreign_key": "preserve me",
                },
            )
            document = driver.sync_corpus_document(paths, manifest)
            self.assertEqual(document["repository_order"], list(manifest.canonical_order))
            self.assertIn("not_selected_but_preserved", document["repositories"])
            self.assertEqual(document["foreign_key"], "preserve me")

    def test_state_refuses_silent_manifest_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = incomplete_manifest(root / "first.json")
            policy = driver.DiskPolicy((root,), volume_minimums={})
            driver.StateLog(root / "state.json", first, first.members, policy)
            second_path = root / "second.json"
            second = incomplete_manifest(second_path, counts=(801, 500))
            with self.assertRaises(driver.StageError):
                driver.StateLog(root / "state.json", second, second.members, policy)

    def test_plan_only_does_not_create_state_or_touch_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path = root / "manifest.json"
            incomplete_manifest(manifest_path)
            corpus_path = root / "CORPUS.json"
            write_json(corpus_path, {"sentinel": True, "repositories": {}})
            before = corpus_path.read_bytes()
            state_path = root / "state.json"
            output = io.StringIO()
            with mock.patch.object(
                driver.replay_common, "CORPUS_PATH", corpus_path
            ), contextlib.redirect_stdout(output):
                exit_code = driver.main(
                    [
                        "--manifest",
                        str(manifest_path),
                        "--allow-incomplete-manifest",
                        "--skip-volume-guards",
                        "--dry-run",
                        "--state",
                        str(state_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(corpus_path.read_bytes(), before)
            self.assertFalse(state_path.exists())
            self.assertEqual(json.loads(output.getvalue())["mode"], "plan_only")


class RunnerTests(unittest.TestCase):
    def test_pipeline_calls_existing_functions_and_reinspects_written_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = incomplete_manifest(root / "manifest.json", counts=(500,))
            member = manifest.members[0]
            paths = driver.HarnessPaths(
                corpus=root / "CORPUS.json",
                clones=root / "clones",
                streams=root / "streams",
                results=root / "results",
            )
            policy = driver.DiskPolicy(
                driver.normalized_roots((paths.clones, paths.streams, paths.results)),
                volume_minimums={},
            )
            stream_hash: dict[str, str] = {}

            def cloned(spec: dict[str, str], document: dict[str, object]) -> None:
                clone = paths.clones / member.slug
                (clone / ".git").mkdir(parents=True)
                record = {
                    **spec,
                    "status": "ok",
                    "resolved_head_sha": "a" * 40,
                    "reachable_commit_count": 500,
                    "first_parent_commit_count": 500,
                    "tracked_file_count_at_head": 1,
                    "partial_clone_promisor": True,
                }
                document["repositories"][member.slug] = record
                driver.atomic_write_json(paths.corpus, document)

            def extracted(spec: dict[str, str], corpus_record: dict[str, object]) -> dict[str, object]:
                stream = paths.stream(member.slug)
                stream.parent.mkdir(parents=True, exist_ok=True)
                header = {
                    "type": "header",
                    "schema_version": driver.replay_common.SCHEMA_VERSION,
                    "source_head_sha": "a" * 40,
                    "capped": False,
                    "git_log_arguments": [
                        "--first-parent",
                        "--reverse",
                        "--root",
                        "--diff-merges=first-parent",
                        "--find-renames=50%",
                        "-l0",
                        "--name-status",
                        "-z",
                    ],
                }
                with gzip.open(stream, "wt", encoding="utf-8") as handle:
                    handle.write(json.dumps(header) + "\n")
                stream_hash["value"] = driver.sha256_file(stream)
                return {
                    "status": "ok",
                    "source_head_sha": "a" * 40,
                    "commit_count": 500,
                    "capped": False,
                    "stream_sha256": stream_hash["value"],
                }

            def replayed(spec: dict[str, str], corpus_record: dict[str, object]) -> dict[str, object]:
                return {
                    "status": "ok",
                    "source_head_sha": "a" * 40,
                    "implementation": {"harness_sha256": "current", "stream_sha256": stream_hash["value"]},
                    "coverage": {"commits_replayed": 500},
                }

            runner = driver.Runner(
                manifest,
                [member],
                policy,
                root / "state.json",
                paths=paths,
                force_stages=driver.STAGES,
                poll_seconds=60,
            )
            with mock.patch.object(
                driver.replay_clone, "process_repository", side_effect=cloned
            ) as clone_call, mock.patch.object(
                driver.replay_extract, "extract_repository", side_effect=extracted
            ) as extract_call, mock.patch.object(
                driver.replay_run, "run_repository", side_effect=replayed
            ) as replay_call, mock.patch.object(
                driver.replay_run, "harness_hashes", return_value=("current", {})
            ):
                runner.run()

            clone_call.assert_called_once()
            extract_call.assert_called_once()
            replay_call.assert_called_once()
            state = driver.read_json(root / "state.json")
            stages = state["repositories"][member.slug]["stages"]
            self.assertEqual(
                {stage: stages[stage]["status"] for stage in driver.STAGES},
                {stage: "ok" for stage in driver.STAGES},
            )
            self.assertEqual(driver.read_json(paths.result(member.slug))["status"], "ok")

    def test_return_value_is_not_trusted_when_clone_status_json_says_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = incomplete_manifest(root / "manifest.json", counts=(500,))
            member = manifest.members[0]
            paths = driver.HarnessPaths(root / "CORPUS.json", root / "clones", root / "streams", root / "results")
            policy = driver.DiskPolicy((root / "accounted",), volume_minimums={})

            def failed_clone(spec: dict[str, str], document: dict[str, object]) -> None:
                document["repositories"][member.slug] = {**spec, "status": "failed", "failure": "fixture"}
                driver.atomic_write_json(paths.corpus, document)

            runner = driver.Runner(
                manifest,
                [member],
                policy,
                root / "state.json",
                paths=paths,
                force_stages=driver.STAGES,
                poll_seconds=60,
            )
            with mock.patch.object(
                driver.replay_clone, "process_repository", side_effect=failed_clone
            ), mock.patch.object(driver.replay_extract, "extract_repository") as extract_call:
                runner.run()
            extract_call.assert_not_called()
            state = driver.read_json(root / "state.json")
            self.assertEqual(state["repositories"][member.slug]["stages"]["clone"]["status"], "failed")
            self.assertEqual(driver.read_json(paths.stream_meta(member.slug))["status"], "failed")
            failed_result = driver.read_json(paths.result(member.slug))
            self.assertEqual(failed_result["status"], "failed")
            self.assertEqual(failed_result["failure_stage"], "clone")

    def test_applied_replay_cap_is_explicitly_logged_as_non_comparable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = incomplete_manifest(root / "manifest.json", counts=(25_000,))
            member = manifest.members[0]
            paths = driver.HarnessPaths(root / "CORPUS.json", root / "clones", root / "streams", root / "results")
            runner = driver.Runner(
                manifest,
                [member],
                driver.DiskPolicy((root / "accounted",), volume_minimums={}),
                root / "state.json",
                paths=paths,
                stop_stage="clone",
            )
            runner._record_cap(
                member,
                {"reachable_commit_count": 25_001, "first_parent_commit_count": 25_000},
                "fixture",
            )
            state = driver.read_json(root / "state.json")
            cap = state["repositories"][member.slug]["cap"]
            self.assertTrue(cap["applied"])
            self.assertTrue(cap["left_truncated"])
            self.assertTrue(cap["learned_indexes_start_empty"])
            self.assertTrue(cap["non_comparable_for_warm_history_claims"])
            self.assertEqual(cap["replay_commits"], 5_000)


class DiskTests(unittest.TestCase):
    def test_nested_accounted_paths_are_not_double_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            (nested / "payload").write_bytes(b"x" * 31)
            roots = driver.normalized_roots((nested, root, nested))
            self.assertEqual(roots, (root.resolve(),))
            policy = driver.DiskPolicy(roots, total_cap_bytes=30, volume_minimums={})
            snapshot = policy.snapshot()
            self.assertEqual(snapshot.accounted_bytes, 31)
            self.assertTrue(snapshot.violations)

    def test_hard_cap_stops_before_any_repository_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = incomplete_manifest(root / "manifest.json", counts=(500,))
            member = manifest.members[0]
            paths = driver.HarnessPaths(root / "CORPUS.json", root / "clones", root / "streams", root / "results")
            accounted = root / "accounted"
            accounted.mkdir()
            (accounted / "payload").write_bytes(b"over")
            runner = driver.Runner(
                manifest,
                [member],
                driver.DiskPolicy((accounted,), total_cap_bytes=3, volume_minimums={}),
                root / "state.json",
                paths=paths,
            )
            with mock.patch.object(driver.replay_clone, "process_repository") as clone_call:
                with self.assertRaises(driver.DiskGuardViolation):
                    runner.run()
            clone_call.assert_not_called()
            events = driver.read_json(root / "state.json")["events"]
            self.assertEqual(events[-1]["event"], "disk_guard_run_start")
            self.assertTrue(events[-1]["disk"]["violations"])


if __name__ == "__main__":
    unittest.main()
