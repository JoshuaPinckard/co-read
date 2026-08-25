#!/usr/bin/env python3
"""Recompute completed conflict-row overlap metadata under the current bounded rule.

The migration is deterministic and idempotent. It reads only task-owned bare
mirrors, refuses active ``.partial`` ledgers, and does not replay or change the
raw merge result.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from instruments.conflicts.miner import (
    DEFAULT_MANIFEST,
    DEFAULT_MIRROR_ROOT,
    DEFAULT_OUTPUT_ROOT,
    OVERLAP_REVISION,
    OVERLAP_RULE,
    BatchCatFile,
    GitRepository,
    MiningError,
    atomic_json,
    canonical_json,
    load_manifest,
    overlap_for_conflicts,
    sha256_file,
)


DEFAULT_REPORT = PROJECT_ROOT / "exploratory" / "conflicts" / "OVERLAP-MIGRATION.json"


def row_stages(row: Mapping[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    raw_conflicts = row.get("conflicts")
    if not isinstance(raw_conflicts, list) or not raw_conflicts:
        raise MiningError("conflicted merge row lacks a nonempty conflicts list")
    paths: list[str] = []
    stages: list[dict[str, Any]] = []
    for raw in raw_conflicts:
        if not isinstance(raw, Mapping):
            raise MiningError("conflicts list contains a non-object value")
        path = raw.get("path")
        if not isinstance(path, str) or not path:
            raise MiningError("conflict object lacks a nonempty path")
        paths.append(path)
        entries = raw.get("stage_entries")
        if not isinstance(entries, list):
            raise MiningError(f"conflict {path!r} lacks stage_entries")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise MiningError(f"conflict {path!r} has a non-object stage entry")
            stages.append(dict(entry))
    return sorted(paths), stages


def migrate_repository(
    repository_spec: Mapping[str, Any],
    mirror_root: Path,
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
    repository = GitRepository(mirror_root / slug)
    if repository.text(["rev-parse", "--is-bare-repository"]) != "true":
        raise MiningError(f"overlap source is not a bare mirror: {slug}")

    before_hash = sha256_file(conflict_path)
    rows: list[dict[str, Any]] = []
    changed_rows = 0
    before_classes: collections.Counter[str] = collections.Counter()
    after_classes: collections.Counter[str] = collections.Counter()
    path_statuses: collections.Counter[str] = collections.Counter()
    with BatchCatFile(repository) as cat_file, conflict_path.open(
        "r", encoding="utf-8"
    ) as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as error:
                raise MiningError(
                    f"invalid conflict JSON {conflict_path}:{line_number}: {error}"
                ) from error
            if not isinstance(raw, Mapping):
                raise MiningError(f"non-object conflict row {conflict_path}:{line_number}")
            row = dict(raw)
            paths, stages = row_stages(row)
            old_overlap = row.get("overlap")
            if isinstance(old_overlap, Mapping):
                before_classes[str(old_overlap.get("classification", "missing"))] += 1
            else:
                before_classes["missing"] += 1
            overlap = overlap_for_conflicts(repository, cat_file, paths, stages)
            if old_overlap != overlap:
                changed_rows += 1
            row["overlap"] = overlap
            after_classes[overlap["classification"]] += 1
            for path_record in overlap["paths"]:
                path_statuses[str(path_record.get("status", "missing"))] += 1
            rows.append(row)

    temporary = conflict_path.with_name(conflict_path.name + ".overlap.partial")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")
    os.replace(temporary, conflict_path)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise MiningError(f"summary is not an object: {summary_path}")
    summary["overlap_revision"] = OVERLAP_REVISION
    summary["overlap_rule"] = OVERLAP_RULE
    output_hashes = summary.get("output_sha256")
    if not isinstance(output_hashes, dict):
        output_hashes = {}
        summary["output_sha256"] = output_hashes
    output_hashes["all_merges"] = sha256_file(all_path)
    output_hashes["conflicts"] = sha256_file(conflict_path)
    atomic_json(summary_path, summary)
    return {
        "after_class_counts": dict(sorted(after_classes.items())),
        "after_sha256": sha256_file(conflict_path),
        "before_class_counts": dict(sorted(before_classes.items())),
        "before_sha256": before_hash,
        "changed_conflicted_merge_rows": changed_rows,
        "conflicted_merge_rows": len(rows),
        "path_status_counts": dict(sorted(path_statuses.items())),
        "repo": str(repository_spec["repo"]),
        "slug": slug,
    }


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--mirror-root", type=Path, default=DEFAULT_MIRROR_ROOT)
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--repo", action="append", dest="repos")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(arguments)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


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
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                migrate_repository,
                repository,
                args.mirror_root.resolve(),
                args.corpus_root.resolve(),
            )
            for repository in repositories
        ]
        results = [future.result() for future in futures]
    results.sort(key=lambda row: row["slug"].encode("utf-8"))
    path_statuses: collections.Counter[str] = collections.Counter()
    for result in results:
        path_statuses.update(result["path_status_counts"])
    report = {
        "overlap_revision": OVERLAP_REVISION,
        "overlap_rule": OVERLAP_RULE,
        "path_status_counts": dict(sorted(path_statuses.items())),
        "repositories": results,
        "repository_count": len(results),
        "schema_version": 1,
        "total_changed_conflicted_merge_rows": sum(
            row["changed_conflicted_merge_rows"] for row in results
        ),
    }
    atomic_json(args.report, report)
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
