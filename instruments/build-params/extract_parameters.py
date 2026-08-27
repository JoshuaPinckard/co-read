"""Extract capacity-planning parameters from frozen Claude Code transcripts.

This extends the event-walk shape in ``instruments/hazard/extract_hazards.py``:
JSONL files are traversed read-only, tool calls are paired to results within a
source file, and copied tool-use IDs are reconciled globally.  Unlike the
historical path-only walker, this instrument retains only compact metadata
needed for distributions; it never emits file contents or command strings.

The corpus is frozen as byte prefixes before the first byte is read.  The
manifest uses the same ordinal/length/raw-byte SHA-256 construction as the
oscillation study.
"""

from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass
import datetime as dt
import hashlib
import heapq
import itertools
import json
import math
import ntpath
import os
from pathlib import Path
import posixpath
import re
import sys
from typing import Any, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from instruments.hazard.invalidation_core import (  # noqa: E402
    ChangeBlock,
    normalize_windows_path,
    parse_structured_patch,
)


CORE_READ_TOOLS = {"Read"}
CORE_WRITE_TOOLS = {"Edit", "Write"}
CORE_TOOLS = CORE_READ_TOOLS | CORE_WRITE_TOOLS
ADJACENT_READ_TOOLS = {"NotebookRead"}
ADJACENT_WRITE_TOOLS = {"NotebookEdit", "MultiEdit"}
STRUCTURED_TOOLS = CORE_TOOLS | ADJACENT_READ_TOOLS | ADJACENT_WRITE_TOOLS
SHELL_TOOLS = {"Bash", "PowerShell", "Powershell"}

SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".cxx", ".go", ".h", ".hpp",
    ".java", ".js", ".jsx", ".kt", ".kts", ".lua", ".mjs", ".php",
    ".pl", ".ps1", ".py", ".r", ".rb", ".rs", ".sh", ".swift",
    ".ts", ".tsx", ".vue", ".zig",
}
MARKDOWN_EXTENSIONS = {".md", ".mdx", ".rst", ".adoc"}
CONFIG_EXTENSIONS = {
    ".cfg", ".conf", ".ini", ".properties", ".toml", ".xml", ".yaml",
    ".yml", ".env",
}
LOCK_BASENAMES = {
    "bun.lock", "bun.lockb", "cargo.lock", "composer.lock", "gemfile.lock",
    "go.sum", "package-lock.json", "pnpm-lock.yaml", "poetry.lock",
    "uv.lock", "yarn.lock",
}
BINARY_EXTENSIONS = {
    ".7z", ".a", ".avi", ".bin", ".bmp", ".class", ".dll", ".doc",
    ".docx", ".dylib", ".eot", ".exe", ".gif", ".gz", ".ico", ".jar",
    ".jpeg", ".jpg", ".mov", ".mp3", ".mp4", ".o", ".otf", ".pdf",
    ".png", ".ppt", ".pptx", ".pyc", ".so", ".tar", ".tgz", ".ttf",
    ".wav", ".webm", ".webp", ".woff", ".woff2", ".xls", ".xlsx",
    ".zip",
}
LOG_EXTENSIONS = {".log", ".jsonl", ".ndjson"}
COMMON_PATH_BASENAMES = {
    ".babelrc", ".dockerignore", ".editorconfig", ".eslintignore",
    ".eslintrc", ".gitattributes", ".gitignore", ".npmrc", ".prettierignore",
    ".prettierrc", "agents.md", "claude.md", "dockerfile", "gemfile",
    "license", "makefile", "package.json", "readme", "tsconfig.json",
}
KNOWN_PATH_EXTENSIONS = (
    SOURCE_EXTENSIONS
    | MARKDOWN_EXTENSIONS
    | CONFIG_EXTENSIONS
    | BINARY_EXTENSIONS
    | LOG_EXTENSIONS
    | {".csv", ".json", ".lock", ".sql", ".tsv", ".txt"}
)

SENSITIVE_COMPONENT_RE = re.compile(
    r"^(?:\.env(?:\..*)?|\.aws|\.ssh|id_(?:rsa|dsa|ecdsa|ed25519)|"
    r".*(?:credential|secret|token|vault).*)$",
    re.IGNORECASE,
)
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
URL_RE = re.compile(r"^(?:https?|ssh|git|data|file)://", re.IGNORECASE)
ENV_PREFIX_RE = re.compile(
    r"^(?:\$env:USERPROFILE|\$\{?HOME\}?|%USERPROFILE%|~)(?=$|[\\/])",
    re.IGNORECASE,
)
TOKEN_RE = re.compile(r'''"(?:[^"`]|`.)*"|'(?:[^']|'')*'|[^\s]+''')
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
UNC_RE = re.compile(r"^\\\\[^\\/]+[\\/][^\\/]+")
RELATIVE_RE = re.compile(r"^(?:\.\.?[\\/])")
PATH_FLAG_RE = re.compile(
    r"^(?:--?(?:config|cwd|dir|directory|file|filepath|input|output|path|project|"
    r"root|source|target|workspace)|/(?:config|file|path))(?::|=)?$",
    re.IGNORECASE,
)
ASSIGNMENT_PATH_RE = re.compile(
    r"^(?:--?(?:config|cwd|dir|directory|file|filepath|input|output|path|project|"
    r"root|source|target|workspace)|/(?:config|file|path))[:=](.+)$",
    re.IGNORECASE,
)

SHELL_WRITE_RE = re.compile(
    r"(?:^|[;&|]\s*|\b)(?:rm|del|erase|mv|move|cp|copy|mkdir|rmdir|touch|"
    r"sed\s+-i|perl\s+-p?i|truncate|tee|patch|git\s+(?:apply|am|checkout|clean|"
    r"reset|restore|revert|merge|cherry-pick|switch)|set-content|add-content|"
    r"out-file|new-item|remove-item|move-item|copy-item|rename-item)\b|"
    r"(?<![<>=])>{1,2}(?![>=])",
    re.IGNORECASE,
)
SHELL_READ_RE = re.compile(
    r"(?:^|[;&|]\s*|\b)(?:cat|type|head|tail|less|more|grep|rg|find|findstr|"
    r"ls|dir|stat|wc|diff|git\s+(?:diff|show|status|log)|get-content|"
    r"get-childitem|select-string|test-path)\b",
    re.IGNORECASE,
)


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (ValueError, OverflowError):
        return None


def iso_utc(epoch: float | None) -> str | None:
    if epoch is None or not math.isfinite(epoch):
        return None
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def nearest_rank(values: Iterable[int | float]) -> dict[str, Any]:
    ordered = sorted(values)
    result: dict[str, Any] = {
        "count": len(ordered),
        "percentile_method": "nearest-rank; rank=ceil(p*n)",
    }
    if not ordered:
        result.update({"p50": None, "p90": None, "p99": None, "max": None})
        return result

    def pick(p: float) -> int | float:
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]

    result.update(
        {"p50": pick(0.50), "p90": pick(0.90), "p99": pick(0.99), "max": ordered[-1]}
    )
    return result


def ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "ratio": numerator / denominator if denominator else None,
        "percent": 100.0 * numerator / denominator if denominator else None,
    }


def canonical_path(value: Any, cwd: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        normalized = normalize_windows_path(
            value, cwd if isinstance(cwd, str) and cwd else None
        )
    except (TypeError, ValueError):
        return None
    return normalized if ntpath.isabs(normalized) else None


def actor_for(record: Mapping[str, Any]) -> tuple[str | None, bool, str | None]:
    session = record.get("sessionId")
    session_value = session if isinstance(session, str) and session else None
    agent = record.get("agentId")
    if isinstance(agent, str) and agent:
        return agent, True, session_value
    return session_value, False, session_value


def source_kind(path: Path) -> str:
    lowered = [part.casefold() for part in path.parts]
    if "subagents" not in lowered:
        return "main"
    return "workflow_subagent" if "workflows" in lowered else "direct_subagent"


def repository_bucket(path: Path, corpus: Path) -> str:
    try:
        relative = path.relative_to(corpus)
    except ValueError:
        return "<outside-corpus>"
    return relative.parts[0].casefold() if relative.parts else "<corpus-root>"


def subagent_layout_hints(path: Path, corpus: Path) -> tuple[str | None, str | None]:
    try:
        parts = path.relative_to(corpus).parts
    except ValueError:
        return None, None
    lowered = [part.casefold() for part in parts]
    if "subagents" not in lowered:
        return None, None
    index = lowered.index("subagents")
    parent_session = parts[index - 1] if index >= 1 else None
    stem = path.stem
    agent = stem[len("agent-"):] if stem.casefold().startswith("agent-") else None
    return parent_session, agent


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


def normalized_utf8_span(lines: Sequence[str], no_newline: bool) -> int:
    if not lines:
        return 0
    total = sum(len(line.encode("utf-8", errors="surrogatepass")) + 1 for line in lines)
    return total - int(no_newline)


@dataclass(frozen=True)
class HunkMetrics:
    old_lines: int
    new_lines: int
    claim_lines: int
    old_bytes: int
    new_bytes: int
    claim_bytes: int


@dataclass(frozen=True)
class RawHunkMetrics:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    context_lines: int
    removed_lines: int
    added_lines: int
    transcript_utf8_old_span_bytes: int | None


def hunk_metrics(change: ChangeBlock) -> HunkMetrics:
    old_count = len(change.old_lines)
    new_count = len(change.new_lines)
    old_bytes = normalized_utf8_span(change.old_lines, change.old_no_newline)
    new_bytes = normalized_utf8_span(change.new_lines, change.new_no_newline)
    return HunkMetrics(
        old_lines=old_count,
        new_lines=new_count,
        claim_lines=max(old_count, new_count),
        old_bytes=old_bytes,
        new_bytes=new_bytes,
        claim_bytes=max(old_bytes, new_bytes),
    )


def logical_lines(content: str) -> tuple[str, ...]:
    lines = content.splitlines()
    if content.endswith(("\n", "\r")):
        lines.append("")
    return tuple(lines)


def validate_patch_against_original(
    original: str, changes: Sequence[ChangeBlock]
) -> bool:
    """Validate every old-side block against the transcript pre-image."""

    preimage = logical_lines(original)
    cursor = 0
    for block in sorted(changes, key=lambda item: (item.old_start, item.new_start)):
        index = 0 if block.old_start == 0 else block.old_start - 1
        if index < cursor or index > len(preimage):
            return False
        end = index + len(block.old_lines)
        if tuple(preimage[index:end]) != block.old_lines:
            return False
        cursor = end
    return True


def original_line_segments(content: str) -> tuple[str, ...]:
    segments = content.splitlines(keepends=True)
    if content.endswith(("\n", "\r")):
        segments.append("")
    return tuple(segments)


def raw_hunk_metrics(
    raw_patch: Sequence[Mapping[str, Any]], original: str | None
) -> tuple[RawHunkMetrics, ...]:
    segments = original_line_segments(original) if isinstance(original, str) else None
    result: list[RawHunkMetrics] = []
    for hunk in raw_patch:
        old_start = hunk.get("oldStart")
        old_count = hunk.get("oldLines")
        new_start = hunk.get("newStart")
        new_count = hunk.get("newLines")
        rendered = hunk.get("lines")
        if (
            any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (old_start, old_count, new_start, new_count))
            or not isinstance(rendered, list)
        ):
            raise ValueError("invalid raw structuredPatch hunk")
        context = removed = added = 0
        for line in rendered:
            if not isinstance(line, str):
                raise ValueError("non-string structuredPatch line")
            if line == "\\ No newline at end of file":
                continue
            if not line or line[0] not in {" ", "-", "+"}:
                raise ValueError("invalid structuredPatch line prefix")
            context += int(line[0] == " ")
            removed += int(line[0] == "-")
            added += int(line[0] == "+")
        exact_old_bytes = None
        if segments is not None:
            start_index = 0 if old_start == 0 else old_start - 1
            end_index = start_index + old_count
            if 0 <= start_index <= end_index <= len(segments):
                exact_old_bytes = sum(
                    len(segment.encode("utf-8", errors="surrogatepass"))
                    for segment in segments[start_index:end_index]
                )
        result.append(
            RawHunkMetrics(
                old_start=old_start,
                old_lines=old_count,
                new_start=new_start,
                new_lines=new_count,
                context_lines=context,
                removed_lines=removed,
                added_lines=added,
                transcript_utf8_old_span_bytes=exact_old_bytes,
            )
        )
    return tuple(result)


