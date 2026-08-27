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

try:
    from .miner import MINER_PROTOCOL_REVISION, MINER_SOURCE_SHA256
except ImportError:  # direct script execution
    from miner import MINER_PROTOCOL_REVISION, MINER_SOURCE_SHA256


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = Path(__file__).with_name("repositories.json")
DEFAULT_ALL_MERGES_ROOT = PROJECT_ROOT / "corpus" / "conflicts" / "_all_merges"
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


def merge_bases_from_mined_rows(
    path: Path,
    summary_path: Path,
    repository: dict[str, Any],
    merges: Sequence[tuple[str, str, str]],
    first_parent_commits: int,
    excluded_octopus_merges: int,
) -> tuple[list[str | None], str, str]:
    """Load bases from a mined all-merges file after exact history validation.

    This post-mining fast path avoids recomputing one ``merge-base`` subprocess
    per merge.  It is deliberately fail-closed: rows must be in the exact
    first-parent order, must name the same parents, and must carry one uniform
    miner protocol/source provenance pair.
    """

    bases: list[str | None] = []
    status_counts: dict[str, int] = {"clean": 0, "conflicted": 0, "no_merge_base": 0}
    multiple_base_count = 0
    all_merges_digest = hashlib.sha256()
    row_count = 0
    try:
        stream = path.open("rb")
    except OSError as error:
        raise HydrationError(f"cannot read mined merge rows {path}: {error}") from error
    with stream:
        for position, raw_line in enumerate(stream, 1):
            all_merges_digest.update(raw_line)
            if not raw_line.endswith(b"\n") or raw_line.endswith(b"\r\n"):
                raise HydrationError(
                    f"mined merge row {position} is not exactly LF-terminated: {path}"
                )
            line = raw_line[:-1]
            if not line:
                raise HydrationError(f"blank mined merge row {position}: {path}")
            if position > len(merges):
                raise HydrationError(
                    f"mined merge rows contain an extra row {position} for "
                    f"{repository['slug']}"
                )
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HydrationError(
                    f"invalid mined merge row {position} in {path}: {error}"
                ) from error
            if not isinstance(row, dict):
                raise HydrationError(
                    f"mined merge row {position} is not an object: {path}"
                )
            try:
                canonical_line = canonical_json(row).encode("ascii")
            except (TypeError, ValueError) as error:
                raise HydrationError(
                    f"mined merge row {position} cannot be canonicalized: {path}: {error}"
                ) from error
            if canonical_line != line:
                raise HydrationError(
                    f"mined merge row {position} is not canonical LF-only JSON: {path}"
                )
            expected = merges[position - 1]
            row_count = position
            merge, parent1, parent2 = expected
            if type(row.get("schema_version")) is not int or row["schema_version"] != 1:
                raise HydrationError(
                    f"mined merge row {position} has unsupported schema for "
                    f"{repository['slug']}"
                )
            if row.get("repo") != repository["repo"]:
                raise HydrationError(
                    f"mined merge row {position} has wrong repo for "
                    f"{repository['slug']}"
                )
            if row.get("merge") != merge or row.get("parents") != [parent1, parent2]:
                raise HydrationError(
                    f"mined merge row {position} differs from first-parent history for "
                    f"{repository['slug']}"
                )

            if row.get("miner_protocol_revision") != MINER_PROTOCOL_REVISION:
                raise HydrationError(
                    f"mined merge row {position} has stale protocol provenance for "
                    f"{repository['slug']}"
                )
            if row.get("miner_source_sha256") != MINER_SOURCE_SHA256:
                raise HydrationError(
                    f"mined merge row {position} has stale source provenance for "
                    f"{repository['slug']}"
                )

            if "merge_base" not in row:
                raise HydrationError(
                    f"mined merge row {position} lacks explicit merge_base for "
                    f"{repository['slug']}"
                )
            base = row["merge_base"]
            merge_bases = row.get("merge_bases")
            multiple_merge_bases = row.get("multiple_merge_bases")
            status = row.get("evaluation_status")
            if status not in status_counts:
                raise HydrationError(
                    f"mined merge row {position} has unsupported status {status!r} for "
                    f"{repository['slug']}"
                )
            status_counts[status] += 1
            if base is None:
                if (
                    status != "no_merge_base"
                    or merge_bases != []
                    or multiple_merge_bases is not False
                ):
                    raise HydrationError(
                        f"mined merge row {position} has inconsistent null base for "
                        f"{repository['slug']}"
                    )
            else:
                if not isinstance(base, str) or not TEXT_OID_RE.fullmatch(base):
                    raise HydrationError(
                        f"mined merge row {position} has invalid base for "
                        f"{repository['slug']}"
                    )
                if status not in {"clean", "conflicted"}:
                    raise HydrationError(
                        f"mined merge row {position} has base with status {status!r} for "
                        f"{repository['slug']}"
                    )
                if (
                    not isinstance(merge_bases, list)
                    or not merge_bases
                    or any(
                        not isinstance(item, str) or not TEXT_OID_RE.fullmatch(item)
                        for item in merge_bases
                    )
                    or merge_bases != sorted(set(merge_bases))
                    or base not in merge_bases
                    or multiple_merge_bases is not (len(merge_bases) > 1)
                ):
                    raise HydrationError(
                        f"mined merge row {position} has inconsistent base list for "
                        f"{repository['slug']}"
                    )
                if multiple_merge_bases:
                    multiple_base_count += 1
            bases.append(base)

    if row_count != len(merges):
        raise HydrationError(
            f"mined merge row count {row_count} != first-parent merge count "
            f"{len(merges)} for {repository['slug']}"
        )

    try:
        summary_raw = summary_path.read_bytes()
    except OSError as error:
        raise HydrationError(
            f"cannot read mined summary {summary_path}: {error}"
        ) from error
    if not summary_raw.endswith(b"\n") or summary_raw.count(b"\n") != 1:
        raise HydrationError(
            f"mined summary is not one terminal-LF JSON record: {summary_path}"
        )
    try:
        summary = json.loads(summary_raw[:-1])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HydrationError(f"invalid mined summary {summary_path}: {error}") from error
    if not isinstance(summary, dict):
        raise HydrationError(f"mined summary is not an object: {summary_path}")
    try:
        canonical_summary = canonical_json(summary).encode("ascii")
    except (TypeError, ValueError) as error:
        raise HydrationError(
            f"mined summary cannot be canonicalized: {summary_path}: {error}"
        ) from error
    if canonical_summary != summary_raw[:-1]:
        raise HydrationError(f"mined summary is not canonical JSON: {summary_path}")

    all_merges_sha256 = all_merges_digest.hexdigest()
    expected_summary_fields = {
        "schema_version": 1,
        "repo": repository["repo"],
        "slug": repository["slug"],
        "head": repository["frozen_head"],
        "miner_protocol_revision": MINER_PROTOCOL_REVISION,
        "miner_source_sha256": MINER_SOURCE_SHA256,
        "first_parent_commits": first_parent_commits,
        "first_parent_merges": len(merges) + excluded_octopus_merges,
        "eligible_two_parent_merges": len(merges),
        "excluded_octopus_merges": excluded_octopus_merges,
        "clean_merges": status_counts["clean"],
        "conflicted_merges": status_counts["conflicted"],
        "failed_merges": status_counts["no_merge_base"],
        "no_merge_base_merges": status_counts["no_merge_base"],
        "multiple_merge_base_merges": multiple_base_count,
    }
    for field, expected_value in expected_summary_fields.items():
        observed_value = summary.get(field)
        if (
            (type(expected_value) is int and type(observed_value) is not int)
            or observed_value != expected_value
        ):
            raise HydrationError(
                f"mined summary field {field!r} differs from validated history/rows "
                f"for {repository['slug']}"
            )
    output_hashes = summary.get("output_sha256")
    if (
        not isinstance(output_hashes, dict)
        or output_hashes.get("all_merges") != all_merges_sha256
    ):
        raise HydrationError(
            f"mined summary all-merges hash differs from {path}"
        )
    return bases, all_merges_sha256, hashlib.sha256(summary_raw).hexdigest()


