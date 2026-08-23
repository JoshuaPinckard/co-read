from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import provenance_v2 as provenance


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
    git(path, "config", "user.name", "Retrieval Test")
    git(path, "config", "user.email", "retrieval@example.invalid")
    git(path, "branch", "-M", "main")
    return path


def commit(repo: Path, name: str, timestamp: int) -> str:
    tracked = repo / "state.txt"
    tracked.write_text(name + "\n", encoding="utf-8")
    git(repo, "add", "state.txt")
    date = f"{timestamp} +0000"
    git(
        repo,
        "commit",
        "-m",
        name,
        env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )
    return git(repo, "rev-parse", "HEAD")


def record(
    *,
    cwd: str = r"C:\repo",
    path: str | None = None,
    branch: str | None = "main",
    timestamp: float | None = 1_700_000_100.0,
) -> dict[str, object]:
    query: dict[str, object] = {"pattern": "needle"}
    if path is not None:
        query["path"] = path
    value: dict[str, object] = {"id": "session:query", "cwd": cwd, "query": query}
    if branch is not None:
        value["git_branch"] = branch
    if timestamp is not None:
        value["ts"] = timestamp
    return value


def test_effective_scope_is_windows_and_msys_aware():
    assert provenance.effective_scope(r"C:\Repo\src", None) == r"c:\repo\src"
    assert provenance.effective_scope(r"C:\Repo\src", r"..\tests") == r"c:\repo\tests"
    assert provenance.effective_scope(r"C:\ignored", r"/c/Users/Joshp/Repo/src") == (
        r"c:\users\joshp\repo\src"
    )
    assert provenance.effective_scope(r"C:\ignored", r"\c\Users\Joshp\Repo\src") == (
        r"c:\users\joshp\repo\src"
    )
    assert provenance.effective_scope(None, "relative/path") is None


def test_effective_scope_for_record_treats_absolute_query_path_as_authoritative():
    value = record(cwd=r"C:\cwd-tree\sub", path=r"C:\target-tree\src")
    assert provenance.effective_scope_for_record(value) == r"c:\target-tree\src"
    assert provenance.effective_scope_for_record(record(cwd=r"C:\cwd-tree\sub")) == (
        r"c:\cwd-tree\sub"
    )


def test_longest_tree_match_is_component_safe_and_deterministic():
    trees = [
        provenance.TreeSpec("repo", r"C:\repo"),
        provenance.TreeSpec("nested-z", r"C:\repo\nested"),
        provenance.TreeSpec("nested-a", r"c:\REPO\NESTED"),
        provenance.TreeSpec("other", r"C:\repository"),
    ]
    assert provenance.longest_logical_tree_match(r"C:\repo\nested\src\x.js", trees).tree_id == (
        "nested-a"
    )
    assert provenance.longest_logical_tree_match(r"C:\repository\x.js", trees).tree_id == "other"
    assert provenance.longest_logical_tree_match(r"C:\repo-suffix\x.js", trees) is None


def test_tree_spec_json_round_trip_and_safe_subtree_validation():
    tree = provenance.TreeSpec(
        tree_id="engine",
        logical_root=r"C:\logical\engine",
        repository_root=r"C:\physical\checkout",
        repository_identity=r"C:\physical\.git",
        repository_relative_root=r"packages\engine",
        current_root=r"C:\current\engine",
        note="synthetic",
    )
    assert provenance.TreeSpec.from_dict(tree.to_dict()) == tree
    assert provenance.trees_from_json(provenance.trees_to_json([tree])) == [tree]

    try:
        provenance.TreeSpec("unsafe", r"C:\logical", repository_relative_root="../escape")
    except ValueError as error:
        assert "safe relative path" in str(error)
    else:
        raise AssertionError("unsafe repository-relative root was accepted")


