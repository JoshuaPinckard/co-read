#!/usr/bin/env python3
"""Run the retrieval benchmark's ripgrep and lexical-index arms.

The functions in this module intentionally return two different views of an
answer:

* ``ranked_paths`` contains normalised absolute paths in the *logical* target
  tree, so it can be compared directly with transcript labels.
* ``payload`` contains exactly what the replayed agent would see.  For the
  control this is ripgrep stdout with Claude Grep's fixed 500-column omission
  rule, followed by the recorded whole-line ``offset``/``head_limit`` window.
  Index responses use one small, stable text block per path and never expose
  scores or benchmark diagnostics.

No shell is involved in the control replay.  Every ripgrep invocation includes
``--no-config`` and ``--color=never`` so machine-local configuration and ANSI
escapes cannot change the benchmark.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import ntpath
import os
from pathlib import Path
import posixpath
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

try:  # Importable both as a package and as ``python instruments/.../arms.py``.
    from .index import build_index, connect_index, query_index
except ImportError:  # pragma: no cover - exercised by the command-line smoke test
    from index import build_index, connect_index, query_index


INDEX_ARMS = frozenset({"bm25", "ident_first", "bm25_pathboost", "bm25_legacy"})
ALL_ARMS = frozenset({"ripgrep", *INDEX_ARMS})
DEFAULT_TOP_K = 20
DEFAULT_TIMEOUT_SECONDS = 60.0
SNIPPET_BYTES = 400

_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_MSYS_ABSOLUTE = re.compile(r"^/([A-Za-z])(?:/(.*))?$")


def _query(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("query")
    return value if isinstance(value, Mapping) else record


def _as_bool(value: Any, name: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    raise ValueError(f"{name} must be boolean, not {value!r}")


def _nonnegative_integer(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer, not {value!r}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        result = int(value)
    else:
        raise ValueError(f"{name} must be a non-negative integer, not {value!r}")
    if result < 0:
        raise ValueError(f"{name} must be a non-negative integer, not {value!r}")
    return result


def _is_windows_root(value: str | os.PathLike[str]) -> bool:
    rendered = os.fspath(value)
    return bool(_WINDOWS_ABSOLUTE.match(rendered) or _MSYS_ABSOLUTE.match(rendered))


def _windows_path(value: str) -> str:
    """Convert ordinary/MSYS Windows spelling to an ntpath spelling."""

    match = _MSYS_ABSOLUTE.match(value.replace("\\", "/"))
    if match:
        tail = (match.group(2) or "").replace("/", "\\")
        return f"{match.group(1)}:\\{tail}" if tail else f"{match.group(1)}:\\"
    return value.replace("/", "\\")


def _normalise_logical(value: str, *, windows: bool) -> str:
    if windows:
        return ntpath.normcase(ntpath.normpath(_windows_path(value)))
    return posixpath.normpath(value.replace("\\", "/"))


def _is_absolute(value: str, *, windows: bool) -> bool:
    if windows:
        return bool(_WINDOWS_ABSOLUTE.match(value) or _MSYS_ABSOLUTE.match(value))
    return posixpath.isabs(value)


def _join_path(base: str, value: str, *, windows: bool) -> str:
    if windows:
        return ntpath.normpath(ntpath.join(base, _windows_path(value)))
    return posixpath.normpath(posixpath.join(base, value.replace("\\", "/")))


def _relative_under(candidate: str, root: str, *, windows: bool) -> str | None:
    """Return a safe POSIX relative path, or ``None`` when outside *root*."""

    module = ntpath if windows else posixpath
    candidate_norm = _normalise_logical(candidate, windows=windows)
    root_norm = _normalise_logical(root, windows=windows)
    try:
        common = module.commonpath((candidate_norm, root_norm))
    except ValueError:
        return None
    if (ntpath.normcase(common) if windows else common) != (
        ntpath.normcase(root_norm) if windows else root_norm
    ):
        return None
    relative = module.relpath(candidate_norm, root_norm)
    if relative in ("", "."):
        return ""
    relative = relative.replace("\\", "/")
    if relative == ".." or relative.startswith("../") or posixpath.isabs(relative):
        return None
    return posixpath.normpath(relative)


def _physical_relative(candidate: Path, root: Path) -> str | None:
    try:
        candidate_resolved = candidate.resolve(strict=False)
        root_resolved = root.resolve(strict=True)
        common = os.path.commonpath(
            (os.path.normcase(str(candidate_resolved)), os.path.normcase(str(root_resolved)))
        )
    except (OSError, ValueError):
        return None
    if common != os.path.normcase(str(root_resolved)):
        return None
    relative = os.path.relpath(candidate_resolved, root_resolved).replace("\\", "/")
    if relative in ("", "."):
        return ""
    if relative == ".." or relative.startswith("../"):
        return None
    return posixpath.normpath(relative)


def _source_path(root: Path, relative: str) -> Path:
    if not relative:
        return root
    return root.joinpath(*relative.split("/"))


def _logical_path(root: str, relative: str, *, windows: bool) -> str:
    candidate = root if not relative else _join_path(root, relative, windows=windows)
    return _normalise_logical(candidate, windows=windows)


def scope_for_record(
    record: Mapping[str, Any],
    logical_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Map a recorded Grep scope into a clean snapshot without escaping it.

    ``logical_root`` is the repository path used by transcript labels;
    ``source_root`` is the tree against which this replay actually runs.  The
    latter may be a temporary clean worktree.  Relative query paths are always
    resolved from the recorded ``cwd``.  An absolute path is accepted only when
    it is below one of these two representations of the same target tree.

    ``allowed_relative_paths`` is complete for a single-file scope.  Directory
    scopes intentionally leave it as ``None``; index-arm filtering derives the
    authoritative list from the index's ``files`` table, not from an unsafe or
    semantically different filesystem walk.
    """

    query = _query(record)
    logical_supplied = os.fspath(logical_root)
    source_supplied = Path(source_root)
    windows = _is_windows_root(logical_supplied)
    logical = _normalise_logical(logical_supplied, windows=windows)

    base_result: dict[str, Any] = {
        "in_scope": False,
        "available": False,
        "reason": None,
        "logical_root": logical,
        "source_root": str(source_supplied),
        "scope_relative_path": None,
        "logical_scope": None,
        "source_scope": None,
        "scope_kind": None,
        "allowed_relative_paths": None,
        "run_cwd": None,
        "rg_target": None,
        "rg_target_absolute": False,
    }

    try:
        source = source_supplied.resolve(strict=True)
    except OSError:
        base_result["reason"] = "source_root_unavailable"
        return base_result
    if not source.is_dir():
        base_result["reason"] = "source_root_not_directory"
        return base_result
    base_result["source_root"] = str(source)

    raw_cwd = record.get("cwd")
    cwd = os.fspath(raw_cwd) if isinstance(raw_cwd, (str, os.PathLike)) and os.fspath(raw_cwd) else None
    raw_scope_value = query.get("path")
    raw_scope = (
        os.fspath(raw_scope_value)
        if isinstance(raw_scope_value, (str, os.PathLike)) and os.fspath(raw_scope_value).strip()
        else "."
    )
    raw_scope = raw_scope.strip().strip('"')

    source_text = _normalise_logical(str(source), windows=windows)

    def representation(value: str) -> tuple[str, str] | None:
        """Return (relative, representation-name) for an absolute candidate."""

        relative = _relative_under(value, logical, windows=windows)
        if relative is not None:
            return relative, "logical"
        relative = _relative_under(value, source_text, windows=windows)
        if relative is not None:
            return relative, "source"
        return None

    cwd_mapping: tuple[str, str] | None = None
    normalised_cwd: str | None = None
    if cwd:
        if _is_absolute(cwd, windows=windows):
            normalised_cwd = _normalise_logical(cwd, windows=windows)
            cwd_mapping = representation(normalised_cwd)

    scope_was_absolute = _is_absolute(raw_scope, windows=windows)
    if scope_was_absolute:
        target = _normalise_logical(raw_scope, windows=windows)
    else:
        if normalised_cwd is None:
            base_result["reason"] = "cwd_missing_for_relative_scope"
            return base_result
        target = _join_path(normalised_cwd, raw_scope, windows=windows)

    target_mapping = representation(target)
    if target_mapping is None:
        base_result["reason"] = "query_scope_outside_root"
        return base_result
    relative, target_representation = target_mapping

    physical_scope = _source_path(source, relative).resolve(strict=False)
    # This second check catches an in-repository symlink/reparse point that
    # resolves beyond the clean snapshot boundary.
    checked_relative = _physical_relative(physical_scope, source)
    if checked_relative is None or checked_relative.replace("\\", "/").casefold() != relative.casefold():
        base_result["reason"] = "mapped_scope_outside_source_root"
        return base_result

    logical_scope = _logical_path(logical, relative, windows=windows)
    if physical_scope.is_file():
        kind = "file"
        available = True
        allowed: list[str] | None = [relative]
    elif physical_scope.is_dir():
        kind = "directory"
        available = True
        allowed = None
    elif physical_scope.exists():
        kind = "other"
        available = False
        allowed = []
    else:
        kind = "missing"
        available = False
        allowed = []

    physical_cwd = source
    cwd_is_mapped = False
    if cwd_mapping is not None:
        cwd_relative, _ = cwd_mapping
        candidate_cwd = _source_path(source, cwd_relative).resolve(strict=False)
        if _physical_relative(candidate_cwd, source) is not None and candidate_cwd.is_dir():
            physical_cwd = candidate_cwd
            cwd_is_mapped = True

    # Preserve relative-vs-absolute invocation spelling where doing so remains
    # safe.  It affects native rg path prefixes and therefore response bytes.
    if not scope_was_absolute and cwd_is_mapped:
        rg_target = raw_scope.replace("/", os.sep).replace("\\", os.sep)
        rg_target_absolute = False
    else:
        rg_target = str(physical_scope)
        rg_target_absolute = True

    base_result.update(
        {
            "in_scope": True,
            "available": available,
            "reason": None if available else f"source_scope_{kind}",
            "scope_relative_path": relative,
            "logical_scope": logical_scope,
            "source_scope": str(physical_scope),
            "scope_kind": kind,
            "allowed_relative_paths": allowed,
            "run_cwd": str(physical_cwd),
            "rg_target": rg_target,
            "rg_target_absolute": rg_target_absolute,
            "target_representation": target_representation,
        }
    )
    return base_result


