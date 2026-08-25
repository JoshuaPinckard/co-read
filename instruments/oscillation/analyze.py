"""Measure cross-agent reversals and region oscillation in Claude transcripts.

This extends the JSONL event walk in ``instruments/hazard/extract_hazards.py``
without changing that historical instrument.  The legacy walker is call-side
and path-only; this analysis pairs calls to results source-locally, uses result
paths, then globally deduplicates copied tool-use IDs.  The transcript corpus
is opened read-only.  A byte-prefix snapshot is fixed before the first read.

Primary population
------------------
``D_pair`` contains adjacent writes to one result-reported path that are:

* successful and backed by an exact structured patch plus string pre-image;
* by different agents (``agentId`` when present, otherwise ``sessionId``);
* strictly serialized (A result precedes B call);
* state-continuous at the changed region after symmetric exact-line alignment; and
* overlapping in the aligned changed-line coordinates.

``D_seq`` contains maximal runs of writes connected by those pair edges.
Pair labels are exact reversal, partial reversal, or independent co-editing.
Sequence labels are mutually exclusive with precedence oscillation, exact,
partial, independent.  Partial reversal uses a lexical inverse-delta score;
the baseline threshold is 0.75 and fixed-denominator sensitivities are emitted
at 0.50 and 0.90.
"""

from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass
import datetime as dt
import difflib
import hashlib
import json
import math
import ntpath
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Iterable, Mapping, Sequence

try:
    from instruments.hazard.invalidation_core import (
        ChangeBlock,
        is_exact_inverse_patch,
        normalize_windows_path,
        parse_structured_patch,
    )
except ModuleNotFoundError:  # direct execution from instruments/oscillation
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hazard"))
    from invalidation_core import (  # type: ignore[no-redef]
        ChangeBlock,
        is_exact_inverse_patch,
        normalize_windows_path,
        parse_structured_patch,
    )


READ_TOOLS = {"Read", "NotebookRead"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SHELL_TOOLS = {"Bash", "PowerShell", "Powershell"}
ALL_STRUCTURED_TOOLS = READ_TOOLS | WRITE_TOOLS
PARTIAL_THRESHOLDS = (0.50, 0.75, 0.90)
PRIMARY_PARTIAL_THRESHOLD = 0.75

SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".cxx", ".go", ".h", ".hpp",
    ".java", ".js", ".jsx", ".kt", ".kts", ".lua", ".mjs", ".php",
    ".pl", ".ps1", ".py", ".r", ".rb", ".rs", ".sh", ".swift",
    ".ts", ".tsx", ".vue", ".zig",
}
DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".adoc", ".txt"}
CONFIG_DATA_EXTENSIONS = {
    ".cfg", ".conf", ".csv", ".env", ".ini", ".json", ".lock",
    ".properties", ".sql", ".toml", ".tsv", ".xml", ".yaml", ".yml",
}
COORDINATION_BASENAMES = {
    "agents.md", "build-queue.md", "build_queue.md", "claude.md",
    "coordination.md", "handoff.md", "memory.md", "progress.md",
    "status.md", "tasks.md", "todo.md",
}
GENERATED_BASENAMES = {
    "cargo.lock", "composer.lock", "go.sum", "package-lock.json",
    "pnpm-lock.yaml", "poetry.lock", "uv.lock", "yarn.lock",
}
GENERATED_PARTS = {
    ".next", "build", "coverage", "dist", "generated", "node_modules",
    "target", "vendor",
}

TOKEN_RE = re.compile(r"[\w]+|[^\w\s]", re.UNICODE)
IMPORT_RE = re.compile(
    r"^(?:"
    r"import\b|from\s+\S+\s+import\b|export\s+.*\s+from\b|"
    r"(?:const|let|var)\s+.+?=\s*require\s*\(|require\s*\(|"
    r"use\s+[^;]+;|using\s+|#\s*include\b|extern\s+crate\b|@import\b"
    r")",
    re.IGNORECASE,
)
GIT_MUTATION_RE = re.compile(
    r"(?:^|[;&|]\s*|\b)git\s+(?:checkout|restore|reset|revert|clean|merge|"
    r"pull|switch|cherry-pick|apply|am)\b",
    re.IGNORECASE,
)
FORMATTER_RE = re.compile(
    r"\b(?:prettier|black|isort|gofmt|rustfmt|clang-format|cargo\s+fmt|"
    r"ruff(?:\s+check)?\s+[^\r\n]*--fix|eslint\s+[^\r\n]*--fix)\b",
    re.IGNORECASE,
)
CODEGEN_RE = re.compile(
    r"\b(?:codegen|generate|generated|protoc|prisma\s+generate|graphql-codegen|"
    r"openapi-generator|swagger-codegen)\b",
    re.IGNORECASE,
)
VOLATILE_METADATA_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:modified|updated|last[ _-]?modified|timestamp|"
    r"generated(?:[ _-]?at)?|date)\s*[:=]",
    re.IGNORECASE,
)


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def iso_utc(epoch: float | None) -> str | None:
    if epoch is None or not math.isfinite(epoch):
        return None
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def logical_lines(content: str) -> tuple[str, ...]:
    """Match the hazard instrument's logical-line convention."""

    lines = content.splitlines()
    if content.endswith(("\n", "\r")):
        lines.append("")
    return tuple(lines)


def canonical_result_path(value: Any, cwd: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = normalize_windows_path(
            value, cwd if isinstance(cwd, str) and cwd else None
        )
    except (TypeError, ValueError):
        return None
    return normalized if ntpath.isabs(normalized) else None


def result_blocks(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]


def apply_patch_lines(
    preimage: Sequence[str], changes: Sequence[ChangeBlock]
) -> tuple[str, ...]:
    """Apply non-overlapping old-coordinate change blocks exactly."""

    output: list[str] = []
    cursor = 0
    for block in sorted(changes, key=lambda item: (item.old_start, item.new_start)):
        index = 0 if block.old_start == 0 else block.old_start - 1
        if index < cursor or index > len(preimage):
            raise ValueError("structured patch blocks overlap or exceed pre-image")
        end = index + len(block.old_lines)
        if tuple(preimage[index:end]) != block.old_lines:
            raise ValueError("structured patch deletion does not match pre-image")
        output.extend(preimage[cursor:index])
        output.extend(block.new_lines)
        cursor = end
    output.extend(preimage[cursor:])
    return tuple(output)


def patch_signature(changes: Sequence[ChangeBlock]) -> tuple[Any, ...]:
    return tuple(
        (
            block.old_start,
            block.new_start,
            block.old_lines,
            block.new_lines,
            block.old_no_newline,
            block.new_no_newline,
        )
        for block in changes
    )


@dataclass(frozen=True)
class Call:
    tool_id: str
    tool: str
    agent: str
    explicit_agent: bool
    session: str
    call_ts: float
    cwd: str | None
    call_uuid: str | None
    source_kind: str
    source_ordinal: int
    source_line: int


@dataclass
class Candidate:
    call: Call
    result_ts: float
    success: bool
    path: str | None = None
    patch: tuple[ChangeBlock, ...] | None = None
    pre_lines: tuple[str, ...] | None = None
    post_lines: tuple[str, ...] | None = None
    read_interval: tuple[int, int] | None = None
    read_lines: tuple[str, ...] | None = None
    metadata_status: str = "missing"


@dataclass(frozen=True)
class Operation:
    tool_id: str
    tool: str
    agent: str
    explicit_agent: bool
    sessions: tuple[str, ...]
    call_ts: float
    result_ts: float
    path: str | None
    success: bool
    patch: tuple[ChangeBlock, ...] | None
    pre_lines: tuple[str, ...] | None
    post_lines: tuple[str, ...] | None
    read_interval: tuple[int, int] | None
    read_lines: tuple[str, ...] | None
    metadata_status: str
    duplicate_occurrences: int

    @property
    def kind(self) -> str:
        return "read" if self.tool in READ_TOOLS else "write"

    @property
    def usable_write(self) -> bool:
        return (
            self.kind == "write"
            and self.path is not None
            and self.patch is not None
            and bool(self.patch)
            and self.pre_lines is not None
            and self.post_lines is not None
        )

    @property
    def localized_read(self) -> bool:
        return self.kind == "read" and self.path is not None and self.read_interval is not None


@dataclass(frozen=True)
class CommandEvent:
    tool_id: str
    agent: str
    sessions: tuple[str, ...]
    cwds: tuple[str, ...]
    timestamp: float
    category: str


def source_kind(path: Path) -> str:
    lowered = [part.casefold() for part in path.parts]
    if "subagents" not in lowered:
        return "main"
    return "workflow_subagent" if "workflows" in lowered else "direct_subagent"


def command_category(command: str) -> str | None:
    if GIT_MUTATION_RE.search(command):
        return "git_mutation"
    if FORMATTER_RE.search(command):
        return "formatter_or_linter"
    if CODEGEN_RE.search(command):
        return "codegen"
    return None


def build_candidate(
    call: Call,
    record: Mapping[str, Any],
    block: Mapping[str, Any],
    top_result: Any,
    diagnostics: collections.Counter[str],
) -> Candidate:
    result_ts = parse_timestamp(record.get("timestamp"))
    if result_ts is None:
        result_ts = call.call_ts
        diagnostics["result_timestamp_fallbacks"] += 1
    success = block.get("is_error") is not True
    candidate = Candidate(call=call, result_ts=result_ts, success=success)
    diagnostics[f"result_occurrences_{call.source_kind}_{call.tool}"] += 1
    diagnostics[
        f"successful_result_occurrences_{call.source_kind}_{call.tool}"
    ] += int(success)
    if not success:
        candidate.metadata_status = "error"
        return candidate

    if call.tool in READ_TOOLS:
        file_result = top_result.get("file") if isinstance(top_result, Mapping) else None
        if not isinstance(file_result, Mapping):
            candidate.metadata_status = "missing_result_file"
            return candidate
        candidate.path = canonical_result_path(file_result.get("filePath"), call.cwd)
        start = file_result.get("startLine")
        count = file_result.get("numLines")
        if (
            isinstance(start, bool)
            or isinstance(count, bool)
            or not isinstance(start, int)
            or not isinstance(count, int)
            or start < 1
            or count < 1
        ):
            candidate.metadata_status = "invalid_read_range"
            return candidate
        candidate.read_interval = (start, start + count)
        content = file_result.get("content")
        if isinstance(content, str):
            lines = logical_lines(content)
            if len(lines) == count:
                candidate.read_lines = lines
            else:
                diagnostics["structured_read_content_length_mismatch"] += 1
        candidate.metadata_status = (
            "exact_read" if candidate.path is not None else "read_path_unusable"
        )
        return candidate

    if not isinstance(top_result, Mapping):
        candidate.metadata_status = "missing_tool_use_result"
        return candidate
    candidate.path = canonical_result_path(top_result.get("filePath"), call.cwd)
    raw_patch = top_result.get("structuredPatch", object())
    original = top_result.get("originalFile", object())
    if not isinstance(raw_patch, list):
        candidate.metadata_status = "missing_structured_patch"
        return candidate
    if not isinstance(original, str):
        candidate.metadata_status = (
            "null_preimage" if original is None else "missing_preimage"
        )
        return candidate
    try:
        changes = parse_structured_patch(raw_patch)
        pre_lines = logical_lines(original)
        post_lines = apply_patch_lines(pre_lines, changes)
    except (TypeError, ValueError):
        candidate.metadata_status = "invalid_patch_or_preimage"
        diagnostics["invalid_patch_or_preimage_occurrences"] += 1
        return candidate
    candidate.patch = changes
    candidate.pre_lines = pre_lines
    candidate.post_lines = post_lines
    if candidate.path is None:
        candidate.metadata_status = "write_path_unusable"
    elif not changes:
        candidate.metadata_status = "empty_patch"
    else:
        candidate.metadata_status = "exact_write"
    return candidate


def choose_identity(
    candidates: Sequence[Candidate], diagnostics: collections.Counter[str]
) -> tuple[str, bool, tuple[str, ...]] | None:
    explicit = {item.call.agent for item in candidates if item.call.explicit_agent}
    sessions = tuple(sorted({item.call.session for item in candidates}))
    if len(explicit) > 1:
        diagnostics["dedup_explicit_agent_conflicts"] += 1
        return None
    if explicit:
        return next(iter(explicit)), True, sessions
    fallback = {item.call.session for item in candidates}
    if len(fallback) != 1:
        diagnostics["dedup_session_identity_conflicts"] += 1
        return None
    return next(iter(fallback)), False, sessions


def merge_candidates(
    tool_id: str,
    candidates: Sequence[Candidate],
    diagnostics: collections.Counter[str],
) -> Operation | None:
    if not candidates:
        return None
    diagnostics["candidate_occurrences"] += len(candidates)
    if len(candidates) > 1:
        diagnostics["duplicated_tool_use_ids"] += 1
        diagnostics["duplicate_candidate_occurrences"] += len(candidates) - 1

    tools = {item.call.tool for item in candidates}
    call_times = {item.call.call_ts for item in candidates}
    result_times = {item.result_ts for item in candidates}
    success_values = {item.success for item in candidates}
    if len(tools) != 1 or len(call_times) != 1 or len(result_times) != 1:
        diagnostics["dedup_call_or_timestamp_conflicts"] += 1
        return None
    if len(success_values) != 1:
        diagnostics["dedup_result_status_conflicts"] += 1
        return None
    identity = choose_identity(candidates, diagnostics)
    if identity is None:
        return None
    agent, explicit_agent, sessions = identity

    paths = {item.path for item in candidates if item.path is not None}
    if len(paths) > 1:
        diagnostics["dedup_result_path_conflicts"] += 1
        return None
    path = next(iter(paths)) if paths else None

    structured_writes = [
        item
        for item in candidates
        if item.patch is not None and item.pre_lines is not None and item.post_lines is not None
    ]
    if structured_writes:
        signatures = {
            (
                patch_signature(item.patch or ()),
                item.pre_lines,
                item.post_lines,
            )
            for item in structured_writes
        }
        if len(signatures) > 1:
            diagnostics["dedup_patch_or_preimage_conflicts"] += 1
            return None
        best = structured_writes[0]
    else:
        structured_reads = [item for item in candidates if item.read_interval is not None]
        if structured_reads:
            signatures = {
                (item.read_interval, item.read_lines) for item in structured_reads
            }
            if len(signatures) > 1:
                diagnostics["dedup_read_metadata_conflicts"] += 1
                return None
            best = max(structured_reads, key=lambda item: item.read_lines is not None)
        else:
            best = candidates[0]

    return Operation(
        tool_id=tool_id,
        tool=next(iter(tools)),
        agent=agent,
        explicit_agent=explicit_agent,
        sessions=sessions,
        call_ts=next(iter(call_times)),
        result_ts=next(iter(result_times)),
        path=path,
        success=next(iter(success_values)),
        patch=best.patch,
        pre_lines=best.pre_lines,
        post_lines=best.post_lines,
        read_interval=best.read_interval,
        read_lines=best.read_lines,
        metadata_status=best.metadata_status,
        duplicate_occurrences=len(candidates),
    )


def scan_corpus(
    corpus: Path,
    *,
    progress_every: int = 250,
) -> tuple[list[Operation], list[CommandEvent], dict[str, Any], list[dict[str, Any]]]:
    """Read a fixed byte prefix of every transcript and reconstruct operations."""

    paths = sorted(corpus.rglob("*.jsonl"), key=lambda item: str(item).casefold())
    snapshots: list[tuple[Path, int]] = []
    for path in paths:
        try:
            snapshots.append((path, path.stat().st_size))
        except OSError:
            snapshots.append((path, -1))

    snapshot_epoch = dt.datetime.now(dt.timezone.utc).timestamp()
    diagnostics: collections.Counter[str] = collections.Counter()
    global_candidates: dict[str, list[Candidate]] = collections.defaultdict(list)
    command_candidates: dict[str, list[tuple[Call, str]]] = collections.defaultdict(list)
    global_digest = hashlib.sha256()
    manifest_files: list[dict[str, Any]] = []

    for ordinal, (path, byte_limit) in enumerate(snapshots, 1):
        kind = source_kind(path)
        file_digest = hashlib.sha256()
        bytes_read = 0
        local_calls: dict[str, list[Call]] = collections.defaultdict(list)
        global_digest.update(str(ordinal).encode("ascii"))
        global_digest.update(b"\0" + str(byte_limit).encode("ascii") + b"\0")
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
                    message = record.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict) or block.get("type") != "tool_use":
                                continue
                            tool = block.get("name")
                            tool_id = block.get("id")
                            input_data = block.get("input")
                            if (
                                not isinstance(tool, str)
                                or not isinstance(tool_id, str)
                                or not isinstance(input_data, dict)
                                or not isinstance(session, str)
                                or record_ts is None
                            ):
                                continue
                            explicit = isinstance(record.get("agentId"), str) and bool(
                                record.get("agentId")
                            )
                            agent = str(record.get("agentId")) if explicit else session
                            call = Call(
                                tool_id=tool_id,
                                tool=tool,
                                agent=agent,
                                explicit_agent=explicit,
                                session=session,
                                call_ts=record_ts,
                                cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                                call_uuid=record.get("uuid") if isinstance(record.get("uuid"), str) else None,
                                source_kind=kind,
                                source_ordinal=ordinal,
                                source_line=line_number,
                            )
                            if tool in ALL_STRUCTURED_TOOLS:
                                local_calls[tool_id].append(call)
                                diagnostics[f"tool_calls_{kind}_{tool}"] += 1
                            elif tool in SHELL_TOOLS:
                                diagnostics[f"shell_calls_{tool}"] += 1
                                command = input_data.get("command") or input_data.get("cmd")
                                if isinstance(command, str):
                                    category = command_category(command)
                                    if category:
                                        command_candidates[tool_id].append((call, category))
                                        diagnostics[f"shell_command_{category}_occurrences"] += 1

                    blocks = result_blocks(record)
                    top_result = record.get("toolUseResult") if len(blocks) == 1 else None
                    parent_uuid = record.get("parentUuid") or record.get(
                        "sourceToolAssistantUUID"
                    )
                    for block in blocks:
                        tool_id = block.get("tool_use_id")
                        if not isinstance(tool_id, str):
                            diagnostics["result_blocks_missing_tool_id"] += 1
                            continue
                        choices = local_calls.get(tool_id, [])
                        if not choices:
                            diagnostics["source_local_results_without_structured_call"] += 1
                            continue
                        exact_parent = [item for item in choices if item.call_uuid == parent_uuid]
                        call = exact_parent[-1] if exact_parent else choices[-1]
                        if not exact_parent:
                            diagnostics["results_without_parent_uuid_match"] += 1
                        candidate = build_candidate(
                            call, record, block, top_result, diagnostics
                        )
                        global_candidates[tool_id].append(candidate)
                        diagnostics[
                            f"metadata_{call.source_kind}_{call.tool}_{candidate.metadata_status}"
                        ] += 1
                        diagnostics["paired_result_occurrences"] += 1
            diagnostics["files_read_successfully"] += 1
        except OSError:
            diagnostics["files_read_failed"] += 1

        manifest_files.append(
            {
                "ordinal": ordinal,
                "byte_length": byte_limit,
                "bytes_read": bytes_read,
                "prefix_sha256": file_digest.hexdigest(),
            }
        )
        if progress_every and (ordinal % progress_every == 0 or ordinal == len(snapshots)):
            print(
                f"scanned {ordinal:,}/{len(snapshots):,} transcript files; "
                f"{len(global_candidates):,} result-linked tool IDs",
                flush=True,
            )

    operations: list[Operation] = []
    for tool_id, candidates in global_candidates.items():
        operation = merge_candidates(tool_id, candidates, diagnostics)
        if operation is not None:
            operations.append(operation)
    operations.sort(key=lambda item: (item.result_ts, item.call_ts, item.tool_id))

    commands: list[CommandEvent] = []
    for tool_id, values in command_candidates.items():
        explicit_agents = {call.agent for call, _ in values if call.explicit_agent}
        fallback_agents = {call.session for call, _ in values}
        categories = {category for _, category in values}
        times = {call.call_ts for call, _ in values}
        if len(explicit_agents) > 1 or len(categories) != 1 or len(times) != 1:
            diagnostics["command_dedup_conflicts"] += 1
            continue
        if explicit_agents:
            agent = next(iter(explicit_agents))
        elif len(fallback_agents) == 1:
            agent = next(iter(fallback_agents))
        else:
            diagnostics["command_dedup_conflicts"] += 1
            continue
        commands.append(
            CommandEvent(
                tool_id=tool_id,
                agent=agent,
                sessions=tuple(sorted(fallback_agents)),
                cwds=tuple(
                    sorted(
                        {
                            normalized
                            for call, _ in values
                            if call.cwd is not None
                            for normalized in [canonical_result_path(call.cwd, None)]
                            if normalized is not None
                        }
                    )
                ),
                timestamp=next(iter(times)),
                category=next(iter(categories)),
            )
        )
    commands.sort(key=lambda item: (item.timestamp, item.tool_id))

    try:
        current_paths = {item for item in corpus.rglob("*.jsonl")}
        snapshot_path_set = {item for item, _ in snapshots}
        added_after_snapshot = len(current_paths - snapshot_path_set)
    except OSError:
        added_after_snapshot = -1

    diagnostics["deduplicated_operations"] = len(operations)
    diagnostics["deduplicated_reads"] = sum(item.kind == "read" for item in operations)
    diagnostics["deduplicated_writes"] = sum(item.kind == "write" for item in operations)
    diagnostics["deduplicated_usable_writes"] = sum(item.usable_write for item in operations)
    diagnostics["deduplicated_localized_reads"] = sum(item.localized_read for item in operations)
    diagnostics["files_added_after_snapshot"] = added_after_snapshot
    metadata = {
        "snapshot_utc": iso_utc(snapshot_epoch),
        "corpus_root": "<redacted-local-claude-projects>",
        "corpus_file_count": len(snapshots),
        "corpus_bytes": sum(max(size, 0) for _, size in snapshots),
        "corpus_snapshot_sha256": global_digest.hexdigest(),
        "identity_rule": "agentId when present across duplicate copies, otherwise sessionId",
        "path_rule": "result metadata only; no tool input path fallback",
        "snapshot_rule": "enumerate files and byte lengths once, then read exactly those prefixes",
        "diagnostics": dict(sorted(diagnostics.items())),
    }
    return operations, commands, metadata, manifest_files


