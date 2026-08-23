"""Mediating shim for the posture experiment.

The shim deliberately does only three jobs: expose an auditable read/test
interface to an agent, coordinate task-lifetime byte claims, and guard writes
through Codex lifecycle hooks.  It is experimental apparatus, not a product
implementation.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Sequence


MAX_BYTE = 2**63 - 1
FORBIDDEN_PARTS = {".git", ".codex", ".posture"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextlib.contextmanager
def database_connection(path: Path):
    """Commit or roll back a short SQLite unit of work, then close it.

    ``sqlite3.Connection``'s own context manager does not close the handle,
    which leaves database files locked on Windows after short-lived hooks.
    """

    connection = connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with database_connection(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monotonic_ns INTEGER NOT NULL,
                timestamp_utc TEXT NOT NULL,
                draw_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                arm TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claims (
                draw_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                arm TEXT NOT NULL,
                claims_json TEXT NOT NULL,
                radius_json TEXT NOT NULL,
                acquired_monotonic_ns INTEGER NOT NULL,
                acquired_utc TEXT NOT NULL,
                PRIMARY KEY (draw_id, agent_id)
            );
            CREATE TABLE IF NOT EXISTS write_snapshots (
                draw_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                tool_use_id TEXT NOT NULL,
                path TEXT NOT NULL,
                existed INTEGER NOT NULL,
                content BLOB,
                sha256 TEXT,
                PRIMARY KEY (draw_id, agent_id, tool_use_id, path)
            );
            """
        )


class Context:
    def __init__(self) -> None:
        self.database = Path(required_env("POSTURE_DB"))
        self.draw_id = required_env("POSTURE_DRAW_ID")
        self.agent_id = required_env("POSTURE_AGENT_ID")
        self.arm = required_env("POSTURE_ARM")
        self.workspace_key = required_env("POSTURE_WORKSPACE_KEY")
        self.root = Path(required_env("POSTURE_WORKTREE")).resolve(strict=True)
        self.radius_path = Path(required_env("POSTURE_RADIUS"))
        self.python = Path(required_env("POSTURE_TEST_PYTHON"))
        initialize_database(self.database)

    def event(self, event_type: str, details: dict[str, Any]) -> None:
        with database_connection(self.database) as connection:
            insert_event(connection, self, event_type, details)


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def insert_event(connection: sqlite3.Connection, context: Context, event_type: str, details: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO events
            (monotonic_ns, timestamp_utc, draw_id, agent_id, arm, event_type, details_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            time.monotonic_ns(),
            utc_now(),
            context.draw_id,
            context.agent_id,
            context.arm,
            event_type,
            json_text(details),
        ),
    )


def normalize_relative(root: Path, value: str, *, allow_missing: bool = False) -> tuple[str, Path]:
    raw = Path(value.replace("\\", "/"))
    if raw.is_absolute() or ".." in raw.parts or any(part in FORBIDDEN_PARTS for part in raw.parts):
        raise ValueError(f"path is outside the observable task surface: {value}")
    resolved = (root / raw).resolve(strict=not allow_missing)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError(f"path escapes worktree: {value}") from error
    if any(part in FORBIDDEN_PARTS for part in Path(relative).parts):
        raise ValueError(f"path is outside the observable task surface: {value}")
    return relative, resolved


def validate_interval(start: int, end: int) -> None:
    if start < 0 or end <= start or end > MAX_BYTE:
        raise ValueError(f"invalid half-open byte interval [{start}, {end})")


def intervals_intersect(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["path"] == right["path"]
        and max(int(left["start"]), int(right["start"]))
        < min(int(left["end"]), int(right["end"]))
    )


