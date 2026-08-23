#!/usr/bin/env python3
"""Build and query the benchmark's lexical SQLite index.

The module deliberately uses only the Python standard library and SQLite FTS5.
It contains no embedding or model fallback.  Public region identity is always
the tuple ``(relative POSIX path, start byte, end byte, SHA-256)``; SQLite row
numbers and traversal order are never exposed as identity.
"""

from __future__ import annotations

import argparse
import bisect
from collections import Counter, deque
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from typing import Iterable, Iterator, Sequence


INDEX_IMPLEMENTATION_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCHEMA_VERSION = "1"
TOKENIZER_VERSION = "identifier-aware-v1"
LEGACY_TOKENIZER_VERSION = "lower-ascii-word-underscore-v1"

MAX_FILE_BYTES = 512 * 1024
CHUNK_BYTES = 4096
CHUNK_OVERLAP_BYTES = 512
SNAP_TOLERANCE_BYTES = 512
DEGENERATE_MIN_BYTES = 8 * 1024
MAX_LINE_BYTES = 128 * 1024
CONTROL_HEAVY_RATIO = 0.01

SKIP_ENTRY_NAMES = frozenset({".git", "node_modules"})
PATH_BM25_WEIGHTS = (1.0, 2.0, 4.0)  # body, path, filename

# Kept byte-for-byte equivalent in meaning to src/lib/search.js.  In
# particular, do not broaden these patterns in this benchmark: doing so would
# make the two indexes observe different security boundaries.
SENSITIVE_FILE = re.compile(
    r"^(?:\.env(?:\..*)?|\.npmrc|\.pypirc|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|"
    r"credentials(?:\.[^.]+)?|secrets?(?:\.[^.]+)?|"
    r"service[-_]?account(?:\.[^.]+)?)$",
    re.IGNORECASE | re.ASCII,
)
SENSITIVE_EXTENSIONS = frozenset({".key", ".pem", ".p12", ".pfx", ".jks", ".keystore"})
PLAINTEXT_SECRET = re.compile(
    r"(?:-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"\bBearer\s+[A-Za-z0-9._~+/\-]{20,}|"
    r"\b(?:sk_(?:live|test|prod)_[A-Za-z0-9]{16,}|"
    r"AIza[0-9A-Za-z_-]{24,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}))\b",
    re.IGNORECASE | re.ASCII,
)

# A lexical unit can be a code identifier or a filename/dotted name.  The full
# unit is retained, then separator and camel-case components are emitted.
_LEXICAL_UNIT = re.compile(r"[A-Za-z0-9_]+(?:[.\-][A-Za-z0-9_]+)*", re.ASCII)
_CAMEL_PART = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[A-Z]+|[0-9]+",
    re.ASCII,
)
_LEGACY_TOKEN = re.compile(r"[a-z0-9_]+", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_SAFE_FTS_TOKEN = re.compile(r"[a-z0-9]+", re.ASCII)


def tokenize_identifier_aware(text: str) -> list[str]:
    """Return full lowercase lexical units plus all code-aware components.

    Order and multiplicity are retained for BM25 term frequency.  A component
    identical to its unsplit full unit is not emitted twice.
    """

    tokens: list[str] = []
    for match in _LEXICAL_UNIT.finditer(str(text)):
        raw = match.group(0)
        full = raw.lower()
        tokens.append(full)
        parts: list[str] = []
        for separated in re.split(r"[_.\-]+", raw):
            if not separated:
                continue
            parts.extend(piece.lower() for piece in _CAMEL_PART.findall(separated))
        if parts != [full]:
            tokens.extend(parts)
    return tokens


def tokenize_legacy(text: str) -> list[str]:
    """Reproduce search.js's literal ``/[a-z0-9_]+/g`` tokenisation."""

    return _LEGACY_TOKEN.findall(str(text).lower())


def identifier_tokens(query: str) -> list[str]:
    """Return distinct, canonical exact identifiers from *query* in order."""

    seen: set[str] = set()
    result: list[str] = []
    for match in _IDENTIFIER.finditer(str(query)):
        token = match.group(0).lower()
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result


def sensitive_file(path: str | os.PathLike[str], content: str) -> bool:
    """The exact filename, extension, and plaintext filter from search.js."""

    name = Path(path).name
    return bool(
        SENSITIVE_FILE.search(name)
        or Path(name).suffix.lower() in SENSITIVE_EXTENSIONS
        or PLAINTEXT_SECRET.search(content)
    )


def _sensitive_reason(path: Path, content: str) -> str | None:
    name = path.name
    if SENSITIVE_FILE.search(name):
        return "sensitive_name"
    if path.suffix.lower() in SENSITIVE_EXTENSIONS:
        return "sensitive_extension"
    if PLAINTEXT_SECRET.search(content):
        return "sensitive_content"
    return None


def _token_encoding(token: str) -> str:
    """Encode one already-normalised token as one safe unicode61 FTS token."""

    # The prefix prevents FTS operators such as OR from acquiring syntax.  Most
    # code tokens need no expansion; punctuation-bearing compounds use hex so
    # unicode61 cannot split the required full token at '.', '-' or '_'.
    if _SAFE_FTS_TOKEN.fullmatch(token):
        return "t" + token
    return "h" + token.encode("utf-8").hex()


def _fts_document(tokens: Iterable[str]) -> str:
    return " ".join(_token_encoding(token) for token in tokens)


def _fts_query(tokens: Iterable[str], column: str | None = None) -> str:
    distinct = list(dict.fromkeys(tokens))
    expression = " OR ".join(_token_encoding(token) for token in distinct)
    if not expression:
        return ""
    return f"{column} : ({expression})" if column else f"({expression})"


def _is_reparse_or_link(entry: os.DirEntry[str]) -> bool:
    try:
        if entry.is_symlink():
            return True
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and getattr(info, "st_file_attributes", 0) & reparse_flag)