def intervals_contact(
    left_start: int,
    left_length: int,
    right_start: int,
    right_length: int,
    *,
    include_boundary_anchors: bool = False,
) -> bool:
    """Contact for half-open line ranges with explicit zero-width anchors."""

    left_end = left_start + left_length
    right_end = right_start + right_length
    if left_length and right_length:
        return left_start < right_end and right_start < left_end
    if not left_length and not right_length:
        return left_start == right_start
    if not left_length:
        if include_boundary_anchors:
            return right_start <= left_start <= right_end
        return right_start < left_start < right_end
    if include_boundary_anchors:
        return left_start <= right_start <= left_end
    return left_start < right_start < left_end


def blocks_contact(
    forward: ChangeBlock,
    following: ChangeBlock,
    *,
    include_boundary_anchors: bool = False,
) -> bool:
    return intervals_contact(
        forward.new_start,
        len(forward.new_lines),
        following.old_start,
        len(following.old_lines),
        include_boundary_anchors=include_boundary_anchors,
    )


def patches_contact(
    forward: Sequence[ChangeBlock],
    following: Sequence[ChangeBlock],
    *,
    include_boundary_anchors: bool = False,
) -> bool:
    return any(
        blocks_contact(left, right, include_boundary_anchors=include_boundary_anchors)
        for left in forward
        for right in following
    )


def _line_mapping(
    source: Sequence[str], target: Sequence[str]
) -> tuple[dict[int, int], dict[int, int]]:
    """Return symmetric exact-line mappings selected by SequenceMatcher.

    A mapping is retained only when the reverse alignment selects the same
    source/target pair.  This conservatively rejects ambiguous repeated-line
    matches instead of manufacturing a coordinate translation.
    """

    forward: dict[int, int] = {}
    reverse: dict[int, int] = {}
    for match in difflib.SequenceMatcher(
        None, tuple(source), tuple(target), autojunk=False
    ).get_matching_blocks():
        for offset in range(match.size):
            forward[match.a + offset] = match.b + offset
    for match in difflib.SequenceMatcher(
        None, tuple(target), tuple(source), autojunk=False
    ).get_matching_blocks():
        for offset in range(match.size):
            reverse[match.a + offset] = match.b + offset
    symmetric_forward = {
        source_index: target_index
        for source_index, target_index in forward.items()
        if reverse.get(target_index) == source_index
    }
    symmetric_reverse = {
        target_index: source_index
        for source_index, target_index in symmetric_forward.items()
    }
    return symmetric_forward, symmetric_reverse


def _map_region_start(
    source: Sequence[str],
    target: Sequence[str],
    start_coordinate: int,
    length: int,
) -> int | None:
    """Map a 1-based region/anchor from one exact line state to another.

    Non-empty regions must map contiguously and symmetrically.  A zero-width
    anchor is accepted only when the exact lines immediately before and after
    it imply the same target boundary; at a file edge the one available
    neighbor is sufficient.  An insertion precisely at an interior anchor
    therefore makes the mapping ambiguous and is rejected.
    """

    source_index = 0 if start_coordinate == 0 else start_coordinate - 1
    if source_index < 0 or source_index > len(source):
        return None
    mapping, _ = _line_mapping(source, target)
    if length:
        if source_index + length > len(source):
            return None
        mapped = [mapping.get(index) for index in range(source_index, source_index + length)]
        if any(index is None for index in mapped):
            return None
        target_indexes = [int(index) for index in mapped]
        if target_indexes != list(range(target_indexes[0], target_indexes[0] + length)):
            return None
        if tuple(target[target_indexes[0] : target_indexes[0] + length]) != tuple(
            source[source_index : source_index + length]
        ):
            return None
        target_index = target_indexes[0]
    else:
        if not source and not target:
            return 0
        boundaries: list[int] = []
        if source_index > 0 and source_index - 1 in mapping:
            boundaries.append(mapping[source_index - 1] + 1)
        if source_index < len(source) and source_index in mapping:
            boundaries.append(mapping[source_index])
        if not boundaries or len(set(boundaries)) != 1:
            return None
        target_index = boundaries[0]
    if target_index < 0 or target_index > len(target):
        return None
    return 0 if not target and target_index == 0 else target_index + 1


@dataclass(frozen=True)
class PairGeometry:
    """A's changed regions mapped into B's pre/post coordinate systems."""

    mapped_new_starts: tuple[int | None, ...]
    mapped_old_starts: tuple[int | None, ...]
    primary_contacts: tuple[tuple[int, int], ...]
    boundary_contacts: tuple[tuple[int, int], ...]


def build_pair_geometry(left: Operation, right: Operation) -> PairGeometry:
    """Align A post→B pre and A pre→B post before testing region contact."""

    assert left.patch is not None and right.patch is not None
    assert left.pre_lines is not None and left.post_lines is not None
    assert right.pre_lines is not None and right.post_lines is not None
    mapped_new = tuple(
        _map_region_start(
            left.post_lines,
            right.pre_lines,
            block.new_start,
            len(block.new_lines),
        )
        for block in left.patch
    )
    mapped_old = tuple(
        _map_region_start(
            left.pre_lines,
            right.post_lines,
            block.old_start,
            len(block.old_lines),
        )
        for block in left.patch
    )
    primary: list[tuple[int, int]] = []
    boundary: list[tuple[int, int]] = []
    for left_index, mapped_start in enumerate(mapped_new):
        if mapped_start is None:
            continue
        left_length = len(left.patch[left_index].new_lines)
        for right_index, following in enumerate(right.patch):
            if intervals_contact(
                mapped_start,
                left_length,
                following.old_start,
                len(following.old_lines),
            ):
                primary.append((left_index, right_index))
            if intervals_contact(
                mapped_start,
                left_length,
                following.old_start,
                len(following.old_lines),
                include_boundary_anchors=True,
            ):
                boundary.append((left_index, right_index))
    return PairGeometry(
        mapped_new_starts=mapped_new,
        mapped_old_starts=mapped_old,
        primary_contacts=tuple(primary),
        boundary_contacts=tuple(boundary),
    )


