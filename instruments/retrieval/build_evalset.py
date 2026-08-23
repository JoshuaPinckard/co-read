"""Build causal Grep-follow-up labels from Claude Code JSONL transcripts.

The corpus is streamed one transcript at a time.  Only compact tool-call and
parent-link metadata for the current transcript is retained in memory.

The canonical output is the 300 second eval set required by SPEC.md.  The 60
and 900 second variants are emitted in the same pass so sensitivity analysis
does not require three 3.5 GiB corpus scans.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import ntpath
import os
import posixpath
import re
import sys
from bisect import bisect_right
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CORPUS = Path(r"C:/Users/USER/.claude/projects")
DEFAULT_OUTPUT = Path(r"C:/Users/USER/Desktop/Blast-Radius/exploratory/retrieval")
WINDOWS = (60, 300, 900)  # fixed by SPEC.md; never tune these on the labels

WINDOWS_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
MSYS_ABS = re.compile(r"^/([A-Za-z])(?:/(.*))?$")
WINDOWS_CONTENT_PATH = re.compile(r"^([A-Za-z]:[\\/].*?):\d+(?::|-)" )
RELATIVE_CONTENT_PATH = re.compile(r"^(.+?):\d+(?::|-)")
QUERY_KEYS = (
    "pattern", "path", "glob", "type", "output_mode", "-i", "-n", "head_limit",
    # Present in the live corpus and output-affecting.  Preserving them is a
    # schema extension required to replay the real control rather than a proxy.
    "-C", "-A", "-B", "-o", "offset", "multiline", "context", "-l", "-a",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--progress-every", type=int, default=100)
    return parser.parse_args()


def timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def normalise_path(value: Any, cwd: Any) -> str | None:
    """Resolve a transcript path lexically, without requiring it to still exist."""
    if not isinstance(value, (str, os.PathLike)):
        return None
    raw = os.fspath(value).strip().strip('"')
    if not raw:
        return None
    base = os.fspath(cwd) if isinstance(cwd, (str, os.PathLike)) else ""
    msys = MSYS_ABS.match(raw.replace("\\", "/"))
    if msys:
        tail = msys.group(2) or ""
        raw = f"{msys.group(1)}:/{tail}"
    windows = bool(WINDOWS_ABS.match(raw) or WINDOWS_ABS.match(base) or "\\" in raw or "\\" in base)
    if windows:
        raw = raw.replace("/", "\\")
        base = base.replace("/", "\\")
        if not ntpath.isabs(raw):
            if not base:
                return None
            raw = ntpath.join(base, raw)
        return ntpath.normcase(ntpath.normpath(raw))
    if not posixpath.isabs(raw):
        if not base:
            return None
        raw = posixpath.join(base, raw)
    return posixpath.normpath(raw)


def visible_result_bytes(content: Any) -> int:
    """Bytes actually placed in the tool_result block seen by the agent."""
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
            elif isinstance(item, str):
                pieces.append(item)
        if pieces:
            return len("".join(pieces).encode("utf-8"))
    if content is None:
        return 0
    return len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def visible_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else item if isinstance(item, str) else ""
            for item in content
        )
    return ""


def looks_like_path(value: str) -> bool:
    if not value or len(value) > 1_024 or value.startswith(("Found ", "No matches", "--")):
        return False
    if any(character.isspace() for character in value) and "/" not in value and "\\" not in value:
        return False
    leaf = re.split(r"[\\/]", value)[-1]
    return bool("/" in value or "\\" in value or "." in leaf)


def valid_path_spelling(value: str) -> bool:
    """Reject result prose that merely contains something path-shaped."""

    if not looks_like_path(value) or "\x00" in value:
        return False
    rendered = value.strip().strip('"')
    if WINDOWS_ABS.match(rendered):
        rendered = rendered[2:]
    return not any(character in rendered for character in '<>:"|?*')


def within_search_scope(path: str, call: dict[str, Any]) -> bool:
    raw_scope = call["input"].get("path")
    scope = normalise_path(raw_scope if isinstance(raw_scope, str) and raw_scope else ".", call.get("cwd"))
    if not scope:
        return True
    windows = bool(WINDOWS_ABS.match(path) or WINDOWS_ABS.match(scope) or "\\" in path or "\\" in scope)
    module = ntpath if windows else posixpath
    candidate = module.normcase(module.normpath(path)) if windows else module.normpath(path)
    root = module.normcase(module.normpath(scope)) if windows else module.normpath(scope)
    try:
        return module.commonpath((candidate, root)) == root
    except ValueError:
        return False


def grep_returned_paths(call: dict[str, Any]) -> list[str]:
    if call.get("result_error"):
        return []
    result = call.get("structured_result")
    content = visible_result_text(call.get("result_content"))
    raw_paths: list[str] = []
    mode = str(call["input"].get("output_mode") or "files_with_matches")

    if isinstance(result, dict):
        for key in ("filenames", "files", "paths"):
            values = result.get(key)
            if isinstance(values, list):
                raw_paths.extend(value for value in values if isinstance(value, str))
        if not content:
            content = next(
                (result[key] for key in ("content", "stdout") if isinstance(result.get(key), str)),
                "",
            )

    if not raw_paths and content:
        for original_line in content.splitlines():
            line = original_line.strip()
            if not line or line.startswith(("Found ", "No matches", "--")):
                continue
            candidate: str | None = None
            if mode == "files_with_matches":
                # A few legacy results claim files_with_matches metadata while
                # presenting content lines. Parse their filename prefix rather
                # than treating the entire matched line as a path.
                match = WINDOWS_CONTENT_PATH.match(line) or RELATIVE_CONTENT_PATH.match(line)
                candidate = match.group(1) if match else None if re.match(r"^\d+(?::|-)", line) else line
            elif mode == "count":
                if WINDOWS_ABS.match(line):
                    match = re.match(r"^([A-Za-z]:[\\/].*):\d+$", line)
                    candidate = match.group(1) if match else None
                elif ":" in line:
                    candidate = line.rsplit(":", 1)[0]
            else:
                match = WINDOWS_CONTENT_PATH.match(line) or RELATIVE_CONTENT_PATH.match(line)
                candidate = match.group(1) if match else None
            if candidate and valid_path_spelling(candidate):
                raw_paths.append(candidate)

    seen: set[str] = set()
    paths: list[str] = []
    for raw in raw_paths:
        resolved = normalise_path(raw, call.get("cwd"))
        if resolved and within_search_scope(resolved, call) and resolved not in seen:
            seen.add(resolved)
            paths.append(resolved)
    return paths


def read_result_path(call: dict[str, Any], diagnostics: collections.Counter[str]) -> str | None:
    if call.get("result_error") or not call.get("result_seen"):
        return None
    result = call.get("structured_result")
    if isinstance(result, dict):
        file_result = result.get("file")
        if isinstance(file_result, dict):
            path = file_result.get("filePath")
            if isinstance(path, str):
                if all(key in file_result for key in ("startLine", "numLines", "totalLines")):
                    diagnostics["read_results_with_complete_window"] += 1
                else:
                    diagnostics["read_results_without_complete_window"] += 1
                resolved = normalise_path(path, call.get("cwd"))
                if resolved:
                    diagnostics["read_paths_from_result"] += 1
                    return resolved

    # Older/subagent records can omit top-level toolUseResult while retaining a
    # successful tool_result.  The path (but never the line window) is recoverable
    # from the paired call.  This fallback is counted and reported.
    content = visible_result_text(call.get("result_content"))
    if "<tool_use_error>" not in content and "Error:" not in content[:80]:
        value = call["input"].get("file_path") or call["input"].get("notebook_path")
        if isinstance(value, str):
            resolved = normalise_path(value, call.get("cwd"))
            if resolved:
                diagnostics["read_paths_from_paired_input_fallback"] += 1
                return resolved
    diagnostics["read_results_without_resolvable_path"] += 1
    return None


def canonical_query(raw: dict[str, Any]) -> dict[str, Any]:
    has_line_number_option = "-n" in raw
    query = {key: raw.get(key) for key in QUERY_KEYS}
    query["pattern"] = str(query.get("pattern") or "")
    query["output_mode"] = str(query.get("output_mode") or "files_with_matches")
    query["-i"] = bool(query.get("-i", False))
    if has_line_number_option:
        query["-n"] = bool(query.get("-n"))
    else:
        # Claude Grep defaults content output to line numbers.  Preserve
        # absence so replay can distinguish that default from explicit false.
        query.pop("-n", None)
    return query


def result_blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == "tool_result"]


def attach_result(
    call: dict[str, Any],
    block: dict[str, Any],
    structured: Any,
    result_ts: float | None,
    result_parent: Any,
    diagnostics: collections.Counter[str],
) -> None:
    if call.get("result_seen"):
        diagnostics["duplicate_tool_results"] += 1
        call_ts = call.get("ts")
        old_ts = call.get("result_ts")
        new_is_better = (
            result_ts is not None
            and (call_ts is None or result_ts >= call_ts)
            and (old_ts is None or result_ts < old_ts)
        )
        if not new_is_better:
            return
    call["result_seen"] = True
    call["result_ts"] = result_ts
    call["result_parent_uuid"] = result_parent if isinstance(result_parent, str) else None
    call["result_content"] = block.get("content")
    call["result_error"] = bool(block.get("is_error", False))
    call["structured_result"] = structured
    if call.get("uuid") and call.get("result_parent_uuid") != call.get("uuid"):
        diagnostics["result_parent_mismatch"] += 1


def scan_transcript(
    path: Path,
    global_seen: set[tuple[str, str]],
    diagnostics: collections.Counter[str],
    corpus_digest: Any | None = None,
    byte_limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, set[str]], dict[str, set[str]]]:
    """Stream one file and retain only relevant calls plus UUID parent links."""
    calls: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_tool_id: dict[tuple[str, str], dict[str, Any]] = {}
    parents: dict[str, set[str]] = collections.defaultdict(set)
    message_bundles: dict[str, set[str]] = collections.defaultdict(set)

    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if byte_limit is not None and handle.tell() > byte_limit:
                diagnostics["lines_appended_after_snapshot_excluded"] += 1
                break
            if corpus_digest is not None:
                corpus_digest.update(raw_line)
            diagnostics["jsonl_lines"] += 1
            try:
                record = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                diagnostics["malformed_json_lines"] += 1
                continue
            if not isinstance(record, dict):
                continue
            uuid = record.get("uuid")
            parent_uuid = record.get("parentUuid")
            if isinstance(uuid, str):
                if uuid in parents and isinstance(parent_uuid, str) and parent_uuid not in parents[uuid]:
                    diagnostics["duplicate_record_uuids"] += 1
                if isinstance(parent_uuid, str):
                    parents[uuid].add(parent_uuid)

            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            message_id = message.get("id") if isinstance(message, dict) else None
            if isinstance(message_id, str) and isinstance(uuid, str):
                message_bundles[message_id].add(uuid)
            session = record.get("sessionId")
            agent = record.get("agentId") or session
            record_ts = timestamp(record.get("timestamp"))
            if isinstance(content, list):
                for content_order, block in enumerate(content):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name")
                    if name not in ("Grep", "Read", "NotebookRead"):
                        continue
                    tool_id = block.get("id")
                    if not isinstance(tool_id, str) or not isinstance(session, str):
                        diagnostics[f"{str(name).lower()}_calls_missing_identity"] += 1
                        continue
                    kind = "grep" if name == "Grep" else "read"
                    diagnostics[f"raw_{kind}_tool_uses"] += 1
                    key = (session, tool_id)
                    if key in global_seen or key in by_key:
                        diagnostics[f"duplicate_{kind}_tool_uses"] += 1
                        continue
                    call = {
                        "kind": kind,
                        "name": name,
                        "session": session,
                        "agent": agent,
                        "tool_id": tool_id,
                        "uuid": uuid if isinstance(uuid, str) else None,
                        "message_id": message_id if isinstance(message_id, str) else None,
                        "parent_uuid": parent_uuid if isinstance(parent_uuid, str) else None,
                        "ts": record_ts,
                        "order": (line_number, content_order),
                        "cwd": record.get("cwd"),
                        "git_branch": record.get("gitBranch"),
                        "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                        "result_seen": False,
                    }
                    calls.append(call)
                    by_key[key] = call
                    by_tool_id[key] = call
                    global_seen.add(key)

            blocks = result_blocks(record)
            top_result = record.get("toolUseResult")
            for block_index, block in enumerate(blocks):
                tool_id = block.get("tool_use_id")
                if not isinstance(tool_id, str):
                    diagnostics["tool_results_missing_id"] += 1
                    continue
                structured = top_result if len(blocks) == 1 else None
                result_key = (session, tool_id) if isinstance(session, str) else None
                call = by_tool_id.get(result_key) if result_key else None
                if call is None:
                    # Calls precede their results in valid transcripts.  Most
                    # unmatched blocks belong to irrelevant tools, so retaining
                    # their (sometimes multi-megabyte) payloads would defeat the
                    # streaming memory bound.
                    diagnostics["unmatched_tool_result_blocks_ignored"] += 1
                else:
                    attach_result(call, block, structured, record_ts, parent_uuid, diagnostics)

    return calls, parents, message_bundles


def is_descendant(node: str | None, ancestors: set[str], parents: dict[str, set[str]]) -> bool:
    if not node or not ancestors or node in ancestors:
        return False
    pending = [node]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for parent in parents.get(current, set()):
            if parent in ancestors:
                return True
            pending.append(parent)
    return False


def group_batches(calls: Iterable[dict[str, Any]], diagnostics: collections.Counter[str]) -> list[dict[str, Any]]:
    batches: dict[tuple[Any, ...], dict[str, Any]] = {}
    for call in calls:
        if call.get("ts") is None:
            continue
        uuid = call.get("uuid")
        message_id = call.get("message_id")
        key = ("message", message_id) if message_id else ("uuid", uuid) if uuid else ("order", *call["order"])
        batch = batches.setdefault(
            key,
            {
                "uuid": uuid,
                "uuids": set(),
                "message_id": call.get("message_id"),
                "ts": call["ts"],
                "order": call["order"],
                "greps": [],
                "reads": [],
                "read_calls": [],
                "unresolved_read": False,
            },
        )
        if uuid:
            batch["uuids"].add(uuid)
        if call["ts"] < batch["ts"] or call["order"] < batch["order"]:
            batch["ts"] = min(batch["ts"], call["ts"])
            batch["order"] = min(batch["order"], call["order"])
        if call["kind"] == "grep":
            batch["greps"].append(call)
        else:
            batch["read_calls"].append(call)
            path = read_result_path(call, diagnostics)
            if path:
                batch["reads"].append(path)
            else:
                batch["unresolved_read"] = True
    return sorted(batches.values(), key=lambda batch: (batch["ts"], batch["order"]))


def causal_timeline(
    grep: dict[str, Any],
    batches: list[dict[str, Any]],
    batch_times: list[float],
    parents: dict[str, set[str]],
    message_bundles: dict[str, set[str]],
    diagnostics: collections.Counter[str],
) -> tuple[list[dict[str, Any]], bool, float | None]:
    anchor = grep.get("result_ts")
    if anchor is None:
        return [], False, None
    anchors = set(message_bundles.get(grep.get("message_id"), set()))
    if grep.get("uuid"):
        anchors.add(grep["uuid"])
    start = bisect_right(batch_times, anchor)
    timeline: list[dict[str, Any]] = []
    saw_non_descendant = False
    first_causal_after_max: float | None = None
    for batch in batches[start:]:
        delta = batch["ts"] - anchor
        if batch["uuids"] & anchors or (
            grep.get("message_id") and batch.get("message_id") == grep.get("message_id")
        ):
            continue
        relevant = bool(batch["greps"] or batch["reads"] or batch["unresolved_read"])
        if not relevant:
            continue
        same_agent = any(
            call["session"] == grep["session"] and call["agent"] == grep["agent"]
            for call in batch["greps"] + batch["read_calls"]
        )
        # Batches are constructed from calls already grouped per agent below;
        # retain this guard for malformed mixed-agent records.
        if not same_agent:
            continue
        if not any(is_descendant(uuid, anchors, parents) for uuid in batch["uuids"]):
            saw_non_descendant = True
            continue
        if delta > WINDOWS[-1]:
            first_causal_after_max = delta
            break
        timeline.append(
            {
                "delta": delta,
                "reads": batch["reads"],
                "unresolved_read": batch["unresolved_read"],
                "has_grep": bool(batch["greps"]),
            }
        )
        if batch["greps"]:
            break
    if saw_non_descendant:
        diagnostics["greps_with_non_descendant_later_actions"] += 1
    return timeline, saw_non_descendant, first_causal_after_max


def outcome_for_window(timeline: list[dict[str, Any]], window: int) -> dict[str, Any] | None:
    reads: list[str] = []
    seen: set[str] = set()
    first_read_delta: float | None = None
    for batch in timeline:
        if batch["delta"] > window:
            break
        if batch["unresolved_read"] and not reads:
            return {"unresolved": True, "reads": [], "grep": False, "seconds": batch["delta"]}
        for path in batch["reads"]:
            if path not in seen:
                seen.add(path)
                reads.append(path)
                if first_read_delta is None:
                    first_read_delta = batch["delta"]
        if batch["has_grep"]:
            if reads:
                return {"reads": reads, "grep": False, "seconds": first_read_delta}
            return {"reads": [], "grep": True, "seconds": batch["delta"]}
    if reads:
        return {"reads": reads, "grep": False, "seconds": first_read_delta}
    return None


def output_record(grep: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{grep['session']}:{grep['tool_id']}",
        "ts": grep["ts"],
        "agent": grep["agent"],
        "cwd": grep.get("cwd"),
        "git_branch": grep.get("git_branch"),
        "query": canonical_query(grep["input"]),
        "returned_paths": grep_returned_paths(grep),
        "followed_by_read": outcome["reads"],
        "followed_by_grep": outcome["grep"],
        "seconds_to_next_action": round(float(outcome["seconds"]), 3),
        "result_bytes": visible_result_bytes(grep.get("result_content")),
    }


def atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def retention_snapshot(
    diagnostics: collections.Counter[str],
    window_stats: dict[int, collections.Counter[str]],
    files_total: int,
    files_processed: int,
    complete: bool,
    corpus: Path,
    corpus_digest: str,
    corpus_bytes_at_snapshot: int,
    snapshot_utc: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "complete": complete,
        "corpus_root": str(corpus.resolve()),
        "corpus_stream_sha256": corpus_digest,
        "corpus_bytes_at_snapshot": corpus_bytes_at_snapshot,
        "snapshot_utc": snapshot_utc,
        "files_total": files_total,
        "files_processed": files_processed,
        "followup_anchor": "grep tool_result timestamp",
        "same_agent_key": "(sessionId, agentId-or-sessionId)",
        "causality_rule": "later call UUID must descend from a UUID in the Grep logical-message bundle",
        "windows_seconds": list(WINDOWS),
        "diagnostics": dict(sorted(diagnostics.items())),
        "retention": {str(window): dict(sorted(window_stats[window].items())) for window in WINDOWS},
    }


def main() -> int:
    args = parse_args()
    corpus = args.corpus.resolve()
    output_dir = args.output_dir.resolve()
    if not corpus.is_dir():
        raise SystemExit(f"corpus is not a directory: {corpus}")
    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(corpus.rglob("*.jsonl"), key=lambda path: str(path).casefold())
    file_snapshots: list[tuple[Path, int]] = []
    for path in files:
        try:
            file_snapshots.append((path, path.stat().st_size))
        except OSError:
            file_snapshots.append((path, -1))
    corpus_bytes_at_snapshot = sum(size for _, size in file_snapshots if size >= 0)
    snapshot_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    output_paths = {
        60: output_dir / "evalset_60.jsonl",
        300: output_dir / "evalset.jsonl",
        900: output_dir / "evalset_900.jsonl",
    }
    partial_paths = {window: path.with_suffix(path.suffix + ".partial") for window, path in output_paths.items()}
    handles = {
        window: partial_paths[window].open("w", encoding="utf-8", newline="\n")
        for window in WINDOWS
    }
    diagnostics: collections.Counter[str] = collections.Counter()
    window_stats = {window: collections.Counter() for window in WINDOWS}
    global_seen: set[tuple[str, str]] = set()
    execution_grep_ids: set[str] = set()
    linked_execution_grep_ids: set[str] = set()
    corpus_digest = hashlib.sha256()
    retention_path = output_dir / "retention.json"
    retention_partial_path = output_dir / "retention.partial.json"

    try:
        for file_index, (transcript, byte_limit) in enumerate(file_snapshots, 1):
            diagnostics["transcript_files_processed"] += 1
            if byte_limit < 0:
                diagnostics["transcript_file_errors"] += 1
                continue
            try:
                relative = transcript.relative_to(corpus).as_posix().encode("utf-8", errors="surrogateescape")
                corpus_digest.update(b"FILE\0" + relative + b"\0" + str(byte_limit).encode("ascii") + b"\0")
                calls, parents, message_bundles = scan_transcript(
                    transcript, global_seen, diagnostics, corpus_digest, byte_limit
                )
                diagnostics["transcript_files_succeeded"] += 1
                for call in calls:
                    if call["kind"] == "grep":
                        execution_grep_ids.add(call["tool_id"])
                        if call.get("result_seen") and call.get("result_ts") is not None:
                            linked_execution_grep_ids.add(call["tool_id"])
            except OSError as error:
                diagnostics["transcript_file_errors"] += 1
                print(f"warning: {transcript}: {error}", file=sys.stderr)
                continue

            by_agent: dict[tuple[str, Any], list[dict[str, Any]]] = collections.defaultdict(list)
            for call in calls:
                by_agent[(call["session"], call["agent"])].append(call)

            for agent_calls in by_agent.values():
                agent_calls.sort(key=lambda call: (call["ts"] if call["ts"] is not None else float("inf"), call["order"]))
                batches = group_batches(agent_calls, diagnostics)
                batch_times = [batch["ts"] for batch in batches]

                for grep in (call for call in agent_calls if call["kind"] == "grep"):
                    diagnostics["unique_grep_calls"] += 1
                    if grep.get("ts") is None:
                        diagnostics["grep_calls_invalid_timestamp"] += 1
                        for window in WINDOWS:
                            window_stats[window]["excluded_invalid_timestamp"] += 1
                        continue
                    if not grep.get("result_seen") or grep.get("result_ts") is None:
                        diagnostics["grep_calls_missing_result"] += 1
                        for window in WINDOWS:
                            window_stats[window]["excluded_missing_grep_result"] += 1
                        continue
                    timeline, saw_non_descendant, after_max = causal_timeline(
                        grep, batches, batch_times, parents, message_bundles, diagnostics
                    )
                    for window in WINDOWS:
                        stats = window_stats[window]
                        stats["grep_calls"] += 1
                        outcome = outcome_for_window(timeline, window)
                        if outcome is not None and outcome.get("unresolved"):
                            stats["excluded_unresolved_read_followup"] += 1
                        elif outcome is not None:
                            stats["resolvable"] += 1
                            if outcome["reads"]:
                                stats["positive_read"] += 1
                            else:
                                stats["failure_next_grep"] += 1
                            record = output_record(grep, outcome)
                            handles[window].write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
                        else:
                            stats["excluded_abandonment"] += 1
                            next_delta = min((item["delta"] for item in timeline), default=None)
                            if next_delta is not None and next_delta > window:
                                stats["abandoned_next_causal_action_after_window"] += 1
                            elif after_max is not None:
                                stats["abandoned_next_causal_action_after_900s"] += 1
                            elif saw_non_descendant:
                                stats["abandoned_only_non_descendant_action_observed"] += 1
                            else:
                                stats["abandoned_no_later_causal_read_or_grep"] += 1

            if file_index % max(args.progress_every, 1) == 0 or file_index == len(files):
                diagnostics["clone_dedup_grep_executions"] = len(execution_grep_ids)
                diagnostics["clone_dedup_linked_grep_executions"] = len(linked_execution_grep_ids)
                diagnostics["schema_ids_from_copied_history"] = (
                    diagnostics["unique_grep_calls"] - len(execution_grep_ids)
                )
                for handle in handles.values():
                    handle.flush()
                snapshot = retention_snapshot(
                    diagnostics, window_stats, len(files), file_index, False, corpus,
                    corpus_digest.hexdigest(), corpus_bytes_at_snapshot, snapshot_utc,
                )
                atomic_write_json(retention_partial_path, snapshot)
                kept = window_stats[300]["resolvable"]
                print(
                    f"{file_index}/{len(files)} transcripts; "
                    f"{diagnostics['unique_grep_calls']} unique Greps; {kept} retained at 300s",
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        for handle in handles.values():
            handle.close()

    complete = diagnostics["transcript_file_errors"] == 0
    final = retention_snapshot(
        diagnostics, window_stats, len(files), diagnostics["transcript_files_succeeded"],
        complete, corpus, corpus_digest.hexdigest(), corpus_bytes_at_snapshot, snapshot_utc,
    )
    for window in WINDOWS:
        stats = final["retention"][str(window)]
        total = diagnostics["unique_grep_calls"]
        stats["all_unique_grep_calls"] = total
        stats["all_excluded"] = total - stats.get("resolvable", 0)
        stats["retention_rate"] = stats.get("resolvable", 0) / total if total else None
    for window in WINDOWS:
        os.replace(partial_paths[window], output_paths[window])
    atomic_write_json(retention_path, final)
    retention_partial_path.unlink(missing_ok=True)
    print(json.dumps(final, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
