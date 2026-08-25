#!/usr/bin/env python3
"""Batch-hydrate blob inputs needed by historical merge replay.

Only task-owned bare mirrors below ``corpus/_conflict_mirrors`` are read or
written.  Discovery disables Git's lazy fetching.  Missing object IDs are then
sent, in sorted fixed-size batches, through the same explicit promisor fetch
shape Git uses for a partial clone.  No source clone or working tree is opened.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence, TypeVar


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("repositories.json")
DEFAULT_BATCH_SIZE = 20_000
DEFAULT_DIFF_PAIR_BATCH_SIZE = 512
DEFAULT_MERGE_BASE_WORKERS = 8
OID_RE = re.compile(rb"[0-9a-f]{40}\Z")
TEXT_OID_RE = re.compile(r"[0-9a-f]{40}\Z")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
ZERO_OID = b"0" * 40
RAW_RECORD_RE = re.compile(
    rb"^:([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) "
    rb"([A-Z][0-9]*)$"
)


class HydrationError(RuntimeError):
    """A deterministic hydration precondition or Git operation failed."""


T = TypeVar("T")


@dataclasses.dataclass(frozen=True)
class GitResult:
    command: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def git_environment(*, no_lazy_fetch: bool) -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("GIT_"):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    if no_lazy_fetch:
        environment["GIT_NO_LAZY_FETCH"] = "1"
    else:
        environment.pop("GIT_NO_LAZY_FETCH", None)
    return environment


def run_git(
    command: Sequence[str],
    *,
    stdin: bytes | None = None,
    no_lazy_fetch: bool,
) -> GitResult:
    completed = subprocess.run(
        list(command),
        input=stdin,
        check=False,
        capture_output=True,
        env=git_environment(no_lazy_fetch=no_lazy_fetch),
    )
    return GitResult(
        command=tuple(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def git_command(git: str, mirror: Path, *arguments: str) -> list[str]:
    return [git, "-C", str(mirror), *arguments]


def checked_git(
    git: str,
    mirror: Path,
    *arguments: str,
    stdin: bytes | None = None,
    no_lazy_fetch: bool = True,
) -> bytes:
    result = run_git(
        git_command(git, mirror, *arguments),
        stdin=stdin,
        no_lazy_fetch=no_lazy_fetch,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise HydrationError(
            f"git {' '.join(arguments)} exited {result.returncode}: {detail}"
        )
    return result.stdout


def parse_first_parent_history(
    output: bytes,
) -> tuple[int, list[tuple[str, str, str]], int]:
    """Return commit count, exact-two-parent merges, and octopus count."""

    merges: list[tuple[str, str, str]] = []
    octopus = 0
    commit_count = 0
    for line_number, line in enumerate(output.splitlines(), 1):
        fields = line.split()
        if not fields:
            continue
        if any(not OID_RE.fullmatch(field) for field in fields):
            raise HydrationError(
                f"invalid rev-list object ID on output line {line_number}"
            )
        commit_count += 1
        if len(fields) == 3:
            merges.append(tuple(field.decode("ascii") for field in fields))
        elif len(fields) > 3:
            octopus += 1
    return commit_count, merges, octopus


def parse_raw_object_ids(output: bytes) -> set[str]:
    """Extract nonzero old/new OIDs from batched ``diff-tree --raw -z`` output."""

    if not output:
        return set()
    fields = output.split(b"\0")
    if fields[-1] != b"":
        raise HydrationError("raw diff-tree output lacks its terminal NUL")
    fields.pop()
    object_ids: set[str] = set()
    index = 0
    while index < len(fields):
        raw_header = fields[index]
        index += 1
        # With --stdin, diff-tree prefixes each comparison with the first
        # tree-ish as a standalone NUL-delimited field. Paths are consumed as
        # part of the preceding raw record, so a path that happens to be a
        # 40-hex string cannot be confused with this separator.
        if OID_RE.fullmatch(raw_header):
            continue
        header, separator, inline_path = raw_header.partition(b"\t")
        match = RAW_RECORD_RE.fullmatch(header)
        if match is None:
            raise HydrationError(
                f"malformed raw diff-tree record header at field {index}: "
                f"{header[:160]!r}"
            )
        if separator:
            if not inline_path:
                raise HydrationError("raw diff-tree record has an empty inline path")
        else:
            if index >= len(fields):
                raise HydrationError("raw diff-tree record lacks its path field")
            if not fields[index]:
                raise HydrationError("raw diff-tree record has an empty path")
            index += 1
        for mode, object_id in ((match.group(1), match.group(3)), (match.group(2), match.group(4))):
            # A 160000 entry names a commit in another repository. It is not
            # an object that the superproject's promisor remote can supply.
            if mode != b"160000" and object_id != ZERO_OID:
                object_ids.add(object_id.decode("ascii"))
    return object_ids


def parse_batch_check(
    requested_oids: Sequence[str], output: bytes
) -> list[str]:
    lines = output.splitlines()
    if len(lines) != len(requested_oids):
        raise HydrationError(
            "cat-file --batch-check returned "
            f"{len(lines)} rows for {len(requested_oids)} requested OIDs"
        )
    missing: list[str] = []
    for position, (expected, line) in enumerate(zip(requested_oids, lines), 1):
        fields = line.split()
        if len(fields) < 2 or not OID_RE.fullmatch(fields[0]):
            raise HydrationError(f"malformed batch-check row {position}: {line!r}")
        observed = fields[0].decode("ascii")
        if observed != expected:
            raise HydrationError(
                f"batch-check row {position} returned {observed}, expected {expected}"
            )
        if fields[1] == b"missing":
            if len(fields) != 2:
                raise HydrationError(
                    f"malformed missing batch-check row {position}: {line!r}"
                )
            missing.append(expected)
        elif len(fields) != 3 or not fields[2].isdigit():
            raise HydrationError(f"malformed extant batch-check row {position}: {line!r}")
    return missing


def batch_check_missing(
    git: str, mirror: Path, object_ids: Sequence[str]
) -> list[str]:
    if not object_ids:
        return []
    payload = ("".join(f"{object_id}\n" for object_id in object_ids)).encode("ascii")
    output = checked_git(
        git,
        mirror,
        "cat-file",
        "--batch-check",
        stdin=payload,
        no_lazy_fetch=True,
    )
    return parse_batch_check(object_ids, output)


def fixed_batches(values: Sequence[T], batch_size: int) -> Iterator[list[T]]:
    if batch_size <= 0:
        raise HydrationError("batch size must be positive")
    for start in range(0, len(values), batch_size):
        yield list(values[start : start + batch_size])


def fetch_command(git: str, mirror: Path) -> list[str]:
    return [
        git,
        "-C",
        str(mirror),
        "-c",
        "fetch.negotiationAlgorithm=noop",
        "fetch",
        "origin",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        "--recurse-submodules=no",
        "--filter=blob:none",
        "--stdin",
    ]


def fetch_missing_batch(
    git: str, mirror: Path, object_ids: Sequence[str], batch_number: int
) -> None:
    payload = ("".join(f"{object_id}\n" for object_id in object_ids)).encode("ascii")
    result = run_git(
        fetch_command(git, mirror),
        stdin=payload,
        no_lazy_fetch=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise HydrationError(
            f"promisor fetch batch {batch_number} exited {result.returncode}: {detail}"
        )


def oid_list_sha256(object_ids: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for object_id in object_ids:
        digest.update(object_id.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_mirror(
    git: str,
    mirror: Path,
    mirror_root: Path,
    expected_url: str,
    frozen_head: str,
) -> None:
    root = mirror_root.resolve(strict=True)
    if mirror.is_symlink():
        raise HydrationError(f"task mirror may not be a symlink: {mirror}")
    try:
        resolved = mirror.resolve(strict=True)
    except FileNotFoundError as error:
        raise HydrationError(f"task mirror does not exist: {mirror}") from error
    if resolved.parent != root:
        raise HydrationError(f"task mirror resolves outside mirror root: {mirror}")
    alternates = resolved / "objects" / "info" / "alternates"
    if alternates.exists() and alternates.read_text(encoding="utf-8").strip():
        raise HydrationError(f"task mirror borrows objects through alternates: {mirror}")

    bare = checked_git(git, resolved, "rev-parse", "--is-bare-repository").strip()
    if bare != b"true":
        raise HydrationError(f"task mirror is not bare: {mirror}")
    origin = checked_git(git, resolved, "remote", "get-url", "origin").strip()
    if origin.decode("utf-8", errors="replace") != expected_url:
        raise HydrationError(
            f"task mirror origin differs from manifest for {mirror.name}"
        )
    promisor = checked_git(
        git, resolved, "config", "--bool", "remote.origin.promisor"
    ).strip()
    if promisor != b"true":
        raise HydrationError(f"task mirror is not a promisor clone: {mirror}")
    partial_filter = checked_git(
        git, resolved, "config", "--get", "remote.origin.partialclonefilter"
    ).strip()
    if partial_filter != b"blob:none":
        raise HydrationError(f"task mirror does not use blob:none: {mirror}")
    pinned = checked_git(
        git, resolved, "rev-parse", f"{frozen_head}^{{commit}}"
    ).strip()
    if pinned.decode("ascii", errors="replace") != frozen_head:
        raise HydrationError(f"frozen head is absent from task mirror: {frozen_head}")


def primary_merge_base(
    git: str, mirror: Path, parent1: str, parent2: str
) -> str | None:
    result = run_git(
        git_command(git, mirror, "merge-base", parent1, parent2),
        no_lazy_fetch=True,
    )
    if result.returncode == 1 and not result.stdout.strip():
        return None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise HydrationError(
            f"git merge-base {parent1} {parent2} exited {result.returncode}: {detail}"
        )
    lines = result.stdout.splitlines()
    if len(lines) != 1 or not OID_RE.fullmatch(lines[0]):
        raise HydrationError(
            f"merge-base returned {len(lines)} noncanonical rows for {parent1} {parent2}"
        )
    return lines[0].decode("ascii")


def changed_object_ids_for_pairs(
    git: str,
    mirror: Path,
    pairs: Sequence[tuple[str, str]],
) -> tuple[set[str], int]:
    """Return changed object IDs using deterministic fixed-size stdin batches."""

    object_ids: set[str] = set()
    invocation_count = 0
    for batch in fixed_batches(pairs, DEFAULT_DIFF_PAIR_BATCH_SIZE):
        stdin = b"".join(f"{base} {parent}\n".encode("ascii") for base, parent in batch)
        raw = checked_git(
            git,
            mirror,
            "diff-tree",
            "-r",
            "--raw",
            "-z",
            "--no-renames",
            "--stdin",
            stdin=stdin,
        )
        object_ids.update(parse_raw_object_ids(raw))
        invocation_count += 1
    return object_ids, invocation_count


def changed_object_ids(
    git: str, mirror: Path, base: str, parent: str
) -> set[str]:
    """Compatibility wrapper for a single logical tree comparison."""

    object_ids, _ = changed_object_ids_for_pairs(git, mirror, [(base, parent)])
    return object_ids


def merge_bases_for_history(
    git: str,
    mirror: Path,
    merges: Sequence[tuple[str, str, str]],
) -> list[str | None]:
    """Compute ordered primary merge bases with bounded read-only concurrency."""

    def resolve(item: tuple[int, tuple[str, str, str]]) -> str | None:
        merge_number, (merge, parent1, parent2) = item
        try:
            return primary_merge_base(git, mirror, parent1, parent2)
        except HydrationError as error:
            raise HydrationError(
                f"merge {merge_number}/{len(merges)} {merge}: {error}"
            ) from error

    with ThreadPoolExecutor(max_workers=DEFAULT_MERGE_BASE_WORKERS) as executor:
        return list(executor.map(resolve, enumerate(merges, 1)))


def hydrate_repository(
    git: str,
    repository: dict[str, Any],
    mirror_root: Path,
    batch_size: int,
) -> dict[str, Any]:
    slug = repository["slug"]
    frozen_head = repository["frozen_head"]
    mirror = mirror_root / slug
    validate_mirror(
        git,
        mirror,
        mirror_root,
        repository["url"],
        frozen_head,
    )
    mirror = mirror.resolve(strict=True)

    history = checked_git(
        git,
        mirror,
        "rev-list",
        "--first-parent",
        "--parents",
        frozen_head,
    )
    first_parent_commits, merges, octopus = parse_first_parent_history(history)
    bases = merge_bases_for_history(git, mirror, merges)
    comparison_pairs: list[tuple[str, str]] = []
    no_merge_base_merges: list[str] = []
    merge_input_digest = hashlib.sha256()
    for (merge, parent1, parent2), base in zip(merges, bases):
        if base is None:
            no_merge_base_merges.append(merge)
        else:
            comparison_pairs.extend(((base, parent1), (base, parent2)))
        merge_input_digest.update(
            f"{merge}\0{parent1}\0{parent2}\0{base or '<none>'}\n".encode("ascii")
        )
    object_ids, diff_tree_invocations = changed_object_ids_for_pairs(
        git, mirror, comparison_pairs
    )

    sorted_oids = sorted(object_ids)
    missing_before = sorted(batch_check_missing(git, mirror, sorted_oids))
    batch_rows: list[dict[str, Any]] = []
    for batch_number, batch in enumerate(
        fixed_batches(missing_before, batch_size), 1
    ):
        fetch_missing_batch(git, mirror, batch, batch_number)
        batch_rows.append(
            {
                "batch": batch_number,
                "first_oid": batch[0],
                "last_oid": batch[-1],
                "oid_count": len(batch),
            }
        )
    missing_after = sorted(batch_check_missing(git, mirror, missing_before))
    if missing_after:
        raise HydrationError(
            f"{len(missing_after)} requested objects remain missing after fetch"
        )

    return {
        "repo": repository["repo"],
        "slug": slug,
        "frozen_head": frozen_head,
        "status": "hydrated" if missing_before else "already_hydrated",
        "mirror": stable_path(mirror),
        "first_parent_commits": first_parent_commits,
        "eligible_two_parent_merges": len(merges),
        "excluded_octopus_merges": octopus,
        "diff_tree_invocations": diff_tree_invocations,
        "diff_tree_logical_comparisons": len(comparison_pairs),
        "diff_tree_pair_batch_size": DEFAULT_DIFF_PAIR_BATCH_SIZE,
        "merge_base_workers": DEFAULT_MERGE_BASE_WORKERS,
        "no_merge_base_count": len(no_merge_base_merges),
        "no_merge_base_merges": no_merge_base_merges,
        "primary_merge_inputs_sha256": merge_input_digest.hexdigest(),
        "candidate_object_count": len(sorted_oids),
        "candidate_oids_sha256": oid_list_sha256(sorted_oids),
        "missing_before_count": len(missing_before),
        "missing_before_sha256": oid_list_sha256(missing_before),
        "fetch_batch_size": batch_size,
        "fetch_batch_count": len(batch_rows),
        "fetch_batches": batch_rows,
        "fetched_oid_request_count": len(missing_before),
        "missing_after_count": 0,
        "missing_after_sha256": oid_list_sha256([]),
    }


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HydrationError(f"cannot read repository manifest {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise HydrationError("repository manifest must be a schema-version-1 object")
    if not isinstance(value.get("mirror_root"), str):
        raise HydrationError("repository manifest lacks mirror_root")
    repositories = value.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise HydrationError("repository manifest lacks repositories")
    seen: set[str] = set()
    for index, repository in enumerate(repositories):
        if not isinstance(repository, dict):
            raise HydrationError(f"manifest repository {index} is not an object")
        for field in ("repo", "slug", "url", "frozen_head"):
            if not isinstance(repository.get(field), str):
                raise HydrationError(
                    f"manifest repository {index} lacks string field {field}"
                )
        if not SLUG_RE.fullmatch(repository["slug"]):
            raise HydrationError(f"unsafe manifest slug: {repository['slug']!r}")
        if not TEXT_OID_RE.fullmatch(repository["frozen_head"]):
            raise HydrationError(
                f"invalid frozen head for {repository['slug']}: "
                f"{repository['frozen_head']!r}"
            )
        if repository["slug"] in seen:
            raise HydrationError(f"duplicate manifest slug: {repository['slug']}")
        seen.add(repository["slug"])
    return value


def select_repositories(
    repositories: Sequence[dict[str, Any]], requested_slugs: Iterable[str]
) -> list[dict[str, Any]]:
    requested = set(requested_slugs)
    if not requested:
        return list(repositories)
    available = {repository["slug"] for repository in repositories}
    unknown = sorted(requested - available)
    if unknown:
        raise HydrationError(f"unknown --only slug(s): {', '.join(unknown)}")
    return [
        repository
        for repository in repositories
        if repository["slug"] in requested
    ]


def build_report(
    results: Sequence[dict[str, Any]], batch_size: int, mirror_root: Path
) -> dict[str, Any]:
    failed = sum(result["status"] == "failed" for result in results)
    return {
        "schema_version": 1,
        "artifact_type": "conflict_mirror_hydration_report",
        "batch_size": batch_size,
        "mirror_root": stable_path(mirror_root),
        "repository_count": len(results),
        "counts": {
            "already_hydrated": sum(
                result["status"] == "already_hydrated" for result in results
            ),
            "hydrated": sum(result["status"] == "hydrated" for result in results),
            "failed": failed,
        },
        "fetch_invocation": [
            "git",
            "-C",
            "<task-owned-bare-mirror>",
            "-c",
            "fetch.negotiationAlgorithm=noop",
            "fetch",
            "origin",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "--recurse-submodules=no",
            "--filter=blob:none",
            "--stdin",
        ],
        "discovery_lazy_fetch": False,
        "results": list(results),
    }


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.partial")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--mirror-root",
        type=Path,
        help="override the task mirror root recorded in repositories.json",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SLUG",
        help="hydrate only this manifest slug; repeat to select multiple repositories",
    )
    parser.add_argument("--report", type=Path, help="write the canonical JSON report")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--git", default="git", help="Git executable")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = parse_arguments(arguments)
    try:
        if args.batch_size <= 0:
            raise HydrationError("--batch-size must be positive")
        manifest = load_manifest(args.manifest.resolve())
        repositories = select_repositories(manifest["repositories"], args.only)
        if args.mirror_root is None:
            mirror_root = (PROJECT_ROOT / manifest["mirror_root"]).resolve(
                strict=False
            )
        else:
            mirror_root = args.mirror_root.resolve(strict=False)
        if not mirror_root.is_dir():
            raise HydrationError(f"task mirror root does not exist: {mirror_root}")
    except HydrationError as error:
        print(f"hydrate_repositories.py: {error}", file=sys.stderr)
        return 2

    results: list[dict[str, Any]] = []
    for repository in repositories:
        try:
            result = hydrate_repository(
                args.git,
                repository,
                mirror_root,
                args.batch_size,
            )
        except Exception as error:  # preserve independent later repository attempts
            result = {
                "repo": repository["repo"],
                "slug": repository["slug"],
                "frozen_head": repository["frozen_head"],
                "mirror": stable_path(mirror_root / repository["slug"]),
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
            }
        results.append(result)
        print(f"{repository['slug']}: {result['status']}", file=sys.stderr, flush=True)

    report = build_report(results, args.batch_size, mirror_root)
    rendered = canonical_json(report) + "\n"
    sys.stdout.write(rendered)
    if args.report is not None:
        atomic_write(args.report.resolve(), rendered)
    return 1 if report["counts"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