def _path_within(path: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root))))
    except ValueError:
        return False
    return common == os.path.normcase(str(root))


def _run_git(root: Path, arguments: Sequence[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="strict",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"git is required to enforce ignore rules: {error}") from error


def _git_context(source_root: Path) -> tuple[Path, str]:
    result = _run_git(source_root, ("rev-parse", "--show-toplevel"))
    if result.returncode != 0:
        detail = result.stderr.strip() or "not a Git worktree"
        raise RuntimeError(f"cannot enforce gitignore below {source_root}: {detail}")
    git_root = Path(result.stdout.strip()).resolve(strict=True)
    if not _path_within(source_root, git_root):
        raise RuntimeError(f"source root resolved outside its Git worktree: {source_root}")
    head = _run_git(source_root, ("rev-parse", "HEAD"))
    head_text = head.stdout.strip() if head.returncode == 0 else "unborn"
    return git_root, head_text


def _ignored_git_paths(git_root: Path, paths: Sequence[str]) -> set[str]:
    """Ask Git about a batch, including tracked paths via --no-index."""

    if not paths:
        return set()
    # With --stdin and -z, Git returns pathnames verbatim and cannot confuse a
    # newline or leading dash in a legal filename with protocol syntax.
    payload = "\0".join(paths) + "\0"
    result = _run_git(git_root, ("check-ignore", "--no-index", "-z", "--stdin"), input_text=payload)
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"git check-ignore failed: {detail}")
    return {item for item in result.stdout.split("\0") if item}


def _enumerate_files(source_root: Path, git_root: Path, counts: Counter[str]) -> Iterator[tuple[Path, str]]:
    """Yield secure, non-ignored regular-file candidates without following links."""

    queue: deque[Path] = deque([source_root])
    # Scanning immediate children in batches lets Git prune ignored directories
    # before their contents are ever visited, without spawning once per entry.
    while queue:
        directories = [queue.popleft() for _ in range(min(64, len(queue)))]
        pending: list[tuple[Path, str, str, bool]] = []
        for directory in directories:
            try:
                with os.scandir(directory) as scan:
                    entries = sorted(scan, key=lambda item: item.name.casefold())
            except OSError:
                counts["excluded_scan_error"] += 1
                continue
            for entry in entries:
                counts["entries_seen"] += 1
                if entry.name.casefold() in SKIP_ENTRY_NAMES:
                    counts["excluded_boundary_entry"] += 1
                    continue
                if _is_reparse_or_link(entry):
                    counts["excluded_symlink_or_reparse"] += 1
                    continue
                candidate = Path(entry.path)
                try:
                    real = candidate.resolve(strict=True)
                except OSError:
                    counts["excluded_stat_error"] += 1
                    continue
                if not _path_within(real, source_root):
                    counts["excluded_out_of_root"] += 1
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    counts["excluded_stat_error"] += 1
                    continue
                if not is_directory and not is_file:
                    counts["excluded_not_regular"] += 1
                    continue
                source_relative = candidate.relative_to(source_root).as_posix()
                git_relative = Path(os.path.relpath(candidate, git_root)).as_posix()
                pending.append((candidate, source_relative, git_relative, is_directory))

        ignored = _ignored_git_paths(git_root, [item[2] for item in pending])
        for candidate, source_relative, git_relative, is_directory in pending:
            if git_relative in ignored:
                counts["excluded_gitignored_entry"] += 1
                continue
            if is_directory:
                queue.append(candidate)
            else:
                counts["files_considered"] += 1
                yield candidate, source_relative


