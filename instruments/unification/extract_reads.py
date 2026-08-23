"""Extract successful Claude Code Read events for one repository.

The transcript corpus is treated as an append-only input.  This script snapshots
the file list and byte sizes before reading, hashes exactly those bytes, and
writes only compact path/timestamp metadata.  No prompt or tool-result content is
retained.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import hashlib
import json
import ntpath
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_CORPUS = Path(r"C:/Users/USER/.claude/projects")
DEFAULT_REPOSITORY = Path(r"C:/Users/USER/Desktop/toolsenabled-current")
DEFAULT_OUTPUT = Path(r"C:/Users/USER/Desktop/Blast-Radius/exploratory/unification/read-events.jsonl.gz")
# Raw subagent JSONL stores Read line numbers followed by a tab.  Some rendered
# exports substitute a right-arrow glyph, so accept both representations.
NUMBERED_CONTENT_LINE = re.compile(r"(?:^|\n)\s*\d+(?:\t|→)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--repository", type=Path, default=DEFAULT_REPOSITORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--progress-every", type=int, default=100)
    return parser.parse_args()


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def normalise_absolute(value: Any, cwd: Any) -> str | None:
    """Resolve a transcript path lexically using Windows path semantics."""
    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = os.fspath(value).strip().strip('"').replace("/", "\\")
    base = os.fspath(cwd).strip().strip('"').replace("/", "\\") if isinstance(cwd, (str, os.PathLike)) else ""
    if not raw:
        return None
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    if base.startswith("\\\\?\\"):
        base = base[4:]
    if not ntpath.isabs(raw):
        if not base:
            return None
        raw = ntpath.join(base, raw)
    return ntpath.normcase(ntpath.normpath(raw))


def relative_to_repository(value: Any, cwd: Any, repository: str) -> str | None:
    candidate = normalise_absolute(value, cwd)
    if candidate is None:
        return None
    try:
        if ntpath.commonpath((candidate, repository)) != repository:
            return None
        relative = ntpath.relpath(candidate, repository)
    except ValueError:
        return None
    if relative in ("", ".") or relative == ".." or relative.startswith("..\\"):
        return None
    return relative.replace("\\", "/")


def result_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]


def visible_result_prefix(content: Any, limit: int = 160) -> str:
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        pieces: list[str] = []
        remaining = limit
        for item in content:
            text = item.get("text", "") if isinstance(item, dict) else item if isinstance(item, str) else ""
            if text:
                pieces.append(text[:remaining])
                remaining -= len(pieces[-1])
            if remaining <= 0:
                break
        return "".join(pieces)
    return ""


def successful_result(block: dict[str, Any], top_result: Any) -> bool:
    if block.get("is_error") is True:
        return False
    if structured_file(top_result) is not None:
        return True
    content = block.get("content")
    if isinstance(content, list):
        return True
    return isinstance(content, str) and NUMBERED_CONTENT_LINE.search(content) is not None


def structured_file(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    file_result = result.get("file")
    return file_result if isinstance(file_result, dict) else None


def merge_call(
    calls: dict[tuple[str, str], dict[str, Any]],
    key: str,
    candidate: dict[str, Any],
    diagnostics: collections.Counter[str],
) -> dict[str, Any]:
    existing = calls.get(key)
    if existing is None:
        calls[key] = candidate
        return candidate
    diagnostics["duplicate_read_tool_uses"] += 1
    existing["occurrences"] = int(existing.get("occurrences", 1)) + 1
    comparable = ("ts", "cwd", "input_path")
    if any(existing.get(field) != candidate.get(field) for field in comparable):
        existing["conflict"] = True
        diagnostics["conflicting_duplicate_read_tool_uses"] += 1
    if existing.get("explicit_agent") and candidate.get("explicit_agent") and existing.get("agent") != candidate.get("agent"):
        existing["conflict"] = True
        diagnostics["conflicting_duplicate_explicit_agent_ids"] += 1
    if (
        not existing.get("explicit_agent")
        and not candidate.get("explicit_agent")
        and existing.get("session") != candidate.get("session")
    ):
        existing["fallback_identity_from_copied_prefix"] = True
    # Forked root transcripts copy historical tool IDs while rewriting sessionId.
    # An explicit agentId wins.  For sessionId fallbacks, use the occurrence in
    # the earliest-created transcript, which is the closest available proxy for
    # the originating task.
    candidate_preferred = (
        bool(candidate.get("explicit_agent")) > bool(existing.get("explicit_agent"))
        or (
            bool(candidate.get("explicit_agent")) == bool(existing.get("explicit_agent"))
            and candidate.get("source_created_ns", 0) < existing.get("source_created_ns", 0)
        )
    )
    if candidate_preferred:
        for field in ("session", "agent", "explicit_agent", "source_created_ns", "order"):
            existing[field] = candidate[field]
    return existing


def attach_result(
    call: dict[str, Any],
    block: dict[str, Any],
    top_result: Any,
    result_ts: float | None,
    diagnostics: collections.Counter[str],
) -> None:
    call["result_records"] += 1
    diagnostics["matched_read_result_records"] += 1
    if not successful_result(block, top_result):
        call["error_results"] += 1
        diagnostics["matched_read_error_results"] += 1
        return
    call["successful_result_seen"] = True
    if result_ts is not None and call.get("result_ts") is None:
        call["result_ts"] = result_ts
    file_result = structured_file(top_result)
    if file_result is None or not isinstance(file_result.get("filePath"), str):
        diagnostics["successful_read_results_without_structured_path"] += 1
        return
    candidate = {
        "path": file_result["filePath"],
        "start_line": file_result.get("startLine"),
        "num_lines": file_result.get("numLines"),
        "total_lines": file_result.get("totalLines"),
    }
    existing = call.get("structured_file")
    if existing is not None and existing != candidate:
        call["conflict"] = True
        diagnostics["conflicting_duplicate_read_results"] += 1
        return
    call["structured_file"] = candidate
    diagnostics["successful_read_results_with_structured_path"] += 1


def scan_corpus(corpus: Path, repository: Path, progress_every: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    files = sorted(corpus.rglob("*.jsonl"), key=lambda path: str(path).casefold())
    snapshots: list[tuple[Path, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
            snapshots.append((path, stat.st_size, stat.st_ctime_ns))
        except OSError:
            snapshots.append((path, -1, 0))

    repository_norm = normalise_absolute(repository.resolve(), None)
    if repository_norm is None:
        raise ValueError(f"could not normalise repository root {repository}")

    calls: dict[str, dict[str, Any]] = {}
    diagnostics: collections.Counter[str] = collections.Counter()
    digest = hashlib.sha256()
    snapshot_at = dt.datetime.now(dt.timezone.utc).isoformat()
    snapshot_bytes = sum(size for _, size, _ in snapshots if size >= 0)

    for file_index, (path, byte_limit, source_created_ns) in enumerate(snapshots, 1):
        relative_name = path.relative_to(corpus).as_posix()
        digest.update(relative_name.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0" + str(byte_limit).encode("ascii") + b"\0")
        if byte_limit < 0:
            diagnostics["transcript_files_stat_failed"] += 1
            continue
        try:
            with path.open("rb") as handle:
                remaining = byte_limit
                line_number = 0
                while remaining > 0:
                    raw_line = handle.readline(remaining)
                    if not raw_line:
                        diagnostics["transcript_files_truncated_after_snapshot"] += 1
                        break
                    remaining -= len(raw_line)
                    line_number += 1
                    digest.update(raw_line)
                    diagnostics["jsonl_lines"] += 1
                    # Avoid decoding prose-only records.  Every relevant call or
                    # result contains one of these schema tokens.
                    if b'"tool_use"' not in raw_line and b'"tool_result"' not in raw_line:
                        continue
                    try:
                        record = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        diagnostics["malformed_relevant_json_lines"] += 1
                        continue
                    if not isinstance(record, dict):
                        continue
                    session = record.get("sessionId")
                    record_ts = parse_timestamp(record.get("timestamp"))
                    message = record.get("message")
                    content = message.get("content") if isinstance(message, dict) else None
                    if isinstance(content, list):
                        for content_order, block in enumerate(content):
                            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("name") != "Read":
                                continue
                            diagnostics["raw_read_tool_uses"] += 1
                            tool_id = block.get("id")
                            if not isinstance(session, str) or not isinstance(tool_id, str):
                                diagnostics["read_tool_uses_missing_identity"] += 1
                                continue
                            raw_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                            candidate = {
                                "session": session,
                                "agent": record.get("agentId") or session,
                                "explicit_agent": isinstance(record.get("agentId"), str) and bool(record.get("agentId")),
                                "tool_id": tool_id,
                                "ts": record_ts,
                                "cwd": record.get("cwd"),
                                "input_path": raw_input.get("file_path"),
                                "order": (relative_name, line_number, content_order),
                                "source_created_ns": source_created_ns,
                                "occurrences": 1,
                                "fallback_identity_from_copied_prefix": False,
                                "result_records": 0,
                                "error_results": 0,
                                "successful_result_seen": False,
                                "result_ts": None,
                                "structured_file": None,
                                "conflict": False,
                            }
                            merge_call(calls, tool_id, candidate, diagnostics)

                    blocks = result_blocks(record)
                    top_result = record.get("toolUseResult") if len(blocks) == 1 else None
                    for block in blocks:
                        tool_id = block.get("tool_use_id")
                        call = calls.get(tool_id) if isinstance(tool_id, str) else None
                        if call is not None:
                            attach_result(call, block, top_result, record_ts, diagnostics)
            diagnostics["transcript_files_succeeded"] += 1
        except OSError:
            diagnostics["transcript_files_read_failed"] += 1

        if progress_every > 0 and (file_index % progress_every == 0 or file_index == len(snapshots)):
            print(
                f"scanned {file_index:,}/{len(snapshots):,} transcript files; "
                f"{len(calls):,} unique Read calls",
                flush=True,
            )

    events: list[dict[str, Any]] = []
    agent_sessions: dict[str, set[str]] = collections.defaultdict(set)
    raw_target_paths: set[str] = set()
    for call in calls.values():
        if call["conflict"]:
            diagnostics["read_calls_excluded_conflict"] += 1
            continue
        if call["ts"] is None:
            diagnostics["read_calls_excluded_invalid_timestamp"] += 1
            continue
        if not call["successful_result_seen"]:
            diagnostics["read_calls_excluded_without_successful_result"] += 1
            continue

        file_result = call.get("structured_file")
        path_source = "result_metadata"
        raw_path = file_result.get("path") if isinstance(file_result, dict) else None
        if raw_path is None:
            # A successful visible result confirms that the call completed.  Old
            # and some subagent records omit top-level toolUseResult metadata;
            # retain the paired input as an explicitly tagged fallback.
            raw_path = call.get("input_path")
            path_source = "paired_input_fallback"
        relative = relative_to_repository(raw_path, call.get("cwd"), repository_norm)
        if relative is None:
            diagnostics[f"successful_reads_outside_target_or_unresolved_{path_source}"] += 1
            continue

        if path_source == "paired_input_fallback":
            diagnostics["target_read_events_from_paired_input_fallback"] += 1
        else:
            diagnostics["target_read_events_from_result_metadata"] += 1
            input_relative = relative_to_repository(call.get("input_path"), call.get("cwd"), repository_norm)
            if input_relative is not None and input_relative != relative:
                diagnostics["structured_result_input_path_mismatches"] += 1

        agent = str(call["agent"])
        session = str(call["session"])
        agent_sessions[agent].add(session)
        raw_target_paths.add(relative)
        if call.get("fallback_identity_from_copied_prefix"):
            diagnostics["target_events_with_creation_time_selected_fallback_identity"] += 1
        events.append(
            {
                "agent": agent,
                "session": session,
                "tool_use_id": call["tool_id"],
                "timestamp": call["ts"],
                "result_timestamp": call.get("result_ts"),
                "path": relative,
                "path_source": path_source,
                "start_line": file_result.get("start_line") if isinstance(file_result, dict) else None,
                "num_lines": file_result.get("num_lines") if isinstance(file_result, dict) else None,
                "copied_occurrences": int(call.get("occurrences", 1)),
                "fallback_identity_from_copied_prefix": bool(
                    call.get("fallback_identity_from_copied_prefix")
                ),
            }
        )

    diagnostics["unique_read_calls"] = len(calls)
    diagnostics["target_read_events"] = len(events)
    diagnostics["target_distinct_raw_paths"] = len(raw_target_paths)
    diagnostics["agents"] = len(agent_sessions)
    diagnostics["agent_ids_seen_in_multiple_sessions"] = sum(len(sessions) > 1 for sessions in agent_sessions.values())
    diagnostics["events_from_multi_session_agent_ids"] = sum(
        1 for event in events if len(agent_sessions[event["agent"]]) > 1
    )
    events.sort(key=lambda event: (event["agent"], event["timestamp"], event["session"], event["tool_use_id"]))

    metadata = {
        "schema_version": 1,
        "measurement": "claude-code-successful-read-events",
        "snapshot_utc": snapshot_at,
        "corpus_root": str(corpus.resolve()),
        "corpus_file_count": len(snapshots),
        "corpus_bytes_at_snapshot": snapshot_bytes,
        "corpus_snapshot_sha256": digest.hexdigest(),
        "target_repository": str(repository.resolve()),
        "identity_rule": "agentId when present, otherwise sessionId",
        "deduplication_key": "global toolUseId; copied root occurrences use the earliest-created transcript for sessionId fallback identity",
        "event_time": "Read tool_use record timestamp; matching successful tool_result is required",
        "path_rule": "toolUseResult.file.filePath, with successful-result paired input fallback tagged separately",
        "diagnostics": dict(sorted(diagnostics.items())),
    }
    return events, metadata


def write_events(path: Path, events: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "header", **metadata}, sort_keys=True) + "\n")
        for event in events:
            handle.write(json.dumps({"type": "read", **event}, sort_keys=True, separators=(",", ":")) + "\n")
    os.replace(temporary, path)

    metadata_path = path.with_suffix("").with_suffix(".meta.json")
    metadata_temporary = metadata_path.with_name(metadata_path.name + ".tmp")
    metadata_temporary.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(metadata_temporary, metadata_path)


def main() -> int:
    args = parse_args()
    corpus = args.corpus.resolve()
    repository = args.repository.resolve()
    if not corpus.is_dir():
        raise SystemExit(f"transcript corpus is not a directory: {corpus}")
    if not (repository / ".git").exists():
        raise SystemExit(f"target is not a Git worktree: {repository}")
    events, metadata = scan_corpus(corpus, repository, args.progress_every)
    write_events(args.output.resolve(), events, metadata)
    print(
        f"wrote {len(events):,} target-repository Read events to {args.output.resolve()}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