@dataclass(frozen=True)
class Call:
    tool_id: str
    tool: str
    actor: str
    explicit_actor: bool
    session: str
    call_ts: float
    cwd: str | None
    call_uuid: str | None
    repo_key: str
    source_kind: str
    source_ordinal: int
    source_line: int


@dataclass(frozen=True)
class MergedCall:
    tool_id: str
    tool: str
    actor: str
    sessions: tuple[str, ...]
    call_ts: float
    repo_keys: tuple[str, ...]


@dataclass(frozen=True)
class OperationCandidate:
    call: Call
    result_ts: float
    success: bool
    path: str | None
    read_start: int | None
    read_num_lines: int | None
    read_content_bytes: int | None
    read_content_line_match: bool | None
    hunks: tuple[HunkMetrics, ...] | None
    raw_hunks: tuple[RawHunkMetrics, ...] | None
    patch_sha256: str | None
    patch_applies_to_original: bool | None
    original_file_bytes: int | None
    original_file_present: bool
    original_file_key_present: bool
    write_result_content_bytes: int | None
    write_create_without_patch: bool
    metadata_status: str


@dataclass(frozen=True)
class Operation:
    tool_id: str
    tool: str
    actor: str
    sessions: tuple[str, ...]
    call_ts: float
    result_ts: float
    repo_keys: tuple[str, ...]
    success: bool
    path: str | None
    read_start: int | None
    read_num_lines: int | None
    read_content_bytes: int | None
    read_content_line_match: bool | None
    hunks: tuple[HunkMetrics, ...] | None
    raw_hunks: tuple[RawHunkMetrics, ...] | None
    patch_applies_to_original: bool | None
    original_file_bytes: int | None
    original_file_present: bool
    original_file_key_present: bool
    write_result_content_bytes: int | None
    write_create_without_patch: bool
    metadata_status: str


@dataclass(frozen=True)
class ShellMention:
    path: str | None
    kind: str
    is_pattern: bool


@dataclass(frozen=True)
class ShellCandidate:
    call: Call
    command_sha256: str
    intent: str
    mentions: tuple[ShellMention, ...]


@dataclass(frozen=True)
class ShellEvent:
    tool_id: str
    tool: str
    actor: str
    sessions: tuple[str, ...]
    timestamp: float
    repo_keys: tuple[str, ...]
    command_sha256: str
    intent: str
    mentions: tuple[ShellMention, ...]
    source_ordinal: int
    source_line: int


def clean_shell_token(token: str) -> str:
    value = token.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    value = value.strip(" \t\r\n,;(){}[]`")
    value = value.rstrip("&|")
    return value


def strip_line_suffix(value: str) -> str:
    match = re.match(r"^(.*\.[A-Za-z0-9_+-]{1,12}):\d+(?::\d+)?$", value)
    return match.group(1) if match else value


def looks_like_path(value: str, *, forced: bool = False) -> tuple[bool, str, bool]:
    if not value or URL_RE.match(value) or value.startswith(("git@", "@{")):
        return False, "excluded", False
    if value in {".", "..", "/", "\\"}:
        return True, "directory", False
    is_pattern = any(char in value for char in "*?[]{}")
    if WINDOWS_ABSOLUTE_RE.match(value) or UNC_RE.match(value):
        return True, "absolute", is_pattern
    if RELATIVE_RE.match(value):
        return True, "explicit_relative", is_pattern
    lowered = value.casefold().rstrip("\\/")
    basename = ntpath.basename(lowered)
    suffix = ntpath.splitext(basename)[1]
    if forced and not value.startswith("-"):
        return True, "flag_argument", is_pattern
    if basename in COMMON_PATH_BASENAMES or basename in LOCK_BASENAMES:
        return True, "common_basename", is_pattern
    if suffix in KNOWN_PATH_EXTENSIONS and not value.startswith("-"):
        return True, "extension", is_pattern
    if ("\\" in value or "/" in value) and not value.startswith(("-", "$", "${")):
        if re.match(r"^[A-Za-z0-9_.@+~-]+(?:[\\/][A-Za-z0-9_.@+~*?\[\]{}-]+)+[\\/]?$", value):
            return True, "slash_path", is_pattern
    return False, "excluded", is_pattern


def canonical_shell_path(value: str, cwd: str | None) -> str | None:
    if value.startswith("/") and not value.startswith("//"):
        return "posix:" + posixpath.normpath(value)
    return canonical_path(value, cwd)


def parse_shell_command(command: str, cwd: str | None, home: str) -> tuple[str, tuple[ShellMention, ...]]:
    has_write = bool(SHELL_WRITE_RE.search(command))
    has_read = bool(SHELL_READ_RE.search(command))
    if has_write and has_read:
        intent = "read_write"
    elif has_write:
        intent = "write"
    elif has_read:
        intent = "read"
    else:
        intent = "ambiguous"

    raw_tokens = TOKEN_RE.findall(command)
    mentions: list[ShellMention] = []
    seen: set[tuple[str | None, str, bool]] = set()
    force_next = False
    opaque_next = False
    for raw in raw_tokens:
        token = clean_shell_token(raw)
        if opaque_next:
            opaque_next = False
            force_next = False
            continue
        if not token:
            force_next = False
            continue
        if token.casefold() in {"-c", "-e", "--eval", "--encodedcommand", "-encodedcommand", "-command"}:
            opaque_next = True
            force_next = False
            continue
        assignment = ASSIGNMENT_PATH_RE.match(token)
        if assignment:
            token = assignment.group(1)
            forced = True
        else:
            forced = force_next
        if PATH_FLAG_RE.match(token):
            force_next = True
            continue
        force_next = False
        if token.startswith(("2>", "1>")):
            token = token[2:]
            forced = True
        elif token.startswith(">>"):
            token = token[2:]
            forced = True
        elif token.startswith(">"):
            token = token[1:]
            forced = True
        token = strip_line_suffix(clean_shell_token(token))
        if ENV_PREFIX_RE.match(token):
            symbolic_kind = "symbolic_home_" + hashlib.sha256(
                token.encode("utf-8", errors="surrogatepass")
            ).hexdigest()[:8]
            key = (None, symbolic_kind, any(char in token for char in "*?[]{}"))
            if key not in seen:
                seen.add(key)
                mentions.append(ShellMention(path=None, kind=symbolic_kind, is_pattern=key[2]))
            continue
        ok, kind, is_pattern = looks_like_path(token, forced=forced)
        if not ok:
            continue
        resolved = None if is_pattern else canonical_shell_path(token, cwd)
        key = (resolved, kind, is_pattern)
        if key in seen:
            continue
        seen.add(key)
        mentions.append(ShellMention(path=resolved, kind=kind, is_pattern=is_pattern))
    return intent, tuple(mentions)


def build_operation_candidate(
    call: Call,
    record: Mapping[str, Any],
    block: Mapping[str, Any],
    top_result: Any,
    diagnostics: collections.Counter[str],
) -> OperationCandidate:
    result_ts = parse_timestamp(record.get("timestamp"))
    if result_ts is None:
        result_ts = call.call_ts
        diagnostics["result_timestamp_fallbacks"] += 1
    success = block.get("is_error") is not True
    common = dict(
        call=call,
        result_ts=result_ts,
        success=success,
        path=None,
        read_start=None,
        read_num_lines=None,
        read_content_bytes=None,
        read_content_line_match=None,
        hunks=None,
        raw_hunks=None,
        patch_sha256=None,
        patch_applies_to_original=None,
        original_file_bytes=None,
        original_file_present=False,
        original_file_key_present=False,
        write_result_content_bytes=None,
        write_create_without_patch=False,
    )
    diagnostics[f"result_occurrences_{call.source_kind}_{call.tool}"] += 1
    diagnostics[f"successful_result_occurrences_{call.source_kind}_{call.tool}"] += int(success)
    if not success:
        return OperationCandidate(metadata_status="error", **common)

    if call.tool in CORE_READ_TOOLS | ADJACENT_READ_TOOLS:
        file_result = top_result.get("file") if isinstance(top_result, Mapping) else None
        if not isinstance(file_result, Mapping):
            return OperationCandidate(metadata_status="missing_result_file", **common)
        path = canonical_path(file_result.get("filePath"), call.cwd)
        start = file_result.get("startLine")
        count = file_result.get("numLines")
        valid_range = (
            not isinstance(start, bool)
            and not isinstance(count, bool)
            and isinstance(start, int)
            and isinstance(count, int)
            and start >= 1
            and count >= 1
        )
        content = file_result.get("content")
        content_bytes = None
        content_match = None
        if isinstance(content, str):
            content_bytes = len(content.encode("utf-8", errors="surrogatepass"))
            # The observed tool metadata treats a terminal separator as an
            # additional empty line. Keep that convention only as a diagnostic;
            # byte distributions do not depend on line-count agreement.
            logical_count = len(content.splitlines()) + int(content.endswith(("\n", "\r")))
            content_match = logical_count == count if valid_range else None
            diagnostics["read_content_line_mismatches"] += int(content_match is False)
        payload = {
            **common,
            "path": path,
            "read_start": start if valid_range else None,
            "read_num_lines": count if valid_range else None,
            "read_content_bytes": content_bytes,
            "read_content_line_match": content_match,
            "metadata_status": (
                "exact_read" if path is not None and valid_range else
                "invalid_read_range" if not valid_range else
                "read_path_unusable"
            ),
        }
        return OperationCandidate(**payload)

    if not isinstance(top_result, Mapping):
        return OperationCandidate(metadata_status="missing_tool_use_result", **common)
    path = canonical_path(top_result.get("filePath"), call.cwd)
    raw_patch = top_result.get("structuredPatch", object())
    original = top_result.get("originalFile", object())
    original_key_present = "originalFile" in top_result
    original_present = isinstance(original, str)
    original_bytes = (
        len(original.encode("utf-8", errors="surrogatepass")) if original_present else None
    )
    parsed_hunks: tuple[HunkMetrics, ...] | None = None
    parsed_raw_hunks: tuple[RawHunkMetrics, ...] | None = None
    patch_digest = None
    patch_applies: bool | None = None
    if isinstance(raw_patch, list):
        try:
            changes = parse_structured_patch(raw_patch)
            parsed_hunks = tuple(hunk_metrics(change) for change in changes)
            parsed_raw_hunks = raw_hunk_metrics(
                raw_patch, original if isinstance(original, str) else None
            )
            patch_applies = (
                validate_patch_against_original(original, changes)
                if isinstance(original, str)
                else None
            )
            patch_digest = hashlib.sha256(
                json.dumps(raw_patch, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8", errors="surrogatepass"
                )
            ).hexdigest()
        except (TypeError, ValueError):
            diagnostics["invalid_structured_patch_occurrences"] += 1
    result_content = top_result.get("content")
    result_content_bytes = (
        len(result_content.encode("utf-8", errors="surrogatepass"))
        if isinstance(result_content, str)
        else None
    )
    full_create = (
        call.tool == "Write"
        and parsed_hunks == ()
        and original is None
        and result_content_bytes is not None
    )
    status = "exact_write"
    if path is None:
        status = "write_path_unusable"
    elif not isinstance(raw_patch, list):
        status = "missing_structured_patch"
    elif parsed_hunks is None:
        status = "invalid_structured_patch"
    elif not parsed_hunks:
        status = "write_create_without_patch" if full_create else "empty_patch"
    elif not original_present:
        status = "patch_without_string_preimage"
    elif patch_applies is False:
        status = "patch_preimage_mismatch"
    payload = {
        **common,
        "path": path,
        "hunks": parsed_hunks,
        "raw_hunks": parsed_raw_hunks,
        "patch_sha256": patch_digest,
        "patch_applies_to_original": patch_applies,
        "original_file_bytes": original_bytes,
        "original_file_present": original_present,
        "original_file_key_present": original_key_present,
        "write_result_content_bytes": result_content_bytes,
        "write_create_without_patch": full_create,
        "metadata_status": status,
    }
    return OperationCandidate(**payload)


