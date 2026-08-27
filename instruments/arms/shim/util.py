from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from . import SCHEMA_VERSION


class ShimError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_json(value) + b"\n")
    os.replace(temporary, path)


def safe_relative(value: str) -> str:
    value = value.replace("\\", "/")
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ShimError(f"unsafe repository-relative path: {value!r}")
    return pure.as_posix()


def tree_path(root: Path, relative: str) -> Path:
    relative = safe_relative(relative)
    candidate = root.joinpath(*PurePosixPath(relative).parts).resolve()
    resolved_root = root.resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ShimError(f"path escapes tree: {relative!r}")
    return candidate


@dataclasses.dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    started_monotonic: float
    finished_monotonic: float
    timed_out: bool = False
    launch_error: str | None = None

    @property
    def actual_seconds(self) -> float:
        return max(0.0, self.finished_monotonic - self.started_monotonic)

    @property
    def finished(self) -> bool:
        return not self.timed_out and self.launch_error is None


@dataclasses.dataclass
class RunningProcess:
    argv: tuple[str, ...]
    process: subprocess.Popen[bytes] | None
    started_monotonic: float
    launch_error: str | None = None


def process_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    if extra:
        environment.update({str(k): str(v) for k, v in extra.items()})
    return environment


def start_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> RunningProcess:
    started = time.monotonic()
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            [str(item) for item in argv],
            cwd=cwd,
            env=dict(env or process_environment()),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **options,
        )
        if stdin is not None:
            assert process.stdin is not None
            process.stdin.write(stdin)
            process.stdin.close()
            process.stdin = None
        return RunningProcess(tuple(map(str, argv)), process, started)
    except OSError as error:
        return RunningProcess(tuple(map(str, argv)), None, started, repr(error))


def kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def finish_process(running: RunningProcess, timeout_seconds: float) -> ProcessResult:
    if running.process is None:
        now = time.monotonic()
        return ProcessResult(
            running.argv,
            None,
            b"",
            (running.launch_error or "launch failed").encode("utf-8"),
            running.started_monotonic,
            now,
            False,
            running.launch_error,
        )
    # The fairness timeout is a launch-to-finish budget.  ``finish_process``
    # may be called after polling or other concurrent setup, so starting a new
    # full timeout here would silently extend an agent's allowance.
    elapsed = max(0.0, time.monotonic() - running.started_monotonic)
    remaining = max(0.0, timeout_seconds - elapsed)
    try:
        stdout, stderr = running.process.communicate(timeout=remaining)
        return ProcessResult(
            running.argv,
            running.process.returncode,
            stdout,
            stderr,
            running.started_monotonic,
            time.monotonic(),
        )
    except subprocess.TimeoutExpired as error:
        kill_process_tree(running.process)
        stdout, stderr = running.process.communicate()
        return ProcessResult(
            running.argv,
            None,
            stdout or error.stdout or b"",
            stderr or error.stderr or b"",
            running.started_monotonic,
            time.monotonic(),
            True,
        )


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float = 300.0,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    check: bool = False,
) -> ProcessResult:
    result = finish_process(
        start_process(argv, cwd=cwd, env=env, stdin=stdin), timeout_seconds
    )
    if check and (
        result.returncode != 0 or result.timed_out or result.launch_error is not None
    ):
        message = result.stderr.decode("utf-8", errors="replace")[-3000:]
        raise ShimError(f"command failed: {list(argv)!r}: {message}")
    return result


def run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float = 300.0,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    effective_env = process_environment()
    if env is not None:
        effective_env.update(env)
    return run_process(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            *args,
        ],
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        env=effective_env,
        check=check,
    )


def git_text(args: Sequence[str], *, cwd: Path, check: bool = True) -> str:
    return run_git(args, cwd=cwd, check=check).stdout.decode(
        "utf-8", errors="replace"
    ).strip()


@dataclasses.dataclass(frozen=True)
class FileState:
    kind: str
    mode: int
    data: bytes

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.data)


Snapshot = dict[str, FileState]


