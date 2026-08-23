from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import history_v2 as history
from provenance_v2 import TreeSpec


BASE_TS = 1_700_000_000


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
    git(path, "config", "user.name", "History V2 Test")
    git(path, "config", "user.email", "history-v2@example.invalid")
    git(path, "branch", "-M", "main")
    return path


def commit_state(repo: Path, name: str, timestamp: int) -> str:
    (repo / "state.txt").write_text(name + "\n", encoding="utf-8")
    git(repo, "add", "-A")
    date = f"{timestamp} +0000"
    git(
        repo,
        "commit",
        "-m",
        name,
        env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )
    return git(repo, "rev-parse", "HEAD")


def tree(
    tree_id: str,
    repository: Path,
    *,
    subtree: str = "",
    identity: str | None = None,
) -> TreeSpec:
    return TreeSpec(
        tree_id=tree_id,
        logical_root=rf"C:\logical\{tree_id}",
        repository_root=str(repository),
        repository_identity=identity or str((repository / ".git").resolve()),
        repository_relative_root=subtree,
    )


def test_cached_selection_avoids_repeating_git_and_keeps_topological_tie_break(
    tmp_path: Path, monkeypatch
):
    repo = init_repo(tmp_path / "repo")
    commit_state(repo, "first", BASE_TS)
    tied_parent = commit_state(repo, "tied-parent", BASE_TS + 100)
    tied_tip = commit_state(repo, "tied-tip", BASE_TS + 100)
    source = tree("source", repo)

    calls: list[tuple[str, tuple[str, ...]]] = []
    original = history._run_git

    def counted(repository, arguments, **kwargs):
        calls.append((os.fspath(repository), tuple(arguments)))
        return original(repository, arguments, **kwargs)

    monkeypatch.setattr(history, "_run_git", counted)
    cache = history.GitHistoryCache()

    selected, resolution = cache.select(source, "main", BASE_TS + 100)
    assert resolution.resolved_ref == "refs/heads/main"
    assert selected is not None
    assert selected.commit == tied_tip
    assert selected.commit != tied_parent
    assert selected.gap_seconds == 0
    calls_after_first_selection = len(calls)
    assert calls_after_first_selection > 0

    repeated, repeated_resolution = cache.select(source, "main", BASE_TS + 100)
    earlier, _ = cache.select(source, "main", BASE_TS + 50)
    assert repeated == selected
    assert repeated_resolution == resolution
    assert earlier is not None and earlier.commit_ts == BASE_TS
    assert len(calls) == calls_after_first_selection


def test_candidate_epoch_is_ineligible_until_its_subtree_exists(tmp_path: Path):
    old_repo = init_repo(tmp_path / "old-epoch")
    old_commit = commit_state(old_repo, "old", BASE_TS)

    new_repo = init_repo(tmp_path / "new-epoch")
    commit_state(new_repo, "before-subtree", BASE_TS + 50)
    (new_repo / "packages" / "engine").mkdir(parents=True)
    (new_repo / "packages" / "engine" / "engine.txt").write_text(
        "new epoch\n", encoding="utf-8"
    )
    new_commit = commit_state(new_repo, "subtree-arrives", BASE_TS + 250)

    old = tree("engine-old", old_repo)
    new = tree("engine-new", new_repo, subtree="packages/engine")
    cache = history.GitHistoryCache()

    before_arrival, resolutions = cache.choose_at_or_before(
        [old, new], "main", BASE_TS + 200
    )
    assert before_arrival is not None
    assert before_arrival.tree_id == "engine-old"
    assert before_arrival.commit == old_commit
    assert resolutions["engine-new"].resolved_ref == "refs/heads/main"
    new_before_arrival, _ = cache.select(new, "main", BASE_TS + 200)
    assert new_before_arrival is None

    after_arrival, _ = cache.choose_at_or_before([old, new], "main", BASE_TS + 300)
    assert after_arrival is not None
    assert after_arrival.tree_id == "engine-new"
    assert after_arrival.commit == new_commit
    assert after_arrival.gap_seconds == 50


def test_branch_resolution_prefers_local_and_deduplicates_same_tip_remotes(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    first = commit_state(repo, "first", BASE_TS)
    second = commit_state(repo, "second", BASE_TS + 100)
    source = tree("source", repo)
    cache = history.GitHistoryCache()

    git(repo, "branch", "shared", first)
    git(repo, "update-ref", "refs/remotes/backup/shared", second)
    git(repo, "update-ref", "refs/remotes/origin/shared", second)
    local = cache.resolve_branch(source, "shared")
    assert local.resolved_ref == "refs/heads/shared"
    assert local.ref_kind == "local"
    assert local.candidates == ("refs/heads/shared",)

    git(repo, "update-ref", "refs/remotes/backup/remote-only", second)
    git(repo, "update-ref", "refs/remotes/origin/remote-only", second)
    remote = cache.resolve_branch(source, "remote-only")
    assert remote.resolved_ref == "refs/remotes/backup/remote-only"
    assert remote.ref_kind == "remote"
    assert remote.candidates == (
        "refs/remotes/backup/remote-only",
        "refs/remotes/origin/remote-only",
    )

    git(repo, "update-ref", "refs/remotes/backup/divergent", first)
    git(repo, "update-ref", "refs/remotes/origin/divergent", second)
    divergent = cache.resolve_branch(source, "divergent")
    assert divergent.resolved_ref is None
    assert divergent.reason == "ambiguous_remote_branch"


def test_missing_branch_and_no_prior_commit_are_distinct(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    commit_state(repo, "first", BASE_TS)
    source = tree("source", repo)
    cache = history.GitHistoryCache()

    missing, missing_resolution = cache.select(source, "does-not-exist", BASE_TS + 100)
    assert missing is None
    assert missing_resolution.resolved_ref is None
    assert missing_resolution.reason == "branch_missing"

    no_prior, valid_resolution = cache.select(source, "main", BASE_TS - 1)
    assert no_prior is None
    assert valid_resolution.resolved_ref == "refs/heads/main"
    assert valid_resolution.reason is None


def test_head_cache_is_scoped_to_each_linked_worktree(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    first = commit_state(repo, "first", BASE_TS)
    second = commit_state(repo, "second", BASE_TS + 100)
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "--detach", str(linked), first)
    common_identity = str((repo / ".git").resolve())
    primary_tree = tree("primary", repo, identity=common_identity)
    linked_tree = tree("linked", linked, identity=common_identity)

    try:
        cache = history.GitHistoryCache()
        primary = cache.head(primary_tree, BASE_TS + 200)
        historical = cache.head(linked_tree, BASE_TS + 200)
        assert primary is not None and primary.commit == second
        assert historical is not None and historical.commit == first
        assert primary.repository_identity == historical.repository_identity
        assert primary.gap_seconds == 100
        assert historical.gap_seconds == 200

        # Both values remain independently cached even though the worktrees
        # deliberately share the same common-repository identity.
        assert cache.head(primary_tree, BASE_TS + 300).commit == second
        assert cache.head(linked_tree, BASE_TS + 300).commit == first
    finally:
        git(repo, "worktree", "remove", "--force", str(linked))
