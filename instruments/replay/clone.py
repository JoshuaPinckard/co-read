"""Clone the specified corpus without blobs and record resolved corpus metadata."""

from __future__ import annotations

import argparse
import collections
import os
from pathlib import Path, PurePosixPath

from common import (
    CLONE_ROOT,
    CORPUS_PATH,
    REPOSITORIES,
    SCHEMA_VERSION,
    atomic_write_json,
    ensure_directories,
    load_json,
    run_git,
    selected_repositories,
    utc_now,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        action="append",
        dest="repos",
        help="Repository slug to process; repeatable. Defaults to the full corpus.",
    )
    return parser.parse_args()


def extension(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return suffix if suffix else "<none>"


def tracked_paths(repository: Path) -> list[str]:
    result = run_git(repository, ["ls-tree", "-r", "--name-only", "-z", "HEAD"], text=False)
    assert isinstance(result.stdout, bytes)
    return [
        token.decode("utf-8", errors="surrogateescape")
        for token in result.stdout.split(b"\0")
        if token
    ]


def collect_metadata(repository: Path) -> dict[str, object]:
    head = str(run_git(repository, ["rev-parse", "HEAD"]).stdout).strip()
    reachable = int(str(run_git(repository, ["rev-list", "--count", "HEAD"]).stdout).strip())
    first_parent = int(
        str(run_git(repository, ["rev-list", "--first-parent", "--count", "HEAD"]).stdout).strip()
    )
    paths = tracked_paths(repository)
    histogram = collections.Counter(extension(path) for path in paths)
    partial_filter = str(
        run_git(repository, ["config", "--get", "remote.origin.promisor"], check=False).stdout
    ).strip()
    return {
        "resolved_head_sha": head,
        "reachable_commit_count": reachable,
        "first_parent_commit_count": first_parent,
        "tracked_file_count_at_head": len(paths),
        "extension_histogram_at_head": dict(sorted(histogram.items())),
        "partial_clone_promisor": partial_filter.lower() == "true",
    }


def corpus_document() -> dict[str, object]:
    existing = load_json(CORPUS_PATH, default={}) or {}
    records = existing.get("repositories", {}) if isinstance(existing, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "measurement": "cross-language-co-change-replay",
        "updated_at_utc": utc_now(),
        "clone_root": "corpus/_clones",
        "clone_filter": "blob:none",
        "repository_order": [repo["slug"] for repo in REPOSITORIES],
        "repositories": records,
    }


def process_repository(spec: dict[str, str], document: dict[str, object]) -> None:
    records = document["repositories"]
    assert isinstance(records, dict)
    destination = CLONE_ROOT / spec["slug"]
    started_at = utc_now()
    record: dict[str, object] = {
        **spec,
        "clone_path": f"corpus/_clones/{spec['slug']}",
        "clone_started_at_utc": started_at,
        "status": "in_progress",
    }
    records[spec["slug"]] = record
    document["updated_at_utc"] = utc_now()
    atomic_write_json(CORPUS_PATH, document)

    try:
        if destination.exists():
            if not (destination / ".git").exists():
                raise RuntimeError(f"Destination exists but is not a Git clone: {destination}")
            record["clone_action"] = "reused_existing_clone"
        else:
            result = run_git(
                None,
                ["clone", "--filter=blob:none", "--no-checkout", spec["url"], str(destination)],
                check=False,
            )
            record["clone_stdout_tail"] = str(result.stdout)[-4_000:]
            record["clone_stderr_tail"] = str(result.stderr)[-4_000:]
            record["clone_returncode"] = result.returncode
            if result.returncode != 0:
                raise RuntimeError(f"git clone exited {result.returncode}")
            record["clone_action"] = "cloned"

        origin = str(run_git(destination, ["remote", "get-url", "origin"]).stdout).strip()
        if origin.casefold().removesuffix(".git") != spec["url"].casefold().removesuffix(".git"):
            raise RuntimeError(f"Existing clone origin mismatch: {origin!r}")
        record.update(collect_metadata(destination))
        record["status"] = "ok"
    except Exception as exc:  # A failed repository is an output, not a reason to lose earlier records.
        record["status"] = "failed"
        record["failure_type"] = type(exc).__name__
        record["failure"] = str(exc)
    finally:
        record["clone_completed_at_utc"] = utc_now()
        document["updated_at_utc"] = utc_now()
        atomic_write_json(CORPUS_PATH, document)
        print(f"{spec['name']}: {record['status']}", flush=True)


def main() -> None:
    ensure_directories()
    document = corpus_document()
    for repository in selected_repositories(parse_args().repos):
        process_repository(repository, document)


if __name__ == "__main__":
    main()