def snapshot_tree(root: Path) -> Snapshot:
    result: Snapshot = {}
    for candidate in sorted(root.rglob("*")):
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            continue
        if relative == ".git" or relative.startswith(".git/"):
            continue
        for retry in range(2):
            try:
                if candidate.is_symlink():
                    result[relative] = FileState(
                        "symlink", candidate.lstat().st_mode, os.readlink(candidate).encode()
                    )
                elif candidate.is_file():
                    result[relative] = FileState(
                        "file", candidate.stat().st_mode, candidate.read_bytes()
                    )
                break
            except (FileNotFoundError, PermissionError):
                # Polling is observational and may race an agent's atomic
                # replace/create/delete. Retry once, then let the next poll or
                # final snapshot observe the stable state.
                if retry == 0:
                    time.sleep(0.005)
                    continue
                break
    return result


def _line_offsets(lines: Sequence[bytes]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def _trim_span(
    old: bytes, new: bytes, old_start: int, new_start: int
) -> tuple[int, int, int, int, bytes, bytes]:
    prefix = 0
    bound = min(len(old), len(new))
    while prefix < bound and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    old_remaining = len(old) - prefix
    new_remaining = len(new) - prefix
    while (
        suffix < old_remaining
        and suffix < new_remaining
        and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]
    ):
        suffix += 1
    old_end = len(old) - suffix if suffix else len(old)
    new_end = len(new) - suffix if suffix else len(new)
    return (
        old_start + prefix,
        old_start + old_end,
        new_start + prefix,
        new_start + new_end,
        old[prefix:old_end],
        new[prefix:new_end],
    )


def byte_regions(old: bytes, new: bytes) -> list[dict[str, Any]]:
    if old == new:
        return []
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    old_offsets = _line_offsets(old_lines)
    new_offsets = _line_offsets(new_lines)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    regions: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_block = b"".join(old_lines[i1:i2])
        new_block = b"".join(new_lines[j1:j2])
        # Refine changed line blocks at byte granularity.  Merely trimming a
        # common prefix and suffix coalesces multiple independent edits on one
        # line into a single broad claim and creates false contested writes.
        byte_matcher = difflib.SequenceMatcher(
            None, old_block, new_block, autojunk=False
        )
        for byte_tag, bi1, bi2, bj1, bj2 in byte_matcher.get_opcodes():
            if byte_tag == "equal":
                continue
            old_segment = old_block[bi1:bi2]
            new_segment = new_block[bj1:bj2]
            old_start = old_offsets[i1] + bi1
            old_end = old_offsets[i1] + bi2
            context_window = 64
            left_context = old[max(0, old_start - context_window) : old_start]
            right_context = old[old_end : old_end + context_window]
            regions.append(
                {
                    "operation": byte_tag,
                    "coordinate_space": "baseline-bytes",
                    "old_start": old_start,
                    "old_end": old_end,
                    "new_start": new_offsets[j1] + bj1,
                    "new_end": new_offsets[j1] + bj2,
                    "old_region_sha256": sha256_bytes(old_segment),
                    "new_region_sha256": sha256_bytes(new_segment),
                    "content_anchor": {
                        "version": "bounded-flanking-context-v1",
                        "window_bytes": context_window,
                        "left_bytes": len(left_context),
                        "right_bytes": len(right_context),
                        "left_sha256": sha256_bytes(left_context),
                        "right_sha256": sha256_bytes(right_context),
                    },
                }
            )
    return regions


def diff_snapshots(before: Snapshot, after: Snapshot) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative in sorted(set(before) | set(after)):
        old = before.get(relative)
        new = after.get(relative)
        if old == new:
            continue
        old_data = old.data if old else b""
        new_data = new.data if new else b""
        records.append(
            {
                "path": relative,
                "before_kind": old.kind if old else "missing",
                "after_kind": new.kind if new else "missing",
                "before_mode": old.mode if old else None,
                "after_mode": new.mode if new else None,
                "before_sha256": sha256_bytes(old_data),
                "after_sha256": sha256_bytes(new_data),
                "before_bytes": len(old_data),
                "after_bytes": len(new_data),
                "regions": byte_regions(old_data, new_data),
            }
        )
    return records


def regions_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    a1, a2 = int(left["old_start"]), int(left["old_end"])
    b1, b2 = int(right["old_start"]), int(right["old_end"])
    if a1 == a2 and b1 == b2:
        return a1 == b1
    if a1 == a2:
        return b1 <= a1 < b2
    if b1 == b2:
        return a1 <= b1 < a2
    return max(a1, b1) < min(a2, b2)