def choose_identity(
    calls: Sequence[Call], diagnostics: collections.Counter[str], prefix: str
) -> tuple[str, tuple[str, ...]] | None:
    explicit = {call.actor for call in calls if call.explicit_actor}
    sessions = tuple(sorted({call.session for call in calls}))
    if len(explicit) > 1:
        diagnostics[f"{prefix}_explicit_actor_conflicts"] += 1
        return None
    if explicit:
        return next(iter(explicit)), sessions
    fallback = {call.session for call in calls}
    if len(fallback) != 1:
        diagnostics[f"{prefix}_session_identity_conflicts"] += 1
        return None
    return next(iter(fallback)), sessions


def merge_call_group(
    tool_id: str, calls: Sequence[Call], diagnostics: collections.Counter[str]
) -> MergedCall | None:
    tools = {call.tool for call in calls}
    times = {call.call_ts for call in calls}
    if len(tools) != 1 or len(times) != 1:
        diagnostics["call_dedup_tool_or_time_conflicts"] += 1
        return None
    identity = choose_identity(calls, diagnostics, "call_dedup")
    if identity is None:
        return None
    actor, sessions = identity
    if len(calls) > 1:
        diagnostics["duplicated_tool_call_ids"] += 1
        diagnostics["duplicate_tool_call_occurrences"] += len(calls) - 1
    return MergedCall(
        tool_id=tool_id,
        tool=next(iter(tools)),
        actor=actor,
        sessions=sessions,
        call_ts=next(iter(times)),
        repo_keys=tuple(sorted({call.repo_key for call in calls})),
    )


def merge_operation_group(
    tool_id: str,
    candidates: Sequence[OperationCandidate],
    diagnostics: collections.Counter[str],
) -> Operation | None:
    calls = [candidate.call for candidate in candidates]
    tools = {call.tool for call in calls}
    call_times = {call.call_ts for call in calls}
    result_times = {candidate.result_ts for candidate in candidates}
    successes = {candidate.success for candidate in candidates}
    if len(tools) != 1 or len(call_times) != 1 or len(result_times) != 1:
        diagnostics["operation_dedup_tool_or_time_conflicts"] += 1
        return None
    if len(successes) != 1:
        diagnostics["operation_dedup_result_status_conflicts"] += 1
        return None
    identity = choose_identity(calls, diagnostics, "operation_dedup")
    if identity is None:
        return None
    actor, sessions = identity
    paths = {candidate.path for candidate in candidates if candidate.path is not None}
    if len(paths) > 1:
        diagnostics["operation_dedup_path_conflicts"] += 1
        return None

    def rank(candidate: OperationCandidate) -> tuple[int, int, int]:
        return (
            int(candidate.hunks is not None or candidate.read_num_lines is not None),
            int(candidate.original_file_present),
            int(candidate.path is not None),
        )

    best_rank = max(rank(candidate) for candidate in candidates)
    best_candidates = [candidate for candidate in candidates if rank(candidate) == best_rank]
    signatures = {
        (
            candidate.path,
            candidate.read_start,
            candidate.read_num_lines,
            candidate.read_content_bytes,
            candidate.read_content_line_match,
            candidate.patch_sha256,
            candidate.patch_applies_to_original,
            candidate.original_file_bytes,
            candidate.original_file_present,
            candidate.original_file_key_present,
            candidate.write_result_content_bytes,
            candidate.write_create_without_patch,
            candidate.hunks,
            candidate.raw_hunks,
        )
        for candidate in best_candidates
    }
    if len(signatures) > 1:
        diagnostics["operation_dedup_metadata_conflicts"] += 1
        return None
    best = best_candidates[0]
    if len(candidates) > 1:
        diagnostics["duplicated_result_tool_use_ids"] += 1
        diagnostics["duplicate_result_occurrences"] += len(candidates) - 1
    return Operation(
        tool_id=tool_id,
        tool=next(iter(tools)),
        actor=actor,
        sessions=sessions,
        call_ts=next(iter(call_times)),
        result_ts=next(iter(result_times)),
        repo_keys=tuple(sorted({call.repo_key for call in calls})),
        success=next(iter(successes)),
        path=next(iter(paths)) if paths else None,
        read_start=best.read_start,
        read_num_lines=best.read_num_lines,
        read_content_bytes=best.read_content_bytes,
        read_content_line_match=best.read_content_line_match,
        hunks=best.hunks,
        raw_hunks=best.raw_hunks,
        patch_applies_to_original=best.patch_applies_to_original,
        original_file_bytes=best.original_file_bytes,
        original_file_present=best.original_file_present,
        original_file_key_present=best.original_file_key_present,
        write_result_content_bytes=best.write_result_content_bytes,
        write_create_without_patch=best.write_create_without_patch,
        metadata_status=best.metadata_status,
    )


def merge_shell_group(
    tool_id: str,
    candidates: Sequence[ShellCandidate],
    diagnostics: collections.Counter[str],
) -> ShellEvent | None:
    calls = [candidate.call for candidate in candidates]
    tools = {call.tool for call in calls}
    times = {call.call_ts for call in calls}
    command_hashes = {candidate.command_sha256 for candidate in candidates}
    intents = {candidate.intent for candidate in candidates}
    if len(tools) != 1 or len(times) != 1 or len(command_hashes) != 1 or len(intents) != 1:
        diagnostics["shell_dedup_command_or_time_conflicts"] += 1
        return None
    identity = choose_identity(calls, diagnostics, "shell_dedup")
    if identity is None:
        return None
    actor, sessions = identity
    mention_signatures = {
        tuple(
            sorted(
                ((mention.path, mention.kind, mention.is_pattern) for mention in candidate.mentions),
                key=lambda item: (item[0] is None, item[0] or "", item[1], item[2]),
            )
        )
        for candidate in candidates
    }
    if len(mention_signatures) != 1:
        diagnostics["shell_dedup_parser_conflicts"] += 1
        return None
    chosen = min(candidates, key=lambda item: (item.call.source_ordinal, item.call.source_line))
    if len(candidates) > 1:
        diagnostics["duplicated_shell_tool_use_ids"] += 1
        diagnostics["duplicate_shell_occurrences"] += len(candidates) - 1
    return ShellEvent(
        tool_id=tool_id,
        tool=next(iter(tools)),
        actor=actor,
        sessions=sessions,
        timestamp=next(iter(times)),
        repo_keys=tuple(sorted({call.repo_key for call in calls})),
        command_sha256=next(iter(command_hashes)),
        intent=next(iter(intents)),
        mentions=chosen.mentions,
        source_ordinal=chosen.call.source_ordinal,
        source_line=chosen.call.source_line,
    )


def update_span(spans: dict[str, list[float]], actor: str, timestamp: float) -> None:
    current = spans.get(actor)
    if current is None:
        spans[actor] = [timestamp, timestamp]
    else:
        current[0] = min(current[0], timestamp)
        current[1] = max(current[1], timestamp)


