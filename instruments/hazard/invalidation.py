"""Join read-then-foreign-write exposure to observable downstream rework.

The historical hazard extractor intentionally remains untouched.  This
instrument first reconstructs tool operations from source-local call/result
pairs, then deduplicates copied transcript prefixes by global tool-use id.  It
reports the original ordered-pair estimand for opening overlap and a separate
first-foreign-write response episode for downstream behavior.

The transcript corpus is opened read-only.  At startup the script snapshots
every JSONL byte length and subsequently reads exactly those prefixes, producing
a per-file SHA-256 manifest beside the aggregate result.
"""

from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import random
import re
from typing import Any, Iterable, Sequence

try:  # Script execution and package-style test imports both work.
    from invalidation_core import (
        ChangeBlock,
        classify_change_overlap,
        is_exact_inverse_patch,
        normalize_windows_path,
        parse_numbered_read_window,
        parse_structured_patch,
        transform_intervals_through_changes,
        union_intervals,
    )
except ImportError:  # pragma: no cover - exercised only through package import
    from .invalidation_core import (
        ChangeBlock,
        classify_change_overlap,
        is_exact_inverse_patch,
        normalize_windows_path,
        parse_numbered_read_window,
        parse_structured_patch,
        transform_intervals_through_changes,
        union_intervals,
    )