class Clock:
    def timestamp(self) -> str:
        raise NotImplementedError

    def monotonic_ns(self) -> int:
        raise NotImplementedError


class RealClock(Clock):
    def timestamp(self) -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class LogicalClock(Clock):
    def __init__(self, start: dt.datetime | None = None) -> None:
        self._start = start or dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        self._tick = 0
        self._lock = threading.Lock()

    def _next(self) -> int:
        with self._lock:
            value = self._tick
            self._tick += 1
            return value

    def timestamp(self) -> str:
        value = self._start + dt.timedelta(milliseconds=self._next())
        return value.isoformat().replace("+00:00", "Z")

    def monotonic_ns(self) -> int:
        return self._next() * 1_000_000


EVENT_ALLOWED_OPS = frozenset(
    {
        "launch",
        "poll",
        "write-set",
        "declare",
        "merge",
        "validate",
        "retry",
        "escalate",
        "complete",
    }
)


def event_semantic_errors(row: Mapping[str, Any]) -> list[str]:
    """Return schema errors that can be recomputed from one retained event."""

    errors: list[str] = []
    if row.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version={row.get('schema_version')!r}")
    for field in ("run_id", "draw_id", "stratum", "timestamp_utc"):
        if not isinstance(row.get(field), str) or not row.get(field):
            errors.append(f"{field} is not a nonempty string")
    for field in ("sequence", "monotonic_ns"):
        if not isinstance(row.get(field), int) or int(row[field]) < 0:
            errors.append(f"{field} is not a nonnegative integer")
    if not isinstance(row.get("site"), dict):
        errors.append("site is not an object")
    if not isinstance(row.get("arm"), dict):
        errors.append("arm is not an object")
    if not isinstance(row.get("principal"), (dict, str)):
        errors.append("principal is neither an object nor a string")
    subject = row.get("subject")
    if not isinstance(subject, dict):
        errors.append("subject is not an object")
    else:
        for field in ("cli", "version"):
            if not isinstance(subject.get(field), str) or not subject.get(field):
                errors.append(f"subject.{field} is not a nonempty string")
        if not isinstance(subject.get("model"), (str, type(None))):
            errors.append("subject.model is neither a string nor null")
    op = row.get("op")
    if op not in EVENT_ALLOWED_OPS:
        errors.append(f"unsupported op={op!r}")
    if not isinstance(row.get("detail"), dict):
        errors.append("detail is not an object")
    for field in ("previous_event_sha256", "event_sha256"):
        value = row.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            errors.append(f"{field} is not a lowercase SHA-256")
    paths = row.get("paths")
    if not isinstance(paths, list):
        errors.append("paths is not an array")
        return errors
    for path_index, item in enumerate(paths):
        label = f"paths[{path_index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} is not an object")
            continue
        if not isinstance(item.get("path"), str) or not item.get("path"):
            errors.append(f"{label}.path is not a nonempty string")
        if op not in {"poll", "write-set"}:
            continue
        for field in ("before_sha256", "after_sha256"):
            value = item.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                errors.append(f"{label}.{field} is not a lowercase SHA-256")
        for field in ("before_bytes", "after_bytes"):
            if not isinstance(item.get(field), int) or int(item[field]) < 0:
                errors.append(f"{label}.{field} is not a nonnegative integer")
        regions = item.get("regions")
        if not isinstance(regions, list):
            errors.append(f"{label}.regions is not an array")
            continue
        for region_index, region in enumerate(regions):
            region_label = f"{label}.regions[{region_index}]"
            if not isinstance(region, dict):
                errors.append(f"{region_label} is not an object")
                continue
            for field in ("old_start", "old_end", "new_start", "new_end"):
                if not isinstance(region.get(field), int) or int(region[field]) < 0:
                    errors.append(f"{region_label}.{field} is not a nonnegative integer")
            for start, end in (("old_start", "old_end"), ("new_start", "new_end")):
                if isinstance(region.get(start), int) and isinstance(region.get(end), int):
                    if region[start] > region[end]:
                        errors.append(f"{region_label}.{start} exceeds {end}")
            for field in ("old_region_sha256", "new_region_sha256"):
                value = region.get(field)
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                ):
                    errors.append(f"{region_label}.{field} is not a lowercase SHA-256")
            anchor = region.get("content_anchor")
            if not isinstance(anchor, dict):
                errors.append(f"{region_label}.content_anchor is not an object")
            else:
                if anchor.get("version") != "bounded-flanking-context-v1":
                    errors.append(f"{region_label}.content_anchor.version is invalid")
                for field in ("window_bytes", "left_bytes", "right_bytes"):
                    if not isinstance(anchor.get(field), int) or int(anchor[field]) < 0:
                        errors.append(
                            f"{region_label}.content_anchor.{field} is not a "
                            "nonnegative integer"
                        )
                for field in ("left_sha256", "right_sha256"):
                    value = anchor.get(field)
                    if (
                        not isinstance(value, str)
                        or len(value) != 64
                        or any(
                            character not in "0123456789abcdef" for character in value
                        )
                    ):
                        errors.append(
                            f"{region_label}.content_anchor.{field} is not a "
                            "lowercase SHA-256"
                        )
    return errors


