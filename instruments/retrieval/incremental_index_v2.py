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
import sqlite3
import stat
import time
from collections import Counter
from typing import Any, Iterable

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


def refresh_index(
    db_path: str | os.PathLike[str],
    source_root: str | os.PathLike[str],
    *,
    logical_root: str | os.PathLike[str],
    force_full: bool = False,
) -> dict[str, Any]:
    """Make *db_path* exactly represent the eligible files in *source_root*.

    The returned dictionary distinguishes the full-build and incremental paths
    and records both audit and mutation counts.  Any exception rolls back the
    incremental transaction; a later invocation can safely retry.
    """

    database_path = Path(db_path)
    source = _validated_source_root(source_root)
    if force_full or not _compatible(database_path):
        stats = canonical.build_index(database_path, source, logical_root=logical_root)
        return {
            "mode": "full",
            "files_added": int(stats.get("files_indexed", 0)),
            "files_changed": 0,
            "files_removed": 0,
            "canonical_stats": stats,
        }

    git_root, git_head = canonical._git_context(source)
    counts: Counter[str] = Counter()
    started = time.perf_counter()
    eligible = _eligible_files(source, git_root, counts)

    database = canonical.connect_index(database_path)
    try:
        existing = {
            str(row["path"]): (str(row["content_sha256"]), int(row["size_bytes"]))
            for row in database.execute("SELECT path, content_sha256, size_bytes FROM files")
        }
        eligible_paths = set(eligible)
        existing_paths = set(existing)
        removed_paths = sorted(existing_paths - eligible_paths, key=str.casefold)
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

        # FTS5 deletes leave tombstones.  Optimize once per historical state so
        # query latency and BM25 corpus statistics match a compact fresh build.
        if removed_paths or changed_paths or added_paths:
            database.execute("INSERT INTO chunk_fts(chunk_fts) VALUES ('optimize')")
            database.execute("INSERT INTO chunk_fts_legacy(chunk_fts_legacy) VALUES ('optimize')")

        final_files = int(database.execute("SELECT count(*) FROM files").fetchone()[0])
        final_chunks = int(database.execute("SELECT count(*) FROM chunks").fetchone()[0])
        final_bytes = int(database.execute("SELECT coalesce(sum(size_bytes), 0) FROM files").fetchone()[0])
        stats = dict(sorted(counts.items()))
        stats.update(
            {
                "elapsed_seconds": time.perf_counter() - started,
                "source_root": str(source),
                "git_head": git_head,
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
        _put_metadata(database, "git_head", git_head)
        _put_metadata(database, "stats", stats)
        _put_metadata(database, "build_complete", "1")
        _put_metadata(database, "built_unix_seconds", time.time())
        database.commit()
        return {
            "mode": "incremental",
            "files_added": len(added_paths),
            "files_changed": len(changed_paths),
            "files_removed": len(removed_paths),
            "deleted_file_rows": removed_count,
            "canonical_stats": stats,
        }
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
