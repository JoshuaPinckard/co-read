#!/usr/bin/env python3
"""Run full byte-for-byte miner repetitions and record the known conflict case."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from instruments.conflicts.miner import (
    BatchCatFile,
    DETERMINISTIC_GIT_ENVIRONMENT_OVERRIDES,
    GitRepository,
    GIT_CONFIG_ARGS,
    MERGE_TREE_INTERPRETATION,
    MINER_PROTOCOL_REVISION,
    MINER_SOURCE_SHA256,
    SCRUBBED_GIT_ENVIRONMENT_KEYS,
    SCRUBBED_GIT_ENVIRONMENT_PREFIXES,
    canonical_json,
    conflict_details,
    load_manifest,
    overlap_for_conflicts,
    parse_merge_tree_output,
    sha256_file,
)


DEFAULT_MANIFEST = Path(__file__).with_name("repositories.json")
DEFAULT_MIRROR_ROOT = PROJECT_ROOT / "corpus" / "_conflict_mirrors"
DEFAULT_CORPUS_ROOT = PROJECT_ROOT / "corpus" / "conflicts"
DEFAULT_SCRATCH_ROOT = DEFAULT_MIRROR_ROOT / "_determinism_runs"
DEFAULT_REPORT = DEFAULT_CORPUS_ROOT / "DETERMINISM.json"
DEFAULT_REPOSITORIES = (
    "pallets__itsdangerous",
    "pygments__pygments",
    "apache__commons-lang",
    "hashicorp__terraform-provider-random",
)

KNOWN_CASE = {
    "repo": "pallets/click",
    "slug": "pallets__click",
    "merge": "240603f240a9ff179d834fede836060d897c6980",
    "parents": [
        "679a7a0eccbdded7a6e85680bdaaf08003765e01",
        "df2e5ed8c4e89f51ff4eddb9600d913083613e62",
    ],
    "expected_merge_base": "8929d392781c8113bc569f388c15c47b94f86581",
}

KNOWN_CLEAN_CASE = {
    "repo": "pallets/click",
    "slug": "pallets__click",
    "merge": "3755db7cce5265720381b31e80a51beb7abe94a2",
    "parents": [
        "5b7b7296fabc5d47d4ffd179be52492095e36f30",
        "47f9cb435b047f4bccf2dcee3ce3dd0bb91d8535",
    ],
    "expected_merge_base": "5b7b7296fabc5d47d4ffd179be52492095e36f30",
}


class VerificationError(RuntimeError):
    """The repeatability verification could not establish its claim."""


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(canonical_json(value) + "\n", encoding="utf-8", newline="\n")
    os.replace(partial, path)


def file_size_sum(root: Path, *, exclude: Path | None = None) -> int:
    excluded = exclude.resolve() if exclude is not None else None
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and (excluded is None or path.resolve() != excluded)
    )


def refresh_disk_measurement(
    report_path: Path,
    mirror_root: Path,
    corpus_root: Path,
) -> dict[str, Any]:
    """Refresh only the post-mining logical byte counts in an existing report."""
    if not report_path.is_file():
        raise VerificationError(f"determinism report is absent: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    mirror_bytes = file_size_sum(mirror_root) if mirror_root.exists() else 0
    corpus_bytes = (
        file_size_sum(corpus_root, exclude=report_path) if corpus_root.exists() else 0
    )
    report["mirror_logical_bytes"] = mirror_bytes
    report["corpus_output_logical_bytes"] = corpus_bytes
    report["total_disk_bytes"] = mirror_bytes + corpus_bytes
    atomic_json(report_path, report)
    return report


def line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def miner_files(root: Path, slug: str) -> list[Path]:
    return [
        root / f"{slug}.jsonl",
        root / "_all_merges" / f"{slug}.jsonl",
        root / "_summaries" / f"{slug}.json",
    ]


def run_full_miner(
    manifest: Path,
    mirror_root: Path,
    output_root: Path,
    slug: str,
    merge_workers: int,
) -> None:
    command = [
        sys.executable,
        str(Path(__file__).with_name("miner.py")),
        "--manifest",
        str(manifest),
        "--mirror-root",
        str(mirror_root),
        "--output-root",
        str(output_root),
        "--repo",
        slug,
        "--no-resume",
        "--progress-every",
        "0",
        "--merge-workers",
        str(merge_workers),
    ]
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode != 0:
        raise VerificationError(
            f"full repeat failed for {slug}: "
            f"{completed.stderr.decode('utf-8', errors='replace').strip()}"
        )


def verify_repository(
    manifest: Path,
    mirror_root: Path,
    corpus_root: Path,
    scratch_root: Path,
    slug: str,
    merge_workers: int,
) -> dict[str, Any]:
    run1 = scratch_root / "run1"
    run2 = scratch_root / "run2"
    run_full_miner(manifest, mirror_root, run1, slug, merge_workers)
    run_full_miner(manifest, mirror_root, run2, slug, merge_workers)
    files: list[dict[str, Any]] = []
    identical = True
    canonical_identical = True
    for first in miner_files(run1, slug):
        relative = first.relative_to(run1)
        second = run2 / relative
        canonical = corpus_root / relative
        if not first.is_file() or not second.is_file():
            raise VerificationError(f"repeat output is absent: {relative}")
        if not canonical.is_file():
            raise VerificationError(f"canonical corpus output is absent: {relative}")
        first_hash = sha256_file(first)
        second_hash = sha256_file(second)
        same = first.read_bytes() == second.read_bytes()
        canonical_hash = sha256_file(canonical)
        same_as_canonical = first.read_bytes() == canonical.read_bytes()
        identical = identical and same
        canonical_identical = canonical_identical and same_as_canonical
        files.append(
            {
                "byte_identical": same,
                "canonical_byte_identical": same_as_canonical,
                "canonical_sha256": canonical_hash,
                "first_sha256": first_hash,
                "path": relative.as_posix(),
                "second_sha256": second_hash,
                "size": first.stat().st_size,
            }
        )
    return {
        "all_merge_rows": line_count(run1 / "_all_merges" / f"{slug}.jsonl"),
        "byte_identical": identical,
        "canonical_byte_identical": canonical_identical,
        "conflict_rows": line_count(run1 / f"{slug}.jsonl"),
        "files": files,
        "slug": slug,
    }


def verify_known_case(mirror_root: Path) -> dict[str, Any]:
    repository = GitRepository(mirror_root / KNOWN_CASE["slug"])
    parent1, parent2 = KNOWN_CASE["parents"]
    merge_base = repository.text(["merge-base", parent1, parent2])
    if merge_base != KNOWN_CASE["expected_merge_base"]:
        raise VerificationError(
            f"known case merge base is {merge_base}, expected {KNOWN_CASE['expected_merge_base']}"
        )
    arguments = [
        "merge-tree",
        "--write-tree",
        "-z",
        "--messages",
        "-Xfind-renames=50%",
        parent1,
        parent2,
    ]
    first = repository.run(arguments, check=False)
    second = repository.run(arguments, check=False)
    if first.returncode != 1 or second.returncode != 1:
        raise VerificationError(
            f"known case statuses were {first.returncode} and {second.returncode}, expected 1"
        )
    if first.stderr or second.stderr:
        raise VerificationError("known case unexpectedly wrote stderr")
    result_tree, stages, messages = parse_merge_tree_output(first.stdout)
    paths = sorted(
        {entry["path"] for entry in stages}.union(
            path
            for message in messages
            if message["type"].startswith("CONFLICT")
            for path in message["paths"]
        )
    )
    with BatchCatFile(repository) as cat_file:
        details = conflict_details(
            repository, cat_file, result_tree, paths, stages, messages
        )
        overlap = overlap_for_conflicts(repository, cat_file, paths, stages)
    return {
        "byte_identical": first.stdout == second.stdout and first.stderr == second.stderr,
        "repository_root_invocation": [
            "git",
            *GIT_CONFIG_ARGS,
            "-C",
            "corpus/_conflict_mirrors/pallets__click",
            "merge-tree",
            "--write-tree",
            "-z",
            "--messages",
            "-Xfind-renames=50%",
            parent1,
            parent2,
        ],
        "conflict_paths": paths,
        "conflicts": details,
        "exit_codes": [first.returncode, second.returncode],
        "interpretation": (
            MERGE_TREE_INTERPRETATION
            + "; NUL field 0 is the result tree, stage records use "
            "1=base/2=P1/3=P2, an empty field ends stages, and message records carry "
            "path count, raw paths, stable short type, and free-form text"
        ),
        "merge": KNOWN_CASE["merge"],
        "merge_base": merge_base,
        "output_sha256": [
            hashlib.sha256(first.stdout).hexdigest(),
            hashlib.sha256(second.stdout).hexdigest(),
        ],
        "output_size": len(first.stdout),
        "overlap": overlap,
        "parents": KNOWN_CASE["parents"],
        "repo": KNOWN_CASE["repo"],
        "result_tree": result_tree,
        "stderr_size": len(first.stderr),
    }


def verify_known_clean_case(mirror_root: Path) -> dict[str, Any]:
    repository = GitRepository(mirror_root / KNOWN_CLEAN_CASE["slug"])
    parent1, parent2 = KNOWN_CLEAN_CASE["parents"]
    merge_base = repository.text(["merge-base", parent1, parent2])
    if merge_base != KNOWN_CLEAN_CASE["expected_merge_base"]:
        raise VerificationError("known clean case returned an unexpected merge base")
    arguments = [
        "merge-tree",
        "--write-tree",
        "-z",
        "--messages",
        "-Xfind-renames=50%",
        parent1,
        parent2,
    ]
    first = repository.run(arguments, check=False)
    second = repository.run(arguments, check=False)
    if first.returncode != 0 or second.returncode != 0:
        raise VerificationError(
            f"known clean case statuses were {first.returncode} and {second.returncode}, expected 0"
        )
    if first.stderr or second.stderr:
        raise VerificationError("known clean case unexpectedly wrote stderr")
    result_tree, stages, messages = parse_merge_tree_output(first.stdout)
    if stages or messages:
        raise VerificationError("known clean case emitted unmerged stages or messages")
    return {
        "byte_identical": first.stdout == second.stdout and first.stderr == second.stderr,
        "exit_codes": [first.returncode, second.returncode],
        "merge": KNOWN_CLEAN_CASE["merge"],
        "merge_base": merge_base,
        "output_sha256": [
            hashlib.sha256(first.stdout).hexdigest(),
            hashlib.sha256(second.stdout).hexdigest(),
        ],
        "output_size": len(first.stdout),
        "parents": KNOWN_CLEAN_CASE["parents"],
        "repo": KNOWN_CLEAN_CASE["repo"],
        "result_tree": result_tree,
        "stage_count": len(stages),
        "message_count": len(messages),
        "stderr_size": len(first.stderr),
    }


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--mirror-root", type=Path, default=DEFAULT_MIRROR_ROOT)
    parser.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    parser.add_argument("--scratch-root", type=Path, default=DEFAULT_SCRATCH_ROOT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--repo", action="append", dest="repos")
    parser.add_argument(
        "--merge-workers",
        type=int,
        default=5,
        help="ordered per-repository merge evaluators used in each repeated full run",
    )
    parser.add_argument(
        "--refresh-disk-only",
        action="store_true",
        help=(
            "refresh the logical byte totals in an existing verification report "
            "without repeating the full two-run check"
        ),
    )
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_args(arguments)
    if args.refresh_disk_only:
        report = refresh_disk_measurement(
            args.report.resolve(),
            args.mirror_root.resolve(),
            args.corpus_root.resolve(),
        )
        print(canonical_json(report))
        return 0 if report.get("all_byte_identical") is True else 1
    if args.merge_workers < 1:
        raise VerificationError("--merge-workers must be at least 1")
    repositories = load_manifest(args.manifest)
    available = {repository["slug"] for repository in repositories}
    selected = args.repos or list(DEFAULT_REPOSITORIES)
    unknown = sorted(set(selected) - available)
    if unknown:
        raise VerificationError(f"unknown repository slug(s): {', '.join(unknown)}")
    if len(set(selected)) < 3:
        raise VerificationError("at least three distinct repositories are required")
    rows = [
        verify_repository(
            args.manifest.resolve(),
            args.mirror_root.resolve(),
            args.corpus_root.resolve(),
            args.scratch_root.resolve(),
            slug,
            args.merge_workers,
        )
        for slug in selected
    ]
    mirror_bytes = file_size_sum(args.mirror_root)
    corpus_bytes = (
        file_size_sum(args.corpus_root, exclude=args.report)
        if args.corpus_root.exists()
        else 0
    )
    known_case = verify_known_case(args.mirror_root)
    known_clean_case = verify_known_clean_case(args.mirror_root)
    report = {
        "all_byte_identical": known_case["byte_identical"]
        and known_clean_case["byte_identical"]
        and all(
            row["byte_identical"] and row["canonical_byte_identical"]
            for row in rows
        ),
        "corpus_output_logical_bytes": corpus_bytes,
        "disk_measurement_rule": (
            "logical sum of file lengths under corpus/_conflict_mirrors and "
            "corpus/conflicts after mining, including _determinism_runs scratch outputs "
            "under the mirror root and excluding this DETERMINISM.json report; "
            "directory metadata and allocated-block slack are excluded"
        ),
        "git_version": GitRepository(args.mirror_root / KNOWN_CASE["slug"]).text(
            ["version"]
        ),
        "git_environment_overrides": dict(
            sorted(DETERMINISTIC_GIT_ENVIRONMENT_OVERRIDES.items())
        ),
        "git_environment_scrubbed": {
            "exact": list(SCRUBBED_GIT_ENVIRONMENT_KEYS),
            "prefixes": list(SCRUBBED_GIT_ENVIRONMENT_PREFIXES),
        },
        "known_case": known_case,
        "known_clean_case": known_clean_case,
        "mirror_logical_bytes": mirror_bytes,
        "merge_workers_per_run": args.merge_workers,
        "miner_protocol_revision": MINER_PROTOCOL_REVISION,
        "miner_source_sha256": MINER_SOURCE_SHA256,
        "platform": platform.platform(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "repositories": rows,
        "schema_version": 1,
        "total_disk_bytes": mirror_bytes + corpus_bytes,
    }
    atomic_json(args.report, report)
    print(canonical_json(report))
    return 0 if report["all_byte_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