class EventLog:
    """Single-writer append-only, fsynced, hash-chained JSONL."""

    ALLOWED_OPS = EVENT_ALLOWED_OPS

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        draw_id: str,
        site: Mapping[str, Any],
        arm: Mapping[str, Any],
        stratum: str,
        clock: Clock,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise ShimError(f"refusing to rewrite append-only log: {self.path}")
        self._handle = self.path.open("xb")
        self._run_id = run_id
        self._draw_id = draw_id
        self._site = dict(site)
        self._arm = dict(arm)
        self._stratum = stratum
        self._clock = clock
        self._sequence = 0
        self._previous = "0" * 64

    def emit(
        self,
        op: str,
        *,
        principal: Mapping[str, Any] | str,
        subject: Mapping[str, Any] | None = None,
        paths: Sequence[Mapping[str, Any]] = (),
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if op not in self.ALLOWED_OPS:
            raise ShimError(f"unsupported event operation: {op!r}")
        normalized_subject = dict(
            subject or {"cli": "harness", "version": "1", "model": None}
        )
        missing_subject = {"cli", "version", "model"} - set(normalized_subject)
        if missing_subject:
            raise ShimError(f"event subject lacks fields: {sorted(missing_subject)}")
        normalized_paths = sorted(
            (dict(item) for item in paths), key=lambda item: item.get("path", "")
        )
        for item in normalized_paths:
            if not isinstance(item.get("path"), str) or not item["path"]:
                raise ShimError(f"event path record lacks a path: {item!r}")
            if op not in {"poll", "write-set"}:
                continue
            if not isinstance(item.get("regions"), list):
                raise ShimError(f"mechanical path record lacks regions: {item['path']}")
            for region in item["regions"]:
                required = {
                    "old_start",
                    "old_end",
                    "new_start",
                    "new_end",
                    "old_region_sha256",
                    "new_region_sha256",
                }
                if not isinstance(region, dict) or required - set(region):
                    raise ShimError(f"mechanical region is incomplete: {item['path']}")
        event = {
            "schema_version": SCHEMA_VERSION,
            "sequence": self._sequence,
            "run_id": self._run_id,
            "draw_id": self._draw_id,
            "site": self._site,
            "arm": self._arm,
            "stratum": self._stratum,
            "principal": principal,
            "subject": normalized_subject,
            "op": op,
            "paths": normalized_paths,
            "timestamp_utc": self._clock.timestamp(),
            "monotonic_ns": self._clock.monotonic_ns(),
            "detail": dict(detail or {}),
            "previous_event_sha256": self._previous,
        }
        event_hash = sha256_bytes(canonical_json(event))
        event["event_sha256"] = event_hash
        semantic_errors = event_semantic_errors(event)
        if semantic_errors:
            raise ShimError("invalid event: " + "; ".join(semantic_errors))
        self._handle.write(canonical_json(event) + b"\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._previous = event_hash
        self._sequence += 1
        return event

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def copy_path_state(source: Path, destination: Path) -> None:
    if source.is_symlink():
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        destination.symlink_to(os.readlink(source))
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    elif destination.exists() or destination.is_symlink():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()


def stable_path_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(record) for record in records), key=lambda row: row["path"])