def prefix_sha256(path: Path, byte_limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    bytes_read = 0
    with path.open("rb") as handle:
        remaining = byte_limit
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            bytes_read += len(chunk)
            remaining -= len(chunk)
    return digest.hexdigest(), bytes_read


def resolve_frozen_snapshots(
    corpus: Path,
    manifest: Mapping[str, Any],
    *,
    progress_every: int = 250,
) -> tuple[list[tuple[Path, int]], dict[str, int]]:
    """Map a path-redacted manifest back to an ordered live tree by prefix hash.

    Current-only additions may appear anywhere in sort order and are skipped.
    A missing, reordered, truncated, or prefix-mutated frozen file fails closed.
    """

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("frozen manifest has no file entries")
    expected_count = manifest.get("file_count")
    expected_bytes = manifest.get("byte_count")
    expected_hash = manifest.get("frozen_prefix_sha256")
    if expected_count != len(entries):
        raise ValueError("frozen manifest file_count mismatch")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("frozen manifest SHA-256 is invalid")
    if [entry.get("ordinal") for entry in entries] != list(range(1, len(entries) + 1)):
        raise ValueError("frozen manifest ordinals are not contiguous")
    if sum(int(entry.get("byte_length", -1)) for entry in entries) != expected_bytes:
        raise ValueError("frozen manifest byte_count mismatch")

    current_paths = sorted(corpus.rglob("*.jsonl"), key=lambda item: str(item).casefold())
    snapshots: list[tuple[Path, int]] = []
    current_index = 0
    candidates_hashed = 0
    for entry in entries:
        byte_limit = entry.get("byte_length")
        prefix_hash = entry.get("prefix_sha256")
        if (
            isinstance(byte_limit, bool)
            or not isinstance(byte_limit, int)
            or byte_limit < 0
            or not isinstance(prefix_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", prefix_hash)
        ):
            raise ValueError(f"invalid frozen manifest entry at ordinal {entry.get('ordinal')}")
        matched = None
        while current_index < len(current_paths):
            candidate = current_paths[current_index]
            current_index += 1
            try:
                if candidate.stat().st_size < byte_limit:
                    continue
                candidate_hash, bytes_read = prefix_sha256(candidate, byte_limit)
            except OSError:
                continue
            candidates_hashed += 1
            if bytes_read == byte_limit and candidate_hash == prefix_hash:
                matched = candidate
                break
        if matched is None:
            raise ValueError(
                "could not map frozen manifest ordinal "
                f"{entry.get('ordinal')} to the current sorted corpus"
            )
        snapshots.append((matched, byte_limit))
        if progress_every and (
            len(snapshots) % progress_every == 0 or len(snapshots) == len(entries)
        ):
            print(
                f"resolved {len(snapshots):,}/{len(entries):,} frozen prefixes; "
                f"hashed {candidates_hashed:,} current candidates",
                flush=True,
            )
    return snapshots, {
        "live_jsonl_files_at_manifest_resolution": len(current_paths),
        "frozen_manifest_files_resolved": len(snapshots),
        "live_files_outside_frozen_manifest": len(current_paths) - len(snapshots),
        "manifest_resolution_candidates_hashed": candidates_hashed,
    }


def scan_corpus(
    corpus: Path,
    *,
    progress_every: int = 250,
    frozen_manifest: Mapping[str, Any] | None = None,
) -> tuple[
    list[Operation],
    list[MergedCall],
    list[ShellEvent],
    dict[str, dict[str, list[float]]],
    dict[str, Any],
    list[dict[str, Any]],
]:
    diagnostics: collections.Counter[str] = collections.Counter()
    if frozen_manifest is None:
        paths = sorted(corpus.rglob("*.jsonl"), key=lambda item: str(item).casefold())
        snapshots: list[tuple[Path, int]] = []
        for path in paths:
            try:
                snapshots.append((path, path.stat().st_size))
            except OSError:
                snapshots.append((path, -1))
        snapshot_epoch = dt.datetime.now(dt.timezone.utc).timestamp()
        expected_frozen_hash = None
        snapshot_rule = "enumerate paths and byte lengths once, then read exactly those byte prefixes"
    else:
        snapshots, resolution_diagnostics = resolve_frozen_snapshots(
            corpus, frozen_manifest, progress_every=progress_every
        )
        diagnostics.update(resolution_diagnostics)
        parsed_snapshot = parse_timestamp(frozen_manifest.get("snapshot_utc"))
        if parsed_snapshot is None:
            raise ValueError("frozen manifest snapshot_utc is invalid")
        snapshot_epoch = parsed_snapshot
        expected_frozen_hash = frozen_manifest.get("frozen_prefix_sha256")
        snapshot_rule = (
            "reuse an existing path-redacted manifest by ordered prefix-hash mapping, "
            "then read exactly its recorded byte prefixes"
        )
    global_operations: dict[str, list[OperationCandidate]] = collections.defaultdict(list)
    global_calls: dict[str, list[Call]] = collections.defaultdict(list)
    global_shell: dict[str, list[ShellCandidate]] = collections.defaultdict(list)
    actor_spans: dict[str, list[float]] = {}
    raw_session_spans: dict[str, list[float]] = {}
    raw_sessions: set[str] = set()
    global_digest = hashlib.sha256()
    manifest_files: list[dict[str, Any]] = []
    home = normalize_windows_path(str(Path.home()))

    for ordinal, (path, byte_limit) in enumerate(snapshots, 1):
        file_digest = hashlib.sha256()
        bytes_read = 0
        repo_key = repository_bucket(path, corpus)
        kind = source_kind(path)
        layout_parent_session, layout_agent = subagent_layout_hints(path, corpus)
        local_calls: dict[str, list[Call]] = collections.defaultdict(list)
        global_digest.update(str(ordinal).encode("ascii"))
        global_digest.update(b"\0" + str(byte_limit).encode("ascii") + b"\0")
        if byte_limit < 0:
            diagnostics["files_stat_failed"] += 1
            manifest_files.append(
                {"ordinal": ordinal, "byte_length": byte_limit, "bytes_read": 0, "prefix_sha256": file_digest.hexdigest()}
            )
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
                    try:
                        record = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        diagnostics["malformed_jsonl_lines"] += 1
                        continue
                    if not isinstance(record, dict):
                        continue
                    actor, explicit_actor, session = actor_for(record)
                    timestamp = parse_timestamp(record.get("timestamp"))
                    if session is not None:
                        raw_sessions.add(session)
                        if timestamp is not None:
                            update_span(raw_session_spans, session, timestamp)
                    if actor is not None and timestamp is not None:
                        update_span(actor_spans, actor, timestamp)
                    if layout_parent_session is not None and session is not None:
                        diagnostics["subagent_records_with_session"] += 1
                        diagnostics["subagent_parent_session_matches"] += int(
                            session == layout_parent_session
                        )
                        diagnostics["subagent_parent_session_mismatches"] += int(
                            session != layout_parent_session
                        )
                    if layout_agent is not None and explicit_actor:
                        diagnostics["subagent_records_with_explicit_agent"] += 1
                        diagnostics["subagent_filename_agent_matches"] += int(actor == layout_agent)
                        diagnostics["subagent_filename_agent_mismatches"] += int(actor != layout_agent)

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
                                or actor is None
                                or session is None
                                or timestamp is None
                            ):
                                diagnostics["unusable_tool_use_blocks"] += 1
                                continue
                            call = Call(
                                tool_id=tool_id,
                                tool=tool,
                                actor=actor,
                                explicit_actor=explicit_actor,
                                session=session,
                                call_ts=timestamp,
                                cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                                call_uuid=record.get("uuid") if isinstance(record.get("uuid"), str) else None,
                                repo_key=repo_key,
                                source_kind=kind,
                                source_ordinal=ordinal,
                                source_line=line_number,
                            )
                            global_calls[tool_id].append(call)
                            diagnostics[f"tool_call_occurrences_{tool}"] += 1
                            diagnostics[f"tool_call_occurrences_source_{kind}"] += 1
                            if tool in STRUCTURED_TOOLS:
                                local_calls[tool_id].append(call)
                                diagnostics[f"structured_call_occurrences_{kind}_{tool}"] += 1
                            if tool in SHELL_TOOLS:
                                command = input_data.get("command") or input_data.get("cmd")
                                if isinstance(command, str):
                                    intent, mentions = parse_shell_command(command, call.cwd, home)
                                    command_hash = hashlib.sha256(
                                        command.encode("utf-8", errors="surrogatepass")
                                    ).hexdigest()
                                    global_shell[tool_id].append(
                                        ShellCandidate(call, command_hash, intent, mentions)
                                    )
                                    diagnostics["shell_command_string_occurrences"] += 1
                                    diagnostics[f"shell_command_occurrences_{tool}"] += 1
                                else:
                                    diagnostics["shell_calls_without_command_string"] += 1

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
                            diagnostics["source_local_results_without_structured_call"] += 1
                            continue
                        exact_parent = [call for call in choices if call.call_uuid == parent_uuid]
                        call = exact_parent[-1] if exact_parent else choices[-1]
                        if not exact_parent:
                            diagnostics["results_without_parent_uuid_match"] += 1
                        candidate = build_operation_candidate(
                            call, record, block, top_result, diagnostics
                        )
                        global_operations[tool_id].append(candidate)
                        diagnostics[
                            f"metadata_{call.source_kind}_{call.tool}_{candidate.metadata_status}"
                        ] += 1
                        diagnostics[
                            f"original_file_key_{call.source_kind}_{call.tool}"
                        ] += int(candidate.original_file_key_present)
                        diagnostics[
                            f"original_file_string_{call.source_kind}_{call.tool}"
                        ] += int(candidate.original_file_present)
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
                f"scanned {ordinal:,}/{len(snapshots):,} transcript prefixes; "
                f"{len(global_calls):,} tool IDs, {len(global_operations):,} result-linked IDs",
                flush=True,
            )

    calls = [
        merged
        for tool_id, group in global_calls.items()
        if (merged := merge_call_group(tool_id, group, diagnostics)) is not None
    ]
    operations = [
        merged
        for tool_id, group in global_operations.items()
        if (merged := merge_operation_group(tool_id, group, diagnostics)) is not None
    ]
    shell_events = [
        merged
        for tool_id, group in global_shell.items()
        if (merged := merge_shell_group(tool_id, group, diagnostics)) is not None
    ]
    calls.sort(key=lambda item: (item.call_ts, item.tool_id))
    operations.sort(key=lambda item: (item.result_ts, item.call_ts, item.tool_id))
    shell_events.sort(key=lambda item: (item.timestamp, item.tool_id))

    try:
        current_paths = set(corpus.rglob("*.jsonl"))
        added_after_snapshot = len(current_paths - {path for path, _ in snapshots})
    except OSError:
        added_after_snapshot = -1
    diagnostics["deduplicated_tool_calls"] = len(calls)
    diagnostics["deduplicated_operations"] = len(operations)
    diagnostics["deduplicated_shell_commands"] = len(shell_events)
    diagnostics["files_added_after_snapshot"] = added_after_snapshot
    observed_frozen_hash = global_digest.hexdigest()
    if expected_frozen_hash is not None and observed_frozen_hash != expected_frozen_hash:
        raise ValueError(
            f"reused frozen-prefix SHA mismatch: {observed_frozen_hash} != {expected_frozen_hash}"
        )
    metadata = {
        "snapshot_utc": iso_utc(snapshot_epoch),
        "corpus_root": "<redacted-current-user>/.claude/projects",
        "corpus_file_count": len(snapshots),
        "corpus_bytes": sum(max(size, 0) for _, size in snapshots),
        "corpus_snapshot_sha256": observed_frozen_hash,
        "identity_rule": "agentId when present, otherwise sessionId",
        "raw_session_id_count": len(raw_sessions),
        "logical_actor_count_with_timestamp": len(actor_spans),
        "path_rule": "result metadata only for structured operations; best-effort parsed command paths reported separately",
        "repository_proxy_rule": "first directory below .claude/projects (Claude project bucket), not a verified VCS root",
        "snapshot_rule": snapshot_rule,
        "diagnostics": dict(sorted(diagnostics.items())),
    }
    return (
        operations,
        calls,
        shell_events,
        {"actor": actor_spans, "session": raw_session_spans},
        metadata,
        manifest_files,
    )


def classify_file_type(path: str) -> str:
    basename = ntpath.basename(path).casefold()
    suffix = ntpath.splitext(basename)[1]
    if suffix in BINARY_EXTENSIONS:
        return "binary"
    if basename in LOCK_BASENAMES or suffix == ".lock":
        return "lock"
    if suffix in {".md", ".mdx", ".rst", ".adoc"}:
        return "markdown"
    if suffix in {".json", ".jsonc", ".json5", ".jsonl", ".ndjson"}:
        return "json"
    if suffix in CONFIG_EXTENSIONS or basename.startswith(".env"):
        return "config"
    if suffix in SOURCE_EXTENSIONS:
        return "source"
    return "other"


def classify_hot_path(path: str) -> str:
    lowered_parts = [part.casefold() for part in re.split(r"[\\/]", path) if part]
    suffix = ntpath.splitext(ntpath.basename(path).casefold())[1]
    file_type = classify_file_type(path)
    if suffix in LOG_EXTENSIONS or "logs" in lowered_parts or "log" in lowered_parts:
        return "log"
    if file_type == "source":
        return "source"
    if file_type in {"config", "json", "lock"}:
        return "config"
    if file_type == "markdown" or suffix in {".txt", ".rst", ".adoc"}:
        return "doc"
    return "other"


def display_path(path: str) -> tuple[str, bool]:
    normalized = path.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    sensitive_index = None
    for index, part in enumerate(parts):
        suffix = ntpath.splitext(part.casefold())[1]
        if SENSITIVE_COMPONENT_RE.match(part) or suffix in SENSITIVE_SUFFIXES:
            sensitive_index = index
            break
    redacted = sensitive_index is not None
    if sensitive_index is not None:
        parts = parts[:sensitive_index] + ["<credential-path-redacted>"]
    rendered = "\\".join(parts)
    rendered = re.sub(
        r"^[A-Za-z]:\\users\\[^\\]+",
        "<home>",
        rendered,
        flags=re.IGNORECASE,
    )
    username = Path.home().name
    if username:
        rendered = re.sub(re.escape(username), "<user>", rendered, flags=re.IGNORECASE)
    if normalized.startswith("\\\\") and not rendered.startswith("\\\\"):
        rendered = "\\\\" + rendered
    return rendered, redacted


def current_git_root(path: str, cache: dict[str, str | None]) -> str | None:
    """Resolve an extant target's current git top-level without invoking git.

    This is deliberately a time-varying current-filesystem lookup.  Historical
    or deleted paths remain unknown rather than being assigned to the Claude
    project bucket.
    """

    if path in cache:
        return cache[path]
    candidate = Path(path)
    start = candidate if candidate.is_dir() else candidate.parent
    resolved = None
    for ancestor in (start, *start.parents):
        try:
            marker = ancestor / ".git"
            if marker.exists():
                resolved = ntpath.normcase(ntpath.normpath(str(ancestor)))
                break
        except OSError:
            break
    cache[path] = resolved
    return resolved


def utc_day(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).date().isoformat()


def iso_week(epoch: float) -> str:
    date = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).date()
    year, week, _ = date.isocalendar()
    return f"{year:04d}-W{week:02d}"


def week_monday(epoch: float) -> dt.date:
    date = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).date()
    return date - dt.timedelta(days=date.weekday())


def categorical_counts(paths: Sequence[str]) -> dict[str, Any]:
    counts = collections.Counter(classify_file_type(path) for path in paths)
    denominator = len(paths)
    distinct_paths = set(paths)
    distinct_counts = collections.Counter(classify_file_type(path) for path in distinct_paths)
    distinct_denominator = len(distinct_paths)
    categories = ["source", "config", "markdown", "json", "lock", "binary", "other"]
    return {
        "denominator": denominator,
        "categories": {
            category: {
                "count": counts.get(category, 0),
                "percent": 100.0 * counts.get(category, 0) / denominator if denominator else None,
                "distinct_file_count": distinct_counts.get(category, 0),
                "distinct_file_percent": (
                    100.0 * distinct_counts.get(category, 0) / distinct_denominator
                    if distinct_denominator else None
                ),
            }
            for category in categories
        },
        "distinct_file_denominator": distinct_denominator,
    }


