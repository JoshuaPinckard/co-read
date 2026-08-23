"""Cached Git-history selection for the retrieval V2 runner.

``provenance_v2`` provides the small, independently tested primitives.  The
full benchmark has thousands of queries but only a handful of repositories and
refs, so invoking ``git rev-list`` once per record would dominate the run.  This
module caches ref histories, filters them by an optional repository subtree,
and supports logical trees whose backing repository changed over time.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
import os
from pathlib import Path
import posixpath
import subprocess
from typing import Any, Iterable, Sequence

try:
    from .provenance_v2 import BranchResolution, TreeSpec
except ImportError:  # pragma: no cover - direct runner import path
    from provenance_v2 import BranchResolution, TreeSpec


@dataclass(frozen=True, slots=True)
class HistoricalSelection:
    tree_id: str
    repository_root: str
    repository_identity: str
    repository_relative_root: str
    requested_branch: str
    resolved_ref: str
    ref_kind: str
    commit: str
    commit_ts: int
    gap_seconds: float
    ancestry_order: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "tree_id": self.tree_id,
            "repository_root": self.repository_root,
            "repository_identity": self.repository_identity,
            "repository_relative_root": self.repository_relative_root,
            "requested_branch": self.requested_branch,
            "resolved_ref": self.resolved_ref,
            "ref_kind": self.ref_kind,
            "commit": self.commit,
            "commit_ts": self.commit_ts,
            "gap_seconds": self.gap_seconds,
            "ancestry_order": self.ancestry_order,
        }


@dataclass(frozen=True, slots=True)
class HeadSelection:
    tree_id: str
    repository_root: str
    repository_identity: str
    repository_relative_root: str
    commit: str
    commit_ts: int
    gap_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tree_id": self.tree_id,
            "repository_root": self.repository_root,
            "repository_identity": self.repository_identity,
            "repository_relative_root": self.repository_relative_root,
            "commit": self.commit,
            "commit_ts": self.commit_ts,
            "gap_seconds": self.gap_seconds,
        }


class HistoryError(RuntimeError):
    pass


def _run_git(
    repository: str | os.PathLike[str],
    arguments: Sequence[str],
    *,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repository), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HistoryError(f"git invocation failed: {error}") from error
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise HistoryError(f"git {' '.join(arguments)} failed: {detail}")
    return completed


def _safe_subtree(value: str) -> str:
    rendered = value.replace("\\", "/")
    rendered = posixpath.normpath(rendered) if rendered else ""
    if rendered in ("", "."):
        return ""
    if rendered == ".." or rendered.startswith("../") or posixpath.isabs(rendered):
        raise ValueError(f"unsafe repository subtree: {value!r}")
    return rendered


class GitHistoryCache:
    """Memoized branch, ancestry, subtree, and HEAD queries."""

    def __init__(self, *, timeout: float = 120.0) -> None:
        self.timeout = timeout
        self._branch_cache: dict[tuple[str, str], BranchResolution] = {}
        self._history_cache: dict[tuple[str, str], tuple[tuple[int, int, str], ...]] = {}
        self._eligible_history_cache: dict[
            tuple[str, str, str], tuple[tuple[tuple[int, int], int, int, str], ...]
        ] = {}
        self._subtree_cache: dict[tuple[str, str, str], bool] = {}
        self._head_cache: dict[tuple[str, str], tuple[str, int] | None] = {}

    @staticmethod
    def repository_key(tree: TreeSpec) -> str:
        value = tree.repository_identity or tree.repository_root
        if not value:
            return ""
        try:
            return os.path.normcase(os.path.normpath(str(Path(value).resolve(strict=False))))
        except OSError:
            return os.path.normcase(os.path.normpath(str(value)))

    def resolve_branch(self, tree: TreeSpec, branch: str) -> BranchResolution:
        if not tree.repository_root:
            return BranchResolution(branch, None, None, reason="repository_missing")
        requested = str(branch or "").strip()
        key = (self.repository_key(tree), requested)
        if key in self._branch_cache:
            return self._branch_cache[key]
        if not requested or requested.upper() == "HEAD":
            result = BranchResolution(requested, None, None, reason="branchless_or_invalid")
            self._branch_cache[key] = result
            return result

        short = requested
        if short.startswith("refs/heads/"):
            short = short[len("refs/heads/") :]
        if short.startswith("refs/remotes/"):
            direct = _run_git(
                tree.repository_root,
                ("show-ref", "--verify", "--quiet", short),
                timeout=self.timeout,
                check=False,
            )
            result = (
                BranchResolution(requested, short, "remote", (short,))
                if direct.returncode == 0
                else BranchResolution(requested, None, None, reason="branch_missing")
            )
            self._branch_cache[key] = result
            return result
        if not short or short.startswith("/") or "\\" in short or ".." in short or "@{" in short:
            result = BranchResolution(requested, None, None, reason="branchless_or_invalid")
            self._branch_cache[key] = result
            return result

        local_ref = f"refs/heads/{short}"
        local = _run_git(
            tree.repository_root,
            ("show-ref", "--verify", "--quiet", local_ref),
            timeout=self.timeout,
            check=False,
        )
        if local.returncode == 0:
            result = BranchResolution(requested, local_ref, "local", (local_ref,))
            self._branch_cache[key] = result
            return result

        listed = _run_git(
            tree.repository_root,
            ("for-each-ref", "--format=%(refname)%09%(objectname)", "refs/remotes"),
            timeout=self.timeout,
        )
        suffix = "/" + short
        candidates: list[tuple[str, str]] = []
        for line in listed.stdout.splitlines():
            pieces = line.strip().split("\t")
            if len(pieces) == 2 and pieces[0].startswith("refs/remotes/") and pieces[0].endswith(suffix):
                candidates.append((pieces[0], pieces[1]))
        candidates.sort()
        refs = tuple(ref for ref, _ in candidates)
        tips = {tip for _, tip in candidates}
        if len(candidates) == 1 or (candidates and len(tips) == 1):
            result = BranchResolution(requested, candidates[0][0], "remote", refs)
        elif not candidates:
            result = BranchResolution(requested, None, None, (), "branch_missing")
        else:
            result = BranchResolution(requested, None, None, refs, "ambiguous_remote_branch")
        self._branch_cache[key] = result
        return result

    def history(self, tree: TreeSpec, resolved_ref: str) -> tuple[tuple[int, int, str], ...]:
        if not tree.repository_root:
            return ()
        key = (self.repository_key(tree), resolved_ref)
        if key in self._history_cache:
            return self._history_cache[key]
        completed = _run_git(
            tree.repository_root,
            ("rev-list", "--timestamp", "--topo-order", resolved_ref),
            timeout=self.timeout,
        )
        rows: list[tuple[int, int, str]] = []
        for order, line in enumerate(completed.stdout.splitlines()):
            pieces = line.strip().split()
            if len(pieces) != 2:
                continue
            try:
                commit_ts = int(pieces[0])
            except ValueError:
                continue
            rows.append((commit_ts, order, pieces[1]))
        result = tuple(rows)
        self._history_cache[key] = result
        return result

    def subtree_exists(self, tree: TreeSpec, commit: str) -> bool:
        subtree = _safe_subtree(tree.repository_relative_root)
        if not subtree:
            return True
        if not tree.repository_root:
            return False
        key = (self.repository_key(tree), commit, subtree)
        if key in self._subtree_cache:
            return self._subtree_cache[key]
        completed = _run_git(
            tree.repository_root,
            ("cat-file", "-t", f"{commit}:{subtree}"),
            timeout=self.timeout,
            check=False,
        )
        exists = completed.returncode == 0 and completed.stdout.strip() == "tree"
        self._subtree_cache[key] = exists
        return exists

    def _eligible_history(
        self, tree: TreeSpec, resolved_ref: str
    ) -> tuple[tuple[tuple[int, int], int, int, str], ...]:
        subtree = _safe_subtree(tree.repository_relative_root)
        cache_key = (self.repository_key(tree), resolved_ref, subtree)
        if cache_key in self._eligible_history_cache:
            return self._eligible_history_cache[cache_key]
        rows = [
            ((commit_ts, -order), commit_ts, order, commit)
            for commit_ts, order, commit in self.history(tree, resolved_ref)
            if self.subtree_exists(tree, commit)
        ]
        rows.sort(key=lambda row: row[0])
        result = tuple(rows)
        self._eligible_history_cache[cache_key] = result
        return result

    def select(self, tree: TreeSpec, branch: str, query_ts: float) -> tuple[HistoricalSelection | None, BranchResolution]:
        if not math.isfinite(float(query_ts)):
            raise ValueError(f"query_ts must be finite, not {query_ts!r}")
        resolution = self.resolve_branch(tree, branch)
        if not resolution.resolved_ref or not resolution.ref_kind or not tree.repository_root:
            return None, resolution
        rows = self._eligible_history(tree, resolution.resolved_ref)
        keys = [row[0] for row in rows]
        position = bisect_right(keys, (math.floor(float(query_ts)), math.inf)) - 1
        if position < 0:
            return None, resolution
        _, commit_ts, order, commit = rows[position]
        identity = self.repository_key(tree)
        return (
            HistoricalSelection(
                tree_id=tree.tree_id,
                repository_root=tree.repository_root,
                repository_identity=identity,
                repository_relative_root=_safe_subtree(tree.repository_relative_root),
                requested_branch=str(branch),
                resolved_ref=resolution.resolved_ref,
                ref_kind=resolution.ref_kind,
                commit=commit,
                commit_ts=commit_ts,
                gap_seconds=float(query_ts) - commit_ts,
                ancestry_order=order,
            ),
            resolution,
        )

    def choose_at_or_before(
        self,
        candidates: Iterable[TreeSpec],
        branch: str,
        query_ts: float,
    ) -> tuple[HistoricalSelection | None, dict[str, BranchResolution]]:
        selections: list[HistoricalSelection] = []
        resolutions: dict[str, BranchResolution] = {}
        for tree in candidates:
            selected, resolution = self.select(tree, branch, query_ts)
            resolutions[tree.tree_id] = resolution
            if selected is not None:
                selections.append(selected)
        if not selections:
            return None, resolutions
        # Closest prior committer time wins; a topological tie is already
        # deterministic within a source, and tree_id breaks cross-source ties.
        selections.sort(key=lambda item: (-item.commit_ts, item.ancestry_order, item.tree_id))
        return selections[0], resolutions

    def head(self, tree: TreeSpec, query_ts: float | None) -> HeadSelection | None:
        if not tree.repository_root:
            return None
        key = (self.repository_key(tree), tree.repository_root)
        if key not in self._head_cache:
            completed = _run_git(
                tree.repository_root,
                ("show", "-s", "--format=%H%x09%ct", "HEAD"),
                timeout=self.timeout,
                check=False,
            )
            parsed: tuple[str, int] | None = None
            if completed.returncode == 0:
                pieces = completed.stdout.strip().split("\t")
                if len(pieces) == 2:
                    try:
                        parsed = (pieces[0], int(pieces[1]))
                    except ValueError:
                        parsed = None
            self._head_cache[key] = parsed
        parsed = self._head_cache[key]
        if parsed is None or not self.subtree_exists(tree, parsed[0]):
            return None
        commit, commit_ts = parsed
        gap = None if query_ts is None else float(query_ts) - commit_ts
        return HeadSelection(
            tree_id=tree.tree_id,
            repository_root=tree.repository_root,
            repository_identity=self.repository_key(tree),
            repository_relative_root=_safe_subtree(tree.repository_relative_root),
            commit=commit,
            commit_ts=commit_ts,
            gap_seconds=gap,
        )


__all__ = [
    "GitHistoryCache",
    "HeadSelection",
    "HistoricalSelection",
    "HistoryError",
]