def geometry_contacts(
    geometry: PairGeometry, *, include_boundary_anchors: bool = False
) -> tuple[tuple[int, int], ...]:
    return (
        geometry.boundary_contacts
        if include_boundary_anchors
        else geometry.primary_contacts
    )


def geometry_shifted(left: Operation, geometry: PairGeometry) -> bool:
    assert left.patch is not None
    contacted_left = {index for index, _ in geometry.boundary_contacts}
    return any(
        geometry.mapped_new_starts[index] != left.patch[index].new_start
        for index in contacted_left
    )


def lexical_tokens(lines: Iterable[str]) -> list[str]:
    return TOKEN_RE.findall("\n".join(lines))


def delta_counters(
    changes: Sequence[ChangeBlock],
    selected: set[int] | None = None,
    *,
    line_atoms: bool = False,
) -> tuple[collections.Counter[str], collections.Counter[str]]:
    old_atoms: list[str] = []
    new_atoms: list[str] = []
    for index, block in enumerate(changes):
        if selected is not None and index not in selected:
            continue
        if line_atoms:
            old_atoms.extend(block.old_lines)
            new_atoms.extend(block.new_lines)
        else:
            old_atoms.extend(lexical_tokens(block.old_lines))
            new_atoms.extend(lexical_tokens(block.new_lines))
    old_counter = collections.Counter(old_atoms)
    new_counter = collections.Counter(new_atoms)
    return old_counter - new_counter, new_counter - old_counter


def inverse_delta_score(
    forward: Sequence[ChangeBlock],
    following: Sequence[ChangeBlock],
    *,
    line_atoms: bool = False,
    include_boundary_anchors: bool = False,
    contact_pairs: Sequence[tuple[int, int]] | None = None,
) -> dict[str, float | None]:
    """Fraction of A's lexical delta canceled by B inside contacted blocks.

    For replacements both removal of A's additions and restoration of A's
    deletions must be substantial, so the score is the minimum direction.  A
    pure insertion/deletion uses its one defined direction.
    """

    selected_forward: set[int] = set()
    selected_following: set[int] = set()
    if contact_pairs is not None:
        for left_index, right_index in contact_pairs:
            selected_forward.add(left_index)
            selected_following.add(right_index)
    else:
        for left_index, left in enumerate(forward):
            for right_index, right in enumerate(following):
                if blocks_contact(
                    left,
                    right,
                    include_boundary_anchors=include_boundary_anchors,
                ):
                    selected_forward.add(left_index)
                    selected_following.add(right_index)
    removed_a, added_a = delta_counters(
        forward, selected_forward, line_atoms=line_atoms
    )
    removed_b, added_b = delta_counters(
        following, selected_following, line_atoms=line_atoms
    )
    remove_fraction = None
    restore_fraction = None
    if added_a:
        hits = sum((added_a & removed_b).values())
        remove_fraction = hits / sum(added_a.values())
    if removed_a:
        hits = sum((removed_a & added_b).values())
        restore_fraction = hits / sum(removed_a.values())
    defined = [item for item in (remove_fraction, restore_fraction) if item is not None]
    score = min(defined) if defined else 0.0
    return {
        "score": score,
        "remove_fraction": remove_fraction,
        "restore_fraction": restore_fraction,
    }


def whitespace_only_patch(changes: Sequence[ChangeBlock]) -> bool:
    old = "\n".join(line for block in changes for line in block.old_lines)
    new = "\n".join(line for block in changes for line in block.new_lines)
    if old == new:
        return False
    return re.sub(r"\s+", "", old) == re.sub(r"\s+", "", new)


def normalize_import_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())


def import_order_only_patch(changes: Sequence[ChangeBlock]) -> bool:
    old = [
        normalize_import_line(line)
        for block in changes
        for line in block.old_lines
        if line.strip()
    ]
    new = [
        normalize_import_line(line)
        for block in changes
        for line in block.new_lines
        if line.strip()
    ]
    if not old or not new or old == new:
        return False
    if not all(IMPORT_RE.match(line) for line in old + new):
        return False
    return collections.Counter(old) == collections.Counter(new)


def patch_mechanical_kind(changes: Sequence[ChangeBlock]) -> str | None:
    if whitespace_only_patch(changes):
        return "whitespace_only"
    if import_order_only_patch(changes):
        return "import_order_only"
    return None


def volatile_metadata_only_contact(
    forward: Sequence[ChangeBlock],
    following: Sequence[ChangeBlock],
    *,
    contact_pairs: Sequence[tuple[int, int]] | None = None,
    include_boundary_anchors: bool = False,
) -> bool:
    """Whether every block participating in primary contact is timestamp metadata."""

    contacted: list[ChangeBlock] = []
    if contact_pairs is not None:
        for left_index, right_index in contact_pairs:
            contacted.extend((forward[left_index], following[right_index]))
    else:
        for left in forward:
            for right in following:
                if blocks_contact(
                    left,
                    right,
                    include_boundary_anchors=include_boundary_anchors,
                ):
                    contacted.extend((left, right))
    if not contacted:
        return False
    lines = [
        line
        for block in contacted
        for line in block.old_lines + block.new_lines
        if line.strip()
    ]
    return bool(lines) and all(VOLATILE_METADATA_RE.match(line) for line in lines)


def per_contact_mechanical_kinds(
    forward: Sequence[ChangeBlock],
    following: Sequence[ChangeBlock],
    contact_pair: tuple[int, int],
) -> tuple[str, ...]:
    """Definite content-based mechanical labels for one aligned contact."""

    left_index, right_index = contact_pair
    kinds = {
        kind
        for kind in (
            patch_mechanical_kind((forward[left_index],)),
            patch_mechanical_kind((following[right_index],)),
        )
        if kind is not None
    }
    if volatile_metadata_only_contact(
        forward,
        following,
        contact_pairs=(contact_pair,),
    ):
        kinds.add("volatile_metadata_only_overlap")
    return tuple(sorted(kinds))


def exact_inverse_block(
    forward: ChangeBlock,
    following: ChangeBlock,
    *,
    mapped_new_start: int,
    mapped_old_start: int | None,
) -> bool:
    return (
        mapped_old_start is not None
        and following.old_start == mapped_new_start
        and following.new_start == mapped_old_start
        and following.old_lines == forward.new_lines
        and following.new_lines == forward.old_lines
        and following.old_no_newline == forward.new_no_newline
        and following.new_no_newline == forward.old_no_newline
    )


def exact_inverse_in_contacted_region(
    forward: Sequence[ChangeBlock],
    following: Sequence[ChangeBlock],
    geometry: PairGeometry,
    *,
    include_boundary_anchors: bool = False,
) -> bool:
    """Exact inverse of every block in the contacted region.

    Disjoint extra hunks are allowed. Different grouping remains a conservative
    false negative unless the complete B post-image restores A's pre-image.
    """

    left_contacted: set[int] = set()
    right_contacted: set[int] = set()
    matches: set[tuple[int, int]] = set()
    for left_index, right_index in geometry_contacts(
        geometry, include_boundary_anchors=include_boundary_anchors
    ):
        left = forward[left_index]
        right = following[right_index]
        left_contacted.add(left_index)
        right_contacted.add(right_index)
        mapped_new_start = geometry.mapped_new_starts[left_index]
        assert mapped_new_start is not None
        if exact_inverse_block(
            left,
            right,
            mapped_new_start=mapped_new_start,
            mapped_old_start=geometry.mapped_old_starts[left_index],
        ):
            matches.add((left_index, right_index))
    if not left_contacted or not right_contacted:
        return False
    return all(any(left == index for left, _ in matches) for index in left_contacted) and all(
        any(right == index for _, right in matches) for index in right_contacted
    )


def local_state_continuous(
    left: Operation,
    right: Operation,
    *,
    include_boundary_anchors: bool = False,
    geometry: PairGeometry | None = None,
) -> bool:
    """Verify A's post-state exactly at every A block contacted by B.

    This admits unrelated changes elsewhere in the file while rejecting stale
    coordinates/content in the region used for reversal classification.
    """

    if (
        left.patch is None
        or right.patch is None
        or left.post_lines is None
        or right.pre_lines is None
    ):
        return False
    if geometry is None:
        geometry = build_pair_geometry(left, right)
    contacts = geometry_contacts(
        geometry, include_boundary_anchors=include_boundary_anchors
    )
    if not contacts:
        return False
    for left_index in {index for index, _ in contacts}:
        block = left.patch[left_index]
        mapped_start = geometry.mapped_new_starts[left_index]
        if mapped_start is None:
            return False
        index = 0 if mapped_start == 0 else mapped_start - 1
        if block.new_lines:
            end = index + len(block.new_lines)
            if tuple(right.pre_lines[index:end]) != block.new_lines:
                return False
            continue
        # Deletion anchors were admitted only when exact neighboring lines map
        # to one unambiguous target boundary in ``_map_region_start``.
    return True


def generated_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    parts = [item for item in normalized.split("/") if item]
    basename = parts[-1] if parts else ""
    return (
        basename in GENERATED_BASENAMES
        or basename.endswith((".min.js", ".min.css", ".map"))
        or any(part in GENERATED_PARTS for part in parts[:-1])
        or any("generated" in part for part in parts)
    )