def test_repository_identity_comparison_is_explicit():
    first = provenance.TreeSpec(
        "first", r"C:\first", repository_root=r"C:\worktree-one", repository_identity="repo-id"
    )
    alias = provenance.TreeSpec(
        "alias", r"C:\alias", repository_root=r"C:\worktree-two", repository_identity="REPO-ID"
    )
    other = provenance.TreeSpec(
        "other", r"C:\other", repository_root=r"C:\worktree-three", repository_identity="other-id"
    )
    unknown = provenance.TreeSpec("unknown", r"C:\unknown")
    assert provenance.compare_repository_identity(first, alias) is provenance.RepositoryRelation.SAME
    assert provenance.compare_repository_identity(first, other) is provenance.RepositoryRelation.DIFFERENT
    assert provenance.compare_repository_identity(first, unknown) is provenance.RepositoryRelation.UNKNOWN


def test_branch_resolution_prefers_exact_local_then_unique_remote(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    first = commit(repo, "first", 1_700_000_000)
    second = commit(repo, "second", 1_700_000_100)
    git(repo, "branch", "shared", first)
    git(repo, "update-ref", "refs/remotes/origin/shared", second)
    local = provenance.resolve_branch(repo, "shared")
    assert local.resolved_ref == "refs/heads/shared"
    assert local.ref_kind == "local"

    git(repo, "update-ref", "refs/remotes/origin/remote-only", first)
    remote = provenance.resolve_branch(repo, "remote-only")
    assert remote.resolved_ref == "refs/remotes/origin/remote-only"
    assert remote.ref_kind == "remote"

    git(repo, "update-ref", "refs/remotes/backup/ambiguous", first)
    git(repo, "update-ref", "refs/remotes/origin/ambiguous", second)
    ambiguous = provenance.resolve_branch(repo, "ambiguous")
    assert ambiguous.resolved_ref is None
    assert ambiguous.reason == "ambiguous_remote_branch"
    assert ambiguous.candidates == (
        "refs/remotes/backup/ambiguous",
        "refs/remotes/origin/ambiguous",
    )


def test_closest_commit_uses_committer_time_and_topological_tie_order(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    first = commit(repo, "first", 1_700_000_000)
    tied_parent = commit(repo, "tied parent", 1_700_000_100)
    tied_tip = commit(repo, "tied tip", 1_700_000_100)
    future = commit(repo, "future", 1_700_000_200)

    before_tie = provenance.closest_commit_at_or_before(repo, "refs/heads/main", 1_700_000_050)
    assert before_tie is not None and before_tie.commit == first
    assert before_tie.gap_seconds == 50

    at_tie = provenance.closest_commit_at_or_before(repo, "refs/heads/main", 1_700_000_100)
    assert at_tie is not None and at_tie.commit == tied_tip
    assert at_tie.commit != tied_parent
    assert at_tie.commit != future
    assert at_tie.gap_seconds == 0
    assert at_tie.ancestry_order == 1  # future is first and excluded by time


def test_reconstruct_query_exact_and_fallback_modes(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    first = commit(repo, "first", 1_700_000_000)
    head = commit(repo, "head", 1_700_000_200)
    identity = str((repo / ".git").resolve())
    main_tree = provenance.TreeSpec(
        "main-tree",
        r"C:\repo",
        repository_root=str(repo),
        repository_identity=identity,
    )

    exact = provenance.reconstruct_query(
        record(timestamp=1_700_000_050),
        [main_tree],
    )
    assert exact.mode is provenance.ReconstructionMode.BRANCH_AT_OR_BEFORE
    assert exact.exact is True
    assert exact.commit == first
    assert exact.commit_ts == 1_700_000_000
    assert exact.gap_seconds == 50
    assert exact.resolved_ref == "refs/heads/main"
    assert exact.to_dict()["mode"] == "branch_at_or_before"

    branchless = provenance.reconstruct_query(
        record(branch="HEAD", timestamp=1_700_000_250), [main_tree]
    )
    assert branchless.mode is provenance.ReconstructionMode.FALLBACK_HEAD_BRANCHLESS
    assert branchless.commit == head
    assert branchless.gap_seconds == 50

    missing = provenance.reconstruct_query(record(branch="deleted"), [main_tree])
    assert missing.mode is provenance.ReconstructionMode.FALLBACK_BRANCH_MISSING
    assert missing.commit == head
    assert missing.reason == "branch_missing"

    no_prior = provenance.reconstruct_query(
        record(timestamp=1_699_999_999), [main_tree]
    )
    assert no_prior.mode is provenance.ReconstructionMode.FALLBACK_NO_PRIOR_COMMIT
    assert no_prior.commit == head
    assert no_prior.resolved_ref == "refs/heads/main"
    assert no_prior.gap_seconds == -201

    invalid_time = provenance.reconstruct_query(record(timestamp=None), [main_tree])
    assert invalid_time.mode is provenance.ReconstructionMode.FALLBACK_INVALID_TIMESTAMP
    assert invalid_time.commit == head
    assert invalid_time.gap_seconds is None


def test_reconstruct_query_rejects_cross_repo_branch_provenance(tmp_path: Path):
    cwd_repo = init_repo(tmp_path / "cwd-repo")
    target_repo = init_repo(tmp_path / "target-repo")
    commit(cwd_repo, "cwd", 1_700_000_000)
    target_head = commit(target_repo, "target", 1_700_000_000)
    trees = [
        provenance.TreeSpec(
            "cwd",
            r"C:\cwd-tree",
            repository_root=str(cwd_repo),
            repository_identity="cwd-repository",
        ),
        provenance.TreeSpec(
            "target",
            r"C:\target-tree",
            repository_root=str(target_repo),
            repository_identity="target-repository",
        ),
    ]
    selected = provenance.reconstruct_query(
        record(cwd=r"C:\cwd-tree", path=r"C:\target-tree\src", timestamp=1_700_000_100),
        trees,
    )
    assert selected.mode is provenance.ReconstructionMode.FALLBACK_CROSS_REPOSITORY
    assert selected.exact is False
    assert selected.commit == target_head
    assert selected.reason == "cwd_target_repository_mismatch"


def test_reconstruct_query_allows_distinct_trees_with_same_repository_identity(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    first = commit(repo, "first", 1_700_000_000)
    trees = [
        provenance.TreeSpec(
            "cwd",
            r"C:\cwd-alias",
            repository_root=str(repo),
            repository_identity="shared",
        ),
        provenance.TreeSpec(
            "target",
            r"C:\target-alias",
            repository_root=str(repo),
            repository_identity="shared",
        ),
    ]
    query = record(cwd=r"C:\cwd-alias", path=r"C:\target-alias", timestamp=None)
    query["timestamp"] = "2023-11-14T22:13:21Z"  # 1_700_000_001 UTC
    selected = provenance.reconstruct_query(query, trees)
    assert selected.mode is provenance.ReconstructionMode.BRANCH_AT_OR_BEFORE
    assert selected.commit == first


def test_reconstruct_query_non_git_and_unavailable_are_explicit(tmp_path: Path):
    non_git = tmp_path / "ordinary"
    non_git.mkdir()
    non_git_tree = provenance.TreeSpec(
        "ordinary", r"C:\ordinary", repository_root=str(non_git), repository_identity="ordinary"
    )
    selected = provenance.reconstruct_query(
        record(cwd=r"C:\ordinary", path=r"C:\ordinary"), [non_git_tree]
    )
    assert selected.mode is provenance.ReconstructionMode.FALLBACK_NON_GIT
    assert selected.commit is None

    unavailable_tree = provenance.TreeSpec("gone", r"C:\gone", available=False)
    unavailable = provenance.reconstruct_query(
        record(cwd=r"C:\gone", path=r"C:\gone"), [unavailable_tree]
    )
    assert unavailable.mode is provenance.ReconstructionMode.UNAVAILABLE_TREE
    assert unavailable.reason == "tree_marked_unavailable"

    unmatched = provenance.reconstruct_query(
        record(cwd=r"C:\elsewhere", path=r"C:\elsewhere"), [unavailable_tree]
    )
    assert unmatched.mode is provenance.ReconstructionMode.UNAVAILABLE_TREE
    assert unmatched.reason == "no_logical_tree_match"