def _glob_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ValueError(f"glob must be a string or list of strings, not {value!r}")


def ripgrep_argv(
    query: Mapping[str, Any],
    target: str,
    *,
    executable: str = "rg",
) -> list[str]:
    """Translate one recorded Claude Grep query into an argv-only rg replay."""

    pattern = query.get("pattern")
    if not isinstance(pattern, str):
        raise ValueError("query.pattern must be a string")
    output_mode = str(query.get("output_mode") or "files_with_matches")
    if output_mode not in {"files_with_matches", "content", "count"}:
        raise ValueError(f"unsupported output_mode: {output_mode!r}")

    # Claude Grep searches ordinary hidden project files (for example
    # .claude/settings.json) while excluding Git internals.
    argv = [
        executable,
        "--no-config",
        "--color=never",
        "--hidden",
        "--glob",
        "!**/.git/**",
        "--glob",
        "!**/.git",
        # Claude Code's Grep wrapper always invokes rg with
        # ``--max-columns 500``.  Without it, one minified/cache line can be
        # millions of bytes even though the agent saw
        # ``[Omitted long matching line]`` in the original tool result.
        "--max-columns",
        "500",
    ]
    if output_mode == "files_with_matches" or _as_bool(query.get("-l"), "-l"):
        argv.append("--files-with-matches")
    elif output_mode == "count":
        argv.append("--count")

    # Claude Grep's content mode defaults to line numbers when -n is absent.
    argv.append("--line-number" if _as_bool(query.get("-n"), "-n", default=True) else "--no-line-number")
    if _as_bool(query.get("-i"), "-i"):
        argv.append("--ignore-case")
    if _as_bool(query.get("-o"), "-o"):
        argv.append("--only-matching")
    if _as_bool(query.get("multiline"), "multiline"):
        argv.append("--multiline")
    if _as_bool(query.get("-a"), "-a"):
        argv.append("--text")

    context = query.get("-C") if query.get("-C") is not None else query.get("context")
    context_count = _nonnegative_integer(context, "context")
    after_count = _nonnegative_integer(query.get("-A"), "-A")
    before_count = _nonnegative_integer(query.get("-B"), "-B")
    if context_count is not None:
        argv.append(f"--context={context_count}")
    if after_count is not None:
        argv.append(f"--after-context={after_count}")
    if before_count is not None:
        argv.append(f"--before-context={before_count}")

    for glob in _glob_values(query.get("glob")):
        argv.extend(("--glob", glob))
    file_type = query.get("type")
    if file_type is not None:
        if not isinstance(file_type, str) or not file_type:
            raise ValueError(f"type must be a non-empty string, not {file_type!r}")
        argv.extend(("--type", file_type))

    # ``--`` is load-bearing: a recorded regex or path beginning with a dash is
    # data, never another benchmark-controlled option.
    argv.extend(("--", pattern, target))
    return argv


