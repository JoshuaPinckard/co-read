#!/usr/bin/env python3
"""Apply the current deterministic file classifier to completed conflict rows.

This is an idempotent migration for rows mined by an earlier classifier revision.
It never replays a merge and refuses to touch an active ``.partial`` ledger.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from instruments.conflicts.miner import (
    CLASSIFICATION_REVISION,
    CLASSIFICATION_RULE,
    DEFAULT_MANIFEST,
    DEFAULT_OUTPUT_ROOT,
    MERGE_TREE_INTERPRETATION,
    MERGE_TREE_INVOCATION,
    MiningError,
    STORAGE_POLICY,
    TEST_PATH_REVISION,
    TEST_PATH_RULE,
    atomic_json,
    canonical_json,
    classify_path,
    load_manifest,
    sha256_file,
)


DEFAULT_REPORT = PROJECT_ROOT / "exploratory" / "conflicts" / "RECLASSIFICATION.json"


def revised_classification(conflict: Mapping[str, Any]) -> dict[str, Any]:
    path = conflict.get("path")
    if not isinstance(path, str) or not path:
        raise MiningError("conflict row lacks a nonempty path")
    path_only = classify_path(path, [])
    existing = conflict.get("classification")
    if (
        path_only["kind"] == "handwritten"
        and isinstance(existing, Mapping)
        and existing.get("kind") == "generated"
        and existing.get("rule") == "generated-header-first-8192-bytes"
    ):
        # Header evidence came from historical stage bytes at mining time and is
        # strictly stronger than the path-only migration can reconstruct.
        return dict(existing)
    return path_only


def migrate_row(row: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    migrated = dict(row)
    raw_conflicts = row.get("conflicts")
    if not isinstance(raw_conflicts, list) or not raw_conflicts:
        raise MiningError("conflicted merge row lacks a nonempty conflicts list")
    changed = 0
    conflicts: list[dict[str, Any]] = []
    for raw in raw_conflicts:
        if not isinstance(raw, Mapping):
            raise MiningError("conflicts list contains a non-object value")
        conflict = dict(raw)
        revised = revised_classification(conflict)
        if conflict.get("classification") != revised:
            changed += 1
        conflict["classification"] = revised
        conflicts.append(conflict)
    migrated["conflicts"] = conflicts
    return migrated, changed


def migrate_repository(
    repository_spec: Mapping[str, Any],
    corpus_root: Path,
) -> dict[str, Any]:
    slug = str(repository_spec["slug"])
    conflict_path = corpus_root / f"{slug}.jsonl"
    all_path = corpus_root / "_all_merges" / f"{slug}.jsonl"
    summary_path = corpus_root / "_summaries" / f"{slug}.json"
    for path in (conflict_path, all_path):
        partial = path.with_name(path.name + ".partial")
        if partial.exists():
            raise MiningError(f"refusing to migrate active ledger {partial}")
    for path in (conflict_path, all_path, summary_path):
        if not path.is_file():
            raise MiningError(f"completed corpus artifact is absent: {path}")

    before_hash = sha256_file(conflict_path)
    rows = []
    changed = 0
    before_kinds: collections.Counter[str] = collections.Counter()
    after_kinds: collections.Counter[str] = collections.Counter()
    with conflict_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise MiningError(
                    f"invalid conflict JSON {conflict_path}:{line_number}: {error}"
                ) from error
            if not isinstance(raw, Mapping):
                raise MiningError(f"non-object conflict row {conflict_path}:{line_number}")
            for item in raw.get("conflicts", []):
                if isinstance(item, Mapping) and isinstance(item.get("classification"), Mapping):
                    before_kinds[str(item["classification"].get("kind", "unknown"))] += 1
            migrated, row_changed = migrate_row(raw)
            changed += row_changed
            for item in migrated["conflicts"]:
                after_kinds[str(item["classification"]["kind"])] += 1
            rows.append(migrated)

    temporary = conflict_path.with_name(conflict_path.name + ".reclassify.partial")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")
    os.replace(temporary, conflict_path)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise MiningError(f"summary is not an object: {summary_path}")
    summary["classification_revision"] = CLASSIFICATION_REVISION
    summary["classification_rule"] = CLASSIFICATION_RULE
    summary["merge_tree_interpretation"] = MERGE_TREE_INTERPRETATION
    summary["merge_tree_invocation"] = list(MERGE_TREE_INVOCATION)
    summary["storage_policy"] = STORAGE_POLICY
    summary["test_path_revision"] = TEST_PATH_REVISION
    summary["test_path_rule"] = TEST_PATH_RULE
    no_merge_base_merges = 0
    with all_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                all_row = json.loads(line)
            except json.JSONDecodeError as error:
                raise MiningError(
                    f"invalid all-merges JSON {all_path}:{line_number}: {error}"
                ) from error
            if isinstance(all_row, Mapping) and all_row.get("evaluation_status") == "no_merge_base":
                no_merge_base_merges += 1
    summary["no_merge_base_merges"] = no_merge_base_merges
    output_hashes = summary.get("output_sha256")
    if not isinstance(output_hashes, dict):
        output_hashes = {}
        summary["output_sha256"] = output_hashes
    output_hashes["all_merges"] = sha256_file(all_path)
    output_hashes["conflicts"] = sha256_file(conflict_path)
    atomic_json(summary_path, summary)
    return {
        "after_kind_counts": dict(sorted(after_kinds.items())),
        "after_sha256": sha256_file(conflict_path),
        "before_kind_counts": dict(sorted(before_kinds.items())),
        "before_sha256": before_hash,
        "changed_conflict_file_occurrences": changed,
        "conflicted_merge_rows": len(rows),
        "repo": str(repository_spec["repo"]),
        "slug": slug,
    }


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--prior-report",
        action="append",
        type=Path,
        default=[],
        help="earlier migration report whose nonzero events should remain in the audit",
    )
    parser.add_argument("--repo", action="append", dest="repos")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    repositories = load_manifest(args.manifest)
    if args.repos:
        requested = set(args.repos)
        available = {str(repository["slug"]) for repository in repositories}
        unknown = sorted(requested - available)
        if unknown:
            raise MiningError(f"unknown repository slug(s): {', '.join(unknown)}")
        repositories = [
            repository for repository in repositories if repository["slug"] in requested
        ]
    results = [
        migrate_repository(repository, args.corpus_root.resolve())
        for repository in repositories
    ]
    event_candidates: list[Mapping[str, Any]] = []
    for prior_path in args.prior_report:
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        if not isinstance(prior, Mapping):
            raise MiningError(f"prior report is not an object: {prior_path}")
        raw_events = prior.get("migration_events")
        if not isinstance(raw_events, list):
            raw_events = prior.get("repositories")
        if isinstance(raw_events, list):
            event_candidates.extend(
                row for row in raw_events if isinstance(row, Mapping)
            )
    event_candidates.extend(results)
    events_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in event_candidates:
        changed = raw.get("changed_conflict_file_occurrences")
        if not isinstance(changed, int) or isinstance(changed, bool) or changed <= 0:
            continue
        identity = (
            str(raw.get("slug", "unknown")),
            str(raw.get("before_sha256", "unknown")),
            str(raw.get("after_sha256", "unknown")),
        )
        events_by_identity[identity] = dict(raw)
    migration_events = [
        events_by_identity[key]
        for key in sorted(events_by_identity, key=lambda item: tuple(x.encode("utf-8") for x in item))
    ]
    report = {
        "classification_revision": CLASSIFICATION_REVISION,
        "classification_rule": CLASSIFICATION_RULE,
        "current_run_changed_conflict_file_occurrences": sum(
            row["changed_conflict_file_occurrences"] for row in results
        ),
        "migration_events": migration_events,
        "repositories": results,
        "repository_count": len(results),
        "schema_version": 1,
        "total_changed_conflict_file_occurrences": sum(
            row["changed_conflict_file_occurrences"] for row in migration_events
        ),
    }
    atomic_json(args.report, report)
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