def concurrency_metrics(events: Iterable[tuple[float, str, tuple[str, ...]]]) -> dict[str, Any]:
    by_minute: dict[int, set[str]] = collections.defaultdict(set)
    by_repo_minute: dict[tuple[str, int], set[str]] = collections.defaultdict(set)
    repo_attribution_conflicts = 0
    for timestamp, actor, repo_keys in events:
        minute = int(timestamp // 60)
        by_minute[minute].add(actor)
        if len(repo_keys) == 1:
            by_repo_minute[(repo_keys[0], minute)].add(actor)
        else:
            repo_attribution_conflicts += 1
    global_values = [len(actors) for actors in by_minute.values()]
    repo_values = [len(actors) for actors in by_repo_minute.values()]
    max_same_repo_by_minute: dict[int, int] = collections.defaultdict(int)
    for (_, minute), actors in by_repo_minute.items():
        max_same_repo_by_minute[minute] = max(max_same_repo_by_minute[minute], len(actors))
    peak_minute = max(by_minute, key=lambda key: len(by_minute[key])) if by_minute else None
    peak_repo = max(by_repo_minute, key=lambda key: len(by_repo_minute[key])) if by_repo_minute else None
    return {
        "simultaneous_sessions_per_active_utc_minute": nearest_rank(global_values),
        "active_utc_minute_denominator": len(by_minute),
        "peak_utc_minute": iso_utc(peak_minute * 60) if peak_minute is not None else None,
        "simultaneous_sessions_per_active_repo_minute": nearest_rank(repo_values),
        "active_repo_minute_denominator": len(by_repo_minute),
        "maximum_same_repo_sessions_per_active_global_minute": nearest_rank(
            max_same_repo_by_minute.values()
        ),
        "peak_repo_minute_utc": iso_utc(peak_repo[1] * 60) if peak_repo is not None else None,
        "events_excluded_from_repo_proxy_for_multiple_buckets": repo_attribution_conflicts,
    }


def paired_concurrency_delta_metrics(
    base_events: Sequence[tuple[float, str, tuple[str, ...]]],
    expanded_events: Sequence[tuple[float, str, tuple[str, ...]]],
) -> dict[str, Any]:
    """Actor-count deltas on the expanded population's minute support."""

    def actor_sets(
        events: Sequence[tuple[float, str, tuple[str, ...]]],
    ) -> tuple[dict[int, set[str]], dict[tuple[str, int], set[str]]]:
        by_minute: dict[int, set[str]] = collections.defaultdict(set)
        by_repo_minute: dict[tuple[str, int], set[str]] = collections.defaultdict(set)
        for timestamp, actor, repo_keys in events:
            minute = int(timestamp // 60)
            by_minute[minute].add(actor)
            if len(repo_keys) == 1:
                by_repo_minute[(repo_keys[0], minute)].add(actor)
        return by_minute, by_repo_minute

    base_minute, base_repo_minute = actor_sets(base_events)
    expanded_minute, expanded_repo_minute = actor_sets(expanded_events)
    global_deltas = [
        len(actors) - len(base_minute.get(minute, set()))
        for minute, actors in expanded_minute.items()
    ]
    repo_deltas = [
        len(actors) - len(base_repo_minute.get(key, set()))
        for key, actors in expanded_repo_minute.items()
    ]
    if any(value < 0 for value in itertools.chain(global_deltas, repo_deltas)):
        raise ValueError("expanded concurrency population does not contain its base")
    return {
        "global_actor_delta_per_expanded_active_utc_minute": nearest_rank(global_deltas),
        "expanded_active_utc_minute_denominator": len(global_deltas),
        "global_minutes_with_positive_actor_delta": ratio(
            sum(value > 0 for value in global_deltas), len(global_deltas)
        ),
        "same_project_actor_delta_per_expanded_active_repo_minute": nearest_rank(repo_deltas),
        "expanded_active_repo_minute_denominator": len(repo_deltas),
        "repo_minutes_with_positive_actor_delta": ratio(
            sum(value > 0 for value in repo_deltas), len(repo_deltas)
        ),
        "support": "structured-plus-shell active minute/repository-minute cells; structured count is zero where absent",
    }


def first_following_delta(reads: Sequence[float], writes: Sequence[float]) -> float | None:
    if not reads or not writes:
        return None
    first_read = min(reads)
    following = [timestamp for timestamp in writes if timestamp >= first_read]
    return min(following) - first_read if following else None


def rolling_distinct_max(
    events: Sequence[tuple[float, str]], window_seconds: float
) -> int:
    """Maximum distinct actors in the half-open rolling interval (t-W, t]."""

    queue: collections.deque[tuple[float, str]] = collections.deque()
    counts: collections.Counter[str] = collections.Counter()
    maximum = 0
    for timestamp, actor in sorted(events):
        boundary = timestamp - window_seconds
        while queue and queue[0][0] <= boundary:
            _, expired_actor = queue.popleft()
            counts[expired_actor] -= 1
            if counts[expired_actor] <= 0:
                del counts[expired_actor]
        queue.append((timestamp, actor))
        counts[actor] += 1
        maximum = max(maximum, len(counts))
    return maximum


def build_metrics(
    operations: Sequence[Operation],
    calls: Sequence[MergedCall],
    shell_events: Sequence[ShellEvent],
    spans: Mapping[str, Mapping[str, Sequence[float]]],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    actor_spans = spans.get("actor", {})
    raw_session_spans = spans.get("session", {})
    successful_core = [operation for operation in operations if operation.tool in CORE_TOOLS and operation.success]
    reads = [operation for operation in successful_core if operation.tool == "Read"]
    writes = [operation for operation in successful_core if operation.tool in CORE_WRITE_TOOLS]
    localized_reads = [
        operation for operation in reads
        if operation.path is not None and operation.read_start is not None and operation.read_num_lines is not None
    ]
    localized_writes = [operation for operation in writes if operation.path is not None]
    patch_writes = [
        operation for operation in localized_writes
        if operation.hunks is not None and len(operation.hunks) > 0
    ]
    region_writes = [
        operation for operation in patch_writes
        if operation.original_file_present and operation.patch_applies_to_original is True
    ]

    per_session_hour: collections.Counter[tuple[str, int]] = collections.Counter()
    per_global_hour: collections.Counter[int] = collections.Counter()
    per_tool: collections.Counter[str] = collections.Counter()
    tool_session_hours: dict[str, collections.Counter[tuple[str, int]]] = collections.defaultdict(collections.Counter)
    family_session_hours: dict[str, collections.Counter[tuple[str, int]]] = collections.defaultdict(collections.Counter)
    for operation in successful_core:
        hour = int(operation.result_ts // 3600)
        per_session_hour[(operation.actor, hour)] += 1
        per_global_hour[hour] += 1
        per_tool[operation.tool] += 1
        tool_session_hours[operation.tool][(operation.actor, hour)] += 1
        family = "read" if operation.tool in CORE_READ_TOOLS else "write"
        family_session_hours[family][(operation.actor, hour)] += 1
    core_calls = [call for call in calls if call.tool in CORE_TOOLS]
    failed_core = [operation for operation in operations if operation.tool in CORE_TOOLS and not operation.success]
    event_volume = {
        "deduplicated_structured_calls": len(core_calls),
        "successful_deduplicated_structured_events": len(successful_core),
        "by_tool": dict(sorted(per_tool.items())),
        "failed_result_linked_structured_events": len(failed_core),
        "result_linked_core_operations_including_errors": sum(operation.tool in CORE_TOOLS for operation in operations),
        "active_session_hour_event_count": nearest_rank(per_session_hour.values()),
        "active_session_hour_denominator": len(per_session_hour),
        "active_session_hour_by_tool": {
            tool: nearest_rank(counter.values())
            for tool, counter in sorted(tool_session_hours.items())
        },
        "active_session_hour_by_family": {
            family: nearest_rank(counter.values())
            for family, counter in sorted(family_session_hours.items())
        },
        "aggregate_utc_hour_event_count": nearest_rank(per_global_hour.values()),
        "active_aggregate_utc_hour_denominator": len(per_global_hour),
        "event_timestamp": "successful result completion time",
    }

    read_windows = {
        "successful_read_denominator": len(reads),
        "localized_valid_window_denominator": len(localized_reads),
        "localized_coverage": ratio(len(localized_reads), len(reads)),
        "start_line": nearest_rank(operation.read_start for operation in localized_reads if operation.read_start is not None),
        "num_lines": nearest_rank(operation.read_num_lines for operation in localized_reads if operation.read_num_lines is not None),
        "returned_window_utf8_bytes": nearest_rank(
            operation.read_content_bytes
            for operation in localized_reads
            if operation.read_content_bytes is not None
        ),
        "returned_utf8_bytes_per_line": nearest_rank(
            operation.read_content_bytes / operation.read_num_lines
            for operation in localized_reads
            if operation.read_content_bytes is not None
            and operation.read_num_lines
        ),
        "returned_window_utf8_bytes_metadata_line_count_aligned": nearest_rank(
            operation.read_content_bytes
            for operation in localized_reads
            if operation.read_content_bytes is not None and operation.read_content_line_match is True
        ),
        "returned_content_byte_denominator": sum(
            operation.read_content_bytes is not None
            for operation in localized_reads
        ),
        "returned_content_line_count_matches_metadata": ratio(
            sum(operation.read_content_line_match is True for operation in localized_reads),
            sum(operation.read_content_line_match is not None for operation in localized_reads),
        ),
        "byte_definition": "UTF-8 byte length of every same-result toolUseResult.file.content string; metadata line-count agreement is diagnostic only, not an eligibility gate; not on-disk encoding or line-ending bytes",
    }

    all_hunks = [hunk for operation in region_writes for hunk in operation.hunks or ()]
    all_raw_hunks = [hunk for operation in region_writes for hunk in operation.raw_hunks or ()]
    edit_regions = {
        "successful_edit_write_denominator": len(writes),
        "localized_write_denominator": len(localized_writes),
        "valid_nonempty_patch_write_denominator_before_preimage_validation": len(patch_writes),
        "exact_preimage_validated_patch_write_denominator": len(region_writes),
        "raw_structured_patch_hunk_denominator": len(all_raw_hunks),
        "parsed_change_block_denominator": len(all_hunks),
        "writes_with_original_file_key": ratio(
            sum(operation.original_file_key_present for operation in writes), len(writes)
        ),
        "writes_with_string_original_file": ratio(
            sum(operation.original_file_present for operation in writes), len(writes)
        ),
        "write_creates_without_patch": sum(operation.write_create_without_patch for operation in writes),
        "write_create_result_utf8_bytes": nearest_rank(
            operation.write_result_content_bytes
            for operation in writes
            if operation.write_create_without_patch and operation.write_result_content_bytes is not None
        ),
        "declared_old_lines_per_raw_hunk_including_context": nearest_rank(hunk.old_lines for hunk in all_raw_hunks),
        "declared_new_lines_per_raw_hunk_including_context": nearest_rank(hunk.new_lines for hunk in all_raw_hunks),
        "context_lines_per_raw_hunk": nearest_rank(hunk.context_lines for hunk in all_raw_hunks),
        "transcript_utf8_old_span_bytes_per_raw_hunk": nearest_rank(
            hunk.transcript_utf8_old_span_bytes
            for hunk in all_raw_hunks
            if hunk.transcript_utf8_old_span_bytes is not None
        ),
        "hunks_per_write": nearest_rank(len(operation.hunks or ()) for operation in region_writes),
        "removed_line_count_per_change_block": nearest_rank(hunk.old_lines for hunk in all_hunks),
        "added_line_count_per_change_block": nearest_rank(hunk.new_lines for hunk in all_hunks),
        "claim_line_span_per_change_block": nearest_rank(hunk.claim_lines for hunk in all_hunks),
        "removed_lf_normalized_utf8_bytes_per_change_block": nearest_rank(hunk.old_bytes for hunk in all_hunks),
        "added_lf_normalized_utf8_bytes_per_change_block": nearest_rank(hunk.new_bytes for hunk in all_hunks),
        "claim_lf_normalized_utf8_bytes_per_change_block": nearest_rank(hunk.claim_bytes for hunk in all_hunks),
        "pure_insertion_change_blocks": ratio(sum(hunk.old_lines == 0 for hunk in all_hunks), len(all_hunks)),
        "pure_deletion_change_blocks": ratio(sum(hunk.new_lines == 0 for hunk in all_hunks), len(all_hunks)),
        "aggregate_claim_lines_per_write": nearest_rank(
            sum(hunk.claim_lines for hunk in operation.hunks or ()) for operation in region_writes
        ),
        "aggregate_claim_bytes_per_write": nearest_rank(
            sum(hunk.claim_bytes for hunk in operation.hunks or ()) for operation in region_writes
        ),
        "original_file_utf8_bytes": nearest_rank(
            operation.original_file_bytes for operation in writes if operation.original_file_bytes is not None
        ),
        "claim_definition": "per contiguous +/- change block max(removed, added); pure insertions are zero-width on the old side but claim their added span; byte proxy is LF-normalized UTF-8 max(old,new)",
        "exact_byte_definition": "raw hunk old-side byte span reconstructed from the same originalFile string with its observed separators; transcript Unicode, not verified disk encoding",
    }

    file_day_sessions: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    file_week_sessions: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    file_sessions: dict[str, set[str]] = collections.defaultdict(set)
    file_read_actor_events: dict[str, list[tuple[float, str]]] = collections.defaultdict(list)
    file_read_events: collections.Counter[str] = collections.Counter()
    first_file_read: dict[str, float] = {}
    first_pair_read: dict[tuple[str, str], float] = {}
    for operation in localized_reads:
        assert operation.path is not None
        path = operation.path
        file_day_sessions[(path, utc_day(operation.result_ts))].add(operation.actor)
        file_week_sessions[(path, iso_week(operation.result_ts))].add(operation.actor)
        file_sessions[path].add(operation.actor)
        file_read_actor_events[path].append((operation.result_ts, operation.actor))
        file_read_events[path] += 1
        first_file_read[path] = min(first_file_read.get(path, operation.result_ts), operation.result_ts)
        pair = (path, operation.actor)
        first_pair_read[pair] = min(first_pair_read.get(pair, operation.result_ts), operation.result_ts)

    daily_values = [len(actors) for actors in file_day_sessions.values()]
    weekly_values = [len(actors) for actors in file_week_sessions.values()]
    per_file_peak_day: dict[str, int] = collections.defaultdict(int)
    per_file_peak_week: dict[str, int] = collections.defaultdict(int)
    for (path, _), actors in file_day_sessions.items():
        per_file_peak_day[path] = max(per_file_peak_day[path], len(actors))
    for (path, _), actors in file_week_sessions.items():
        per_file_peak_week[path] = max(per_file_peak_week[path], len(actors))
    rolling_day = {
        path: rolling_distinct_max(events, 86_400)
        for path, events in file_read_actor_events.items()
    }
    rolling_week = {
        path: rolling_distinct_max(events, 604_800)
        for path, events in file_read_actor_events.items()
    }
    hot_paths = []
    ranked_paths = sorted(
        file_sessions,
        key=lambda path: (
            -rolling_week[path], -rolling_day[path],
            -len(file_sessions[path]), -file_read_events[path], path,
        ),
    )[:20]
    redacted_hot_count = 0
    for rank, path in enumerate(ranked_paths, 1):
        shown, redacted = display_path(path)
        redacted_hot_count += int(redacted)
        hot_paths.append(
            {
                "rank": rank,
                "path": shown,
                "path_id": hashlib.sha256(path.encode("utf-8", errors="surrogatepass")).hexdigest()[:16],
                "credential_redacted": redacted,
                "category": classify_hot_path(path),
                "rolling_24h_max_distinct_sessions": rolling_day[path],
                "rolling_7d_max_distinct_sessions": rolling_week[path],
                "peak_distinct_sessions_utc_day": per_file_peak_day[path],
                "peak_distinct_sessions_iso_week": per_file_peak_week[path],
                "all_time_distinct_sessions": len(file_sessions[path]),
                "read_events": file_read_events[path],
            }
        )
    read_multiplicity = {
        "file_utc_day_bucket_denominator": len(file_day_sessions),
        "distinct_sessions_per_file_utc_day": nearest_rank(daily_values),
        "file_iso_week_bucket_denominator": len(file_week_sessions),
        "distinct_sessions_per_file_iso_week": nearest_rank(weekly_values),
        "per_file_peak_daily_multiplicity": nearest_rank(per_file_peak_day.values()),
        "per_file_peak_weekly_multiplicity": nearest_rank(per_file_peak_week.values()),
        "per_file_rolling_24h_max_distinct_sessions": nearest_rank(rolling_day.values()),
        "per_file_rolling_7d_max_distinct_sessions": nearest_rank(rolling_week.values()),
        "distinct_file_denominator": len(file_sessions),
        "top_20_hottest_files": hot_paths,
        "credential_redacted_top_path_count": redacted_hot_count,
    }

    read_times: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    write_call_times: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    write_result_times: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for operation in localized_reads:
        assert operation.path is not None
        read_times[(operation.actor, operation.path)].append(operation.result_ts)
    for operation in localized_writes:
        assert operation.path is not None
        write_call_times[(operation.actor, operation.path)].append(operation.call_ts)
        write_result_times[(operation.actor, operation.path)].append(operation.result_ts)
    rw_keys = set(read_times) & set(write_call_times)
    literal_read_to_write_values: list[float] = []
    subsequent_read_to_write_values: list[float] = []
    literal_eligible_keys: set[tuple[str, str]] = set()
    no_following_write = 0
    first_write_before_first_read = 0
    for key in rw_keys:
        first_read = min(read_times[key])
        first_write = min(write_call_times[key])
        if first_write >= first_read:
            literal_read_to_write_values.append(first_write - first_read)
            literal_eligible_keys.add(key)
        else:
            first_write_before_first_read += 1
        delta = first_following_delta(read_times[key], write_call_times[key])
        if delta is None:
            no_following_write += 1
        else:
            subsequent_read_to_write_values.append(delta)
    linger_values: list[float] = []
    missing_or_negative_session_end = 0
    for (actor, _), times in write_result_times.items():
        end = actor_spans.get(actor, (None, None))[1]
        last_write = max(times)
        if not isinstance(end, (int, float)) or end < last_write:
            missing_or_negative_session_end += 1
        else:
            linger_values.append(end - last_write)
    last_write_by_actor: dict[str, float] = {}
    for (actor, _), times in write_result_times.items():
        last_write_by_actor[actor] = max(last_write_by_actor.get(actor, float("-inf")), max(times))
    session_level_linger: list[float] = []
    for actor, last_write in last_write_by_actor.items():
        end = actor_spans.get(actor, (None, None))[1]
        if isinstance(end, (int, float)) and end >= last_write:
            session_level_linger.append(end - last_write)
    read_write_intervals = {
        "session_file_pairs_with_read_and_write": len(rw_keys),
        "literal_pairs_first_write_at_or_after_first_read": len(literal_read_to_write_values),
        "pairs_first_write_before_first_read": first_write_before_first_read,
        "first_read_result_to_absolute_first_write_call_seconds": nearest_rank(literal_read_to_write_values),
        "session_file_pairs_with_any_write_after_first_read": len(subsequent_read_to_write_values),
        "pairs_with_no_write_after_first_read": no_following_write,
        "first_read_result_to_first_following_write_call_seconds_sensitivity": nearest_rank(subsequent_read_to_write_values),
        "session_file_pairs_with_write_for_linger": len(write_result_times),
        "last_write_result_to_session_end_seconds": nearest_rank(linger_values),
        "last_write_of_any_file_to_session_end_seconds_per_actor": nearest_rank(session_level_linger),
        "linger_pairs_missing_or_negative_session_end": missing_or_negative_session_end,
        "session_end_definition": "last timestamped record for the logical actor in the frozen corpus prefix",
        "censoring": "no explicit close marker; live, paused, or crashed sessions are right-censored at the frozen last record",
    }

    structured_concurrency_events = [
        (operation.call_ts, operation.actor, operation.repo_keys)
        for operation in successful_core
    ]
    shell_concurrency_events = [
        (event.timestamp, event.actor, event.repo_keys) for event in shell_events
    ]
    structured_plus_shell_events = structured_concurrency_events + shell_concurrency_events
    project_proxy_structured_concurrency = concurrency_metrics(structured_concurrency_events)
    raw_session_project_proxy_concurrency = concurrency_metrics(
        (operation.call_ts, operation.sessions[0], operation.repo_keys)
        for operation in successful_core
        if len(operation.sessions) == 1
    )
    all_tool_concurrency = concurrency_metrics(
        (call.call_ts, call.actor, call.repo_keys) for call in calls
    )
    structured_plus_shell_concurrency = concurrency_metrics(structured_plus_shell_events)
    structured_plus_shell_paired_delta = paired_concurrency_delta_metrics(
        structured_concurrency_events, structured_plus_shell_events
    )
    git_root_cache: dict[str, str | None] = {}
    current_filesystem_lookup_utc = iso_utc(dt.datetime.now(dt.timezone.utc).timestamp())
    target_repo_events: list[tuple[float, str, tuple[str, ...]]] = []
    target_repo_resolved = 0
    target_repo_unknown = 0
    for operation in successful_core:
        if operation.path is None:
            target_repo_unknown += 1
            continue
        root = current_git_root(operation.path, git_root_cache)
        if root is None:
            target_repo_unknown += 1
        else:
            target_repo_resolved += 1
            target_repo_events.append((operation.call_ts, operation.actor, (root,)))
    current_git_root_concurrency = concurrency_metrics(target_repo_events)
    concurrency = {
        "structured_successful_read_edit_write_project_bucket_proxy": project_proxy_structured_concurrency,
        "raw_session_id_project_bucket_proxy": raw_session_project_proxy_concurrency,
        "structured_target_current_git_root": current_git_root_concurrency,
        "current_git_root_attribution": ratio(target_repo_resolved, target_repo_resolved + target_repo_unknown),
        "current_git_root_unknown_event_count": target_repo_unknown,
        "all_deduplicated_tool_calls_project_bucket_sensitivity": all_tool_concurrency,
        "structured_plus_shell_project_bucket_sensitivity": structured_plus_shell_concurrency,
        "repository_scope": (
            "primary same-repository slice resolves each structured target to an extant current .git ancestor; "
            "historical/deleted targets are unknown. Claude project directory results are a separately labeled proxy"
        ),
        "current_filesystem_lookup_utc": current_filesystem_lookup_utc,
        "primary_identity": "logical actor: agentId when present, otherwise sessionId",
    }

    new_files_by_week: collections.Counter[str] = collections.Counter()
    new_pairs_by_week: collections.Counter[str] = collections.Counter()
    monday_by_label: dict[str, dt.date] = {}
    for timestamp in first_file_read.values():
        label = iso_week(timestamp)
        new_files_by_week[label] += 1
        monday_by_label[label] = week_monday(timestamp)
    for timestamp in first_pair_read.values():
        label = iso_week(timestamp)
        new_pairs_by_week[label] += 1
        monday_by_label[label] = week_monday(timestamp)
    growth: list[dict[str, Any]] = []
    all_record_timestamps = [timestamp for span in actor_spans.values() for timestamp in span]
    if monday_by_label:
        start = (
            week_monday(min(all_record_timestamps))
            if all_record_timestamps else min(monday_by_label.values())
        )
        end = (
            week_monday(max(all_record_timestamps))
            if all_record_timestamps else max(monday_by_label.values())
        )
        cumulative_files = 0
        cumulative_pairs = 0
        current = start
        while current <= end:
            year, week, _ = current.isocalendar()
            label = f"{year:04d}-W{week:02d}"
            new_files = new_files_by_week[label]
            new_pairs = new_pairs_by_week[label]
            cumulative_files += new_files
            cumulative_pairs += new_pairs
            growth.append(
                {
                    "iso_week": label,
                    "new_distinct_files": new_files,
                    "cumulative_distinct_files": cumulative_files,
                    "new_file_session_pairs": new_pairs,
                    "cumulative_file_session_pairs": cumulative_pairs,
                }
            )
            current += dt.timedelta(days=7)
    index_cardinality = {
        "distinct_files_ever_read": len(first_file_read),
        "distinct_file_session_pairs": len(first_pair_read),
        "growth_week_denominator_including_zero_growth_weeks": len(growth),
        "growth_time_span_definition": "every ISO week from the first to last timestamped logical-actor record in the frozen corpus, including leading, trailing, and internal zero-growth weeks",
        "new_distinct_files_per_iso_week": nearest_rank(row["new_distinct_files"] for row in growth),
        "new_file_session_pairs_per_iso_week": nearest_rank(row["new_file_session_pairs"] for row in growth),
        "weekly_growth": growth,
    }

    shell_path_commands = [event for event in shell_events if event.mentions]
    shell_all_mentions = [mention for event in shell_events for mention in event.mentions]
    shell_canonical_mentions = [
        (event, mention)
        for event in shell_events
        for mention in event.mentions
        if mention.path is not None and not mention.is_pattern
    ]
    shell_command_with_canonical = {
        event.tool_id for event, _ in shell_canonical_mentions
    }
    shell_intents = collections.Counter(event.intent for event in shell_events)
    shell_tools = collections.Counter(event.tool for event in shell_events)
    localized_structured_path_events = len(localized_reads) + len(localized_writes)
    canonical_shell_paths = {mention.path for _, mention in shell_canonical_mentions if mention.path is not None}
    canonical_shell_pairs = {
        (mention.path, event.actor)
        for event, mention in shell_canonical_mentions
        if mention.path is not None
    }
    diagnostics = metadata.get("diagnostics", {})
    structured_source_coverage: dict[str, Any] = {}
    for source in ("main", "direct_subagent", "workflow_subagent"):
        source_payload: dict[str, Any] = {}
        for tool in ("Read", "Edit", "Write", "NotebookRead", "NotebookEdit", "MultiEdit"):
            successful = int(diagnostics.get(f"successful_result_occurrences_{source}_{tool}", 0))
            exact = int(diagnostics.get(f"metadata_{source}_{tool}_exact_read", 0)) + int(
                diagnostics.get(f"metadata_{source}_{tool}_exact_write", 0)
            )
            source_payload[tool] = {
                "successful_result_occurrences": successful,
                "exact_structured_metadata_occurrences": exact,
                "exact_metadata_coverage": ratio(exact, successful),
                "original_file_key_occurrences": int(
                    diagnostics.get(f"original_file_key_{source}_{tool}", 0)
                ),
                "original_file_string_occurrences": int(
                    diagnostics.get(f"original_file_string_{source}_{tool}", 0)
                ),
            }
        structured_source_coverage[source] = source_payload
    capture_coverage = {
        "deduplicated_shell_commands": len(shell_events),
        "deduplicated_structured_file_tool_calls": len(core_calls),
        "shell_by_tool": dict(sorted(shell_tools.items())),
        "shell_by_intent_heuristic": dict(sorted(shell_intents.items())),
        "commands_with_any_parser_path_mention": len(shell_path_commands),
        "commands_with_canonical_non_pattern_path": len(shell_command_with_canonical),
        "canonical_non_pattern_path_mentions": len(shell_canonical_mentions),
        "unresolved_or_pattern_path_mentions": sum(
            mention.path is None or mention.is_pattern for mention in shell_all_mentions
        ),
        "mentions_per_parser_positive_command": nearest_rank(
            len(event.mentions) for event in shell_path_commands
        ),
        "distinct_canonical_shell_paths": len(canonical_shell_paths),
        "distinct_canonical_shell_path_actor_pairs": len(canonical_shell_pairs),
        "localized_structured_read_edit_write_events": localized_structured_path_events,
        "all_successful_structured_read_edit_write_events": len(successful_core),
        "shell_channel_calls_to_structured_file_tool_calls": ratio(len(shell_events), len(core_calls)),
        "shell_commands_to_successful_structured_events": ratio(len(shell_events), len(successful_core)),
        "parser_positive_shell_commands_to_localized_structured_path_events": ratio(
            len(shell_command_with_canonical), localized_structured_path_events
        ),
        "canonical_shell_mentions_to_localized_structured_path_events": ratio(
            len(shell_canonical_mentions), localized_structured_path_events
        ),
        "parser_description": (
            "quote-aware token scan; absolute/UNC, explicit relative, path-flag, common-basename, "
            "and known-extension candidates; resolves literals against recorded cwd; retains home/environment "
            "aliases as unresolved symbolic mentions; excludes URLs, switches, unresolved variables, and globs "
            "from canonical-file counts"
        ),
        "intent_description": "regex heuristic: read, write, read_write, or ambiguous; intent is not filesystem-effect proof",
        "structured_result_metadata_by_source_occurrence": structured_source_coverage,
    }

    shell_day_sessions: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    shell_week_sessions: dict[tuple[str, str], set[str]] = collections.defaultdict(set)
    shell_file_sessions: dict[str, set[str]] = collections.defaultdict(set)
    shell_file_actor_events: dict[str, list[tuple[float, str]]] = collections.defaultdict(list)
    shell_pair_first: dict[tuple[str, str], float] = {}
    shell_file_first: dict[str, float] = {}
    shell_read_times: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    shell_write_times: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    shell_mention_times: dict[tuple[str, str], list[float]] = collections.defaultdict(list)
    for event, mention in shell_canonical_mentions:
        assert mention.path is not None
        path = mention.path
        shell_day_sessions[(path, utc_day(event.timestamp))].add(event.actor)
        shell_week_sessions[(path, iso_week(event.timestamp))].add(event.actor)
        shell_file_sessions[path].add(event.actor)
        shell_file_actor_events[path].append((event.timestamp, event.actor))
        pair = (path, event.actor)
        shell_pair_first[pair] = min(shell_pair_first.get(pair, event.timestamp), event.timestamp)
        shell_file_first[path] = min(shell_file_first.get(path, event.timestamp), event.timestamp)
        actor_path = (event.actor, path)
        shell_mention_times[actor_path].append(event.timestamp)
        if event.intent in {"read", "read_write"}:
            shell_read_times[actor_path].append(event.timestamp)
        if event.intent in {"write", "read_write"}:
            shell_write_times[actor_path].append(event.timestamp)

    combined_day = {key: set(value) for key, value in file_day_sessions.items()}
    combined_week = {key: set(value) for key, value in file_week_sessions.items()}
    for key, actors in shell_day_sessions.items():
        combined_day.setdefault(key, set()).update(actors)
    for key, actors in shell_week_sessions.items():
        combined_week.setdefault(key, set()).update(actors)
    combined_file_actor_events = {
        path: list(events) for path, events in file_read_actor_events.items()
    }
    for path, events in shell_file_actor_events.items():
        combined_file_actor_events.setdefault(path, []).extend(events)
    combined_rolling_day = {
        path: rolling_distinct_max(events, 86_400)
        for path, events in combined_file_actor_events.items()
    }
    combined_rolling_week = {
        path: rolling_distinct_max(events, 604_800)
        for path, events in combined_file_actor_events.items()
    }
    paths_with_daily_lift = sum(
        combined_rolling_day.get(path, 0) > rolling_day.get(path, 0)
        for path in combined_rolling_day
    )
    paths_with_weekly_lift = sum(
        combined_rolling_week.get(path, 0) > rolling_week.get(path, 0)
        for path in combined_rolling_week
    )

    combined_reads: dict[tuple[str, str], list[float]] = {
        key: list(values) for key, values in read_times.items()
    }
    combined_writes: dict[tuple[str, str], list[float]] = {
        key: list(values) for key, values in write_call_times.items()
    }
    for key, values in shell_read_times.items():
        combined_reads.setdefault(key, []).extend(values)
    for key, values in shell_write_times.items():
        combined_writes.setdefault(key, []).extend(values)
    combined_rw_keys = set(combined_reads) & set(combined_writes)
    combined_interval_values = [
        delta
        for key in combined_rw_keys
        if (delta := first_following_delta(combined_reads[key], combined_writes[key])) is not None
    ]

    union_files = set(first_file_read) | set(shell_file_first)
    union_pairs = set(first_pair_read) | set(shell_pair_first)
    shell_overlap_any = shell_overlap_earlier = shell_overlap_between = shell_overlap_later = 0
    for key in literal_eligible_keys:
        mentions = shell_mention_times.get(key, [])
        if not mentions:
            continue
        shell_overlap_any += 1
        first_read = min(read_times[key])
        first_write = min(write_call_times[key])
        shell_overlap_earlier += int(any(timestamp < first_read for timestamp in mentions))
        shell_overlap_between += int(any(first_read <= timestamp <= first_write for timestamp in mentions))
        shell_overlap_later += int(any(timestamp > first_write for timestamp in mentions))
    blind_spot = {
        "common_shell_gap": {
            "deduplicated_shell_commands": len(shell_events),
            "commands_with_canonical_path": len(shell_command_with_canonical),
            "canonical_path_mentions": len(shell_canonical_mentions),
            "relative_to_successful_structured_events": ratio(len(shell_events), len(successful_core)),
        },
        "item_1_event_volume": {
            "untyped_shell_commands_relative_to_structured_events": ratio(len(shell_events), len(successful_core)),
            "parser_positive_shell_commands_relative_to_structured_events": ratio(
                len(shell_command_with_canonical), len(successful_core)
            ),
        },
        "item_2_read_windows": {
            "structured_localized_read_windows": len(localized_reads),
            "shell_canonical_path_mentions_without_line_windows": len(shell_canonical_mentions),
            "shell_mentions_relative_to_read_windows": ratio(len(shell_canonical_mentions), len(localized_reads)),
            "heuristic_read_or_read_write_shell_commands_with_canonical_paths": len(
                {
                    event.tool_id
                    for event, _ in shell_canonical_mentions
                    if event.intent in {"read", "read_write"}
                }
            ),
        },
        "item_3_edit_regions": {
            "structured_patch_hunks": len(all_hunks),
            "shell_write_or_read_write_commands_with_canonical_paths": len(
                {
                    event.tool_id
                    for event, _ in shell_canonical_mentions
                    if event.intent in {"write", "read_write"}
                }
            ),
            "ambiguous_shell_commands_with_canonical_paths": len(
                {
                    event.tool_id
                    for event, _ in shell_canonical_mentions
                    if event.intent == "ambiguous"
                }
            ),
        },
        "item_4_read_multiplicity": {
            "primary_daily_distribution": nearest_rank(daily_values),
            "all_shell_mentions_as_reads_daily_scenario_sensitivity": nearest_rank(
                len(actors) for actors in combined_day.values()
            ),
            "primary_weekly_distribution": nearest_rank(weekly_values),
            "all_shell_mentions_as_reads_weekly_scenario_sensitivity": nearest_rank(
                len(actors) for actors in combined_week.values()
            ),
            "all_shell_mentions_as_reads_rolling_24h_per_path": nearest_rank(combined_rolling_day.values()),
            "all_shell_mentions_as_reads_rolling_7d_per_path": nearest_rank(combined_rolling_week.values()),
            "paths_whose_rolling_24h_max_increases": paths_with_daily_lift,
            "paths_whose_rolling_7d_max_increases": paths_with_weekly_lift,
            "maximum_rolling_24h_absolute_lift": max(
                (combined_rolling_day.get(path, 0) - rolling_day.get(path, 0) for path in combined_rolling_day),
                default=0,
            ),
            "maximum_rolling_7d_absolute_lift": max(
                (combined_rolling_week.get(path, 0) - rolling_week.get(path, 0) for path in combined_rolling_week),
                default=0,
            ),
        },
        "item_5_read_write_intervals": {
            "primary_literal_eligible_session_file_pairs": len(literal_read_to_write_values),
            "primary_subsequent_write_proxy_pairs": len(subsequent_read_to_write_values),
            "heuristic_shell_read_write_union_eligible_pairs": len(combined_interval_values),
            "heuristic_union_interval_seconds": nearest_rank(combined_interval_values),
            "literal_pairs_with_any_same_path_shell_mention": ratio(
                shell_overlap_any, len(literal_eligible_keys)
            ),
            "literal_pairs_with_same_path_shell_mention_before_first_read": shell_overlap_earlier,
            "literal_pairs_with_same_path_shell_mention_between_read_and_write": shell_overlap_between,
            "literal_pairs_with_same_path_shell_mention_after_first_write": shell_overlap_later,
            "warning": "shell intent is heuristic and cannot be treated as observed read/write truth",
        },
        "item_6_concurrency": {
            "structured_only_project_bucket_proxy": project_proxy_structured_concurrency,
            "structured_only_current_git_root_resolved_slice": current_git_root_concurrency,
            "structured_plus_shell_calls": structured_plus_shell_concurrency,
            "paired_actor_count_delta_on_structured_plus_shell_minute_support": structured_plus_shell_paired_delta,
            "note": "structured-plus-shell minute counts isolate the observed shell timestamp channel but still miss non-tool thinking time and do not prove duration overlap",
        },
        "item_7_index_cardinality": {
            "structured_distinct_files": len(first_file_read),
            "structured_distinct_file_session_pairs": len(first_pair_read),
            "union_if_all_recovered_shell_mentions_were_reads_distinct_paths": len(union_files),
            "union_if_all_recovered_shell_mentions_were_reads_path_session_pairs": len(union_pairs),
            "shell_only_distinct_paths": len(set(shell_file_first) - set(first_file_read)),
            "shell_only_path_session_pairs": len(set(shell_pair_first) - set(first_pair_read)),
            "candidate_path_cardinality_lift": ratio(
                len(union_files) - len(first_file_read), len(first_file_read)
            ),
            "candidate_path_session_pair_cardinality_lift": ratio(
                len(union_pairs) - len(first_pair_read), len(first_pair_read)
            ),
        },
    }

    read_paths = [operation.path for operation in localized_reads if operation.path is not None]
    write_paths = [operation.path for operation in localized_writes if operation.path is not None]
    read_event_mix = categorical_counts(read_paths)
    write_event_mix = categorical_counts(write_paths)
    read_by_path = collections.Counter(read_paths)
    write_by_path = collections.Counter(write_paths)
    for category, payload in read_event_mix["categories"].items():
        payload["events_per_distinct_file"] = nearest_rank(
            count for path, count in read_by_path.items() if classify_file_type(path) == category
        )
    for category, payload in write_event_mix["categories"].items():
        payload["events_per_distinct_file"] = nearest_rank(
            count for path, count in write_by_path.items() if classify_file_type(path) == category
        )
    def per_actor_category_distributions(events: Sequence[Operation]) -> dict[str, Any]:
        totals = collections.Counter(operation.actor for operation in events)
        by_category: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
        for operation in events:
            if operation.path is not None:
                by_category[classify_file_type(operation.path)][operation.actor] += 1
        return {
            category: {
                "event_count_including_zero": nearest_rank(
                    by_category[category].get(actor, 0) for actor in totals
                ),
                "event_share_percent_including_zero": nearest_rank(
                    100.0 * by_category[category].get(actor, 0) / totals[actor]
                    for actor in totals
                ),
                "active_actor_denominator": len(totals),
            }
            for category in ("source", "config", "markdown", "json", "lock", "binary", "other")
        }
    file_type_mix = {
        "localized_read_event_distribution": read_event_mix,
        "localized_write_event_distribution": write_event_mix,
        "read_category_distribution_per_read_active_actor": per_actor_category_distributions(localized_reads),
        "write_category_distribution_per_write_active_actor": per_actor_category_distributions(localized_writes),
        "classification_rule": "mutually exclusive path-extension/basename classifier; binary means binary-looking extension, not inspected content",
    }

    calls_by_actor: collections.Counter[str] = collections.Counter(call.actor for call in calls)
    core_by_actor: collections.Counter[str] = collections.Counter(operation.actor for operation in successful_core)
    shell_by_actor: collections.Counter[str] = collections.Counter(event.actor for event in shell_events)
    calls_by_raw_session: collections.Counter[str] = collections.Counter()
    core_by_raw_session: collections.Counter[str] = collections.Counter()
    reads_by_raw_session: collections.Counter[str] = collections.Counter()
    writes_by_raw_session: collections.Counter[str] = collections.Counter()
    ambiguous_call_session_count = 0
    ambiguous_operation_session_count = 0
    localized_read_ids = {operation.tool_id for operation in localized_reads}
    localized_write_ids = {operation.tool_id for operation in localized_writes}
    for call in calls:
        if len(call.sessions) == 1:
            calls_by_raw_session[call.sessions[0]] += 1
        else:
            ambiguous_call_session_count += 1
    for operation in successful_core:
        if len(operation.sessions) == 1:
            session = operation.sessions[0]
            core_by_raw_session[session] += 1
            reads_by_raw_session[session] += int(operation.tool_id in localized_read_ids)
            writes_by_raw_session[session] += int(operation.tool_id in localized_write_ids)
        else:
            ambiguous_operation_session_count += 1
    structured_actor_spans: dict[str, list[float]] = {}
    for operation in successful_core:
        update_span(structured_actor_spans, operation.actor, operation.call_ts)
        update_span(structured_actor_spans, operation.actor, operation.result_ts)
    all_call_actors = set(calls_by_actor)
    all_raw_sessions = set(raw_session_spans)
    session_lengths = {
        "logical_actor_identity_rule": "agentId when present, otherwise sessionId",
        "actors_with_any_deduplicated_tool_call": len(all_call_actors),
        "all_tool_calls_per_actor": nearest_rank(calls_by_actor.values()),
        "successful_read_edit_write_events_per_core_active_actor": nearest_rank(core_by_actor.values()),
        "core_active_actor_denominator": len(core_by_actor),
        "successful_read_edit_write_events_per_any_tool_actor_including_zero": nearest_rank(
            core_by_actor.get(actor, 0) for actor in all_call_actors
        ),
        "shell_commands_per_shell_active_actor": nearest_rank(shell_by_actor.values()),
        "shell_active_actor_denominator": len(shell_by_actor),
        "raw_session_id_count": len(all_raw_sessions),
        "all_tool_calls_per_raw_session_including_zero": nearest_rank(
            calls_by_raw_session.get(session, 0) for session in all_raw_sessions
        ),
        "successful_read_edit_write_events_per_raw_session_including_zero": nearest_rank(
            core_by_raw_session.get(session, 0) for session in all_raw_sessions
        ),
        "localized_reads_per_raw_session_including_zero": nearest_rank(
            reads_by_raw_session.get(session, 0) for session in all_raw_sessions
        ),
        "localized_writes_per_raw_session_including_zero": nearest_rank(
            writes_by_raw_session.get(session, 0) for session in all_raw_sessions
        ),
        "ambiguous_session_tool_calls_excluded": ambiguous_call_session_count,
        "ambiguous_session_structured_operations_excluded": ambiguous_operation_session_count,
        "wall_clock_seconds_first_to_last_timestamped_record": nearest_rank(
            max(0.0, span[1] - span[0]) for span in actor_spans.values()
        ),
        "wall_clock_actor_denominator": len(actor_spans),
        "zero_duration_actor_count": sum(span[1] == span[0] for span in actor_spans.values()),
        "raw_session_wall_clock_seconds_first_to_last_timestamped_record": nearest_rank(
            max(0.0, span[1] - span[0]) for span in raw_session_spans.values()
        ),
        "raw_session_wall_clock_denominator": len(raw_session_spans),
        "structured_active_span_seconds_per_core_active_actor": nearest_rank(
            max(0.0, span[1] - span[0]) for span in structured_actor_spans.values()
        ),
        "wall_clock_definition": "first to last timestamped record for the logical actor in the frozen prefix; idle gaps remain included",
    }

    sample_seed = str(metadata.get("corpus_snapshot_sha256", "")) + "\0build-params-shell-audit-v1\0"
    def sample_hash(event: ShellEvent) -> str:
        return hashlib.sha256(
            (sample_seed + event.tool + "\0" + event.tool_id + "\0" + event.command_sha256).encode("utf-8")
        ).hexdigest()

    def sample_stratum(event: ShellEvent) -> str:
        tool = "PowerShell" if event.tool.casefold() == "powershell" else "Bash"
        polarity = "positive" if event.mentions else "negative"
        return f"{tool}_{polarity}"

    stratum_targets = {
        "Bash_positive": 20,
        "Bash_negative": 10,
        "PowerShell_positive": 10,
        "PowerShell_negative": 10,
    }
    stratum_populations = collections.Counter(sample_stratum(event) for event in shell_events)
    sample_events: list[ShellEvent] = []
    for stratum, target in stratum_targets.items():
        sample_events.extend(
            heapq.nsmallest(
                target,
                (event for event in shell_events if sample_stratum(event) == stratum),
                key=sample_hash,
            )
        )
    if len(sample_events) < 50:
        selected_ids = {event.tool_id for event in sample_events}
        sample_events.extend(
            heapq.nsmallest(
                50 - len(sample_events),
                (event for event in shell_events if event.tool_id not in selected_ids),
                key=sample_hash,
            )
        )
    sample_events.sort(key=lambda event: (sample_stratum(event), sample_hash(event)))
    validation_sample = []
    for rank, event in enumerate(sample_events, 1):
        validation_sample.append(
            {
                "sample_rank": rank,
                "selection_hash": sample_hash(event),
                "stratum": sample_stratum(event),
                "command_sha256": event.command_sha256,
                "tool_id_sha256": hashlib.sha256(event.tool_id.encode("utf-8")).hexdigest(),
                "source_ordinal": event.source_ordinal,
                "source_line": event.source_line,
                "parser_command_positive": bool(event.mentions),
                "parser_canonical_mention_count": sum(
                    mention.path is not None and not mention.is_pattern for mention in event.mentions
                ),
                "parser_pattern_or_unresolved_mention_count": sum(
                    mention.path is None or mention.is_pattern for mention in event.mentions
                ),
                "intent_heuristic": event.intent,
            }
        )

    return {
        "event_volume": event_volume,
        "read_window_sizes": read_windows,
        "edit_region_sizes": edit_regions,
        "read_multiplicity": read_multiplicity,
        "read_to_write_intervals": read_write_intervals,
        "observed_concurrency": concurrency,
        "index_cardinality": index_cardinality,
        "capture_coverage": capture_coverage,
        "file_type_mix": file_type_mix,
        "session_lengths": session_lengths,
        "shell_gap_items_1_through_7": blind_spot,
        "shell_validation_sample": {
            "selection_rule": (
                "deterministic disproportionate strata: Bash parser-positive 20, Bash negative 10, "
                "PowerShell positive 10, PowerShell negative 10; lowest frozen-corpus-seeded SHA-256 ranks"
            ),
            "population_denominator": len(shell_events),
            "stratum_population_counts": dict(sorted(stratum_populations.items())),
            "stratum_sample_targets": stratum_targets,
            "sample_size": len(validation_sample),
            "entries": validation_sample,
        },
    }


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path.home() / ".claude" / "projects",
        help="Claude Code projects tree (opened read-only)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("exploratory/build-params/extraction.json"),
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=Path("exploratory/build-params/corpus-manifest.json"),
    )
    parser.add_argument(
        "--sample-output",
        type=Path,
        default=Path("exploratory/build-params/shell-validation-sample.json"),
    )
    parser.add_argument(
        "--freeze-manifest-input",
        type=Path,
        help=(
            "reuse an existing path-redacted prefix manifest; current-only JSONL files "
            "are excluded and every frozen prefix hash is verified before extraction"
        ),
    )
    parser.add_argument("--progress-every", type=int, default=250)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    corpus = args.corpus.resolve()
    if not corpus.is_dir():
        raise SystemExit(f"corpus directory does not exist: {corpus}")
    frozen_manifest = None
    if args.freeze_manifest_input is not None:
        manifest_path = args.freeze_manifest_input.resolve()
        try:
            frozen_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"could not read frozen manifest {manifest_path}: {exc}") from exc
        if not isinstance(frozen_manifest, dict):
            raise SystemExit(f"frozen manifest is not a JSON object: {manifest_path}")
    operations, calls, shell_events, spans, metadata, manifest_files = scan_corpus(
        corpus,
        progress_every=max(0, args.progress_every),
        frozen_manifest=frozen_manifest,
    )
    metrics = build_metrics(operations, calls, shell_events, spans, metadata)
    extraction = {
        "schema_version": 1,
        "generated_utc": iso_utc(dt.datetime.now(dt.timezone.utc).timestamp()),
        "scope": {
            "workload": "one team, one Claude Code harness, Node-dominated",
            "use": "self-calibrating @perrepo seed defaults, not universal constants",
            "corpus_access": "read-only frozen byte prefixes",
        },
        "corpus": metadata,
        "percentile_rule": "nearest-rank over the explicitly named population; max is observed maximum",
        "parameters": metrics,
    }
    manifest = {
        "schema_version": 1,
        "snapshot_utc": metadata["snapshot_utc"],
        "file_count": metadata["corpus_file_count"],
        "byte_count": metadata["corpus_bytes"],
        "frozen_prefix_sha256": metadata["corpus_snapshot_sha256"],
        "paths_redacted": True,
        "files": manifest_files,
    }
    sample = {
        "schema_version": 1,
        "corpus_frozen_prefix_sha256": metadata["corpus_snapshot_sha256"],
        **metrics["shell_validation_sample"],
    }
    atomic_write_json(args.output.resolve(), extraction)
    atomic_write_json(args.manifest_output.resolve(), manifest)
    atomic_write_json(args.sample_output.resolve(), sample)
    print(f"wrote {args.output.resolve()}", flush=True)
    print(f"wrote {args.manifest_output.resolve()}", flush=True)
    print(f"wrote {args.sample_output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