def _slice_response_lines(payload: bytes, query: Mapping[str, Any]) -> bytes:
    offset = _nonnegative_integer(query.get("offset"), "offset") or 0
    head_limit = _nonnegative_integer(query.get("head_limit"), "head_limit")
    # Claude's Grep tool uses an explicit zero as "unlimited".  The corpus has
    # non-empty recorded results for these calls, so slicing to zero lines
    # would create false control misses.
    if head_limit == 0:
        head_limit = None
    if not offset and head_limit is None:
        return payload
    lines = payload.splitlines(keepends=True)
    end = None if head_limit is None else offset + head_limit
    return b"".join(lines[offset:end])


def _logicalise_ripgrep_payload(payload: bytes, scope: Mapping[str, Any]) -> bytes:
    """Hide the temporary worktree path from an absolute-path replay.

    The control executes in a clean physical worktree, but the original agent
    searched the logical checkout path.  Leaving the random temporary prefix in
    stdout would inflate response bytes and leak a benchmark-only path.
    """

    if not payload or not scope.get("rg_target_absolute"):
        return payload
    physical = str(scope.get("source_scope") or "")
    logical = str(scope.get("logical_scope") or "")
    if not physical or not logical:
        return payload
    replacements = [
        (physical.encode("utf-8"), logical.encode("utf-8")),
        (physical.replace("\\", "/").encode("utf-8"), logical.replace("\\", "/").encode("utf-8")),
    ]
    result = payload
    for old, new in sorted(set(replacements), key=lambda pair: len(pair[0]), reverse=True):
        result = result.replace(old, new)
    return result


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_ripgrep_process(
    argv: Sequence[str],
    cwd: str,
    *,
    timeout_seconds: float,
    line_limit: int | None,
) -> tuple[bytes, bytes, int, bool, bool]:
    """Run rg without a shell, stopping after a complete visible line cap.

    Returns stdout, stderr, return code, timed-out, response-capped.  A capped
    child is intentionally terminated after the exact recorded response window
    has arrived and is therefore not an execution error.
    """

    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    capped = threading.Event()

    def consume_stdout() -> None:
        while True:
            piece = process.stdout.readline()
            if not piece:
                return
            stdout_chunks.append(piece)
            if line_limit is not None and len(stdout_chunks) >= line_limit:
                capped.set()
                return

    def consume_stderr() -> None:
        while piece := process.stderr.read(65_536):
            stderr_chunks.append(piece)

    stdout_thread = threading.Thread(target=consume_stdout, name="retrieval-rg-stdout", daemon=True)
    stderr_thread = threading.Thread(target=consume_stderr, name="retrieval-rg-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    response_capped = False
    while process.poll() is None:
        if capped.is_set():
            response_capped = True
            _terminate_process(process)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process(process)
            break
        capped.wait(min(0.02, remaining))
    if capped.is_set():
        response_capped = True
    if process.poll() is None:
        _terminate_process(process)
    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)
    return (
        b"".join(stdout_chunks),
        b"".join(stderr_chunks),
        int(process.returncode if process.returncode is not None else -1),
        timed_out,
        response_capped,
    )


