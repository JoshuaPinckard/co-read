#!/usr/bin/env python3
"""Run the corrected, multi-tree Grep-replacement benchmark.

V2 derives query trees from the eval records, reconstructs a clean Git state
per query, validates the recorded scope in that state, and then executes the
five benchmark arms once for the union of the 60/300/900-second populations.
The three windows are aggregated independently from those shared run rows.

This runner intentionally does not change the original extractor, arms,
indexer, or scorer.  Its JSONL artifacts contain only data rows (no headers),
are resumable under a content fingerprint, and classify every retained query
as exact, an explicit fallback, or unscored with a reason.
"""

from __future__ import annotations

import argparse
import contextlib
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import random
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

try:  # Package imports for tests.
    from . import arms, index
    from .history_v2 import GitHistoryCache, HeadSelection, HistoricalSelection
    from .incremental_index_v2 import refresh_index
    from .metrics_v2 import ALL_ARMS, aggregate_metrics_v2
    from .provenance_v2 import TreeSpec
    from .tree_catalog_v2 import (
        CatalogBuild,
        ScopeAssignment,
        TreeCatalogEntry,
        build_catalog,
        load_evalsets,
        write_catalog_outputs,
    )
except ImportError:  # Direct execution: python instruments/retrieval/run_v2.py
    import arms
    import index
    from history_v2 import GitHistoryCache, HeadSelection, HistoricalSelection
    from incremental_index_v2 import refresh_index
    from metrics_v2 import ALL_ARMS, aggregate_metrics_v2
    from provenance_v2 import TreeSpec
    from tree_catalog_v2 import (
        CatalogBuild,
        ScopeAssignment,
        TreeCatalogEntry,
        build_catalog,
        load_evalsets,
        write_catalog_outputs,
    )


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVAL_DIR = ROOT / "exploratory" / "retrieval" / "v2"
WINDOWS = (60, 300, 900)
TOP_K = 20
QUERY_ORDER_SEED = 20260823
RUN_SCHEMA = "retrieval-run-v2/1"
PROVENANCE_SCHEMA = "retrieval-reconstruction-v2/1"
EXCLUSION_SCHEMA = "retrieval-exclusion-v2/1"
WARMUP_SENTINEL = "codex_retrieval_v2_warmup_8b66d69ec6d548c999993f77ef64d60c"
FALLBACK_MODES = frozenset({"head_fallback", "non_git_current_fallback"})
_TREE_OBJECT_CACHE: dict[tuple[str, str, str], str | None] = {}
_REF_TIP_CACHE: dict[tuple[str, str], str | None] = {}

EVAL_FILENAMES = {
    60: "evalset_60.jsonl",
    300: "evalset.jsonl",
    900: "evalset_900.jsonl",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while piece := handle.read(1 << 20):
            digest.update(piece)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{number}: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: row is not an object")
            rows.append(value)
    return rows


def _numeric_timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, str) and value.strip():
        try:
            result = float(value)
        except ValueError:
            try:
                result = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except (ValueError, OverflowError):
                return None
        return result if math.isfinite(result) else None
    return None


def _execution_signature(record: Mapping[str, Any]) -> str:
    value = {
        "id": record.get("id"),
        "ts": record.get("ts", record.get("timestamp")),
        "cwd": record.get("cwd"),
        "git_branch": record.get("git_branch", record.get("gitBranch")),
        "query": record.get("query"),
    }
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def union_records(
    evalsets: Mapping[int, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[int, ...]]]:
    """Return one execution record per ID and its independent window membership."""

    records: dict[str, dict[str, Any]] = {}
    signatures: dict[str, str] = {}
    membership: dict[str, set[int]] = defaultdict(set)
    for window in sorted(evalsets):
        for raw in evalsets[window]:
            if raw.get("id") is None:
                raise ValueError(f"window {window} contains a record without id")
            record_id = str(raw["id"])
            signature = _execution_signature(raw)
            if record_id in signatures and signatures[record_id] != signature:
                raise ValueError(
                    f"record {record_id} has different execution inputs across windows"
                )
            if record_id not in records:
                records[record_id] = dict(raw)
                signatures[record_id] = signature
            membership[record_id].add(int(window))
    return records, {key: tuple(sorted(value)) for key, value in membership.items()}


def assignment_index(build: CatalogBuild) -> dict[str, ScopeAssignment]:
    """Collapse per-window assignments after proving their tree mapping agrees."""

    indexed: dict[str, ScopeAssignment] = {}
    fields = (
        "effective_scope",
        "cwd",
        "target_tree_id",
        "cwd_tree_id",
        "target_mapping_kind",
        "cwd_mapping_kind",
        "target_reason",
        "cwd_reason",
        "target_available",
        "cwd_available",
        "outside_any_indexed_tree",
    )
    for assignment in build.assignments:
        previous = indexed.get(assignment.record_id)
        if previous is not None and any(
            getattr(previous, field) != getattr(assignment, field) for field in fields
        ):
            raise ValueError(
                f"record {assignment.record_id} has inconsistent tree assignments across windows"
            )
        indexed.setdefault(assignment.record_id, assignment)
    return indexed


def _candidate_specs(entry: TreeCatalogEntry | None) -> list[TreeSpec]:
    if entry is None or not entry.tree.available:
        return []
    if entry.epoch_candidates:
        return [candidate.as_tree(entry.tree) for candidate in entry.epoch_candidates]
    if entry.tree.repository_root:
        return [entry.tree]
    return []