def load_radius(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def expand_radius(claims: list[dict[str, Any]], configuration: dict[str, Any]) -> list[dict[str, Any]]:
    radius: list[dict[str, Any]] = [dict(claim) for claim in claims]
    seen = {(item["path"], item["start"], item["end"]) for item in radius}
    files = configuration.get("files", {})
    for claim in claims:
        for candidate in files.get(claim["path"], []):
            item = {
                "path": candidate["path"],
                "start": 0,
                "end": MAX_BYTE,
                "score": candidate["score"],
                "source": claim["path"],
            }
            key = (item["path"], item["start"], item["end"])
            if key not in seen:
                radius.append(item)
                seen.add(key)
    return radius


def conflict_rows(
    connection: sqlite3.Connection,
    context: Context,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT agent_id, claims_json, radius_json FROM claims WHERE draw_id = ? AND agent_id <> ?",
        (context.draw_id, context.agent_id),
    ).fetchall()
    conflicts: list[dict[str, Any]] = []
    for row in rows:
        live_radius = json.loads(row["radius_json"])
        hits = [
            {"claim": claim, "live_radius": radius}
            for claim in incoming
            for radius in live_radius
            if intervals_intersect(claim, radius)
        ]
        if hits:
            conflicts.append({"agent_id": row["agent_id"], "intersections": hits})
    return conflicts


def acquire_claims(context: Context, claims: list[dict[str, Any]]) -> dict[str, Any]:
    if active_claims(context):
        reason = "claims are atomic and task-lifetime; a second claim is forbidden"
        context.event("claim_denied", {"claims": claims, "reason": reason})
        raise ValueError(reason)
    configuration = load_radius(context.radius_path)
    radius = expand_radius(claims, configuration)
    wait_started = time.monotonic()
    blocked_logged = False
    all_conflicting_agents: set[str] = set()
    while True:
        with database_connection(context.database) as connection:
            connection.execute("BEGIN IMMEDIATE")
            conflicts = conflict_rows(connection, context, claims)
            all_conflicting_agents.update(conflict["agent_id"] for conflict in conflicts)
            if context.arm == "blocking" and conflicts:
                if not blocked_logged:
                    insert_event(
                        connection,
                        context,
                        "block",
                        {"claims": claims, "conflicts": conflicts, "reason": "claim_intersects_live_radius"},
                    )
                    blocked_logged = True
                connection.commit()
            else:
                acquired_ns = time.monotonic_ns()
                acquired_utc = utc_now()
                connection.execute(
                    """
                    INSERT INTO claims
                        (draw_id, agent_id, arm, claims_json, radius_json, acquired_monotonic_ns, acquired_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(draw_id, agent_id) DO UPDATE SET
                        arm=excluded.arm,
                        claims_json=excluded.claims_json,
                        radius_json=excluded.radius_json,
                        acquired_monotonic_ns=excluded.acquired_monotonic_ns,
                        acquired_utc=excluded.acquired_utc
                    """,
                    (
                        context.draw_id,
                        context.agent_id,
                        context.arm,
                        json_text(claims),
                        json_text(radius),
                        acquired_ns,
                        acquired_utc,
                    ),
                )
                wait_seconds = time.monotonic() - wait_started
                insert_event(
                    connection,
                    context,
                    "claim",
                    {
                        "claims": claims,
                        "conflicts": conflicts,
                        "conflicting_agents_seen": sorted(all_conflicting_agents),
                        "collision_exposed": bool(all_conflicting_agents or conflicts),
                        "wait_seconds": wait_seconds,
                    },
                )
                insert_event(
                    connection,
                    context,
                    "radius",
                    {
                        "claims": claims,
                        "radius": radius,
                        "top_k": configuration.get("top_k"),
                        "threshold": configuration.get("threshold"),
                        "provenance": configuration.get("provenance"),
                    },
                )
                if blocked_logged:
                    insert_event(
                        connection,
                        context,
                        "release",
                        {"kind": "blocking_wait", "wait_seconds": wait_seconds},
                    )
                connection.commit()
                return {
                    "claims": claims,
                    "radius": radius,
                    "collision_exposed": bool(all_conflicting_agents or conflicts),
                    "wait_seconds": wait_seconds,
                }
        time.sleep(0.1)


def release_claims(context: Context, *, reason: str) -> None:
    with database_connection(context.database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT acquired_monotonic_ns, claims_json FROM claims WHERE draw_id = ? AND agent_id = ?",
            (context.draw_id, context.agent_id),
        ).fetchone()
        connection.execute(
            "DELETE FROM claims WHERE draw_id = ? AND agent_id = ?",
            (context.draw_id, context.agent_id),
        )
        if row is not None:
            held = max(0.0, (time.monotonic_ns() - row["acquired_monotonic_ns"]) / 1e9)
            insert_event(
                connection,
                context,
                "release",
                {"kind": "claim", "reason": reason, "held_seconds": held, "claims": json.loads(row["claims_json"])},
            )
        connection.commit()


def active_claims(context: Context) -> list[dict[str, Any]]:
    with database_connection(context.database) as connection:
        row = connection.execute(
            "SELECT claims_json FROM claims WHERE draw_id = ? AND agent_id = ?",
            (context.draw_id, context.agent_id),
        ).fetchone()
    return [] if row is None else json.loads(row["claims_json"])


def claim_covers_file(context: Context, relative: str, resolved: Path) -> bool:
    del resolved
    return any(
        claim["path"] == relative
        and int(claim["start"]) == 0
        and int(claim["end"]) == MAX_BYTE
        for claim in active_claims(context)
    )


def parse_patch_paths(command: str) -> list[str]:
    prefixes = (
        "*** Update File: ",
        "*** Add File: ",
        "*** Delete File: ",
        "*** Move to: ",
    )
    result: list[str] = []
    for line in command.splitlines():
        for prefix in prefixes:
            if line.startswith(prefix):
                value = line[len(prefix) :].strip()
                if value not in result:
                    result.append(value)
    return result


def snapshot_paths(context: Context, paths: list[tuple[str, Path]], tool_use_id: str) -> None:
    with database_connection(context.database) as connection:
        for relative, resolved in paths:
            existed = resolved.is_file()
            content = resolved.read_bytes() if existed else None
            connection.execute(
                """
                INSERT OR REPLACE INTO write_snapshots
                    (draw_id, agent_id, tool_use_id, path, existed, content, sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.draw_id,
                    context.agent_id,
                    tool_use_id,
                    relative,
                    int(existed),
                    content,
                    sha256_bytes(content) if content is not None else None,
                ),
            )


def unified_patch(path: str, before: bytes | None, after: bytes | None) -> tuple[str | None, bool]:
    try:
        before_text = "" if before is None else before.decode("utf-8")
        after_text = "" if after is None else after.decode("utf-8")
    except UnicodeDecodeError:
        return None, True
    lines = difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=f"a/{path}" if before is not None else "/dev/null",
        tofile=f"b/{path}" if after is not None else "/dev/null",
        n=3,
    )
    return "".join(lines), False


def finish_write(context: Context, tool_use_id: str, response: Any) -> None:
    with database_connection(context.database) as connection:
        rows = connection.execute(
            """
            SELECT path, existed, content, sha256 FROM write_snapshots
            WHERE draw_id = ? AND agent_id = ? AND tool_use_id = ? ORDER BY path
            """,
            (context.draw_id, context.agent_id, tool_use_id),
        ).fetchall()
        for row in rows:
            relative, resolved = normalize_relative(context.root, row["path"], allow_missing=True)
            after = resolved.read_bytes() if resolved.is_file() else None
            before = row["content"] if row["existed"] else None
            patch, binary = unified_patch(relative, before, after)
            insert_event(
                connection,
                context,
                "write",
                {
                    "path": relative,
                    "before_sha256": row["sha256"],
                    "after_sha256": sha256_bytes(after) if after is not None else None,
                    "before_exists": bool(row["existed"]),
                    "after_exists": after is not None,
                    "changed": before != after,
                    "binary": binary,
                    "patch": patch,
                    "tool_use_id": tool_use_id,
                    "tool_response": response,
                },
            )
def hook_allow(additional_context: str | None = None) -> None:
    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }
    if additional_context:
        output["hookSpecificOutput"]["additionalContext"] = additional_context
    print(json.dumps(output))


def hook_deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def safe_wrapper_command(command: str, python_executable: Path | None = None) -> bool:
    # Click's tracked paths contain no spaces. Keeping the grammar deliberately
    # narrow rejects PowerShell expansion, comments, pipes, redirects, command
    # substitution, and command chaining instead of trying to sanitize them.
    token = r"[A-Za-z0-9_./\\:*?=+\-,\[\]@]+"
    single_quoted = r"'[A-Za-z0-9_./\\:*?=+\-,\[\]@ ]*'"
    interpreter = (
        r"python(?:\.exe)?"
        if python_executable is None
        else rf'(?:"{re.escape(str(python_executable.resolve()))}"|{re.escape(str(python_executable.resolve()))})'
    )
    grammar = rf"{interpreter}[ ]+\.posture[/\\]agent_tool\.py(?:[ ]+(?:{token}|{single_quoted}))*[ ]*"
    return re.fullmatch(grammar, command, flags=re.IGNORECASE) is not None


def hook_pre(context: Context, payload: dict[str, Any]) -> int:
    tool_name = payload.get("tool_name")
    tool_use_id = str(payload.get("tool_use_id", "unknown"))
    tool_input = payload.get("tool_input") or {}
    command = str(tool_input.get("command", ""))
    if tool_name in {"Bash", "shell_command"}:
        context.event("shell_request", {"tool_use_id": tool_use_id, "command": command})
        if safe_wrapper_command(command, context.python):
            hook_allow()
        else:
            reason = "Use only `python .posture/agent_tool.py ...` for shell operations; direct shell access is outside the measured interface."
            context.event("shell_denied", {"tool_use_id": tool_use_id, "command": command, "reason": reason})
            hook_deny(reason)
        return 0
    if tool_name != "apply_patch":
        reason = f"Local tool {tool_name!r} is outside the measured Bash/apply_patch interface."
        context.event("tool_denied", {"tool_use_id": tool_use_id, "tool_name": tool_name, "reason": reason})
        hook_deny(reason)
        return 0
    raw_paths = parse_patch_paths(command)
    if not raw_paths:
        reason = "The patch did not identify any supported file operation."
        context.event("write_denied", {"tool_use_id": tool_use_id, "reason": reason})
        hook_deny(reason)
        return 0
    resolved_paths: list[tuple[str, Path]] = []
    try:
        for raw in raw_paths:
            relative, resolved = normalize_relative(context.root, raw, allow_missing=True)
            if not claim_covers_file(context, relative, resolved):
                raise ValueError(f"no active whole-file byte claim covers {relative}")
            resolved_paths.append((relative, resolved))
    except (OSError, ValueError) as error:
        reason = str(error)
        context.event("write_denied", {"tool_use_id": tool_use_id, "paths": raw_paths, "reason": reason})
        hook_deny(reason)
        return 0
    paths = [relative for relative, _ in resolved_paths]
    snapshot_paths(context, resolved_paths, tool_use_id)
    context.event(
        "write_attempt",
        {
            "tool_use_id": tool_use_id,
            "paths": paths,
            "write_mutex": "none",
            "patch_command": command,
        },
    )
    hook_allow()
    return 0


def hook_post(context: Context, payload: dict[str, Any]) -> int:
    tool_name = payload.get("tool_name")
    tool_use_id = str(payload.get("tool_use_id", "unknown"))
    response = payload.get("tool_response")
    if tool_name == "apply_patch":
        finish_write(context, tool_use_id, response)
    elif tool_name in {"Bash", "shell_command"}:
        context.event("shell_complete", {"tool_use_id": tool_use_id, "tool_response": response})
    else:
        context.event("tool_post_unexpected", {"tool_use_id": tool_use_id, "tool_name": tool_name})
    return 0


def hook_session(context: Context, payload: dict[str, Any]) -> int:
    context.event(
        "session_start",
        {
            "session_id": payload.get("session_id"),
            "cwd": payload.get("cwd"),
            "model": payload.get("model"),
            "permission_mode": payload.get("permission_mode"),
            "source": payload.get("source"),
        },
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart"}}))
    return 0


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )
    return [item.decode("utf-8", errors="surrogateescape") for item in result.stdout.split(b"\0") if item]


def tool_list(context: Context, prefix: str | None) -> int:
    files = tracked_files(context.root)
    if prefix:
        normalized = prefix.replace("\\", "/").rstrip("/") + "/"
        files = [path for path in files if path.startswith(normalized)]
    context.event("read", {"kind": "listing", "prefix": prefix, "result_count": len(files)})
    print("\n".join(files))
    return 0


def tool_read(context: Context, value: str, start: int, end: int | None) -> int:
    relative, path = normalize_relative(context.root, value)
    content = path.read_bytes()
    selected_end = len(content) if end is None else min(end, len(content))
    if start < 0 or selected_end < start:
        raise ValueError("invalid read interval")
    payload = content[start:selected_end]
    context.event(
        "read",
        {"kind": "file", "path": relative, "start": start, "end": selected_end, "size": len(content), "sha256": sha256_bytes(content)},
    )
    print(f"# {relative} bytes [{start},{selected_end}) of {len(content)}")
    sys.stdout.flush()
    sys.stdout.buffer.write(payload)
    if payload and not payload.endswith(b"\n"):
        print()
    return 0


def tool_size(context: Context, value: str) -> int:
    relative, path = normalize_relative(context.root, value, allow_missing=True)
    size = path.stat().st_size if path.exists() else 0
    context.event("read", {"kind": "metadata", "path": relative, "size": size})
    print(json.dumps({"path": relative, "size": size, "claim": f"{relative}:*"}))
    return 0


def tool_search(context: Context, pattern: str, prefixes: list[str]) -> int:
    expression = re.compile(pattern)
    files = tracked_files(context.root)
    normalized_prefixes = [prefix.replace("\\", "/").rstrip("/") for prefix in prefixes]
    if normalized_prefixes:
        files = [path for path in files if any(path == prefix or path.startswith(prefix + "/") for prefix in normalized_prefixes)]
    matches: list[str] = []
    for relative in files:
        try:
            _, path = normalize_relative(context.root, relative)
            content = path.read_bytes()
            text = content.decode("utf-8")
        except (UnicodeDecodeError, OSError, ValueError):
            continue
        context.event(
            "read",
            {
                "kind": "search",
                "path": relative,
                "pattern": pattern,
                "size": len(content),
                "sha256": sha256_bytes(content),
            },
        )
        for line_number, line in enumerate(text.splitlines(), start=1):
            if expression.search(line):
                matches.append(f"{relative}:{line_number}:{line}")
    print("\n".join(matches))
    return 0


def git_observe(context: Context, arguments: Sequence[str], kind: str) -> int:
    result = subprocess.run(
        ["git", *arguments],
        cwd=context.root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        shell=False,
    )
    context.event("read", {"kind": kind, "arguments": list(arguments), "exit_code": result.returncode})
    sys.stdout.buffer.write(result.stdout)
    return result.returncode


def validate_pytest_nodeids(root: Path, values: list[str]) -> list[str]:
    if len(values) > 12:
        raise ValueError("at most 12 pytest node IDs may be requested")
    for value in values:
        if value.startswith("-") or any(character in value for character in "*?{}|;&$`><\"\r\n"):
            raise ValueError(f"pytest options and shell syntax are forbidden: {value!r}")
        path_part = value.split("::", 1)[0].replace("\\", "/")
        if not path_part.startswith("tests/") or not path_part.endswith(".py"):
            raise ValueError(f"pytest argument must be a tests/*.py node ID: {value!r}")
        normalize_relative(root, path_part)
    return values


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            shell=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, 15)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    if process.poll() is None:
        process.kill()


def copy_test_snapshot(context: Context, destination: Path) -> str:
    def ignored(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {".git", ".codex", ".posture", "__pycache__", ".pytest_cache"}}

    shutil.copytree(context.root, destination, ignore=ignored)
    digest = hashlib.sha256()
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        relative = path.relative_to(destination).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def tool_test(context: Context, pytest_arguments: list[str]) -> int:
    nodeids = validate_pytest_nodeids(context.root, pytest_arguments)
    test_temp_root = Path(required_env("POSTURE_TEST_TEMP_ROOT")).resolve(strict=True)
    python_compat = Path(required_env("POSTURE_PYTHON_COMPAT")).resolve(strict=True)
    if not (python_compat / "sitecustomize.py").is_file():
        raise ValueError("POSTURE_PYTHON_COMPAT does not contain sitecustomize.py")
    if (python_compat / "__pycache__").exists():
        raise ValueError("loadable compatibility bytecode is forbidden")
    timeout = float(os.environ.get("POSTURE_TEST_TIMEOUT", "150"))
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix=f"test-{context.agent_id}-", dir=test_temp_root) as directory:
        snapshot = Path(directory) / "tree"
        snapshot_sha256 = copy_test_snapshot(context, snapshot)
        source_root = snapshot / "src" if (snapshot / "src").is_dir() else snapshot
        shadows = {
            candidate.resolve()
            for candidate in (snapshot / "sitecustomize.py", source_root / "sitecustomize.py")
            if candidate.is_file()
        }
        if shadows:
            raise ValueError(
                f"worktree shadows frozen sitecustomize.py: {sorted(map(str, shadows))}"
            )
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(python_compat), str(source_root))
        )
        command = [str(context.python), "-m", "pytest", "-p", "no:cacheprovider", *nodeids]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=snapshot,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        started = time.monotonic()
        timed_out = False
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(process)
            stdout, _ = process.communicate()
        elapsed = time.monotonic() - started
        exit_code = 124 if timed_out else int(process.returncode)
        output = stdout.decode("utf-8", errors="replace")
    context.event(
        "test",
        {
            "command": command,
            "exit_code": exit_code,
            "elapsed_seconds": elapsed,
            "timed_out": timed_out,
            "snapshot_sha256": snapshot_sha256,
            "output": output[-20000:],
        },
    )
    sys.stdout.write(output)
    return exit_code


def parse_claim_specs(context: Context, values: list[str]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for value in values:
        if value.endswith(":*"):
            raw_path = value[:-2]
            relative, _ = normalize_relative(context.root, raw_path, allow_missing=True)
            end = MAX_BYTE
            start = 0
        else:
            raw_path, raw_start, raw_end = value.rsplit(":", 2)
            relative, _ = normalize_relative(context.root, raw_path, allow_missing=True)
            start, end = int(raw_start), int(raw_end)
        validate_interval(start, end)
        claims.append({"path": relative, "start": start, "end": end})
    if not claims:
        raise ValueError("at least one claim is required")
    return sorted(claims, key=lambda item: (item["path"], item["start"], item["end"]))


def build_agent_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measured interface for posture experiment agents")
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list")
    listing.add_argument("prefix", nargs="?")
    reading = subparsers.add_parser("read")
    reading.add_argument("path")
    reading.add_argument("--start", type=int, default=0)
    reading.add_argument("--end", type=int)
    sizing = subparsers.add_parser("size")
    sizing.add_argument("path")
    search = subparsers.add_parser("search")
    search.add_argument("pattern")
    search.add_argument("prefix", nargs="*")
    subparsers.add_parser("status")
    subparsers.add_parser("diff")
    test = subparsers.add_parser("test")
    test.add_argument("pytest_argument", nargs="*")
    claim = subparsers.add_parser("claim")
    claim.add_argument("spec", nargs="+")
    subparsers.add_parser("claims")
    return parser


def agent_main(arguments: Sequence[str] | None = None) -> int:
    args = build_agent_parser().parse_args(arguments)
    context = Context()
    if args.command == "list":
        return tool_list(context, args.prefix)
    if args.command == "read":
        return tool_read(context, args.path, args.start, args.end)
    if args.command == "size":
        return tool_size(context, args.path)
    if args.command == "search":
        return tool_search(context, args.pattern, args.prefix)
    if args.command == "status":
        return git_observe(context, ["status", "--short"], "git_status")
    if args.command == "diff":
        return git_observe(context, ["diff", "--", ".", ":(exclude).codex", ":(exclude).posture"], "git_diff")
    if args.command == "test":
        return tool_test(context, args.pytest_argument)
    if args.command == "claim":
        print(json.dumps(acquire_claims(context, parse_claim_specs(context, args.spec)), indent=2, sort_keys=True))
        return 0
    if args.command == "claims":
        print(json.dumps(active_claims(context), indent=2, sort_keys=True))
        return 0
    raise AssertionError(args.command)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init")
    initialize.add_argument("database", type=Path)
    hook = subparsers.add_parser("hook")
    hook.add_argument("phase", choices=("pre", "post", "session"))
    subparsers.add_parser("agent")
    args, remainder = parser.parse_known_args(arguments)
    if args.command == "init":
        initialize_database(args.database)
        return 0
    if args.command == "agent":
        return agent_main(remainder)
    context = Context()
    payload = json.load(sys.stdin)
    if args.phase == "pre":
        return hook_pre(context, payload)
    if args.phase == "post":
        return hook_post(context, payload)
    return hook_session(context, payload)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Pre-hook failures deny the action. A post/session failure cannot undo
        # work, so persist apparatus invalidity and emit an event-appropriate
        # response that the runner will treat as a failed draw.
        if len(sys.argv) >= 2 and sys.argv[1] == "hook":
            reason = f"posture hook failed: {type(error).__name__}: {error}"
            with contextlib.suppress(Exception):
                Context().event("apparatus_invalid", {"phase": sys.argv[2] if len(sys.argv) > 2 else None, "reason": reason})
            phase = sys.argv[2] if len(sys.argv) > 2 else "pre"
            if phase == "pre":
                hook_deny(reason)
            elif phase == "post":
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reason}}))
            else:
                print(json.dumps({"continue": False, "stopReason": reason, "hookSpecificOutput": {"hookEventName": "SessionStart"}}))
            raise SystemExit(0)
        raise
