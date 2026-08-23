"""Inspect and materialize reverted-commit tasks for the posture experiment.

This module consumes the stream produced by ``instruments/replay/extract.py``;
it does not walk Git history or normalize renames independently.  Git is used
only to obtain commit text and the exact first-parent binary diff.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STREAM_ROOT = PROJECT_ROOT / "exploratory" / "language-hole" / "streams"
DEFAULT_OUTPUT = PROJECT_ROOT / "exploratory" / "posture" / "task-candidates.json"


def command(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdin: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        shell=False,
    )


def git(repository: Path, *arguments: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return command(["git", "-c", "core.longpaths=true", *arguments], cwd=repository, stdin=stdin, check=check)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_stream(slug: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = STREAM_ROOT / f"{slug}.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8", errors="surrogatepass") as handle:
        header = json.loads(handle.readline())
        commits = [json.loads(line) for line in handle]
    if header.get("type") != "header" or any(commit.get("type") != "commit" for commit in commits):
        raise ValueError(f"invalid replay extraction stream: {path}")
    return header, commits


def paths_for_commit(commit: dict[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for change in commit["changes"]:
        if change["status"] == "R":
            paths.append(change["new_path"])
        else:
            paths.append(change["path"])
    return tuple(paths)


def text(repository: Path, sha: str, fmt: str) -> str:
    return git(repository, "show", "-s", f"--format={fmt}", sha).stdout.decode("utf-8", errors="replace").strip()


def first_parent_diff(repository: Path, commit: dict[str, Any]) -> bytes:
    parents = commit["parents"]
    if not parents:
        raise ValueError("root commit cannot be a reverted task")
    return git(
        repository,
        "diff",
        "--binary",
        "--full-index",
        "--find-renames=50%",
        "-l0",
        parents[0],
        commit["sha"],
        "--",
    ).stdout


def reverse_check(repository: Path, patch: bytes) -> tuple[bool, str]:
    result = git(repository, "apply", "--reverse", "--check", "--binary", "-", stdin=patch, check=False)
    return result.returncode == 0, result.stderr.decode("utf-8", errors="replace").strip()


def candidate_records(
    repository: Path,
    commits: Iterable[dict[str, Any]],
    *,
    check_reverse: bool,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for commit in commits:
        paths = paths_for_commit(commit)
        statuses = tuple(change["status"] for change in commit["changes"])
        if not (1 <= len(paths) <= 8):
            continue
        if any(status != "M" for status in statuses):
            continue
        if not any(path.startswith("src/") for path in paths):
            continue
        patch = first_parent_diff(repository, commit) if check_reverse else b""
        applicable, error = reverse_check(repository, patch) if check_reverse else (None, "not_checked")
        subject = text(repository, commit["sha"], "%s")
        body = text(repository, commit["sha"], "%B")
        records.append(
            {
                "index": commit["index"],
                "sha": commit["sha"],
                "parents": commit["parents"],
                "timestamp": commit["timestamp"],
                "subject": subject,
                "message": body,
                "paths": list(paths),
                "path_count": len(paths),
                "has_source_and_test": any(path.startswith("tests/") for path in paths),
                "reverse_applies_individually_at_head": applicable,
                "reverse_check_error": error or None,
                "patch_bytes": len(patch) if check_reverse else None,
            }
        )
    return records


def pairwise_overlap(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for left, right in itertools.combinations(records, 2):
        shared = sorted(set(left["paths"]) & set(right["paths"]))
        if shared:
            result.append({"left": left["sha"], "right": right["sha"], "shared_paths": shared})
    return result


def sets_of_four(records: Sequence[dict[str, Any]]) -> tuple[list[list[str]], list[list[str]]]:
    # Bound combinatorics by retaining only individually applicable source+test
    # tasks for bundle suggestions.  Selection is still reviewed manually.
    eligible = [
        record
        for record in records
        if record["reverse_applies_individually_at_head"] and record["has_source_and_test"]
    ]
    cliques: list[list[str]] = []
    independent: list[list[str]] = []
    for group in itertools.combinations(eligible, 4):
        intersections = [set(a["paths"]) & set(b["paths"]) for a, b in itertools.combinations(group, 2)]
        if all(intersections):
            cliques.append([record["sha"] for record in group])
        if not any(intersections):
            independent.append([record["sha"] for record in group])
        if len(cliques) >= 100 and len(independent) >= 100:
            break
    return cliques, independent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--skip-reverse-check",
        action="store_true",
        help="Inventory normalized path sets first; exact reverse checks can be limited to selected tasks later.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = args.repo.resolve(strict=True)
    header, commits = load_stream(args.slug)
    records = candidate_records(repository, commits, check_reverse=not args.skip_reverse_check)
    cliques, independent = sets_of_four(records) if not args.skip_reverse_check else ([], [])
    value = {
        "schema_version": 1,
        "measurement": "posture-reverted-task-candidate-audit",
        "repository": str(repository),
        "source_head_sha": header["source_head_sha"],
        "stream_protocol": {
            "git_log_arguments": header["git_log_arguments"],
            "rename_handling": "from instruments/replay/extract.py stream",
            "ground_truth_diff": "commit against first parent",
        },
        "candidates": records,
        "overlap_edges": pairwise_overlap(records),
        "four_task_cliques": cliques,
        "four_task_independent_sets": independent,
    }
    atomic_json(args.output.resolve(), value)
    print(
        json.dumps(
            {
                "candidates": len(records),
                "applicable": sum(record["reverse_applies_individually_at_head"] is True for record in records),
                "cliques": len(cliques),
                "independent_sets": len(independent),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
