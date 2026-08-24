"""Incrementally refresh the protected retrieval index for historical snapshots.

The benchmark's canonical implementation lives in :mod:`index` and remains
unchanged.  Rebuilding its roughly 388 MiB SQLite database for every historical
commit would make timestamp reconstruction needlessly expensive, even though
adjacent commits usually change only a handful of files.  This module preserves
the canonical schema, filters, tokenisers, chunk identities, and FTS documents,
while replacing only file rows whose eligible content changed.

``refresh_index`` is intentionally snapshot-agnostic: callers materialise a
clean Git worktree (or a subtree within one) before invoking it.  A first call
uses the protected full builder.  Later calls audit the whole eligible file set,
delete stale rows, and insert changed rows using the protected primitives.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import posixpath
import sqlite3
import stat
import subprocess
import time
from collections import Counter
from contextlib import contextmanager
from functools import wraps
from typing import Any, Iterable, Sequence

try:  # Importable both as a package and as a direct script dependency.
    from . import index as canonical
except ImportError:  # pragma: no cover - direct runner import path
    import index as canonical


PROVENANCE_KEYS = {
    "schema_version": canonical.SCHEMA_VERSION,
    "index_implementation_sha256": canonical.INDEX_IMPLEMENTATION_SHA256,
    "tokenizer_version": canonical.TOKENIZER_VERSION,
    "legacy_tokenizer_version": canonical.LEGACY_TOKENIZER_VERSION,
    "region_identity": "path+startByte+endByte+sha256",
    "chunk_bytes": str(canonical.CHUNK_BYTES),
    "chunk_overlap_bytes": str(canonical.CHUNK_OVERLAP_BYTES),
    "snap_tolerance_bytes": str(canonical.SNAP_TOLERANCE_BYTES),
    "max_file_bytes": str(canonical.MAX_FILE_BYTES),
    "max_line_bytes": str(canonical.MAX_LINE_BYTES),
}

DELTA_SCHEMA = "git-delta-v1"
FTS_MAINTENANCE_POLICY = (
    "canonical full builds optimize FTS; Git-delta and full-snapshot refreshes "
    "use FTS5 default automerge without per-state optimize"
)


@contextmanager
def _longpath_git_environment() -> Iterable[None]:
    """Add ``core.longpaths=true`` for protected canonical Git helpers.

    The protected index module invokes Git internally and cannot be edited for
    this analysis.  Git's documented ``GIT_CONFIG_COUNT`` interface applies the
    setting to those child processes without mutating the source repository.
    """

    count_name = "GIT_CONFIG_COUNT"
    previous_count = os.environ.get(count_name)
    try:
        slot = int(previous_count) if previous_count is not None else 0
    except ValueError as error:
        raise RuntimeError("GIT_CONFIG_COUNT is not an integer") from error
    if slot < 0:
        raise RuntimeError("GIT_CONFIG_COUNT must not be negative")
    key_name = f"GIT_CONFIG_KEY_{slot}"
    value_name = f"GIT_CONFIG_VALUE_{slot}"
    previous_key = os.environ.get(key_name)
    previous_value = os.environ.get(value_name)
    os.environ[count_name] = str(slot + 1)
    os.environ[key_name] = "core.longpaths"
    os.environ[value_name] = "true"
    try:
        yield
    finally:
        if previous_count is None:
            os.environ.pop(count_name, None)
        else:
            os.environ[count_name] = previous_count
        if previous_key is None:
            os.environ.pop(key_name, None)
        else:
            os.environ[key_name] = previous_key
        if previous_value is None:
            os.environ.pop(value_name, None)
        else:
            os.environ[value_name] = previous_value


def _with_longpath_git_environment(function: Any) -> Any:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _longpath_git_environment():
            return function(*args, **kwargs)

    return wrapped


def _discard_database(path: Path) -> None:
    """Remove only the caller-supplied SQLite database and its sidecars."""

    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)


def _validated_source_root(source_root: str | os.PathLike[str]) -> Path:
    """Apply the canonical builder's root-boundary checks on every refresh."""

    supplied = Path(source_root)
    try:
        info = supplied.lstat()
    except OSError as error:
        raise ValueError(f"source root is unavailable: {source_root}") from error
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or (
        reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
    ):
        raise ValueError(f"source root must not be a symlink or reparse point: {source_root}")
    root = supplied.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    return root


