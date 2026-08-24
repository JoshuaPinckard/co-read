"""Empirical query-tree catalogue for the retrieval benchmark V2.

The first benchmark indexed one assumed checkout.  The transcript records do
not support that assumption: an absolute ``query.path`` may target a different
checkout from ``cwd``, and many of the named worktrees no longer exist at their
transcript spelling.  This module derives logical trees from every eval-set
record, discovers the Git repositories and worktree registries present on the
benchmark host, and emits an auditable assignment for both target and cwd.

There is deliberately no retrieval or scoring code here.  Historical commit
selection remains in :mod:`provenance_v2`; this catalogue supplies its
``TreeSpec`` inputs and records cases where a logical tree has more than one
possible repository epoch.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
import hashlib
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from provenance_v2 import (
    TreeSpec,
    effective_scope_for_record,
    normalise_absolute_path,
    path_within_logical_tree,
)


SCHEMA_VERSION = "tree-catalog-v2/1"
DEFAULT_WINDOW_FILES: dict[int, str] = {
    60: "evalset_60.jsonl",
    300: "evalset.jsonl",
    900: "evalset_900.jsonl",
}
_WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")


def _windows_path(value: str) -> bool:
    return bool(_WINDOWS_DRIVE.match(value) or "\\" in value)


def _norm(value: Any, base: Any = None) -> str | None:
    return normalise_absolute_path(value, base)


def _key(value: str) -> str:
    normalised = _norm(value)
    if normalised is None:
        raise ValueError(f"path is not absolute: {value!r}")
    return normalised.casefold() if _windows_path(normalised) else normalised


def _basename(value: str) -> str:
    return (ntpath.basename(value) if _windows_path(value) else posixpath.basename(value))


def _dirname(value: str) -> str:
    return ntpath.dirname(value) if _windows_path(value) else posixpath.dirname(value)


def _join(root: str, *parts: str) -> str:
    module = ntpath if _windows_path(root) else posixpath
    return module.normpath(module.join(root, *parts))


def _relative(path: str, root: str) -> str | None:
    if not path_within_logical_tree(path, root):
        return None
    module = ntpath if _windows_path(path) or _windows_path(root) else posixpath
    try:
        value = module.relpath(path, root)
    except ValueError:
        return None
    return "" if value == "." else value.replace("\\", "/")


def _first_relative_component(path: str, root: str) -> str | None:
    relative = _relative(path, root)
    if not relative:
        return None
    return relative.split("/", 1)[0]


def _path_exists(path: str | None) -> bool:
    return bool(path and os.path.exists(path))


def _path_is_file(path: str | None) -> bool:
    return bool(path and os.path.isfile(path))


def _git(
    repository: str,
    arguments: Sequence[str],
    *,
    git_executable: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [git_executable, "-C", repository, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(
            [git_executable, "-C", repository, *arguments],
            124,
            "",
            str(error),
        )


@dataclass(frozen=True, slots=True)
class EpochCandidate:
    """One repository source that may represent a logical tree epoch.

    An epoch candidate is not silently selected.  In particular, the current
    plain ``Desktop\\toolsenabled`` directory has appeared both as a subtree of
    the broad Desktop repository and as the checkout root of the private engine
    repository.  A runner must resolve a candidate using branch/time evidence.
    """

    candidate_id: str
    source_kind: str
    repository_root: str
    repository_identity: str
    repository_relative_root: str = ""
    matching_observed_branches: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_kind": self.source_kind,
            "repository_root": self.repository_root,
            "repository_identity": self.repository_identity,
            "repository_relative_root": self.repository_relative_root,
            "matching_observed_branches": list(self.matching_observed_branches),
            "evidence": list(self.evidence),
        }

    def as_tree(self, logical_tree: TreeSpec) -> TreeSpec:
        """Materialise this candidate as a TreeSpec for provenance selection."""

        return TreeSpec(
            tree_id=f"{logical_tree.tree_id}@{self.candidate_id}",
            logical_root=logical_tree.logical_root,
            repository_root=self.repository_root,
            repository_identity=self.repository_identity,
            repository_relative_root=self.repository_relative_root,
            current_root=logical_tree.current_root,
            available=logical_tree.available,
            note=(
                f"epoch candidate {self.candidate_id}; source must be selected "
                "before reconstruction"
            ),
        )


@dataclass(frozen=True, slots=True)
class TreeCatalogEntry:
    tree: TreeSpec
    mapping_kind: str
    evidence: tuple[str, ...] = ()
    epoch_candidates: tuple[EpochCandidate, ...] = ()
    target_counts: tuple[tuple[int, int], ...] = ()
    cwd_counts: tuple[tuple[int, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = self.tree.to_dict()
        value.update(
            {
                "mapping_kind": self.mapping_kind,
                "evidence": list(self.evidence),
                "epoch_candidates": [item.to_dict() for item in self.epoch_candidates],
                "target_counts": {str(window): count for window, count in self.target_counts},
                "cwd_counts": {str(window): count for window, count in self.cwd_counts},
            }
        )
        return value


@dataclass(frozen=True, slots=True)
class ScopeAssignment:
    window_seconds: int
    sequence: int
    record_id: str
    effective_scope: str | None
    cwd: str | None
    target_tree_id: str | None
    cwd_tree_id: str | None
    target_mapping_kind: str | None
    cwd_mapping_kind: str | None
    target_reason: str | None
    cwd_reason: str | None
    target_evidence: tuple[str, ...] = ()
    cwd_evidence: tuple[str, ...] = ()
    target_available: bool = False
    cwd_available: bool = False
    outside_any_indexed_tree: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "window_seconds": self.window_seconds,
            "sequence": self.sequence,
            "record_id": self.record_id,
            "effective_scope": self.effective_scope,
            "cwd": self.cwd,
            "target_tree_id": self.target_tree_id,
            "cwd_tree_id": self.cwd_tree_id,
            "target_mapping_kind": self.target_mapping_kind,
            "cwd_mapping_kind": self.cwd_mapping_kind,
            "target_reason": self.target_reason,
            "cwd_reason": self.cwd_reason,
            "target_evidence": list(self.target_evidence),
            "cwd_evidence": list(self.cwd_evidence),
            "target_available": self.target_available,
            "cwd_available": self.cwd_available,
            "outside_any_indexed_tree": self.outside_any_indexed_tree,
        }


@dataclass(frozen=True, slots=True)
class CatalogBuild:
    entries: tuple[TreeCatalogEntry, ...]
    assignments: tuple[ScopeAssignment, ...]
    record_counts: tuple[tuple[int, int], ...]

    @property
    def trees(self) -> tuple[TreeSpec, ...]:
        return tuple(entry.tree for entry in self.entries)

    @property
    def entry_by_id(self) -> dict[str, TreeCatalogEntry]:
        return {entry.tree.tree_id: entry for entry in self.entries}

    def to_dict(self) -> dict[str, Any]:
        target_totals = Counter[int]()
        outside_totals = Counter[int]()
        for assignment in self.assignments:
            if assignment.target_tree_id is not None:
                target_totals[assignment.window_seconds] += 1
            if assignment.outside_any_indexed_tree:
                outside_totals[assignment.window_seconds] += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "windows_seconds": [window for window, _ in self.record_counts],
            "counts": {
                "records_by_window": {
                    str(window): count for window, count in self.record_counts
                },
                "target_assignments_by_window": {
                    str(window): target_totals[window]
                    for window, _ in self.record_counts
                },
                "outside_any_indexed_tree_by_window": {
                    str(window): outside_totals[window]
                    for window, _ in self.record_counts
                },
            },
            "trees": [entry.to_dict() for entry in self.entries],
        }


@dataclass(slots=True)
class _GitRepository:
    identity: str
    roots: set[str] = field(default_factory=set)
    worktrees: set[str] = field(default_factory=set)

    @property
    def representative_root(self) -> str:
        def rank(path: str) -> tuple[int, int, str]:
            name = _basename(path).casefold()
            # The persistent canonical checkouts are preferable to numbered
            # or temporary worktrees as a history-command source.
            preferred = int(name not in {"toolsenabled-current", "mission-control", "engine"})
            return preferred, len(path), path.casefold()

        # A root observed through a valid .git marker is a much safer command
        # source than an arbitrary registered worktree (the legacy repository,
        # for example, has short-lived C:\\te-* worktrees in its registry).
        candidates = [path for path in self.roots if _path_exists(path)]
        if not candidates:
            candidates = [path for path in self.worktrees if _path_exists(path)]
        if not candidates:
            candidates = list(self.roots | self.worktrees)
        if not candidates:
            raise ValueError(f"repository {self.identity!r} has no checkout roots")
        return min(candidates, key=rank)


@dataclass(frozen=True, slots=True)
class _Marker:
    root: str
    valid: bool
    top_level: str | None = None
    common_dir: str | None = None
    detail: str | None = None


@dataclass(slots=True)
class _Environment:
    markers: dict[str, _Marker]
    path_markers: dict[str, str | None]
    repositories: dict[str, _GitRepository]
    worktree_paths: dict[str, _GitRepository]
    basename_aliases: dict[str, _GitRepository]
    desktop_repositories: tuple[_GitRepository, ...]
    git_executable: str
    timeout: float


@dataclass(frozen=True, slots=True)
class _Draft:
    logical_root: str
    mapping_kind: str
    reason: str
    evidence: tuple[str, ...]
    repository_root: str | None = None
    repository_identity: str | None = None
    repository_relative_root: str = ""
    current_root: str | None = None
    available: bool = True


def _tree_id(logical_root: str) -> str:
    normalised = _key(logical_root)
    slug = re.sub(r"[^a-z0-9]+", "-", _basename(normalised).casefold()).strip("-")
    slug = slug[:32] or "root"
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def _iter_ancestors(path: str) -> Iterable[str]:
    current = path
    if _path_is_file(current):
        current = _dirname(current)
    seen: set[str] = set()
    while current and _key(current) not in seen:
        seen.add(_key(current))
        yield current
        parent = _dirname(current)
        if not parent or _key(parent) == _key(current):
            break
        current = parent


def _locate_marker(path: str) -> str | None:
    for ancestor in _iter_ancestors(path):
        marker = _join(ancestor, ".git")
        if os.path.lexists(marker):
            return _norm(ancestor)
    return None


def _marker_target(root: str) -> str | None:
    """Resolve the Git directory named by ``root/.git`` without Git ascent."""

    marker = Path(_join(root, ".git"))
    try:
        if marker.is_dir():
            return _norm(marker.resolve(strict=False))
        if marker.is_file():
            first_line = marker.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            if not first_line.casefold().startswith("gitdir:"):
                return None
            target = first_line.split(":", 1)[1].strip()
            resolved = Path(target)
            if not resolved.is_absolute():
                resolved = marker.parent / resolved
            return _norm(resolved.resolve(strict=False))
    except (OSError, IndexError):
        return None
    return None


def _inspect_marker(
    root: str,
    *,
    git_executable: str,
    timeout: float,
) -> _Marker:
    expected_git_dir = _marker_target(root)
    if expected_git_dir is None:
        return _Marker(root=root, valid=False, detail=".git marker is unreadable or malformed")
    completed = _git(
        root,
        (
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
            "--absolute-git-dir",
        ),
        git_executable=git_executable,
        timeout=timeout,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode or len(lines) < 3:
        detail = completed.stderr.strip() or completed.stdout.strip() or "git rev-parse failed"
        return _Marker(root=root, valid=False, detail=detail)
    top_level = _norm(lines[-3])
    common_dir = _norm(lines[-2], root)
    actual_git_dir = _norm(Path(lines[-1]).resolve(strict=False))
    if top_level is None or common_dir is None or actual_git_dir is None:
        return _Marker(root=root, valid=False, detail="git returned non-absolute metadata")
    if _key(actual_git_dir) != _key(expected_git_dir):
        return _Marker(
            root=root,
            valid=False,
            detail=(
                "git ignored the local .git marker and ascended to another repository: "
                f"marker={expected_git_dir}; discovered={actual_git_dir}"
            ),
        )
    return _Marker(
        root=root,
        valid=True,
        top_level=top_level,
        common_dir=common_dir,
    )


def _worktrees(
    repository: str,
    *,
    git_executable: str,
    timeout: float,
) -> tuple[str, ...]:
    completed = _git(
        repository,
        ("worktree", "list", "--porcelain"),
        git_executable=git_executable,
        timeout=timeout,
    )
    if completed.returncode:
        return ()
    values: set[str] = set()
    for line in completed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        value = _norm(line[len("worktree ") :])
        if value is not None:
            values.add(value)
    return tuple(sorted(values, key=str.casefold))


def _discover_environment(
    paths: Iterable[str],
    *,
    git_executable: str,
    timeout: float,
) -> _Environment:
    marker_roots: set[str] = set()
    path_markers: dict[str, str | None] = {}
    ordered_paths = sorted({_key(item) for item in paths}, key=str.casefold)
    logical_toolsenabled_roots: set[str] = set()
    for path in ordered_paths:
        discovered_markers = [
            _norm(ancestor)
            for ancestor in _iter_ancestors(path)
            if os.path.lexists(_join(ancestor, ".git"))
        ]
        discovered_markers = [item for item in discovered_markers if item is not None]
        path_markers[_key(path)] = discovered_markers[0] if discovered_markers else None
        marker_roots.update(discovered_markers)
        if _windows_path(path):
            token = "\\desktop\\toolsenabled"
            index = path.casefold().find(token)
            if index >= 0:
                end = index + len(token)
                if end == len(path) or path[end] == "\\":
                    logical_toolsenabled_roots.add(path[:end])

    # The archived checkout is itself provenance evidence for the former plain
    # toolsenabled tree, even when no retained query points directly into its
    # directory.  Discover it as a candidate source, not as the default source.
    for logical_root in sorted(logical_toolsenabled_roots, key=str.casefold):
        archive = _join(logical_root, "legacy")
        if os.path.lexists(_join(archive, ".git")):
            marker_roots.add(_key(archive))
            path_markers[_key(archive)] = _key(archive)

    markers: dict[str, _Marker] = {}
    repositories: dict[str, _GitRepository] = {}
    ordered_marker_roots = sorted(marker_roots, key=str.casefold)

    def inspect(root: str) -> _Marker:
        return _inspect_marker(root, git_executable=git_executable, timeout=timeout)

    # A real eval population references hundreds of worktrees.  Each rev-parse
    # is independent, while serial process startup is particularly expensive
    # on Windows, so bound the discovery fan-out rather than paying it N times.
    with ThreadPoolExecutor(max_workers=min(12, max(1, len(ordered_marker_roots)))) as pool:
        inspected_markers = tuple(pool.map(inspect, ordered_marker_roots))
    for root, marker in zip(ordered_marker_roots, inspected_markers):
        markers[_key(root)] = marker
        if not marker.valid or marker.common_dir is None:
            continue
        identity = _key(marker.common_dir)
        repository = repositories.setdefault(identity, _GitRepository(identity))
        repository.roots.add(root)
        if marker.top_level:
            repository.roots.add(marker.top_level)

    ordered_repositories = sorted(repositories.values(), key=lambda item: item.identity)

    def list_worktrees(repository: _GitRepository) -> tuple[str, ...]:
        return _worktrees(
            repository.representative_root,
            git_executable=git_executable,
            timeout=timeout,
        )

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(ordered_repositories)))) as pool:
        registry_results = tuple(pool.map(list_worktrees, ordered_repositories))
    for repository, registered in zip(ordered_repositories, registry_results):
        for worktree in registered:
            repository.worktrees.add(worktree)

    worktree_paths: dict[str, _GitRepository] = {}
    aliases: dict[str, set[str]] = defaultdict(set)
    for identity, repository in repositories.items():
        for worktree in repository.worktrees | repository.roots:
            worktree_paths[_key(worktree)] = repository
            aliases[_basename(worktree).casefold()].add(identity)
    basename_aliases = {
        name: repositories[next(iter(identities))]
        for name, identities in aliases.items()
        if len(identities) == 1
    }

    desktop_repositories = tuple(
        sorted(
            (
                repository
                for repository in repositories.values()
                if any(_basename(root).casefold() == "desktop" for root in repository.roots)
            ),
            key=lambda item: item.identity,
        )
    )
    return _Environment(
        markers=markers,
        path_markers=path_markers,
        repositories=repositories,
        worktree_paths=worktree_paths,
        basename_aliases=basename_aliases,
        desktop_repositories=desktop_repositories,
        git_executable=git_executable,
        timeout=timeout,
    )


def _marker_for_path(path: str, environment: _Environment) -> _Marker | None:
    path_key = _key(path)
    if path_key not in environment.path_markers:
        environment.path_markers[path_key] = _locate_marker(path)
    root = environment.path_markers[path_key]
    if root is None:
        return None
    key = _key(root)
    marker = environment.markers.get(key)
    if marker is None:
        marker = _inspect_marker(
            root,
            git_executable=environment.git_executable,
            timeout=environment.timeout,
        )
        environment.markers[key] = marker
    return marker


def _repository_for_marker(marker: _Marker, environment: _Environment) -> _GitRepository | None:
    if marker.common_dir is None:
        return None
    return environment.repositories.get(_key(marker.common_dir))


def _registered_worktree_match(
    path: str, environment: _Environment
) -> tuple[str, _GitRepository] | None:
    # Registry roots are directories.  Walking the lexical ancestors is both
    # component-safe and O(path depth); comparing every query with hundreds of
    # registered worktrees made the original catalogue pass quadratic.
    current = path
    seen: set[str] = set()
    while current:
        current_key = _key(current)
        if current_key in seen:
            break
        seen.add(current_key)
        repository = environment.worktree_paths.get(current_key)
        if repository is not None:
            return current_key, repository
        parent = _dirname(current)
        if not parent or _key(parent) == current_key:
            break
        current = parent
    return None


def _desktop_context(
    path: str, environment: _Environment
) -> tuple[str, str, _GitRepository] | None:
    contexts: list[tuple[int, str, str, _GitRepository]] = []
    for repository in environment.desktop_repositories:
        for root in repository.roots:
            if _basename(root).casefold() != "desktop":
                continue
            component = _first_relative_component(path, root)
            if component:
                contexts.append((len(root), root, component, repository))
    if not contexts:
        return None
    _, root, component, repository = max(contexts, key=lambda item: item[0])
    return root, component, repository


def _same_repository_worktree(
    repository: _GitRepository, basename: str
) -> str | None:
    candidates = [
        item
        for item in repository.worktrees | repository.roots
        if _basename(item).casefold() == basename.casefold() and _path_exists(item)
    ]
    return min(candidates, key=lambda value: (len(value), value.casefold())) if candidates else None


def _alias_draft(
    logical_root: str,
    basename: str,
    repository: _GitRepository,
    *,
    reason: str,
) -> _Draft:
    current = _same_repository_worktree(repository, basename)
    return _Draft(
        logical_root=logical_root,
        mapping_kind="worktree_registry_basename_alias",
        reason=reason,
        evidence=(
            f"basename={basename}",
            f"unique_repository_identity={repository.identity}",
            f"current_equivalent={current or 'none'}",
        ),
        repository_root=repository.representative_root,
        repository_identity=repository.identity,
        current_root=current,
        available=True,
    )


_UUID_COMPONENT = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _empirical_non_git_root(path: str) -> str:
    """Choose a stable non-Git project/session root from the target path.

    These boundaries are structural, not guessed repository identities.  In
    particular, Claude scratch space is separated by encoded cwd and session
    UUID; grouping it all at ``Temp\\claude`` would turn deleted session trees
    into false current-state successes.  Installed packages similarly stop at
    their package root rather than at the user's whole home directory.
    """

    if not _windows_path(path):
        pieces = [part for part in path.split("/") if part]
        if "tmp" in (part.casefold() for part in pieces):
            index = [part.casefold() for part in pieces].index("tmp")
            end = min(len(pieces), index + 2)
            return "/" + "/".join(pieces[:end])
        return path

    parts = path.split("\\")
    lowered = [part.casefold() for part in parts]

    def through(index: int) -> str:
        return _norm("\\".join(parts[: index + 1])) or path

    if "temp" in lowered:
        index = lowered.index("temp")
        if index + 1 >= len(parts) or not parts[index + 1]:
            return through(index)
        child = lowered[index + 1]
        if child != "claude":
            return through(index + 1)
        if index + 2 >= len(parts) or not parts[index + 2]:
            return through(index + 1)
        session_parent = lowered[index + 2]
        if session_parent == "prof" and index + 3 < len(parts):
            return through(index + 3)
        if (
            session_parent.startswith("c--")
            and index + 3 < len(parts)
            and _UUID_COMPONENT.match(parts[index + 3])
        ):
            return through(index + 3)
        return through(index + 2)

    if "lanes" in lowered:
        index = lowered.index("lanes")
        return through(index + 1) if index + 1 < len(parts) else through(index)

    user_index: int | None = None
    for index, value in enumerate(lowered[:-1]):
        if value == "users" and parts[index + 1]:
            user_index = index + 1
            break
    if user_index is not None and user_index + 1 < len(parts):
        child_index = user_index + 1
        child = lowered[child_index]
        if child == ".vscode" and child_index + 2 < len(parts):
            if lowered[child_index + 1] == "extensions":
                return through(child_index + 2)
        if child == "appdata" and child_index + 1 < len(parts):
            area = lowered[child_index + 1]
            if area == "local" and child_index + 2 < len(parts):
                if lowered[child_index + 2] == "programs":
                    if "capability" in lowered[child_index + 3 :]:
                        return through(lowered.index("capability", child_index + 3))
                    if child_index + 3 < len(parts):
                        return through(child_index + 3)
            if area == "roaming" and child_index + 2 < len(parts):
                if "startup" in lowered[child_index + 2 :]:
                    return through(lowered.index("startup", child_index + 2))
                if "node_modules" in lowered[child_index + 2 :]:
                    modules = lowered.index("node_modules", child_index + 2)
                    if modules + 1 < len(parts):
                        package_end = modules + 1
                        if parts[package_end].startswith("@") and package_end + 1 < len(parts):
                            package_end += 1
                        return through(package_end)
                return through(child_index + 2)
        return through(child_index)

    if "programdata" in lowered:
        index = lowered.index("programdata")
        if "startup" in lowered[index + 1 :]:
            return through(lowered.index("startup", index + 1))
        return through(index + 1) if index + 1 < len(parts) else through(index)
    return path


def _classify(path: str | None, environment: _Environment) -> _Draft | None:
    if path is None:
        return None
    path = _key(path)
    marker = _marker_for_path(path, environment)

    if marker is not None and not marker.valid:
        return _Draft(
            logical_root=marker.root,
            mapping_kind="broken_git_marker",
            reason="nearest .git marker is not a valid checkout; use current-tree control only",
            evidence=(f"git_marker={_join(marker.root, '.git')}", f"detail={marker.detail}"),
            current_root=marker.root if _path_exists(marker.root) else None,
            available=_path_exists(marker.root),
        )

    desktop = _desktop_context(path, environment)
    if desktop is not None:
        desktop_root, component, desktop_repository = desktop
        logical_root = _join(desktop_root, component)
        component_key = component.casefold()

        # A nested checkout with its own valid marker is authoritative.  The
        # broad Desktop repository is only an umbrella fallback.
        if marker is not None and marker.valid and _key(marker.root) != _key(desktop_root):
            repository = _repository_for_marker(marker, environment)
            if repository is not None:
                return _Draft(
                    logical_root=marker.root,
                    mapping_kind="current_git_boundary",
                    reason="nearest valid .git boundary",
                    evidence=(
                        f"git_top_level={marker.top_level}",
                        f"git_common_dir={marker.common_dir}",
                    ),
                    repository_root=marker.top_level or repository.representative_root,
                    repository_identity=repository.identity,
                    current_root=marker.root,
                    available=True,
                )

        # Historical worktree spellings frequently survive only in transcripts.
        # Match the top-level basename only when the registry makes repository
        # identity unambiguous.
        alias_repository = environment.basename_aliases.get(component_key)
        if component_key != "toolsenabled" and alias_repository is not None:
            exact = _registered_worktree_match(path, environment)
            if exact is None or _key(exact[0]) != _key(logical_root):
                return _alias_draft(
                    logical_root,
                    component,
                    alias_repository,
                    reason="Desktop child basename uniquely identifies a registered worktree repository",
                )

        if component_key == "toolsenabled":
            exists = _path_exists(logical_root)
            return _Draft(
                logical_root=logical_root,
                mapping_kind="logical_toolsenabled_epoch",
                reason=(
                    "plain toolsenabled is an organisational tree with multiple historical "
                    "repository-source candidates"
                ),
                evidence=(
                    f"desktop_umbrella={desktop_root}",
                    "source selection deferred to epoch_candidates",
                ),
                current_root=logical_root if exists else None,
                available=exists or bool(desktop_repository),
            )

        exists = _path_exists(logical_root)
        return _Draft(
            logical_root=logical_root,
            mapping_kind="desktop_repository_subtree",
            reason=(
                "query path is within one top-level subtree of the broad Desktop repository; "
                "historical reconstruction remains available when the current subtree is absent"
            ),
            evidence=(
                f"desktop_repository_identity={desktop_repository.identity}",
                f"subtree={component}",
                f"current_subtree_exists={str(exists).lower()}",
            ),
            repository_root=desktop_repository.representative_root,
            repository_identity=desktop_repository.identity,
            repository_relative_root=component,
            current_root=logical_root if exists else None,
            available=True,
        )

    if marker is not None and marker.valid:
        repository = _repository_for_marker(marker, environment)
        if repository is not None:
            return _Draft(
                logical_root=marker.root,
                mapping_kind="current_git_boundary",
                reason="nearest valid .git boundary",
                evidence=(
                    f"git_top_level={marker.top_level}",
                    f"git_common_dir={marker.common_dir}",
                ),
                repository_root=marker.top_level or repository.representative_root,
                repository_identity=repository.identity,
                current_root=marker.root,
                available=True,
            )

    registered = _registered_worktree_match(path, environment)
    if registered is not None:
        root, repository = registered
        return _Draft(
            logical_root=root,
            mapping_kind="registered_worktree_path",
            reason="path is inside a Git worktree registry entry",
            evidence=(f"registered_path={root}", f"repository_identity={repository.identity}"),
            repository_root=repository.representative_root,
            repository_identity=repository.identity,
            current_root=root if _path_exists(root) else None,
            available=True,
        )

    logical_root = _empirical_non_git_root(path)

    basename = _basename(logical_root).casefold()
    alias_repository = environment.basename_aliases.get(basename)
    if alias_repository is not None:
        return _alias_draft(
            logical_root,
            _basename(logical_root),
            alias_repository,
            reason="project-anchor basename uniquely identifies a registered worktree repository",
        )

    exists = _path_exists(logical_root)
    return _Draft(
        logical_root=logical_root,
        mapping_kind="non_git_current_tree" if exists else "unavailable_empirical_tree",
        reason=(
            "no valid Git boundary; current filesystem fallback"
            if exists
            else "no valid Git boundary and empirical tree root is absent"
        ),
        evidence=(f"empirical_anchor={logical_root}", f"current_root_exists={str(exists).lower()}"),
        current_root=logical_root if exists else None,
        available=exists,
    )


def _draft_rank(draft: _Draft) -> int:
    return {
        "current_git_boundary": 9,
        "worktree_registry_basename_alias": 8,
        "registered_worktree_path": 8,
        "logical_toolsenabled_epoch": 7,
        "desktop_repository_subtree": 6,
        "broken_git_marker": 5,
        "non_git_current_tree": 4,
        "unavailable_empirical_tree": 1,
    }.get(draft.mapping_kind, 0)


def _merge_drafts(existing: _Draft | None, candidate: _Draft) -> _Draft:
    if existing is None:
        return candidate
    if _draft_rank(candidate) > _draft_rank(existing):
        primary, other = candidate, existing
    else:
        primary, other = existing, candidate
    evidence = tuple(sorted(set(primary.evidence + other.evidence), key=str.casefold))
    return replace(primary, evidence=evidence, available=primary.available or other.available)


def _branch_refs(
    repository: _GitRepository,
    *,
    git_executable: str,
    timeout: float,
) -> tuple[str, ...]:
    completed = _git(
        repository.representative_root,
        ("for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes"),
        git_executable=git_executable,
        timeout=timeout,
    )
    if completed.returncode:
        return ()
    return tuple(sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()}))


def _branch_is_present(branch: str, refs: Sequence[str]) -> bool:
    rendered = branch.strip()
    if not rendered or rendered.upper() == "HEAD":
        return False
    if rendered.startswith("refs/"):
        return rendered in refs
    return f"refs/heads/{rendered}" in refs or any(
        ref.startswith("refs/remotes/") and ref.endswith("/" + rendered) for ref in refs
    )


def _toolsenabled_candidates(
    logical_root: str,
    observed_branches: Iterable[str],
    environment: _Environment,
) -> tuple[EpochCandidate, ...]:
    branches = tuple(sorted({item for item in observed_branches if item}, key=str.casefold))
    values: list[EpochCandidate] = []

    for desktop in environment.desktop_repositories:
        refs = _branch_refs(
            desktop,
            git_executable=environment.git_executable,
            timeout=environment.timeout,
        )
        matches = tuple(branch for branch in branches if _branch_is_present(branch, refs))
        values.append(
            EpochCandidate(
                candidate_id="desktop-subtree",
                source_kind="desktop_repository_subtree",
                repository_root=desktop.representative_root,
                repository_identity=desktop.identity,
                repository_relative_root=_basename(logical_root),
                matching_observed_branches=matches,
                evidence=(
                    "current plain toolsenabled is a top-level Desktop repository subtree",
                    f"observed branches present={','.join(matches) or 'none'}",
                ),
            )
        )

    archive_repositories = [
        repository
        for repository in environment.repositories.values()
        if any(
            _basename(root).casefold() == "legacy"
            and _basename(_dirname(root)).casefold() == "toolsenabled"
            for root in repository.roots | repository.worktrees
        )
    ]
    for index, repository in enumerate(sorted(archive_repositories, key=lambda item: item.identity)):
        refs = _branch_refs(
            repository,
            git_executable=environment.git_executable,
            timeout=environment.timeout,
        )
        matches = tuple(branch for branch in branches if _branch_is_present(branch, refs))
        suffix = "" if len(archive_repositories) == 1 else f"-{index + 1}"
        values.append(
            EpochCandidate(
                candidate_id=f"archived-legacy-root{suffix}",
                source_kind="archived_legacy_checkout_root",
                repository_root=repository.representative_root,
                repository_identity=repository.identity,
                repository_relative_root="",
                matching_observed_branches=matches,
                evidence=(
                    "Desktop/toolsenabled/legacy is a distinct valid Git checkout",
                    "its main/rescue history is a candidate for the former plain checkout",
                    f"observed branches present={','.join(matches) or 'none'}",
                ),
            )
        )

    engine_repositories = [
        repository
        for repository in environment.repositories.values()
        if any(
            _basename(root).casefold() == "toolsenabled-current"
            for root in repository.roots | repository.worktrees
        )
    ]
    for index, repository in enumerate(sorted(engine_repositories, key=lambda item: item.identity)):
        refs = _branch_refs(
            repository,
            git_executable=environment.git_executable,
            timeout=environment.timeout,
        )
        matches = tuple(branch for branch in branches if _branch_is_present(branch, refs))
        suffix = "" if len(engine_repositories) == 1 else f"-{index + 1}"
        values.append(
            EpochCandidate(
                candidate_id=f"legacy-engine-root{suffix}",
                source_kind="legacy_checkout_root",
                repository_root=repository.representative_root,
                repository_identity=repository.identity,
                repository_relative_root="",
                matching_observed_branches=matches,
                evidence=(
                    "toolsenabled-current worktree registry identifies the private engine history",
                    "legacy main/rescue branches may represent the former plain toolsenabled checkout",
                    f"observed branches present={','.join(matches) or 'none'}",
                ),
            )
        )
    return tuple(sorted(values, key=lambda item: item.candidate_id))


def build_catalog(
    evalsets: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    git_executable: str = "git",
    git_timeout: float = 20.0,
) -> CatalogBuild:
    """Build a deterministic catalogue and assignment manifest.

    ``evalsets`` must contain independently generated populations keyed by
    window seconds.  Every record is processed; the target comes from
    :func:`effective_scope_for_record`, so an absolute ``query.path`` is
    authoritative over ``cwd``.
    """

    windows = tuple(sorted(int(window) for window in evalsets))
    normalised_records: list[tuple[int, int, Mapping[str, Any], str | None, str | None]] = []
    discovery_paths: set[str] = set()
    for window in windows:
        for sequence, record in enumerate(evalsets[window]):
            target = effective_scope_for_record(record)
            cwd = _norm(record.get("cwd"))
            if target is not None:
                discovery_paths.add(target)
            if cwd is not None:
                discovery_paths.add(cwd)
            normalised_records.append((window, sequence, record, target, cwd))

    environment = _discover_environment(
        discovery_paths,
        git_executable=git_executable,
        timeout=git_timeout,
    )
    drafts: dict[str, _Draft] = {}
    draft_cache: dict[str, _Draft | None] = {}

    def classify_cached(path: str | None) -> _Draft | None:
        if path is None:
            return None
        path_key = _key(path)
        if path_key not in draft_cache:
            draft_cache[path_key] = _classify(path, environment)
        return draft_cache[path_key]

    classified: list[tuple[int, int, Mapping[str, Any], str | None, str | None, _Draft | None, _Draft | None]] = []
    observed_target_branches: dict[str, set[str]] = defaultdict(set)
    for window, sequence, record, target, cwd in normalised_records:
        target_draft = classify_cached(target)
        cwd_draft = classify_cached(cwd)
        for draft in (target_draft, cwd_draft):
            if draft is not None:
                root_key = _key(draft.logical_root)
                drafts[root_key] = _merge_drafts(drafts.get(root_key), draft)
        branch = record.get("git_branch", record.get("gitBranch"))
        if target_draft is not None and isinstance(branch, str) and branch.strip():
            observed_target_branches[_key(target_draft.logical_root)].add(branch.strip())
        classified.append((window, sequence, record, target, cwd, target_draft, cwd_draft))

    entries_by_root: dict[str, TreeCatalogEntry] = {}
    for root_key, draft in sorted(drafts.items(), key=lambda item: item[0]):
        candidates: tuple[EpochCandidate, ...] = ()
        if draft.mapping_kind == "logical_toolsenabled_epoch":
            candidates = _toolsenabled_candidates(
                draft.logical_root,
                observed_target_branches.get(root_key, ()),
                environment,
            )
        tree = TreeSpec(
            tree_id=_tree_id(draft.logical_root),
            logical_root=draft.logical_root,
            repository_root=draft.repository_root,
            repository_identity=draft.repository_identity,
            repository_relative_root=draft.repository_relative_root,
            current_root=draft.current_root,
            available=draft.available,
            note=(
                "repository epoch is ambiguous; inspect epoch_candidates"
                if candidates
                else draft.reason
            ),
        )
        entries_by_root[root_key] = TreeCatalogEntry(
            tree=tree,
            mapping_kind=draft.mapping_kind,
            evidence=draft.evidence,
            epoch_candidates=candidates,
        )

    assignments: list[ScopeAssignment] = []
    target_counts: dict[str, Counter[int]] = defaultdict(Counter)
    cwd_counts: dict[str, Counter[int]] = defaultdict(Counter)
    for window, sequence, record, target, cwd, target_draft, cwd_draft in classified:
        target_entry = (
            entries_by_root.get(_key(target_draft.logical_root)) if target_draft else None
        )
        cwd_entry = entries_by_root.get(_key(cwd_draft.logical_root)) if cwd_draft else None
        if target_entry:
            target_counts[target_entry.tree.tree_id][window] += 1
        if cwd_entry:
            cwd_counts[cwd_entry.tree.tree_id][window] += 1
        target_indexable = bool(
            target_entry
            and target_entry.tree.available
            and (
                target_entry.tree.repository_root
                or target_entry.epoch_candidates
            )
        )
        assignments.append(
            ScopeAssignment(
                window_seconds=window,
                sequence=sequence,
                record_id=str(record.get("id", f"record-{sequence}")),
                effective_scope=target,
                cwd=cwd,
                target_tree_id=target_entry.tree.tree_id if target_entry else None,
                cwd_tree_id=cwd_entry.tree.tree_id if cwd_entry else None,
                target_mapping_kind=target_draft.mapping_kind if target_draft else None,
                cwd_mapping_kind=cwd_draft.mapping_kind if cwd_draft else None,
                target_reason=target_draft.reason if target_draft else "effective scope is invalid",
                cwd_reason=cwd_draft.reason if cwd_draft else "cwd is invalid",
                target_evidence=target_draft.evidence if target_draft else (),
                cwd_evidence=cwd_draft.evidence if cwd_draft else (),
                target_available=bool(target_entry and target_entry.tree.available),
                cwd_available=bool(cwd_entry and cwd_entry.tree.available),
                outside_any_indexed_tree=not target_indexable,
            )
        )

    entries: list[TreeCatalogEntry] = []
    for entry in sorted(entries_by_root.values(), key=lambda item: item.tree.tree_id):
        entries.append(
            replace(
                entry,
                target_counts=tuple(
                    (window, target_counts[entry.tree.tree_id][window]) for window in windows
                ),
                cwd_counts=tuple(
                    (window, cwd_counts[entry.tree.tree_id][window]) for window in windows
                ),
            )
        )
    return CatalogBuild(
        entries=tuple(entries),
        assignments=tuple(assignments),
        record_counts=tuple((window, len(evalsets[window])) for window in windows),
    )


def load_evalsets(
    eval_dir: str | os.PathLike[str],
    *,
    window_files: Mapping[int, str] = DEFAULT_WINDOW_FILES,
) -> dict[int, list[dict[str, Any]]]:
    directory = Path(eval_dir)
    result: dict[int, list[dict[str, Any]]] = {}
    for window, filename in sorted(window_files.items()):
        records: list[dict[str, Any]] = []
        with (directory / filename).open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"{filename}:{line_number}: record is not an object")
                records.append(value)
        result[int(window)] = records
    return result


def write_catalog_outputs(
    build: CatalogBuild,
    output_dir: str | os.PathLike[str],
) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    catalog_path = directory / "tree-catalog-v2.json"
    manifest_path = directory / "scope-manifest-v2.jsonl"
    catalog_path.write_text(
        json.dumps(build.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
        for assignment in sorted(
            build.assignments,
            key=lambda item: (item.window_seconds, item.sequence, item.record_id),
        ):
            handle.write(
                json.dumps(assignment.to_dict(), sort_keys=True, ensure_ascii=False) + "\n"
            )
    return catalog_path, manifest_path


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    default_eval_dir = project_root / "exploratory" / "retrieval" / "v2"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, default=default_eval_dir)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--git", default="git", dest="git_executable")
    parser.add_argument("--git-timeout", type=float, default=20.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    evalsets = load_evalsets(args.eval_dir)
    build = build_catalog(
        evalsets,
        git_executable=args.git_executable,
        git_timeout=args.git_timeout,
    )
    output_dir = args.output_dir or args.eval_dir
    catalog_path, manifest_path = write_catalog_outputs(build, output_dir)
    counts = build.to_dict()["counts"]
    print(json.dumps(counts, sort_keys=True))
    print(catalog_path)
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CatalogBuild",
    "DEFAULT_WINDOW_FILES",
    "EpochCandidate",
    "SCHEMA_VERSION",
    "ScopeAssignment",
    "TreeCatalogEntry",
    "build_catalog",
    "load_evalsets",
    "main",
    "write_catalog_outputs",
]
