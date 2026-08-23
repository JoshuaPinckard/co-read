"""Generic tree and Git-time provenance helpers for retrieval benchmark V2.

This module deliberately contains no machine-specific tree catalogue.  Callers
provide :class:`TreeSpec` records derived from the eval set and the repositories
available on the benchmark host.

The transcript's ``git_branch`` describes the checkout containing ``cwd``.  It
is therefore usable for the query target only when the cwd and target trees have
the same repository identity.  Cross-repository absolute searches are explicit
fallbacks rather than accidental same-named-branch matches.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from enum import Enum
import json
import math
import ntpath
import os
import posixpath
import re
import subprocess
from typing import Any, Iterable, Mapping, Sequence


WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
MSYS_ABSOLUTE = re.compile(r"^[\\/]([A-Za-z])(?:[\\/](.*))?$")


class ReconstructionMode(str, Enum):
    """How the physical state for one query was selected."""

    BRANCH_AT_OR_BEFORE = "branch_at_or_before"
    FALLBACK_HEAD_BRANCHLESS = "fallback_head_branchless"
    FALLBACK_CROSS_REPOSITORY = "fallback_cross_repository"
    FALLBACK_BRANCH_MISSING = "fallback_branch_missing"
    FALLBACK_NO_PRIOR_COMMIT = "fallback_no_prior_commit"
    FALLBACK_NON_GIT = "fallback_non_git"
    FALLBACK_INVALID_TIMESTAMP = "fallback_invalid_timestamp"
    UNAVAILABLE_TREE = "unavailable_tree"


class RepositoryRelation(str, Enum):
    SAME = "same"
    DIFFERENT = "different"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TreeSpec:
    """One logical query tree and the repository that can materialise it.

    ``logical_root`` is the path spelling used by transcript labels.
    ``repository_root`` is any local checkout/bare repository suitable for Git
    history commands.  ``repository_relative_root`` locates the logical tree
    inside a checkout materialised from that repository.  Worktrees belonging
    to the same repository should share an explicit ``repository_identity``
    (normally the canonical Git common-directory path).
    """

    tree_id: str
    logical_root: str
    repository_root: str | None = None
    repository_identity: str | None = None
    repository_relative_root: str = ""
    current_root: str | None = None
    available: bool = True
    note: str | None = None

    def __post_init__(self) -> None:
        if not self.tree_id.strip():
            raise ValueError("tree_id must not be empty")
        if normalise_absolute_path(self.logical_root) is None:
            raise ValueError(f"logical_root must be absolute: {self.logical_root!r}")
        relative = self.repository_relative_root.replace("\\", "/")
        relative = posixpath.normpath(relative) if relative else ""
        if relative in ("", "."):
            relative = ""
        if relative == ".." or relative.startswith("../") or posixpath.isabs(relative):
            raise ValueError(
                "repository_relative_root must be a safe relative path, not "
                f"{self.repository_relative_root!r}"
            )
        object.__setattr__(self, "repository_relative_root", relative)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tree_id": self.tree_id,
            "logical_root": self.logical_root,
            "repository_root": self.repository_root,
            "repository_identity": self.repository_identity,
            "repository_relative_root": self.repository_relative_root,
            "current_root": self.current_root,
            "available": self.available,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TreeSpec":
        return cls(
            tree_id=str(value["tree_id"]),
            logical_root=str(value["logical_root"]),
            repository_root=_optional_string(value.get("repository_root")),
            repository_identity=_optional_string(value.get("repository_identity")),
            repository_relative_root=str(value.get("repository_relative_root") or ""),
            current_root=_optional_string(value.get("current_root")),
            available=bool(value.get("available", True)),
            note=_optional_string(value.get("note")),
        )


@dataclass(frozen=True, slots=True)
class BranchResolution:
    requested_branch: str
    resolved_ref: str | None
    ref_kind: str | None
    candidates: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.resolved_ref is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_branch": self.requested_branch,
            "resolved_ref": self.resolved_ref,
            "ref_kind": self.ref_kind,
            "candidates": list(self.candidates),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CommitSelection:
    commit: str
    commit_ts: int
    gap_seconds: float
    ancestry_order: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "commit_ts": self.commit_ts,
            "gap_seconds": self.gap_seconds,
            "ancestry_order": self.ancestry_order,
        }


@dataclass(frozen=True, slots=True)
class QueryProvenance:
    record_id: str | None
    query_ts: float | None
    effective_scope: str | None
    cwd_tree_id: str | None
    target_tree_id: str | None
    cwd_repository_identity: str | None
    target_repository_identity: str | None
    requested_branch: str | None
    resolved_ref: str | None
    mode: ReconstructionMode
    exact: bool
    commit: str | None
    commit_ts: int | None
    gap_seconds: float | None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "query_ts": self.query_ts,
            "effective_scope": self.effective_scope,
            "cwd_tree_id": self.cwd_tree_id,
            "target_tree_id": self.target_tree_id,
            "cwd_repository_identity": self.cwd_repository_identity,
            "target_repository_identity": self.target_repository_identity,
            "requested_branch": self.requested_branch,
            "resolved_ref": self.resolved_ref,
            "mode": self.mode.value,
            "exact": self.exact,
            "commit": self.commit,
            "commit_ts": self.commit_ts,
            "gap_seconds": self.gap_seconds,
            "reason": self.reason,
        }


class GitCommandError(RuntimeError):
    pass


def _optional_string(value: Any) -> str | None:
    if isinstance(value, (str, os.PathLike)):
        rendered = os.fspath(value)
        return rendered if rendered else None
    return None


def _strip_path(value: Any) -> str | None:
    if not isinstance(value, (str, os.PathLike)):
        return None
    rendered = os.fspath(value).strip().strip('"')
    return rendered or None


def _as_windows_path(value: str) -> str:
    match = MSYS_ABSOLUTE.match(value)
    if match:
        tail = (match.group(2) or "").replace("/", "\\")
        return f"{match.group(1)}:\\{tail}" if tail else f"{match.group(1)}:\\"
    return value.replace("/", "\\")


def _looks_windows(value: str) -> bool:
    return bool(
        WINDOWS_ABSOLUTE.match(value)
        or MSYS_ABSOLUTE.match(value)
        or re.match(r"^[A-Za-z]:", value)
        or "\\" in value
    )


def normalise_absolute_path(value: Any, base: Any = None) -> str | None:
    """Return an absolute lexical path using Windows/MSYS-aware semantics.

    Filesystem existence is deliberately irrelevant.  This lets callers map
    vanished worktrees from historical transcript paths.
    """

    raw = _strip_path(value)
    if raw is None:
        return None
    base_text = _strip_path(base)
    windows = _looks_windows(raw) or bool(base_text and _looks_windows(base_text))
    if windows:
        candidate = _as_windows_path(raw)
        base_candidate = _as_windows_path(base_text) if base_text else None
        if not ntpath.isabs(candidate):
            if not base_candidate or not ntpath.isabs(base_candidate):
                return None
            candidate = ntpath.join(base_candidate, candidate)
        if not ntpath.isabs(candidate):
            return None
        return ntpath.normcase(ntpath.normpath(candidate))

    candidate = raw.replace("\\", "/")
    if not posixpath.isabs(candidate):
        if not base_text:
            return None
        base_candidate = base_text.replace("\\", "/")
        if not posixpath.isabs(base_candidate):
            return None
        candidate = posixpath.join(base_candidate, candidate)
    if not posixpath.isabs(candidate):
        return None
    return posixpath.normpath(candidate)


def effective_scope(cwd: Any, query_path: Any = None) -> str | None:
    """Resolve a Grep target, treating an omitted/empty ``path`` as ``.``."""

    raw_scope = _strip_path(query_path) or "."
    return normalise_absolute_path(raw_scope, cwd)


def effective_scope_for_record(record: Mapping[str, Any]) -> str | None:
    query = record.get("query")
    query_path = query.get("path") if isinstance(query, Mapping) else record.get("path")
    return effective_scope(record.get("cwd"), query_path)


def _path_parts(path: str) -> tuple[str, ...]:
    if _looks_windows(path):
        drive, tail = ntpath.splitdrive(_as_windows_path(path))
        pieces = tuple(piece.casefold() for piece in re.split(r"[\\/]+", tail) if piece)
        return ((drive.casefold(),) if drive else ()) + pieces
    return tuple(piece for piece in path.split("/") if piece)


def path_within_logical_tree(path: Any, logical_root: Any) -> bool:
    candidate = normalise_absolute_path(path)
    root = normalise_absolute_path(logical_root)
    if candidate is None or root is None:
        return False
    windows = _looks_windows(candidate) or _looks_windows(root)
    module = ntpath if windows else posixpath
    try:
        common = module.commonpath((candidate, root))
    except ValueError:
        return False
    if windows:
        return ntpath.normcase(common) == ntpath.normcase(root)
    return common == root


def longest_logical_tree_match(path: Any, trees: Iterable[TreeSpec]) -> TreeSpec | None:
    """Return the deepest component-safe logical-root match.

    Duplicate spellings are resolved by ``tree_id`` so a malformed catalogue
    remains deterministic.  Callers should normally reject duplicate roots when
    constructing the environment-specific catalogue.
    """

    candidate = normalise_absolute_path(path)
    if candidate is None:
        return None
    matches: list[tuple[int, int, str, TreeSpec]] = []
    for tree in trees:
        root = normalise_absolute_path(tree.logical_root)
        if root is None or not path_within_logical_tree(candidate, root):
            continue
        matches.append((len(_path_parts(root)), len(root), tree.tree_id, tree))
    if not matches:
        return None
    # Deeper and then longer roots win.  For identical normalised roots, the
    # lexicographically smallest tree_id is the stable catalogue tie-break.
    best_depth = max(item[0] for item in matches)
    matches = [item for item in matches if item[0] == best_depth]
    best_length = max(item[1] for item in matches)
    matches = [item for item in matches if item[1] == best_length]
    return min(matches, key=lambda item: item[2])[3]


def _canonical_identity(value: str | None) -> str | None:
    if not value:
        return None
    normalised = normalise_absolute_path(value)
    if normalised is not None:
        return normalised.casefold() if _looks_windows(normalised) else normalised
    return value.casefold()


def repository_identity(tree: TreeSpec | None) -> str | None:
    if tree is None:
        return None
    return _canonical_identity(tree.repository_identity or tree.repository_root)


def compare_repository_identity(
    left: TreeSpec | None,
    right: TreeSpec | None,
) -> RepositoryRelation:
    left_identity = repository_identity(left)
    right_identity = repository_identity(right)
    if left_identity is None or right_identity is None:
        return RepositoryRelation.UNKNOWN
    return RepositoryRelation.SAME if left_identity == right_identity else RepositoryRelation.DIFFERENT


def _git(
    repository: str | os.PathLike[str],
    arguments: Sequence[str],
    *,
    git_executable: str = "git",
    timeout: float = 60.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [git_executable, "-C", os.fspath(repository), *arguments],
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
        raise GitCommandError(f"git invocation failed: {error}") from error
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise GitCommandError(f"git {' '.join(arguments)} failed: {detail}")
    return completed


def is_git_repository(
    repository: str | os.PathLike[str] | None,
    *,
    git_executable: str = "git",
    timeout: float = 60.0,
) -> bool:
    if not repository:
        return False
    try:
        return _git(
            repository,
            ("rev-parse", "--git-dir"),
            git_executable=git_executable,
            timeout=timeout,
            check=False,
        ).returncode == 0
    except GitCommandError:
        return False


def _verify_ref(
    repository: str | os.PathLike[str],
    ref: str,
    *,
    git_executable: str,
    timeout: float,
) -> bool:
    return _git(
        repository,
        ("show-ref", "--verify", "--quiet", ref),
        git_executable=git_executable,
        timeout=timeout,
        check=False,
    ).returncode == 0


def _short_branch_name(branch: str) -> str | None:
    rendered = branch.strip()
    if not rendered or rendered.upper() == "HEAD":
        return None
    if rendered.startswith("refs/heads/"):
        rendered = rendered[len("refs/heads/") :]
    elif rendered.startswith("refs/remotes/"):
        # An explicitly fully qualified remote ref is handled directly by
        # resolve_branch; there is no unambiguous short suffix to infer here.
        return rendered
    if not rendered or rendered.startswith("/"):
        return None
    if "\\" in rendered or ".." in rendered or "@{" in rendered:
        return None
    return rendered


def resolve_branch(
    repository: str | os.PathLike[str],
    branch: str,
    *,
    git_executable: str = "git",
    timeout: float = 60.0,
) -> BranchResolution:
    """Resolve an exact local branch, otherwise one unique remote branch."""

    requested = str(branch).strip()
    short = _short_branch_name(requested)
    if short is None:
        return BranchResolution(requested, None, None, reason="branchless_or_invalid")

    if short.startswith("refs/remotes/"):
        if _verify_ref(
            repository, short, git_executable=git_executable, timeout=timeout
        ):
            return BranchResolution(requested, short, "remote", (short,))
        return BranchResolution(requested, None, None, reason="branch_missing")

    local_ref = f"refs/heads/{short}"
    if _verify_ref(
        repository, local_ref, git_executable=git_executable, timeout=timeout
    ):
        return BranchResolution(requested, local_ref, "local", (local_ref,))

    completed = _git(
        repository,
        ("for-each-ref", "--format=%(refname)", "refs/remotes"),
        git_executable=git_executable,
        timeout=timeout,
    )
    suffix = "/" + short
    candidates = tuple(
        sorted(
            ref.strip()
            for ref in completed.stdout.splitlines()
            if ref.strip().startswith("refs/remotes/") and ref.strip().endswith(suffix)
        )
    )
    if len(candidates) == 1:
        return BranchResolution(requested, candidates[0], "remote", candidates)
    if not candidates:
        return BranchResolution(requested, None, None, (), "branch_missing")
    return BranchResolution(requested, None, None, candidates, "ambiguous_remote_branch")


def closest_commit_at_or_before(
    repository: str | os.PathLike[str],
    ref: str,
    query_ts: float,
    *,
    git_executable: str = "git",
    timeout: float = 60.0,
) -> CommitSelection | None:
    """Select the closest committer-time commit at or before ``query_ts``.

    The entire selected-ref ancestry is considered.  Ties in committer time use
    the first commit in deterministic ``git rev-list --topo-order`` output.
    """

    try:
        timestamp = float(query_ts)
    except (TypeError, ValueError):
        raise ValueError(f"query_ts must be numeric, not {query_ts!r}") from None
    completed = _git(
        repository,
        ("rev-list", "--timestamp", "--topo-order", ref),
        git_executable=git_executable,
        timeout=timeout,
    )
    best: tuple[int, int, str] | None = None
    for order, line in enumerate(completed.stdout.splitlines()):
        pieces = line.strip().split()
        if len(pieces) != 2:
            continue
        try:
            commit_ts = int(pieces[0])
        except ValueError:
            continue
        commit = pieces[1]
        if commit_ts > timestamp:
            continue
        # Earlier log/topological order wins an equal-time tie.
        candidate = (commit_ts, -order, commit)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    if best is None:
        return None
    commit_ts, negative_order, commit = best
    return CommitSelection(commit, commit_ts, timestamp - commit_ts, -negative_order)


def _head_selection(
    repository: str | os.PathLike[str],
    query_ts: float | None,
    *,
    git_executable: str,
    timeout: float,
) -> CommitSelection | None:
    completed = _git(
        repository,
        ("show", "-s", "--format=%H%x09%ct", "HEAD"),
        git_executable=git_executable,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        return None
    pieces = completed.stdout.strip().split("\t")
    if len(pieces) != 2:
        return None
    try:
        commit_ts = int(pieces[1])
    except ValueError:
        return None
    gap = float(query_ts) - commit_ts if query_ts is not None else None
    # QueryProvenance permits a nullable gap, while CommitSelection is used only
    # internally here.  Invalid-timestamp callers handle HEAD fields directly.
    return CommitSelection(pieces[0], commit_ts, gap if gap is not None else 0.0, 0)


def _numeric_timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        rendered = float(value)
        return rendered if math.isfinite(rendered) else None
    if isinstance(value, str) and value.strip():
        rendered = value.strip()
        try:
            numeric = float(rendered)
        except ValueError:
            try:
                numeric = dt.datetime.fromisoformat(rendered.replace("Z", "+00:00")).timestamp()
            except (ValueError, OverflowError):
                return None
        return numeric if math.isfinite(numeric) else None
    return None


def reconstruct_query(
    record: Mapping[str, Any],
    trees: Iterable[TreeSpec],
    *,
    git_executable: str = "git",
    timeout: float = 60.0,
) -> QueryProvenance:
    """Determine exact historical state or an explicit fallback for a query."""

    catalogue = tuple(trees)
    record_id = str(record["id"]) if record.get("id") is not None else None
    query_ts = _numeric_timestamp(record.get("ts", record.get("timestamp")))
    scope = effective_scope_for_record(record)
    target_tree = longest_logical_tree_match(scope, catalogue) if scope else None
    cwd_path = normalise_absolute_path(record.get("cwd"))
    cwd_tree = longest_logical_tree_match(cwd_path, catalogue) if cwd_path else None
    requested_branch = _optional_string(record.get("git_branch", record.get("gitBranch")))
    cwd_identity = repository_identity(cwd_tree)
    target_identity = repository_identity(target_tree)

    def result(
        mode: ReconstructionMode,
        *,
        exact: bool = False,
        resolved_ref: str | None = None,
        selection: CommitSelection | None = None,
        reason: str | None = None,
    ) -> QueryProvenance:
        return QueryProvenance(
            record_id=record_id,
            query_ts=query_ts,
            effective_scope=scope,
            cwd_tree_id=cwd_tree.tree_id if cwd_tree else None,
            target_tree_id=target_tree.tree_id if target_tree else None,
            cwd_repository_identity=cwd_identity,
            target_repository_identity=target_identity,
            requested_branch=requested_branch,
            resolved_ref=resolved_ref,
            mode=mode,
            exact=exact,
            commit=selection.commit if selection else None,
            commit_ts=selection.commit_ts if selection else None,
            gap_seconds=(selection.gap_seconds if selection and query_ts is not None else None),
            reason=reason,
        )

    if target_tree is None:
        return result(ReconstructionMode.UNAVAILABLE_TREE, reason="no_logical_tree_match")
    if not target_tree.available:
        return result(ReconstructionMode.UNAVAILABLE_TREE, reason="tree_marked_unavailable")
    repository = target_tree.repository_root
    if not repository or not is_git_repository(
        repository, git_executable=git_executable, timeout=timeout
    ):
        return result(ReconstructionMode.FALLBACK_NON_GIT, reason="target_tree_not_git")

    head = _head_selection(
        repository,
        query_ts,
        git_executable=git_executable,
        timeout=timeout,
    )
    relation = compare_repository_identity(cwd_tree, target_tree)
    if relation is not RepositoryRelation.SAME:
        reason = (
            "repository_identity_unknown"
            if relation is RepositoryRelation.UNKNOWN
            else "cwd_target_repository_mismatch"
        )
        return result(
            ReconstructionMode.FALLBACK_CROSS_REPOSITORY,
            selection=head,
            reason=reason,
        )
    if not requested_branch or requested_branch.upper() == "HEAD":
        return result(
            ReconstructionMode.FALLBACK_HEAD_BRANCHLESS,
            selection=head,
            reason="git_branch_missing_or_head",
        )
    if query_ts is None:
        return result(
            ReconstructionMode.FALLBACK_INVALID_TIMESTAMP,
            selection=head,
            reason="query_timestamp_missing_or_invalid",
        )

    resolution = resolve_branch(
        repository,
        requested_branch,
        git_executable=git_executable,
        timeout=timeout,
    )
    if not resolution.resolved_ref:
        return result(
            ReconstructionMode.FALLBACK_BRANCH_MISSING,
            selection=head,
            reason=resolution.reason or "branch_missing",
        )
    selection = closest_commit_at_or_before(
        repository,
        resolution.resolved_ref,
        query_ts,
        git_executable=git_executable,
        timeout=timeout,
    )
    if selection is None:
        return result(
            ReconstructionMode.FALLBACK_NO_PRIOR_COMMIT,
            resolved_ref=resolution.resolved_ref,
            selection=head,
            reason="no_commit_at_or_before_query",
        )
    return result(
        ReconstructionMode.BRANCH_AT_OR_BEFORE,
        exact=True,
        resolved_ref=resolution.resolved_ref,
        selection=selection,
    )


def trees_to_json(trees: Iterable[TreeSpec], *, indent: int | None = 2) -> str:
    """Serialize a catalogue without introducing an environment-specific file."""

    return json.dumps(
        [tree.to_dict() for tree in trees],
        indent=indent,
        sort_keys=True,
        ensure_ascii=False,
    )


def trees_from_json(payload: str) -> list[TreeSpec]:
    value = json.loads(payload)
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ValueError("tree catalogue JSON must be an array of objects")
    return [TreeSpec.from_dict(item) for item in value]


__all__ = [
    "BranchResolution",
    "CommitSelection",
    "GitCommandError",
    "QueryProvenance",
    "ReconstructionMode",
    "RepositoryRelation",
    "TreeSpec",
    "closest_commit_at_or_before",
    "compare_repository_identity",
    "effective_scope",
    "effective_scope_for_record",
    "is_git_repository",
    "longest_logical_tree_match",
    "normalise_absolute_path",
    "path_within_logical_tree",
    "reconstruct_query",
    "repository_identity",
    "resolve_branch",
    "trees_from_json",
    "trees_to_json",
]