READ_TOOLS = {"Read", "NotebookRead"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS
CODE_EXTENSIONS = {
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".go",
    ".rs", ".java", ".rb", ".c", ".h", ".cpp", ".hpp", ".cs",
    ".php", ".sh", ".ps1", ".sql", ".tf", ".yaml", ".yml",
    ".json", ".toml",
}

# The original run saved aggregates but no byte manifest.  This post-hoc cutoff
# is the last call included by the unique cutoff that reproduces all committed
# recount numbers over the 5,547 then-existing files.  It is forensic, not a
# claim that the old live scan was atomic.
DEFAULT_COHORT_CUTOFF = "2026-08-23T11:15:09.101Z"
WINDOW_SECONDS = 3600.0
QUIESCENCE_SECONDS = 3600.0
MIN_CLEAN_FOLLOWUP_SECONDS = 300.0

_SESSION_RE = re.compile(br'"sessionId"\s*:\s*"([^"\\]+)"')
_TIMESTAMP_RE = re.compile(br'"timestamp"\s*:\s*"([^"\\]+)"')


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def iso_utc(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


def legacy_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    # Match extract_hazards.py exactly.  In particular, normcase changes case
    # and slash direction on Windows but deliberately does not collapse dot
    # segments; canonical primary paths below use the stricter rule.
    return ntpath.normcase(value)


def canonical_path(value: Any, cwd: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        path = normalize_windows_path(value, cwd if isinstance(cwd, str) else None)
    except (TypeError, ValueError):
        return None
    return path if ntpath.isabs(path) else None


def is_code_path(path: str) -> bool:
    return ntpath.splitext(path)[1].lower() in CODE_EXTENSIONS


def result_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [
        block for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def visible_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    pieces: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            pieces.append(item["text"])
        elif isinstance(item, str):
            pieces.append(item)
    return "\n".join(pieces) if pieces else None


@dataclass
class Call:
    tool_id: str
    tool: str
    session: str
    actor: str
    legacy_agent: str
    explicit_agent: bool
    call_ts: float
    cwd: str | None
    input_path: str
    call_uuid: str | None
    source_rel: str
    source_created_ns: int
    source_line: int
    content_order: int
    input_data: dict[str, Any]


@dataclass
class Candidate:
    call: Call
    result_ts: float
    success: bool
    path: str | None
    path_source: str
    read_interval: tuple[int, int] | None = None
    read_source: str = "none"
    read_line_signatures: tuple[str, ...] = ()
    read_lines: tuple[str, ...] | None = None
    patch: tuple[ChangeBlock, ...] = ()
    patch_status: str = "missing"
    original_file_status: str = "missing"
    original_file_lines: tuple[str, ...] | None = None
    result_path_raw: str | None = None


@dataclass
class Operation:
    tool_id: str
    tool: str
    session: str
    actor: str
    legacy_agent: str
    explicit_agent: bool
    call_ts: float
    result_ts: float
    path: str | None
    legacy_path: str | None
    success: bool
    read_interval: tuple[int, int] | None
    read_source: str
    read_lines: tuple[str, ...] | None
    patch: tuple[ChangeBlock, ...]
    patch_status: str
    original_file_status: str
    original_file_lines: tuple[str, ...] | None
    origin_created_ns: int
    origin_rel: str
    call_uuid: str | None
    duplicate_occurrences: int = 1
    identity_conflict: bool = False
    metadata_conflict: bool = False

    @property
    def kind(self) -> str:
        return "r" if self.tool in READ_TOOLS else "w"

    @property
    def cohort_region_usable(self) -> bool:
        if self.kind == "r":
            return self.read_interval is not None
        return self.patch_status == "exact" and bool(self.patch)


@dataclass(frozen=True)
class ReadDependency:
    """One localized read and its current dependency footprint.

    ``original_interval`` and ``lines`` remain in the version actually read so
    they can be checked against B's preimage.  ``footprint`` and ``anchors`` are
    propagated through any exact intervening edits by A and are the coordinates
    used for structural contact with B's write.
    """

    result_ts: float
    original_interval: tuple[int, int]
    source: str
    lines: tuple[str, ...] | None
    footprint: tuple[tuple[int, int], ...]
    anchors: tuple[int, ...] = ()


@dataclass(frozen=True)
class LegacyEvent:
    timestamp: float
    agent: str
    session: str
    kind: str
    path: str
    tool_id: str


def logical_lines(content: str) -> tuple[str, ...]:
    lines = content.splitlines()
    if content.endswith(("\n", "\r")):
        lines.append("")
    return tuple(lines)


def structured_read(
    top_result: Any,
) -> tuple[tuple[int, int] | None, tuple[str, ...], tuple[str, ...] | None]:
    if not isinstance(top_result, dict):
        return None, (), None
    file_result = top_result.get("file")
    if not isinstance(file_result, dict):
        return None, (), None
    start = file_result.get("startLine")
    count = file_result.get("numLines")
    if (
        isinstance(start, bool) or isinstance(count, bool)
        or not isinstance(start, int) or not isinstance(count, int)
        or start < 1 or count < 1
    ):
        return None, (), None
    content = file_result.get("content")
    signatures: tuple[str, ...] = ()
    retained_lines: tuple[str, ...] | None = None
    if isinstance(content, str):
        # Exact content is retained only in memory for pre-image validation and
        # is never serialized.  Signatures support conflict checks without
        # leaking source text into artifacts.
        lines = logical_lines(content)
        retained_lines = lines if len(lines) == count else None
        signatures = tuple(
            hashlib.sha256(line.encode("utf-8", errors="surrogatepass")).hexdigest()
            for line in lines[:count]
        )
    return (start, start + count), signatures, retained_lines


def patch_matches_preimage(
    changes: Sequence[ChangeBlock], preimage_lines: Sequence[str]
) -> bool:
    for block in changes:
        if not block.old_lines:
            continue
        start = block.old_start - 1
        end = block.old_end - 1
        if start < 0 or tuple(preimage_lines[start:end]) != block.old_lines:
            return False
    return True


def build_candidate(
    call: Call,
    record: dict[str, Any],
    block: dict[str, Any],
    top_result: Any,
    diagnostics: collections.Counter[str],
) -> Candidate:
    result_ts = parse_timestamp(record.get("timestamp"))
    if result_ts is None:
        result_ts = call.call_ts
        diagnostics["results_using_call_timestamp_fallback"] += 1
    success = block.get("is_error") is not True
    if not success:
        diagnostics[f"{call.tool}_error_results"] += 1

    raw_result_path: str | None = None
    read_interval: tuple[int, int] | None = None
    read_signatures: tuple[str, ...] = ()
    read_lines: tuple[str, ...] | None = None
    read_source = "none"
    patch: tuple[ChangeBlock, ...] = ()
    patch_status = "missing"
    original_status = "missing"
    original_lines: tuple[str, ...] | None = None

    if call.tool in READ_TOOLS:
        file_result = top_result.get("file") if isinstance(top_result, dict) else None
        if isinstance(file_result, dict):
            raw_result_path = file_result.get("filePath") if isinstance(file_result.get("filePath"), str) else None
            read_interval, read_signatures, read_lines = structured_read(top_result)
            if read_interval is not None:
                read_source = "structured_result"
        if read_interval is None and success:
            text = visible_text(block.get("content"))
            if text is not None:
                try:
                    window = parse_numbered_read_window(text)
                except (TypeError, ValueError):
                    diagnostics["visible_read_windows_unparseable"] += 1
                else:
                    read_interval = window.interval
                    read_signatures = window.signatures
                    read_lines = window.lines
                    read_source = "visible_result_fallback"
    else:
        if isinstance(top_result, dict):
            raw_result_path = top_result.get("filePath") if isinstance(top_result.get("filePath"), str) else None
            original = top_result.get("originalFile", object())
            if isinstance(original, str):
                original_status = "nonempty_string" if original else "empty_string"
                original_lines = logical_lines(original)
            elif original is None:
                original_status = "null"
            else:
                original_status = "missing"
            raw_patch = top_result.get("structuredPatch", object())
            if isinstance(raw_patch, list):
                try:
                    patch = parse_structured_patch(raw_patch)
                except (TypeError, ValueError):
                    patch_status = "invalid"
                    diagnostics["structured_patches_invalid"] += 1
                else:
                    patch_status = "exact" if patch else "empty"
                    if (
                        patch
                        and original_lines is not None
                        and not patch_matches_preimage(patch, original_lines)
                    ):
                        patch_status = "invalid"
                        patch = ()
                        diagnostics["structured_patches_preimage_mismatch"] += 1
            else:
                patch_status = "missing"

    chosen_raw = raw_result_path or call.input_path
    chosen_path = canonical_path(chosen_raw, call.cwd)
    path_source = "result_metadata" if raw_result_path else "paired_input_fallback"
    if raw_result_path:
        result_norm = canonical_path(raw_result_path, call.cwd)
        input_norm = canonical_path(call.input_path, call.cwd)
        if result_norm is not None and input_norm is not None and result_norm != input_norm:
            diagnostics["result_input_path_mismatches"] += 1

    return Candidate(
        call=call,
        result_ts=result_ts,
        success=success,
        path=chosen_path,
        path_source=path_source,
        read_interval=read_interval,
        read_source=read_source,
        read_line_signatures=read_signatures,
        read_lines=read_lines,
        patch=patch,
        patch_status=patch_status,
        original_file_status=original_status,
        original_file_lines=original_lines,
        result_path_raw=raw_result_path,
    )


def candidate_rank(candidate: Candidate) -> tuple[int, int, int, int]:
    structured = int(
        candidate.read_source == "structured_result"
        or candidate.patch_status == "exact"
    )
    localized = int(candidate.read_interval is not None or candidate.patch_status in {"exact", "empty"})
    result_path = int(candidate.path_source == "result_metadata")
    preimage = int(candidate.original_file_status == "nonempty_string")
    return structured, localized, result_path, preimage


def merge_candidates(
    tool_id: str,
    candidates: Sequence[Candidate],
    diagnostics: collections.Counter[str],
) -> Operation | None:
    if not candidates:
        return None
    diagnostics["candidate_occurrences"] += len(candidates)
    if len(candidates) > 1:
        diagnostics["duplicated_tool_ids"] += 1
        diagnostics["duplicate_candidate_occurrences"] += len(candidates) - 1

    signatures = {
        (
            candidate.call.tool,
            candidate.call.call_ts,
            legacy_path(candidate.call.input_path),
        )
        for candidate in candidates
    }
    metadata_conflict = len(signatures) > 1
    if metadata_conflict:
        diagnostics["conflicting_duplicate_call_signatures"] += 1
        return None

    # The earliest-created transcript is the originating copy for 97.2% of
    # repeated ids where exactly one file existed at call time; explicit actor
    # identity is the deterministic tie-break within that source.
    origin_candidate = min(
        candidates,
        key=lambda c: (
            c.call.source_created_ns,
            c.call.source_rel.casefold(),
            c.call.source_line,
            c.call.content_order,
            0 if c.call.explicit_agent else 1,
        ),
    )
    identity_pairs = {(c.call.session, c.call.actor) for c in candidates}
    identity_conflict = len(identity_pairs) > 1
    if identity_conflict:
        diagnostics["duplicate_ids_with_identity_conflict"] += 1

    successes = {candidate.success for candidate in candidates}
    if len(successes) > 1:
        diagnostics["duplicate_ids_with_result_status_conflict"] += 1
        return None

    best = max(
        candidates,
        key=lambda c: (
            candidate_rank(c),
            -c.call.source_created_ns,
            c.call.source_rel.casefold(),
        ),
    )
    distinct_paths = {candidate.path for candidate in candidates if candidate.path is not None}
    if len(distinct_paths) > 1:
        diagnostics["duplicate_ids_with_path_conflict"] += 1
        return None

    distinct_intervals = {
        candidate.read_interval for candidate in candidates
        if candidate.read_interval is not None
    }
    if len(distinct_intervals) > 1:
        diagnostics["duplicate_ids_with_read_window_conflict"] += 1
        return None

    distinct_patches = {
        tuple(
            (b.old_start, b.new_start, b.old_signatures, b.new_signatures)
            for b in candidate.patch
        )
        for candidate in candidates
        if candidate.patch_status == "exact"
    }
    if len(distinct_patches) > 1:
        diagnostics["duplicate_ids_with_patch_conflict"] += 1
        return None

    call = origin_candidate.call
    return Operation(
        tool_id=tool_id,
        tool=call.tool,
        session=call.session,
        actor=call.actor,
        legacy_agent=call.legacy_agent,
        explicit_agent=call.explicit_agent,
        call_ts=call.call_ts,
        result_ts=best.result_ts,
        path=best.path,
        legacy_path=legacy_path(call.input_path),
        success=best.success,
        read_interval=best.read_interval,
        read_source=best.read_source,
        read_lines=best.read_lines,
        patch=best.patch,
        patch_status=best.patch_status,
        original_file_status=best.original_file_status,
        original_file_lines=best.original_file_lines,
        origin_created_ns=call.source_created_ns,
        origin_rel=call.source_rel,
        call_uuid=call.call_uuid,
        duplicate_occurrences=len(candidates),
        identity_conflict=identity_conflict,
        metadata_conflict=metadata_conflict,
    )


def scan_corpus(
    corpus: Path,
    cohort_cutoff: float,
    progress_every: int,
) -> tuple[list[Operation], list[LegacyEvent], dict[str, Any], list[dict[str, Any]]]:
    paths = sorted(corpus.rglob("*.jsonl"), key=lambda p: str(p).casefold())
    snapshots: list[tuple[Path, int, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            snapshots.append((path, -1, 0, 0))
        else:
            snapshots.append((path, stat.st_size, stat.st_ctime_ns, stat.st_mtime_ns))

    snapshot_epoch = dt.datetime.now(dt.timezone.utc).timestamp()
    cutoff_ns = int(cohort_cutoff * 1_000_000_000)
    diagnostics: collections.Counter[str] = collections.Counter()
    global_candidates: dict[str, list[Candidate]] = collections.defaultdict(list)
    legacy_events: list[LegacyEvent] = []
    session_last_ts: dict[str, float] = {}
    global_digest = hashlib.sha256()
    manifest_files: list[dict[str, Any]] = []

    for file_index, (path, byte_limit, created_ns, _modified_ns) in enumerate(snapshots, 1):
        relative = path.relative_to(corpus).as_posix()
        # The public manifest preserves byte-prefix identity without exposing
        # local project names, transcript UUIDs, or filesystem timestamps.
        global_digest.update(str(file_index).encode("ascii"))
        global_digest.update(b"\0" + str(byte_limit).encode("ascii") + b"\0")
        file_digest = hashlib.sha256()
        local_calls: dict[str, list[Call]] = collections.defaultdict(list)
        agent_hint = path.stem if path.stem.startswith("agent-") else None
        bytes_read = 0
        if byte_limit < 0:
            diagnostics["files_stat_failed"] += 1
            continue
        try:
            with path.open("rb") as handle:
                remaining = byte_limit
                line_number = 0
                while remaining > 0:
                    raw = handle.readline(remaining)
                    if not raw:
                        diagnostics["files_truncated_after_snapshot"] += 1
                        break
                    remaining -= len(raw)
                    bytes_read += len(raw)
                    line_number += 1
                    file_digest.update(raw)
                    global_digest.update(raw)
                    diagnostics["jsonl_lines"] += 1

                    if b'"tool_use"' not in raw and b'"tool_result"' not in raw:
                        session_match = _SESSION_RE.search(raw)
                        timestamp_match = _TIMESTAMP_RE.search(raw)
                        if session_match and timestamp_match:
                            try:
                                session_value = session_match.group(1).decode("utf-8")
                                timestamp_value = timestamp_match.group(1).decode("ascii")
                            except UnicodeDecodeError:
                                pass
                            else:
                                parsed = parse_timestamp(timestamp_value)
                                if parsed is not None:
                                    session_last_ts[session_value] = max(
                                        parsed, session_last_ts.get(session_value, -math.inf)
                                    )
                        continue
                    try:
                        record = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        diagnostics["malformed_relevant_lines"] += 1
                        continue
                    if not isinstance(record, dict):
                        continue
                    session = record.get("sessionId")
                    record_ts = parse_timestamp(record.get("timestamp"))
                    if isinstance(session, str) and record_ts is not None:
                        session_last_ts[session] = max(
                            record_ts, session_last_ts.get(session, -math.inf)
                        )
                    message = record.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if isinstance(content, list):
                        for content_order, block in enumerate(content):
                            if not isinstance(block, dict) or block.get("type") != "tool_use":
                                continue
                            tool = block.get("name")
                            tool_id = block.get("id")
                            input_data = block.get("input")
                            if (
                                tool not in ALL_TOOLS
                                or not isinstance(tool_id, str)
                                or not isinstance(input_data, dict)
                                or not isinstance(session, str)
                                or record_ts is None
                            ):
                                continue
                            input_path = input_data.get("file_path") or input_data.get("notebook_path")
                            if not isinstance(input_path, str) or not input_path:
                                continue
                            explicit = isinstance(record.get("agentId"), str) and bool(record.get("agentId"))
                            actor = str(record.get("agentId")) if explicit else "MAIN"
                            old_agent = str(record.get("agentId") or agent_hint or session)
                            call = Call(
                                tool_id=tool_id,
                                tool=tool,
                                session=session,
                                actor=actor,
                                legacy_agent=old_agent,
                                explicit_agent=explicit,
                                call_ts=record_ts,
                                cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                                input_path=input_path,
                                call_uuid=record.get("uuid") if isinstance(record.get("uuid"), str) else None,
                                source_rel=relative,
                                source_created_ns=created_ns,
                                source_line=line_number,
                                content_order=content_order,
                                input_data=input_data,
                            )
                            local_calls[tool_id].append(call)
                            diagnostics[f"raw_{tool}_calls"] += 1
                            if created_ns <= cutoff_ns and record_ts <= cohort_cutoff:
                                old_path = legacy_path(input_path)
                                if old_path is not None:
                                    legacy_events.append(
                                        LegacyEvent(
                                            record_ts,
                                            old_agent,
                                            session,
                                            "r" if tool in READ_TOOLS else "w",
                                            old_path,
                                            tool_id,
                                        )
                                    )

                    blocks = result_blocks(record)
                    top_result = record.get("toolUseResult") if len(blocks) == 1 else None
                    parent_uuid = record.get("parentUuid") or record.get("sourceToolAssistantUUID")
                    for block in blocks:
                        tool_id = block.get("tool_use_id")
                        if not isinstance(tool_id, str):
                            diagnostics["result_blocks_missing_tool_id"] += 1
                            continue
                        choices = local_calls.get(tool_id, [])
                        if not choices:
                            diagnostics["source_local_results_without_call"] += 1
                            continue
                        exact_parent = [call for call in choices if call.call_uuid == parent_uuid]
                        call = exact_parent[-1] if exact_parent else choices[-1]
                        if not exact_parent:
                            diagnostics["results_paired_without_parent_uuid_match"] += 1
                        global_candidates[tool_id].append(
                            build_candidate(call, record, block, top_result, diagnostics)
                        )
                        diagnostics["paired_result_occurrences"] += 1
            diagnostics["files_read_successfully"] += 1
        except OSError:
            diagnostics["files_read_failed"] += 1

        manifest_files.append(
            {
                "ordinal": file_index,
                "byte_length": byte_limit,
                "bytes_read": bytes_read,
                "created_before_endpoint_cutoff": created_ns <= cutoff_ns,
                "prefix_sha256": file_digest.hexdigest(),
            }
        )
        if progress_every > 0 and (file_index % progress_every == 0 or file_index == len(snapshots)):
            print(
                f"scanned {file_index:,}/{len(snapshots):,} transcript files; "
                f"{len(global_candidates):,} result-linked tool ids",
                flush=True,
            )

    operations: list[Operation] = []
    for tool_id, candidates in global_candidates.items():
        operation = merge_candidates(tool_id, candidates, diagnostics)
        if operation is not None:
            operations.append(operation)
    operations.sort(key=lambda op: (op.result_ts, op.call_ts, op.tool_id))
    diagnostics["deduplicated_operations"] = len(operations)
    diagnostics["legacy_cohort_occurrences"] = len(legacy_events)
    diagnostics["session_count"] = len(session_last_ts)

    metadata = {
        "snapshot_utc": iso_utc(snapshot_epoch),
        "snapshot_epoch": snapshot_epoch,
        "corpus_root": "<redacted-local-claude-projects>",
        "corpus_file_count": len(snapshots),
        "corpus_bytes": sum(max(size, 0) for _, size, _, _ in snapshots),
        "corpus_snapshot_sha256": global_digest.hexdigest(),
        "cohort_cutoff_utc": iso_utc(cohort_cutoff),
        "cohort_cutoff_provenance": (
            "post-hoc last-call cutoff that reproduces the committed 5,547-file "
            "hazard recount; the historical scan saved no byte manifest"
        ),
        "session_last_timestamp": session_last_ts,
        "diagnostics": dict(sorted(diagnostics.items())),
    }
    return operations, legacy_events, metadata, manifest_files


def hazard_counts(events: Iterable[LegacyEvent], code_only: bool) -> dict[str, Any]:
    by_path: dict[str, list[LegacyEvent]] = collections.defaultdict(list)
    for event in events:
        if code_only and not is_code_path(event.path):
            continue
        by_path[event.path].append(event)
    pair_count = 0
    paths: set[str] = set()
    readers: set[str] = set()
    writers: set[str] = set()
    for path, path_events in by_path.items():
        reads = [event for event in path_events if event.kind == "r"]
        writes = [event for event in path_events if event.kind == "w"]
        for read in reads:
            for write in writes:
                delta = write.timestamp - read.timestamp
                if 0 < delta <= WINDOW_SECONDS and write.agent != read.agent:
                    pair_count += 1
                    paths.add(path)
                    readers.add(read.agent)
                    writers.add(write.agent)
    return {
        "path_bearing_occurrences": sum(len(value) for value in by_path.values()),
        "paths_considered": len(by_path),
        "ordered_pairs": pair_count,
        "hazard_paths": len(paths),
        "readers": len(readers),
        "writers": len(writers),
    }


def legacy_duplication(events: Sequence[LegacyEvent]) -> dict[str, Any]:
    by_id: dict[str, list[LegacyEvent]] = collections.defaultdict(list)
    for event in events:
        by_id[event.tool_id].append(event)
    repeated = {tool_id: values for tool_id, values in by_id.items() if len(values) > 1}
    return {
        "occurrences": len(events),
        "unique_tool_use_ids": len(by_id),
        "repeated_tool_use_ids": len(repeated),
        "copied_or_repeated_occurrences": len(events) - len(by_id),
        "repeated_ids_with_actor_or_session_disagreement": sum(
            len({(event.session, event.agent) for event in values}) > 1
            for values in repeated.values()
        ),
    }


def deduplicated_pair_counts(operations: Sequence[Operation], cutoff: float) -> dict[str, Any]:
    eligible = [
        op for op in operations
        if op.success
        and op.legacy_path is not None
        and is_code_path(op.legacy_path)
        and op.call_ts <= cutoff
        and op.origin_created_ns <= int(cutoff * 1_000_000_000)
    ]
    by_path: dict[str, list[Operation]] = collections.defaultdict(list)
    for op in eligible:
        assert op.legacy_path is not None
        by_path[op.legacy_path].append(op)
    totals = collections.Counter()
    path_sets: dict[str, set[str]] = collections.defaultdict(set)
    readers: set[str] = set()
    writers: set[str] = set()
    for path, path_ops in by_path.items():
        reads = [op for op in path_ops if op.kind == "r"]
        writes_for_path = [op for op in path_ops if op.kind == "w"]
        for read in reads:
            for write in writes_for_path:
                delta = write.call_ts - read.call_ts
                if not (0 < delta <= WINDOW_SECONDS):
                    continue
                read_identity = (read.session, read.actor)
                write_identity = (write.session, write.actor)
                if read_identity == write_identity:
                    continue
                scope = "same_session" if read.session == write.session else "cross_session"
                totals[scope] += 1
                totals["all"] += 1
                path_sets[scope].add(path)
                path_sets["all"].add(path)
                readers.add(f"{read.session}:{read.actor}")
                writers.add(f"{write.session}:{write.actor}")
    return {
        "ordered_pairs": totals["all"],
        "hazard_paths": len(path_sets["all"]),
        "readers": len(readers),
        "writers": len(writers),
        "same_session_pairs": totals["same_session"],
        "same_session_paths": len(path_sets["same_session"]),
        "cross_session_pairs": totals["cross_session"],
        "cross_session_paths": len(path_sets["cross_session"]),
        "paths_in_both_scopes": len(path_sets["same_session"] & path_sets["cross_session"]),
    }


def map_dependency_footprint(
    intervals: Sequence[tuple[int, int]],
    changes: Sequence[ChangeBlock],
) -> tuple[tuple[tuple[int, int], ...], tuple[int, ...], Any]:
    transformed = transform_intervals_through_changes(intervals, changes)
    mapped = list(transformed.intervals)
    anchors: list[int] = []
    for block in changes:
        contact = classify_change_overlap(intervals, [block])
        if contact.strict:
            if block.new_start < block.new_end:
                mapped.append(block.new_interval)
            else:
                anchors.append(block.new_start)
    return union_intervals(mapped), tuple(sorted(set(anchors))), transformed.overlap


def changed_post_regions(changes: Sequence[ChangeBlock]) -> tuple[tuple[int, int], ...]:
    return union_intervals(
        block.new_interval for block in changes if block.new_start < block.new_end
    )


def changed_post_anchors(changes: Sequence[ChangeBlock]) -> tuple[int, ...]:
    return tuple(sorted({block.new_start for block in changes if block.new_start == block.new_end}))


def transform_anchor(anchor: int, changes: Sequence[ChangeBlock]) -> int:
    delta = 0
    for block in sorted(changes, key=lambda item: (item.old_start, item.old_end)):
        if block.is_insertion:
            if block.old_start <= anchor:
                delta += len(block.new_lines)
        elif block.old_end <= anchor:
            delta += len(block.new_lines) - len(block.old_lines)
        elif block.old_start <= anchor < block.old_end:
            return block.new_start
    return anchor + delta


def read_touches_changed(
    read_interval: tuple[int, int],
    changed: Sequence[tuple[int, int]],
    anchors: Sequence[int] = (),
) -> bool:
    return any(
        read_interval[0] < region[1] and region[0] < read_interval[1]
        for region in changed
    ) or any(read_interval[0] <= anchor < read_interval[1] for anchor in anchors)


def target_touches_patch(
    intervals: Sequence[tuple[int, int]],
    anchors: Sequence[int],
    changes: Sequence[ChangeBlock],
) -> bool:
    if intervals and classify_change_overlap(intervals, changes).strict:
        return True
    for anchor in anchors:
        for block in changes:
            if block.old_lines and block.old_start <= anchor < block.old_end:
                return True
            if block.is_insertion and block.old_start == anchor:
                return True
    return False


def map_dependency_targets(
    intervals: Sequence[tuple[int, int]],
    anchors: Sequence[int],
    changes: Sequence[ChangeBlock],
) -> tuple[tuple[tuple[int, int], ...], tuple[int, ...], Any, bool]:
    mapped, mapped_anchors, overlap = map_dependency_footprint(intervals, changes)
    intervals_out = list(mapped)
    anchors_out = list(mapped_anchors)
    anchor_contact = False
    for anchor in anchors:
        touching: list[ChangeBlock] = []
        for block in changes:
            if block.old_lines and block.old_start <= anchor < block.old_end:
                touching.append(block)
            elif block.is_insertion and block.old_start == anchor:
                touching.append(block)
        if not touching:
            anchors_out.append(transform_anchor(anchor, changes))
            continue
        anchor_contact = True
        for block in touching:
            if block.new_start < block.new_end:
                intervals_out.append(block.new_interval)
            else:
                anchors_out.append(block.new_start)
    return (
        union_intervals(intervals_out),
        tuple(sorted(set(anchors_out))),
        overlap,
        overlap.strict or anchor_contact,
    )


def validate_opening_preimage(
    read_parts: Sequence[ReadDependency],
    opening: Operation,
    pending_chain_known: bool,
) -> str:
    if not pending_chain_known:
        return "intervening_reader_write"
    if opening.original_file_lines is None:
        return "preimage_unavailable"
    for part in read_parts:
        interval = part.original_interval
        lines = part.lines
        if lines is None or len(lines) != interval[1] - interval[0]:
            return "read_content_unavailable"
        start = interval[0] - 1
        end = interval[1] - 1
        if tuple(opening.original_file_lines[start:end]) != lines:
            return "preimage_mismatch"
    return "matched"


def new_episode(
    session: str,
    reader: str,
    path: str,
    read_parts: Sequence[ReadDependency],
    opening: Operation,
    pending_chain_known: bool,
) -> dict[str, Any]:
    initial_footprint = union_intervals(
        interval for part in read_parts for interval in part.footprint
    )
    initial_anchors = tuple(sorted({anchor for part in read_parts for anchor in part.anchors}))
    validation = validate_opening_preimage(
        read_parts, opening, pending_chain_known
    )
    episode = {
        "session": session,
        "reader": reader,
        "path": path,
        "start_ts": opening.result_ts,
        "opening_call_ts": opening.call_ts,
        "opening_write_id": opening.tool_id,
        "opening_writer": opening.actor,
        "last_read_ts": max(part.result_ts for part in read_parts),
        "read_count": len(read_parts),
        "read_sources": {part.source for part in read_parts},
        "footprint": initial_footprint,
        "footprint_anchors": initial_anchors,
        "changed_regions": (),
        "changed_anchors": (),
        "region_chain_known": True,
        "strict_overlap": False,
        "destructive_overlap": False,
        "internal_insertion_overlap": False,
        "boundary_insertion_contact": False,
        "foreign_write_count": 0,
        "unknown_write_count": 0,
        "foreign_writers": set(),
        "first_edit_any": None,
        "unrelated_edit_count": 0,
        "response": None,
        "response_ts": None,
        "index_edit_id": None,
        "index_touches_changed": False,
        "index_patch": (),
        "opening_preimage_validation": validation,
    }
    episode["foreign_write_count"] = 1
    episode["foreign_writers"].add(opening.actor)
    if opening.patch_status != "exact" or not opening.patch:
        episode["region_chain_known"] = False
        episode["unknown_write_count"] = 1
    else:
        mapped, anchors, overlap, strict_contact = map_dependency_targets(
            initial_footprint, initial_anchors, opening.patch
        )
        episode["footprint"] = mapped
        episode["footprint_anchors"] = anchors
        episode["destructive_overlap"] = overlap.destructive
        episode["internal_insertion_overlap"] = overlap.internal_insertion
        episode["boundary_insertion_contact"] = overlap.boundary_insertion
        episode["strict_overlap"] = strict_contact
        episode["changed_regions"] = changed_post_regions(opening.patch)
        episode["changed_anchors"] = changed_post_anchors(opening.patch)

    if not episode["region_chain_known"]:
        episode["opening_region_class"] = "unknown"
    elif episode["strict_overlap"]:
        episode["opening_region_class"] = "region_overlapping"
    else:
        episode["opening_region_class"] = "file_only"
    episode["verified_region_class"] = (
        episode["opening_region_class"]
        if validation == "matched" and episode["opening_region_class"] != "unknown"
        else "unknown"
    )
    episode["opening_boundary_insertion_contact"] = episode["boundary_insertion_contact"]
    return episode


def finalize_episode(episode: dict[str, Any]) -> dict[str, Any]:
    # The estimand is the opening B write.  A later foreign write is a competing
    # exposure, never merged into or allowed to change this row label.
    episode["region_class"] = episode["verified_region_class"]
    episode["read_sources"] = sorted(episode["read_sources"])
    episode["foreign_writers"] = sorted(episode["foreign_writers"])
    return episode


def reader_action_censor(
    episode: dict[str, Any],
    reader: str,
    action: Operation,
    path_ops: Sequence[Operation],
) -> tuple[str, float] | None:
    """Return a competing/ambiguous response that precedes A's action."""

    start_ts = episode["start_ts"]
    if action.call_ts <= start_ts:
        return "ambiguous_concurrent_reader_action", action.result_ts
    foreign = [
        op for op in path_ops
        if op.kind == "w"
        and op.actor != reader
        and op.tool_id != episode["opening_write_id"]
        and op.result_ts > episode["opening_call_ts"]
    ]
    opening_concurrent = [
        op for op in foreign
        if op.call_ts <= start_ts and episode["opening_call_ts"] <= op.result_ts
    ]
    if opening_concurrent:
        first = min(
            opening_concurrent,
            key=lambda op: (op.call_ts, op.result_ts, op.tool_id),
        )
        return "ambiguous_concurrent_foreign_write", first.result_ts
    competitor = min(
        (op for op in foreign if start_ts < op.call_ts),
        key=lambda op: (op.call_ts, op.result_ts, op.tool_id),
        default=None,
    )
    if competitor is None:
        return None
    if competitor.call_ts < action.call_ts:
        return "competing_foreign_write", competitor.result_ts
    if not action.result_ts < competitor.call_ts:
        return (
            "ambiguous_reader_action_vs_competing_write",
            max(action.result_ts, competitor.result_ts),
        )
    return None


def build_response_episodes(
    operations: Sequence[Operation],
    cutoff: float,
    diagnostics: collections.Counter[str],
) -> list[dict[str, Any]]:
    cutoff_ns = int(cutoff * 1_000_000_000)
    usable = [
        op for op in operations
        if op.success and op.path is not None and is_code_path(op.path)
    ]
    grouped: dict[tuple[str, str], list[Operation]] = collections.defaultdict(list)
    for op in usable:
        assert op.path is not None
        grouped[(op.session, op.path)].append(op)

    episodes: list[dict[str, Any]] = []
    for (session, path), path_ops in grouped.items():
        path_ops.sort(key=lambda op: (op.result_ts, op.call_ts, op.tool_id))
        readers = {
            op.actor for op in path_ops
            if op.kind == "r"
            and op.call_ts <= cutoff
            and op.origin_created_ns <= cutoff_ns
        }
        for reader in readers:
            pending: list[ReadDependency] = []
            pending_chain_known = True
            episode: dict[str, Any] | None = None
            for op in path_ops:
                if op.kind == "r" and op.actor == reader:
                    if episode is not None:
                        censor = reader_action_censor(episode, reader, op, path_ops)
                        if censor is not None:
                            episode["response"], episode["response_ts"] = censor
                            episodes.append(finalize_episode(episode))
                            episode = None
                            pending = []
                            pending_chain_known = True
                            continue
                    if op.read_interval is None:
                        if episode is not None:
                            episode["unlocalized_reread_count"] = episode.get("unlocalized_reread_count", 0) + 1
                        continue
                    if episode is not None and read_touches_changed(
                        op.read_interval,
                        episode["changed_regions"],
                        episode["changed_anchors"],
                    ):
                        episode["response"] = "reread_first"
                        episode["response_ts"] = op.result_ts
                        episodes.append(finalize_episode(episode))
                        episode = None
                        pending = [
                            ReadDependency(
                                op.result_ts,
                                op.read_interval,
                                op.read_source,
                                op.read_lines,
                                (op.read_interval,),
                            )
                        ]
                        pending_chain_known = True
                    elif episode is None:
                        if not pending_chain_known:
                            pending = []
                            pending_chain_known = True
                        pending.append(
                            ReadDependency(
                                op.result_ts,
                                op.read_interval,
                                op.read_source,
                                op.read_lines,
                                (op.read_interval,),
                            )
                        )
                    else:
                        # A fresh read elsewhere does not enlarge the stale
                        # pre-B footprint used to classify a later edit.
                        episode["nonoverlap_reread_count"] = episode.get(
                            "nonoverlap_reread_count", 0
                        ) + 1
                    continue

                if op.kind != "w":
                    continue
                if op.actor == reader:
                    if episode is None:
                        if pending:
                            pending_chain_known = False
                            if op.patch_status == "exact" and op.patch:
                                mapped_pending: list[ReadDependency] = []
                                for part in pending:
                                    mapped, anchors, _, _ = map_dependency_targets(
                                        part.footprint, part.anchors, op.patch
                                    )
                                    mapped_pending.append(
                                        ReadDependency(
                                            part.result_ts,
                                            part.original_interval,
                                            part.source,
                                            part.lines,
                                            mapped,
                                            anchors,
                                        )
                                    )
                                pending = mapped_pending
                        continue
                    censor = reader_action_censor(episode, reader, op, path_ops)
                    if censor is not None:
                        episode["response"], episode["response_ts"] = censor
                        episodes.append(finalize_episode(episode))
                        episode = None
                        pending.clear()
                        pending_chain_known = True
                        continue
                    if episode["first_edit_any"] is None:
                        episode["first_edit_any"] = op.tool_id
                    if op.patch_status != "exact" or not op.patch:
                        episode["response"] = "edit_unlocalized"
                        episode["response_ts"] = op.result_ts
                        episodes.append(finalize_episode(episode))
                        episode = None
                        pending.clear()
                        continue
                    touch = target_touches_patch(
                        episode["footprint"], episode["footprint_anchors"], op.patch
                    )
                    if touch:
                        episode["response"] = "relevant_edit"
                        episode["response_ts"] = op.result_ts
                        episode["index_edit_id"] = op.tool_id
                        episode["index_patch"] = op.patch
                        episode["index_touches_changed"] = target_touches_patch(
                            episode["changed_regions"],
                            episode["changed_anchors"],
                            op.patch,
                        )
                        episodes.append(finalize_episode(episode))
                        episode = None
                        pending.clear()
                    else:
                        episode["unrelated_edit_count"] += 1
                        if episode["region_chain_known"]:
                            episode["footprint"] = transform_intervals_through_changes(
                                episode["footprint"], op.patch
                            ).intervals
                            episode["changed_regions"] = transform_intervals_through_changes(
                                episode["changed_regions"], op.patch
                            ).intervals if episode["changed_regions"] else ()
                            episode["footprint_anchors"] = tuple(
                                transform_anchor(anchor, op.patch)
                                for anchor in episode["footprint_anchors"]
                            )
                            episode["changed_anchors"] = tuple(
                                transform_anchor(anchor, op.patch)
                                for anchor in episode["changed_anchors"]
                            )
                    continue

                # Foreign write from the reader's perspective.
                if episode is not None:
                    episode["response"] = (
                        "ambiguous_concurrent_foreign_write"
                        if op.call_ts <= episode["start_ts"]
                        else "competing_foreign_write"
                    )
                    episode["response_ts"] = op.result_ts
                    episodes.append(finalize_episode(episode))
                    episode = None
                    pending.clear()
                    pending_chain_known = True
                    continue

                definite_parts = [
                    part for part in pending
                    if part.result_ts < op.call_ts
                    and op.call_ts - part.result_ts <= WINDOW_SECONDS
                ]
                ambiguous_parts = [
                    part for part in pending
                    if op.call_ts <= part.result_ts <= op.result_ts
                ]
                if ambiguous_parts:
                    diagnostics["read_write_interval_order_ambiguous"] += len(ambiguous_parts)
                opening_is_cohort = (
                    op.call_ts <= cutoff
                    and op.origin_created_ns <= cutoff_ns
                )
                if definite_parts and opening_is_cohort:
                    episode = new_episode(
                        session,
                        reader,
                        path,
                        definite_parts,
                        op,
                        pending_chain_known,
                    )
                pending.clear()
                pending_chain_known = True

            if episode is not None:
                episode["response"] = "no_relevant_response"
                episodes.append(finalize_episode(episode))
    diagnostics["response_episodes"] = len(episodes)
    return episodes


def opening_preimage_validation(read: Operation, write: Operation) -> str:
    """Check that the localized Read content is the content B actually edited."""

    if write.original_file_lines is None:
        return "preimage_unavailable"
    if read.read_interval is None or read.read_lines is None:
        return "read_content_unavailable"
    if len(read.read_lines) != read.read_interval[1] - read.read_interval[0]:
        return "read_content_unavailable"
    start = read.read_interval[0] - 1
    end = read.read_interval[1] - 1
    if start < 0 or tuple(write.original_file_lines[start:end]) != read.read_lines:
        return "preimage_mismatch"
    return "matched"


def follow_hazard_pair(
    read: Operation,
    write: Operation,
    path_ops: Sequence[Operation],
    row: dict[str, Any],
) -> None:
    """Join one ordered hazard pair to A's first relevant post-B action.

    The pair remains the opening unit. A later foreign write is a competing
    exposure. The strictly ordered first A read/write is classified as the
    response; later actions are retained only as uncensored presence flags.
    """

    row.update(
        {
            "first_edit_any": None,
            "reader_write_after_opening_any": False,
            "exact_reader_write_after_opening_any": False,
            "localized_reread_after_opening_any": False,
            "index_edit_id": None,
            "index_patch": (),
            "index_touches_changed": False,
            "response": "observed_end_no_reader_action",
            "response_ts": None,
            "unrelated_edit_count": 0,
            "nonoverlap_reread_count": 0,
            "unlocalized_reread_count": 0,
        }
    )
    if read.read_interval is None or write.patch_status != "exact" or not write.patch:
        row["response"] = "opening_region_unlocalized"
        return
    if read.path is None or write.path is None or read.path != write.path:
        row["response"] = "canonical_path_mismatch"
        return

    footprint, footprint_anchors, _ = map_dependency_footprint(
        (read.read_interval,), write.patch
    )
    changed = changed_post_regions(write.patch)
    changed_anchors = changed_post_anchors(write.patch)
    start_ts = write.result_ts
    relevant = [
        op for op in path_ops
        if op.tool_id not in {read.tool_id, write.tool_id}
        and (op.actor == read.actor or op.kind == "w")
    ]

    later_reader_actions = [
        op for op in relevant if op.actor == read.actor and start_ts < op.call_ts
    ]
    later_reader_writes = [op for op in later_reader_actions if op.kind == "w"]
    later_localized_reads = [
        op for op in later_reader_actions
        if op.kind == "r" and op.read_interval is not None
    ]
    if later_reader_writes:
        first_write = min(
            later_reader_writes, key=lambda op: (op.call_ts, op.result_ts, op.tool_id)
        )
        row["first_edit_any"] = first_write.tool_id
        row["reader_write_after_opening_any"] = True
        row["exact_reader_write_after_opening_any"] = any(
            op.patch_status == "exact" and bool(op.patch)
            for op in later_reader_writes
        )
    row["localized_reread_after_opening_any"] = bool(later_localized_reads)

    concurrent = [
        op for op in relevant
        if op.call_ts <= write.result_ts and write.call_ts <= op.result_ts
    ]
    if concurrent:
        first = min(concurrent, key=lambda op: (op.call_ts, op.result_ts, op.tool_id))
        row["response"] = "ambiguous_with_opening_write"
        row["response_ts"] = first.result_ts
        return

    reader_action = min(
        later_reader_actions,
        key=lambda op: (op.call_ts, op.result_ts, op.tool_id),
        default=None,
    )
    competing_write = min(
        (
            op for op in relevant
            if op.actor != read.actor and op.kind == "w" and start_ts < op.call_ts
        ),
        key=lambda op: (op.call_ts, op.result_ts, op.tool_id),
        default=None,
    )
    if reader_action is None:
        if competing_write is not None:
            row["response"] = "competing_foreign_write"
            row["response_ts"] = competing_write.result_ts
        return
    if competing_write is not None and competing_write.call_ts < reader_action.call_ts:
        row["response"] = "competing_foreign_write"
        row["response_ts"] = competing_write.result_ts
        return
    if (
        competing_write is not None
        and not reader_action.result_ts < competing_write.call_ts
    ):
        row["response"] = "ambiguous_reader_action_vs_competing_write"
        row["response_ts"] = max(reader_action.result_ts, competing_write.result_ts)
        return

    row["response_ts"] = reader_action.result_ts
    if reader_action.kind == "r":
        if reader_action.read_interval is None:
            row["response"] = "reread_unlocalized_first"
            row["unlocalized_reread_count"] = 1
        elif read_touches_changed(
            reader_action.read_interval, changed, changed_anchors
        ):
            row["response"] = "reread_changed_region_first"
        else:
            row["response"] = "reread_elsewhere_first"
            row["nonoverlap_reread_count"] = 1
        return

    if reader_action.patch_status != "exact" or not reader_action.patch:
        row["response"] = "edit_unlocalized_first"
        return
    if target_touches_patch(footprint, footprint_anchors, reader_action.patch):
        row["response"] = "relevant_edit"
        row["index_edit_id"] = reader_action.tool_id
        row["index_patch"] = reader_action.patch
        row["index_touches_changed"] = target_touches_patch(
            changed, changed_anchors, reader_action.patch
        )
    else:
        row["response"] = "edit_elsewhere_first"
        row["unrelated_edit_count"] = 1


def build_strict_hazard_pairs(
    operations: Sequence[Operation],
    cutoff: float,
    diagnostics: collections.Counter[str],
) -> list[dict[str, Any]]:
    """Build the deduplicated same-session pair population behind the join.

    The historical one-hour window remains call-to-call for comparability, but
    a pair is admitted only when A's successful Read result completed before
    B invoked the write.  The stricter ordering removes concurrent tool calls.
    """

    cutoff_ns = int(cutoff * 1_000_000_000)
    eligible = [
        op for op in operations
        if op.success
        and op.legacy_path is not None
        and is_code_path(op.legacy_path)
        and op.call_ts <= cutoff
        and op.origin_created_ns <= cutoff_ns
    ]
    by_legacy_path: dict[tuple[str, str], list[Operation]] = collections.defaultdict(list)
    by_canonical_path: dict[tuple[str, str], list[Operation]] = collections.defaultdict(list)
    for op in eligible:
        assert op.legacy_path is not None
        by_legacy_path[(op.session, op.legacy_path)].append(op)
    # Follow-up is observed through the frozen transcript end.  The historical
    # cutoff applies only to opening-pair endpoints, not to later responses.
    for op in operations:
        if op.success and op.path is not None and is_code_path(op.path):
            by_canonical_path[(op.session, op.path)].append(op)
    for values in by_canonical_path.values():
        values.sort(key=lambda op: (op.result_ts, op.call_ts, op.tool_id))

    rows: list[dict[str, Any]] = []
    for (session, legacy_name), path_ops in by_legacy_path.items():
        reads = [op for op in path_ops if op.kind == "r"]
        writes = [op for op in path_ops if op.kind == "w"]
        for read in reads:
            for write in writes:
                delta = write.call_ts - read.call_ts
                if not (0 < delta <= WINDOW_SECONDS) or read.actor == write.actor:
                    continue
                if not read.result_ts < write.call_ts:
                    diagnostics["strict_pairs_excluded_for_overlapping_calls"] += 1
                    continue

                offset_class = "unknown"
                boundary_contact = False
                destructive = False
                internal_insertion = False
                if (
                    read.read_interval is not None
                    and write.patch_status == "exact"
                    and write.patch
                ):
                    contact = classify_change_overlap((read.read_interval,), write.patch)
                    offset_class = "region_overlapping" if contact.strict else "file_only"
                    boundary_contact = contact.boundary_insertion
                    destructive = contact.destructive
                    internal_insertion = contact.internal_insertion

                validation = opening_preimage_validation(read, write)
                verified_class = (
                    offset_class
                    if validation == "matched" and offset_class != "unknown"
                    else "unknown"
                )
                canonical_match = (
                    read.path is not None and write.path is not None and read.path == write.path
                )
                path = read.path if canonical_match else (read.path or write.path or legacy_name)
                row: dict[str, Any] = {
                    "session": session,
                    "path": path,
                    "reader": read.actor,
                    "writer": write.actor,
                    "read_id": read.tool_id,
                    "opening_write_id": write.tool_id,
                    "opening_call_ts": write.call_ts,
                    "start_ts": write.result_ts,
                    "offset_region_class": offset_class,
                    "region_class": verified_class,
                    "opening_preimage_validation": validation,
                    "read_localized": read.read_interval is not None,
                    "write_patch_localized": write.patch_status == "exact" and bool(write.patch),
                    "boundary_insertion_contact": boundary_contact,
                    "destructive_overlap": destructive,
                    "internal_insertion_overlap": internal_insertion,
                    "canonical_path_match": canonical_match,
                    "read_identity_conflict": read.identity_conflict,
                    "write_identity_conflict": write.identity_conflict,
                }
                follow_hazard_pair(
                    read,
                    write,
                    by_canonical_path.get((session, path), ()),
                    row,
                )
                rows.append(row)
    diagnostics["strict_ordered_hazard_pairs"] = len(rows)
    return rows


def attach_rework_outcomes(
    episodes: list[dict[str, Any]],
    operations: Sequence[Operation],
    session_last: dict[str, float],
    snapshot_epoch: float,
) -> None:
    by_session_path: dict[tuple[str, str], list[Operation]] = collections.defaultdict(list)
    for op in operations:
        if op.success and op.path is not None and op.kind == "w":
            by_session_path[(op.session, op.path)].append(op)
    for values in by_session_path.values():
        values.sort(key=lambda op: (op.call_ts, op.result_ts, op.tool_id))

    for episode in episodes:
        episode["rework"] = None
        episode["clean"] = None
        episode["reverted"] = False
        episode["reedited_by_reader"] = False
        episode["superseded"] = False
        episode["followup_chain_known"] = False
        if episode["response"] != "relevant_edit" or not episode["index_patch"]:
            continue
        target = changed_post_regions(episode["index_patch"])
        target_anchors = changed_post_anchors(episode["index_patch"])
        if not target and not target_anchors:
            continue
        chain_known = True
        rework = False
        previous_result_ts = episode["response_ts"]
        for op in by_session_path[(episode["session"], episode["path"])]:
            if op.tool_id == episode["index_edit_id"]:
                continue
            if op.result_ts <= episode["response_ts"]:
                continue
            if op.call_ts <= episode["response_ts"]:
                chain_known = False
                break
            if op.call_ts <= previous_result_ts:
                # Concurrent file writes need not share a common preimage, so
                # no single coordinate transform is licensed.
                chain_known = False
                break
            if op.patch_status != "exact" or not op.patch:
                chain_known = False
                break
            contact = target_touches_patch(target, target_anchors, op.patch)
            if contact:
                rework = True
                if op.actor == episode["reader"]:
                    episode["reedited_by_reader"] = True
                else:
                    episode["superseded"] = True
                if is_exact_inverse_patch(episode["index_patch"], op.patch):
                    episode["reverted"] = True
                break
            target = transform_intervals_through_changes(target, op.patch).intervals
            target_anchors = tuple(
                transform_anchor(anchor, op.patch) for anchor in target_anchors
            )
            previous_result_ts = op.result_ts

        episode["followup_chain_known"] = chain_known
        if rework:
            episode["rework"] = True
            episode["clean"] = False
            continue
        last_ts = session_last.get(episode["session"], episode["response_ts"])
        enough_followup = last_ts - episode["response_ts"] >= MIN_CLEAN_FOLLOWUP_SECONDS
        quiescent = snapshot_epoch - last_ts >= QUIESCENCE_SECONDS
        if chain_known and enough_followup and quiescent:
            episode["rework"] = False
            episode["clean"] = True


def summarize_schema(operations: Sequence[Operation], cutoff: float) -> dict[str, Any]:
    cutoff_ns = int(cutoff * 1_000_000_000)
    cohort = [
        op for op in operations
        if op.call_ts <= cutoff
        and op.origin_created_ns <= cutoff_ns
    ]
    output: dict[str, Any] = {}
    for tool in ("Read", "Edit", "Write"):
        chosen = [op for op in cohort if op.tool == tool]
        successful = [op for op in chosen if op.success]
        actor_breakdown: dict[str, collections.Counter[str]] = {
            "main": collections.Counter(),
            "explicit_sidechain": collections.Counter(),
        }
        for op in successful:
            key = "explicit_sidechain" if op.explicit_agent else "main"
            actor_breakdown[key]["successful"] += 1
            if op.read_interval is not None:
                actor_breakdown[key][f"read_{op.read_source}"] += 1
            actor_breakdown[key][f"patch_{op.patch_status}"] += int(op.kind == "w")
        result = {
            "calls_with_linked_result": len(chosen),
            "successful": len(successful),
            "errors": len(chosen) - len(successful),
            "actor_breakdown": {
                key: dict(sorted(counter.items()))
                for key, counter in actor_breakdown.items()
            },
        }
        if tool == "Read":
            result.update(
                {
                    "localized_structured": sum(op.read_source == "structured_result" for op in successful),
                    "localized_visible_fallback": sum(op.read_source == "visible_result_fallback" for op in successful),
                    "localized_total": sum(op.read_interval is not None for op in successful),
                }
            )
        else:
            result.update(
                {
                    "patch_exact_nonempty": sum(op.patch_status == "exact" and bool(op.patch) for op in successful),
                    "patch_empty": sum(op.patch_status == "empty" for op in successful),
                    "patch_missing_or_invalid": sum(op.patch_status in {"missing", "invalid"} for op in successful),
                    "original_file_nonempty": sum(op.original_file_status == "nonempty_string" for op in successful),
                    "original_file_null": sum(op.original_file_status == "null" for op in successful),
                }
            )
        output[tool] = result
    return output


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float] | None:
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot take a percentile of an empty sequence")
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def clustered_proportion_interval(
    rows: Sequence[dict[str, Any]],
    *,
    class_field: str,
    positive_value: str,
    cluster_field: str,
    iterations: int = 10_000,
) -> tuple[float, float] | None:
    groups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in rows:
        groups[str(row[cluster_field])].append(row)
    keys = sorted(groups)
    if len(keys) < 2:
        return None
    seed_material = f"hazard-invalidation-v1:{class_field}:{cluster_field}".encode()
    seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:8], "big")
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(iterations):
        sampled = [rng.choice(keys) for _ in keys]
        total = 0
        positive = 0
        for key in sampled:
            values = groups[key]
            total += len(values)
            positive += sum(row[class_field] == positive_value for row in values)
        if total:
            draws.append(positive / total)
    if not draws:
        return None
    return percentile(draws, 0.025), percentile(draws, 0.975)


def summarize_episodes(episodes: Sequence[dict[str, Any]]) -> dict[str, Any]:
    region_counts = collections.Counter(ep["region_class"] for ep in episodes)
    flow: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for episode in episodes:
        flow[episode["region_class"]][episode["response"]] += 1

    classifiable = [ep for ep in episodes if ep["region_class"] in {"region_overlapping", "file_only"}]
    overlap = [ep for ep in classifiable if ep["region_class"] == "region_overlapping"]
    overlap_sessions = {ep["session"] for ep in overlap}
    overlap_paths = {ep["path"] for ep in overlap}
    verified_session_interval = clustered_proportion_interval(
        classifiable,
        class_field="region_class",
        positive_value="region_overlapping",
        cluster_field="session",
    )
    verified_path_interval = clustered_proportion_interval(
        classifiable,
        class_field="region_class",
        positive_value="region_overlapping",
        cluster_field="path",
    )
    overlap_sufficient = (
        len(classifiable) >= 100
        and len(overlap) >= 30
        and len(overlap_sessions) >= 20
        and len(overlap_paths) >= 20
        and verified_session_interval is not None
        and verified_path_interval is not None
        and verified_session_interval[1] - verified_session_interval[0] <= 0.20
        and verified_path_interval[1] - verified_path_interval[0] <= 0.20
    )
    overlap_fraction = len(overlap) / len(classifiable) if overlap_sufficient else None

    opening_counts = collections.Counter(ep["opening_region_class"] for ep in episodes)
    opening_classifiable = [
        ep for ep in episodes
        if ep["opening_region_class"] in {"region_overlapping", "file_only"}
    ]
    opening_overlap = [
        ep for ep in opening_classifiable
        if ep["opening_region_class"] == "region_overlapping"
    ]
    opening_sessions = {ep["session"] for ep in opening_overlap}
    opening_paths = {ep["path"] for ep in opening_overlap}
    opening_session_interval = clustered_proportion_interval(
        opening_classifiable,
        class_field="opening_region_class",
        positive_value="region_overlapping",
        cluster_field="session",
    )
    opening_path_interval = clustered_proportion_interval(
        opening_classifiable,
        class_field="opening_region_class",
        positive_value="region_overlapping",
        cluster_field="path",
    )
    opening_sufficient = (
        len(opening_classifiable) >= 100
        and len(opening_overlap) >= 30
        and len(opening_sessions) >= 20
        and len(opening_paths) >= 20
        and opening_session_interval is not None
        and opening_path_interval is not None
        and opening_session_interval[1] - opening_session_interval[0] <= 0.20
        and opening_path_interval[1] - opening_path_interval[0] <= 0.20
    )

    table = {
        "region_overlapping": {"rework_followed": 0, "clean_followed": 0},
        "file_only": {"rework_followed": 0, "clean_followed": 0},
    }

    eligible: list[dict[str, Any]] = []
    for episode in episodes:
        if episode["region_class"] not in table:
            continue
        if episode.get("rework") is True:
            table[episode["region_class"]]["rework_followed"] += 1
            eligible.append(episode)
        elif episode.get("clean") is True:
            table[episode["region_class"]]["clean_followed"] += 1
            eligible.append(episode)

    broad_overlap_outcomes = [
        ep for ep in eligible if ep["region_class"] == "region_overlapping"
    ]
    overlap_outcomes = [
        ep for ep in broad_overlap_outcomes if ep["index_touches_changed"]
    ]
    overlap_rework = sum(ep.get("rework") is True for ep in overlap_outcomes)
    outcome_sessions = {ep["session"] for ep in overlap_outcomes}
    outcome_paths = {ep["path"] for ep in overlap_outcomes}
    interval = wilson_interval(overlap_rework, len(overlap_outcomes))
    outcome_sufficient = (
        len(overlap_outcomes) >= 100
        and len(outcome_sessions) >= 20
        and len(outcome_paths) >= 20
        and overlap_rework >= 5
        and len(overlap_outcomes) - overlap_rework >= 5
        and interval is not None
        and interval[1] - interval[0] <= 0.20
    )
    rate = overlap_rework / len(overlap_outcomes) if outcome_sufficient else None

    return {
        "total_response_episodes": len(episodes),
        "region_class_counts": dict(sorted(region_counts.items())),
        "flow_by_region_class": {
            key: dict(sorted(counter.items())) for key, counter in sorted(flow.items())
        },
        "overlap_measurement": {
            "classifiable_episodes": len(classifiable),
            "region_overlapping": len(overlap),
            "file_only": len(classifiable) - len(overlap),
            "region_overlap_sessions": len(overlap_sessions),
            "region_overlap_paths": len(overlap_paths),
            "sample_sufficient": overlap_sufficient,
            "descriptive_fraction": len(overlap) / len(classifiable) if classifiable else None,
            "fraction": overlap_fraction,
            "naive_wilson_95": wilson_interval(len(overlap), len(classifiable)) if classifiable else None,
            "session_cluster_bootstrap_95": verified_session_interval,
            "path_cluster_bootstrap_95": verified_path_interval,
            "primary_overlap_rule": "deleted/replaced read line or insertion strictly inside the read window; edge insertions are sensitivity-only",
        },
        "opening_write_overlap_measurement": {
            "region_class_counts": dict(sorted(opening_counts.items())),
            "classifiable_episodes": len(opening_classifiable),
            "region_overlapping": len(opening_overlap),
            "file_only": len(opening_classifiable) - len(opening_overlap),
            "region_overlap_sessions": len(opening_sessions),
            "region_overlap_paths": len(opening_paths),
            "sample_sufficient": opening_sufficient,
            "descriptive_fraction": (
                len(opening_overlap) / len(opening_classifiable)
                if opening_classifiable else None
            ),
            "fraction": (
                len(opening_overlap) / len(opening_classifiable)
                if opening_sufficient else None
            ),
            "naive_wilson_95": wilson_interval(
                len(opening_overlap), len(opening_classifiable)
            ) if opening_classifiable else None,
            "session_cluster_bootstrap_95": opening_session_interval,
            "path_cluster_bootstrap_95": opening_path_interval,
            "boundary_only_contacts": sum(
                ep["opening_region_class"] == "file_only"
                and ep["opening_boundary_insertion_contact"]
                for ep in episodes
            ),
            "preimage_validation": dict(sorted(collections.Counter(
                ep["opening_preimage_validation"] for ep in episodes
            ).items())),
            "actor_topology": dict(sorted(collections.Counter(
                (
                    ("main" if ep["reader"] == "MAIN" else "explicit_sidechain")
                    + "_reader__"
                    + ("main" if ep["opening_writer"] == "MAIN" else "explicit_sidechain")
                    + "_writer"
                )
                for ep in episodes
            ).items())),
        },
        "contingency": table,
        "confirmed_invalidation_proxy": {
            "eligible_read_region_overlap_outcomes": len(broad_overlap_outcomes),
            "eligible_region_overlap_outcomes": len(overlap_outcomes),
            "rework_followed": overlap_rework,
            "clean_followed": len(overlap_outcomes) - overlap_rework,
            "sessions": len(outcome_sessions),
            "paths": len(outcome_paths),
            "sample_sufficient": outcome_sufficient,
            "fraction": rate,
            "wilson_95": interval if outcome_sufficient else None,
            "rate_withheld_reason": None if outcome_sufficient else (
                "requires >=100 observable edits touching B's changed footprint across >=20 sessions and >=20 paths, "
                "both outcome counts >=5, and a 95% interval no wider than 20 percentage points"
            ),
        },
        "rework_subtypes": {
            "exact_revert": sum(ep.get("reverted") is True for ep in eligible),
            "reader_reedit": sum(ep.get("reedited_by_reader") is True for ep in eligible),
            "foreign_supersession": sum(ep.get("superseded") is True for ep in eligible),
        },
    }


def summarize_hazard_pairs(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate the original ordered-pair estimand without hiding dependence."""

    offset_rows = [
        row for row in rows
        if row["canonical_path_match"]
        if row["offset_region_class"] in {"region_overlapping", "file_only"}
    ]
    offset_overlap = [
        row for row in offset_rows if row["offset_region_class"] == "region_overlapping"
    ]
    verified_rows = [
        row for row in rows
        if row["canonical_path_match"]
        if row["region_class"] in {"region_overlapping", "file_only"}
    ]
    verified_overlap = [
        row for row in verified_rows if row["region_class"] == "region_overlapping"
    ]

    def overlap_measurement(
        selected: Sequence[dict[str, Any]],
        overlap: Sequence[dict[str, Any]],
        class_field: str,
        *,
        version_verified: bool,
    ) -> dict[str, Any]:
        sessions = {row["session"] for row in overlap}
        paths = {row["path"] for row in overlap}
        session_interval = clustered_proportion_interval(
            selected,
            class_field=class_field,
            positive_value="region_overlapping",
            cluster_field="session",
        )
        path_interval = clustered_proportion_interval(
            selected,
            class_field=class_field,
            positive_value="region_overlapping",
            cluster_field="path",
        )
        sufficient = (
            version_verified
            and len(selected) >= 100
            and len(overlap) >= 30
            and len(sessions) >= 20
            and len(paths) >= 20
            and session_interval is not None
            and path_interval is not None
            and session_interval[1] - session_interval[0] <= 0.20
            and path_interval[1] - path_interval[0] <= 0.20
        )
        return {
            "classifiable_pairs": len(selected),
            "region_overlapping": len(overlap),
            "file_only": len(selected) - len(overlap),
            "classifiable_sessions": len({row["session"] for row in selected}),
            "classifiable_paths": len({row["path"] for row in selected}),
            "region_overlap_sessions": len(sessions),
            "region_overlap_paths": len(paths),
            "opening_writes": len({row["opening_write_id"] for row in selected}),
            "version_verified": version_verified,
            "sample_sufficient": sufficient,
            "descriptive_fraction": len(overlap) / len(selected) if selected else None,
            "fraction": len(overlap) / len(selected) if sufficient else None,
            "naive_wilson_95": wilson_interval(len(overlap), len(selected)) if selected else None,
            "session_cluster_bootstrap_95": session_interval,
            "path_cluster_bootstrap_95": path_interval,
            "boundary_only_contacts": sum(
                row[class_field] == "file_only" and row["boundary_insertion_contact"]
                for row in selected
            ),
            "rate_withheld_reason": None if sufficient else (
                "requires exact Read-content/B-preimage agreement, >=100 classifiable pairs, "
                ">=20 overlap sessions and paths, and session/path clustered 95% intervals "
                "no wider than 20 percentage points"
            ),
        }

    offset_measurement = overlap_measurement(
        offset_rows,
        offset_overlap,
        "offset_region_class",
        version_verified=False,
    )
    verified_measurement = overlap_measurement(
        verified_rows,
        verified_overlap,
        "region_class",
        version_verified=True,
    )

    exposure_groups: dict[tuple[str, str, str, str], set[str]] = collections.defaultdict(set)
    for row in offset_rows:
        exposure_groups[
            (row["session"], row["reader"], row["path"], row["opening_write_id"])
        ].add(row["offset_region_class"])
    grouped_classes = collections.Counter(
        "mixed" if len(classes) > 1 else next(iter(classes))
        for classes in exposure_groups.values()
    )

    flow: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    offset_flow: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for row in rows:
        flow[row["region_class"]][row["response"]] += 1
        offset_flow[row["offset_region_class"]][row["response"]] += 1

    table = {
        "region_overlapping": {"rework_followed": 0, "clean_followed": 0},
        "file_only": {"rework_followed": 0, "clean_followed": 0},
    }
    eligible: list[dict[str, Any]] = []
    for row in verified_rows:
        if row.get("rework") is True:
            table[row["region_class"]]["rework_followed"] += 1
            eligible.append(row)
        elif row.get("clean") is True:
            table[row["region_class"]]["clean_followed"] += 1
            eligible.append(row)

    broad_overlap_outcomes = [
        row for row in eligible if row["region_class"] == "region_overlapping"
    ]
    overlap_outcomes = [
        row for row in broad_overlap_outcomes if row["index_touches_changed"]
    ]
    rate_rows = [
        dict(
            row,
            outcome_class=(
                "rework_followed" if row.get("rework") is True else "clean_followed"
            ),
        )
        for row in overlap_outcomes
    ]
    rework_count = sum(row.get("rework") is True for row in overlap_outcomes)
    outcome_sessions = {row["session"] for row in overlap_outcomes}
    outcome_paths = {row["path"] for row in overlap_outcomes}
    session_outcome_interval = clustered_proportion_interval(
        rate_rows,
        class_field="outcome_class",
        positive_value="rework_followed",
        cluster_field="session",
    )
    path_outcome_interval = clustered_proportion_interval(
        rate_rows,
        class_field="outcome_class",
        positive_value="rework_followed",
        cluster_field="path",
    )
    outcome_sufficient = (
        len(overlap_outcomes) >= 100
        and len(outcome_sessions) >= 20
        and len(outcome_paths) >= 20
        and rework_count >= 5
        and len(overlap_outcomes) - rework_count >= 5
        and session_outcome_interval is not None
        and path_outcome_interval is not None
        and session_outcome_interval[1] - session_outcome_interval[0] <= 0.20
        and path_outcome_interval[1] - path_outcome_interval[0] <= 0.20
    )

    offset_preimage = collections.Counter(
        row["opening_preimage_validation"] for row in offset_rows
    )

    def behavior_counts(selected: Sequence[dict[str, Any]]) -> dict[str, int]:
        return {
            "pairs": len(selected),
            "any_reader_write_after_opening_ignoring_censor": sum(
                row["reader_write_after_opening_any"] for row in selected
            ),
            "exact_reader_write_after_opening_ignoring_censor": sum(
                row["exact_reader_write_after_opening_any"] for row in selected
            ),
            "localized_reread_after_opening_ignoring_censor": sum(
                row["localized_reread_after_opening_any"] for row in selected
            ),
            "localized_edit_touching_propagated_read_region_first": sum(
                row["response"] == "relevant_edit" for row in selected
            ),
            "unlocalized_reader_edit_first": sum(
                row["response"] == "edit_unlocalized_first" for row in selected
            ),
            "reread_of_changed_region_first": sum(
                row["response"] == "reread_changed_region_first" for row in selected
            ),
        }

    def topology_counts(selected: Sequence[dict[str, Any]]) -> dict[str, int]:
        return dict(sorted(collections.Counter(
            (
                ("main" if row["reader"] == "MAIN" else "explicit_sidechain")
                + "_reader__"
                + ("main" if row["writer"] == "MAIN" else "explicit_sidechain")
                + "_writer"
            )
            for row in selected
        ).items()))

    return {
        "strict_ordered_pairs": len(rows),
        "offset_only_overlap_measurement": offset_measurement,
        "preimage_verified_overlap_measurement": verified_measurement,
        "offset_classifiable_preimage_validation": dict(sorted(offset_preimage.items())),
        "offset_attrition": {
            "canonical_path_mismatch": sum(
                not row["canonical_path_match"] for row in rows
            ),
            "read_unlocalized": sum(not row["read_localized"] for row in rows),
            "write_patch_unlocalized_after_localized_read": sum(
                row["read_localized"] and not row["write_patch_localized"]
                for row in rows
            ),
            "write_patch_unlocalized_independent": sum(
                not row["write_patch_localized"] for row in rows
            ),
            "both_read_and_write_unlocalized": sum(
                not row["read_localized"] and not row["write_patch_localized"]
                for row in rows
            ),
            "read_unlocalized_after_localized_write": sum(
                row["write_patch_localized"] and not row["read_localized"] for row in rows
            ),
        },
        "reader_write_exposure_groups": {
            "groups": len(exposure_groups),
            "all_region_overlapping": grouped_classes["region_overlapping"],
            "mixed_read_pairs": grouped_classes["mixed"],
            "all_file_only": grouped_classes["file_only"],
        },
        "actor_topology": {
            "all_strict_pairs": topology_counts(rows),
            "offset_classifiable_pairs": topology_counts(offset_rows),
            "preimage_verified_pairs": topology_counts(verified_rows),
        },
        "flow_by_verified_region_class": {
            key: dict(sorted(counter.items())) for key, counter in sorted(flow.items())
        },
        "flow_by_offset_region_class": {
            key: dict(sorted(counter.items()))
            for key, counter in sorted(offset_flow.items())
        },
        "subsequent_behavior": {
            "all_strict_pairs": behavior_counts(rows),
            "offset_classifiable_pairs": behavior_counts(offset_rows),
            "preimage_verified_pairs": behavior_counts(verified_rows),
            "verified_overlap_no_reread_then_relevant_edit": sum(
                row["region_class"] == "region_overlapping"
                and row["response"] == "relevant_edit"
                for row in rows
            ),
        },
        "contingency": table,
        "confirmed_invalidation_proxy": {
            "eligible_read_region_overlap_outcomes": len(broad_overlap_outcomes),
            "eligible_region_overlap_outcomes": len(overlap_outcomes),
            "rework_followed": rework_count,
            "clean_followed": len(overlap_outcomes) - rework_count,
            "sessions": len(outcome_sessions),
            "paths": len(outcome_paths),
            "sample_sufficient": outcome_sufficient,
            "fraction": (
                rework_count / len(overlap_outcomes) if outcome_sufficient else None
            ),
            "session_cluster_bootstrap_95": (
                session_outcome_interval if outcome_sufficient else None
            ),
            "path_cluster_bootstrap_95": (
                path_outcome_interval if outcome_sufficient else None
            ),
            "rate_withheld_reason": None if outcome_sufficient else (
                "requires >=100 observable verified-overlap edits touching B's changed "
                "footprint across >=20 "
                "sessions and paths, both outcome counts >=5, and session/path clustered "
                "95% intervals no wider than 20 percentage points"
            ),
        },
        "identity_conflict_sensitivity": {
            "pairs_without_conflicted_endpoints": sum(
                not row["read_identity_conflict"] and not row["write_identity_conflict"]
                for row in rows
            ),
            "offset_classifiable_without_conflicted_endpoints": sum(
                row["canonical_path_match"]
                and row["offset_region_class"] in {"region_overlapping", "file_only"}
                and not row["read_identity_conflict"]
                and not row["write_identity_conflict"]
                for row in rows
            ),
            "verified_classifiable_without_conflicted_endpoints": sum(
                row["canonical_path_match"]
                and row["region_class"] in {"region_overlapping", "file_only"}
                and not row["read_identity_conflict"]
                and not row["write_identity_conflict"]
                for row in rows
            ),
        },
        "rework_subtypes": {
            "exact_revert": sum(row.get("reverted") is True for row in eligible),
            "reader_reedit": sum(row.get("reedited_by_reader") is True for row in eligible),
            "foreign_supersession": sum(row.get("superseded") is True for row in eligible),
        },
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path.home() / ".claude" / "projects")
    parser.add_argument("--cohort-cutoff", default=DEFAULT_COHORT_CUTOFF)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exploratory/hazard/invalidation-results.json"),
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=Path("exploratory/hazard/invalidation-corpus-manifest.json"),
    )
    parser.add_argument("--progress-every", type=int, default=250)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    corpus = args.corpus.resolve()
    if not corpus.is_dir():
        raise SystemExit(f"transcript corpus is not a directory: {corpus}")
    cutoff = parse_timestamp(args.cohort_cutoff)
    if cutoff is None:
        raise SystemExit(f"invalid --cohort-cutoff: {args.cohort_cutoff!r}")

    operations, legacy_events, metadata, manifest_files = scan_corpus(
        corpus, cutoff, args.progress_every
    )
    pair_diagnostics: collections.Counter[str] = collections.Counter()
    hazard_pairs = build_strict_hazard_pairs(operations, cutoff, pair_diagnostics)
    episode_diagnostics: collections.Counter[str] = collections.Counter()
    episodes = build_response_episodes(operations, cutoff, episode_diagnostics)
    session_last = {
        key: float(value) for key, value in metadata.pop("session_last_timestamp").items()
    }
    attach_rework_outcomes(
        hazard_pairs,
        operations,
        session_last,
        float(metadata["snapshot_epoch"]),
    )
    attach_rework_outcomes(
        episodes,
        operations,
        session_last,
        float(metadata["snapshot_epoch"]),
    )

    result = {
        "schema_version": 2,
        "measurement": "read-foreign-write-pairs-and-response-episodes",
        "metadata": metadata,
        "definitions": {
            "actor": "(sessionId, agentId), with MAIN for records lacking agentId",
            "ordering": "Read successful result completes before foreign write invocation",
            "window_seconds": WINDOW_SECONDS,
            "scope": "same session and canonical code path",
            "opening_overlap_unit": "one deduplicated same-session ordered Read/foreign-Write pair from the historical one-hour call-to-call population, additionally requiring the Read result to precede the write call",
            "downstream_unit": "one reader/path response episode opened by the first definite foreign write; a later foreign write is a competing exposure that censors follow-up rather than being merged",
            "region_verification": "opening Read lines must match B's non-null originalFile at the reported offsets before exact patch contact is called verified; offset-only rows are retained as a tagged sensitivity",
            "central_outcome_eligibility": "verified region-overlap opening plus a localized reader index edit that touches B's propagated changed footprint",
            "contingency_eligibility": "broader requested table: a localized reader index edit touches any propagated part of the prior read footprint; reported separately from the central rate",
            "clean": "qualifying localized reader edit, intact structured-write follow-up chain, >=5 minutes observed follow-up, quiescent session, and no structural rework",
            "rework": "later exact revert, same-reader re-edit, or foreign supersession touching the propagated index-edit footprint",
        },
        "historical_recount_forensic_replication": {
            "all_files": hazard_counts(legacy_events, code_only=False),
            "code_only": hazard_counts(legacy_events, code_only=True),
            "duplication": legacy_duplication(legacy_events),
        },
        "successful_deduplicated_pair_sensitivity": deduplicated_pair_counts(operations, cutoff),
        "schema_coverage_in_deduplicated_cohort": summarize_schema(operations, cutoff),
        "pair_diagnostics": dict(sorted(pair_diagnostics.items())),
        "hazard_pair_results": summarize_hazard_pairs(hazard_pairs),
        "episode_diagnostics": dict(sorted(episode_diagnostics.items())),
        "episode_results": summarize_episodes(episodes),
    }
    manifest = {
        "schema_version": 1,
        "snapshot_utc": metadata["snapshot_utc"],
        "corpus_root_redacted": True,
        "file_count": metadata["corpus_file_count"],
        "byte_count": metadata["corpus_bytes"],
        "snapshot_sha256": metadata["corpus_snapshot_sha256"],
        "files": manifest_files,
    }
    atomic_json(args.output.resolve(), result)
    atomic_json(args.manifest_output.resolve(), manifest)
    print(f"wrote aggregate results to {args.output.resolve()}", flush=True)
    print(f"wrote byte-prefix manifest to {args.manifest_output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