def _git(
    root: Path,
    arguments: Sequence[str],
    *,
    allow_absent: bool = False,
    timeout: float = 300.0,
) -> str:
    """Run one read-only Git query with Windows long-path support enabled."""

    try:
        completed = subprocess.run(
            ["git", "-c", "core.longpaths=true", "-C", str(root), *arguments],
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {error}") from error
    if completed.returncode == 0:
        return completed.stdout
    if allow_absent and completed.returncode == 1:
        return ""
    detail = completed.stderr.strip() or f"exit {completed.returncode}"
    raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")


def _normalise_repository_relative_root(value: str | os.PathLike[str]) -> str:
    raw = os.fspath(value).replace("\\", "/").strip("/")
    if not raw or raw == ".":
        return ""
    normalised = posixpath.normpath(raw)
    if normalised == ".." or normalised.startswith("../") or posixpath.isabs(normalised):
        raise ValueError(f"repository-relative root escapes its repository: {value}")
    return normalised


def _derived_repository_relative_root(source: Path, git_root: Path) -> str:
    relative = os.path.relpath(source, git_root)
    return _normalise_repository_relative_root(Path(relative).as_posix())


def _same_repository_relative_root(left: str, right: str) -> bool:
    if os.name != "nt":
        return left == right
    return os.path.normcase(left.replace("/", "\\")) == os.path.normcase(
        right.replace("/", "\\")
    )


def _default_stream_identity(git_root: Path, *, git_timeout: float) -> str:
    common_text = _git(
        git_root, ("rev-parse", "--git-common-dir"), timeout=git_timeout
    ).strip()
    common = Path(common_text)
    if not common.is_absolute():
        common = git_root / common
    return os.path.normcase(str(common.resolve(strict=True)))


def _resolve_commit(git_root: Path, value: str, *, git_timeout: float) -> str:
    resolved = _git(
        git_root,
        ("rev-parse", "--verify", f"{value}^{{commit}}"),
        timeout=git_timeout,
    )
    commit = resolved.strip()
    if not commit:
        raise RuntimeError(f"Git did not resolve commit {value!r}")
    return commit


def _metadata(database: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in database.execute("SELECT key, value FROM metadata")
    }


def _path_fingerprint(digest: Any, label: str, path: Path | None) -> None:
    digest.update(label.encode("utf-8") + b"\0")
    if path is None:
        digest.update(b"<unset>\0")
        return
    digest.update(os.path.normcase(str(path)).encode("utf-8") + b"\0")
    try:
        info = path.lstat()
        digest.update(str(info.st_mode).encode("ascii") + b"\0")
        digest.update(path.read_bytes() + b"\0")
    except OSError as error:
        digest.update(f"<{type(error).__name__}:{error.errno}>".encode("ascii") + b"\0")


def _git_path(
    git_root: Path, arguments: Sequence[str], *, git_timeout: float
) -> Path | None:
    rendered = _git(
        git_root, arguments, allow_absent=True, timeout=git_timeout
    ).strip()
    if not rendered:
        return None
    path = Path(rendered)
    if not path.is_absolute():
        path = git_root / path
    return path.resolve(strict=False)


def _policy_fingerprint(git_root: Path, *, git_timeout: float) -> str:
    """Fingerprint non-commit inputs that affect ignore and checkout policy."""

    digest = hashlib.sha256()
    # Hash, but never persist, effective configuration.  This catches changes
    # to ignore files, attributes files, checkout filters, line endings,
    # symlink handling, and sparse-checkout policy between historical states.
    config = _git(
        git_root,
        ("config", "--null", "--list", "--show-origin"),
        timeout=git_timeout,
    )
    digest.update(config.encode("utf-8") + b"\0")
    _path_fingerprint(
        digest,
        "info/exclude",
        _git_path(
            git_root,
            ("rev-parse", "--git-path", "info/exclude"),
            git_timeout=git_timeout,
        ),
    )
    _path_fingerprint(
        digest,
        "info/attributes",
        _git_path(
            git_root,
            ("rev-parse", "--git-path", "info/attributes"),
            git_timeout=git_timeout,
        ),
    )
    _path_fingerprint(
        digest,
        "core.excludesFile",
        _git_path(
            git_root,
            ("config", "--path", "--get", "core.excludesFile"),
            git_timeout=git_timeout,
        ),
    )
    _path_fingerprint(
        digest,
        "core.attributesFile",
        _git_path(
            git_root,
            ("config", "--path", "--get", "core.attributesFile"),
            git_timeout=git_timeout,
        ),
    )
    return digest.hexdigest()


def _snapshot_clean(git_root: Path, *, git_timeout: float) -> bool:
    status = _git(
        git_root,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"),
        timeout=git_timeout,
    )
    return not status


def _changed_git_paths(
    git_root: Path, old_commit: str, new_commit: str, *, git_timeout: float
) -> list[str]:
    output = _git(
        git_root,
        (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            old_commit,
            new_commit,
            "--",
        ),
        timeout=git_timeout,
    )
    result: list[str] = []
    for item in output.split("\0"):
        if not item:
            continue
        normalised = item.replace("\\", "/")
        if normalised.startswith("/") or normalised == ".." or normalised.startswith("../"):
            raise RuntimeError(f"Git diff returned an unsafe path: {item!r}")
        result.append(normalised)
    return sorted(set(result), key=str.casefold)


def _is_prefix_path(prefix: str, path: str) -> bool:
    return not prefix or path == prefix or path.startswith(prefix + "/")


def _applicable_control_changes(
    changed_git_paths: Iterable[str], repository_relative_root: str
) -> list[str]:
    controls: list[str] = []
    for path in changed_git_paths:
        name = posixpath.basename(path)
        if name not in {".gitignore", ".gitattributes"}:
            continue
        directory = posixpath.dirname(path)
        if _is_prefix_path(directory, repository_relative_root) or _is_prefix_path(
            repository_relative_root, directory
        ):
            controls.append(path)
    return controls


def _source_relative_path(git_path: str, repository_relative_root: str) -> str | None:
    if not repository_relative_root:
        return git_path
    prefix = repository_relative_root + "/"
    if not git_path.startswith(prefix):
        return None
    return git_path[len(prefix) :]


def _regular_path_within_source(source: Path, relative: str) -> Path | None:
    parts = relative.split("/")
    if not relative or any(not part or part in {".", ".."} for part in parts):
        return None
    current = source
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for index, part in enumerate(parts):
        if part.casefold() in canonical.SKIP_ENTRY_NAMES:
            return None
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode) or (
            reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            return None
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                return None
        elif not stat.S_ISREG(info.st_mode):
            return None
    try:
        resolved = current.resolve(strict=True)
    except OSError:
        return None
    return resolved if canonical._path_within(resolved, source) else None


def _eligible_changed_files(
    source: Path,
    git_root: Path,
    repository_relative_root: str,
    changed_git_paths: Iterable[str],
    counts: Counter[str],
) -> tuple[dict[str, tuple[bytes, str, str]], set[str]]:
    """Read only changed current files while applying canonical eligibility."""

    candidates: dict[str, tuple[Path, str]] = {}
    touched: set[str] = set()
    for git_path in changed_git_paths:
        relative = _source_relative_path(git_path, repository_relative_root)
        if relative is None or not relative:
            continue
        touched.add(relative)
        path = _regular_path_within_source(source, relative)
        if path is None:
            counts["delta_absent_or_ineligible_path"] += 1
            continue
        candidates[relative] = (path, git_path)

    ignored = canonical._ignored_git_paths(
        git_root, [git_path for _, git_path in candidates.values()]
    )
    eligible: dict[str, tuple[bytes, str, str]] = {}
    for relative in sorted(candidates, key=str.casefold):
        path, git_path = candidates[relative]
        if git_path in ignored:
            counts["delta_excluded_gitignored_entry"] += 1
            continue
        raw, read_reason = canonical._read_file(path)
        if read_reason:
            counts[f"delta_excluded_{read_reason}"] += 1
            continue
        assert raw is not None
        content, content_reason = canonical._content_for_index(path, raw)
        if content_reason:
            counts[f"delta_excluded_{content_reason}"] += 1
            continue
        assert content is not None
        eligible[relative] = (raw, content, hashlib.sha256(raw).hexdigest())
        counts["delta_files_read"] += 1
        counts["delta_bytes_read"] += len(raw)
    return eligible, touched


def _compatible(db_path: Path) -> bool:
    if not db_path.is_file():
        return False
    try:
        metadata = canonical.index_stats(db_path)
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        return False
    if str(metadata.get("build_complete")) != "1":
        return False
    return all(str(metadata.get(key)) == str(value) for key, value in PROVENANCE_KEYS.items())


def _eligible_files(root: Path, git_root: Path, counts: Counter[str]) -> dict[str, tuple[bytes, str, str]]:
    """Return path -> (raw bytes, decoded content, SHA-256) for canonical inputs."""

    result: dict[str, tuple[bytes, str, str]] = {}
    for path, relative in canonical._enumerate_files(root, git_root, counts):
        raw, read_reason = canonical._read_file(path)
        if read_reason:
            counts[f"excluded_{read_reason}"] += 1
            continue
        assert raw is not None
        content, content_reason = canonical._content_for_index(path, raw)
        if content_reason:
            counts[f"excluded_{content_reason}"] += 1
            continue
        assert content is not None
        result[relative] = (raw, content, hashlib.sha256(raw).hexdigest())
    return result


def _delete_paths(database: sqlite3.Connection, paths: Iterable[str]) -> int:
    removed = 0
    for path in paths:
        rowids = [
            int(row[0])
            for row in database.execute("SELECT internal_rowid FROM chunks WHERE path = ?", (path,)).fetchall()
        ]
        if rowids:
            database.executemany("DELETE FROM chunk_fts WHERE internal_rowid = ?", ((value,) for value in rowids))
            database.executemany(
                "DELETE FROM chunk_fts_legacy WHERE internal_rowid = ?", ((value,) for value in rowids)
            )
            # ident_postings and chunks are removed by the files(path) cascade,
            # but deleting postings explicitly makes the operation independent
            # of a caller changing PRAGMA foreign_keys on its connection.
            database.executemany("DELETE FROM ident_postings WHERE internal_rowid = ?", ((value,) for value in rowids))
        removed += database.execute("DELETE FROM files WHERE path = ?", (path,)).rowcount
    return removed


def _insert_file(
    database: sqlite3.Connection,
    relative: str,
    raw: bytes,
    content: str,
    file_digest: str,
) -> tuple[int, int]:
    """Insert one file exactly as canonical.build_index does."""

    regions = list(canonical._chunks(raw))
    filename = Path(relative).name
    database.execute(
        "INSERT INTO files(path, name, size_bytes, content_sha256, chunk_count) VALUES (?, ?, ?, ?, ?)",
        (relative, filename, len(raw), file_digest, len(regions)),
    )
    path_tokens = canonical.tokenize_identifier_aware(relative)
    name_tokens = canonical.tokenize_identifier_aware(filename)
    path_tokens_json = json.dumps(path_tokens, ensure_ascii=True, separators=(",", ":"))
    name_tokens_json = json.dumps(name_tokens, ensure_ascii=True, separators=(",", ":"))
    encoded_path = canonical._fts_document(path_tokens)
    encoded_name = canonical._fts_document(name_tokens)

    for start_byte, end_byte in regions:
        piece_bytes = raw[start_byte:end_byte]
        piece = piece_bytes.decode("utf-8", errors="strict")
        digest = hashlib.sha256(piece_bytes).hexdigest()
        region_id = canonical._region_id(relative, start_byte, end_byte, digest)
        start_line, end_line = canonical._line_bounds(raw, start_byte, end_byte)
        aware_tokens = canonical.tokenize_identifier_aware(piece)
        legacy_tokens = canonical.tokenize_legacy(piece)
        inserted = database.execute(
            """
            INSERT INTO chunks(
              region_id, path, filename, start_byte, end_byte,
              content_sha256, start_line, end_line, text,
              body_tokens_json, legacy_tokens_json,
              path_tokens_json, name_tokens_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                region_id,
                relative,
                filename,
                start_byte,
                end_byte,
                digest,
                start_line,
                end_line,
                piece,
                json.dumps(aware_tokens, ensure_ascii=True, separators=(",", ":")),
                json.dumps(legacy_tokens, ensure_ascii=True, separators=(",", ":")),
                path_tokens_json,
                name_tokens_json,
            ),
        )
        internal_rowid = inserted.lastrowid
        if internal_rowid is None:
            raise RuntimeError("SQLite did not assign an internal row key")
        database.execute(
            "INSERT INTO chunk_fts(internal_rowid, body_tokens, path_tokens, name_tokens) VALUES (?, ?, ?, ?)",
            (internal_rowid, canonical._fts_document(aware_tokens), encoded_path, encoded_name),
        )
        database.execute(
            "INSERT INTO chunk_fts_legacy(internal_rowid, body_tokens) VALUES (?, ?)",
            (internal_rowid, canonical._fts_document(legacy_tokens)),
        )
        database.executemany(
            "INSERT INTO ident_postings(token, internal_rowid) VALUES (?, ?)",
            ((token, internal_rowid) for token in sorted(set(canonical.identifier_tokens(piece)))),
        )
    return len(regions), len(raw)


def _put_metadata(database: sqlite3.Connection, key: str, value: Any) -> None:
    rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    database.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, rendered))


def _put_v2_metadata(
    database: sqlite3.Connection,
    *,
    stream_identity: str,
    repository_relative_root: str,
    source_root: Path,
    policy_fingerprint: str,
    snapshot_clean: bool,
) -> None:
    values = {
        "v2_delta_schema": DELTA_SCHEMA,
        "v2_stream_identity": stream_identity,
        "v2_repository_relative_root": repository_relative_root,
        "v2_source_root": str(source_root),
        "v2_policy_fingerprint": policy_fingerprint,
        "v2_snapshot_clean": "1" if snapshot_clean else "0",
        "v2_fts_maintenance_policy": FTS_MAINTENANCE_POLICY,
    }
    for key, value in values.items():
        _put_metadata(database, key, value)


def _full_build(
    database_path: Path,
    source: Path,
    *,
    logical_root: str | os.PathLike[str],
    stream_identity: str,
    repository_relative_root: str,
    policy_fingerprint: str,
    snapshot_clean: bool,
    reason: str,
) -> dict[str, Any]:
    _discard_database(database_path)
    stats = canonical.build_index(database_path, source, logical_root=logical_root)
    database = canonical.connect_index(database_path)
    try:
        database.execute("BEGIN IMMEDIATE")
        _put_v2_metadata(
            database,
            stream_identity=stream_identity,
            repository_relative_root=repository_relative_root,
            source_root=source,
            policy_fingerprint=policy_fingerprint,
            snapshot_clean=snapshot_clean,
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
    return {
        "mode": "full",
        "audit_mode": "canonical_full_build",
        "fallback_reason": reason,
        "files_added": int(stats.get("files_indexed", 0)),
        "files_changed": 0,
        "files_removed": 0,
        "fts_maintenance_policy": FTS_MAINTENANCE_POLICY,
        "canonical_stats": stats,
    }


@_with_longpath_git_environment
def refresh_index(
    db_path: str | os.PathLike[str],
    source_root: str | os.PathLike[str],
    *,
    logical_root: str | os.PathLike[str],
    force_full: bool = False,
    expected_commit: str | None = None,
    stream_identity: str | None = None,
    repository_relative_root: str | os.PathLike[str] | None = None,
    git_timeout: float = 300.0,
) -> dict[str, Any]:
    """Make *db_path* exactly represent the eligible files in *source_root*.

    The returned dictionary distinguishes the full-build and incremental paths
    and records both audit and mutation counts.  Any exception rolls back the
    incremental transaction; a later invocation can safely retry.
    """

    database_path = Path(db_path)
    source = _validated_source_root(source_root)
    git_root, git_head = canonical._git_context(source)
    if git_head == "unborn":
        raise RuntimeError("historical retrieval snapshots require a committed Git HEAD")
    current_commit = _resolve_commit(git_root, git_head, git_timeout=git_timeout)
    if expected_commit is not None:
        resolved_expected = _resolve_commit(
            git_root, str(expected_commit), git_timeout=git_timeout
        )
        if resolved_expected != current_commit:
            raise RuntimeError(
                f"source HEAD is {current_commit}, expected historical commit {resolved_expected}"
            )

    derived_relative_root = _derived_repository_relative_root(source, git_root)
    if repository_relative_root is None:
        relative_root = derived_relative_root
    else:
        supplied_relative_root = _normalise_repository_relative_root(repository_relative_root)
        if not _same_repository_relative_root(supplied_relative_root, derived_relative_root):
            raise ValueError(
                "repository_relative_root does not identify source_root: "
                f"expected {derived_relative_root or '.'}, got {supplied_relative_root or '.'}"
            )
        # Git diff emits tree paths with repository casing.  Preserve the
        # physical spelling after validating a Windows case-insensitive input.
        relative_root = derived_relative_root
    selected_stream_identity = str(
        stream_identity
        or _default_stream_identity(git_root, git_timeout=git_timeout)
    )
    if not selected_stream_identity:
        raise ValueError("stream_identity must not be empty")

    policy_fingerprint = _policy_fingerprint(git_root, git_timeout=git_timeout)
    snapshot_clean = _snapshot_clean(git_root, git_timeout=git_timeout)
    if not snapshot_clean:
        raise RuntimeError(
            "refusing to index a dirty historical worktree; reset/clean the owned snapshot first"
        )
    if force_full or not _compatible(database_path):
        reason = "force_full" if force_full else "database_absent_or_incompatible"
        return _full_build(
            database_path,
            source,
            logical_root=logical_root,
            stream_identity=selected_stream_identity,
            repository_relative_root=relative_root,
            policy_fingerprint=policy_fingerprint,
            snapshot_clean=snapshot_clean,
            reason=reason,
        )

    database = canonical.connect_index(database_path)
    try:
        prior_metadata = _metadata(database)
    finally:
        database.close()

    stream_matches = (
        prior_metadata.get("v2_delta_schema") == DELTA_SCHEMA
        and prior_metadata.get("v2_stream_identity") == selected_stream_identity
        and prior_metadata.get("v2_repository_relative_root") == relative_root
        and os.path.normcase(prior_metadata.get("v2_source_root", ""))
        == os.path.normcase(str(source))
    )
    if not stream_matches:
        return _full_build(
            database_path,
            source,
            logical_root=logical_root,
            stream_identity=selected_stream_identity,
            repository_relative_root=relative_root,
            policy_fingerprint=policy_fingerprint,
            snapshot_clean=snapshot_clean,
            reason="stream_metadata_mismatch",
        )

    prior_commit_text = prior_metadata.get("git_head", "")
    try:
        prior_commit = _resolve_commit(
            git_root, prior_commit_text, git_timeout=git_timeout
        )
    except RuntimeError:
        prior_commit = ""

    counts: Counter[str] = Counter()
    started = time.perf_counter()
    changed_git_paths: list[str] = []
    audit_mode = "git_delta"
    fallback_reason: str | None = None
    eligible: dict[str, tuple[bytes, str, str]]
    touched_paths: set[str] | None = None

    if not prior_commit:
        fallback_reason = "prior_commit_unavailable"
    elif prior_metadata.get("v2_snapshot_clean") != "1":
        fallback_reason = "prior_snapshot_not_recorded_clean"
    elif prior_metadata.get("v2_policy_fingerprint") != policy_fingerprint:
        fallback_reason = "ignore_or_checkout_policy_changed"
    else:
        try:
            changed_git_paths = _changed_git_paths(
                git_root,
                prior_commit,
                current_commit,
                git_timeout=git_timeout,
            )
            controls = _applicable_control_changes(changed_git_paths, relative_root)
            if controls:
                names = sorted({posixpath.basename(path) for path in controls})
                fallback_reason = "applicable_control_changed:" + ",".join(names)
        except RuntimeError as error:
            fallback_reason = f"git_delta_unavailable:{type(error).__name__}: {error}"

    if fallback_reason:
        audit_mode = "full_snapshot"
        eligible = _eligible_files(source, git_root, counts)
    else:
        eligible, touched_paths = _eligible_changed_files(
            source, git_root, relative_root, changed_git_paths, counts
        )

    database = canonical.connect_index(database_path)
    try:
        existing = {
            str(row["path"]): (str(row["content_sha256"]), int(row["size_bytes"]))
            for row in database.execute("SELECT path, content_sha256, size_bytes FROM files")
        }
        eligible_paths = set(eligible)
        existing_paths = set(existing)
        if touched_paths is None:
            removed_candidates = existing_paths - eligible_paths
        else:
            removed_candidates = (existing_paths & touched_paths) - eligible_paths
        removed_paths = sorted(removed_candidates, key=str.casefold)
        changed_paths = sorted(
            (
                path
                for path in existing_paths & eligible_paths
                if existing[path] != (eligible[path][2], len(eligible[path][0]))
            ),
            key=str.casefold,
        )
        added_paths = sorted(eligible_paths - existing_paths, key=str.casefold)

        database.execute("BEGIN IMMEDIATE")
        _put_metadata(database, "build_complete", "0")
        removed_count = _delete_paths(database, [*removed_paths, *changed_paths])
        inserted_chunks = 0
        inserted_bytes = 0
        for path in [*changed_paths, *added_paths]:
            raw, content, digest = eligible[path]
            chunk_count, byte_count = _insert_file(database, path, raw, content, digest)
            inserted_chunks += chunk_count
            inserted_bytes += byte_count

        # FTS5 maintains document counts and BM25 corpus statistics on ordinary
        # insert/delete operations.  Its default automerge bounds segment
        # growth.  A per-state `optimize` would rewrite multi-gigabyte indexes
        # and is deliberately reserved for the protected canonical full build.
        final_files = int(database.execute("SELECT count(*) FROM files").fetchone()[0])
        final_chunks = int(database.execute("SELECT count(*) FROM chunks").fetchone()[0])
        final_bytes = int(database.execute("SELECT coalesce(sum(size_bytes), 0) FROM files").fetchone()[0])
        stats = dict(sorted(counts.items()))
        stats.update(
            {
                "elapsed_seconds": time.perf_counter() - started,
                "source_root": str(source),
                "git_head": current_commit,
                "delta_from_commit": prior_commit or None,
                "delta_changed_git_paths": len(changed_git_paths),
                "audit_mode": audit_mode,
                "fallback_reason": fallback_reason,
                "fts_maintenance_policy": FTS_MAINTENANCE_POLICY,
                "files_indexed": final_files,
                "chunks_indexed": final_chunks,
                "bytes_indexed": final_bytes,
                "incremental_files_added": len(added_paths),
                "incremental_files_changed": len(changed_paths),
                "incremental_files_removed": len(removed_paths),
                "incremental_chunks_inserted": inserted_chunks,
                "incremental_bytes_inserted": inserted_bytes,
            }
        )
        _put_metadata(database, "source_root", str(source))
        _put_metadata(database, "logical_root", str(logical_root))
        _put_metadata(database, "git_root", str(git_root))
        _put_metadata(database, "git_head", current_commit)
        _put_metadata(database, "stats", stats)
        _put_v2_metadata(
            database,
            stream_identity=selected_stream_identity,
            repository_relative_root=relative_root,
            source_root=source,
            policy_fingerprint=policy_fingerprint,
            snapshot_clean=snapshot_clean,
        )
        _put_metadata(database, "build_complete", "1")
        _put_metadata(database, "built_unix_seconds", time.time())
        database.commit()
        return {
            "mode": "incremental",
            "audit_mode": audit_mode,
            "fallback_reason": fallback_reason,
            "from_commit": prior_commit or None,
            "to_commit": current_commit,
            "changed_git_paths": len(changed_git_paths),
            "files_added": len(added_paths),
            "files_changed": len(changed_paths),
            "files_removed": len(removed_paths),
            "deleted_file_rows": removed_count,
            "fts_maintenance_policy": FTS_MAINTENANCE_POLICY,
            "canonical_stats": stats,
        }
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