def _empty_result(arm: str, started: float, error: str, metadata: dict[str, Any]) -> dict[str, Any]:
    payload = f"[error] {error}\n".encode("utf-8")
    return {
        "arm": arm,
        "ranked_paths": [],
        "response_bytes": len(payload),
        "response_sha256": hashlib.sha256(payload).hexdigest(),
        "payload": payload,
        "error": error,
        "latency_ms": (time.perf_counter() - started) * 1000.0,
        "metadata": metadata,
    }


def _result(
    arm: str,
    started: float,
    ranked_paths: list[str],
    payload: bytes,
    error: str | None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    if error:
        marker = f"[error] {error}\n".encode("utf-8")
        payload = payload + (b"\n" if payload and not payload.endswith(b"\n") else b"") + marker
    return {
        "arm": arm,
        "ranked_paths": ranked_paths,
        "response_bytes": len(payload),
        "response_sha256": hashlib.sha256(payload).hexdigest(),
        "payload": payload,
        "error": error,
        "latency_ms": (time.perf_counter() - started) * 1000.0,
        "metadata": metadata,
    }


def _path_line_to_relative(line: str, run_cwd: Path, source_root: Path) -> str | None:
    value = line.rstrip("\r")
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = run_cwd / candidate
    if not candidate.is_file():
        return None
    return _physical_relative(candidate, source_root)


def _prefixed_line_to_relative(line: str, run_cwd: Path, source_root: Path) -> str | None:
    """Extract rg's filename prefix, including ``C:\\...`` drive colons.

    Match lines use ``:`` separators and context lines use ``-``.  Rather than
    guessing where a Windows drive, a dash-heavy filename, or a colon in match
    text ends, candidates are tested from left to right and accepted only when
    the complete prefix names a real file inside the snapshot.
    """

    value = line.rstrip("\r")
    for index, character in enumerate(value):
        if character not in ":-" or (character == ":" and index == 1 and value[:1].isalpha()):
            continue
        prefix = value[:index]
        if not prefix:
            continue
        candidate = Path(prefix)
        if not candidate.is_absolute():
            candidate = run_cwd / candidate
        if not candidate.is_file():
            continue
        relative = _physical_relative(candidate, source_root)
        if relative is not None:
            return relative
    return None


def ripgrep_ranked_paths(
    payload: bytes,
    query: Mapping[str, Any],
    scope: Mapping[str, Any],
    logical_root: str | os.PathLike[str],
    source_root: str | os.PathLike[str],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[str]:
    """Recover unique result paths from visible native rg output."""

    if not payload or top_k <= 0:
        return []
    run_cwd = Path(str(scope["run_cwd"]))
    source = Path(source_root).resolve(strict=True)
    windows = _is_windows_root(logical_root)
    effective_files_mode = str(query.get("output_mode") or "files_with_matches") == "files_with_matches"
    effective_files_mode = effective_files_mode or _as_bool(query.get("-l"), "-l")

    relative_paths: list[str] = []
    if scope.get("scope_kind") == "file":
        # Native rg suppresses the filename in several single-file output
        # modes.  The explicit, already-validated scope is then unambiguous.
        relative_paths.append(str(scope["scope_relative_path"]))
    else:
        text = payload.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if not line or line == "--":
                continue
            if effective_files_mode:
                relative = _path_line_to_relative(line, run_cwd, source)
            else:
                relative = _prefixed_line_to_relative(line, run_cwd, source)
            if relative is not None:
                relative_paths.append(relative)

    seen: set[str] = set()
    ranked: list[str] = []
    logical = os.fspath(logical_root)
    for relative in relative_paths:
        key = relative.replace("\\", "/").casefold()
        if key in seen:
            continue
        seen.add(key)
        ranked.append(_logical_path(logical, relative, windows=windows))
        if len(ranked) >= top_k:
            break
    return ranked


def run_ripgrep(
    record: Mapping[str, Any],
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str],
    *,
    top_k: int = DEFAULT_TOP_K,
    executable: str = "rg",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the real ripgrep control for one eval-set record."""

    started = time.perf_counter()
    scope = scope_for_record(record, logical_root, source_root)
    metadata: dict[str, Any] = {"scope": scope, "tree": "provided_snapshot"}
    if not scope["in_scope"] or not scope["available"]:
        return _empty_result("ripgrep", started, str(scope["reason"]), metadata)
    if top_k < 0:
        return _empty_result("ripgrep", started, "top_k_must_be_nonnegative", metadata)

    query = _query(record)
    try:
        argv = ripgrep_argv(query, str(scope["rg_target"]), executable=executable)
        # Validate response-window arguments before spending time in rg.
        offset = _nonnegative_integer(query.get("offset"), "offset") or 0
        head_limit = _nonnegative_integer(query.get("head_limit"), "head_limit")
        if head_limit == 0:
            head_limit = None
        line_limit = None if head_limit is None else offset + head_limit
    except ValueError as error:
        return _empty_result("ripgrep", started, f"invalid_query: {error}", metadata)
    metadata["argv"] = argv

    try:
        native, native_stderr, return_code, timed_out, response_capped = _run_ripgrep_process(
            argv,
            str(scope["run_cwd"]),
            timeout_seconds=timeout_seconds,
            line_limit=line_limit,
        )
    except OSError as error:
        return _empty_result("ripgrep", started, f"ripgrep_unavailable: {error}", metadata)

    if timed_out:
        physical_payload = _slice_response_lines(native, query)
        payload = _logicalise_ripgrep_payload(physical_payload, scope)
        metadata.update({"timed_out": True, "native_stdout_bytes": len(native)})
        return _result(
            "ripgrep",
            started,
            [],
            payload,
            f"ripgrep_timeout_after_{timeout_seconds:g}s",
            metadata,
        )

    physical_payload = _slice_response_lines(native, query)
    payload = _logicalise_ripgrep_payload(physical_payload, scope)
    stderr = native_stderr.decode("utf-8", errors="replace").strip()
    metadata.update(
        {
            "exit_code": return_code,
            "native_stdout_bytes": len(native),
            "stderr_bytes": len(native_stderr),
            "response_capped": response_capped,
        }
    )
    if response_capped:
        return_code = 0
    if return_code > 1:
        detail = stderr[:2_000] or f"exit {return_code}"
        return _result(
            "ripgrep",
            started,
            [],
            payload,
            f"ripgrep_error: {detail}",
            metadata,
        )
    if return_code not in (0, 1):
        return _result(
            "ripgrep",
            started,
            [],
            payload,
            f"ripgrep_unexpected_exit_{return_code}",
            metadata,
        )

    # Exit 1 is ripgrep's documented, successful "no matches" result.
    ranked = ripgrep_ranked_paths(
        physical_payload,
        query,
        scope,
        logical_root,
        source_root,
        top_k=top_k,
    )
    return _result("ripgrep", started, ranked, payload, None, metadata)


def _brace_expand(pattern: str, *, limit: int = 2_048) -> list[str]:
    """Expand the brace alternation used by rg/globset query filters."""

    def first_brace(value: str) -> tuple[int, int] | None:
        start = -1
        depth = 0
        for index, character in enumerate(value):
            if character == "{" and (index == 0 or value[index - 1] != "\\"):
                if depth == 0:
                    start = index
                depth += 1
            elif character == "}" and depth:
                depth -= 1
                if depth == 0:
                    return start, index
        return None

    pending = [pattern]
    expanded: list[str] = []
    while pending:
        value = pending.pop()
        brace = first_brace(value)
        if brace is None:
            expanded.append(value)
            if len(expanded) > limit:
                raise ValueError("glob brace expansion is too large")
            continue
        start, end = brace
        inside = value[start + 1 : end]
        choices: list[str] = []
        depth = 0
        mark = 0
        for index, character in enumerate(inside):
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
            elif character == "," and depth == 0:
                choices.append(inside[mark:index])
                mark = index + 1
        choices.append(inside[mark:])
        if len(choices) == 1:
            expanded.append(value)
            continue
        for choice in reversed(choices):
            pending.append(value[:start] + choice + value[end + 1 :])
        if len(pending) + len(expanded) > limit:
            raise ValueError("glob brace expansion is too large")
    return expanded


def _translate_glob(pattern: str) -> str:
    pieces: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            end = index
            while end < len(pattern) and pattern[end] == "*":
                end += 1
            double = end - index >= 2
            if double and end < len(pattern) and pattern[end] == "/":
                pieces.append("(?:.*/)?")
                index = end + 1
            else:
                pieces.append(".*" if double else "[^/]*")
                index = end
            continue
        if character == "?":
            pieces.append("[^/]")
            index += 1
            continue
        if character == "[":
            end = pattern.find("]", index + 1)
            if end != -1:
                body = pattern[index + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                elif body.startswith("^"):
                    body = "\\" + body
                pieces.append("[" + body.replace("\\", "\\\\") + "]")
                index = end + 1
                continue
        # globset supports the common extglob spelling used once in the corpus.
        if character == "!" and index + 1 < len(pattern) and pattern[index + 1] == "(":
            end = pattern.find(")", index + 2)
            if end != -1:
                choices = "|".join(re.escape(item) for item in pattern[index + 2 : end].split("|"))
                pieces.append(f"(?!(?:{choices})(?=/|$))[^/]*")
                index = end + 1
                continue
        if character == "\\" and index + 1 < len(pattern):
            pieces.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        pieces.append(re.escape(character))
        index += 1
    return "".join(pieces)


@functools.lru_cache(maxsize=4_096)
def _compiled_glob(pattern: str) -> tuple[re.Pattern[str], ...]:
    anchored = pattern.startswith("/")
    body = pattern[1:] if anchored else pattern
    expressions: list[re.Pattern[str]] = []
    for expanded in _brace_expand(body):
        translated = _translate_glob(expanded)
        if anchored:
            expression = f"^{translated}$"
        elif "/" in expanded:
            expression = f"(?:^|.*/){translated}$"
        else:
            expression = f"(?:^|.*/){translated}$"
        expressions.append(re.compile(expression))
    return tuple(expressions)


def _matches_glob(subject: str, pattern: str) -> bool:
    normalised = subject.replace("\\", "/")
    return any(expression.search(normalised) is not None for expression in _compiled_glob(pattern))


def _glob_accepts(subject: str, values: Sequence[str]) -> bool:
    if not values:
        return True
    rules: list[tuple[bool, str]] = []
    for value in values:
        negative = value.startswith("!") and not value.startswith("!(")
        rules.append((negative, value[1:] if negative else value))
    positive_rules = [pattern for negative, pattern in rules if not negative]
    accepted = not positive_rules or any(_matches_glob(subject, pattern) for pattern in positive_rules)
    if not accepted:
        return False
    return not any(_matches_glob(subject, pattern) for negative, pattern in rules if negative)


@functools.lru_cache(maxsize=8)
def _ripgrep_types(executable: str = "rg") -> dict[str, tuple[str, ...]]:
    try:
        result = subprocess.run(
            [executable, "--no-config", "--type-list"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"cannot obtain ripgrep file types: {error}") from error
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"cannot obtain ripgrep file types: {detail or result.returncode}")
    types: dict[str, tuple[str, ...]] = {}
    for line in result.stdout.decode("utf-8", errors="strict").splitlines():
        name, separator, globs = line.partition(": ")
        if separator:
            types[name] = tuple(item.strip() for item in globs.split(",") if item.strip())
    return types


def _filter_subject(relative: str, scope: Mapping[str, Any], source_root: Path) -> str:
    absolute = _source_path(source_root, relative)
    if scope.get("rg_target_absolute"):
        return str(absolute).replace("\\", "/")
    run_cwd = Path(str(scope["run_cwd"]))
    return os.path.relpath(absolute, run_cwd).replace("\\", "/")


def _allowed_index_paths(
    conn: sqlite3.Connection,
    query: Mapping[str, Any],
    scope: Mapping[str, Any],
    source_root: Path,
    *,
    rg_executable: str = "rg",
) -> list[str] | None:
    relative_scope = str(scope["scope_relative_path"] or "").replace("\\", "/")
    kind = scope["scope_kind"]
    globs = _glob_values(query.get("glob"))
    type_name = query.get("type")
    type_globs: tuple[str, ...] = ()
    if type_name is not None:
        if not isinstance(type_name, str) or not type_name:
            raise ValueError(f"type must be a non-empty string, not {type_name!r}")
        type_globs = _ripgrep_types(rg_executable).get(type_name, ())
        if not type_globs:
            raise ValueError(f"unrecognised ripgrep file type: {type_name}")

    # The whole index already is the root scope.  Avoid constructing and
    # inserting a needless thousands-row temp allow-list in the common case.
    if kind == "directory" and not relative_scope and not globs and not type_globs:
        return None

    if kind == "file":
        # The validated scope is already the complete one-file allow-list.
        # Scanning every indexed path here would add O(repository files) work
        # to the common single-file query without changing semantics.
        subject = _filter_subject(relative_scope, scope, source_root)
        if globs and not _glob_accepts(subject, globs):
            return []
        if type_globs and not any(_matches_glob(subject, pattern) for pattern in type_globs):
            return []
        return [relative_scope]

    rows = conn.execute("SELECT path FROM files ORDER BY path COLLATE NOCASE, path").fetchall()
    prefix = relative_scope.rstrip("/")
    prefix_folded = prefix.casefold()
    allowed: list[str] = []
    for row in rows:
        relative = str(row[0]).replace("\\", "/")
        folded = relative.casefold()
        if kind == "file":
            if folded != prefix_folded:
                continue
        elif prefix and not folded.startswith(prefix_folded + "/"):
            continue
        subject = _filter_subject(relative, scope, source_root)
        if globs and not _glob_accepts(subject, globs):
            continue
        if type_globs and not any(_matches_glob(subject, pattern) for pattern in type_globs):
            continue
        allowed.append(relative)
    return allowed


def _utf8_prefix(text: str, byte_limit: int) -> bytes:
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_limit:
        return encoded
    return encoded[:byte_limit].decode("utf-8", errors="ignore").encode("utf-8")


def format_index_response(
    rows: Iterable[Mapping[str, Any]],
    logical_root: str | os.PathLike[str],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> tuple[list[str], bytes]:
    """Format one score-free, 400-byte-snippet region per unique path."""

    windows = _is_windows_root(logical_root)
    logical = os.fspath(logical_root)
    seen: set[str] = set()
    ranked: list[str] = []
    blocks: list[bytes] = []
    for row in rows:
        relative = str(row["path"]).replace("\\", "/")
        normalised = posixpath.normpath(relative)
        if normalised in ("", ".", "..") or normalised.startswith("../") or posixpath.isabs(normalised):
            raise ValueError(f"index returned unsafe relative path: {relative!r}")
        key = normalised.casefold()
        if key in seen:
            continue
        seen.add(key)
        start_line = int(row["start_line"])
        end_line = int(row["end_line"])
        header = f"{normalised}:{start_line}-{end_line}\n".encode("utf-8")
        snippet = _utf8_prefix(str(row.get("text", "")), SNIPPET_BYTES)
        blocks.append(header + snippet)
        ranked.append(_logical_path(logical, normalised, windows=windows))
        if len(ranked) >= top_k:
            break
    return ranked, b"\n\n".join(blocks)


def run_index_arm(
    arm: str,
    record: Mapping[str, Any],
    conn: sqlite3.Connection,
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str],
    *,
    top_k: int = DEFAULT_TOP_K,
    rg_executable: str = "rg",
) -> dict[str, Any]:
    """Run one of the four SQLite-backed lexical retrieval arms."""

    started = time.perf_counter()
    arm_name = arm.strip().lower()
    if arm_name not in INDEX_ARMS:
        raise ValueError(f"unknown index arm: {arm}")
    scope = scope_for_record(record, logical_root, source_root)
    metadata: dict[str, Any] = {"scope": scope}
    if not scope["in_scope"] or not scope["available"]:
        return _empty_result(arm_name, started, str(scope["reason"]), metadata)
    if top_k < 0:
        return _empty_result(arm_name, started, "top_k_must_be_nonnegative", metadata)
    if top_k == 0:
        return _result(arm_name, started, [], b"", None, metadata)

    query = _query(record)
    pattern = query.get("pattern")
    if not isinstance(pattern, str):
        return _empty_result(arm_name, started, "invalid_query: query.pattern must be a string", metadata)
    try:
        allowed = _allowed_index_paths(
            conn,
            query,
            scope,
            Path(source_root).resolve(strict=True),
            rg_executable=rg_executable,
        )
        metadata["allowed_path_count"] = None if allowed is None else len(allowed)
        rows = query_index(
            conn,
            pattern,
            arm_name,
            top_k,
            allowed_paths=allowed,
            ignore_case=_as_bool(query.get("-i"), "-i"),
        )
        # query_index ranks unique paths.  The formatter still collapses
        # defensively so a malformed/older index cannot inflate K with chunks.
        ranked, payload = format_index_response(rows, logical_root, top_k=top_k)
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as error:
        return _empty_result(arm_name, started, f"index_error: {error}", metadata)
    return _result(arm_name, started, ranked, payload, None, metadata)


def ripgrep(
    record: Mapping[str, Any],
    conn: sqlite3.Connection | None,
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str],
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    del conn
    return run_ripgrep(record, source_root, logical_root, top_k=top_k)


def bm25(
    record: Mapping[str, Any],
    conn: sqlite3.Connection,
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str],
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    return run_index_arm("bm25", record, conn, source_root, logical_root, top_k=top_k)


def ident_first(
    record: Mapping[str, Any],
    conn: sqlite3.Connection,
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str],
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    return run_index_arm("ident_first", record, conn, source_root, logical_root, top_k=top_k)


def bm25_pathboost(
    record: Mapping[str, Any],
    conn: sqlite3.Connection,
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str],
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    return run_index_arm("bm25_pathboost", record, conn, source_root, logical_root, top_k=top_k)


def bm25_legacy(
    record: Mapping[str, Any],
    conn: sqlite3.Connection,
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str],
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    return run_index_arm("bm25_legacy", record, conn, source_root, logical_root, top_k=top_k)


def run_arm(
    arm: str,
    record: Mapping[str, Any],
    conn: sqlite3.Connection | None,
    source_root: str | os.PathLike[str],
    logical_root: str | os.PathLike[str],
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Uniform public dispatcher used by ``score.py``."""

    arm_name = arm.strip().lower()
    if arm_name == "ripgrep":
        return run_ripgrep(record, source_root, logical_root, top_k=top_k)
    if arm_name not in INDEX_ARMS:
        raise ValueError(f"unknown arm: {arm}")
    if conn is None:
        started = time.perf_counter()
        return _empty_result(arm_name, started, "index_connection_required", {})
    return run_index_arm(arm_name, record, conn, source_root, logical_root, top_k=top_k)


def _self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="retrieval-arms-selftest-") as temporary:
        root = Path(temporary) / "snapshot"
        root.mkdir()
        init = subprocess.run(
            ["git", "-C", str(root), "init", "-q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
        if init.returncode:
            raise AssertionError(init.stderr.decode("utf-8", errors="replace"))
        (root / "src").mkdir()
        (root / "src" / "parse-lease.js").write_text(
            "const parseLease = value => value;\nconst needle = parseLease(input);\n",
            encoding="utf-8",
        )
        (root / "src" / "other.md").write_text("needle in markdown\n", encoding="utf-8")
        logical = r"C:\logical\repo"
        database_path = Path(temporary) / "index.sqlite"
        build_index(database_path, root, logical)
        conn = connect_index(database_path)
        try:
            record = {
                "cwd": logical,
                "query": {
                    "pattern": "parseLease",
                    "path": logical,
                    "glob": "*.js",
                    "type": None,
                    "output_mode": "content",
                    "-i": False,
                    "-n": True,
                    "head_limit": None,
                    "offset": None,
                },
            }
            results = {
                arm: run_arm(arm, record, conn, root, logical, top_k=20)
                for arm in ("ripgrep", "bm25", "ident_first", "bm25_pathboost", "bm25_legacy")
            }
            expected = ntpath.normcase(ntpath.join(logical, "src", "parse-lease.js"))
            for arm, result in results.items():
                if result["error"] is not None:
                    raise AssertionError(f"{arm} failed: {result['error']}")
                if result["ranked_paths"][:1] != [expected]:
                    raise AssertionError(f"{arm} path parsing/ranking failed: {result['ranked_paths']}")
                if result["response_bytes"] != len(result["payload"]):
                    raise AssertionError(f"{arm} byte accounting failed")
                if hashlib.sha256(result["payload"]).hexdigest() != result["response_sha256"]:
                    raise AssertionError(f"{arm} payload hash failed")
            for arm in INDEX_ARMS:
                text = results[arm]["payload"].decode("utf-8")
                if "src/parse-lease.js:1-" not in text or "score" in text.lower():
                    raise AssertionError(f"{arm} response formatter failed: {text!r}")
            outside = {
                "cwd": logical,
                "query": {"pattern": "needle", "path": r"C:\outside"},
            }
            rejected = scope_for_record(outside, logical, root)
            if rejected["in_scope"] or rejected["reason"] != "query_scope_outside_root":
                raise AssertionError(f"outside-root scope was accepted: {rejected}")
            argv = results["ripgrep"]["metadata"]["argv"]
            if argv[:3] != ["rg", "--no-config", "--color=never"] or "--" not in argv:
                raise AssertionError(f"unsafe rg argv: {argv}")
            return {
                "ok": True,
                "arms": sorted(results),
                "ripgrep_response_bytes": results["ripgrep"]["response_bytes"],
                "index_response_bytes": {
                    arm: results[arm]["response_bytes"] for arm in sorted(INDEX_ARMS)
                },
            }
        finally:
            conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("self-test",))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _parser().parse_args(argv)
    print(json.dumps(_self_test(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