def _read_file(path: Path) -> tuple[bytes | None, str | None]:
    """Read at most the cap plus one byte and classify basic file failures."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None, "read_error"
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None, "not_regular"
        if info.st_size == 0:
            return None, "empty"
        if info.st_size > MAX_FILE_BYTES:
            return None, "too_large"
        pieces: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            piece = os.read(descriptor, min(65536, remaining))
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
        raw = b"".join(pieces)
    except OSError:
        return None, "read_error"
    finally:
        os.close(descriptor)
    if not raw:
        return None, "empty"
    if len(raw) > MAX_FILE_BYTES:
        return None, "too_large"
    return raw, None


def _content_for_index(path: Path, raw: bytes) -> tuple[str | None, str | None]:
    if b"\x00" in raw:
        return None, "binary_nul"
    if len(raw) > DEGENERATE_MIN_BYTES and raw.count(b"\n") < 2:
        return None, "degenerate_fewer_than_two_lf"
    if any(len(line) > MAX_LINE_BYTES for line in raw.split(b"\n")):
        return None, "degenerate_line_over_cap"
    try:
        content = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, "invalid_utf8"
    controls = sum(
        1
        for character in content
        if (ord(character) < 32 or 127 <= ord(character) <= 159)
        and character not in "\t\n\r\f"
    )
    if controls / max(1, len(content)) > CONTROL_HEAVY_RATIO:
        return None, "control_heavy"
    reason = _sensitive_reason(path, content)
    if reason:
        return None, reason
    return content, None


def _structural_boundaries(raw: bytes) -> list[tuple[int, bool]]:
    boundaries: list[tuple[int, bool]] = [(0, False)]
    position = 0
    while position < len(raw):
        newline = raw.find(b"\n", position)
        end = len(raw) if newline < 0 else newline + 1
        line = raw[position:end].rstrip(b"\r\n")
        is_blank = not line.strip(b" \t")
        is_column_zero = bool(line) and line[:1] not in (b" ", b"\t")
        if position and (is_blank or is_column_zero):
            boundaries.append((position, is_blank))
        if newline < 0:
            break
        position = newline + 1
    if boundaries[-1][0] != len(raw):
        boundaries.append((len(raw), False))
    return boundaries


def _utf8_boundary(raw: bytes, position: int) -> int:
    position = max(0, min(position, len(raw)))
    while 0 < position < len(raw) and raw[position] & 0xC0 == 0x80:
        position -= 1
    return position


def _snap_boundary(raw: bytes, boundaries: Sequence[tuple[int, bool]], target: int) -> int:
    offsets = [item[0] for item in boundaries]
    insertion = bisect.bisect_left(offsets, target)
    choices = boundaries[max(0, insertion - 2) : min(len(boundaries), insertion + 2)]
    close = [item for item in choices if abs(item[0] - target) <= SNAP_TOLERANCE_BYTES]
    if not close:
        return _utf8_boundary(raw, target)
    # Distance is load-bearing.  Blank lines win only an exact distance tie;
    # an earlier boundary wins the remaining tie for stable deterministic cuts.
    return min(close, key=lambda item: (abs(item[0] - target), not item[1], item[0]))[0]


def _chunks(raw: bytes) -> Iterator[tuple[int, int]]:
    boundaries = _structural_boundaries(raw)
    start = 0
    while start < len(raw):
        desired_end = min(start + CHUNK_BYTES, len(raw))
        end = len(raw) if desired_end == len(raw) else _snap_boundary(raw, boundaries, desired_end)
        if end <= start:
            end = _utf8_boundary(raw, desired_end)
        if end <= start:
            end = min(len(raw), start + CHUNK_BYTES)
        yield start, end
        if end >= len(raw):
            return
        desired_start = max(start + 1, end - CHUNK_OVERLAP_BYTES)
        next_start = _snap_boundary(raw, boundaries, desired_start)
        if next_start <= start or next_start >= end:
            next_start = _utf8_boundary(raw, desired_start)
        if next_start <= start or next_start >= end:
            next_start = max(start + 1, end - CHUNK_OVERLAP_BYTES)
        start = next_start


def _line_bounds(raw: bytes, start: int, end: int) -> tuple[int, int]:
    start_line = raw.count(b"\n", 0, start) + 1
    last_included = max(start, end - 1)
    end_line = raw.count(b"\n", 0, last_included) + 1
    return start_line, end_line


def _region_id(path: str, start: int, end: int, digest: str) -> str:
    # A canonical, reversible encoding of the public identity tuple.  It is not
    # a chunk ordinal and does not change merely because another region exists.
    return json.dumps([path, start, end, digest], ensure_ascii=True, separators=(",", ":"))


def _open_database(path: str | os.PathLike[str]) -> sqlite3.Connection:
    database = sqlite3.connect(str(path), timeout=60.0)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    database.execute("PRAGMA busy_timeout = 60000")
    return database


def _create_schema(database: sqlite3.Connection) -> None:
    database.executescript(
        """
        DROP TABLE IF EXISTS ident_postings;
        DROP TABLE IF EXISTS chunk_fts_legacy;
        DROP TABLE IF EXISTS chunk_fts;
        DROP TABLE IF EXISTS chunks;
        DROP TABLE IF EXISTS files;
        DROP TABLE IF EXISTS metadata;

        CREATE TABLE metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE files (
          path TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          size_bytes INTEGER NOT NULL,
          content_sha256 TEXT NOT NULL,
          chunk_count INTEGER NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE chunks (
          -- A compact SQLite join key only.  It is never returned and is not a
          -- region/chunk identity; region_id below is the canonical public
          -- path+byte-bounds+hash identity.
          internal_rowid INTEGER PRIMARY KEY,
          region_id TEXT NOT NULL UNIQUE,
          path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
          filename TEXT NOT NULL,
          start_byte INTEGER NOT NULL,
          end_byte INTEGER NOT NULL,
          content_sha256 TEXT NOT NULL,
          start_line INTEGER NOT NULL,
          end_line INTEGER NOT NULL,
          text TEXT NOT NULL,
          body_tokens_json TEXT NOT NULL,
          legacy_tokens_json TEXT NOT NULL,
          path_tokens_json TEXT NOT NULL,
          name_tokens_json TEXT NOT NULL,
          UNIQUE(path, start_byte, end_byte, content_sha256)
        );

        CREATE VIRTUAL TABLE chunk_fts USING fts5(
          internal_rowid UNINDEXED,
          body_tokens,
          path_tokens,
          name_tokens,
          tokenize = 'unicode61 remove_diacritics 0'
        );

        CREATE VIRTUAL TABLE chunk_fts_legacy USING fts5(
          internal_rowid UNINDEXED,
          body_tokens,
          tokenize = 'unicode61 remove_diacritics 0'
        );

        CREATE TABLE ident_postings (
          token TEXT NOT NULL,
          internal_rowid INTEGER NOT NULL REFERENCES chunks(internal_rowid) ON DELETE CASCADE,
          PRIMARY KEY(token, internal_rowid)
        ) WITHOUT ROWID;
        CREATE INDEX ident_postings_internal ON ident_postings(internal_rowid);
        CREATE INDEX chunks_path_start ON chunks(path, start_byte);
        """
    )


def _put_metadata(database: sqlite3.Connection, key: str, value: object) -> None:
    rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    database.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, rendered))


def build_index(
    db_path: str | os.PathLike[str],
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str] | None = None,
) -> dict[str, object]:
    """Build a fresh benchmark index and return its measured build statistics.

    ``logical_root`` is provenance metadata only.  Public result paths always
    remain POSIX paths relative to ``source_root``.
    """

    supplied_root = Path(source_root)
    try:
        supplied_info = supplied_root.lstat()
    except OSError as error:
        raise ValueError(f"source root is unavailable: {source_root}") from error
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(supplied_info.st_mode) or (
        reparse_flag and getattr(supplied_info, "st_file_attributes", 0) & reparse_flag
    ):
        raise ValueError(f"source root must not be a symlink or reparse point: {source_root}")
    root = supplied_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")

    git_root, git_head = _git_context(root)
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    database = _open_database(target)
    counts: Counter[str] = Counter()
    started = time.perf_counter()
    try:
        database.execute("PRAGMA journal_mode = WAL")
        database.execute("PRAGMA synchronous = NORMAL")
        database.execute("PRAGMA temp_store = MEMORY")
        _create_schema(database)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "index_implementation_sha256": INDEX_IMPLEMENTATION_SHA256,
            "tokenizer_version": TOKENIZER_VERSION,
            "legacy_tokenizer_version": LEGACY_TOKENIZER_VERSION,
            "source_root": str(root),
            "logical_root": str(logical_root) if logical_root is not None else str(root),
            "git_root": str(git_root),
            "git_head": git_head,
            "path_format": "source-root-relative-posix",
            "region_identity": "path+startByte+endByte+sha256",
            "chunk_bytes": CHUNK_BYTES,
            "chunk_overlap_bytes": CHUNK_OVERLAP_BYTES,
            "snap_tolerance_bytes": SNAP_TOLERANCE_BYTES,
            "max_file_bytes": MAX_FILE_BYTES,
            "degenerate_min_bytes": DEGENERATE_MIN_BYTES,
            "max_line_bytes": MAX_LINE_BYTES,
            "control_heavy_ratio": CONTROL_HEAVY_RATIO,
            "bm25_path_weights": list(PATH_BM25_WEIGHTS),
            "fts_token_encoding": "t+safe-token-or-h+utf8-hex",
            "gitignore_mode": "git-check-ignore---no-index",
            "build_complete": "0",
        }
        for key, value in metadata.items():
            _put_metadata(database, key, value)
        database.commit()

        insert_file = database.execute
        for path, relative_path in _enumerate_files(root, git_root, counts):
            raw, read_reason = _read_file(path)
            if read_reason:
                counts[f"excluded_{read_reason}"] += 1
                continue
            assert raw is not None
            content, content_reason = _content_for_index(path, raw)
            if content_reason:
                counts[f"excluded_{content_reason}"] += 1
                continue
            assert content is not None

            regions = list(_chunks(raw))
            filename = path.name
            file_digest = hashlib.sha256(raw).hexdigest()
            insert_file(
                "INSERT INTO files(path, name, size_bytes, content_sha256, chunk_count) VALUES (?, ?, ?, ?, ?)",
                (relative_path, filename, len(raw), file_digest, len(regions)),
            )
            path_tokens = tokenize_identifier_aware(relative_path)
            name_tokens = tokenize_identifier_aware(filename)
            path_tokens_json = json.dumps(path_tokens, ensure_ascii=True, separators=(",", ":"))
            name_tokens_json = json.dumps(name_tokens, ensure_ascii=True, separators=(",", ":"))
            encoded_path = _fts_document(path_tokens)
            encoded_name = _fts_document(name_tokens)

            for start_byte, end_byte in regions:
                piece_bytes = raw[start_byte:end_byte]
                piece = piece_bytes.decode("utf-8", errors="strict")
                digest = hashlib.sha256(piece_bytes).hexdigest()
                region_id = _region_id(relative_path, start_byte, end_byte, digest)
                start_line, end_line = _line_bounds(raw, start_byte, end_byte)
                aware_tokens = tokenize_identifier_aware(piece)
                legacy_tokens = tokenize_legacy(piece)
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
                        relative_path,
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
                    (internal_rowid, _fts_document(aware_tokens), encoded_path, encoded_name),
                )
                database.execute(
                    "INSERT INTO chunk_fts_legacy(internal_rowid, body_tokens) VALUES (?, ?)",
                    (internal_rowid, _fts_document(legacy_tokens)),
                )
                exact = set(identifier_tokens(piece))
                database.executemany(
                    "INSERT INTO ident_postings(token, internal_rowid) VALUES (?, ?)",
                    ((token, internal_rowid) for token in sorted(exact)),
                )
                counts["chunks_indexed"] += 1

            counts["files_indexed"] += 1
            counts["bytes_indexed"] += len(raw)
            if counts["files_indexed"] % 100 == 0:
                _put_metadata(database, "partial_stats", dict(counts))
                database.commit()

        database.execute("INSERT INTO chunk_fts(chunk_fts) VALUES ('optimize')")
        database.execute("INSERT INTO chunk_fts_legacy(chunk_fts_legacy) VALUES ('optimize')")
        elapsed = time.perf_counter() - started
        stats: dict[str, object] = dict(sorted(counts.items()))
        stats["elapsed_seconds"] = elapsed
        stats["source_root"] = str(root)
        stats["git_head"] = git_head
        _put_metadata(database, "stats", stats)
        _put_metadata(database, "build_complete", "1")
        _put_metadata(database, "built_unix_seconds", time.time())
        database.execute("DELETE FROM metadata WHERE key = 'partial_stats'")
        database.commit()
        return stats
    except Exception as error:
        try:
            database.rollback()
            _put_metadata(database, "build_complete", "0")
            _put_metadata(database, "partial_stats", dict(counts))
            _put_metadata(database, "build_error", f"{type(error).__name__}: {error}")
            database.commit()
        except sqlite3.Error:
            pass
        raise
    finally:
        database.close()


def connect_index(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    """Open a completed index for query helpers."""

    database = _open_database(db_path)
    try:
        row = database.execute("SELECT value FROM metadata WHERE key = 'build_complete'").fetchone()
    except sqlite3.Error:
        database.close()
        raise ValueError(f"not a retrieval index: {db_path}") from None
    if row is None or row[0] != "1":
        database.close()
        raise ValueError(f"retrieval index build is incomplete: {db_path}")
    return database


def _normalise_allowed_path(path: str, source_root: str | None) -> str:
    candidate = str(path)
    if source_root and os.path.isabs(candidate):
        try:
            candidate = os.path.relpath(candidate, source_root)
        except ValueError:
            pass
    candidate = candidate.replace("\\", "/")
    while candidate.startswith("./"):
        candidate = candidate[2:]
    return candidate


def _allowed_join(
    database: sqlite3.Connection,
    allowed_paths: Iterable[str] | None,
) -> str:
    if allowed_paths is None:
        return ""
    source_row = database.execute("SELECT value FROM metadata WHERE key = 'source_root'").fetchone()
    source_root = source_row[0] if source_row else None
    paths = sorted({_normalise_allowed_path(str(path), source_root) for path in allowed_paths})
    database.execute(
        "CREATE TEMP TABLE IF NOT EXISTS allowed_query_paths(path TEXT PRIMARY KEY COLLATE NOCASE) WITHOUT ROWID"
    )
    database.execute("DELETE FROM allowed_query_paths")
    database.executemany("INSERT INTO allowed_query_paths(path) VALUES (?)", ((path,) for path in paths))
    return " JOIN allowed_query_paths allowed ON allowed.path = c.path COLLATE NOCASE "


def _result_rows(rows: Iterable[sqlite3.Row], *, exact: bool = False) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for row in rows:
        raw_score = float(row["rank_value"])
        score = raw_score if exact else -raw_score
        results.append(
            {
                "path": row["path"],
                "start_byte": int(row["start_byte"]),
                "end_byte": int(row["end_byte"]),
                "hash": row["content_sha256"],
                "start_line": int(row["start_line"]),
                "end_line": int(row["end_line"]),
                "text": row["text"],
                "score": score,
            }
        )
    return results


def query_index(
    conn: sqlite3.Connection,
    query_text: str,
    arm: str,
    limit: int,
    allowed_paths: Iterable[str] | None = None,
    ignore_case: bool = False,
) -> list[dict[str, object]]:
    """Query one index arm and return ranked stable regions.

    The lexical index is deliberately canonicalised to lowercase, matching the
    tokenisation required by the spec.  ``ignore_case`` is accepted so the arm
    harness can pass the recorded Grep option uniformly; it does not change the
    index's canonical BM25 or exact-posting semantics.
    """

    del ignore_case
    if not isinstance(limit, int) or limit <= 0:
        return []
    allowed_join = _allowed_join(conn, allowed_paths)
    if allowed_paths is not None:
        allowed_count = conn.execute("SELECT count(*) FROM allowed_query_paths").fetchone()[0]
        if not allowed_count:
            return []

    common_columns = """
      c.path, c.start_byte, c.end_byte, c.content_sha256,
      c.start_line, c.end_line, c.text
    """
    arm_name = arm.lower().strip()

    if arm_name == "ident_first":
        identifiers = identifier_tokens(query_text)
        if not identifiers:
            # This is the only fallback allowed by the arm definition.
            arm_name = "bm25"
        else:
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS current_ident_tokens(token TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            conn.execute("DELETE FROM current_ident_tokens")
            conn.executemany("INSERT INTO current_ident_tokens(token) VALUES (?)", ((item,) for item in identifiers))
            sql = f"""
              WITH scored AS MATERIALIZED (
                SELECT {common_columns}, count(*) AS rank_value
                FROM current_ident_tokens query_token
                JOIN ident_postings posting ON posting.token = query_token.token
                JOIN chunks c ON c.internal_rowid = posting.internal_rowid
                {allowed_join}
                GROUP BY c.region_id
              ), per_path AS (
                SELECT scored.*,
                       row_number() OVER (
                         PARTITION BY path
                         ORDER BY rank_value DESC, start_byte ASC, end_byte ASC, content_sha256 ASC
                       ) AS path_rank
                FROM scored
              )
              SELECT path, start_byte, end_byte, content_sha256,
                     start_line, end_line, text, rank_value
              FROM per_path
              WHERE path_rank = 1
              ORDER BY rank_value DESC, path COLLATE NOCASE ASC, start_byte ASC, end_byte ASC
              LIMIT ?
            """
            return _result_rows(conn.execute(sql, (limit,)).fetchall(), exact=True)

    aware_tokens = tokenize_identifier_aware(query_text)
    if arm_name in {"bm25_legacy", "legacy_bm25", "bm25_nocamel"}:
        legacy_tokens = tokenize_legacy(query_text)
        expression = _fts_query(legacy_tokens, "body_tokens")
        if not expression:
            return []
        sql = f"""
          WITH scored AS MATERIALIZED (
            SELECT {common_columns}, bm25(chunk_fts_legacy, 0.0, 1.0) AS rank_value
            FROM chunk_fts_legacy
            JOIN chunks c ON c.internal_rowid = chunk_fts_legacy.internal_rowid
            {allowed_join}
            WHERE chunk_fts_legacy MATCH ?
          ), per_path AS (
            SELECT scored.*,
                   row_number() OVER (
                     PARTITION BY path
                     ORDER BY rank_value ASC, start_byte ASC, end_byte ASC, content_sha256 ASC
                   ) AS path_rank
            FROM scored
          )
          SELECT path, start_byte, end_byte, content_sha256,
                 start_line, end_line, text, rank_value
          FROM per_path
          WHERE path_rank = 1
          ORDER BY rank_value ASC, path COLLATE NOCASE ASC, start_byte ASC, end_byte ASC
          LIMIT ?
        """
        return _result_rows(conn.execute(sql, (expression, limit)).fetchall())

    if not aware_tokens:
        return []
    if arm_name == "bm25":
        expression = _fts_query(aware_tokens, "body_tokens")
        # internal_rowid is the first, UNINDEXED FTS column.  It still occupies a
        # weight position, hence the leading zero in both weight lists.
        weights = "0.0, 1.0, 0.0, 0.0"
    elif arm_name == "bm25_pathboost":
        expression = _fts_query(aware_tokens)
        weights = "0.0, " + ", ".join(str(weight) for weight in PATH_BM25_WEIGHTS)
    else:
        raise ValueError(f"unknown index arm: {arm}")

    sql = f"""
      WITH scored AS MATERIALIZED (
        SELECT {common_columns}, bm25(chunk_fts, {weights}) AS rank_value
        FROM chunk_fts
        JOIN chunks c ON c.internal_rowid = chunk_fts.internal_rowid
        {allowed_join}
        WHERE chunk_fts MATCH ?
      ), per_path AS (
        SELECT scored.*,
               row_number() OVER (
                 PARTITION BY path
                 ORDER BY rank_value ASC, start_byte ASC, end_byte ASC, content_sha256 ASC
               ) AS path_rank
        FROM scored
      )
      SELECT path, start_byte, end_byte, content_sha256,
             start_line, end_line, text, rank_value
      FROM per_path
      WHERE path_rank = 1
      ORDER BY rank_value ASC, path COLLATE NOCASE ASC, start_byte ASC, end_byte ASC
      LIMIT ?
    """
    return _result_rows(conn.execute(sql, (expression, limit)).fetchall())


def index_stats(db_path: str | os.PathLike[str]) -> dict[str, object]:
    database = _open_database(db_path)
    try:
        metadata = {row["key"]: row["value"] for row in database.execute("SELECT key, value FROM metadata")}
        for key in ("stats", "partial_stats", "bm25_path_weights"):
            if key in metadata:
                try:
                    metadata[key] = json.loads(metadata[key])
                except (TypeError, json.JSONDecodeError):
                    pass
        metadata["database_bytes"] = Path(db_path).stat().st_size
        metadata["files"] = database.execute("SELECT count(*) FROM files").fetchone()[0]
        metadata["chunks"] = database.execute("SELECT count(*) FROM chunks").fetchone()[0]
        metadata["identifier_postings"] = database.execute("SELECT count(*) FROM ident_postings").fetchone()[0]
        return metadata
    finally:
        database.close()


def _self_test() -> dict[str, object]:
    examples = {
        "parseLease": ["parselease", "parse", "lease"],
        "MAX_RETRY_MS": ["max_retry_ms", "max", "retry", "ms"],
        "tool-registry.js": ["tool-registry.js", "tool", "registry", "js"],
        "a.b.c": ["a.b.c", "a", "b", "c"],
    }
    for text, expected in examples.items():
        actual = tokenize_identifier_aware(text)
        if actual != expected:
            raise AssertionError(f"tokenizer {text!r}: expected {expected!r}, got {actual!r}")

    with tempfile.TemporaryDirectory(prefix="retrieval-index-selftest-") as temporary:
        root = Path(temporary) / "repo"
        root.mkdir()
        init = _run_git(root, ("init", "-q"))
        if init.returncode:
            raise AssertionError(init.stderr.strip())
        (root / "src").mkdir()
        (root / "src" / "lease.js").write_text("const parseLease = value => value;\n", encoding="utf-8")
        (root / "src" / "many-leases.js").write_text(
            "".join(f"const parseLease{i} = parseLease(value);\n" for i in range(400)),
            encoding="utf-8",
        )
        (root / ".hidden-source").write_text("hiddenSource is legitimate\n", encoding="utf-8")
        (root / ".env.local").write_text("NOT_A_REAL_SECRET=value\n", encoding="utf-8")
        (root / "credential.txt").write_text("ghp_" + "A" * 20 + "\n", encoding="utf-8")
        (root / "tracked-then-ignored.txt").write_text("must not enter index\n", encoding="utf-8")
        add = _run_git(root, ("add", "-f", "tracked-then-ignored.txt"))
        if add.returncode:
            raise AssertionError(add.stderr.strip())
        (root / ".gitignore").write_text("tracked-then-ignored.txt\nignored/\n", encoding="utf-8")
        (root / "ignored").mkdir()
        (root / "ignored" / "private.txt").write_text("ignored content\n", encoding="utf-8")

        database_path = Path(temporary) / "index.sqlite"
        stats = build_index(database_path, root)
        database = connect_index(database_path)
        try:
            indexed = {row[0] for row in database.execute("SELECT path FROM files")}
            if "src/lease.js" not in indexed or ".hidden-source" not in indexed:
                raise AssertionError(f"legitimate files missing: {sorted(indexed)}")
            forbidden = {".env.local", "credential.txt", "tracked-then-ignored.txt", "ignored/private.txt"}
            if indexed & forbidden:
                raise AssertionError(f"excluded files indexed: {sorted(indexed & forbidden)}")
            aware = query_index(database, "parse lease", "bm25", 10)
            legacy = query_index(database, "parse lease", "bm25_legacy", 10)
            exact = query_index(database, "parseLease", "ident_first", 10)
            path_boost = query_index(database, "lease.js", "bm25_pathboost", 10)
            restricted = query_index(
                database,
                "parseLease",
                "bm25",
                10,
                allowed_paths=[str(root / "src" / "lease.js")],
            )
            forbidden_restriction = query_index(
                database, "parseLease", "bm25", 10, allowed_paths=[".hidden-source"]
            )
            unique_top_two = query_index(database, "parseLease", "bm25", 2)
            if not aware or legacy or not exact or not path_boost or not restricted or forbidden_restriction:
                raise AssertionError(
                    "query smoke failed: "
                    f"aware={len(aware)}, legacy={len(legacy)}, exact={len(exact)}, "
                    f"path_boost={len(path_boost)}, restricted={len(restricted)}, "
                    f"forbidden_restriction={len(forbidden_restriction)}"
                )
            if len(unique_top_two) != 2 or len({item["path"] for item in unique_top_two}) != 2:
                raise AssertionError(f"top-K was crowded by one multi-chunk path: {unique_top_two!r}")
            first = aware[0]
            expected_keys = {
                "path", "start_byte", "end_byte", "hash", "start_line", "end_line", "text", "score"
            }
            if set(first) != expected_keys:
                raise AssertionError(f"public result keys changed: {sorted(first)}")
        finally:
            database.close()
        return {"ok": True, "tokenizer_examples": len(examples), "build": stats}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build a fresh FTS5 index")
    build.add_argument("--db", required=True, help="SQLite output path")
    build.add_argument("--root", required=True, help="Git worktree to index")
    build.add_argument("--logical-root", help="optional provenance label")
    stats = commands.add_parser("stats", help="print index metadata and counts")
    stats.add_argument("--db", required=True, help="SQLite index path")
    commands.add_parser("self-test", help="run tokenizer, exclusion, and query smoke tests")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "build":
        result = build_index(arguments.db, arguments.root, arguments.logical_root)
    elif arguments.command == "stats":
        result = index_stats(arguments.db)
    else:
        result = _self_test()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