def _run_git(
    repository: str | os.PathLike[str],
    arguments: Sequence[str],
    *,
    timeout: float = 300.0,
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
        raise RuntimeError(f"git invocation failed: {error}") from error
    if check and completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return completed


def _checkout_info(path: str | os.PathLike[str]) -> tuple[str, str, str] | None:
    """Return (top-level, common-dir identity, path relative to top-level)."""

    root = Path(path)
    if not root.exists():
        return None
    top = _run_git(root, ("rev-parse", "--show-toplevel"), check=False)
    common = _run_git(
        root,
        ("rev-parse", "--path-format=absolute", "--git-common-dir"),
        check=False,
    )
    if top.returncode or common.returncode:
        return None
    top_level = Path(top.stdout.strip()).resolve(strict=True)
    common_dir = Path(common.stdout.strip()).resolve(strict=False)
    candidate = root.resolve(strict=True)
    try:
        relative = candidate.relative_to(top_level)
    except ValueError:
        return None
    rendered = "" if str(relative) == "." else relative.as_posix()
    return str(top_level), os.path.normcase(str(common_dir)), rendered


def _head_tree_candidates(
    entry: TreeCatalogEntry,
    candidates: Sequence[TreeSpec],
    cache: GitHistoryCache,
) -> list[tuple[TreeSpec, str]]:
    """Prefer HEAD from the target-equivalent current worktree."""

    result: list[tuple[TreeSpec, str]] = []
    current_root = entry.tree.current_root
    info = _checkout_info(current_root) if current_root else None
    if info is not None and current_root is not None:
        _, current_identity, current_relative = info
        for candidate in candidates:
            if cache.repository_key(candidate) != current_identity:
                continue
            result.append(
                (
                    TreeSpec(
                        tree_id=f"{candidate.tree_id}@current-head",
                        logical_root=entry.tree.logical_root,
                        # `git -C current_root show HEAD` observes this linked
                        # worktree's HEAD, not the representative checkout's.
                        repository_root=current_root,
                        repository_identity=current_identity,
                        repository_relative_root=current_relative,
                        current_root=current_root,
                        available=True,
                        note="target-equivalent current worktree HEAD",
                    ),
                    "current_root",
                )
            )

    seen = {cache.repository_key(tree) for tree, _ in result}
    for candidate in candidates:
        identity = cache.repository_key(candidate)
        if identity not in seen:
            result.append((candidate, "representative_checkout"))
            seen.add(identity)
    return result


def _selection_fields(selection: HistoricalSelection | HeadSelection) -> dict[str, Any]:
    return {
        "repository_root": selection.repository_root,
        "repository_identity": selection.repository_identity,
        "repository_relative_root": selection.repository_relative_root,
        "commit": selection.commit,
        "commit_ts": selection.commit_ts,
        "gap_seconds": selection.gap_seconds,
    }


def _selection_tree_object(selection: HistoricalSelection) -> str | None:
    subtree = _safe_relative(selection.repository_relative_root)
    cache_key = (selection.repository_identity, selection.commit, subtree)
    if cache_key in _TREE_OBJECT_CACHE:
        return _TREE_OBJECT_CACHE[cache_key]
    object_name = f"{selection.commit}:{subtree}" if subtree else f"{selection.commit}^{{tree}}"
    completed = _run_git(
        selection.repository_root,
        ("rev-parse", "--verify", object_name),
        check=False,
    )
    result = completed.stdout.strip() if completed.returncode == 0 else None
    _TREE_OBJECT_CACHE[cache_key] = result
    return result


def _ref_tip(repository: str, resolved_ref: str | None) -> str | None:
    if not resolved_ref:
        return None
    key = (os.path.normcase(os.path.normpath(repository)), resolved_ref)
    if key in _REF_TIP_CACHE:
        return _REF_TIP_CACHE[key]
    completed = _run_git(
        repository, ("rev-parse", "--verify", resolved_ref), check=False
    )
    result = completed.stdout.strip() if completed.returncode == 0 else None
    _REF_TIP_CACHE[key] = result
    return result


def _head_fallback(
    entry: TreeCatalogEntry,
    query_ts: float | None,
    cache: GitHistoryCache,
    *,
    reason: str,
) -> dict[str, Any] | None:
    candidates = _candidate_specs(entry)
    for tree, source in _head_tree_candidates(entry, candidates, cache):
        selected = cache.head(tree, query_ts)
        if selected is not None:
            return {
                **_selection_fields(selected),
                "mode": "head_fallback",
                "exact": False,
                "reason": reason,
                "resolved_ref": None,
                "resolved_ref_tip": None,
                "ref_kind": None,
                "fallback_head_source": source,
            }
    return None


def reconstruct_record(
    record: Mapping[str, Any],
    assignment: ScopeAssignment,
    entries: Mapping[str, TreeCatalogEntry],
    windows_seconds: Sequence[int],
    cache: GitHistoryCache,
) -> dict[str, Any]:
    """Classify one query before snapshot-scope validation."""

    record_id = str(record["id"])
    target_entry = entries.get(str(assignment.target_tree_id)) if assignment.target_tree_id else None
    cwd_entry = entries.get(str(assignment.cwd_tree_id)) if assignment.cwd_tree_id else None
    query_ts = _numeric_timestamp(record.get("ts", record.get("timestamp")))
    requested_branch = str(record.get("git_branch", record.get("gitBranch")) or "")

    base: dict[str, Any] = {
        "schema_version": PROVENANCE_SCHEMA,
        "record_id": record_id,
        "windows_seconds": list(sorted(int(value) for value in windows_seconds)),
        "query_ts": query_ts,
        "effective_scope": assignment.effective_scope,
        "cwd": assignment.cwd,
        "assigned_target_tree_id": assignment.target_tree_id,
        "target_tree_id": None,
        "cwd_tree_id": assignment.cwd_tree_id,
        "logical_root": target_entry.tree.logical_root if target_entry else None,
        "target_mapping_kind": assignment.target_mapping_kind,
        "cwd_mapping_kind": assignment.cwd_mapping_kind,
        "requested_branch": requested_branch,
        "resolved_ref": None,
        "resolved_ref_tip": None,
        "ref_kind": None,
        "mode": "unavailable",
        "exact": False,
        "commit": None,
        "commit_ts": None,
        "gap_seconds": None,
        "reason": assignment.target_reason or "target_tree_unmapped",
        "repository_root": None,
        "repository_identity": None,
        "repository_relative_root": None,
        "current_root": target_entry.tree.current_root if target_entry else None,
        "partial_arms": [],
        "cwd_repository_identities_with_branch": [],
        "fallback_head_source": None,
        "indexable": False,
        # Git history cannot reveal dirty or untracked state at transcript time.
        "dirty_state_reconstructable": False,
        "attempted_reconstruction_exact": False,
        "original_mode": None,
    }
    if target_entry is None or not target_entry.tree.available:
        return base

    target_candidates = _candidate_specs(target_entry)
    if not target_candidates:
        current = Path(target_entry.tree.current_root) if target_entry.tree.current_root else None
        partial_arms = ["ripgrep"] if current is not None and current.exists() else []
        base.update(
            {
                "mode": "non_git_current_fallback",
                "reason": "target_is_not_a_git_checkout; protected index requires Git",
                "partial_arms": partial_arms,
            }
        )
        return base

    cwd_candidates = _candidate_specs(cwd_entry)
    logical_tree_matches = bool(
        assignment.target_tree_id
        and assignment.cwd_tree_id
        and assignment.target_tree_id == assignment.cwd_tree_id
    )
    eligible: list[HistoricalSelection] = []
    candidate_evidence: list[dict[str, Any]] = []
    if logical_tree_matches and query_ts is not None and requested_branch:
        for candidate in target_candidates:
            selected_candidate, resolution = cache.select(
                candidate, requested_branch, query_ts
            )
            evidence: dict[str, Any] = {
                "candidate_tree_id": candidate.tree_id,
                "repository_root": candidate.repository_root,
                "repository_identity": cache.repository_key(candidate),
                "resolved_ref": resolution.resolved_ref,
                "resolution_reason": resolution.reason,
                "eligible": selected_candidate is not None,
            }
            if selected_candidate is not None:
                tree_object = _selection_tree_object(selected_candidate)
                evidence.update(
                    {
                        "commit": selected_candidate.commit,
                        "commit_ts": selected_candidate.commit_ts,
                        "gap_seconds": selected_candidate.gap_seconds,
                        "ref_kind": selected_candidate.ref_kind,
                        "resolved_ref_tip": _ref_tip(
                            selected_candidate.repository_root,
                            selected_candidate.resolved_ref,
                        ),
                        "target_tree_object": tree_object,
                        "target_tree_available_at_selected_commit": bool(tree_object),
                    }
                )
                evidence["eligible"] = bool(tree_object)
                if tree_object:
                    eligible.append(selected_candidate)
            candidate_evidence.append(evidence)
    base["epoch_candidate_evidence"] = candidate_evidence
    base["cwd_repository_identities_with_branch"] = sorted(
        {item["repository_identity"] for item in candidate_evidence if item.get("eligible")}
    )

    selected: HistoricalSelection | None = None
    epoch_ambiguous = False
    if len(eligible) == 1:
        selected = eligible[0]
    elif len(eligible) > 1:
        objects = {
            str(item.get("target_tree_object"))
            for item in candidate_evidence
            if item.get("eligible") and item.get("target_tree_object")
        }
        every_object_known = all(
            item.get("target_tree_object") for item in candidate_evidence if item.get("eligible")
        )
        if every_object_known and len(objects) == 1:
            selected = sorted(
                eligible,
                key=lambda item: (-item.commit_ts, item.ancestry_order, item.tree_id),
            )[0]
        else:
            epoch_ambiguous = True

    if selected is not None:
        base.update(
            {
                **_selection_fields(selected),
                "target_tree_id": target_entry.tree.tree_id,
                "mode": "historical_exact",
                "exact": True,
                "reason": None,
                "resolved_ref": selected.resolved_ref,
                "resolved_ref_tip": _ref_tip(selected.repository_root, selected.resolved_ref),
                "ref_kind": selected.ref_kind,
                "indexable": True,
                "attempted_reconstruction_exact": True,
            }
        )
        return base

    if query_ts is None:
        fallback_reason = "invalid_or_missing_query_timestamp"
    elif not requested_branch or requested_branch.upper() == "HEAD":
        fallback_reason = "branchless_or_HEAD"
    elif not logical_tree_matches:
        fallback_reason = "cwd_target_logical_tree_mismatch"
    elif epoch_ambiguous:
        fallback_reason = "ambiguous_epoch_source"
    elif not cwd_candidates:
        fallback_reason = "cwd_repository_unavailable_or_non_git"
    else:
        target_reasons = Counter(
            str(
                item.get("resolution_reason")
                or (
                    "target_tree_absent_at_closest_commit"
                    if item.get("commit") and not item.get("target_tree_available_at_selected_commit")
                    else "no_commit_at_or_before_query"
                )
            )
            for item in candidate_evidence
        )
        fallback_reason = "no_commit_at_or_before_query"
        if target_reasons:
            fallback_reason += ":" + ",".join(
                f"{reason}={count}" for reason, count in sorted(target_reasons.items())
            )

    fallback = _head_fallback(target_entry, query_ts, cache, reason=fallback_reason)
    if fallback is None:
        base["reason"] = fallback_reason + "; target_HEAD_unavailable"
        return base
    base.update(
        {
            **fallback,
            "target_tree_id": target_entry.tree.tree_id,
            "indexable": True,
        }
    )
    return base


def reconstruct_union(
    records: Mapping[str, Mapping[str, Any]],
    membership: Mapping[str, Sequence[int]],
    build: CatalogBuild,
    *,
    cache: GitHistoryCache | None = None,
) -> list[dict[str, Any]]:
    history = cache or GitHistoryCache()
    assignments = assignment_index(build)
    entries = build.entry_by_id
    rows: list[dict[str, Any]] = []
    for record_id in sorted(records):
        assignment = assignments.get(record_id)
        if assignment is None:
            raise ValueError(f"catalog has no assignment for retained record {record_id}")
        rows.append(
            reconstruct_record(
                records[record_id], assignment, entries, membership[record_id], history
            )
        )
    return rows


def _safe_relative(value: Any) -> str:
    rendered = str(value or "").replace("\\", "/")
    rendered = posixpath.normpath(rendered) if rendered else ""
    if rendered in ("", "."):
        return ""
    if rendered == ".." or rendered.startswith("../") or posixpath.isabs(rendered):
        raise ValueError(f"unsafe repository-relative root: {value!r}")
    return rendered


def _state_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("repository_identity") or ""),
        _safe_relative(row.get("repository_relative_root")),
        str(row.get("commit") or ""),
    )


def _state_id(key: tuple[str, str, str]) -> str:
    digest = hashlib.sha256("\0".join(key).encode("utf-8")).hexdigest()[:16]
    return f"state-{digest}"


@contextlib.contextmanager
def detached_worktree(
    repository: str | os.PathLike[str],
    commit: str,
    container_parent: str | os.PathLike[str],
    *,
    timeout: float = 300.0,
) -> Iterator[Path]:
    """Yield one clean detached worktree without mutating a live checkout."""

    repository_path = Path(repository).resolve(strict=True)
    parent = Path(container_parent)
    parent.mkdir(parents=True, exist_ok=True)
    parent = parent.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="retrieval-v2-state-", dir=parent) as temporary:
        container = Path(temporary).resolve(strict=True)
        if os.path.commonpath((str(container), str(parent))) != str(parent):
            raise RuntimeError("temporary worktree container escaped its owned parent")
        worktree = container / "repo"
        _run_git(
            repository_path,
            (
                "-c",
                "core.longpaths=true",
                "worktree",
                "add",
                "--detach",
                str(worktree),
                str(commit),
            ),
            timeout=timeout,
        )
        try:
            yield worktree.resolve(strict=True)
        finally:
            removed = _run_git(
                repository_path,
                (
                    "-c",
                    "core.longpaths=true",
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                ),
                timeout=timeout,
                check=False,
            )
            # TemporaryDirectory owns exactly `container`.  If Git could not
            # clean its registered worktree, remove only the verified child.
            if removed.returncode and worktree.exists():
                resolved = worktree.resolve(strict=False)
                if os.path.commonpath((str(resolved), str(container))) != str(container):
                    raise RuntimeError("refusing cleanup outside the temporary container")
                shutil.rmtree(resolved)


