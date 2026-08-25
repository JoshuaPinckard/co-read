"""Focused invariants for the deterministic conflict analysis.

Run from the repository root with::

    python -m unittest instruments.conflicts.test_analyze -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from instruments.conflicts import analyze


def occurrence(
    *,
    merge: str,
    path: str,
    kind: str = "handwritten",
    size: int | None = 100,
    conflicted: int | None = 10,
    slug: str = "example__repo",
) -> analyze.ConflictOccurrence:
    return analyze.ConflictOccurrence(
        repo=slug.replace("__", "/"),
        slug=slug,
        merge=merge,
        path=path,
        language="Python",
        shape="library",
        kind=kind,
        range_status="ok" if conflicted is not None else "structural",
        file_size=size,
        conflicted_bytes=conflicted,
    )


class ArithmeticTests(unittest.TestCase):
    def test_determinism_status_fails_closed_on_stale_source_hash(self) -> None:
        result = analyze.determinism_status(
            {
                "schema_version": 1,
                "miner_protocol_revision": analyze.MINER_PROTOCOL_REVISION,
                "miner_source_sha256": "0" * 64,
                "all_byte_identical": True,
                "repositories": [],
                "known_case": {"byte_identical": True, "exit_codes": [1, 1]},
                "known_clean_case": {"byte_identical": True, "exit_codes": [0, 0]},
            }
        )
        self.assertFalse(result["passed"])
        self.assertTrue(
            any("miner source hash" in problem for problem in result["problems"])
        )

    def test_strict_overlap_denominator_retains_boundary_only_cases(self) -> None:
        spec = analyze.RepositorySpec("o__r", "o/r", "Python", "library", None, None, {})
        repository = analyze.RepositoryData(
            spec,
            {},
            [],
            [
                {"overlap": {"classification": "overlap"}},
                {"overlap": {"classification": "boundary_only"}},
                {"overlap": {"classification": "same_file_disjoint"}},
            ],
            None,
        )
        summary = analyze.overlap_summary([repository])
        self.assertEqual(summary["strict_overlap_rate"], analyze.rate(1, 3))
        self.assertEqual(summary["boundary_inclusive_overlap_rate"], analyze.rate(2, 3))

    def test_boundary_with_unknown_is_only_boundary_decidable(self) -> None:
        spec = analyze.RepositorySpec("o__r", "o/r", "Python", "library", None, None, {})
        repository = analyze.RepositoryData(
            spec,
            {},
            [],
            [
                {"overlap": {"classification": "overlap"}},
                {"overlap": {"classification": "same_file_disjoint"}},
                {"overlap": {"classification": "boundary_with_unclassifiable"}},
                {"overlap": {"classification": "mixed_unclassifiable"}},
            ],
            None,
        )
        summary = analyze.overlap_summary([repository])
        self.assertEqual(summary["strict_overlap_rate"], analyze.rate(1, 2))
        self.assertEqual(summary["strict_classification_coverage"], analyze.rate(2, 4))
        self.assertEqual(summary["boundary_inclusive_overlap_rate"], analyze.rate(2, 3))
        self.assertEqual(summary["boundary_classification_coverage"], analyze.rate(3, 4))

    def test_preparation_summary_removes_machine_paths(self) -> None:
        summary = analyze.summarize_preparation(
            {
                "results": [
                    {
                        "repo": "o/r",
                        "slug": "o__r",
                        "status": "prepared",
                        "clone_mode": "reference-and-dissociate",
                        "source": "C:/private/source",
                        "verification": {
                            "bare": True,
                            "promisor": True,
                            "alternates": False,
                            "partial_clone_filter": "blob:none",
                            "pinned_commit": "a" * 40,
                        },
                    }
                ]
            }
        )
        self.assertEqual(summary["repository_count"], 1)
        self.assertEqual(summary["independent_bare_partial_mirrors_verified"], 1)
        self.assertNotIn("private", json.dumps(summary))

    def test_hydration_summary_records_no_lazy_fetch_audit(self) -> None:
        summary = analyze.summarize_hydration(
            {
                "discovery_lazy_fetch": False,
                "results": [
                    {
                        "status": "already_hydrated",
                        "missing_before_count": 0,
                        "missing_after_count": 0,
                        "fetch_batch_count": 0,
                    },
                    {
                        "status": "hydrated",
                        "missing_before_count": 3,
                        "missing_after_count": 0,
                        "fetch_batch_count": 1,
                    },
                ],
            }
        )
        self.assertEqual(summary["repository_count"], 2)
        self.assertEqual(summary["repositories_complete_after"], 2)
        self.assertEqual(summary["missing_before_count"], 3)
        self.assertEqual(summary["missing_after_count"], 0)
        self.assertEqual(summary["fetch_batch_count"], 1)
        self.assertFalse(summary["discovery_lazy_fetch"])

    def test_prefixed_unavailable_range_status_rejects_regions(self) -> None:
        spec = analyze.RepositorySpec("o__r", "o/r", "Python", "library", None, None, {})
        repository = analyze.RepositoryData(spec, {}, [], [], None)
        conflict = {
            "path": "src/example.py",
            "classification": {"kind": "handwritten"},
            "range_status": "unavailable_no_text_markers",
            "result_blob": {"oid": "a" * 40, "size": 10},
            "conflicted_bytes": 2,
            "conflicted_fraction": 0.2,
            "regions": [
                {
                    "blob_oid": "a" * 40,
                    "blob_size": 10,
                    "byte_start": 1,
                    "byte_end": 3,
                    "coordinate_space": "merge-tree-result-blob",
                    "includes_marker_lines": True,
                    "line_start": 1,
                    "line_end": 1,
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "unavailable range status"):
            analyze.parse_occurrence(repository, "m", conflict, source="test")

    def test_zero_denominator_is_undefined(self) -> None:
        value = analyze.rate(0, 0)
        self.assertIsNone(value["value"])
        self.assertEqual(analyze.format_rate(value), "0 / 0 (undefined)")
        self.assertIsNone(analyze.wilson_interval(0, 0))

    def test_wilson_handles_zero_successes_without_zero_width(self) -> None:
        low, high = analyze.wilson_interval(0, 10) or (None, None)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high or 0.0, 0.2775327998628892)

    def test_union_uses_half_open_ranges_and_does_not_double_count(self) -> None:
        self.assertEqual(analyze.union_length([(0, 4), (2, 6), (6, 7), (10, 12)]), 9)

    def test_auc_uses_half_credit_for_ties(self) -> None:
        self.assertEqual(analyze.auc_probability_of_superiority([3], [1, 3, 4]), 0.5)
        self.assertEqual(analyze.auc_probability_of_superiority([5], [1, 2]), 1.0)

    def test_seeded_bootstrap_is_repeatable(self) -> None:
        first = analyze.bootstrap_percentile_interval(
            [0.25, 0.5, 0.75], label="test", base_seed="seed", replicates=200
        )
        second = analyze.bootstrap_percentile_interval(
            [0.25, 0.5, 0.75], label="test", base_seed="seed", replicates=200
        )
        self.assertEqual(first, second)

    def test_repository_cluster_interval_is_repeatable_and_requires_three_contributors(self) -> None:
        counts = {"a": (1, 2), "b": (0, 2), "c": (2, 2), "zero": (0, 0)}
        first = analyze.cluster_rate_interval(
            counts, label="cluster", base_seed="seed", replicates=250
        )
        second = analyze.cluster_rate_interval(
            counts, label="cluster", base_seed="seed", replicates=250
        )
        self.assertEqual(first, second)
        self.assertEqual(first[1], 3)
        self.assertEqual(first[2], 250)
        self.assertIsNotNone(first[0])
        withheld = analyze.cluster_rate_interval(
            {"a": (1, 2), "b": (0, 2), "zero": (0, 0)},
            label="cluster-small",
            base_seed="seed",
            replicates=250,
        )
        self.assertIsNone(withheld[0])

    def test_bins_are_half_open_at_declared_boundaries(self) -> None:
        self.assertEqual(analyze.select_bin(1, analyze.COMMIT_BINS).key, "1")
        self.assertEqual(analyze.select_bin(2, analyze.COMMIT_BINS).key, "2_3")
        self.assertEqual(analyze.select_bin(128, analyze.COMMIT_BINS).key, "128_plus")

    def test_failed_evaluation_is_not_clean(self) -> None:
        self.assertFalse(
            analyze.row_is_evaluable(
                {"evaluation_status": "failed", "conflicted": False}
            )
        )

    def test_surrogate_escaped_git_path_has_stable_bytes_and_safe_markdown(self) -> None:
        path = "bad-\udcff-name"
        self.assertEqual(analyze.stable_path_bytes(path), b"bad-\xff-name")
        self.assertEqual(analyze.markdown_escape(path), r"bad-\udcff-name")


class DistributionTests(unittest.TestCase):
    def test_top_one_percent_uses_ceil_and_deterministic_tie_break(self) -> None:
        rows = [
            occurrence(merge=f"m{i}", path=f"src/f{i:03}.py")
            for i in range(101)
        ]
        rows.extend(
            [
                occurrence(merge="repeat-a", path="src/f100.py"),
                occurrence(merge="repeat-b", path="src/f100.py"),
                occurrence(merge="repeat-c", path="src/f099.py"),
            ]
        )
        result = analyze.concentration(rows, stratum="handwritten")
        self.assertEqual(result["distinct_repo_paths"], 101)
        self.assertEqual(result["top_one_percent_file_count"], 2)
        self.assertEqual(
            [item["path"] for item in result["top_files"]],
            ["src/f100.py", "src/f099.py"],
        )
        self.assertEqual(result["top_one_percent_occurrence_share"]["numerator"], 5)
        self.assertEqual(result["top_one_percent_occurrence_share"]["denominator"], 104)

    def test_concentration_splits_artifacts_from_handwritten(self) -> None:
        rows = [
            occurrence(merge="a", path="src/a.py"),
            occurrence(merge="b", path="Cargo.lock", kind="lockfile"),
            occurrence(merge="c", path="generated.pb.go", kind="generated"),
        ]
        self.assertEqual(
            analyze.concentration(rows, stratum="handwritten")["conflict_file_occurrences"],
            1,
        )
        self.assertEqual(
            analyze.concentration(rows, stratum="artifacts")["conflict_file_occurrences"],
            2,
        )

    def test_granularity_keeps_structural_conflicts_in_the_denominator(self) -> None:
        rows = [
            occurrence(merge="a", path="src/a.py", size=100, conflicted=10),
            occurrence(merge="b", path="src/b.py", size=None, conflicted=None),
        ]
        result = analyze.granularity(rows, stratum="all")
        self.assertEqual(result["measurement_coverage"], analyze.rate(1, 2))
        self.assertEqual(result["file_ratio_distribution"]["median"], 0.1)
        self.assertEqual(result["threshold_counts"]["at_most_10_percent"], analyze.rate(1, 1))


class EndToEndTests(unittest.TestCase):
    def write_json(self, path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def write_jsonl(self, path: Path, rows: list[dict[str, object]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def merge_row(self, repo: str, merge: str, conflicted: bool, exposure: int) -> dict[str, object]:
        return {
            "repo": repo,
            "merge": merge,
            "miner_protocol_revision": analyze.MINER_PROTOCOL_REVISION,
            "miner_source_sha256": analyze.MINER_SOURCE_SHA256,
            "conflicted": conflicted,
            "evaluation_status": "evaluated",
            "divergence": {
                "combined_commits": exposure,
                "max_wall_clock_seconds": exposure * 86_400,
                "negative_clock": False,
            },
            "diffs": {
                "parent1": {"lines_changed": exposure * 10, "binary_files": 0},
                "parent2": {"lines_changed": exposure * 5, "binary_files": 0},
            },
        }

    def conflict_row(
        self,
        repo: str,
        merge: str,
        exposure: int,
        *,
        path: str,
        kind: str,
        candidate: bool,
        overlap: str,
    ) -> dict[str, object]:
        row = self.merge_row(repo, merge, True, exposure)
        row.update(
            {
                "conflicts": [
                    {
                        "path": path,
                        "classification": {"kind": kind},
                        "range_status": "measured_text_markers",
                        "result_blob": {"oid": "d" * 40, "size": 100},
                        "conflicted_bytes": 15,
                        "conflicted_fraction": 0.15,
                        "regions": [
                            {
                                "blob_oid": "d" * 40,
                                "blob_size": 100,
                                "byte_start": 10,
                                "byte_end": 20,
                                "coordinate_space": "merge-tree-result-blob",
                                "includes_marker_lines": True,
                                "line_start": 2,
                                "line_end": 3,
                            },
                            {
                                "blob_oid": "d" * 40,
                                "blob_size": 100,
                                "byte_start": 18,
                                "byte_end": 25,
                                "coordinate_space": "merge-tree-result-blob",
                                "includes_marker_lines": True,
                                "line_start": 4,
                                "line_end": 5,
                            },
                        ],
                    }
                ],
                "conflicted_paths": [path],
                "overlap": {
                    "classification": overlap,
                    "paths": [{"path": path}],
                    "rule_revision": analyze.OVERLAP_REVISION,
                },
                "both_sides_touched_tests": candidate,
            }
        )
        return row

    def test_full_report_is_byte_identical_and_reconciles_zero_merge_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus = root / "corpus"
            output = root / "output"
            manifest = root / "repositories.json"
            repositories = [
                {
                    "slug": "a__alpha",
                    "repo": "a/alpha",
                    "url": "https://example.invalid/a/alpha.git",
                    "frozen_head": "a" * 40,
                    "primary_language": "Python",
                    "primary_shape": "library",
                    "project_shape_note": "Python library",
                },
                {
                    "slug": "b__beta",
                    "repo": "b/beta",
                    "url": "https://example.invalid/b/beta.git",
                    "frozen_head": "b" * 40,
                    "primary_language": "Go",
                    "primary_shape": "infrastructure",
                    "project_shape_note": "Go infrastructure",
                },
                {
                    "slug": "c__squash",
                    "repo": "c/squash",
                    "url": "https://example.invalid/c/squash.git",
                    "frozen_head": "c" * 40,
                    "primary_language": "TypeScript",
                    "primary_shape": "application",
                    "project_shape_note": "squash workflow",
                },
            ]
            self.write_json(manifest, {"schema_version": 1, "repositories": repositories})

            for repository in repositories:
                slug = str(repository["slug"])
                repo = str(repository["repo"])
                if slug == "c__squash":
                    all_rows: list[dict[str, object]] = []
                    conflict_rows: list[dict[str, object]] = []
                else:
                    clean = self.merge_row(repo, f"{slug}-clean", False, 1)
                    rich = self.conflict_row(
                        repo,
                        f"{slug}-conflict",
                        8,
                        path="Cargo.lock" if slug == "b__beta" else "src/main.py",
                        kind="lockfile" if slug == "b__beta" else "handwritten",
                        candidate=slug == "a__alpha",
                        overlap="overlap" if slug == "a__alpha" else "same_file_disjoint",
                    )
                    all_rows = [clean, self.merge_row(repo, f"{slug}-conflict", True, 8)]
                    conflict_rows = [rich]
                all_path = corpus / "_all_merges" / f"{slug}.jsonl"
                conflict_path = corpus / f"{slug}.jsonl"
                self.write_jsonl(all_path, all_rows)
                self.write_jsonl(conflict_path, conflict_rows)
                summary = {
                    "schema_version": 1,
                    "repo": repo,
                    "slug": slug,
                    "miner_protocol_revision": analyze.MINER_PROTOCOL_REVISION,
                    "miner_source_sha256": analyze.MINER_SOURCE_SHA256,
                    "classification_revision": analyze.CLASSIFICATION_REVISION,
                    "conflict_range_revision": analyze.CONFLICT_RANGE_REVISION,
                    "overlap_revision": analyze.OVERLAP_REVISION,
                    "test_path_revision": analyze.TEST_PATH_REVISION,
                    "git_version": "git version test",
                    "git_environment_overrides": dict(
                        sorted(analyze.DETERMINISTIC_GIT_ENVIRONMENT_OVERRIDES.items())
                    ),
                    "git_environment_scrubbed": {
                        "exact": list(analyze.SCRUBBED_GIT_ENVIRONMENT_KEYS),
                        "prefixes": list(analyze.SCRUBBED_GIT_ENVIRONMENT_PREFIXES),
                    },
                    "first_parent_commits": 10,
                    "eligible_two_parent_merges": len(all_rows),
                    "evaluable_merges": len(all_rows),
                    "clean_merges": len(all_rows) - len(conflict_rows),
                    "conflicted_merges": len(conflict_rows),
                    "failed_merges": 0,
                    "mirror_verification": {
                        "alternates": False,
                        "bare": True,
                        "direct_child": True,
                        "origin": repository["url"],
                        "partial_clone_filter": "blob:none",
                        "promisor": True,
                        "reparse_point": False,
                        "shallow": False,
                    },
                    "python_implementation": "CPython",
                    "python_version": "test",
                    "merge_tree_invocation": list(analyze.MERGE_TREE_INVOCATION),
                    "merge_tree_interpretation": analyze.MERGE_TREE_INTERPRETATION,
                    "storage_policy": analyze.STORAGE_POLICY,
                    "classification_rule": analyze.CLASSIFICATION_RULE,
                    "overlap_rule": analyze.OVERLAP_RULE,
                    "test_path_rule": analyze.TEST_PATH_RULE,
                    "output_sha256": {
                        "all_merges": analyze.sha256_file(all_path),
                        "conflicts": analyze.sha256_file(conflict_path),
                    },
                }
                self.write_json(corpus / "_summaries" / f"{slug}.json", summary)

            determinism_rows = []
            for repository in repositories:
                slug = str(repository["slug"])
                relative_paths = [
                    Path(f"{slug}.jsonl"),
                    Path("_all_merges") / f"{slug}.jsonl",
                    Path("_summaries") / f"{slug}.json",
                ]
                files = []
                for relative in relative_paths:
                    canonical_path = corpus / relative
                    digest = analyze.sha256_file(canonical_path)
                    files.append(
                        {
                            "path": relative.as_posix(),
                            "byte_identical": True,
                            "canonical_byte_identical": True,
                            "canonical_sha256": digest,
                            "first_sha256": digest,
                            "second_sha256": digest,
                            "size": canonical_path.stat().st_size,
                        }
                    )
                determinism_rows.append(
                    {
                        "slug": slug,
                        "byte_identical": True,
                        "canonical_byte_identical": True,
                        "files": files,
                    }
                )
            self.write_json(
                corpus / "DETERMINISM.json",
                {
                    "schema_version": 1,
                    "git_version": "git version test",
                    "miner_protocol_revision": analyze.MINER_PROTOCOL_REVISION,
                    "miner_source_sha256": analyze.MINER_SOURCE_SHA256,
                    "all_byte_identical": True,
                    "repositories": determinism_rows,
                    "known_case": {
                        "byte_identical": True,
                        "exit_codes": [1, 1],
                        "interpretation": "conflict",
                    },
                    "known_clean_case": {
                        "byte_identical": True,
                        "exit_codes": [0, 0],
                    },
                },
            )

            arguments = [
                "--repositories",
                str(manifest),
                "--corpus",
                str(corpus),
                "--output",
                str(output),
                "--replicates",
                "200",
            ]
            analyze.main(arguments)
            first_metrics = (output / "metrics.json").read_bytes()
            first_report = (output / "MINING.md").read_bytes()
            analyze.main(arguments)
            self.assertEqual(first_metrics, (output / "metrics.json").read_bytes())
            self.assertEqual(first_report, (output / "MINING.md").read_bytes())

            metrics = json.loads(first_metrics)
            self.assertEqual(metrics["overall"]["population"]["conflict_rate"], analyze.rate(2, 4))
            self.assertEqual(metrics["candidates"]["candidate_rate"], analyze.rate(1, 2))
            python_breakdown = next(
                row
                for row in metrics["breakdowns"]["language"]
                if row["label"] == "Python"
            )
            self.assertEqual(
                set(python_breakdown["granularity_by_stratum"]),
                {"all", "handwritten", "generated", "artifacts"},
            )
            zero = next(
                row for row in metrics["repository_population"] if row["slug"] == "c__squash"
            )
            self.assertEqual(zero["conflict_rate"], analyze.rate(0, 0))
            report = first_report.decode("utf-8")
            self.assertIn("0 / 0 (undefined)", report)
            self.assertIn("Determinism check: **PASS**", report)
            self.assertIn('argv=["git","-c","core.attributesFile=', report)
            self.assertIn('advice.submoduleMergeConflict=false', report)


if __name__ == "__main__":
    unittest.main()