def hydrate_repository(
    git: str,
    repository: dict[str, Any],
    mirror_root: Path,
    batch_size: int,
    mined_all_merges_root: Path | None = None,
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
        "--reverse",
        frozen_head,
    )
    first_parent_commits, merges, octopus = parse_first_parent_history(history)
    mined_all_merges_sha256: str | None = None
    mined_summary_sha256: str | None = None
    mined_all_merges_path: Path | None = None
    mined_summary_path: Path | None = None
    if mined_all_merges_root is None:
        bases = merge_bases_for_history(git, mirror, merges)
        merge_base_source = "git_merge_base"
        merge_base_invocations = len(merges)
        merge_base_workers = DEFAULT_MERGE_BASE_WORKERS
    else:
        mined_all_merges_path = mined_all_merges_root / f"{slug}.jsonl"
        mined_summary_path = mined_all_merges_root.parent / "_summaries" / f"{slug}.json"
        bases, mined_all_merges_sha256, mined_summary_sha256 = merge_bases_from_mined_rows(
            mined_all_merges_path,
            mined_summary_path,
            repository,
            merges,
            first_parent_commits,
            octopus,
        )
        merge_base_source = "validated_mined_all_merges"
        merge_base_invocations = 0
        merge_base_workers = 0
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
        "merge_base_invocations": merge_base_invocations,
        "merge_base_workers": merge_base_workers,
        "merge_base_source": merge_base_source,
        "mined_merge_protocol_revision": (
            MINER_PROTOCOL_REVISION if mined_all_merges_root is not None else None
        ),
        "mined_merge_source_sha256": (
            MINER_SOURCE_SHA256 if mined_all_merges_root is not None else None
        ),
        "mined_all_merges_path": (
            stable_path(mined_all_merges_path) if mined_all_merges_path else None
        ),
        "mined_all_merges_sha256": mined_all_merges_sha256,
        "mined_summary_path": stable_path(mined_summary_path) if mined_summary_path else None,
        "mined_summary_sha256": mined_summary_sha256,
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
    merge_base_sources: dict[str, int] = {}
    for result in results:
        source = result.get("merge_base_source")
        if isinstance(source, str):
            merge_base_sources[source] = merge_base_sources.get(source, 0) + 1
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
        "merge_base_source_counts": dict(sorted(merge_base_sources.items())),
        "current_miner_protocol_revision": MINER_PROTOCOL_REVISION,
        "current_miner_source_sha256": MINER_SOURCE_SHA256,
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
    parser.add_argument(
        "--use-mined-bases",
        action="store_true",
        help=(
            "post-mining fast path using canonical all-merges rows below "
            "corpus/conflicts/_all_merges"
        ),
    )
    parser.add_argument(
        "--mined-all-merges-root",
        type=Path,
        help=(
            "post-mining fast path: load bases from validated canonical all-merges "
            "JSONL files below this directory instead of rerunning git merge-base"
        ),
    )
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
        if not args.use_mined_bases and args.mined_all_merges_root is None:
            mined_all_merges_root = None
        else:
            mined_all_merges_root = (
                args.mined_all_merges_root or DEFAULT_ALL_MERGES_ROOT
            ).resolve(strict=False)
            if not mined_all_merges_root.is_dir():
                raise HydrationError(
                    "mined all-merges root does not exist: "
                    f"{mined_all_merges_root}"
                )
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
                mined_all_merges_root,
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