@contextlib.contextmanager
def _materialized_source(
    row: Mapping[str, Any],
    worktree_parent: Path,
    *,
    timeout: float,
) -> Iterator[Path]:
    repository = row.get("repository_root")
    commit = row.get("commit")
    if not repository or not commit:
        raise RuntimeError("state lacks repository_root or commit")
    relative = _safe_relative(row.get("repository_relative_root"))
    with detached_worktree(repository, str(commit), worktree_parent, timeout=timeout) as checkout:
        source = checkout.joinpath(*relative.split("/")) if relative else checkout
        if not source.is_dir():
            raise RuntimeError(f"repository subtree is absent at state: {relative or '.'}")
        resolved = source.resolve(strict=True)
        if os.path.commonpath((str(resolved), str(checkout))) != str(checkout):
            raise RuntimeError("repository subtree escaped detached worktree")
        yield resolved


def _scope_status(
    record: Mapping[str, Any], row: Mapping[str, Any], source_root: Path
) -> tuple[bool, str, dict[str, Any]]:
    logical_root = row.get("logical_root")
    if not logical_root:
        return False, "logical_root_missing", {}
    scope = arms.scope_for_record(
        record,
        logical_root=str(logical_root),
        source_root=source_root,
    )
    okay = bool(scope.get("in_scope") and scope.get("available"))
    reason = str(scope.get("reason") or ("ok" if okay else "scope_unavailable"))
    return okay, reason, scope


