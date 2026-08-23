from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import incremental_index_v2 as incremental
import index as canonical


ARMS = ("bm25", "ident_first", "bm25_pathboost", "bm25_legacy")
QUERIES = (
    "alphaShared",
    "parseLease",
    "oldNeedle",
    "newNeedle",
    "deletedSymbol",
    "addedSymbol",
    "ignoredSymbol",
)


def git(repo: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=merged,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(
            f"git {' '.join(arguments)} failed ({completed.returncode}): "
            f"{completed.stderr or completed.stdout}"
        )
    return completed.stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.name", "Incremental Index Test")
    git(path, "config", "user.email", "incremental@example.invalid")
    git(path, "branch", "-M", "main")
    return path


def commit_all(repo: Path, message: str, timestamp: int) -> str:
    git(repo, "add", "-A")
    date = f"{timestamp} +0000"
    git(
        repo,
        "commit",
        "-m",
        message,
        env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )
    return git(repo, "rev-parse", "HEAD")


def make_three_commits(repo: Path) -> tuple[str, str, str]:
    (repo / "keep.txt").write_text(
        "alphaShared parseLease stableToken\nsecond line\nthird line\n",
        encoding="utf-8",
    )
    (repo / "change.txt").write_text(
        "oldNeedle alphaShared\nsecond line\nthird line\n",
        encoding="utf-8",
    )
    (repo / "delete.txt").write_text(
        "deletedSymbol alphaShared\nsecond line\nthird line\n",
        encoding="utf-8",
    )
    (repo / "ignored.txt").write_text(
        "ignoredSymbol alphaShared\nsecond line\nthird line\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    # Keep the ignored path in Git history so changing only .gitignore changes
    # canonical eligibility across snapshots.
    git(repo, "add", "-f", "ignored.txt")
    first = commit_all(repo, "first", 1_700_000_000)

    (repo / "change.txt").write_text(
        "newNeedle alphaShared parseLease\nsecond line changed\nthird line\n",
        encoding="utf-8",
    )
    (repo / "delete.txt").unlink()
    (repo / "added.txt").write_text(
        "addedSymbol alphaShared\nsecond line\nthird line\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("# ignored.txt is eligible now\n", encoding="utf-8")
    second = commit_all(repo, "second", 1_700_000_100)

    # The file remains tracked, but canonical check-ignore --no-index excludes
    # tracked ignored paths too.  Incremental refresh must delete its old rows.
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    third = commit_all(repo, "third", 1_700_000_200)
    return first, second, third


def checkout(repo: Path, commit: str) -> None:
    git(repo, "checkout", "--detach", "--force", commit)


def rows(database: sqlite3.Connection, sql: str) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in database.execute(sql).fetchall())


def stable_database_state(db_path: Path) -> dict[str, object]:
    database = canonical.connect_index(db_path)
    try:
        metadata = {
            str(row[0]): str(row[1])
            for row in database.execute("SELECT key, value FROM metadata")
            if row[0] in incremental.PROVENANCE_KEYS or row[0] in {"git_head", "build_complete"}
        }
        return {
            "metadata": metadata,
            "files": rows(
                database,
                """
                SELECT path, name, size_bytes, content_sha256, chunk_count
                FROM files ORDER BY path COLLATE BINARY
                """,
            ),
            "chunks": rows(
                database,
                """
                SELECT region_id, path, filename, start_byte, end_byte,
                       content_sha256, start_line, end_line, text,
                       body_tokens_json, legacy_tokens_json,
                       path_tokens_json, name_tokens_json
                FROM chunks
                ORDER BY path COLLATE BINARY, start_byte, end_byte, content_sha256
                """,
            ),
            "identifier_postings": rows(
                database,
                """
                SELECT p.token, c.path, c.start_byte, c.end_byte, c.content_sha256
                FROM ident_postings p
                JOIN chunks c ON c.internal_rowid = p.internal_rowid
                ORDER BY p.token COLLATE BINARY, c.path COLLATE BINARY,
                         c.start_byte, c.end_byte, c.content_sha256
                """,
            ),
            "aware_fts": rows(
                database,
                """
                SELECT c.path, c.start_byte, c.end_byte, c.content_sha256,
                       f.body_tokens, f.path_tokens, f.name_tokens
                FROM chunk_fts f
                JOIN chunks c ON c.internal_rowid = f.internal_rowid
                ORDER BY c.path COLLATE BINARY, c.start_byte, c.end_byte, c.content_sha256
                """,
            ),
            "legacy_fts": rows(
                database,
                """
                SELECT c.path, c.start_byte, c.end_byte, c.content_sha256,
                       f.body_tokens
                FROM chunk_fts_legacy f
                JOIN chunks c ON c.internal_rowid = f.internal_rowid
                ORDER BY c.path COLLATE BINARY, c.start_byte, c.end_byte, c.content_sha256
                """,
            ),
        }
    finally:
        database.close()


def query_rankings(db_path: Path) -> dict[str, dict[str, list[tuple[object, ...]]]]:
    database = canonical.connect_index(db_path)
    try:
        result: dict[str, dict[str, list[tuple[object, ...]]]] = {}
        for arm in ARMS:
            result[arm] = {}
            for query in QUERIES:
                ranked = canonical.query_index(database, query, arm, 20)
                result[arm][query] = [
                    (
                        row["path"],
                        row["start_byte"],
                        row["end_byte"],
                        row["hash"],
                        row["start_line"],
                        row["end_line"],
                        row["text"],
                        row["score"],
                    )
                    for row in ranked
                ]
        return result
    finally:
        database.close()


def assert_matches_fresh(
    repo: Path,
    incremental_db: Path,
    fresh_db: Path,
    logical_root: str,
) -> None:
    canonical.build_index(fresh_db, repo, logical_root=logical_root)
    assert stable_database_state(incremental_db) == stable_database_state(fresh_db)
    assert query_rankings(incremental_db) == query_rankings(fresh_db)


def test_incremental_refresh_is_canonically_equivalent_across_three_commits(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    first, second, third = make_three_commits(repo)
    incremental_db = tmp_path / "incremental.sqlite"
    logical_root = r"C:\logical\repo"

    checkout(repo, first)
    initial = incremental.refresh_index(incremental_db, repo, logical_root=logical_root)
    assert initial["mode"] == "full"
    assert_matches_fresh(repo, incremental_db, tmp_path / "fresh-first.sqlite", logical_root)
    first_paths = {row[0] for row in stable_database_state(incremental_db)["files"]}
    assert "ignored.txt" not in first_paths

    checkout(repo, second)
    updated = incremental.refresh_index(incremental_db, repo, logical_root=logical_root)
    assert updated["mode"] == "incremental"
    assert updated["files_added"] == 2  # added.txt and newly eligible ignored.txt
    assert updated["files_changed"] == 2  # change.txt and .gitignore
    assert updated["files_removed"] == 1  # delete.txt
    assert_matches_fresh(repo, incremental_db, tmp_path / "fresh-second.sqlite", logical_root)
    second_paths = {row[0] for row in stable_database_state(incremental_db)["files"]}
    assert {"added.txt", "ignored.txt"} <= second_paths
    assert "delete.txt" not in second_paths

    checkout(repo, third)
    reignored = incremental.refresh_index(incremental_db, repo, logical_root=logical_root)
    assert reignored["mode"] == "incremental"
    assert reignored["files_added"] == 0
    assert reignored["files_changed"] == 1  # .gitignore
    assert reignored["files_removed"] == 1  # ignored.txt becomes ineligible
    assert_matches_fresh(repo, incremental_db, tmp_path / "fresh-third.sqlite", logical_root)
    third_paths = {row[0] for row in stable_database_state(incremental_db)["files"]}
    assert "ignored.txt" not in third_paths


def test_incremental_failure_rolls_back_to_last_complete_state(tmp_path: Path, monkeypatch):
    repo = init_repo(tmp_path / "repo")
    first, second, _ = make_three_commits(repo)
    database_path = tmp_path / "incremental.sqlite"
    logical_root = r"C:\logical\repo"

    checkout(repo, first)
    incremental.refresh_index(database_path, repo, logical_root=logical_root)
    before = stable_database_state(database_path)

    checkout(repo, second)

    def fail_insert(*_args, **_kwargs):
        raise RuntimeError("synthetic insertion failure")

    monkeypatch.setattr(incremental, "_insert_file", fail_insert)
    with pytest.raises(RuntimeError, match="synthetic insertion failure"):
        incremental.refresh_index(database_path, repo, logical_root=logical_root)

    assert stable_database_state(database_path) == before
    metadata = canonical.index_stats(database_path)
    assert metadata["build_complete"] == "1"


def test_refresh_rejects_symlink_source_even_when_database_is_compatible(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    make_three_commits(repo)
    database_path = tmp_path / "incremental.sqlite"
    incremental.refresh_index(database_path, repo, logical_root=r"C:\logical\repo")
    linked = tmp_path / "linked-repo"
    try:
        linked.symlink_to(repo, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable on this host: {error}")
    with pytest.raises(ValueError, match="symlink or reparse point"):
        incremental.refresh_index(database_path, linked, logical_root=r"C:\logical\repo")
