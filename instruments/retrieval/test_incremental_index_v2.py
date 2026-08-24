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


def test_force_full_replaces_an_existing_unrelated_repository_index(tmp_path: Path):
    first = init_repo(tmp_path / "first")
    (first / "first.txt").write_text("firstNeedle\n", encoding="utf-8")
    commit_all(first, "first", 1_700_000_000)
    second = init_repo(tmp_path / "second")
    (second / "second.txt").write_text("secondNeedle\n", encoding="utf-8")
    commit_all(second, "second", 1_700_000_100)
    database_path = tmp_path / "active.sqlite"

    incremental.refresh_index(database_path, first, logical_root=r"C:\logical\first")
    rebuilt = incremental.refresh_index(
        database_path,
        second,
        logical_root=r"C:\logical\second",
        force_full=True,
    )

    assert rebuilt["mode"] == "full"
    database = canonical.connect_index(database_path)
    try:
        assert [row[0] for row in database.execute("SELECT path FROM files ORDER BY path")] == [
            "second.txt"
        ]
    finally:
        database.close()


def test_incomplete_database_is_discarded_before_full_rebuild(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    (repo / "state.txt").write_text("needle\n", encoding="utf-8")
    commit_all(repo, "state", 1_700_000_000)
    database_path = tmp_path / "active.sqlite"
    incremental.refresh_index(database_path, repo, logical_root=r"C:\logical\repo")
    database = sqlite3.connect(database_path)
    try:
        database.execute("UPDATE metadata SET value = '0' WHERE key = 'build_complete'")
        database.commit()
    finally:
        database.close()

    rebuilt = incremental.refresh_index(
        database_path, repo, logical_root=r"C:\logical\repo"
    )
    assert rebuilt["mode"] == "full"
    completed = canonical.connect_index(database_path)
    completed.close()


def test_git_delta_reads_only_changed_files_and_skips_per_state_fts_optimize(
    tmp_path: Path, monkeypatch
):
    repo = init_repo(tmp_path / "repo")
    (repo / "stable.txt").write_text(
        "stableNeedle alphaShared\nsecond line\nthird line\n", encoding="utf-8"
    )
    changing = repo / "changing.txt"
    changing.write_text(
        "oldNeedle alphaShared\nsecond line\nthird line\n", encoding="utf-8"
    )
    first = commit_all(repo, "first", 1_700_000_000)
    database_path = tmp_path / "incremental.sqlite"
    logical_root = r"C:\logical\repo"
    initial = incremental.refresh_index(
        database_path,
        repo,
        logical_root=logical_root,
        expected_commit=first,
        stream_identity="fixture-stream",
        repository_relative_root="",
    )
    assert initial["mode"] == "full"

    changing.write_text(
        "newNeedle alphaShared\nsecond line changed\nthird line\n", encoding="utf-8"
    )
    second = commit_all(repo, "second", 1_700_000_100)

    def forbid_full_snapshot(*_args, **_kwargs):
        raise AssertionError("ordinary commit unexpectedly audited the full snapshot")

    monkeypatch.setattr(incremental, "_eligible_files", forbid_full_snapshot)
    statements: list[str] = []
    original_connect = canonical.connect_index

    def traced_connect(path):
        connection = original_connect(path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(canonical, "connect_index", traced_connect)
    updated = incremental.refresh_index(
        database_path,
        repo,
        logical_root=logical_root,
        expected_commit=second,
        stream_identity="fixture-stream",
        repository_relative_root="",
    )
    assert updated["mode"] == "incremental"
    assert updated["audit_mode"] == "git_delta"
    assert updated["changed_git_paths"] == 1
    assert updated["files_changed"] == 1
    assert updated["canonical_stats"]["delta_files_read"] == 1
    assert updated["fts_maintenance_policy"] == incremental.FTS_MAINTENANCE_POLICY
    assert not any(
        "insert into chunk_fts(chunk_fts) values ('optimize')" in sql.casefold()
        or "insert into chunk_fts_legacy(chunk_fts_legacy) values ('optimize')"
        in sql.casefold()
        for sql in statements
    )

    # Restore the protected connection helper before building the independent
    # fresh oracle; the wrapper itself remains the only implementation changed.
    monkeypatch.setattr(canonical, "connect_index", original_connect)
    assert_matches_fresh(repo, database_path, tmp_path / "fresh.sqlite", logical_root)


def test_git_delta_is_exact_between_non_ancestor_commits(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    (repo / "base.txt").write_text(
        "baseNeedle alphaShared\nsecond line\nthird line\n", encoding="utf-8"
    )
    base = commit_all(repo, "base", 1_700_000_000)
    git(repo, "checkout", "-b", "left")
    (repo / "left.txt").write_text(
        "oldNeedle alphaShared\nsecond line\nthird line\n", encoding="utf-8"
    )
    left = commit_all(repo, "left", 1_700_000_100)
    database_path = tmp_path / "incremental.sqlite"
    logical_root = r"C:\logical\repo"
    incremental.refresh_index(
        database_path,
        repo,
        logical_root=logical_root,
        expected_commit=left,
        stream_identity="divergent-stream",
        repository_relative_root="",
    )

    git(repo, "checkout", "-b", "right", base)
    (repo / "right.txt").write_text(
        "newNeedle alphaShared\nsecond line\nthird line\n", encoding="utf-8"
    )
    right = commit_all(repo, "right", 1_700_000_200)
    updated = incremental.refresh_index(
        database_path,
        repo,
        logical_root=logical_root,
        expected_commit=right,
        stream_identity="divergent-stream",
        repository_relative_root="",
    )
    assert updated["audit_mode"] == "git_delta"
    assert updated["from_commit"] == left
    assert updated["to_commit"] == right
    assert updated["files_added"] == 1
    assert updated["files_removed"] == 1
    assert_matches_fresh(repo, database_path, tmp_path / "fresh.sqlite", logical_root)


@pytest.mark.parametrize("control_name", [".gitignore", ".gitattributes"])
def test_ancestor_control_change_forces_exact_full_snapshot_audit(
    tmp_path: Path, control_name: str
):
    repo = init_repo(tmp_path / "repo")
    source = repo / "src"
    source.mkdir()
    (source / "keep.txt").write_text(
        "stableNeedle alphaShared\nsecond line\nthird line\n", encoding="utf-8"
    )
    (source / "hidden.txt").write_text(
        "ignoredSymbol alphaShared\nsecond line\nthird line\n", encoding="utf-8"
    )
    first = commit_all(repo, "first", 1_700_000_000)
    database_path = tmp_path / "incremental.sqlite"
    logical_root = r"C:\logical\repo\src"
    incremental.refresh_index(
        database_path,
        source,
        logical_root=logical_root,
        expected_commit=first,
        stream_identity="subtree-stream",
        repository_relative_root="src",
    )

    if control_name == ".gitignore":
        (repo / control_name).write_text("src/hidden.txt\n", encoding="utf-8")
    else:
        (repo / control_name).write_text("src/*.txt text eol=lf\n", encoding="utf-8")
    second = commit_all(repo, "control", 1_700_000_100)
    updated = incremental.refresh_index(
        database_path,
        source,
        logical_root=logical_root,
        expected_commit=second,
        stream_identity="subtree-stream",
        repository_relative_root="src",
    )
    assert updated["mode"] == "incremental"
    assert updated["audit_mode"] == "full_snapshot"
    assert updated["fallback_reason"] == f"applicable_control_changed:{control_name}"
    if control_name == ".gitignore":
        assert updated["files_removed"] == 1
    assert_matches_fresh(source, database_path, tmp_path / "fresh.sqlite", logical_root)


def test_expected_commit_mismatch_fails_before_mutating_completed_index(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    target = repo / "state.txt"
    target.write_text("oldNeedle\nsecond line\nthird line\n", encoding="utf-8")
    first = commit_all(repo, "first", 1_700_000_000)
    database_path = tmp_path / "incremental.sqlite"
    incremental.refresh_index(
        database_path,
        repo,
        logical_root=r"C:\logical\repo",
        expected_commit=first,
        stream_identity="expected-stream",
        repository_relative_root="",
    )
    before = stable_database_state(database_path)
    target.write_text("newNeedle\nsecond line\nthird line\n", encoding="utf-8")
    commit_all(repo, "second", 1_700_000_100)

    with pytest.raises(RuntimeError, match="expected historical commit"):
        incremental.refresh_index(
            database_path,
            repo,
            logical_root=r"C:\logical\repo",
            expected_commit=first,
            stream_identity="expected-stream",
            repository_relative_root="",
        )
    assert stable_database_state(database_path) == before


def test_dirty_historical_worktree_is_rejected_before_indexing(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    target = repo / "state.txt"
    target.write_text("committedNeedle\nsecond line\nthird line\n", encoding="utf-8")
    commit = commit_all(repo, "state", 1_700_000_000)
    target.write_text("dirtyNeedle\nsecond line\nthird line\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="dirty historical worktree"):
        incremental.refresh_index(
            tmp_path / "dirty.sqlite",
            repo,
            logical_root=r"C:\logical\repo",
            expected_commit=commit,
            stream_identity="dirty-stream",
            repository_relative_root="",
        )
    assert not (tmp_path / "dirty.sqlite").exists()


def test_stream_identity_mismatch_forces_canonical_rebuild(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    (repo / "state.txt").write_text(
        "stableNeedle\nsecond line\nthird line\n", encoding="utf-8"
    )
    commit = commit_all(repo, "state", 1_700_000_000)
    database_path = tmp_path / "incremental.sqlite"
    incremental.refresh_index(
        database_path,
        repo,
        logical_root=r"C:\logical\repo",
        expected_commit=commit,
        stream_identity="first-stream",
        repository_relative_root="",
    )
    rebuilt = incremental.refresh_index(
        database_path,
        repo,
        logical_root=r"C:\logical\repo",
        expected_commit=commit,
        stream_identity="second-stream",
        repository_relative_root="",
    )
    assert rebuilt["mode"] == "full"
    assert rebuilt["fallback_reason"] == "stream_metadata_mismatch"


def test_policy_change_forces_full_snapshot_audit(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    (repo / "state.txt").write_text(
        "stableNeedle\nsecond line\nthird line\n", encoding="utf-8"
    )
    commit = commit_all(repo, "state", 1_700_000_000)
    database_path = tmp_path / "incremental.sqlite"
    incremental.refresh_index(
        database_path,
        repo,
        logical_root=r"C:\logical\repo",
        expected_commit=commit,
        stream_identity="policy-stream",
        repository_relative_root="",
    )
    git(repo, "config", "benchmark.synthetic-policy", "changed")
    updated = incremental.refresh_index(
        database_path,
        repo,
        logical_root=r"C:\logical\repo",
        expected_commit=commit,
        stream_identity="policy-stream",
        repository_relative_root="",
    )
    assert updated["mode"] == "incremental"
    assert updated["audit_mode"] == "full_snapshot"
    assert updated["fallback_reason"] == "ignore_or_checkout_policy_changed"
def test_git_queries_enable_longpaths_and_honor_timeout(monkeypatch, tmp_path):
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(incremental.subprocess, "run", fake_run)
    assert incremental._git(tmp_path, ("status", "--short"), timeout=17) == "ok\n"
    assert observed["argv"][:5] == [
        "git",
        "-c",
        "core.longpaths=true",
        "-C",
        str(tmp_path),
    ]
    assert observed["timeout"] == 17


def test_longpath_environment_is_scoped(monkeypatch):
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("GIT_CONFIG_KEY_0", raising=False)
    monkeypatch.delenv("GIT_CONFIG_VALUE_0", raising=False)
    with incremental._longpath_git_environment():
        assert os.environ["GIT_CONFIG_COUNT"] == "1"
        assert os.environ["GIT_CONFIG_KEY_0"] == "core.longpaths"
        assert os.environ["GIT_CONFIG_VALUE_0"] == "true"
    assert "GIT_CONFIG_COUNT" not in os.environ
    assert "GIT_CONFIG_KEY_0" not in os.environ
    assert "GIT_CONFIG_VALUE_0" not in os.environ