def interval_union_length(intervals: Iterable[tuple[int, int]]) -> int:
    materialized = sorted((start, end) for start, end in intervals if end > start)
    if not materialized:
        return 0
    total = 0
    start, end = materialized[0]
    for next_start, next_end in materialized[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def wholesale_ratio(operation: Operation) -> float:
    if not operation.usable_write or operation.patch is None:
        return 0.0
    old_changed = interval_union_length(
        (
            (max(block.old_start - 1, 0), max(block.old_start - 1, 0) + len(block.old_lines))
            for block in operation.patch
        )
    )
    new_changed = interval_union_length(
        (
            (max(block.new_start - 1, 0), max(block.new_start - 1, 0) + len(block.new_lines))
            for block in operation.patch
        )
    )
    old_denominator = max(len(operation.pre_lines or ()), 1)
    new_denominator = max(len(operation.post_lines or ()), 1)
    return max(old_changed / old_denominator, new_changed / new_denominator)


def classify_file(path: str) -> str:
    normalized = path.replace("\\", "/").casefold()
    parts = [item for item in normalized.split("/") if item]
    basename = parts[-1] if parts else ""
    extension = ntpath.splitext(basename)[1]
    if generated_path(path):
        return "generated_build"
    if extension in {".md", ".mdx"} and (
        basename in COORDINATION_BASENAMES
        or "memory" in parts[:-1]
        or "build-queue" in basename
        or "handoff" in basename
        or "coordination" in basename
        or "agent-status" in basename
    ):
        return "coordination_markdown"
    if extension in DOC_EXTENSIONS:
        return "other_documentation"
    if extension in SOURCE_EXTENSIONS:
        return "source_code"
    if extension in CONFIG_DATA_EXTENSIONS or basename in {
        "dockerfile", "makefile", "package.json", "pyproject.toml"
    }:
        return "config_data_lock"
    return "other"


@dataclass
class PairEdge:
    path: str
    left_position: int
    right_position: int
    left: Operation
    right: Operation
    classification: str
    inverse: dict[str, float | None]
    line_inverse: dict[str, float | None]
    latency_seconds: float
    mechanical_kinds: tuple[str, ...]
    command_causes: tuple[str, ...]
    generated: bool
    wholesale: bool
    contact_pairs: tuple[tuple[int, int], ...] = ()
    contact_mechanical_kinds: tuple[tuple[str, ...], ...] = ()
    read_category: str | None = None


@dataclass
class SequenceRecord:
    path: str
    writes: list[Operation]
    edges: list[PairEdge]
    classification: str
    oscillation_subtype: str | None
    cause_partition: str
    file_type: str


def command_causes_between(
    left: Operation,
    right: Operation,
    commands: Sequence[CommandEvent],
) -> tuple[str, ...]:
    sessions = set(left.sessions) | set(right.sessions)
    agents = {left.agent, right.agent}
    target = left.path or right.path

    def command_scope_matches(event: CommandEvent) -> bool:
        if target is None or not event.cwds:
            return False
        return any(target == cwd or target.startswith(cwd.rstrip("\\") + "\\") for cwd in event.cwds)

    return tuple(
        sorted(
            {
                event.category
                for event in commands
                if left.result_ts < event.timestamp < right.call_ts
                and (event.agent in agents or bool(sessions & set(event.sessions)))
                and command_scope_matches(event)
            }
        )
    )


def pair_classification(
    left: Operation,
    right: Operation,
    *,
    threshold: float = PRIMARY_PARTIAL_THRESHOLD,
    include_boundary_anchors: bool = False,
    geometry: PairGeometry | None = None,
) -> tuple[str, dict[str, float | None], dict[str, float | None]]:
    assert left.patch is not None and right.patch is not None
    if geometry is None:
        geometry = build_pair_geometry(left, right)
    contacts = geometry_contacts(
        geometry, include_boundary_anchors=include_boundary_anchors
    )
    exact = (
        is_exact_inverse_patch(left.patch, right.patch)
        or right.post_lines == left.pre_lines
        or exact_inverse_in_contacted_region(
            left.patch,
            right.patch,
            geometry,
            include_boundary_anchors=include_boundary_anchors,
        )
    )
    if exact:
        inverse = inverse_delta_score(
            left.patch,
            right.patch,
            include_boundary_anchors=include_boundary_anchors,
            contact_pairs=contacts,
        )
        line_inverse = inverse_delta_score(
            left.patch,
            right.patch,
            line_atoms=True,
            include_boundary_anchors=include_boundary_anchors,
            contact_pairs=contacts,
        )
        return "exact_reversal", inverse, line_inverse
    inverse = inverse_delta_score(
        left.patch,
        right.patch,
        include_boundary_anchors=include_boundary_anchors,
        contact_pairs=contacts,
    )
    line_inverse = inverse_delta_score(
        left.patch,
        right.patch,
        line_atoms=True,
        include_boundary_anchors=include_boundary_anchors,
        contact_pairs=contacts,
    )
    if float(inverse["score"] or 0.0) >= threshold:
        return "partial_reversal", inverse, line_inverse
    return "independent_coediting", inverse, line_inverse


def read_contacts_revert(read: Operation, revert: Operation) -> bool:
    if read.read_interval is None or revert.patch is None:
        return False
    start, end = read.read_interval
    return any(
        intervals_contact(
            start,
            end - start,
            block.old_start,
            len(block.old_lines),
        )
        for block in revert.patch
    )


def read_matches_preimage(read: Operation, write: Operation) -> bool:
    if (
        read.read_interval is None
        or read.read_lines is None
        or write.pre_lines is None
    ):
        return False
    start, end = read.read_interval
    index = start - 1
    return tuple(write.pre_lines[index : end - 1]) == read.read_lines


def reversal_read_category(
    edge: PairEdge,
    reads_by_path_agent: Mapping[tuple[str, str], Sequence[Operation]],
) -> str:
    reads = [
        item
        for item in reads_by_path_agent.get((edge.path, edge.right.agent), ())
        if item.result_ts < edge.right.call_ts
    ]
    post = [item for item in reads if edge.left.result_ts < item.result_ts]
    post_region = [item for item in post if read_contacts_revert(item, edge.right)]
    if any(read_matches_preimage(item, edge.right) for item in post_region):
        return "post_A_verified_region_read"
    if post_region:
        return "post_A_offset_only_region_read"
    pre_region = [item for item in reads if read_contacts_revert(item, edge.right)]
    if pre_region:
        return "only_pre_A_region_read"
    if post:
        return "post_A_file_read_outside_region"
    return "no_observed_localized_read"


def persistent_contact_paths(
    edges: Sequence[PairEdge],
) -> tuple[tuple[int, ...], ...]:
    """Contact-index paths that persist through every edge in a write run.

    A contact on edge i names a changed block on write i and a changed block
    on write i+1.  It continues through the next edge only when that middle
    write's right-side block index is the next contact's left-side block index.
    This is the conservative structured-patch notion of "one region".
    """

    if not edges:
        return ()
    paths: list[tuple[int, ...]] = [
        (contact_index,)
        for contact_index in range(len(edges[0].contact_pairs))
    ]
    for edge_index in range(1, len(edges)):
        previous = edges[edge_index - 1]
        current = edges[edge_index]
        extended: list[tuple[int, ...]] = []
        for path in paths:
            previous_right_block = previous.contact_pairs[path[-1]][1]
            for contact_index, (current_left_block, _) in enumerate(
                current.contact_pairs
            ):
                if previous_right_block == current_left_block:
                    extended.append(path + (contact_index,))
        paths = extended
        if not paths:
            break
    return tuple(paths)


def oscillation_region_witnesses(
    writes: Sequence[Operation], edges: Sequence[PairEdge]
) -> tuple[tuple[int, int, tuple[tuple[int, ...], ...]], ...]:
    """Repeated-writer subruns that preserve at least one block path."""

    witnesses: list[tuple[int, int, tuple[tuple[int, ...], ...]]] = []
    for left_index in range(len(writes) - 2):
        for right_index in range(left_index + 2, len(writes)):
            if writes[left_index].agent != writes[right_index].agent:
                continue
            if not any(
                item.agent != writes[left_index].agent
                for item in writes[left_index + 1 : right_index]
            ):
                continue
            paths = persistent_contact_paths(edges[left_index:right_index])
            if paths:
                witnesses.append((left_index, right_index, paths))
                break
    return tuple(witnesses)


def sequence_classification(
    writes: Sequence[Operation],
    edges: Sequence[PairEdge],
    *,
    threshold: float = PRIMARY_PARTIAL_THRESHOLD,
) -> str:
    if oscillation_region_witnesses(writes, edges):
        return "oscillation"
    if any(edge.classification == "exact_reversal" for edge in edges):
        return "exact_reversal"
    if any(float(edge.inverse["score"] or 0.0) >= threshold for edge in edges):
        return "partial_reversal"
    return "independent_coediting"


def oscillation_subtype(
    writes: Sequence[Operation], edges: Sequence[PairEdge]
) -> str | None:
    witnesses = oscillation_region_witnesses(writes, edges)
    if not witnesses:
        return None
    found_reversal_reapplication = False
    for left_index, right_index, _ in witnesses:
        if right_index != left_index + 2:
            continue
        left = edges[left_index]
        right = edges[left_index + 1]
        if (
            left.classification == "exact_reversal"
            and right.classification == "exact_reversal"
        ):
            return "exact_cycle"
        if left.classification in {"exact_reversal", "partial_reversal"} and right.classification in {
            "exact_reversal",
            "partial_reversal",
        }:
            found_reversal_reapplication = True
    return "reversal_reapplication" if found_reversal_reapplication else "ABA_only"


def oscillation_witness_cause_partitions(
    path: str,
    writes: Sequence[Operation],
    edges: Sequence[PairEdge],
) -> tuple[str, ...]:
    """Cause partitions for minimal repeated-writer witnesses within a run.

    Boundary-inclusive edges can merge an ABA run with unrelated neighbors.
    Mechanical filtering therefore evaluates the repeated-writer subruns, not
    whether every write in the enlarged maximal sequence is mechanical.
    """

    partitions: list[str] = []
    for left_index, right_index, region_paths in oscillation_region_witnesses(
        writes, edges
    ):
        witness_edges = edges[left_index:right_index]
        path_is_definite = [
            all(
                bool(witness_edges[offset].contact_mechanical_kinds[contact_index])
                for offset, contact_index in enumerate(region_path)
            )
            for region_path in region_paths
        ]
        if path_is_definite and all(path_is_definite):
            partitions.append("definite_mechanical_only")
            continue
        if any(
            bool(witness_edges[offset].contact_mechanical_kinds[contact_index])
            for region_path in region_paths
            for offset, contact_index in enumerate(region_path)
        ):
            partitions.append("mixed_definite_mechanical")
            continue
        command_kinds = {
            kind for edge in witness_edges for kind in edge.command_causes
        }
        if generated_path(path) or "codegen" in command_kinds:
            partitions.append("suspected_generated_or_codegen")
        elif any(edge.wholesale for edge in witness_edges) or "git_mutation" in command_kinds:
            partitions.append("suspected_wholesale_or_git")
        elif "formatter_or_linter" in command_kinds:
            partitions.append("suspected_formatter_or_linter")
        else:
            partitions.append("no_detected_mechanical_cause")
    return tuple(partitions)


def sequence_cause_partition(
    path: str, writes: Sequence[Operation], edges: Sequence[PairEdge]
) -> str:
    edge_mechanical = [bool(edge.mechanical_kinds) for edge in edges]
    mechanical = [
        patch_mechanical_kind(item.patch or ()) is not None for item in writes
    ]
    command_kinds = {kind for edge in edges for kind in edge.command_causes}
    if edge_mechanical and all(edge_mechanical):
        return "definite_mechanical_only"
    if any(edge_mechanical):
        return "mixed_definite_mechanical"
    if mechanical and all(mechanical):
        return "definite_mechanical_only"
    if any(mechanical):
        return "mixed_definite_mechanical"
    if generated_path(path) or "codegen" in command_kinds:
        return "suspected_generated_or_codegen"
    if any(edge.wholesale for edge in edges) or "git_mutation" in command_kinds:
        return "suspected_wholesale_or_git"
    if "formatter_or_linter" in command_kinds:
        return "suspected_formatter_or_linter"
    return "no_detected_mechanical_cause"


def build_pairs_and_sequences(
    operations: Sequence[Operation],
    commands: Sequence[CommandEvent],
    *,
    include_boundary_anchors: bool = False,
) -> tuple[list[PairEdge], list[SequenceRecord], dict[str, int]]:
    attrition: collections.Counter[str] = collections.Counter()
    by_path: dict[str, list[Operation]] = collections.defaultdict(list)
    for operation in operations:
        if operation.kind != "write" or not operation.success:
            continue
        attrition["deduplicated_successful_write_operations"] += 1
        if operation.path is None:
            attrition["write_operations_without_result_path"] += 1
            continue
        if operation.metadata_status == "empty_patch":
            attrition["known_noop_write_operations"] += 1
            continue
        by_path[operation.path].append(operation)
        attrition["result_localized_non_noop_write_events"] += 1
        if operation.usable_write:
            attrition["usable_exact_write_events"] += 1

    reads_by_path_agent: dict[tuple[str, str], list[Operation]] = collections.defaultdict(list)
    for operation in operations:
        if operation.success and operation.localized_read and operation.path is not None:
            reads_by_path_agent[(operation.path, operation.agent)].append(operation)
    for reads in reads_by_path_agent.values():
        reads.sort(key=lambda item: (item.result_ts, item.tool_id))

    pairs: list[PairEdge] = []
    boundary_only = 0
    boundary_classifications: collections.Counter[str] = collections.Counter()
    continuity_breaks_with_git = 0
    for path, writes in by_path.items():
        writes.sort(key=lambda item: (item.result_ts, item.call_ts, item.tool_id))
        for position in range(len(writes) - 1):
            left = writes[position]
            right = writes[position + 1]
            attrition["adjacent_result_localized_write_pairs"] += 1
            if left.agent == right.agent:
                attrition["same_agent_adjacent_pairs"] += 1
                continue
            attrition["cross_agent_adjacent_pairs"] += 1
            if not (left.result_ts < right.call_ts):
                attrition["concurrent_or_ambiguous_cross_agent_pairs"] += 1
                continue
            attrition["strictly_serialized_cross_agent_pairs"] += 1
            if not left.usable_write or not right.usable_write:
                attrition["serialized_pairs_missing_exact_write_metadata"] += 1
                continue
            attrition["serialized_exact_metadata_pairs"] += 1
            assert left.post_lines is not None and right.pre_lines is not None
            assert left.patch is not None and right.patch is not None
            geometry = build_pair_geometry(left, right)
            primary_contact = bool(geometry.primary_contacts)
            boundary_contact = bool(geometry.boundary_contacts)
            if not boundary_contact:
                raw_boundary_contact = patches_contact(
                    left.patch,
                    right.patch,
                    include_boundary_anchors=True,
                )
                if raw_boundary_contact:
                    attrition["contacted_region_alignment_failures"] += 1
                    attrition["local_state_continuity_breaks"] += 1
                    causes = command_causes_between(left, right, commands)
                    if "git_mutation" in causes:
                        continuity_breaks_with_git += 1
                elif any(start is not None for start in geometry.mapped_new_starts):
                    attrition["mapped_coordinate_disjoint_exact_metadata_pairs"] += 1
                else:
                    attrition["unmapped_or_ambiguous_region_pairs"] += 1
                continue
            attrition["coordinate_contact_exact_metadata_pairs"] += 1
            if geometry_shifted(left, geometry):
                attrition["shifted_coordinate_contacts_recovered"] += 1
            if left.post_lines == right.pre_lines:
                attrition["full_file_state_continuous_contact_pairs"] += 1
            if not local_state_continuous(
                left,
                right,
                include_boundary_anchors=(
                    include_boundary_anchors or not primary_contact
                ),
                geometry=geometry,
            ):
                attrition["local_state_continuity_breaks"] += 1
                causes = command_causes_between(left, right, commands)
                if "git_mutation" in causes:
                    continuity_breaks_with_git += 1
                continue
            attrition["locally_state_continuous_contact_pairs"] += 1
            if not primary_contact:
                boundary_only += 1
                classification, _, _ = pair_classification(
                    left,
                    right,
                    include_boundary_anchors=True,
                    geometry=geometry,
                )
                boundary_classifications[classification] += 1
                if not include_boundary_anchors:
                    continue
            attrition["eligible_overlapping_pairs_D_pair"] += 1
            classification, inverse, line_inverse = pair_classification(
                left,
                right,
                include_boundary_anchors=include_boundary_anchors,
                geometry=geometry,
            )
            contacts = geometry_contacts(
                geometry, include_boundary_anchors=include_boundary_anchors
            )
            mechanical_kinds = tuple(
                sorted(
                    {
                        kind
                        for kind in (
                            patch_mechanical_kind(left.patch),
                            patch_mechanical_kind(right.patch),
                        )
                        if kind is not None
                    }
                    | (
                        {"volatile_metadata_only_overlap"}
                        if volatile_metadata_only_contact(
                            left.patch,
                            right.patch,
                            contact_pairs=contacts,
                            include_boundary_anchors=include_boundary_anchors,
                        )
                        else set()
                    )
                )
            )
            edge = PairEdge(
                path=path,
                left_position=position,
                right_position=position + 1,
                left=left,
                right=right,
                classification=classification,
                inverse=inverse,
                line_inverse=line_inverse,
                latency_seconds=right.call_ts - left.result_ts,
                mechanical_kinds=mechanical_kinds,
                command_causes=command_causes_between(left, right, commands),
                generated=generated_path(path),
                wholesale=max(wholesale_ratio(left), wholesale_ratio(right)) >= 0.80,
                contact_pairs=tuple(contacts),
                contact_mechanical_kinds=tuple(
                    per_contact_mechanical_kinds(
                        left.patch,
                        right.patch,
                        contact_pair,
                    )
                    for contact_pair in contacts
                ),
            )
            if classification in {"exact_reversal", "partial_reversal"}:
                edge.read_category = reversal_read_category(edge, reads_by_path_agent)
            pairs.append(edge)

    attrition["boundary_anchor_only_pairs_sensitivity"] = boundary_only
    for classification, count in boundary_classifications.items():
        attrition[f"boundary_anchor_only_{classification}"] = count
    attrition["continuity_breaks_with_observed_git_mutation"] = continuity_breaks_with_git

    pair_groups: dict[str, list[PairEdge]] = collections.defaultdict(list)
    for pair in pairs:
        pair_groups[pair.path].append(pair)
    sequences: list[SequenceRecord] = []
    for path, edges in pair_groups.items():
        edges.sort(key=lambda edge: edge.left_position)
        run: list[PairEdge] = []
        for edge in edges:
            if run and edge.left_position != run[-1].right_position:
                writes = [run[0].left] + [item.right for item in run]
                classification = sequence_classification(writes, run)
                sequences.append(
                    SequenceRecord(
                        path=path,
                        writes=writes,
                        edges=list(run),
                        classification=classification,
                        oscillation_subtype=oscillation_subtype(writes, run),
                        cause_partition=sequence_cause_partition(path, writes, run),
                        file_type=classify_file(path),
                    )
                )
                run = []
            run.append(edge)
        if run:
            writes = [run[0].left] + [item.right for item in run]
            classification = sequence_classification(writes, run)
            sequences.append(
                SequenceRecord(
                    path=path,
                    writes=writes,
                    edges=list(run),
                    classification=classification,
                    oscillation_subtype=oscillation_subtype(writes, run),
                    cause_partition=sequence_cause_partition(path, writes, run),
                    file_type=classify_file(path),
                )
            )
    sequences.sort(key=lambda item: (item.writes[0].result_ts, item.path))
    attrition["multi_agent_region_sequences_D_seq"] = len(sequences)
    return pairs, sequences, dict(sorted(attrition.items()))


def percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * probability
    low = math.floor(index)
    high = math.ceil(index)
    if low == high:
        return ordered[low]
    weight = index - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def summarize_latencies(values: Sequence[float]) -> dict[str, Any]:
    bins = collections.Counter()
    for value in values:
        if value < 60:
            bins["under_1m"] += 1
        elif value < 300:
            bins["1m_to_5m"] += 1
        elif value < 3600:
            bins["5m_to_1h"] += 1
        elif value < 86400:
            bins["1h_to_24h"] += 1
        else:
            bins["24h_or_more"] += 1
    return {
        "denominator_reversal_edges": len(values),
        "minimum_seconds": min(values) if values else None,
        "q1_seconds": percentile(values, 0.25),
        "median_seconds": statistics.median(values) if values else None,
        "q3_seconds": percentile(values, 0.75),
        "p90_seconds": percentile(values, 0.90),
        "maximum_seconds": max(values) if values else None,
        "bins": dict(sorted(bins.items())),
    }


def operation_patch_text(operation: Operation) -> str:
    assert operation.patch is not None
    chunks: list[str] = []
    for block in operation.patch:
        chunks.append(
            f"@@ -{block.old_start},{len(block.old_lines)} "
            f"+{block.new_start},{len(block.new_lines)} @@"
        )
        chunks.extend(f"-{line}" for line in block.old_lines)
        chunks.extend(f"+{line}" for line in block.new_lines)
    return "\n".join(chunks)


def operation_summary(operation: Operation) -> dict[str, Any]:
    return {
        "tool_use_id": operation.tool_id,
        "agent": operation.agent,
        "sessions": list(operation.sessions),
        "tool": operation.tool,
        "call_timestamp": iso_utc(operation.call_ts),
        "result_timestamp": iso_utc(operation.result_ts),
        "hunks": [
            {
                "old_range_1_based_half_open": [
                    block.old_start,
                    block.old_start + len(block.old_lines),
                ],
                "new_range_1_based_half_open": [
                    block.new_start,
                    block.new_start + len(block.new_lines),
                ],
                "oldString": "\n".join(block.old_lines),
                "newString": "\n".join(block.new_lines),
            }
            for block in operation.patch or ()
        ],
        "diff": operation_patch_text(operation),
    }


def sequence_summary(sequence: SequenceRecord) -> dict[str, Any]:
    witnesses = oscillation_region_witnesses(sequence.writes, sequence.edges)
    return {
        "path": sequence.path,
        "file_type": sequence.file_type,
        "classification": sequence.classification,
        "oscillation_subtype": sequence.oscillation_subtype,
        "cause_partition": sequence.cause_partition,
        "write_count": len(sequence.writes),
        "distinct_agent_count": len({item.agent for item in sequence.writes}),
        "duration_seconds": sequence.writes[-1].result_ts - sequence.writes[0].result_ts,
        "agents": [item.agent for item in sequence.writes],
        "oscillation_region_witnesses": [
            {
                "write_start_index": left_index,
                "write_end_index": right_index,
                "contact_index_paths": [list(path) for path in paths],
            }
            for left_index, right_index, paths in witnesses
        ],
        "oscillation_witness_cause_partitions": list(
            oscillation_witness_cause_partitions(
                sequence.path,
                sequence.writes,
                sequence.edges,
            )
        ),
        "writes": [operation_summary(item) for item in sequence.writes],
        "edges": [
            {
                "classification": edge.classification,
                "lexical_inverse_score": edge.inverse["score"],
                "line_inverse_score": edge.line_inverse["score"],
                "latency_seconds": edge.latency_seconds,
                "read_category": edge.read_category,
                "mechanical_kinds": list(edge.mechanical_kinds),
                "command_causes": list(edge.command_causes),
                "generated_path": edge.generated,
                "wholesale_80pct": edge.wholesale,
                "contact_pairs": [list(pair) for pair in edge.contact_pairs],
                "contact_mechanical_kinds": [
                    list(kinds) for kinds in edge.contact_mechanical_kinds
                ],
            }
            for edge in sequence.edges
        ],
    }


def analyze_population(
    operations: Sequence[Operation],
    commands: Sequence[CommandEvent],
    metadata: Mapping[str, Any],
) -> tuple[dict[str, Any], list[PairEdge], list[SequenceRecord]]:
    pairs, sequences, attrition = build_pairs_and_sequences(operations, commands)
    boundary_pairs, boundary_sequences, _ = build_pairs_and_sequences(
        operations,
        commands,
        include_boundary_anchors=True,
    )
    d_pair = len(pairs)
    d_seq = len(sequences)
    d_pair_boundary = len(boundary_pairs)
    d_seq_boundary = len(boundary_sequences)

    pair_counts = collections.Counter(edge.classification for edge in pairs)
    sequence_counts = collections.Counter(item.classification for item in sequences)
    sequence_flags = {
        "contains_exact_reversal": sum(
            any(edge.classification == "exact_reversal" for edge in item.edges)
            for item in sequences
        ),
        "contains_partial_reversal_at_0_75": sum(
            any(
                edge.classification == "partial_reversal" for edge in item.edges
            )
            for item in sequences
        ),
        "oscillation": sequence_counts["oscillation"],
        "independent_no_reversal_no_recurrence": sequence_counts[
            "independent_coediting"
        ],
    }

    sensitivities: dict[str, Any] = {}
    for threshold in PARTIAL_THRESHOLDS:
        label = f"{threshold:.2f}"
        partial_pair_count = sum(
            edge.classification != "exact_reversal"
            and float(edge.inverse["score"] or 0.0) >= threshold
            for edge in pairs
        )
        independent_pair_count = d_pair - pair_counts["exact_reversal"] - partial_pair_count
        seq_classes = collections.Counter(
            sequence_classification(item.writes, item.edges, threshold=threshold)
            for item in sequences
        )
        sensitivities[label] = {
            "pair_denominator_D_pair": d_pair,
            "partial_reversal_pairs": partial_pair_count,
            "independent_pairs": independent_pair_count,
            "sequence_denominator_D_seq": d_seq,
            "mutually_exclusive_sequence_counts": dict(sorted(seq_classes.items())),
        }
    line_atom_partial = sum(
        edge.classification != "exact_reversal"
        and float(edge.line_inverse["score"] or 0.0) >= PRIMARY_PARTIAL_THRESHOLD
        for edge in pairs
    )
    primary_pair_keys = {
        (edge.path, edge.left.tool_id, edge.right.tool_id) for edge in pairs
    }
    boundary_only_pairs = [
        edge
        for edge in boundary_pairs
        if (edge.path, edge.left.tool_id, edge.right.tool_id) not in primary_pair_keys
    ]
    boundary_additional = len(boundary_only_pairs)
    boundary_counts = collections.Counter(
        edge.classification for edge in boundary_only_pairs
    )
    boundary_inclusive_counts = collections.Counter(
        edge.classification for edge in boundary_pairs
    )
    boundary_sequence_counts = collections.Counter(
        item.classification for item in boundary_sequences
    )
    boundary_sequence_flags = {
        "contains_exact_reversal": sum(
            any(edge.classification == "exact_reversal" for edge in item.edges)
            for item in boundary_sequences
        ),
        "contains_partial_reversal_at_0_75": sum(
            any(edge.classification == "partial_reversal" for edge in item.edges)
            for item in boundary_sequences
        ),
        "oscillation": boundary_sequence_counts["oscillation"],
        "independent_no_reversal_no_recurrence": boundary_sequence_counts[
            "independent_coediting"
        ],
    }
    boundary_oscillations = [
        item for item in boundary_sequences if item.classification == "oscillation"
    ]
    boundary_subtypes = collections.Counter(
        item.oscillation_subtype for item in boundary_oscillations
    )
    boundary_cause_counts = collections.Counter(
        item.cause_partition for item in boundary_sequences
    )
    boundary_type_rows: dict[str, dict[str, int]] = {}
    for file_type in sorted(
        {item.file_type for item in boundary_sequences}
        | {
            "coordination_markdown",
            "other_documentation",
            "source_code",
            "config_data_lock",
            "generated_build",
            "other",
        }
    ):
        selected = [
            item for item in boundary_sequences if item.file_type == file_type
        ]
        boundary_type_rows[file_type] = {
            "sequence_denominator": len(selected),
            "oscillations": sum(
                item.classification == "oscillation" for item in selected
            ),
        }

    reversal_edges = [
        edge
        for edge in pairs
        if edge.classification in {"exact_reversal", "partial_reversal"}
    ]
    nonmechanical_reversal_edges = [
        edge
        for edge in reversal_edges
        if not edge.mechanical_kinds
        and not edge.generated
        and not edge.wholesale
        and not edge.command_causes
    ]
    read_categories = collections.Counter(
        edge.read_category or "unclassified" for edge in reversal_edges
    )
    cause_counts = collections.Counter(item.cause_partition for item in sequences)
    oscillations = [item for item in sequences if item.classification == "oscillation"]
    subtype_counts = collections.Counter(item.oscillation_subtype for item in oscillations)
    oscillation_witness_causes = {
        (item.path, tuple(write.tool_id for write in item.writes)):
        oscillation_witness_cause_partitions(item.path, item.writes, item.edges)
        for item in oscillations
    }
    oscillations_after_definite_mechanical = [
        item
        for item in oscillations
        if any(
            cause != "definite_mechanical_only"
            for cause in oscillation_witness_causes[
                (item.path, tuple(write.tool_id for write in item.writes))
            ]
        )
    ]
    unflagged_oscillations = [
        item
        for item in oscillations
        if "no_detected_mechanical_cause"
        in oscillation_witness_causes[
            (item.path, tuple(write.tool_id for write in item.writes))
        ]
    ]
    strong_oscillations = [
        item
        for item in oscillations
        if item.oscillation_subtype in {"exact_cycle", "reversal_reapplication"}
    ]
    strong_unflagged = [
        item
        for item in strong_oscillations
        if "no_detected_mechanical_cause"
        in oscillation_witness_causes[
            (item.path, tuple(write.tool_id for write in item.writes))
        ]
    ]
    boundary_witness_causes = {
        (item.path, tuple(write.tool_id for write in item.writes)):
        oscillation_witness_cause_partitions(item.path, item.writes, item.edges)
        for item in boundary_oscillations
    }
    boundary_after_definite_mechanical = [
        item
        for item in boundary_oscillations
        if any(
            cause != "definite_mechanical_only"
            for cause in boundary_witness_causes[
                (item.path, tuple(write.tool_id for write in item.writes))
            ]
        )
    ]
    boundary_unflagged = [
        item
        for item in boundary_oscillations
        if "no_detected_mechanical_cause"
        in boundary_witness_causes[
            (item.path, tuple(write.tool_id for write in item.writes))
        ]
    ]

    by_type: dict[str, dict[str, int]] = {}
    for file_type in sorted({item.file_type for item in sequences} | {
        "coordination_markdown", "other_documentation", "source_code",
        "config_data_lock", "generated_build", "other",
    }):
        type_sequences = [item for item in sequences if item.file_type == file_type]
        type_oscillations = [
            item for item in type_sequences if item.classification == "oscillation"
        ]
        by_type[file_type] = {
            "sequence_denominator": len(type_sequences),
            "oscillations": len(type_oscillations),
            "unflagged_oscillations": sum(
                item.cause_partition == "no_detected_mechanical_cause"
                for item in type_oscillations
            ),
        }

    extension_rows: dict[str, dict[str, int]] = {}
    for extension in sorted({ntpath.splitext(item.path)[1].casefold() or "<none>" for item in sequences}):
        selected = [
            item
            for item in sequences
            if (ntpath.splitext(item.path)[1].casefold() or "<none>") == extension
        ]
        extension_rows[extension] = {
            "sequence_denominator": len(selected),
            "oscillations": sum(item.classification == "oscillation" for item in selected),
        }

    longest = None
    if oscillations:
        longest_record = max(
            oscillations,
            key=lambda item: (
                len(item.writes),
                len({write.agent for write in item.writes}),
                item.writes[-1].result_ts - item.writes[0].result_ts,
            ),
        )
        longest = {
            "path": longest_record.path,
            "write_count": len(longest_record.writes),
            "distinct_agent_count": len({item.agent for item in longest_record.writes}),
            "agent_phase_count": 1
            + sum(
                left.agent != right.agent
                for left, right in zip(
                    longest_record.writes, longest_record.writes[1:]
                )
            ),
            "duration_seconds": longest_record.writes[-1].result_ts
            - longest_record.writes[0].result_ts,
            "file_type": longest_record.file_type,
            "classification": longest_record.oscillation_subtype,
        }

    boundary_longest = None
    if boundary_oscillations:
        boundary_longest_record = max(
            boundary_oscillations,
            key=lambda item: (
                len(item.writes),
                len({write.agent for write in item.writes}),
                item.writes[-1].result_ts - item.writes[0].result_ts,
            ),
        )
        boundary_longest = {
            "path": boundary_longest_record.path,
            "write_count": len(boundary_longest_record.writes),
            "distinct_agent_count": len(
                {item.agent for item in boundary_longest_record.writes}
            ),
            "duration_seconds": boundary_longest_record.writes[-1].result_ts
            - boundary_longest_record.writes[0].result_ts,
            "file_type": boundary_longest_record.file_type,
            "classification": boundary_longest_record.oscillation_subtype,
        }

    example_rank = {"exact_cycle": 0, "reversal_reapplication": 1, "ABA_only": 2}
    cause_rank = {
        "no_detected_mechanical_cause": 0,
        "suspected_formatter_or_linter": 1,
        "suspected_generated_or_codegen": 2,
        "suspected_wholesale_or_git": 3,
        "mixed_definite_mechanical": 4,
        "definite_mechanical_only": 5,
    }
    primary_oscillation_keys = {
        (item.path, tuple(write.tool_id for write in item.writes))
        for item in oscillations
    }
    additional_boundary_oscillations = [
        item
        for item in boundary_oscillations
        if (item.path, tuple(write.tool_id for write in item.writes))
        not in primary_oscillation_keys
    ]
    example_candidates = [(item, "primary") for item in oscillations] + [
        (item, "boundary_inclusive_sensitivity")
        for item in additional_boundary_oscillations
    ]
    examples = sorted(
        example_candidates,
        key=lambda item: (
            example_rank.get(item[0].oscillation_subtype or "", 9),
            cause_rank.get(item[0].cause_partition, 9),
            sum(
                len(block.old_lines) + len(block.new_lines)
                for write in item[0].writes
                for block in write.patch or ()
            ),
            item[0].writes[0].result_ts,
        ),
    )[:3]

    diagnostics = metadata.get("diagnostics", {})
    schema_coverage: dict[str, dict[str, int]] = {}
    for source in ("main", "direct_subagent", "workflow_subagent"):
        successful_reads = sum(
            int(diagnostics.get(f"successful_result_occurrences_{source}_{tool}", 0))
            for tool in READ_TOOLS
        )
        successful_writes = sum(
            int(diagnostics.get(f"successful_result_occurrences_{source}_{tool}", 0))
            for tool in WRITE_TOOLS
        )
        exact_nonempty_writes = sum(
            int(diagnostics.get(f"metadata_{source}_{tool}_exact_write", 0))
            for tool in WRITE_TOOLS
        )
        localized_reads = sum(
            int(diagnostics.get(f"metadata_{source}_{tool}_exact_read", 0))
            for tool in READ_TOOLS
        )
        schema_coverage[source] = {
            "successful_read_result_occurrences": successful_reads,
            "localized_structured_read_occurrences": localized_reads,
            "successful_write_result_occurrences": successful_writes,
            "usable_nonempty_patch_preimage_occurrences": exact_nonempty_writes,
        }

    result = {
        "schema_version": 1,
        "generated_utc": iso_utc(dt.datetime.now(dt.timezone.utc).timestamp()),
        "corpus": dict(metadata),
        "method": {
            "pair_denominator_D_pair": (
                "deduplicated successful strictly serialized state-continuous "
                "adjacent cross-agent writes with exact result patches/pre-images "
                "and overlapping changed ranges"
            ),
            "sequence_denominator_D_seq": (
                "maximal runs of two or more writes connected by eligible D_pair edges"
            ),
            "partial_reversal": (
                "within contacted blocks, min(fraction of A-added lexical tokens "
                "removed by B, fraction of A-removed lexical tokens restored by B); "
                "for pure insertion/deletion use the one defined direction"
            ),
            "primary_partial_threshold": PRIMARY_PARTIAL_THRESHOLD,
            "partial_threshold_sensitivities": list(PARTIAL_THRESHOLDS),
            "oscillation_region": (
                "writer recurrence with at least one structured-patch block contact path that "
                "persists through every edge in the repeated-writer subrun; middle-write block "
                "indexes must join, a conservative same-region criterion"
            ),
            "coordinates": "1-based half-open logical-line ranges; patch context excluded",
            "adjacency": "literal next result-localized non-noop write to the file",
            "state_continuity": (
                "A post-image changed regions are mapped into B originalFile by symmetric exact-line "
                "alignment before contact; A pre-image regions are separately mapped into B post-image "
                "for exact inverses; ambiguous repeated-line and changed-anchor mappings are rejected"
            ),
            "read_awareness": (
                "successful localized Read results by B before B call; strongest category "
                "requires a post-A overlapping read whose returned content equals B pre-image slice"
            ),
        },
        "attrition": attrition,
        "schema_coverage_by_source": schema_coverage,
        "denominators": {
            "D_pair_cross_agent_overlapping_transitions": d_pair,
            "D_seq_multi_agent_region_sequences": d_seq,
            "reversal_edges_exact_or_partial_at_0_75": len(reversal_edges),
            "raw_oscillation_sequences": len(oscillations),
        },
        "pair_classifications": dict(sorted(pair_counts.items())),
        "sequence_classifications_mutually_exclusive": dict(
            sorted(sequence_counts.items())
        ),
        "sequence_nonexclusive_flags": sequence_flags,
        "partial_sensitivity": sensitivities,
        "line_atom_partial_pairs_at_0_75": {
            "count": line_atom_partial,
            "denominator_D_pair": d_pair,
        },
        "boundary_anchor_sensitivity": {
            "primary_D_pair": d_pair,
            "additional_boundary_only_pairs": boundary_additional,
            "boundary_inclusive_denominator": d_pair_boundary,
            "boundary_only_classifications": dict(sorted(boundary_counts.items())),
            "boundary_inclusive_classifications": dict(
                sorted(boundary_inclusive_counts.items())
            ),
            "primary_D_seq": d_seq,
            "boundary_inclusive_sequence_denominator": d_seq_boundary,
            "boundary_inclusive_sequence_classifications": dict(
                sorted(boundary_sequence_counts.items())
            ),
            "boundary_inclusive_sequence_nonexclusive_flags": boundary_sequence_flags,
            "boundary_inclusive_oscillation_subtypes": dict(
                sorted((str(key), value) for key, value in boundary_subtypes.items())
            ),
            "boundary_inclusive_raw_oscillations": len(boundary_oscillations),
            "boundary_inclusive_oscillation_witness_cause_partitions": dict(
                sorted(
                    collections.Counter(
                        cause
                        for causes in boundary_witness_causes.values()
                        for cause in causes
                    ).items()
                )
            ),
            "boundary_inclusive_after_definite_mechanical_only_exclusion": len(
                boundary_after_definite_mechanical
            ),
            "boundary_inclusive_unflagged_oscillations": len(boundary_unflagged),
            "boundary_inclusive_sequence_cause_partition": dict(
                sorted(boundary_cause_counts.items())
            ),
            "boundary_inclusive_file_types": boundary_type_rows,
            "boundary_inclusive_longest_oscillation": boundary_longest,
        },
        "oscillation": {
            "raw_count": len(oscillations),
            "subtypes": dict(sorted((str(key), value) for key, value in subtype_counts.items())),
            "witness_cause_partitions": dict(
                sorted(
                    collections.Counter(
                        cause
                        for causes in oscillation_witness_causes.values()
                        for cause in causes
                    ).items()
                )
            ),
            "strong_textual_reversal_reapplication_count": len(strong_oscillations),
            "after_definite_mechanical_only_exclusion": len(
                oscillations_after_definite_mechanical
            ),
            "unflagged_count": len(unflagged_oscillations),
            "strong_unflagged_count": len(strong_unflagged),
            "longest": longest,
        },
        "mechanical_separation": {
            "sequence_partition": dict(sorted(cause_counts.items())),
            "sequence_denominator_D_seq": d_seq,
            "raw_reversal_edges": len(reversal_edges),
            "unflagged_reversal_edges": len(nonmechanical_reversal_edges),
            "wholesale_threshold": 0.80,
            "definite_rules": [
                "whitespace_only",
                "import_order_only",
                "volatile_metadata_only_overlap",
            ],
            "suspected_rules": [
                "generated/lock/build path or codegen command between writes",
                "at least 80% changed-line coverage or mutating git command between writes",
                "formatter/linter command between writes",
            ],
        },
        "reversal_latency": summarize_latencies(
            [edge.latency_seconds for edge in reversal_edges]
        ),
        "reverter_read_status": {
            "denominator_reversal_edges": len(reversal_edges),
            "categories": dict(sorted(read_categories.items())),
        },
        "file_types": by_type,
        "extensions": extension_rows,
        "examples": [
            {**sequence_summary(item), "population": population}
            for item, population in examples
        ],
        "audit_sequences": [sequence_summary(item) for item in sequences],
        "audit_boundary_inclusive_sequences": [
            sequence_summary(item) for item in boundary_sequences
        ],
        "example_availability": {
            "requested": 3,
            "available_raw_oscillations": len(oscillations),
            "available_additional_boundary_inclusive_oscillations": len(
                additional_boundary_oscillations
            ),
            "printed": len(examples),
        },
    }
    return result, pairs, sequences


def rate_text(count: int, denominator: int) -> str:
    if denominator == 0:
        return f"{count} / {denominator} (undefined)"
    return f"{count} / {denominator} ({count / denominator:.3%})"


def format_seconds(value: float | None) -> str:
    if value is None:
        return "not observed"
    if value < 60:
        return f"{value:.1f} s"
    if value < 3600:
        return f"{value / 60:.1f} min"
    if value < 86400:
        return f"{value / 3600:.1f} h"
    return f"{value / 86400:.1f} d"


def render_report(result: Mapping[str, Any]) -> str:
    denominators = result["denominators"]
    d_pair = int(denominators["D_pair_cross_agent_overlapping_transitions"])
    d_seq = int(denominators["D_seq_multi_agent_region_sequences"])
    oscillation = result["oscillation"]
    unflagged = int(oscillation["unflagged_count"])
    raw_oscillations = int(oscillation["raw_count"])
    strong_unflagged = int(oscillation["strong_unflagged_count"])
    reversal_edges = int(denominators["reversal_edges_exact_or_partial_at_0_75"])
    if raw_oscillations:
        verdict = (
            f"**Does it happen? Yes under the requested broad ABA definition: "
            f"{rate_text(raw_oscillations, d_seq)} multi-agent region-write sequences recurred "
            f"(denominator `D_seq={d_seq:,}`). However, "
            f"{rate_text(reversal_edges, d_pair)} eligible transitions reversed prior work "
            f"(denominator `D_pair={d_pair:,}`), and unflagged textual reversal–reapplication "
            f"cycles were {rate_text(strong_unflagged, d_seq)} (denominator `D_seq={d_seq:,}`).**"
        )
    else:
        verdict = (
            f"**Does it happen? Not observed in the measurable structured-result channel: "
            f"{rate_text(raw_oscillations, d_seq)} broad ABA sequences (denominator "
            f"`D_seq={d_seq:,}`) and {rate_text(reversal_edges, d_pair)} reversal transitions "
            f"(denominator `D_pair={d_pair:,}`).**"
        )
    lines = [verdict, "", "# Cross-agent reversal and oscillation", ""]
    corpus = result["corpus"]
    diagnostics = corpus["diagnostics"]
    lines.extend(
        [
            "## Coverage caveat",
            "",
            (
                "This is one team, one Claude Code harness, and a Node-dominated workload in "
                "which agents were largely assigned compatible goals. A low rate is evidence only "
                "about this workload; it is not evidence that adversarial task assignment is safe."
            ),
            "",
            (
                "The literal `C:/Users/USER/.claude/projects` path did not exist on this host. "
                "The run used the current user's equivalent `.claude/projects` tree, opened "
                f"read-only, and froze {int(corpus['corpus_file_count']):,} JSONL byte prefixes "
                f"({int(corpus['corpus_bytes']):,} bytes) at `{corpus['snapshot_utc']}`. "
                f"The frozen-prefix SHA-256 is `{corpus['corpus_snapshot_sha256']}`."
            ),
            "",
            (
                "Structured event counts are lower bounds; ratios are not directionally bounded "
                "because missing operations can enter either numerator or denominator. Paths "
                "embedded only in Bash/PowerShell commands are not writes in either. "
                "Successful subagent Edit/Write results in this corpus lack `toolUseResult`; input "
                "`old_string`/`new_string` was deliberately not substituted because the requested "
                "hunk ranges and complete pre-image are result evidence."
            ),
            "",
            (
                f"The frozen scan observed {int(diagnostics.get('shell_calls_Bash', 0)):,} Bash "
                f"and {int(diagnostics.get('shell_calls_PowerShell', 0)):,} PowerShell calls, "
                "whose command-string paths remain outside structured write capture. It also "
                f"quarantined {int(diagnostics.get('dedup_session_identity_conflicts', 0)):,} "
                "globally repeated tool-use IDs whose session-based identities conflicted."
            ),
            "",
            "## Verdict and denominators",
            "",
            (
                f"The broad writer-recurrence (ABA) rate is {rate_text(raw_oscillations, d_seq)}; "
                f"denominator `D_seq={d_seq:,}`. The stronger textual reversal-and-reapplication "
                f"count after cause flags is {rate_text(strong_unflagged, d_seq)} against the "
                f"fixed original denominator "
                f"`D_seq={d_seq:,}`. Broad ABA is a structural candidate count, not evidence that "
                "the file state failed to progress or that agents held opposed objectives. With "
                f"only {d_seq:,} eligible sequences and no result-side subagent coverage, this can "
                "document the concern as unobserved in this channel, not as design-wide out of scope."
            ),
            "",
            (
                f"`D_pair = {d_pair:,}`: all deduplicated, successful, strictly serialized, "
                "locally state-continuous, adjacent cross-agent write pairs whose result-derived "
                "changed ranges overlap after A's post-image is mapped into B's pre-image by "
                "symmetric exact-line alignment. Local continuity means every contacted A block "
                "maps exactly; unrelated insertions/deletions elsewhere may shift its coordinates."
            ),
            "",
            (
                f"`D_seq = {d_seq:,}`: all maximal multi-agent region-write sequences formed by "
                "one or more contiguous `D_pair` edges. This is the denominator for every sequence "
                "classification and headline rate. An oscillation additionally requires one "
                "structured-patch block contact path to persist through the repeated-writer subrun."
            ),
            "",
            "## Four sequence classifications",
            "",
            (
                "The four rows are mutually exclusive and exhaustive over `D_seq`, with precedence "
                "oscillation → exact reversal → partial reversal → independent co-editing. Thus an "
                "A–B–A exact cycle on one persisted region appears in the oscillation row, not "
                "again in exact reversal; writer recurrence across unrelated regions does not count."
            ),
            "",
            "| Classification | Count and fraction of all multi-agent region-write sequences |",
            "|---|---:|",
        ]
    )
    seq_counts = result["sequence_classifications_mutually_exclusive"]
    for key, label in (
        ("oscillation", "Oscillation (writer recurs after a foreign phase)"),
        ("exact_reversal", "Exact reversal, non-oscillating"),
        ("partial_reversal", "Partial reversal, non-oscillating"),
        ("independent_coediting", "Independent co-editing control"),
    ):
        count = int(seq_counts.get(key, 0))
        lines.append(f"| {label} | {rate_text(count, d_seq)}; denominator `D_seq={d_seq:,}` |")
    flags = result["sequence_nonexclusive_flags"]
    lines.extend(
        [
            "",
            (
                f"As non-exclusive content flags, {rate_text(int(flags['contains_exact_reversal']), d_seq)} "
                f"sequences contained an exact inverse edge and "
                f"{rate_text(int(flags['contains_partial_reversal_at_0_75']), d_seq)} contained a "
                f"baseline partial inverse edge; both use denominator `D_seq={d_seq:,}` and can "
                "also be oscillations."
            ),
            "",
            "### Pair-transition control",
            "",
            (
                "An exact reversal restores every A block contacted by B at its aligned structural "
                "location, swapping exact old/new content; B may also have disjoint hunks. Complete "
                "whole-file restoration is accepted as a stronger grouping-insensitive case. "
                "Different hunk grouping in a merely regional restore remains a conservative false "
                "negative."
            ),
            "",
            "| Successive-pair label | Count and fraction of eligible overlapping pairs |",
            "|---|---:|",
        ]
    )
    pair_counts = result["pair_classifications"]
    for key, label in (
        ("exact_reversal", "Exact reversal"),
        ("partial_reversal", "Partial reversal at 0.75"),
        ("independent_coediting", "Independent co-editing"),
    ):
        count = int(pair_counts.get(key, 0))
        lines.append(f"| {label} | {rate_text(count, d_pair)}; denominator `D_pair={d_pair:,}` |")

    lines.extend(
        [
            "",
            "### Partial-reversal definition and sensitivity",
            "",
            (
                "Within the contacted blocks, the lexical inverse score is the fraction of A-added "
                "tokens B removes and the fraction of A-removed tokens B restores, matched with "
                "multiplicity. For a replacement the score is the smaller direction; for a pure "
                "insertion/deletion it is the one defined direction. A non-exact edge is "
                "“substantial” at the fixed-before-run baseline `score ≥ 0.75`. This prevents `1→2→3` "
                "from counting merely because B removed `2` without restoring `1`."
            ),
            "",
            "| Threshold | Partial pairs / fixed `D_pair` | Partial-only non-oscillating sequences / fixed `D_seq` |",
            "|---:|---:|---:|",
        ]
    )
    for threshold in ("0.50", "0.75", "0.90"):
        row = result["partial_sensitivity"][threshold]
        partial_pairs = int(row["partial_reversal_pairs"])
        seq_partial = int(
            row["mutually_exclusive_sequence_counts"].get("partial_reversal", 0)
        )
        lines.append(
            f"| {threshold} | {rate_text(partial_pairs, d_pair)}; denominator `D_pair={d_pair:,}` "
            f"| {rate_text(seq_partial, d_seq)}; denominator `D_seq={d_seq:,}` |"
        )
    line_atom = result["line_atom_partial_pairs_at_0_75"]
    boundary = result["boundary_anchor_sensitivity"]
    boundary_denominator = int(boundary["boundary_inclusive_denominator"])
    boundary_sequence_denominator = int(
        boundary["boundary_inclusive_sequence_denominator"]
    )
    boundary_oscillations = int(
        boundary["boundary_inclusive_raw_oscillations"]
    )
    lines.extend(
        [
            "",
            (
                f"Tokenization sensitivity: using exact logical lines as atoms at the same 0.75 "
                f"threshold yields {rate_text(int(line_atom['count']), d_pair)} partial pairs; "
                f"denominator `D_pair={d_pair:,}`."
            ),
            "",
            "### Insertion-anchor boundary sensitivity",
            "",
            (
                "Primary overlap excludes a zero-width insertion exactly at the boundary of A's "
                "changed range. Including those anchors adds "
                f"{int(boundary['additional_boundary_only_pairs']):,} pairs, giving denominator "
                f"`D_pair_boundary={boundary_denominator:,}`. Rebuilding maximal runs from all "
                f"boundary-inclusive edges gives denominator `D_seq_boundary="
                f"{boundary_sequence_denominator:,}`; it is not derived arithmetically from `D_seq`."
            ),
            "",
            "| Boundary-inclusive pair label | Count / `D_pair_boundary` |",
            "|---|---:|",
        ]
    )
    for key, label in (
        ("exact_reversal", "Exact reversal"),
        ("partial_reversal", "Partial reversal at 0.75"),
        ("independent_coediting", "Independent co-editing"),
    ):
        count = int(boundary["boundary_inclusive_classifications"].get(key, 0))
        lines.append(
            f"| {label} | {rate_text(count, boundary_denominator)}; denominator "
            f"`D_pair_boundary={boundary_denominator:,}` |"
        )
    lines.extend(
        [
            "",
            "| Boundary-inclusive sequence label | Count / `D_seq_boundary` |",
            "|---|---:|",
        ]
    )
    for key, label in (
        ("oscillation", "Oscillation"),
        ("exact_reversal", "Exact reversal, non-oscillating"),
        ("partial_reversal", "Partial reversal, non-oscillating"),
        ("independent_coediting", "Independent co-editing"),
    ):
        count = int(
            boundary["boundary_inclusive_sequence_classifications"].get(key, 0)
        )
        lines.append(
            f"| {label} | {rate_text(count, boundary_sequence_denominator)}; denominator "
            f"`D_seq_boundary={boundary_sequence_denominator:,}` |"
        )
    boundary_after_mechanical = int(
        boundary[
            "boundary_inclusive_after_definite_mechanical_only_exclusion"
        ]
    )
    lines.extend(
        [
            "",
            (
                "Headline sensitivity: broad ABA is "
                f"{rate_text(raw_oscillations, d_seq)} with denominator `D_seq={d_seq:,}` under "
                "primary overlap and "
                f"{rate_text(boundary_oscillations, boundary_sequence_denominator)} with denominator "
                f"`D_seq_boundary={boundary_sequence_denominator:,}` when boundary anchors count. "
                "After excluding oscillations whose repeated-writer witnesses are all definitely "
                "mechanical it is "
                f"{rate_text(boundary_after_mechanical, boundary_sequence_denominator)} with "
                f"denominator `D_seq_boundary={boundary_sequence_denominator:,}`."
            ),
        ]
    )
    lines.extend(
        [
            "",
            "## Oscillation detail",
            "",
        ]
    )
    subtype = oscillation["subtypes"]
    for key, label in (
        ("exact_cycle", "Exact S0→S1→S0→S1 cycles"),
        ("reversal_reapplication", "Partial/exact reversal followed by re-application"),
        ("ABA_only", "ABA recurrence without textual no-progress proof"),
    ):
        count = int(subtype.get(key, 0))
        lines.append(
            f"- {label}: {rate_text(count, d_seq)}; denominator `D_seq={d_seq:,}`."
        )
    longest = oscillation.get("longest")
    if longest:
        lines.extend(
            [
                "",
                (
                    f"The longest observed oscillation has {int(longest['write_count'])} writes, "
                    f"{int(longest['agent_phase_count'])} agent phases, and "
                    f"**{int(longest['distinct_agent_count'])} distinct agents** over "
                    f"{format_seconds(float(longest['duration_seconds']))}; it is a "
                    f"`{longest['file_type']}` file (`{longest['path']}`)."
                ),
            ]
        )
    else:
        lines.extend(["", "No oscillation was observed, so there is no longest run."])

    mechanical = result["mechanical_separation"]
    lines.extend(
        [
            "",
            "## Mechanical-cause separation",
            "",
            (
                "Definite mechanical overlap means exact whitespace-only change, import-order-only "
                "change, or contact solely through a recognized `modified`/`updated` timestamp "
                "metadata line. Suspected causes are generated/lock/build paths, an "
                "observed formatter/codegen/git command strictly between writes, or a Write/Edit "
                "whose changed-line coverage reaches 80% of the pre- or post-image. The 80% flag is "
                "a disclosed audit heuristic, not a fitted classifier. Oscillation filtering is "
                "computed on persisted contact paths, so a one-edge neighboring hunk cannot change "
                "the cause assigned to the recurring region. Changed or ambiguously "
                "aligned contacted regions are excluded before `D_pair`; exact unrelated changes "
                "elsewhere may shift the region without excluding it."
            ),
            "",
            "| Cause partition | Count and fraction of all sequences | Treatment |",
            "|---|---:|---|",
        ]
    )
    treatments = {
        "definite_mechanical_only": "excluded from substantive numerator",
        "mixed_definite_mechanical": "flagged; not silently called substantive",
        "suspected_generated_or_codegen": "flagged suspected artifact",
        "suspected_wholesale_or_git": "flagged suspected tree rewrite",
        "suspected_formatter_or_linter": "flagged suspected formatter/linter",
        "no_detected_mechanical_cause": "retained as unflagged, not proven semantic",
    }
    cause_partition = mechanical["sequence_partition"]
    for key in treatments:
        count = int(cause_partition.get(key, 0))
        lines.append(
            f"| `{key}` | {rate_text(count, d_seq)}; denominator `D_seq={d_seq:,}` | "
            f"{treatments[key]} |"
        )
    attrition = result["attrition"]
    unflagged_population = int(cause_partition.get("no_detected_mechanical_cause", 0))
    lines.extend(
        [
            "",
            (
                f"Raw oscillations: {rate_text(raw_oscillations, d_seq)}; after excluding only "
                f"oscillations whose repeated-writer witnesses are definitely mechanical: "
                f"{rate_text(int(oscillation['after_definite_mechanical_only_exclusion']), d_seq)}; "
                f"with every detected/suspected mechanical cause removed: "
                f"{rate_text(unflagged, d_seq)}. Each is a count against the fixed original "
                f"denominator `D_seq={d_seq:,}`. Conditional on the unflagged population, the "
                f"oscillation rate is {rate_text(unflagged, unflagged_population)} with denominator "
                f"`D_seq_unflagged={unflagged_population:,}`."
            ),
            "",
            (
                f"There were {int(attrition.get('contacted_region_alignment_failures', 0)):,} serialized "
                "cross-agent exact-metadata raw-coordinate contacts excluded because the contacted "
                "A region could not be symmetrically aligned into B's pre-image; "
                f"{int(attrition.get('continuity_breaks_with_observed_git_mutation', 0)):,} had an "
                "observed mutating git command between the two structured writes. A break can also "
                "come from Bash/PowerShell, another unlocalized writer, or stale/copy artifacts."
            ),
            "",
            "## Reversal timing",
            "",
        ]
    )
    latency = result["reversal_latency"]
    d_reversal = int(latency["denominator_reversal_edges"])
    lines.append(
        f"For all {d_reversal:,} exact-or-baseline-partial reversal edges (denominator "
        f"`D_reversal={d_reversal:,}`), A-completion to B-invocation latency was: median "
        f"{format_seconds(latency['median_seconds'])}, Q1 {format_seconds(latency['q1_seconds'])}, "
        f"Q3 {format_seconds(latency['q3_seconds'])}, p90 {format_seconds(latency['p90_seconds'])}, "
        f"and maximum {format_seconds(latency['maximum_seconds'])}."
    )
    lines.extend(
        [
            "",
            "| Latency bin | Reversal edges / `D_reversal` |",
            "|---|---:|",
        ]
    )
    bin_labels = {
        "under_1m": "Under 1 minute",
        "1m_to_5m": "1–5 minutes",
        "5m_to_1h": "5 minutes–1 hour",
        "1h_to_24h": "1–24 hours",
        "24h_or_more": "24 hours or more",
    }
    for key, label in bin_labels.items():
        count = int(latency["bins"].get(key, 0))
        lines.append(
            f"| {label} | {rate_text(count, d_reversal)}; denominator `D_reversal={d_reversal:,}` |"
        )

    read_status = result["reverter_read_status"]
    lines.extend(
        [
            "",
            "## Did the reverting agent read the region?",
            "",
            (
                "The categories below partition every exact-or-baseline-partial reversal edge. "
                "“No observed localized Read” is not literal unawareness: Grep, prompts, shared "
                "messages, Bash, PowerShell, and unstructured subagent results can expose content."
            ),
            "",
            "| Reverter's strongest observed Read evidence | Count / all reversal edges |",
            "|---|---:|",
        ]
    )
    read_labels = {
        "post_A_verified_region_read": "Post-A region Read; content verified against B pre-image",
        "post_A_offset_only_region_read": "Post-A region Read; offset only",
        "only_pre_A_region_read": "Only a pre-A region Read",
        "post_A_file_read_outside_region": "Post-A file Read outside reverting region",
        "no_observed_localized_read": "No observed localized Read",
        "unclassified": "Unclassified",
    }
    for key, label in read_labels.items():
        count = int(read_status["categories"].get(key, 0))
        lines.append(
            f"| {label} | {rate_text(count, d_reversal)}; denominator `D_reversal={d_reversal:,}` |"
        )

    lines.extend(
        [
            "",
            "## File-type concentration",
            "",
            "| File type | Oscillations / sequences in that type | Share of all oscillations |",
            "|---|---:|---:|",
        ]
    )
    for file_type, row in result["file_types"].items():
        type_denom = int(row["sequence_denominator"])
        count = int(row["oscillations"])
        lines.append(
            f"| `{file_type}` | {rate_text(count, type_denom)}; denominator "
            f"`D_seq_type={type_denom:,}` | {rate_text(count, raw_oscillations)}; denominator "
            f"`D_oscillation={raw_oscillations:,}` |"
        )

    lines.extend(
        [
            "",
            "Boundary-inclusive file-type sensitivity:",
            "",
            "| File type | Oscillations / boundary-inclusive sequences in that type |",
            "|---|---:|",
        ]
    )
    for file_type, row in boundary["boundary_inclusive_file_types"].items():
        type_denom = int(row["sequence_denominator"])
        count = int(row["oscillations"])
        lines.append(
            f"| `{file_type}` | {rate_text(count, type_denom)}; denominator "
            f"`D_seq_boundary_type={type_denom:,}` |"
        )

    lines.extend(
        [
            "",
            "## Measurement attrition",
            "",
            (
                "The requested literal reuse of `extract_hazards.py` was not possible without "
                "violating the result-only requirement: that historical iterator reads call input "
                "paths and exposes neither tool IDs nor results (and importing it has an output "
                "directory side effect). It remains unchanged. This instrument uses the same "
                "JSONL traversal shape but performs its own source-local call/result pairing and "
                "uses only result paths. This is an explicit instrument deviation, not claimed reuse."
            ),
            "",
            "| Stage | Count |",
            "|---|---:|",
        ]
    )
    attrition_order = (
        "deduplicated_successful_write_operations",
        "write_operations_without_result_path",
        "result_localized_non_noop_write_events",
        "usable_exact_write_events",
        "adjacent_result_localized_write_pairs",
        "cross_agent_adjacent_pairs",
        "strictly_serialized_cross_agent_pairs",
        "serialized_pairs_missing_exact_write_metadata",
        "serialized_exact_metadata_pairs",
        "mapped_coordinate_disjoint_exact_metadata_pairs",
        "unmapped_or_ambiguous_region_pairs",
        "coordinate_contact_exact_metadata_pairs",
        "shifted_coordinate_contacts_recovered",
        "full_file_state_continuous_contact_pairs",
        "contacted_region_alignment_failures",
        "local_state_continuity_breaks",
        "locally_state_continuous_contact_pairs",
        "boundary_anchor_only_pairs_sensitivity",
        "eligible_overlapping_pairs_D_pair",
        "multi_agent_region_sequences_D_seq",
    )
    for key in attrition_order:
        lines.append(f"| `{key}` | {int(attrition.get(key, 0)):,} |")

    lines.extend(
        [
            "",
            "### Result-metadata coverage by source",
            "",
            (
                "These are successful result *occurrences* before global duplicate quarantine. "
                "A usable write requires a result path, a nonempty valid patch, and a string "
                "pre-image; field presence or `originalFile: null` is not counted as usable."
            ),
            "",
            "| Transcript source | Localized Reads / successful Reads | Usable nonempty writes / successful writes |",
            "|---|---:|---:|",
        ]
    )
    source_labels = {
        "main": "Main transcript",
        "direct_subagent": "Direct subagent",
        "workflow_subagent": "Workflow subagent",
    }
    for source, label in source_labels.items():
        row = result["schema_coverage_by_source"][source]
        read_denominator = int(row["successful_read_result_occurrences"])
        write_denominator = int(row["successful_write_result_occurrences"])
        lines.append(
            f"| {label} | {rate_text(int(row['localized_structured_read_occurrences']), read_denominator)}; "
            f"denominator `{read_denominator:,}` successful Read results | "
            f"{rate_text(int(row['usable_nonempty_patch_preimage_occurrences']), write_denominator)}; "
            f"denominator `{write_denominator:,}` successful Edit/Write results |"
        )

    examples = result["examples"]
    lines.extend(["", "## Concrete oscillation sequences", ""])
    availability = result["example_availability"]
    if int(availability["printed"]) < 3:
        primary_available = int(availability["available_raw_oscillations"])
        boundary_additional_available = int(
            availability["available_additional_boundary_inclusive_oscillations"]
        )
        available_count = primary_available + boundary_additional_available
        noun = "sequence" if available_count == 1 else "sequences"
        verb = "exists" if available_count == 1 else "exist"
        lines.append(
            f"Only {available_count} distinct raw oscillation {noun} {verb} across the primary "
            f"population ({primary_available} in `D_seq`) and additional boundary-inclusive "
            f"sensitivity candidates ({boundary_additional_available}); all are printed. Three "
            "examples do not exist."
        )
        lines.append("")
    else:
        lines.append(
            "The three examples are selected deterministically: strongest textual cycle first, "
            "then fewer detected mechanical causes, then smaller full changed-block diffs. Every "
            "changed line for every write in the selected sequence is printed; patch context is "
            "not part of `structuredPatch` change blocks."
        )
        lines.append("")
    for index, example in enumerate(examples, 1):
        lines.extend(
            [
                f"### Example {index}: `{example['path']}`",
                "",
                (
                    f"Classification: `{example['classification']}` / "
                    f"`{example['oscillation_subtype']}`; cause partition "
                    f"`{example['cause_partition']}`; file type `{example['file_type']}`; "
                    f"population `{example['population']}`; "
                    "persisted-region witness cause(s) `"
                    + ", ".join(example["oscillation_witness_cause_partitions"])
                    + "`; "
                    f"{int(example['write_count'])} writes by "
                    f"{int(example['distinct_agent_count'])} distinct agents over "
                    f"{format_seconds(float(example['duration_seconds']))}."
                ),
                "",
            ]
        )
        for write_index, write in enumerate(example["writes"], 1):
            lines.extend(
                [
                    f"Write {write_index}: agent `{write['agent']}`, `{write['tool']}`, call "
                    f"`{write['call_timestamp']}`, result `{write['result_timestamp']}`.",
                    "",
                    "````diff",
                    write["diff"],
                    "````",
                    "",
                ]
            )
        for edge_index, edge in enumerate(example["edges"], 1):
            lines.append(
                f"Edge {edge_index}: `{edge['classification']}`, lexical inverse score "
                f"`{float(edge['lexical_inverse_score'] or 0.0):.3f}`, latency "
                f"{format_seconds(float(edge['latency_seconds']))}, Read status "
                f"`{edge['read_category'] or 'not_applicable_non_reversal'}`."
            )
        lines.append("")

    main_success_writes = sum(
        int(value)
        for key, value in diagnostics.items()
        if key.startswith("successful_result_occurrences_main_")
        and key.rsplit("_", 1)[-1] in WRITE_TOOLS
    )
    lines.extend(
        [
            "## What I got wrong",
            "",
            "| Prior premise | Verdict | Correction |",
            "|---|---|---|",
            (
                "| “98.7% of Edit/Write results have patches and complete pre-images” | Wrong "
                "population | That figure describes main-transcript field presence and includes "
                "null/empty cases; successful subagent results have no `toolUseResult` object. The "
                "primary rate is conditional on usable result evidence. |"
            ),
            (
                "| The historical event walker could supply region writes | False | It walks "
                "call inputs only. Result-local pairing and global duplicate quarantine were "
                "required. |"
            ),
            (
                "| Any ABA sequence proves opposed goals | Too strong | ABA is a structural "
                "candidate; compatible iteration, rollback after tests, and handoff remain viable "
                "explanations. |"
            ),
            "",
            "## Claims that could NOT be verified",
            "",
            "- That any structural reversal or ABA recurrence was caused by genuinely opposed task objectives.",
            "- The reversal/oscillation behavior of metadata-less subagent Edit/Write results.",
            "- Writes performed through Bash, PowerShell, git, formatters, linters, code generators, or other tools without structured write results.",
            "- Actual reverter awareness when no localized Read was observed; prompts, Grep, shell output, and shared context are not joined.",
            "- Semantic equivalence reversals that restore behavior without restoring lexical tokens or exact lines.",
            "- Generalization beyond this team, harness, compatible-goal workload, and Node-dominated corpus.",
            "- That absence of a detected formatter/codegen/git command proves a change was non-mechanical.",
            "",
            "## What would change this verdict",
            "",
            "1. Persist result-side `filePath`, `structuredPatch`, and non-null `originalFile` for subagent writes, then rerun the same fixed-denominator classifier.",
            "2. Persist structured paths and pre/post state for Bash, PowerShell, git, formatter, linter, and codegen mutations so continuity breaks become observable writes.",
            "3. Attach immutable task-objective IDs and explicit incompatibility labels to agents; then compare structural cycles under compatible versus opposed assignments.",
            "4. Replicate on intentionally adversarial assignments, independent teams/harnesses, and a language-balanced corpus.",
            "5. Blind-review every unflagged candidate with task prompts, tests, and repository state; genuine opposed-goal cycles would raise the substantive numerator.",
            "",
            "## Confidence by claim",
            "",
            "| Claim | Confidence | Reason |",
            "|---|---|---|",
            (
                f"| `D_pair={d_pair:,}` and `D_seq={d_seq:,}` within the frozen structured-result "
                "slice | High | Byte-prefix snapshot, result-only paths, source-local pairing, "
                "global identity-conflict quarantine, exact patch application, strict ordering, "
                "and symmetric exact-line mapping at contacted blocks; ambiguous repeated-line "
                "alignments are rejected. |"
            ),
            (
                "| Exact reversal counts within `D_pair` | Moderate-high, conservative | Exact "
                "regional inverse blocks and complete-restoration cases are unambiguous; a regional "
                "restore expressed with different hunk grouping can still be missed. |"
            ),
            (
                "| Partial reversal counts | Moderate | The construct is explicit and threshold "
                "sensitivity is reported, but lexical tokens are an imperfect proxy for intent. |"
            ),
            (
                "| Structural ABA/oscillation counts | High structurally; low as goal conflict | "
                "Agent recurrence and state continuity are exact in-slice; objectives are not "
                "recorded or labeled. |"
            ),
            (
                "| Boundary-inclusive ABA sensitivity | High structurally in-slice | Boundary "
                "edges are fully materialized and maximal runs are rebuilt; the sensitivity is "
                "not inferred from pair counts. |"
            ),
            (
                "| Observed localized Read categories | High as recorded-tool evidence; low as "
                "actual awareness | Result paths/ranges/content are exact where present, but other "
                "information channels are omitted. |"
            ),
            (
                "| Mechanical separation | Moderate for whitespace/import; low-to-moderate for "
                "codegen/git | Definite predicates are content-based; command/path and 80% flags "
                "are incomplete heuristics. |"
            ),
            (
                "| Low rate applies outside this workload | No supported claim | One team, one "
                "harness, compatible goals, Node dominance, and missing subagent/shell patches. |"
            ),
            "",
            "## Reproduction",
            "",
            "```powershell",
            "python instruments/oscillation/analyze.py `",
            "  --corpus \"$env:USERPROFILE\\.claude\\projects\" `",
            "  --output exploratory/oscillation/results.json `",
            "  --manifest-output exploratory/oscillation/corpus-manifest.json `",
            "  --report-output exploratory/oscillation/RESULTS.md",
            "python -m unittest instruments.oscillation.test_analyze -v",
            "```",
            "",
            (
                f"Diagnostic context: the frozen scan observed {main_success_writes:,} successful "
                f"main Edit/Write result occurrences. The sideagent result channel was separately "
                "counted in schema diagnostics; its successful writes cannot enter `D_pair` without "
                "result paths/ranges/pre-images."
            ),
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="read-only Claude transcript root",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exploratory/oscillation/results.json"),
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=Path("exploratory/oscillation/corpus-manifest.json"),
    )
    parser.add_argument(
        "--report-output",
        type=Path,
        default=Path("exploratory/oscillation/RESULTS.md"),
    )
    parser.add_argument("--progress-every", type=int, default=250)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    corpus = args.corpus.resolve()
    if not corpus.is_dir():
        raise SystemExit(f"corpus directory does not exist: {corpus}")
    operations, commands, metadata, manifest_files = scan_corpus(
        corpus, progress_every=max(args.progress_every, 0)
    )
    result, _, _ = analyze_population(operations, commands, metadata)
    manifest = {
        "schema_version": 1,
        "snapshot_utc": metadata["snapshot_utc"],
        "file_count": metadata["corpus_file_count"],
        "byte_count": metadata["corpus_bytes"],
        "frozen_prefix_sha256": metadata["corpus_snapshot_sha256"],
        "paths_redacted": True,
        "files": manifest_files,
    }
    atomic_write_json(args.output.resolve(), result)
    atomic_write_json(args.manifest_output.resolve(), manifest)
    atomic_write_text(args.report_output.resolve(), render_report(result))
    print(f"wrote {args.output.resolve()}", flush=True)
    print(f"wrote {args.manifest_output.resolve()}", flush=True)
    print(f"wrote {args.report_output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
