#!/usr/bin/env python3
"""Re-analyze the frozen conflict corpus without invoking Git or mining repositories.

The script is deliberately read-only.  It prints a JSON audit record derived from
the canonical conflict JSONL, all-merges JSONL, and the three arms site manifests.
"""

from __future__ import annotations

import argparse
import collections
import json
import statistics
from pathlib import Path
from typing import Any, Iterable


ARTIFACT_KINDS = {"generated", "lockfile", "vendored"}
STRICT_DECIDABLE = {"overlap", "same_file_disjoint", "boundary_only"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            row["_source_file"] = path.name
            row["_source_line"] = line_number
            rows.append(row)
    return rows


def read_corpus(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conflict_rows: list[dict[str, Any]] = []
    all_merge_rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl"), key=lambda item: item.name.encode()):
        conflict_rows.extend(read_jsonl(path))
    for path in sorted(
        (directory / "_all_merges").glob("*.jsonl"),
        key=lambda item: item.name.encode(),
    ):
        all_merge_rows.extend(read_jsonl(path))
    return conflict_rows, all_merge_rows


def identity(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["repo"]), str(row["merge"])


def percent(numerator: int, denominator: int) -> float | None:
    return numerator * 100.0 / denominator if denominator else None


def numeric_summary(values: Iterable[int]) -> dict[str, float | int | None]:
    materialized = list(values)
    if not materialized:
        return {
            "n": 0,
            "sum": 0,
            "min": None,
            "median": None,
            "mean": None,
            "max": None,
        }
    return {
        "n": len(materialized),
        "sum": sum(materialized),
        "min": min(materialized),
        "median": statistics.median(materialized),
        "mean": statistics.fmean(materialized),
        "max": max(materialized),
    }


def sorted_counter(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(collections.Counter(values).items()))


def site_population(arms_directory: Path) -> list[dict[str, Any]]:
    python_manifest = json.loads(
        (arms_directory / "sites.json").read_text(encoding="utf-8")
    )
    go_manifest = json.loads(
        (arms_directory / "sites-go.json").read_text(encoding="utf-8")
    )
    java_manifest = json.loads(
        (arms_directory / "sites-java.json").read_text(encoding="utf-8")
    )

    selected: list[dict[str, Any]] = []
    for row in python_manifest["sites"]:
        if row.get("validated") is True and row.get("verdict") == "VALIDATED":
            selected.append(
                {
                    "manifest": "sites.json",
                    "language_gate": "Python validated",
                    "repo": row["repo"],
                    "merge": row["merge"],
                    "contradictory_status": row.get("joint_source_check", {}).get(
                        "status"
                    ),
                }
            )
    for row in go_manifest["sites"]:
        if row.get("runner_eligible") is True and row.get("verdict") == "ELIGIBLE":
            selected.append(
                {
                    "manifest": "sites-go.json",
                    "language_gate": "Go runner-eligible",
                    "repo": row["repo"],
                    "merge": row["merge"],
                    "contradictory_status": None,
                }
            )
    for row in java_manifest:
        if row.get("verdict") == "passed":
            selected.append(
                {
                    "manifest": "sites-java.json",
                    "language_gate": "Java gate passed",
                    "repo": row["repo"],
                    "merge": row["merge"],
                    "contradictory_status": None,
                }
            )

    selected.sort(
        key=lambda row: (
            row["manifest"].encode(),
            row["repo"].encode(),
            row["merge"].encode(),
        )
    )
    identities = [(row["repo"], row["merge"]) for row in selected]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate eligible site identity across site manifests")
    if len(selected) != 19:
        raise ValueError(f"expected 19 eligible sites, found {len(selected)}")
    return selected


def analyze_sites(
    conflict_by_identity: dict[tuple[str, str], dict[str, Any]],
    arms_directory: Path,
) -> dict[str, Any]:
    sites = site_population(arms_directory)
    for site in sites:
        key = (site["repo"], site["merge"])
        if key not in conflict_by_identity:
            raise ValueError(f"site is absent from conflict rows: {key}")
        site["overlap_classification"] = conflict_by_identity[key]["overlap"][
            "classification"
        ]
    sites_report = (arms_directory / "SITES.md").read_text(encoding="utf-8")
    unsatisfiable_sites = [
        site
        for site in sites
        if site["contradictory_status"] == "MUTUALLY_UNSATISFIABLE"
    ]
    for site in unsatisfiable_sites:
        if site["merge"] not in sites_report or "MUTUALLY_UNSATISFIABLE" not in sites_report:
            raise ValueError(
                f"structured contradictory result is not reconciled in SITES.md: {site}"
            )
    site_classifications = collections.Counter(
        site["overlap_classification"] for site in sites
    )
    expected_site_classes = {
        "overlap",
        "same_file_disjoint",
        "boundary_only",
        "unclassifiable",
    }
    unexpected_site_classes = set(site_classifications) - expected_site_classes
    if unexpected_site_classes:
        raise ValueError(f"unexpected site overlap classes: {unexpected_site_classes}")
    return {
        "count": len(sites),
        "manifest_counts": sorted_counter(site["manifest"] for site in sites),
        "classification_counts": {
            classification: site_classifications[classification]
            for classification in sorted(expected_site_classes)
        },
        "contradictory_status_counts": sorted_counter(
            str(site["contradictory_status"] or "NOT_ASSESSED") for site in sites
        ),
        "constructible_validated_python_sites": sum(
            site["contradictory_status"]
            in {"MUTUALLY_UNSATISFIABLE", "JOINTLY_SATISFIABLE"}
            for site in sites
        ),
        "mutually_unsatisfiable_count": len(unsatisfiable_sites),
        "mutually_unsatisfiable_sites": [
            {"repo": site["repo"], "merge": site["merge"]}
            for site in sites
            if site["contradictory_status"] == "MUTUALLY_UNSATISFIABLE"
        ],
        "sites": sites,
    }


def conflict_occurrences(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [conflict for row in rows for conflict in row.get("conflicts", [])]


def overlap_paths(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [path for row in rows for path in row.get("overlap", {}).get("paths", [])]


def group_observables(rows: list[dict[str, Any]]) -> dict[str, Any]:
    occurrences = conflict_occurrences(rows)
    artifacts = [
        item
        for item in occurrences
        if item.get("classification", {}).get("kind") in ARTIFACT_KINDS
    ]
    handwritten = [
        item
        for item in occurrences
        if item.get("classification", {}).get("kind") == "handwritten"
    ]
    range_status_merge_counts: collections.Counter[str] = collections.Counter()
    conflict_file_count_bins: collections.Counter[str] = collections.Counter()
    merge_origin_mix_counts: collections.Counter[str] = collections.Counter()
    for row in rows:
        for status in {
            str(item.get("range_status", "missing"))
            for item in row.get("conflicts", [])
        }:
            range_status_merge_counts[status] += 1
        conflict_files = len(row.get("conflicted_paths", []))
        if conflict_files == 1:
            conflict_file_count_bins["1"] += 1
        elif conflict_files == 2:
            conflict_file_count_bins["2"] += 1
        elif conflict_files <= 5:
            conflict_file_count_bins["3-5"] += 1
        elif conflict_files <= 10:
            conflict_file_count_bins["6-10"] += 1
        elif conflict_files <= 50:
            conflict_file_count_bins["11-50"] += 1
        elif conflict_files <= 100:
            conflict_file_count_bins["51-100"] += 1
        elif conflict_files <= 500:
            conflict_file_count_bins["101-500"] += 1
        else:
            conflict_file_count_bins["501+"] += 1
        has_artifact = any(
            item.get("classification", {}).get("kind") in ARTIFACT_KINDS
            for item in row.get("conflicts", [])
        )
        has_handwritten = any(
            item.get("classification", {}).get("kind") == "handwritten"
            for item in row.get("conflicts", [])
        )
        if has_artifact and has_handwritten:
            merge_origin_mix_counts["mixed"] += 1
        elif has_artifact:
            merge_origin_mix_counts["artifact_only"] += 1
        elif has_handwritten:
            merge_origin_mix_counts["handwritten_only"] += 1
        else:
            merge_origin_mix_counts["neither"] += 1
    return {
        "merges": len(rows),
        "conflict_file_counts": numeric_summary(
            len(row.get("conflicted_paths", [])) for row in rows
        ),
        "conflict_file_count_bins": dict(sorted(conflict_file_count_bins.items())),
        "parent1_changed_file_counts": numeric_summary(
            int(row["diffs"]["parent1"]["files"]) for row in rows
        ),
        "parent2_changed_file_counts": numeric_summary(
            int(row["diffs"]["parent2"]["files"]) for row in rows
        ),
        "combined_side_file_counts": numeric_summary(
            int(row["diffs"]["parent1"]["files"])
            + int(row["diffs"]["parent2"]["files"])
            for row in rows
        ),
        "smaller_side_file_counts": numeric_summary(
            min(
                int(row["diffs"]["parent1"]["files"]),
                int(row["diffs"]["parent2"]["files"]),
            )
            for row in rows
        ),
        "larger_side_file_counts": numeric_summary(
            max(
                int(row["diffs"]["parent1"]["files"]),
                int(row["diffs"]["parent2"]["files"]),
            )
            for row in rows
        ),
        "conflict_file_occurrences": len(occurrences),
        "range_status_counts": sorted_counter(
            str(item.get("range_status", "missing")) for item in occurrences
        ),
        "merges_with_range_status": dict(sorted(range_status_merge_counts.items())),
        "artifact_occurrences": len(artifacts),
        "handwritten_occurrences": len(handwritten),
        "origin_kind_counts": sorted_counter(
            str(item.get("classification", {}).get("kind", "missing"))
            for item in occurrences
        ),
        "merge_origin_mix_counts": dict(sorted(merge_origin_mix_counts.items())),
        "other_origin_occurrences": len(occurrences) - len(artifacts) - len(handwritten),
        "merges_with_artifact": sum(
            any(
                item.get("classification", {}).get("kind") in ARTIFACT_KINDS
                for item in row.get("conflicts", [])
            )
            for row in rows
        ),
        "merges_with_handwritten": sum(
            any(
                item.get("classification", {}).get("kind") == "handwritten"
                for item in row.get("conflicts", [])
            )
            for row in rows
        ),
        "overlap_path_status_counts": sorted_counter(
            str(item.get("status", "missing")) for item in overlap_paths(rows)
        ),
    }


def unknown_path_details(rows: list[dict[str, Any]]) -> dict[str, Any]:
    detail_counter: collections.Counter[str] = collections.Counter()
    status_range_counter: collections.Counter[str] = collections.Counter()
    merge_reason_sets: collections.Counter[str] = collections.Counter()
    missing_stage_patterns: collections.Counter[str] = collections.Counter()
    unknown_count = 0

    for row in rows:
        conflict_by_path = {
            str(conflict["path"]): conflict for conflict in row.get("conflicts", [])
        }
        merge_reasons: set[str] = set()
        for path_record in row.get("overlap", {}).get("paths", []):
            status = str(path_record.get("status", "missing"))
            if status == "classifiable":
                continue
            unknown_count += 1
            merge_reasons.add(status)
            conflict = conflict_by_path.get(str(path_record["path"]))
            range_status = (
                str(conflict.get("range_status", "missing"))
                if conflict is not None
                else "conflict_record_missing"
            )
            status_range_counter[f"{status} | {range_status}"] += 1
            detail_counter[status] += 1

            if status == "unclassifiable_missing_or_nonblob_stage" and conflict:
                entries = {int(item["stage"]): item for item in conflict["stage_entries"]}
                missing = [str(stage) for stage in (1, 2, 3) if stage not in entries]
                nonblob = [
                    f"{stage}:{entries[stage].get('mode')}"
                    for stage in sorted(entries)
                    if str(entries[stage].get("mode")) == "160000"
                ]
                pieces: list[str] = []
                if missing:
                    pieces.append("missing-stage-" + "+".join(missing))
                if nonblob:
                    pieces.append("gitlink-" + "+".join(nonblob))
                if not pieces:
                    pieces.append("all-stages-listed-no-row-level-subtype")
                missing_stage_patterns["; ".join(pieces)] += 1
        merge_reason_sets[" + ".join(sorted(merge_reasons)) or "none"] += 1

    return {
        "unknown_path_count": unknown_count,
        "path_reason_counts": dict(sorted(detail_counter.items())),
        "path_reason_by_range_status": dict(sorted(status_range_counter.items())),
        "merge_reason_set_counts": dict(sorted(merge_reason_sets.items())),
        "missing_or_nonblob_stage_patterns": dict(
            sorted(missing_stage_patterns.items())
        ),
    }


def analyze_censoring(conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    decidable = [
        row
        for row in conflicts
        if row.get("overlap", {}).get("classification") in STRICT_DECIDABLE
    ]
    censored = [row for row in conflicts if row not in decidable]
    intersecting = sum(
        row.get("overlap", {}).get("classification") == "overlap"
        for row in decidable
    )
    total = len(conflicts)
    classification_counts = sorted_counter(
        str(row.get("overlap", {}).get("classification", "missing"))
        for row in conflicts
    )
    repo_censored = collections.Counter(str(row["repo"]) for row in censored)
    repo_decidable = collections.Counter(str(row["repo"]) for row in decidable)
    repos = sorted(set(repo_censored) | set(repo_decidable))
    return {
        "total_conflicted": total,
        "classification_counts": classification_counts,
        "decidable": len(decidable),
        "censored": len(censored),
        "intersecting_decidable": intersecting,
        "decidable_coverage_percent": percent(len(decidable), total),
        "reported_intersection_percent": percent(intersecting, len(decidable)),
        "lower_bound": {
            "intersecting": intersecting,
            "denominator": total,
            "percent": percent(intersecting, total),
        },
        "upper_bound": {
            "intersecting": intersecting + len(censored),
            "denominator": total,
            "percent": percent(intersecting + len(censored), total),
        },
        "repos": [
            {
                "repo": repo,
                "censored": repo_censored[repo],
                "decidable": repo_decidable[repo],
            }
            for repo in repos
        ],
        "censored_observables": group_observables(censored),
        "decidable_observables": group_observables(decidable),
        "censored_unknown_path_details": unknown_path_details(censored),
        "censored_merges": [
            {
                "repo": row["repo"],
                "merge": row["merge"],
                "classification": row["overlap"]["classification"],
                "conflict_files": len(row["conflicted_paths"]),
                "parent1_files": row["diffs"]["parent1"]["files"],
                "parent2_files": row["diffs"]["parent2"]["files"],
                "overlap_path_status_counts": sorted_counter(
                    str(item.get("status", "missing"))
                    for item in row["overlap"]["paths"]
                ),
            }
            for row in censored
        ],
    }


def diff_has_full_path_set(row: dict[str, Any], side: str) -> bool:
    diff = row.get("diffs", {}).get(side, {})
    candidate_keys = {
        "paths",
        "changed_paths",
        "files_changed",
        "file_paths",
        "all_paths",
    }
    return any(key in diff and isinstance(diff[key], list) for key in candidate_keys)


def analyze_conditional_rates(
    all_merges: list[dict[str, Any]], repository_names: Iterable[str] | None = None
) -> dict[str, Any]:
    evaluable = [
        row for row in all_merges if row.get("evaluation_status") in {"clean", "conflicted"}
    ]
    def both_sides_nonempty(row: dict[str, Any]) -> bool:
        return (
            int(row["diffs"]["parent1"]["files"]) > 0
            and int(row["diffs"]["parent2"]["files"]) > 0
        )

    conditioned = [row for row in evaluable if both_sides_nonempty(row)]
    repos = sorted(
        set(repository_names or ()) | {str(row["repo"]) for row in all_merges}
    )
    per_repo: list[dict[str, Any]] = []
    for repo in repos:
        rows = [row for row in conditioned if row["repo"] == repo]
        conflicts = sum(bool(row["conflicted"]) for row in rows)
        per_repo.append(
            {
                "repo": repo,
                "conflicted": conflicts,
                "denominator": len(rows),
                "percent": percent(conflicts, len(rows)),
            }
        )
    numerator = sum(bool(row["conflicted"]) for row in conditioned)
    excluded = [row for row in evaluable if not both_sides_nonempty(row)]
    empty_side_counts = collections.Counter()
    for row in excluded:
        parent1_empty = int(row["diffs"]["parent1"]["files"]) == 0
        parent2_empty = int(row["diffs"]["parent2"]["files"]) == 0
        if parent1_empty and parent2_empty:
            empty_side_counts["both_empty"] += 1
        elif parent1_empty:
            empty_side_counts["parent1_only_empty"] += 1
        elif parent2_empty:
            empty_side_counts["parent2_only_empty"] += 1
    one_commit = [
        row for row in evaluable if row.get("divergence", {}).get("combined_commits") == 1
    ]
    full_paths_available = all(
        diff_has_full_path_set(row, side)
        for row in evaluable
        for side in ("parent1", "parent2")
    )
    diff_key_shapes = sorted(
        {
            tuple(sorted(row.get("diffs", {}).get(side, {}).keys()))
            for row in evaluable
            for side in ("parent1", "parent2")
        }
    )
    return {
        "all_two_parent_rows": len(all_merges),
        "evaluable": len(evaluable),
        "baseline_conflicted": sum(bool(row["conflicted"]) for row in evaluable),
        "both_sides_nonempty": {
            "conflicted": numerator,
            "denominator": len(conditioned),
            "percent": percent(numerator, len(conditioned)),
            "share_of_evaluable_percent": percent(len(conditioned), len(evaluable)),
            "excluded_evaluable": len(excluded),
            "excluded_empty_side_counts": dict(sorted(empty_side_counts.items())),
            "per_repo": per_repo,
        },
        "combined_divergence_one_commit": {
            "conflicted": sum(bool(row["conflicted"]) for row in one_commit),
            "denominator": len(one_commit),
            "percent": percent(sum(bool(row["conflicted"]) for row in one_commit), len(one_commit)),
        },
        "common_file_condition": {
            "computable_from_rows": full_paths_available,
            "observed_diff_key_shapes": [list(shape) for shape in diff_key_shapes],
            "reason": (
                "Each side records only aggregate file counts plus partial binary/test path "
                "lists; clean rows do not record the complete base-to-parent changed-path sets."
            ),
        },
    }


def validate_corpus(
    conflicts: list[dict[str, Any]], all_merges: list[dict[str, Any]]
) -> dict[str, Any]:
    conflict_ids = [identity(row) for row in conflicts]
    all_ids = [identity(row) for row in all_merges]
    if len(conflict_ids) != len(set(conflict_ids)):
        raise ValueError("duplicate conflict identity")
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("duplicate all-merges identity")
    all_by_id = {identity(row): row for row in all_merges}
    missing = [key for key in conflict_ids if key not in all_by_id]
    mismatched = [
        key
        for key in conflict_ids
        if key in all_by_id and not bool(all_by_id[key].get("conflicted"))
    ]
    if missing or mismatched:
        raise ValueError(
            f"conflict/all-merges reconciliation failed: missing={missing}, mismatched={mismatched}"
        )
    return {
        "conflict_rows": len(conflicts),
        "all_merge_rows": len(all_merges),
        "identity_reconciled": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Blast-Radius checkout root",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="emit only the headline reconciliation fields",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    definitions_text = (root / "exploratory" / "conflicts" / "MINING.md").read_text(
        encoding="utf-8"
    )
    required_definition_fragments = [
        "Strict base-coordinate overlap occurred in 304 / 372",
        "unclassifiable_refinement_limit",
        "handwritten is the operational default",
    ]
    missing_definitions = [
        fragment for fragment in required_definition_fragments if fragment not in definitions_text
    ]
    if missing_definitions:
        raise ValueError(f"MINING.md definition checks failed: {missing_definitions}")
    corpus_directory = root / "corpus" / "conflicts"
    conflicts, all_merges = read_corpus(corpus_directory)
    repository_names = [
        path.stem.replace("__", "/", 1)
        for path in (corpus_directory / "_all_merges").glob("*.jsonl")
    ]
    validation = validate_corpus(conflicts, all_merges)
    conflict_by_identity = {identity(row): row for row in conflicts}
    result = {
        "schema_version": 1,
        "kind": "conflict_corpus_reanalysis",
        "inputs": {
            "conflicts": "corpus/conflicts/*.jsonl",
            "all_merges": "corpus/conflicts/_all_merges/*.jsonl",
            "definitions": "exploratory/conflicts/MINING.md",
            "sites": [
                "exploratory/arms/sites.json",
                "exploratory/arms/sites-go.json",
                "exploratory/arms/sites-java.json",
                "exploratory/arms/SITES.md",
            ],
        },
        "validation": validation,
        "definition_checks": {
            "mining_fragments_reconciled": True,
            "sites_report_reconciled": True,
        },
        "site_stratification": analyze_sites(
            conflict_by_identity, root / "exploratory" / "arms"
        ),
        "censoring": analyze_censoring(conflicts),
        "conditional_rates": analyze_conditional_rates(all_merges, repository_names),
    }
    output = result
    if args.summary:
        output = {
            "validation": result["validation"],
            "definition_checks": result["definition_checks"],
            "site_stratification": {
                key: result["site_stratification"][key]
                for key in (
                    "count",
                    "manifest_counts",
                    "classification_counts",
                    "contradictory_status_counts",
                    "constructible_validated_python_sites",
                    "mutually_unsatisfiable_count",
                )
            },
            "censoring": {
                key: result["censoring"][key]
                for key in (
                    "total_conflicted",
                    "classification_counts",
                    "decidable",
                    "censored",
                    "intersecting_decidable",
                    "lower_bound",
                    "upper_bound",
                )
            },
            "conditional_rates": result["conditional_rates"],
        }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