def _unscore(
    row: Mapping[str, Any],
    reason: str,
    *,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(row)
    original_mode = row.get("mode")
    original_selection = {
        key: row.get(key)
        for key in (
            "commit",
            "commit_ts",
            "gap_seconds",
            "resolved_ref",
            "resolved_ref_tip",
            "ref_kind",
            "repository_root",
            "repository_identity",
            "repository_relative_root",
        )
        if row.get(key) is not None
    }
    result.update(
        {
            "target_tree_id": None,
            "indexable": False,
            "exact": False,
            "mode": "unscored",
            "original_mode": row.get("original_mode") or original_mode,
            "attempted_reconstruction_exact": bool(
                row.get("attempted_reconstruction_exact") or row.get("exact") is True
            ),
            "reason": reason,
            "commit": None,
            "commit_ts": None,
            "gap_seconds": None,
            "resolved_ref": None,
            "resolved_ref_tip": None,
            "ref_kind": None,
            "repository_root": None,
            "repository_identity": None,
            "repository_relative_root": None,
        }
    )
    merged_detail = {
        "original_mode": original_mode,
        "original_selection": original_selection,
    }
    if detail:
        merged_detail.update(detail)
    result["exclusion_detail"] = merged_detail
    return result


def _group_rows_by_state(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str], list[Mapping[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("target_tree_id") and row.get("commit") and row.get("repository_root"):
            grouped[_state_key(row)].append(row)
    return grouped


def _scope_failures(
    records: Mapping[str, Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    worktree_parent: Path,
    git_timeout: float,
    materializer: Callable[..., contextlib.AbstractContextManager[Path]],
) -> dict[str, tuple[str, dict[str, Any]]]:
    failures: dict[str, tuple[str, dict[str, Any]]] = {}
    if materializer is not _materialized_source:
        # Test/custom materializers keep the simple per-state contract.
        for _, state_rows in sorted(_group_rows_by_state(rows).items()):
            exemplar = state_rows[0]
            try:
                with materializer(exemplar, worktree_parent, timeout=git_timeout) as source_root:
                    for row in state_rows:
                        record_id = str(row["record_id"])
                        try:
                            okay, reason, scope = _scope_status(records[record_id], row, source_root)
                            if not okay:
                                failures[record_id] = (
                                    reason,
                                    {"failure_kind": "scope_unavailable", "scope": scope},
                                )
                        except Exception as error:
                            failures[record_id] = (
                                "scope_preflight_exception",
                                {
                                    "failure_kind": "scope_exception",
                                    "error": f"{type(error).__name__}: {error}",
                                },
                            )
            except Exception as error:
                detail = {
                    "failure_kind": "infrastructure",
                    "materialization_error": f"{type(error).__name__}: {error}",
                }
                for row in state_rows:
                    failures[str(row["record_id"])] = (
                        "snapshot_materialization_failed",
                        detail,
                    )
        return failures

    worktree_parent.mkdir(parents=True, exist_ok=True)
    scratch = worktree_parent.resolve(strict=True).parent
    for _, states in _ordered_streams(rows):
        exemplar = states[0][1][0]
        try:
            with owned_stream_worktree(exemplar, worktree_parent, timeout=git_timeout) as checkout:
                for key, state_rows in states:
                    try:
                        source_root = checkout_owned_stream_state(
                            checkout, key[2], key[1], scratch, timeout=git_timeout
                        )
                        for row in state_rows:
                            record_id = str(row["record_id"])
                            try:
                                okay, reason, scope = _scope_status(records[record_id], row, source_root)
                                if not okay:
                                    failures[record_id] = (
                                        reason,
                                        {"failure_kind": "scope_unavailable", "scope": scope},
                                    )
                            except Exception as error:
                                failures[record_id] = (
                                    "scope_preflight_exception",
                                    {
                                        "failure_kind": "scope_exception",
                                        "error": f"{type(error).__name__}: {error}",
                                    },
                                )
                    except Exception as error:
                        detail = {
                            "failure_kind": "infrastructure",
                            "materialization_error": f"{type(error).__name__}: {error}",
                        }
                        for row in state_rows:
                            failures[str(row["record_id"])] = (
                                "snapshot_materialization_failed",
                                detail,
                            )
        except Exception as error:
            detail = {
                "failure_kind": "infrastructure",
                "materialization_error": f"{type(error).__name__}: {error}",
            }
            for _, state_rows in states:
                for row in state_rows:
                    failures[str(row["record_id"])] = (
                        "snapshot_stream_materialization_failed",
                        detail,
                    )
    return failures


def preflight_reconstructed_scopes(
    records: Mapping[str, Mapping[str, Any]],
    initial_rows: Sequence[Mapping[str, Any]],
    build: CatalogBuild,
    *,
    cache: GitHistoryCache | None = None,
    worktree_parent: Path,
    git_timeout: float = 300.0,
    materializer: Callable[..., contextlib.AbstractContextManager[Path]] = _materialized_source,
) -> list[dict[str, Any]]:
    """Validate exact scopes, retrying failures on target-equivalent HEAD.

    This is deliberately a preflight rather than a silent filter in the timed
    run.  The final provenance artifact therefore records the state that was
    actually measured and can be fingerprinted before any resumable rows are
    accepted.
    """

    history = cache or GitHistoryCache()
    entries = build.entry_by_id
    by_id = {str(row["record_id"]): dict(row) for row in initial_rows}
    exact_rows = [row for row in by_id.values() if row.get("exact") is True]
    exact_failures = _scope_failures(
        records,
        exact_rows,
        worktree_parent=worktree_parent,
        git_timeout=git_timeout,
        materializer=materializer,
    )

    # Replace an exact state whose scope is unavailable with an explicit HEAD
    # fallback.  Preserve the attempted exact selection for the audit trail.
    for record_id, (scope_reason, scope_detail) in exact_failures.items():
        original = by_id[record_id]
        if scope_detail.get("failure_kind") != "scope_unavailable":
            by_id[record_id] = _unscore(
                original,
                f"exact_snapshot_preflight_failed:{scope_reason}",
                detail=scope_detail,
            )
            continue
        assigned = original.get("assigned_target_tree_id")
        entry = entries.get(str(assigned)) if assigned else None
        fallback = (
            _head_fallback(
                entry,
                _numeric_timestamp(original.get("query_ts")),
                history,
                reason="exact_snapshot_scope_unavailable",
            )
            if entry is not None
            else None
        )
        attempted = {
            "commit": original.get("commit"),
            "commit_ts": original.get("commit_ts"),
            "gap_seconds": original.get("gap_seconds"),
            "resolved_ref": original.get("resolved_ref"),
            "scope_reason": scope_reason,
            "scope_detail": scope_detail.get("scope"),
        }
        if fallback is None:
            by_id[record_id] = _unscore(
                original,
                "exact_snapshot_scope_unavailable; target_HEAD_unavailable",
                detail={"attempted_exact": attempted},
            )
            continue
        replacement = dict(original)
        replacement.update(fallback)
        replacement.update(
            {
                "target_tree_id": assigned,
                "indexable": True,
                "attempted_exact": attempted,
            }
        )
        by_id[record_id] = replacement

    # All HEAD fallbacks, both initial and exact-scope retries, must also prove
    # that the recorded scope exists before entering the scored population.
    head_rows = [
        row
        for row in by_id.values()
        if row.get("target_tree_id") and row.get("mode") == "head_fallback"
    ]
    head_failures = _scope_failures(
        records,
        head_rows,
        worktree_parent=worktree_parent,
        git_timeout=git_timeout,
        materializer=materializer,
    )
    for record_id, (reason, scope) in head_failures.items():
        row = by_id[record_id]
        prefix = (
            "exact_and_HEAD_scopes_unavailable"
            if row.get("attempted_exact")
            else "HEAD_snapshot_scope_unavailable"
        )
        by_id[record_id] = _unscore(
            row,
            f"{prefix}:{reason}",
            detail={"head_preflight": scope},
        )

    return [by_id[record_id] for record_id in sorted(by_id)]


def exclusion_rows(provenance: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in provenance:
        if item.get("target_tree_id") not in (None, ""):
            continue
        rows.append(
            {
                "schema_version": EXCLUSION_SCHEMA,
                "record_id": str(item["record_id"]),
                "windows_seconds": list(item.get("windows_seconds") or []),
                "assigned_target_tree_id": item.get("assigned_target_tree_id"),
                "mode": item.get("mode"),
                "reason": item.get("reason") or "unscored",
                "detail": item.get("exclusion_detail"),
            }
        )
    return rows


def _partial_current_mapping(row: Mapping[str, Any]) -> tuple[Path, str]:
    current_raw = row.get("current_root")
    logical_raw = row.get("logical_root")
    if not current_raw or not logical_raw:
        raise RuntimeError("current fallback lacks current_root or logical_root")
    current = Path(str(current_raw)).resolve(strict=True)
    logical = Path(str(logical_raw))
    if current.is_dir():
        return current, str(logical)
    if current.is_file():
        # scope_for_record requires a directory source root.  A file-valued
        # empirical tree is safely replayed from its parent while the recorded
        # absolute file path remains the scope restriction.
        return current.parent, str(logical.parent)
    raise RuntimeError("current fallback root is neither a file nor directory")


def preflight_partial_current_scopes(
    records: Mapping[str, Mapping[str, Any]],
    provenance: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in provenance:
        row = dict(raw)
        if "ripgrep" not in (row.get("partial_arms") or []):
            result.append(row)
            continue
        record_id = str(row["record_id"])
        try:
            source_root, logical_root = _partial_current_mapping(row)
            scope = arms.scope_for_record(
                records[record_id], logical_root=logical_root, source_root=source_root
            )
            if not (scope.get("in_scope") and scope.get("available")):
                row = _unscore(
                    row,
                    f"non_git_current_scope_unavailable:{scope.get('reason') or 'unknown'}",
                    detail={"scope": scope},
                )
                row["partial_arms"] = []
            else:
                row["partial_logical_root"] = logical_root
                row["partial_source_root"] = str(source_root)
        except Exception as error:
            row = _unscore(
                row,
                f"non_git_current_preflight_failed:{type(error).__name__}: {error}",
            )
            row["partial_arms"] = []
        result.append(row)
    return result


def preflight_index_states(
    provenance: Sequence[Mapping[str, Any]],
    *,
    scratch_dir: Path,
    git_timeout: float = 300.0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prove each selected snapshot can support the protected index.

    A state-level index failure is not converted into thousands of plausible
    empty per-query answers.  Its queries become explicitly unscored before
    the run fingerprint is fixed; actual query-level arm failures remain timed
    error rows later.
    """

    scratch_dir.mkdir(parents=True, exist_ok=True)
    scratch = scratch_dir.resolve(strict=True)
    worktrees = scratch / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    worktrees = worktrees.resolve(strict=True)
    database = (scratch / "active-index-v2.sqlite").resolve(strict=False)
    for candidate in (worktrees, database):
        if os.path.commonpath((str(candidate), str(scratch))) != str(scratch):
            raise RuntimeError("index preflight path escaped --scratch-dir")

    by_id = {str(row["record_id"]): dict(row) for row in provenance}
    stats: list[dict[str, Any]] = []
    for stream_key, states in _ordered_streams(list(by_id.values())):
        exemplar = states[0][1][0]
        stream_index_ready = False
        try:
            with owned_stream_worktree(exemplar, worktrees, timeout=git_timeout) as checkout:
                for key, state_rows in states:
                    state_exemplar = state_rows[0]
                    state_stat: dict[str, Any] = {
                        "state_id": _state_id(key),
                        "commit": key[2],
                        "queries": len(state_rows),
                    }
                    try:
                        source_root = checkout_owned_stream_state(
                            checkout, key[2], key[1], scratch, timeout=git_timeout
                        )
                        built = refresh_index(
                            database,
                            source_root,
                            logical_root=str(state_exemplar["logical_root"]),
                            force_full=not stream_index_ready,
                            expected_commit=key[2],
                            stream_identity=key[0],
                            repository_relative_root=key[1],
                        )
                        stream_index_ready = True
                        state_stat["index"] = built
                    except Exception as error:
                        reason = f"index_state_unavailable: {type(error).__name__}: {error}"
                        state_stat["error"] = reason
                        for row in state_rows:
                            record_id = str(row["record_id"])
                            by_id[record_id] = _unscore(
                                row,
                                reason,
                                detail={"state_id": _state_id(key)},
                            )
                    stats.append(state_stat)
        except Exception as error:
            # Failure to create the owned stream worktree affects every state
            # in the stream and is explicitly reflected in provenance.
            for key, state_rows in states:
                reason = f"index_stream_unavailable: {type(error).__name__}: {error}"
                stats.append(
                    {"state_id": _state_id(key), "commit": key[2], "queries": len(state_rows), "error": reason}
                )
                for row in state_rows:
                    by_id[str(row["record_id"])] = _unscore(
                        row, reason, detail={"state_id": _state_id(key)}
                    )
    return [by_id[key] for key in sorted(by_id)], {
        "states": stats,
        "state_count": len(stats),
        "failed_states": sum(bool(item.get("error")) for item in stats),
        "active_index_path": str(database),
    }


def run_fingerprint(
    eval_dir: Path,
    catalog_path: Path,
    provenance_path: Path,
) -> str:
    digest = hashlib.sha256()
    configuration = {
        "schema_version": RUN_SCHEMA,
        "windows_seconds": list(WINDOWS),
        "arms": list(ALL_ARMS),
        "top_k": TOP_K,
        "query_order_seed": QUERY_ORDER_SEED,
        "warmup_sentinel": WARMUP_SENTINEL,
    }
    digest.update(json.dumps(configuration, sort_keys=True).encode("utf-8") + b"\0")
    for window, filename in sorted(EVAL_FILENAMES.items()):
        path = eval_dir / filename
        digest.update(f"eval:{window}:{filename}".encode("utf-8") + b"\0")
        digest.update(sha256_file(path).encode("ascii") + b"\0")
    retention = eval_dir / "retention.json"
    if retention.exists():
        digest.update(b"retention\0" + sha256_file(retention).encode("ascii") + b"\0")
    for label, path in (("catalog", catalog_path), ("provenance", provenance_path)):
        digest.update(label.encode("ascii") + b"\0" + sha256_file(path).encode("ascii") + b"\0")
    implementation_dir = Path(__file__).resolve().parent
    for name in (
        "arms.py",
        "index.py",
        "run_v2.py",
        "history_v2.py",
        "incremental_index_v2.py",
        "metrics_v2.py",
        "provenance_v2.py",
        "tree_catalog_v2.py",
    ):
        digest.update(name.encode("ascii") + b"\0")
        digest.update((implementation_dir / name).read_bytes() + b"\0")
    return digest.hexdigest()


def _read_existing_runs(
    paths: Sequence[Path], expected_fingerprint: str
) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    selected_path = next((path for path in paths if path.exists()), None)
    if selected_path is None:
        return rows
    with selected_path.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                # Only a torn final line is a valid crash-recovery condition.
                if handle.read().strip():
                    raise ValueError(f"{selected_path}:{number}: corrupt JSONL") from error
                break
            if not isinstance(row, dict):
                raise ValueError(f"{selected_path}:{number}: row is not an object")
            if row.get("fingerprint") != expected_fingerprint:
                raise RuntimeError(
                    f"{selected_path} belongs to a different run fingerprint; "
                    "use a different output directory"
                )
            record_id = str(row.get("record_id") or "")
            arm = str(row.get("arm") or "")
            if not record_id or arm not in ALL_ARMS:
                raise ValueError(f"{selected_path}:{number}: invalid run row key")
            key = (record_id, arm)
            if key in rows:
                raise ValueError(f"{selected_path}:{number}: duplicate run row {key}")
            rows[key] = row
    return rows


def _error_result(message: str) -> dict[str, Any]:
    payload = f"[error] {message}\n".encode("utf-8")
    return {
        "ranked_paths": [],
        "response_bytes": len(payload),
        "response_sha256": hashlib.sha256(payload).hexdigest(),
        "payload": payload,
        "error": message,
        "metadata": {},
    }


def serialise_unavailable_result(
    record_id: str,
    arm_name: str,
    reason: str,
    fingerprint: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Represent an arm that could not be attempted without inventing metrics."""

    return {
        "schema_version": RUN_SCHEMA,
        "record_id": record_id,
        "arm": arm_name,
        "fingerprint": fingerprint,
        "tree_id": provenance.get("target_tree_id")
        or provenance.get("assigned_target_tree_id"),
        "logical_root": provenance.get("logical_root"),
        "state_id": _state_id(_state_key(provenance)),
        "reconstruction_mode": provenance.get("mode"),
        "commit": provenance.get("commit"),
        "ranked_paths": [],
        "returned_paths": None,
        "response_bytes": None,
        "response_sha256": None,
        "latency_ms": None,
        "error": None,
        "diagnostic": None,
        "unavailable": True,
        "unavailable_reason": reason,
    }


def serialise_arm_result(
    record_id: str,
    arm_name: str,
    result: Mapping[str, Any],
    latency_ms: float,
    fingerprint: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    payload = result.get("response", result.get("payload", b""))
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, (bytes, bytearray)):
        payload = b""
    response_bytes = int(result.get("response_bytes", len(payload)))
    response_hash = str(result.get("response_sha256") or hashlib.sha256(bytes(payload)).hexdigest())
    ranked = [str(path) for path in (result.get("ranked_paths") or [])]
    return {
        "schema_version": RUN_SCHEMA,
        "record_id": record_id,
        "arm": arm_name,
        "fingerprint": fingerprint,
        "tree_id": provenance.get("target_tree_id") or provenance.get("assigned_target_tree_id"),
        "logical_root": provenance.get("logical_root"),
        "state_id": _state_id(_state_key(provenance)),
        "reconstruction_mode": provenance.get("mode"),
        "commit": provenance.get("commit"),
        "ranked_paths": ranked,
        "returned_paths": len(ranked),
        "response_bytes": response_bytes,
        "response_sha256": response_hash,
        # Outer measurement includes response serialization performed by the arm.
        "latency_ms": latency_ms,
        "error": result.get("error"),
        "diagnostic": result.get("diagnostic", result.get("metadata")),
    }


def _warm_ripgrep(source_root: Path) -> dict[str, Any]:
    query = {"pattern": WARMUP_SENTINEL, "output_mode": "files_with_matches"}
    argv = arms.ripgrep_argv(query, str(source_root))
    started = time.perf_counter()
    completed = subprocess.run(
        argv,
        cwd=str(source_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        check=False,
        shell=False,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode not in (0, 1):
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ripgrep warmup failed ({completed.returncode}): {detail}")
    return {
        "sentinel_sha256": hashlib.sha256(WARMUP_SENTINEL.encode()).hexdigest(),
        "elapsed_seconds": elapsed,
        "exit_code": completed.returncode,
        "stdout_bytes": len(completed.stdout),
        "stderr_bytes": len(completed.stderr),
        "query_and_label_independent": True,
    }


def _warm_index(connection: Any) -> dict[str, Any]:
    started = time.perf_counter()
    files = int(connection.execute("SELECT count(*) FROM files").fetchone()[0])
    chunks = int(connection.execute("SELECT count(*) FROM chunks").fetchone()[0])
    for arm_name in ("bm25", "ident_first", "bm25_pathboost", "bm25_legacy"):
        index.query_index(connection, WARMUP_SENTINEL, arm_name, TOP_K)
    return {
        "files": files,
        "chunks": chunks,
        "elapsed_seconds": time.perf_counter() - started,
        "query_and_label_independent": True,
    }


def _ordered_states(
    provenance: Sequence[Mapping[str, Any]],
) -> list[tuple[tuple[str, str, str], list[Mapping[str, Any]]]]:
    grouped = _group_rows_by_state(provenance)

    def key(item: tuple[tuple[str, str, str], list[Mapping[str, Any]]]) -> tuple[Any, ...]:
        state, rows = item
        commit_times = [
            float(row["commit_ts"])
            for row in rows
            if isinstance(row.get("commit_ts"), (int, float))
        ]
        return (state[0], state[1], min(commit_times) if commit_times else math.inf, state[2])

    ordered: list[tuple[tuple[str, str, str], list[Mapping[str, Any]]]] = []
    for state, rows in sorted(grouped.items(), key=key):
        state_seed = int(hashlib.sha256(_state_id(state).encode()).hexdigest()[:16], 16)
        shuffled = list(sorted(rows, key=lambda row: str(row["record_id"])))
        random.Random(QUERY_ORDER_SEED ^ state_seed).shuffle(shuffled)
        ordered.append((state, shuffled))
    return ordered


def _ordered_streams(
    provenance: Sequence[Mapping[str, Any]],
) -> list[
    tuple[
        tuple[str, str],
        list[tuple[tuple[str, str, str], list[Mapping[str, Any]]]],
    ]
]:
    streams: dict[
        tuple[str, str],
        list[tuple[tuple[str, str, str], list[Mapping[str, Any]]]],
    ] = defaultdict(list)
    for state, rows in _ordered_states(provenance):
        streams[(state[0], state[1])].append((state, rows))
    return [(key, streams[key]) for key in sorted(streams)]


@contextlib.contextmanager
def owned_stream_worktree(
    exemplar: Mapping[str, Any],
    worktree_parent: Path,
    *,
    timeout: float,
) -> Iterator[Path]:
    """Keep one owned detached worktree for a chronological state stream."""

    repository = Path(str(exemplar["repository_root"])).resolve(strict=True)
    initial_commit = str(exemplar["commit"])
    worktree_parent.mkdir(parents=True, exist_ok=True)
    parent = worktree_parent.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="retrieval-v2-stream-", dir=parent) as temporary:
        container = Path(temporary).resolve(strict=True)
        if os.path.commonpath((str(container), str(parent))) != str(parent):
            raise RuntimeError("stream container escaped scratch boundary")
        checkout = container / "repo"
        _run_git(
            repository,
            (
                "-c",
                "core.longpaths=true",
                "worktree",
                "add",
                "--detach",
                str(checkout),
                initial_commit,
            ),
            timeout=timeout,
        )
        try:
            yield checkout.resolve(strict=True)
        finally:
            removed = _run_git(
                repository,
                (
                    "-c",
                    "core.longpaths=true",
                    "worktree",
                    "remove",
                    "--force",
                    str(checkout),
                ),
                timeout=timeout,
                check=False,
            )
            if removed.returncode and checkout.exists():
                resolved = checkout.resolve(strict=False)
                if os.path.commonpath((str(resolved), str(container))) != str(container):
                    raise RuntimeError("refusing stream cleanup outside owned container")
                shutil.rmtree(resolved)


def checkout_owned_stream_state(
    checkout: Path,
    commit: str,
    repository_relative_root: str,
    scratch_root: Path,
    *,
    timeout: float,
) -> Path:
    """Reset/clean only a verified disposable worktree, then return its subtree."""

    resolved = checkout.resolve(strict=True)
    scratch = scratch_root.resolve(strict=True)
    if os.path.commonpath((str(resolved), str(scratch))) != str(scratch):
        raise RuntimeError("refusing reset outside --scratch-dir")
    if not (resolved / ".git").exists():
        raise RuntimeError("owned stream path is no longer a Git worktree")
    _run_git(
        resolved,
        ("-c", "core.longpaths=true", "reset", "--hard", str(commit)),
        timeout=timeout,
    )
    _run_git(
        resolved,
        ("-c", "core.longpaths=true", "clean", "-ffdx"),
        timeout=timeout,
    )
    actual = _run_git(resolved, ("rev-parse", "HEAD"), timeout=timeout).stdout.strip()
    if actual != str(commit):
        raise RuntimeError(f"owned stream reset selected {actual}, expected {commit}")
    relative = _safe_relative(repository_relative_root)
    source = resolved.joinpath(*relative.split("/")) if relative else resolved
    if not source.is_dir():
        raise RuntimeError(f"repository subtree absent after reset: {relative or '.'}")
    source = source.resolve(strict=True)
    if os.path.commonpath((str(source), str(resolved))) != str(resolved):
        raise RuntimeError("stream subtree escaped owned worktree")
    return source


class _RunPersistenceError(RuntimeError):
    """A benchmark artifact could not be written or serialized safely."""


def execute_runs(
    records: Mapping[str, Mapping[str, Any]],
    provenance: Sequence[Mapping[str, Any]],
    *,
    runs_path: Path,
    fingerprint: str,
    scratch_dir: Path,
    git_timeout: float = 300.0,
    progress_every: int = 100,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resume and execute all arms, reusing one owned worktree per stream."""

    selected = [row for row in provenance if row.get("target_tree_id")]
    partial_selected = [
        row
        for row in provenance
        if not row.get("target_tree_id") and "ripgrep" in (row.get("partial_arms") or [])
    ]
    expected = {
        (str(row["record_id"]), arm_name)
        for row in selected
        for arm_name in ALL_ARMS
    }
    expected.update((str(row["record_id"]), "ripgrep") for row in partial_selected)
    partial_path = runs_path.with_suffix(runs_path.suffix + ".partial")
    existing = _read_existing_runs((partial_path, runs_path), fingerprint)
    completed = {key: row for key, row in existing.items() if key in expected}
    scratch_dir.mkdir(parents=True, exist_ok=True)
    scratch = scratch_dir.resolve(strict=True)
    worktrees = scratch / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    worktrees = worktrees.resolve(strict=True)
    db_path = (scratch / "active-index-v2.sqlite").resolve(strict=False)
    for candidate in (worktrees, db_path):
        if os.path.commonpath((str(candidate), str(scratch))) != str(scratch):
            raise RuntimeError("execution path escaped --scratch-dir")

    _atomic_jsonl(partial_path, (completed[key] for key in sorted(completed)))
    state_stats: list[dict[str, Any]] = []
    full_builds = incremental_builds = 0
    query_ordinal = 0
    total_queries = len(selected) + len(partial_selected)
    done_queries = sum(
        all((str(row["record_id"]), arm) in completed for arm in ALL_ARMS)
        for row in selected
    )
    done_queries += sum(
        (str(row["record_id"]), "ripgrep") in completed for row in partial_selected
    )

    with partial_path.open("a", encoding="utf-8", newline="\n") as output:
        def append_unavailable(
            rows: Sequence[Mapping[str, Any]],
            arm_names: Sequence[str],
            reason: str,
        ) -> int:
            """Durably fill missing pairs with non-metric availability rows."""

            nonlocal done_queries
            added = 0
            try:
                for row in rows:
                    record_id = str(row["record_id"])
                    required = (
                        tuple(ALL_ARMS)
                        if row.get("target_tree_id")
                        else tuple(str(arm) for arm in (row.get("partial_arms") or []))
                    )
                    was_complete = all((record_id, arm) in completed for arm in required)
                    for arm_name in arm_names:
                        pair = (record_id, arm_name)
                        if pair in completed:
                            continue
                        run_row = serialise_unavailable_result(
                            record_id,
                            arm_name,
                            reason,
                            fingerprint,
                            row,
                        )
                        completed[pair] = run_row
                        output.write(
                            json.dumps(
                                run_row,
                                sort_keys=True,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            + "\n"
                        )
                        added += 1
                    if not was_complete and all(
                        (record_id, arm) in completed for arm in required
                    ):
                        done_queries += 1
                output.flush()
            except Exception as error:
                raise _RunPersistenceError(
                    f"could not persist unavailable run rows: {type(error).__name__}: {error}"
                ) from error
            if added and (
                done_queries % progress_every == 0 or done_queries == total_queries
            ):
                print(
                    f"scored {done_queries}/{total_queries} union queries; "
                    f"{len(completed)}/{len(expected)} arm rows",
                    file=sys.stderr,
                    flush=True,
                )
            return added

        for stream_key, states in _ordered_streams(selected):
            pending_states = [
                (state, rows)
                for state, rows in states
                if not {
                    (str(row["record_id"]), arm) for row in rows for arm in ALL_ARMS
                }.issubset(completed)
            ]
            # Advance ordinals for all states, including resumed ones, so arm
            # rotation is invariant under interruption.
            ordinal_by_id: dict[str, int] = {}
            for _, rows in states:
                for row in rows:
                    query_ordinal += 1
                    ordinal_by_id[str(row["record_id"])] = query_ordinal
            if not pending_states:
                for state, rows in states:
                    state_stats.append(
                        {"state_id": _state_id(state), "commit": state[2], "queries": len(rows), "resumed": True}
                    )
                continue

            stream_exemplar = pending_states[0][1][0]
            try:
                with owned_stream_worktree(
                    stream_exemplar, worktrees, timeout=git_timeout
                ) as checkout:
                    stream_index_ready = False
                    for state_key, state_rows in states:
                        state_expected = {
                            (str(row["record_id"]), arm) for row in state_rows for arm in ALL_ARMS
                        }
                        if state_expected.issubset(completed):
                            state_stats.append(
                                {"state_id": _state_id(state_key), "commit": state_key[2], "queries": len(state_rows), "resumed": True}
                            )
                            continue
                        exemplar = state_rows[0]
                        state_stat: dict[str, Any] = {
                            "state_id": _state_id(state_key),
                            "stream_id": hashlib.sha256("\0".join(stream_key).encode()).hexdigest()[:16],
                            "repository_identity": state_key[0],
                            "repository_relative_root": state_key[1],
                            "commit": state_key[2],
                            "commit_ts": exemplar.get("commit_ts"),
                            "queries": len(state_rows),
                            "resumed_pairs": len(state_expected & completed.keys()),
                        }
                        connection = None
                        state_ready = False
                        try:
                            source_root = checkout_owned_stream_state(
                                checkout,
                                state_key[2],
                                state_key[1],
                                scratch,
                                timeout=git_timeout,
                            )
                            built = refresh_index(
                                db_path,
                                source_root,
                                logical_root=str(exemplar["logical_root"]),
                                force_full=not stream_index_ready,
                                expected_commit=state_key[2],
                                stream_identity=state_key[0],
                                repository_relative_root=state_key[1],
                                git_timeout=git_timeout,
                            )
                            stream_index_ready = True
                            state_stat["index"] = built
                            if built.get("mode") == "full":
                                full_builds += 1
                            else:
                                incremental_builds += 1
                            connection = index.connect_index(db_path)
                            warmup: dict[str, Any] = {
                                "ripgrep_traversal": _warm_ripgrep(source_root),
                                "index_database": _warm_index(connection),
                                "included_in_query_latency": False,
                            }
                            state_stat["warmup"] = warmup
                            state_ready = True

                            for row in state_rows:
                                record_id = str(row["record_id"])
                                query_was_complete = all(
                                    (record_id, arm) in completed for arm in ALL_ARMS
                                )
                                rotation = (ordinal_by_id[record_id] - 1) % len(ALL_ARMS)
                                arm_order = ALL_ARMS[rotation:] + ALL_ARMS[:rotation]
                                for arm_name in arm_order:
                                    pair = (record_id, arm_name)
                                    if pair in completed:
                                        continue
                                    started = time.perf_counter_ns()
                                    try:
                                        result = arms.run_arm(
                                            arm_name,
                                            records[record_id],
                                            conn=connection,
                                            source_root=source_root,
                                            logical_root=str(row["logical_root"]),
                                            top_k=TOP_K,
                                        )
                                    except Exception as error:
                                        # A genuine query-level failure remains
                                        # a timed row and contributes to error rate.
                                        result = _error_result(f"{type(error).__name__}: {error}")
                                    elapsed = (time.perf_counter_ns() - started) / 1_000_000
                                    run_row = serialise_arm_result(
                                        record_id, arm_name, result, elapsed, fingerprint, row
                                    )
                                    completed[pair] = run_row
                                    output.write(
                                        json.dumps(run_row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
                                    )
                                output.flush()
                                if not query_was_complete:
                                    done_queries += 1
                                if done_queries % progress_every == 0 or done_queries == total_queries:
                                    print(
                                        f"scored {done_queries}/{total_queries} union queries; {len(completed)}/{len(expected)} arm rows",
                                        file=sys.stderr,
                                        flush=True,
                                    )
                        except Exception as error:
                            state_stat["state_error"] = f"{type(error).__name__}: {error}"
                            if state_ready:
                                # Arm exceptions are converted to timed error
                                # rows above.  Anything else after readiness is
                                # an artifact/integrity failure, not arm
                                # unavailability that may be papered over.
                                raise _RunPersistenceError(
                                    f"state {_state_id(state_key)} failed after setup: "
                                    f"{type(error).__name__}: {error}"
                                ) from error
                            reason = (
                                f"state infrastructure unavailable: "
                                f"{type(error).__name__}: {error}"
                            )
                            state_stat["unavailable_arm_rows"] = append_unavailable(
                                state_rows, ALL_ARMS, reason
                            )
                            # A failed full build or interrupted delta cannot
                            # be the base for the next state.
                            stream_index_ready = False
                        finally:
                            if connection is not None:
                                connection.close()
                        state_stats.append(state_stat)
            except _RunPersistenceError:
                raise
            except Exception as error:
                reason = (
                    f"stream worktree unavailable: {type(error).__name__}: {error}"
                )
                unavailable_rows = 0
                for _, state_rows in states:
                    unavailable_rows += append_unavailable(
                        state_rows, ALL_ARMS, reason
                    )
                state_stats.append(
                    {
                        "stream_id": hashlib.sha256(
                            "\0".join(stream_key).encode()
                        ).hexdigest()[:16],
                        "repository_identity": stream_key[0],
                        "repository_relative_root": stream_key[1],
                        "stream_error": reason,
                        "unavailable_arm_rows": unavailable_rows,
                    }
                )

        partial_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in partial_selected:
            partial_groups[
                (str(row["partial_source_root"]), str(row["partial_logical_root"]))
            ].append(row)
        for (source_text, logical_root), rows in sorted(partial_groups.items()):
            pending_rows = [
                row
                for row in rows
                if (str(row["record_id"]), "ripgrep") not in completed
            ]
            if not pending_rows:
                state_stats.append(
                    {
                        "state_id": "current-non-git-" + hashlib.sha256(
                            (source_text + "\0" + logical_root).encode()
                        ).hexdigest()[:16],
                        "mode": "non_git_current_fallback",
                        "queries": len(rows),
                        "partial_arms": ["ripgrep"],
                        "resumed": True,
                    }
                )
                continue
            group_stat: dict[str, Any] = {
                "state_id": "current-non-git-" + hashlib.sha256(
                    (source_text + "\0" + logical_root).encode()
                ).hexdigest()[:16],
                "mode": "non_git_current_fallback",
                "queries": len(rows),
                "partial_arms": ["ripgrep"],
            }
            partial_ready = False
            try:
                source_root = Path(source_text).resolve(strict=True)
                group_stat["warmup"] = {
                    "ripgrep_traversal": _warm_ripgrep(source_root),
                    "included_in_query_latency": False,
                }
                partial_ready = True
                for row in sorted(pending_rows, key=lambda item: str(item["record_id"])):
                    record_id = str(row["record_id"])
                    pair = (record_id, "ripgrep")
                    if pair in completed:
                        continue
                    started = time.perf_counter_ns()
                    try:
                        result = arms.run_arm(
                            "ripgrep",
                            records[record_id],
                            conn=None,
                            source_root=source_root,
                            logical_root=logical_root,
                            top_k=TOP_K,
                        )
                    except Exception as error:
                        result = _error_result(f"{type(error).__name__}: {error}")
                    elapsed = (time.perf_counter_ns() - started) / 1_000_000
                    run_row = serialise_arm_result(
                        record_id, "ripgrep", result, elapsed, fingerprint, row
                    )
                    # A file-valued replay executes from its parent, but truth
                    # filtering remains anchored to the original file-valued
                    # logical root so sibling Reads cannot become relevant.
                    run_row["execution_logical_root"] = logical_root
                    completed[pair] = run_row
                    output.write(
                        json.dumps(run_row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
                    output.flush()
                    done_queries += 1
                    if done_queries % progress_every == 0 or done_queries == total_queries:
                        print(
                            f"scored {done_queries}/{total_queries} union queries; {len(completed)}/{len(expected)} arm rows",
                            file=sys.stderr,
                            flush=True,
                        )
            except Exception as error:
                group_stat["state_error"] = f"{type(error).__name__}: {error}"
                if partial_ready:
                    raise _RunPersistenceError(
                        "non-Git current fallback failed after setup: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                reason = (
                    "non-Git current ripgrep unavailable: "
                    f"{type(error).__name__}: {error}"
                )
                group_stat["unavailable_arm_rows"] = append_unavailable(
                    rows, ("ripgrep",), reason
                )
            state_stats.append(group_stat)

    missing = expected - completed.keys()
    if missing:
        raise RuntimeError(f"run ended with {len(missing)} missing arm rows")
    if completed.keys() - expected:
        raise RuntimeError(f"run contains {len(completed.keys() - expected)} unexpected arm rows")
    final_rows = [completed[key] for key in sorted(expected)]
    _atomic_jsonl(runs_path, final_rows)
    if partial_path.exists():
        partial_path.unlink()
    return final_rows, {
        "selected_queries": len(selected),
        "partial_control_queries": len(partial_selected),
        "arm_rows": len(final_rows),
        "resumed_arm_rows": len(existing.keys() & expected),
        "states": state_stats,
        "state_count": len(state_stats),
        "stream_count": len(_ordered_streams(selected)),
        "index_full_refreshes": full_builds,
        "index_incremental_refreshes": incremental_builds,
        "active_index_path": str(db_path),
    }


def _artifact_descriptor(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"path": str(path.resolve()), "sha256": sha256_file(path)}
    if rows is not None:
        value["rows"] = rows
    return value


def _mutable_state(provenance: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    values: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in provenance:
        repository = row.get("repository_root")
        commit = row.get("commit")
        if not repository or not commit:
            continue
        if row.get("mode") == "historical_exact" and row.get("resolved_ref"):
            key = (str(repository), "ref", str(row["resolved_ref"]))
            values[key] = {
                "repository_root": str(repository),
                "kind": "ref",
                "name": str(row["resolved_ref"]),
                "expected": row.get("resolved_ref_tip"),
            }
        elif row.get("mode") == "head_fallback":
            key = (str(repository), "head", "HEAD")
            values[key] = {
                "repository_root": str(repository),
                "kind": "head",
                "name": "HEAD",
                "expected": str(commit),
            }
        for evidence in row.get("epoch_candidate_evidence") or []:
            if not isinstance(evidence, Mapping) or not evidence.get("eligible"):
                continue
            resolved_ref = evidence.get("resolved_ref")
            expected_tip = evidence.get("resolved_ref_tip")
            repository = evidence.get("repository_root")
            # Older evidence rows may not carry a repository root; recover it
            # from the selected row only when the identities match.
            if not repository and evidence.get("repository_identity") == row.get("repository_identity"):
                repository = row.get("repository_root")
            if repository and resolved_ref and expected_tip:
                key = (str(repository), "ref", str(resolved_ref))
                values[key] = {
                    "repository_root": str(repository),
                    "kind": "epoch_candidate_ref",
                    "name": str(resolved_ref),
                    "expected": str(expected_tip),
                }
    return [values[key] for key in sorted(values)]


def _validate_mutable_state(values: Sequence[Mapping[str, Any]]) -> None:
    for item in values:
        repository = str(item["repository_root"])
        name = str(item["name"])
        expected = str(item.get("expected") or "")
        completed = _run_git(repository, ("rev-parse", "--verify", name), check=False)
        actual = completed.stdout.strip() if completed.returncode == 0 else ""
        if not expected or actual != expected:
            raise RuntimeError(
                f"prepared mutable Git state changed for {repository} {name}: "
                f"expected {expected or '<missing>'}, found {actual or '<missing>'}; "
                "rerun with --reprepare before scoring"
            )


def prepare_run(
    eval_dir: Path,
    output_dir: Path,
    scratch_dir: Path,
    *,
    git_timeout: float,
) -> tuple[
    dict[int, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    evalsets = load_evalsets(eval_dir)
    if tuple(sorted(evalsets)) != WINDOWS:
        raise ValueError(f"eval windows must be exactly {WINDOWS}, found {tuple(sorted(evalsets))}")
    records, membership = union_records(evalsets)
    build = build_catalog(evalsets)
    catalog_path, _ = write_catalog_outputs(build, output_dir)
    history = GitHistoryCache(timeout=git_timeout)
    initial = reconstruct_union(records, membership, build, cache=history)
    scoped = preflight_reconstructed_scopes(
        records,
        initial,
        build,
        cache=history,
        worktree_parent=scratch_dir / "worktrees",
        git_timeout=git_timeout,
    )
    # Index construction is an execution concern, not provenance.  The former
    # implementation built every historical state here and then built every
    # state again during scoring; an interruption before the prepare plan was
    # written lost all of that work.  Scope/materialisation is already checked
    # above.  Scoring owns the durable, resumable index pass and records any
    # state-level arm unavailability explicitly.
    provenance = scoped
    index_preflight = {
        "performed": False,
        "reason": "index states are built once in the resumable scoring pass",
        "state_count": len(_group_rows_by_state(provenance)),
        "failed_states": None,
    }
    provenance = preflight_partial_current_scopes(records, provenance)
    provenance_path = output_dir / "reconstruction-v2.jsonl"
    exclusions_path = output_dir / "exclusions-v2.jsonl"
    exclusions = exclusion_rows(provenance)
    _atomic_jsonl(provenance_path, provenance)
    _atomic_jsonl(exclusions_path, exclusions)
    fingerprint = run_fingerprint(eval_dir, catalog_path, provenance_path)
    plan = {
        "schema_version": RUN_SCHEMA,
        "generated_utc": _utc_now(),
        "fingerprint": fingerprint,
        "windows_seconds": list(WINDOWS),
        "arms": list(ALL_ARMS),
        "query_cap": None,
        "artifacts": {
            "catalog": _artifact_descriptor(catalog_path),
            "provenance": _artifact_descriptor(provenance_path, rows=len(provenance)),
            "exclusions": _artifact_descriptor(exclusions_path, rows=len(exclusions)),
            "retention": _artifact_descriptor(eval_dir / "retention.json"),
            "evalsets": {
                str(window): _artifact_descriptor(eval_dir / EVAL_FILENAMES[window])
                for window in WINDOWS
            },
        },
        "mutable_git_state": _mutable_state(provenance),
        "index_preflight": index_preflight,
        "notes": [
            "Branch resolution is local-first and otherwise accepts only an unambiguous remote suffix.",
            "Historical selection is limited to ancestry still reachable from surviving local/remote refs.",
            "Clean commits cannot reconstruct dirty or untracked contemporaneous state.",
        ],
    }
    _atomic_json(output_dir / "prepare-plan-v2.json", plan)
    return evalsets, records, provenance, exclusions, plan


def load_prepared_run(
    eval_dir: Path, output_dir: Path
) -> tuple[
    dict[int, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    plan_path = output_dir / "prepare-plan-v2.json"
    if not plan_path.exists():
        raise RuntimeError("prepare-plan-v2.json is absent; run preparation first")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != RUN_SCHEMA:
        raise RuntimeError("prepared plan has an incompatible schema")
    evalsets = load_evalsets(eval_dir)
    records, _ = union_records(evalsets)
    provenance_path = output_dir / "reconstruction-v2.jsonl"
    exclusions_path = output_dir / "exclusions-v2.jsonl"
    catalog_path = output_dir / "tree-catalog-v2.json"
    provenance = _load_jsonl(provenance_path)
    exclusions = _load_jsonl(exclusions_path)
    expected_hashes = plan.get("artifacts") or {}
    checks = {
        "catalog": catalog_path,
        "provenance": provenance_path,
        "exclusions": exclusions_path,
        "retention": eval_dir / "retention.json",
    }
    for name, path in checks.items():
        expected = str((expected_hashes.get(name) or {}).get("sha256") or "")
        actual = sha256_file(path)
        if not expected or expected != actual:
            raise RuntimeError(f"prepared artifact {name} changed; rerun with --reprepare")
    for window in WINDOWS:
        descriptor = ((expected_hashes.get("evalsets") or {}).get(str(window)) or {})
        expected = str(descriptor.get("sha256") or "")
        actual = sha256_file(eval_dir / EVAL_FILENAMES[window])
        if not expected or expected != actual:
            raise RuntimeError(f"evalset {window}s changed; rerun with --reprepare")
    fingerprint = run_fingerprint(eval_dir, catalog_path, provenance_path)
    if fingerprint != plan.get("fingerprint"):
        raise RuntimeError("runner implementation or prepared inputs changed; rerun with --reprepare")
    _validate_mutable_state(plan.get("mutable_git_state") or [])
    if set(records) != {str(row.get("record_id")) for row in provenance}:
        raise RuntimeError("prepared provenance is not one-to-one with union retained IDs")
    return evalsets, records, provenance, exclusions, plan


def _arm_unavailability_by_tree(
    run_rows: Sequence[Mapping[str, Any]],
    provenance: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = defaultdict(dict)
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in run_rows:
        grouped[(str(row.get("tree_id") or ""), str(row.get("arm") or ""))].append(row)
    for (tree_id, arm), rows in grouped.items():
        explicit = [row for row in rows if row.get("unavailable") is True]
        if tree_id and arm and explicit:
            reasons = Counter(
                str(row.get("unavailable_reason") or "unspecified infrastructure failure")
                for row in explicit
            )
            reason, count = reasons.most_common(1)[0]
            suffix = "" if len(reasons) == 1 else f"; {len(reasons)} distinct reasons"
            scope = "all" if len(explicit) == len(rows) else f"{len(explicit)} of {len(rows)}"
            result[tree_id][arm] = f"{scope} rows unavailable: {reason}{suffix}"
        elif tree_id and arm and rows and all(row.get("error") for row in rows):
            reasons = Counter(str(row.get("error")) for row in rows)
            reason, count = reasons.most_common(1)[0]
            suffix = "" if len(reasons) == 1 else f"; {len(reasons)} distinct errors"
            result[tree_id][arm] = f"all {count if len(reasons) == 1 else len(rows)} rows failed: {reason}{suffix}"

    assigned: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    scored_tree_ids = {str(row.get("tree_id")) for row in run_rows if row.get("tree_id")}
    for row in provenance:
        tree_id = row.get("assigned_target_tree_id")
        if tree_id:
            assigned[str(tree_id)].append(row)
    for tree_id, rows in assigned.items():
        if tree_id in scored_tree_ids:
            partial_arms = {
                str(arm)
                for row in rows
                for arm in (row.get("partial_arms") or [])
            }
            if partial_arms:
                for arm in ALL_ARMS:
                    if arm not in partial_arms:
                        result[tree_id][arm] = (
                            "protected index requires a Git worktree; only the current-tree "
                            "ripgrep control could run"
                        )
            continue
        reason_counts = Counter(str(row.get("reason") or "unscored") for row in rows)
        reason = reason_counts.most_common(1)[0][0]
        for arm in ALL_ARMS:
            result[tree_id][arm] = f"tree has no scored rows: {reason}"
    return {tree: dict(sorted(arms_by_name.items())) for tree, arms_by_name in sorted(result.items())}


def _partial_arm_rows_by_tree(
    run_rows: Sequence[Mapping[str, Any]],
    provenance: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, int]]]:
    partial_windows = {
        str(row["record_id"]): tuple(str(value) for value in (row.get("windows_seconds") or []))
        for row in provenance
        if not row.get("target_tree_id") and row.get("partial_arms")
    }
    counts: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    for row in run_rows:
        if row.get("unavailable") is True:
            continue
        record_id = str(row.get("record_id"))
        if record_id not in partial_windows:
            continue
        tree_id = str(row.get("tree_id") or "")
        arm = str(row.get("arm") or "")
        if tree_id and arm:
            for window in partial_windows[record_id]:
                counts[window][tree_id][arm] += 1
    return {
        str(window): {
            tree: dict(sorted(arm_counts.items()))
            for tree, arm_counts in sorted(counts[str(window)].items())
        }
        for window in WINDOWS
    }


def _validate_complete_rows(
    run_rows: Sequence[Mapping[str, Any]],
    provenance: Sequence[Mapping[str, Any]],
    fingerprint: str,
) -> None:
    expected = {
        (str(row["record_id"]), arm)
        for row in provenance
        if row.get("target_tree_id")
        for arm in ALL_ARMS
    }
    expected.update(
        (str(row["record_id"]), "ripgrep")
        for row in provenance
        if not row.get("target_tree_id") and "ripgrep" in (row.get("partial_arms") or [])
    )
    actual: set[tuple[str, str]] = set()
    for row in run_rows:
        if row.get("fingerprint") != fingerprint:
            raise RuntimeError("run row fingerprint differs from prepared plan")
        key = (str(row.get("record_id") or ""), str(row.get("arm") or ""))
        if key in actual:
            raise RuntimeError(f"duplicate run pair {key}")
        actual.add(key)
    if actual != expected:
        raise RuntimeError(
            f"refusing incomplete final summary: expected {len(expected)} arm rows, "
            f"found {len(actual)} ({len(expected - actual)} missing, {len(actual - expected)} extra)"
        )


def write_metrics_and_summary(
    evalsets: Mapping[int, Sequence[Mapping[str, Any]]],
    provenance: Sequence[Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, Any]],
    run_rows: Sequence[Mapping[str, Any]],
    plan: Mapping[str, Any],
    *,
    output_dir: Path,
    execution_stats: Mapping[str, Any] | None,
) -> tuple[Path, Path]:
    fingerprint = str(plan["fingerprint"])
    _validate_complete_rows(run_rows, provenance, fingerprint)
    metrics = aggregate_metrics_v2(evalsets, run_rows, provenance, arms=ALL_ARMS)
    metrics["run_fingerprint"] = fingerprint
    metrics["generated_utc"] = _utc_now()
    metrics_path = output_dir / "metrics-v2.json"
    _atomic_json(metrics_path, metrics)

    runs_path = output_dir / "runs-v2.jsonl"
    provenance_path = output_dir / "reconstruction-v2.jsonl"
    exclusions_path = output_dir / "exclusions-v2.jsonl"
    catalog_path = output_dir / "tree-catalog-v2.json"
    exact_by_window: dict[str, int] = {}
    fallback_by_window: dict[str, int] = {}
    unscored_by_window: dict[str, int] = {}
    non_git_by_window: dict[str, int] = {}
    non_git_unscored_by_window: dict[str, int] = {}
    by_id = {str(row["record_id"]): row for row in provenance}
    for window in WINDOWS:
        ids = [str(record["id"]) for record in evalsets[window]]
        values = [by_id[record_id] for record_id in ids]
        exact_by_window[str(window)] = sum(row.get("exact") is True for row in values)
        fallback_by_window[str(window)] = sum(
            str(row.get("mode")) in FALLBACK_MODES for row in values
        )
        unscored_by_window[str(window)] = len(values) - (
            exact_by_window[str(window)] + fallback_by_window[str(window)]
        )
        non_git_by_window[str(window)] = sum(
            row.get("mode") == "non_git_current_fallback" for row in values
        )
        non_git_unscored_by_window[str(window)] = sum(
            row.get("mode") == "non_git_current_fallback" and not row.get("target_tree_id")
            for row in values
        )

    execution = dict(execution_stats or {})
    execution.update(
        {
            "query_order_seed": QUERY_ORDER_SEED,
            "warmup": {
                "policy": "fixed no-match ripgrep traversal and fixed no-match SQLite queries per state",
                "included_in_query_latency": False,
            },
            "index_caps": {
                "max_file_bytes": index.MAX_FILE_BYTES,
                "max_line_bytes": index.MAX_LINE_BYTES,
                "chunk_bytes": index.CHUNK_BYTES,
                "chunk_overlap_bytes": index.CHUNK_OVERLAP_BYTES,
                "snap_tolerance_bytes": index.SNAP_TOLERANCE_BYTES,
            },
            "preflight": plan.get("index_preflight"),
        }
    )
    summary = {
        "schema_version": RUN_SCHEMA,
        "generated_utc": _utc_now(),
        "fingerprint": fingerprint,
        "complete": True,
        "debug": False,
        "final": True,
        "windows_seconds": list(WINDOWS),
        "arms": list(ALL_ARMS),
        "query_cap": None,
        "artifacts": {
            "runs": _artifact_descriptor(runs_path, rows=len(run_rows)),
            "provenance": _artifact_descriptor(provenance_path, rows=len(provenance)),
            "exclusions": _artifact_descriptor(exclusions_path, rows=len(exclusions)),
            "metrics": _artifact_descriptor(metrics_path),
            "catalog": _artifact_descriptor(catalog_path),
            # Prepared-plan reuse hash-checks this descriptor before scoring.
            "retention": dict(plan["artifacts"]["retention"]),
        },
        "execution": execution,
        "reconstruction": {
            "exact_queries_by_window": exact_by_window,
            "fallback_queries_by_window": fallback_by_window,
            "unscored_or_unavailable_queries_by_window": unscored_by_window,
            "non_git_fallback_queries_by_window": non_git_by_window,
            "non_git_unscored_queries_by_window": non_git_unscored_by_window,
            # No transcript records a contemporaneous Git status/diff.
            "dirty_state_unreconstructable_queries_by_window": {
                str(window): len(evalsets[window]) for window in WINDOWS
            },
        },
        "arm_unavailability_by_tree": _arm_unavailability_by_tree(run_rows, provenance),
        "partial_arm_rows_by_tree": _partial_arm_rows_by_tree(run_rows, provenance),
        "notes": [
            "Every retained union ID has one exact/fallback/unscored provenance row.",
            "gitBranch is used only when cwd and target have the same logical tree ID; sibling worktrees sharing a common Git directory fall back to target HEAD.",
            "Epoch candidates are exact only when unique or when all eligible candidates resolve to the identical target tree object.",
            "Branch resolution is local-first; remote suffixes must be unambiguous.",
            "Historical reconstruction is limited to ancestry reachable from surviving refs.",
            "Clean Git snapshots cannot reproduce dirty or untracked state at query time.",
            "All arms share the same snapshot; one disposable SQLite database is incrementally refreshed between states.",
        ],
    }
    summary_path = output_dir / "run-summary-v2.json"
    _atomic_json(summary_path, summary)
    return metrics_path, summary_path


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, default=DEFAULT_EVAL_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--scratch-dir",
        type=Path,
        default=None,
        help="disposable detached worktrees and the one active SQLite DB (use a roomy volume)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--aggregate-only", action="store_true")
    parser.add_argument(
        "--reprepare",
        action="store_true",
        help="replace a prepared plan only when no run rows exist",
    )
    parser.add_argument("--git-timeout", type=float, default=300.0)
    parser.add_argument("--progress-every", type=int, default=100)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    eval_dir = args.eval_dir.resolve(strict=True)
    output_dir = (args.output_dir or args.eval_dir).resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    scratch_dir = (args.scratch_dir or (output_dir / ".scratch-v2")).resolve(strict=False)
    plan_path = output_dir / "prepare-plan-v2.json"
    runs_path = output_dir / "runs-v2.jsonl"
    partial_runs = runs_path.with_suffix(runs_path.suffix + ".partial")

    if args.reprepare and (runs_path.exists() or partial_runs.exists()):
        raise RuntimeError(
            "refusing --reprepare while runs-v2 JSONL exists; preserve it and use a new output directory"
        )
    if args.aggregate_only and args.reprepare:
        raise ValueError("--aggregate-only cannot be combined with --reprepare")

    if args.reprepare or not plan_path.exists():
        if args.aggregate_only:
            raise RuntimeError("--aggregate-only requires an existing prepared plan")
        prepared = prepare_run(
            eval_dir,
            output_dir,
            scratch_dir,
            git_timeout=args.git_timeout,
        )
    else:
        prepared = load_prepared_run(eval_dir, output_dir)
    evalsets, records, provenance, exclusions, plan = prepared

    if args.prepare_only:
        print(
            json.dumps(
                {
                    "prepared": str(plan_path),
                    "fingerprint": plan["fingerprint"],
                    "union_queries": len(provenance),
                    "scorable_queries": sum(bool(row.get("target_tree_id")) for row in provenance),
                    "excluded_queries": len(exclusions),
                },
                indent=2,
            )
        )
        return 0

    execution_stats: dict[str, Any] | None = None
    if args.aggregate_only:
        if not runs_path.exists():
            raise RuntimeError("--aggregate-only requires runs-v2.jsonl")
        run_rows = _load_jsonl(runs_path)
        state_stats_path = output_dir / "state-stats-v2.json"
        execution_stats = (
            json.loads(state_stats_path.read_text(encoding="utf-8"))
            if state_stats_path.exists()
            else None
        )
    else:
        run_rows, execution_stats = execute_runs(
            records,
            provenance,
            runs_path=runs_path,
            fingerprint=str(plan["fingerprint"]),
            scratch_dir=scratch_dir,
            git_timeout=args.git_timeout,
            progress_every=max(1, int(args.progress_every)),
        )
        _atomic_json(output_dir / "state-stats-v2.json", execution_stats)

    metrics_path, summary_path = write_metrics_and_summary(
        evalsets,
        provenance,
        exclusions,
        run_rows,
        plan,
        output_dir=output_dir,
        execution_stats=execution_stats,
    )
    print(
        json.dumps(
            {
                "runs": str(runs_path),
                "metrics": str(metrics_path),
                "summary": str(summary_path),
                "scored_queries": sum(bool(row.get("target_tree_id")) for row in provenance),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
