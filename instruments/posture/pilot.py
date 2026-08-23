"""Run the preregistered n=5 posture pilot with concurrent Codex agents."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from instruments.posture import shim
from instruments.posture.gate_repository import normalized_junit
from instruments.posture.task_builder import atomic_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = PROJECT_ROOT / "exploratory" / "posture" / "TASKS.json"
DEFAULT_SCHEDULE = PROJECT_ROOT / "exploratory" / "posture" / "pilot" / "SCHEDULE.json"
DEFAULT_RUN_ROOT = PROJECT_ROOT / "exploratory" / "posture" / "pilot" / "runs"
DEFAULT_SMOKE_ROOT = PROJECT_ROOT / "exploratory" / "posture" / "smoke" / "live-hook-contract"
DEFAULT_SMOKE_SUMMARY = DEFAULT_SMOKE_ROOT / "summary.json"

APPARATUS_FILES = (
    Path("instruments/posture/analyze.py"),
    Path("instruments/posture/pilot.py"),
    Path("instruments/posture/prepare.py"),
    Path("instruments/posture/gate_repository.py"),
    Path("instruments/posture/radius.py"),
    Path("instruments/posture/shim.py"),
    Path("instruments/posture/task_builder.py"),
    Path("instruments/replay/extract.py"),
    Path("instruments/replay/common.py"),
    Path("instruments/replay/replay.py"),
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    stdin: bytes | None = None,
    environment: dict[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        list(arguments),
        cwd=cwd,
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
        shell=False,
        timeout=timeout,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {arguments!r}\n"
            + result.stdout.decode("utf-8", errors="replace")
            + result.stderr.decode("utf-8", errors="replace")
        )
    return result


def git(repository: Path, *arguments: str, stdin: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return run(["git", "-c", "core.longpaths=true", *arguments], cwd=repository, stdin=stdin, check=check)


def output_text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


def relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")


def project_path(value: str | Path, *, strict: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve(strict=strict)


def command_version(arguments: Sequence[str], *, cwd: Path = PROJECT_ROOT) -> str:
    result = run(arguments, cwd=cwd, check=False, timeout=30)
    return (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()


def pinned_artifacts(tasks_path: Path, tasks: dict[str, Any]) -> dict[str, str]:
    """Hash every file whose contents can change a measured draw."""

    paths = {tasks_path.resolve(), *(PROJECT_ROOT / path for path in APPARATUS_FILES)}
    design_path = project_path(tasks["design_path"])
    paths.add(design_path)
    compatibility = project_path(tasks["repository"]["python_compat"])
    compatibility_sitecustomize = compatibility / "sitecustomize.py"
    if (compatibility / "__pycache__").exists():
        raise RuntimeError("loadable compatibility bytecode is forbidden")
    paths.add(compatibility_sitecustomize)
    for bundle in tasks.get("bundles", []):
        radius = bundle.get("radius", {})
        if isinstance(radius, dict) and radius.get("path"):
            paths.add((PROJECT_ROOT / radius["path"]).resolve())
        for task in bundle.get("tasks", []):
            for key, value in task.items():
                if key.endswith("_patch") and isinstance(value, str) and value:
                    paths.add((PROJECT_ROOT / value).resolve())
    missing = [str(path) for path in sorted(paths) if not path.is_file()]
    if missing:
        raise RuntimeError(f"pinned apparatus files are missing: {missing}")
    hashes = {relative(path): sha256_file(path) for path in sorted(paths)}
    if hashes.get(relative(design_path)) != tasks.get("design_sha256"):
        raise RuntimeError("DESIGN.json differs from the task-construction record")
    compatibility_path = relative(compatibility_sitecustomize)
    expected_compatibility_hash = tasks["repository"].get(
        "python_compat_sitecustomize_sha256"
    )
    if hashes.get(compatibility_path) != expected_compatibility_hash:
        raise RuntimeError(
            "Python compatibility layer differs from the task-construction record"
        )
    return hashes


def test_pythonpath(repository_config: dict[str, Any], worktree: Path) -> str:
    compatibility = project_path(repository_config["python_compat"])
    sitecustomize = compatibility / "sitecustomize.py"
    if not sitecustomize.is_file():
        raise RuntimeError(f"Python compatibility layer is missing: {sitecustomize}")
    if (compatibility / "__pycache__").exists():
        raise RuntimeError("loadable compatibility bytecode is forbidden")
    source_root = worktree / "src" if (worktree / "src").is_dir() else worktree
    shadows = {
        candidate.resolve()
        for candidate in (worktree / "sitecustomize.py", source_root / "sitecustomize.py")
        if candidate.is_file()
    }
    if shadows:
        raise RuntimeError(f"worktree shadows frozen sitecustomize.py: {sorted(map(str, shadows))}")
    return os.pathsep.join((str(compatibility), str(source_root)))


def codex_runtime_artifacts(codex_path: Path) -> dict[str, str]:
    """Hash the launcher and the actual npm/native execution chain it selects."""

    package_root = codex_path.parent / "node_modules" / "@openai" / "codex"
    if not package_root.is_dir():
        raise RuntimeError(
            f"cannot resolve the installed @openai/codex package from {codex_path}"
        )
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is unavailable for the Codex npm launcher")
    node_path = Path(node).resolve(strict=True)
    node_arch = command_version([str(node_path), "-p", "process.arch"])
    target_by_host = {
        ("win32", "x64"): ("codex-win32-x64", "x86_64-pc-windows-msvc"),
        ("win32", "arm64"): ("codex-win32-arm64", "aarch64-pc-windows-msvc"),
        ("linux", "x64"): ("codex-linux-x64", "x86_64-unknown-linux-musl"),
        ("linux", "arm64"): ("codex-linux-arm64", "aarch64-unknown-linux-musl"),
        ("darwin", "x64"): ("codex-darwin-x64", "x86_64-apple-darwin"),
        ("darwin", "arm64"): ("codex-darwin-arm64", "aarch64-apple-darwin"),
    }
    platform_key = sys.platform
    selection = target_by_host.get((platform_key, node_arch))
    if selection is None:
        raise RuntimeError(
            f"unsupported host while pinning the Codex runtime: {platform_key}/{node_arch}"
        )
    platform_package, target = selection
    platform_root = package_root / "node_modules" / "@openai" / platform_package
    vendor_root = platform_root / "vendor" / target
    required = {
        codex_path,
        package_root / "package.json",
        package_root / "bin" / "codex.js",
        platform_root / "package.json",
    }
    required.update(path for path in vendor_root.rglob("*") if path.is_file())
    required.add(node_path)
    missing = sorted(str(path) for path in required if not path.is_file())
    if missing:
        raise RuntimeError(f"Codex runtime files are missing: {missing}")
    return {str(path.resolve()): sha256_file(path) for path in sorted(required)}


def runtime_versions(tasks: dict[str, Any]) -> dict[str, str]:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI is unavailable")
    test_python = project_path(tasks["repository"]["test_python"])
    codex_path = Path(codex).resolve(strict=True)
    codex_artifacts = codex_runtime_artifacts(codex_path)
    harness_python = Path(sys.executable).resolve(strict=True)
    compatibility = project_path(tasks["repository"]["python_compat"])
    compatibility_environment = os.environ.copy()
    compatibility_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    compatibility_environment["PYTHONPATH"] = str(compatibility)
    compatibility_script = (
        "import collections, json, sitecustomize, sys; "
        "print(json.dumps({'python': sys.version, 'sitecustomize': sitecustomize.__file__, "
        "'iterable_module': collections.Iterable.__module__, "
        "'iterable_name': collections.Iterable.__name__}, sort_keys=True))"
    )
    compatibility_result = run(
        [str(test_python), "-c", compatibility_script],
        cwd=PROJECT_ROOT,
        environment=compatibility_environment,
    )
    compatibility_probe = json.loads(
        compatibility_result.stdout.decode("utf-8", errors="strict").strip()
    )
    expected_activation = tasks["repository"].get("python_compat_activation", {})
    for key in ("python", "sitecustomize", "iterable_module", "iterable_name"):
        if compatibility_probe.get(key) != expected_activation.get(key):
            raise RuntimeError(f"Python compatibility activation changed for {key}")
    snapshot = {
        "codex_executable": str(codex_path),
        "codex_executable_sha256": sha256_file(codex_path),
        "codex_runtime_artifact_sha256": json.dumps(
            codex_artifacts, sort_keys=True, separators=(",", ":")
        ),
        "codex_version": command_version([codex, "--version"]),
        "git_version": command_version(["git", "--version"]),
        "harness_python_executable": str(harness_python),
        "harness_python_sha256": sha256_file(harness_python),
        "harness_python_version": sys.version,
        "test_python_executable": str(test_python),
        "test_python_sha256": sha256_file(test_python),
        "test_python_version": command_version([str(test_python), "--version"]),
        "pytest_version": command_version([str(test_python), "-m", "pytest", "--version"]),
        "test_environment_freeze": command_version(
            [str(test_python), "-m", "pip", "freeze", "--all"]
        ),
        "test_python_compatibility_probe": json.dumps(
            compatibility_probe, sort_keys=True, separators=(",", ":")
        ),
    }
    preparation_runtime = tasks.get("preparation_test_runtime")
    comparable_keys = (
        "test_python_executable",
        "test_python_sha256",
        "test_python_version",
        "pytest_version",
        "test_environment_freeze",
    )
    current_test_runtime = {key: snapshot[key] for key in comparable_keys}
    if preparation_runtime != current_test_runtime:
        raise RuntimeError(
            "test interpreter/package environment differs from task construction"
        )
    return snapshot


def apparatus_fingerprint(artifacts: dict[str, str], versions: dict[str, str]) -> str:
    payload = json.dumps(
        {"artifact_sha256": artifacts, "runtime_versions": versions},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def verify_schedule_apparatus(
    tasks_path: Path,
    tasks: dict[str, Any],
    schedule: dict[str, Any],
) -> dict[str, Any]:
    expected_draws = deterministic_draws(tasks)
    if schedule.get("draws") != expected_draws:
        raise RuntimeError("schedule draw plan differs from the seeded canonical plan")
    if schedule.get("draw_count") != len(expected_draws):
        raise RuntimeError("schedule draw count differs from the seeded canonical plan")
    if schedule.get("draws_per_cell") != tasks["pilot"]["draws_per_cell"]:
        raise RuntimeError("schedule cell count differs from TASKS.json")
    if schedule.get("random_seed") != tasks["pilot"]["random_seed"]:
        raise RuntimeError("schedule random seed differs from TASKS.json")
    current_artifacts = pinned_artifacts(tasks_path, tasks)
    current_versions = runtime_versions(tasks)
    current_smoke = verify_live_smoke(tasks_path, tasks, current_artifacts, current_versions)
    if schedule.get("live_hook_smoke") != current_smoke:
        raise RuntimeError("schedule live-hook smoke record is missing or stale")
    expected_artifacts = schedule.get("artifact_sha256")
    expected_versions = schedule.get("runtime_versions")
    if current_artifacts != expected_artifacts:
        raise RuntimeError("pinned artifact hashes changed after schedule preregistration")
    if current_versions != expected_versions:
        raise RuntimeError("pinned runtime versions changed after schedule preregistration")
    fingerprint = apparatus_fingerprint(current_artifacts, current_versions)
    if fingerprint != schedule.get("apparatus_fingerprint"):
        raise RuntimeError("apparatus fingerprint does not match the preregistered schedule")
    return {
        "verified": True,
        "artifact_sha256": current_artifacts,
        "runtime_versions": current_versions,
        "fingerprint": fingerprint,
    }


def verify_snapshot_unchanged(
    tasks_path: Path,
    tasks: dict[str, Any],
    expected_artifacts: dict[str, str],
    expected_versions: dict[str, str],
) -> dict[str, Any]:
    """Fail closed if files or runtimes change while one measured operation runs."""

    try:
        current_artifacts = pinned_artifacts(tasks_path, tasks)
        current_versions = runtime_versions(tasks)
    except Exception as error:  # retained verbatim as apparatus evidence
        return {"verified": False, "error": f"{type(error).__name__}: {error}"}
    artifacts_match = current_artifacts == expected_artifacts
    versions_match = current_versions == expected_versions
    return {
        "verified": artifacts_match and versions_match,
        "artifact_hashes_match": artifacts_match,
        "runtime_versions_match": versions_match,
        "fingerprint": apparatus_fingerprint(current_artifacts, current_versions),
    }


@dataclass
class ApparatusContext:
    draw_id: str
    agent_id: str
    arm: str


def apparatus_event(
    database: Path,
    draw_id: str,
    arm: str,
    event_type: str,
    details: dict[str, Any],
    *,
    agent_id: str = "_harness",
) -> None:
    context = ApparatusContext(draw_id=draw_id, agent_id=agent_id, arm=arm)
    with shim.database_connection(database) as connection:
        shim.insert_event(connection, context, event_type, details)  # type: ignore[arg-type]


def release_agent(database: Path, draw_id: str, arm: str, agent_id: str, reason: str) -> None:
    context = ApparatusContext(draw_id=draw_id, agent_id=agent_id, arm=arm)
    with shim.database_connection(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT acquired_monotonic_ns, claims_json FROM claims WHERE draw_id = ? AND agent_id = ?",
            (draw_id, agent_id),
        ).fetchone()
        connection.execute(
            "DELETE FROM claims WHERE draw_id = ? AND agent_id = ?",
            (draw_id, agent_id),
        )
        if row is not None:
            held = max(0.0, (time.monotonic_ns() - row["acquired_monotonic_ns"]) / 1e9)
            shim.insert_event(  # type: ignore[arg-type]
                connection,
                context,
                "release",
                {
                    "kind": "claim",
                    "reason": reason,
                    "held_seconds": held,
                    "claims": json.loads(row["claims_json"]),
                },
            )


def deterministic_draws(tasks: dict[str, Any]) -> list[dict[str, Any]]:
    pilot = tasks["pilot"]
    seed = int(str(pilot["random_seed"]), 0)
    generator = random.Random(seed)
    arms = list(pilot["arms"])
    draws: list[dict[str, Any]] = []
    ordinal = 0
    for block in range(1, int(pilot["draws_per_cell"]) + 1):
        # Interleave bundles by block so time drift does not align with a
        # single overlap factor.
        bundle_order = list(tasks["bundles"])
        generator.shuffle(bundle_order)
        for bundle in bundle_order:
            arm_order = list(arms)
            generator.shuffle(arm_order)
            for arm_position, arm in enumerate(arm_order, start=1):
                ordinal += 1
                merge_order = [task["task_id"] for task in bundle["tasks"]]
                generator.shuffle(merge_order)
                launch_order = [task["task_id"] for task in bundle["tasks"]]
                generator.shuffle(launch_order)
                draw_id = f"{ordinal:03d}-{bundle['bundle_id']}-b{block:02d}-{arm}"
                draws.append(
                    {
                        "ordinal": ordinal,
                        "draw_id": draw_id,
                        "bundle_id": bundle["bundle_id"],
                        "factor": bundle["factor"],
                        "block": block,
                        "arm": arm,
                        "arm_position_within_bundle_block": arm_position,
                        "merge_order": merge_order,
                        "launch_order": launch_order,
                    }
                )
    return draws


def make_schedule(tasks_path: Path, schedule_path: Path) -> dict[str, Any]:
    tasks = load_json(tasks_path)
    pilot = tasks["pilot"]
    draws = deterministic_draws(tasks)
    artifacts = pinned_artifacts(tasks_path, tasks)
    versions = runtime_versions(tasks)
    smoke = verify_live_smoke(tasks_path, tasks, artifacts, versions)
    schedule = {
        "schema_version": 1,
        "measurement": "posture-pilot-preregistered-schedule",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
        "tasks_path": relative(tasks_path),
        "tasks_sha256": sha256_file(tasks_path),
        "random_seed": pilot["random_seed"],
        "draws_per_cell": pilot["draws_per_cell"],
        "draw_count": len(draws),
        "artifact_sha256": artifacts,
        "runtime_versions": versions,
        "apparatus_fingerprint": apparatus_fingerprint(artifacts, versions),
        "live_hook_smoke": smoke,
        "draws": draws,
    }
    if schedule_path.exists():
        existing = load_json(schedule_path)
        comparable_existing = {key: value for key, value in existing.items() if key != "created_at_utc"}
        comparable_new = {key: value for key, value in schedule.items() if key != "created_at_utc"}
        if comparable_existing != comparable_new:
            raise RuntimeError("existing pilot schedule disagrees with deterministic preregistration")
        return existing
    atomic_json(schedule_path, schedule)
    return schedule


def verify_live_smoke(
    tasks_path: Path,
    tasks: dict[str, Any],
    artifacts: dict[str, str] | None = None,
    versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not DEFAULT_SMOKE_SUMMARY.is_file():
        raise RuntimeError("live Codex hook-contract smoke has not been run")
    summary = load_json(DEFAULT_SMOKE_SUMMARY)
    current_artifacts = artifacts if artifacts is not None else pinned_artifacts(tasks_path, tasks)
    current_versions = versions if versions is not None else runtime_versions(tasks)
    fingerprint = apparatus_fingerprint(current_artifacts, current_versions)
    if summary.get("apparatus_fingerprint") != fingerprint:
        raise RuntimeError("live hook smoke is stale relative to the current apparatus")
    if summary.get("tasks_sha256") != sha256_file(tasks_path):
        raise RuntimeError("live hook smoke used a different task manifest")
    contract = summary.get("contract", {})
    required = (
        "session_start",
        "allowed_read",
        "allowed_claim",
        "denied_direct_shell",
        "changed_apply_patch",
        "successful_file_change_mapping",
        "marker_present_in_final_tree",
        "claim_release",
        "finished_model",
        "apparatus_unchanged_during_smoke",
        "zero_apparatus_invalid",
    )
    if set(contract) != set(required) or not all(
        contract.get(key) is True for key in required
    ):
        raise RuntimeError(f"live hook smoke contract is incomplete: {contract}")
    return {
        "path": relative(DEFAULT_SMOKE_SUMMARY),
        "sha256": sha256_file(DEFAULT_SMOKE_SUMMARY),
        "apparatus_fingerprint": fingerprint,
        "contract": contract,
    }


def create_worktree(repository: Path, path: Path, commit: str, *, branch: str | None = None) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to replace retained run worktree: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    arguments = ["worktree", "add"]
    if branch is None:
        arguments.append("--detach")
    else:
        arguments.extend(["-b", branch])
    arguments.extend([str(path), commit])
    git(repository, *arguments)


def verify_base(worktree: Path, base_commit: str, base_tree: str) -> dict[str, Any]:
    head = output_text(git(worktree, "rev-parse", "HEAD"))
    head_tree = output_text(git(worktree, "rev-parse", "HEAD^{tree}"))
    status = output_text(git(worktree, "status", "--short"))
    valid = head == base_commit and head_tree == base_tree and not status
    if not valid:
        raise RuntimeError(
            f"worktree reset verification failed: head={head}, tree={head_tree}, status={status!r}"
        )
    return {"head": head, "tree": head_tree, "status": status, "verified": True}


def hook_configuration(python: Path) -> dict[str, Any]:
    shim_path = Path(shim.__file__).resolve()
    command = f'"{python}" "{shim_path}" hook'
    return {
        "description": "Frozen posture experiment measurement hooks.",
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command + " session",
                            "timeout": 30,
                            "statusMessage": "Verifying posture measurement hooks",
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command + " pre",
                            "timeout": 180,
                            "statusMessage": "Recording measured tool operation",
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command + " post",
                            "timeout": 180,
                            "statusMessage": "Recording measured tool result",
                        }
                    ],
                }
            ],
        },
    }


def prepare_agent_surface(worktree: Path, test_python: Path) -> None:
    posture_dir = worktree / ".posture"
    hooks_dir = worktree / ".codex"
    posture_dir.mkdir(parents=True, exist_ok=False)
    hooks_dir.mkdir(parents=True, exist_ok=False)
    wrapper = (
        "from __future__ import annotations\n"
        "import sys\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "from instruments.posture.shim import agent_main\n"
        "raise SystemExit(agent_main())\n"
    )
    (posture_dir / "agent_tool.py").write_text(wrapper, encoding="utf-8", newline="\n")
    (hooks_dir / "hooks.json").write_text(
        json.dumps(hook_configuration(test_python), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def agent_prompt(task: dict[str, Any], agent_count: int, test_python: str) -> str:
    wrapper_command = f'"{test_python}" .posture/agent_tool.py'
    return f"""You are one of {agent_count} concurrently running agents in a controlled experiment. Implement only the task below in the current checkout.

Experimental interface rules (equal in every arm):
- Do not inspect Git history, commits, branches, the network, .git, .codex, or .posture.
- Use shell only as `{wrapper_command} <command>`. Available commands are `list [prefix]`, `read PATH [--start N --end N]`, `size PATH`, `search REGEX [PREFIX ...]`, `status`, `diff`, `test [TEST_PATH ...]`, `claim PATH:* [PATH:* ...]`, and `claims`.
- All file reads/searches/tests must go through that interface. Direct shell commands will be denied.
- Before your first write, make one atomic task-lifetime claim containing every file you expect to edit, using whole-file byte claims such as `{wrapper_command} claim PATH_ONE:* PATH_TWO:*`. In blocking mode that command can wait; let it finish. Do not release or narrow the claim.
- Edit with the apply_patch tool only. Choose the complete claim set once: a second claim is forbidden. If you omitted a file, its write will be denied and that finished response remains valid experimental data.
- Run focused tests through the measured interface. Do not create commits or branches.
- A concise final answer is enough. Failure to solve is still a valid finished response; do not invent success.

Task message:
{task['message']}

Issue / pull-request text:
{task['issue_text']}
"""


def agent_environment(
    *,
    base: dict[str, str],
    database: Path,
    draw: dict[str, Any],
    agent_id: str,
    worktree: Path,
    workspace_key: str,
    radius_path: Path,
    test_python: Path,
    test_timeout: float,
    test_temp_root: Path,
    python_compat: Path,
) -> dict[str, str]:
    environment = dict(base)
    environment.update(
        {
            "POSTURE_DB": str(database),
            "POSTURE_DRAW_ID": draw["draw_id"],
            "POSTURE_AGENT_ID": agent_id,
            "POSTURE_ARM": draw["arm"],
            "POSTURE_WORKSPACE_KEY": workspace_key,
            "POSTURE_WORKTREE": str(worktree),
            "POSTURE_RADIUS": str(radius_path),
            "POSTURE_TEST_PYTHON": str(test_python),
            "POSTURE_TEST_TIMEOUT": str(test_timeout),
            "POSTURE_TEST_TEMP_ROOT": str(test_temp_root),
            "POSTURE_PYTHON_COMPAT": str(python_compat),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "",
            "NO_COLOR": "1",
        }
    )
    return environment


@dataclass
class AgentProcess:
    task: dict[str, Any]
    worktree: Path
    environment: dict[str, str]
    process: subprocess.Popen[bytes]
    stdout_handle: Any
    stderr_handle: Any
    started_monotonic: float
    started_utc: str
    prompt_released_monotonic: float | None = None
    prompt_released_utc: str | None = None
    completed_monotonic: float | None = None
    completed_utc: str | None = None
    return_code: int | None = None
    timed_out: bool = False
    released: bool = False


def codex_exec_command(
    codex: str,
    worktree: Path,
    coordination_dir: Path,
    output_root: Path,
) -> list[str]:
    return [
        codex,
        "exec",
        "-m",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="ultra"',
        "-s",
        "workspace-write",
        "--dangerously-bypass-hook-trust",
        "--enable",
        "hooks",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "-o",
        str(output_root / "final.txt"),
        "-C",
        str(worktree),
        "--add-dir",
        str(coordination_dir),
        "-",
    ]


def terminate_process_tree(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    """Terminate the Codex process and all helpers it spawned."""

    if process.poll() is not None:
        return {"needed": False, "return_code": process.returncode}
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=30,
        )
        method = "taskkill_tree"
        details = {
            "taskkill_exit_code": result.returncode,
            "taskkill_output": (result.stdout + result.stderr).decode("utf-8", errors="replace"),
        }
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            details = {"killpg_exit_code": 0}
        except ProcessLookupError:
            details = {"killpg_exit_code": 0, "already_exited": True}
        method = "kill_process_group"
    try:
        return_code = process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        return_code = process.wait(timeout=30)
        details["fallback_process_kill"] = True
    return {"needed": True, "method": method, "return_code": return_code, **details}


def launch_agents(
    tasks: list[dict[str, Any]],
    worktrees: dict[str, Path],
    environments: dict[str, dict[str, str]],
    attempt_root: Path,
    model_timeout: float,
    launch_order: list[str],
) -> list[AgentProcess]:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI is unavailable")
    task_by_id = {task["task_id"]: task for task in tasks}
    if len(task_by_id) != len(tasks) or set(launch_order) != set(task_by_id) or len(launch_order) != len(tasks):
        raise RuntimeError("preregistered launch order is not a permutation of bundle task ids")
    states: list[AgentProcess] = []
    coordination_dir = Path(next(iter(environments.values()))["POSTURE_DB"]).parent
    try:
        # Start every process with an open stdin before releasing any prompt.
        for task_id in launch_order:
            task = task_by_id[task_id]
            output_root = attempt_root / "agents" / task_id
            output_root.mkdir(parents=True, exist_ok=True)
            stdout_handle = (output_root / "events.jsonl").open("wb")
            stderr_handle = (output_root / "stderr.txt").open("wb")
            command = codex_exec_command(
                codex,
                worktrees[task_id],
                coordination_dir,
                output_root,
            )
            started = time.monotonic()
            popen_options: dict[str, Any] = {}
            if os.name == "nt":
                popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_options["start_new_session"] = True
            try:
                process = subprocess.Popen(
                    command,
                    cwd=worktrees[task_id],
                    env=environments[task_id],
                    stdin=subprocess.PIPE,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                    **popen_options,
                )
            except Exception:
                stdout_handle.close()
                stderr_handle.close()
                raise
            states.append(
                AgentProcess(
                    task=task,
                    worktree=worktrees[task_id],
                    environment=environments[task_id],
                    process=process,
                    stdout_handle=stdout_handle,
                    stderr_handle=stderr_handle,
                    started_monotonic=started,
                    started_utc=dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                )
            )

        database = Path(states[0].environment["POSTURE_DB"])
        draw_id = states[0].environment["POSTURE_DRAW_ID"]
        arm = states[0].environment["POSTURE_ARM"]
        apparatus_event(
            database,
            draw_id,
            arm,
            "prompt_barrier_ready",
            {"agent_count": len(states), "launch_order": launch_order},
        )
        release_times: list[float] = []
        for state in states:
            assert state.process.stdin is not None
            state.process.stdin.write(
                agent_prompt(
                    state.task,
                    len(tasks),
                    state.environment["POSTURE_TEST_PYTHON"],
                ).encode("utf-8")
            )
            state.process.stdin.close()
            state.prompt_released_monotonic = time.monotonic()
            state.prompt_released_utc = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
            release_times.append(state.prompt_released_monotonic)
        apparatus_event(
            database,
            draw_id,
            arm,
            "prompt_barrier_released",
            {
                "agent_count": len(states),
                "launch_order": launch_order,
                "release_skew_seconds": max(release_times) - min(release_times),
            },
        )

        while any(state.return_code is None for state in states):
            now = time.monotonic()
            for state in states:
                if state.return_code is not None:
                    continue
                return_code = state.process.poll()
                start = state.prompt_released_monotonic or state.started_monotonic
                if return_code is None and now - start > model_timeout:
                    state.timed_out = True
                    termination = terminate_process_tree(state.process)
                    apparatus_event(
                        database,
                        draw_id,
                        arm,
                        "model_timeout",
                        {"timeout_seconds": model_timeout, "termination": termination},
                        agent_id=state.task["task_id"],
                    )
                    return_code = state.process.returncode
                if return_code is not None:
                    state.return_code = return_code
                    state.completed_monotonic = time.monotonic()
                    state.completed_utc = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
                    state.stdout_handle.close()
                    state.stderr_handle.close()
                    if not state.released:
                        release_agent(
                            database,
                            draw_id,
                            arm,
                            state.environment["POSTURE_AGENT_ID"],
                            "model_timeout" if state.timed_out else "model_exit",
                        )
                        state.released = True
            time.sleep(0.05)
        return states
    finally:
        # Apparatus failures and Ctrl-C must not leave descendants or claims alive.
        cleanup_errors: list[str] = []
        active_exception = sys.exc_info()[0] is not None
        for state in states:
            try:
                if state.process.poll() is None:
                    terminate_process_tree(state.process)
                if state.return_code is None:
                    state.return_code = state.process.returncode
                    state.completed_monotonic = time.monotonic()
                    state.completed_utc = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
                if state.process.stdin is not None and not state.process.stdin.closed:
                    state.process.stdin.close()
                if not state.stdout_handle.closed:
                    state.stdout_handle.close()
                if not state.stderr_handle.closed:
                    state.stderr_handle.close()
            except Exception as error:  # cleanup continues for every remaining agent
                cleanup_errors.append(f"process cleanup for {state.task['task_id']}: {error!r}")
            if not state.released:
                try:
                    release_agent(
                        Path(state.environment["POSTURE_DB"]),
                        state.environment["POSTURE_DRAW_ID"],
                        state.environment["POSTURE_ARM"],
                        state.environment["POSTURE_AGENT_ID"],
                        "apparatus_finally_cleanup",
                    )
                    state.released = True
                except Exception as error:  # do not prevent cleanup of later agents
                    cleanup_errors.append(f"claim release for {state.task['task_id']}: {error!r}")
        if cleanup_errors and not active_exception:
            raise RuntimeError("; ".join(cleanup_errors))


def jsonl_has_turn_completed(path: Path) -> bool:
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") in {"turn.completed", "turn.completed"}:
            return True
    return False


def model_finished_response(*, timed_out: bool, turn_completed: bool, final_present: bool) -> bool:
    return not timed_out and (turn_completed or final_present)


def model_record(state: AgentProcess, attempt_root: Path) -> dict[str, Any]:
    task_id = state.task["task_id"]
    root = attempt_root / "agents" / task_id
    event_path = root / "events.jsonl"
    final_path = root / "final.txt"
    turn_completed = jsonl_has_turn_completed(event_path)
    final_exists = final_path.is_file() and bool(final_path.read_text(encoding="utf-8", errors="replace").strip())
    # Fairness is response-based, not process-exit-based: a completed final
    # response is data even if the CLI subsequently exits nonzero.
    finished = model_finished_response(
        timed_out=state.timed_out,
        turn_completed=turn_completed,
        final_present=final_exists,
    )
    measurement_start = state.prompt_released_monotonic or state.started_monotonic
    elapsed = (state.completed_monotonic or time.monotonic()) - measurement_start
    return {
        "task_id": task_id,
        "started_at_utc": state.started_utc,
        "prompt_released_at_utc": state.prompt_released_utc,
        "process_setup_seconds": max(0.0, measurement_start - state.started_monotonic),
        "completed_at_utc": state.completed_utc,
        "elapsed_seconds": elapsed,
        "agent_minutes": elapsed / 60.0,
        "return_code": state.return_code,
        "process_exit_success": state.return_code == 0,
        "timed_out": state.timed_out,
        "turn_completed_event": turn_completed,
        "final_message_present": final_exists,
        "model_finished": finished,
        "events_path": relative(event_path),
        "stderr_path": relative(root / "stderr.txt"),
        "final_path": relative(final_path),
    }


def run_live_smoke(tasks_path: Path, smoke_root: Path) -> dict[str, Any]:
    """Exercise the live Codex hook contract without producing task outcome data."""

    if smoke_root.exists():
        raise FileExistsError(f"refusing to replace retained live smoke evidence: {smoke_root}")
    tasks = load_json(tasks_path)
    artifacts = pinned_artifacts(tasks_path, tasks)
    versions = runtime_versions(tasks)
    fingerprint = apparatus_fingerprint(artifacts, versions)
    bundle = next(
        bundle for bundle in tasks["bundles"] if bundle["collision_condition"] == "overlapping"
    )
    repository_config = tasks["repository"]
    repository = project_path(repository_config["clone"])
    test_python = project_path(repository_config["test_python"])
    python_compat = project_path(repository_config["python_compat"])
    worktree = smoke_root / "worktree"
    create_worktree(repository, worktree, bundle["base_commit"])
    base_verification = verify_base(worktree, bundle["base_commit"], bundle["base_tree"])
    prepare_agent_surface(worktree, test_python)
    coordination = smoke_root / "coordination"
    coordination.mkdir(parents=True, exist_ok=False)
    database = coordination / "events.sqlite3"
    shim.initialize_database(database)
    test_temp = coordination / "test-temp"
    test_temp.mkdir()
    draw = {"draw_id": "live-hook-contract-smoke", "arm": "advisory"}
    environment = agent_environment(
        base=os.environ,
        database=database,
        draw=draw,
        agent_id="smoke-agent",
        worktree=worktree,
        workspace_key="smoke",
        radius_path=PROJECT_ROOT / bundle["radius"]["path"],
        test_python=test_python,
        test_timeout=float(repository_config["test_timeout_seconds"]),
        test_temp_root=test_temp,
        python_compat=python_compat,
    )
    output_root = smoke_root / "agent"
    output_root.mkdir()
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI is unavailable")
    command = codex_exec_command(codex, worktree, coordination, output_root)
    wrapper = f'"{test_python}" .posture/agent_tool.py'
    prompt = f"""This is a live apparatus contract smoke, not an experimental task or outcome. Follow every numbered step exactly, even the intentionally denied step.

1. Run `{wrapper} read CHANGES.rst --start 1 --end 8`.
2. Run `{wrapper} claim CHANGES.rst:*` and wait for it to finish.
3. Attempt the direct shell command `git status`. It is expected to be denied; do not work around the denial.
4. Use the apply_patch tool to add one plain line `POSTURE LIVE HOOK SMOKE MARKER` at the very end of CHANGES.rst.
5. Finish with a short message. Do not run tests, inspect history, use the network, or edit any other file.
"""
    events_path = output_root / "events.jsonl"
    stderr_path = output_root / "stderr.txt"
    stdout_handle = events_path.open("wb")
    stderr_handle = stderr_path.open("wb")
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=worktree,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=stdout_handle,
        stderr=stderr_handle,
        shell=False,
        **popen_options,
    )
    timed_out = False
    termination = None
    try:
        assert process.stdin is not None
        process.stdin.write(prompt.encode("utf-8"))
        process.stdin.close()
        try:
            return_code = process.wait(timeout=float(tasks["pilot"]["model_timeout_seconds"]))
        except subprocess.TimeoutExpired:
            timed_out = True
            termination = terminate_process_tree(process)
            return_code = process.returncode
    finally:
        stdout_handle.close()
        stderr_handle.close()
        release_agent(database, draw["draw_id"], draw["arm"], "smoke-agent", "smoke_exit")
    elapsed = time.monotonic() - started
    turn_completed = jsonl_has_turn_completed(events_path)
    final_path = output_root / "final.txt"
    final_present = final_path.is_file() and bool(
        final_path.read_text(encoding="utf-8", errors="replace").strip()
    )
    finished = model_finished_response(
        timed_out=timed_out,
        turn_completed=turn_completed,
        final_present=final_present,
    )
    post_snapshot = verify_snapshot_unchanged(
        tasks_path, tasks, artifacts, versions
    )
    if not post_snapshot.get("verified"):
        apparatus_event(
            database,
            draw["draw_id"],
            draw["arm"],
            "apparatus_invalid",
            {"stage": "post_smoke_hash_verification", **post_snapshot},
        )
    rows = event_rows(database, draw["draw_id"])
    types = [row["event_type"] for row in rows]
    changed_writes = [
        row
        for row in rows
        if row["event_type"] == "write"
        and row["agent_id"] == "smoke-agent"
        and row["details"].get("changed")
    ]
    expected_claim = {"path": "CHANGES.rst", "start": 0, "end": shim.MAX_BYTE}
    exact_read = any(
        row["event_type"] == "read"
        and row["agent_id"] == "smoke-agent"
        and row["details"].get("kind") == "file"
        and row["details"].get("path") == "CHANGES.rst"
        and row["details"].get("start") == 1
        and row["details"].get("end") == 8
        for row in rows
    )
    exact_claim = any(
        row["event_type"] == "claim"
        and row["agent_id"] == "smoke-agent"
        and row["details"].get("claims") == [expected_claim]
        for row in rows
    )
    exact_denial = any(
        row["event_type"] == "shell_denied"
        and row["agent_id"] == "smoke-agent"
        and str(row["details"].get("command", "")).strip().lower() == "git status"
        for row in rows
    )
    exact_write = any(
        row["details"].get("path") == "CHANGES.rst"
        and "POSTURE LIVE HOOK SMOKE MARKER" in str(row["details"].get("patch", ""))
        for row in changed_writes
    )
    marker_present = worktree.joinpath("CHANGES.rst").read_text(
        encoding="utf-8", errors="replace"
    ).rstrip().endswith("POSTURE LIVE HOOK SMOKE MARKER")
    file_change_evidence = successful_file_change_evidence(rows, events_path)
    exact_file_change_mapping = any(
        item.get("path") == "CHANGES.rst"
        for item in file_change_evidence["mapped_changed_files"]
    )
    contract = {
        "session_start": any(
            row["event_type"] == "session_start" and row["agent_id"] == "smoke-agent"
            for row in rows
        ),
        "allowed_read": exact_read,
        "allowed_claim": exact_claim,
        "denied_direct_shell": exact_denial,
        "changed_apply_patch": exact_write,
        "successful_file_change_mapping": exact_file_change_mapping,
        "marker_present_in_final_tree": marker_present,
        "claim_release": any(
            row["event_type"] == "release"
            and row["agent_id"] == "smoke-agent"
            and row["details"].get("kind") == "claim"
            and row["details"].get("claims") == [expected_claim]
            for row in rows
        ),
        "finished_model": finished,
        "apparatus_unchanged_during_smoke": bool(post_snapshot.get("verified")),
        "zero_apparatus_invalid": "apparatus_invalid" not in types,
    }
    model = {
        "events_path": relative(events_path),
        "model_finished": finished,
    }
    summary = {
        "schema_version": 1,
        "measurement": "posture-live-hook-contract-smoke-no-task-outcome",
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
        "tasks_path": relative(tasks_path),
        "tasks_sha256": sha256_file(tasks_path),
        "apparatus_fingerprint": fingerprint,
        "artifact_sha256": artifacts,
        "runtime_versions": versions,
        "post_smoke_hash_verification": post_snapshot,
        "base_verification": base_verification,
        "command": command,
        "return_code": return_code,
        "timed_out": timed_out,
        "termination": termination,
        "elapsed_seconds": elapsed,
        "contract": contract,
        "hook_audit": hook_audit(rows, {"smoke-agent": model}),
        "successful_file_change_evidence": file_change_evidence,
        "changed_write_events": [row["details"] for row in changed_writes],
        "event_types": types,
        "database": relative(database),
        "events": relative(smoke_root / "events.jsonl"),
        "worktree": relative(worktree),
    }
    export_events(database, draw["draw_id"], smoke_root / "events.jsonl")
    atomic_json(smoke_root / "summary.json", summary)
    if not all(contract.values()):
        raise RuntimeError(f"live hook contract smoke failed: {contract}")
    return summary


def save_worktree_patch(worktree: Path, destination: Path) -> dict[str, Any]:
    result = git(worktree, "diff", "--binary", "--", ".", ":(exclude).codex", ":(exclude).posture", check=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(result.stdout)
    return {
        "path": relative(destination),
        "sha256": hashlib.sha256(result.stdout).hexdigest(),
        "bytes": len(result.stdout),
        "git_exit_code": result.returncode,
    }


def commit_isolated_agent(worktree: Path, task_id: str, destination: Path) -> dict[str, Any]:
    status_before = output_text(git(worktree, "status", "--short"))
    git(worktree, "add", "-A", "--", ".", ":(exclude).codex/**", ":(exclude).posture/**")
    patch = git(worktree, "diff", "--cached", "--binary")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(patch.stdout)
    if not patch.stdout:
        return {
            "task_id": task_id,
            "status_before": status_before,
            "commit": None,
            "patch_path": relative(destination),
            "patch_sha256": hashlib.sha256(patch.stdout).hexdigest(),
            "patch_bytes": 0,
        }
    result = git(
        worktree,
        "-c",
        "user.name=Posture Apparatus",
        "-c",
        "user.email=posture-apparatus@example.invalid",
        "commit",
        "-m",
        f"posture agent result: {task_id}",
    )
    return {
        "task_id": task_id,
        "status_before": status_before,
        "commit": output_text(git(worktree, "rev-parse", "HEAD")),
        "commit_output": (result.stdout + result.stderr).decode("utf-8", errors="replace"),
        "patch_path": relative(destination),
        "patch_sha256": hashlib.sha256(patch.stdout).hexdigest(),
        "patch_bytes": len(patch.stdout),
    }


def merge_isolated(
    repository: Path,
    integration: Path,
    commits: dict[str, dict[str, Any]],
    merge_order: list[str],
    database: Path,
    draw: dict[str, Any],
) -> dict[str, Any]:
    merges: list[dict[str, Any]] = []
    for task_id in merge_order:
        commit = commits[task_id]["commit"]
        if commit is None:
            record = {"task_id": task_id, "commit": None, "merged": False, "reason": "no_agent_edit"}
            merges.append(record)
            apparatus_event(database, draw["draw_id"], draw["arm"], "merge", record, agent_id=task_id)
            continue
        pre_merge = {
            "head": output_text(git(integration, "rev-parse", "HEAD")),
            "tree": output_text(git(integration, "rev-parse", "HEAD^{tree}")),
            "status": output_text(git(integration, "status", "--short")),
        }
        result = git(
            integration,
            "-c",
            "user.name=Posture Apparatus",
            "-c",
            "user.email=posture-apparatus@example.invalid",
            "merge",
            "--no-ff",
            "--no-edit",
            commit,
            check=False,
        )
        if result.returncode:
            status = output_text(git(integration, "status", "--short"))
            unmerged_paths = output_text(
                git(integration, "diff", "--name-only", "--diff-filter=U", check=False)
            ).splitlines()
            abort = git(integration, "merge", "--abort", check=False)
            post_abort = {
                "head": output_text(git(integration, "rev-parse", "HEAD")),
                "tree": output_text(git(integration, "rev-parse", "HEAD^{tree}")),
                "status": output_text(git(integration, "status", "--short")),
            }
            restored = abort.returncode == 0 and post_abort == pre_merge
            record = {
                "task_id": task_id,
                "commit": commit,
                "merged": False,
                "reason": "merge_conflict_or_failure",
                "merge_conflict": bool(unmerged_paths),
                "unmerged_paths": unmerged_paths,
                "exit_code": result.returncode,
                "status": status,
                "output": (result.stdout + result.stderr).decode("utf-8", errors="replace"),
                "abort_exit_code": abort.returncode,
                "abort_output": (abort.stdout + abort.stderr).decode("utf-8", errors="replace"),
                "pre_merge": pre_merge,
                "post_abort": post_abort,
                "abort_restored_pre_merge_state": restored,
            }
            if not restored:
                apparatus_event(database, draw["draw_id"], draw["arm"], "merge", record, agent_id=task_id)
                raise RuntimeError(f"merge abort did not restore clean pre-merge state for {task_id}")
        else:
            record = {
                "task_id": task_id,
                "commit": commit,
                "merged": True,
                "merge_conflict": False,
                "exit_code": 0,
                "result_commit": output_text(git(integration, "rev-parse", "HEAD")),
                "output": (result.stdout + result.stderr).decode("utf-8", errors="replace"),
            }
        merges.append(record)
        apparatus_event(database, draw["draw_id"], draw["arm"], "merge", record, agent_id=task_id)
    return {"merge_order": merge_order, "merges": merges, "final_commit": output_text(git(integration, "rev-parse", "HEAD"))}


def run_suite(repository_config: dict[str, Any], worktree: Path, output: Path) -> dict[str, Any]:
    python = project_path(repository_config["test_python"])
    junit_path = output.with_suffix(".junit.xml")
    command = [
        str(python),
        "-m",
        "pytest",
        *repository_config["pytest_arguments"],
        f"--junitxml={junit_path}",
    ]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_ADDOPTS"] = ""
    environment["PYTHONPATH"] = test_pythonpath(repository_config, worktree)
    started = time.monotonic()
    started_utc = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
    timeout_seconds = float(repository_config["test_timeout_seconds"])
    timed_out = False
    timeout_error: str | None = None
    termination: dict[str, Any] | None = None
    popen_options: dict[str, Any] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(
        command,
        cwd=worktree,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        **popen_options,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        payload = stdout + stderr
        exit_code: int | None = process.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        timeout_error = str(error)
        termination = terminate_process_tree(process)
        stdout, stderr = process.communicate(timeout=30)
        payload = stdout + stderr + f"\nPOSTURE SUITE TIMEOUT AFTER {timeout_seconds}s\n".encode("utf-8")
        exit_code = None
    elapsed = time.monotonic() - started
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    normalized = normalized_junit(junit_path) if junit_path.is_file() else None
    return {
        "command": command,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "timeout_error": timeout_error,
        "timeout_termination": termination,
        "elapsed_seconds": elapsed,
        "started_at_utc": started_utc,
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
        "output_path": relative(output),
        "output_sha256": hashlib.sha256(payload).hexdigest(),
        "junit_path": relative(junit_path) if junit_path.is_file() else None,
        "junit_sha256": sha256_file(junit_path) if junit_path.is_file() else None,
        "normalized": normalized,
    }


def testcase_key(case: dict[str, Any]) -> tuple[str, str]:
    return (str(case.get("classname", "")), str(case.get("name", "")))


def all_ground_truth_reference_cases(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    sequences = bundle["construction"]["oracle_sequence_checks"]
    all_sequence = next(sequence for sequence in sequences if sequence["label"] == "all")
    normalized = all_sequence["pre_run_test"].get("normalized")
    if not normalized or not isinstance(normalized.get("cases"), list):
        raise RuntimeError("TASKS.json lacks all-ground-truth JUnit case identities")
    return normalized["cases"]


def synthetic_base_reference_cases(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    runs = bundle["construction"]["baseline_determinism"]["runs"]
    normalized = runs[0].get("normalized") if runs else None
    if not normalized or not isinstance(normalized.get("cases"), list):
        raise RuntimeError("TASKS.json lacks synthetic-base JUnit case identities")
    return normalized["cases"]


def classify_visible_regression(
    bundle: dict[str, Any],
    suite: dict[str, Any],
) -> dict[str, Any]:
    normalized = suite.get("normalized")
    if not normalized or not isinstance(normalized.get("cases"), list):
        return {
            "verified": False,
            "correct": False,
            "wrong_verified": False,
            "reason": "visible_evaluator_junit_missing_or_unparseable",
        }
    reference_cases = synthetic_base_reference_cases(bundle)
    reference_index = {testcase_key(case): case for case in reference_cases}
    observed_cases = normalized["cases"]
    observed_index = {testcase_key(case): case for case in observed_cases}
    if len(reference_index) != len(reference_cases) or len(observed_index) != len(observed_cases):
        return {
            "verified": False,
            "correct": False,
            "wrong_verified": False,
            "reason": "duplicate_visible_testcase_identity",
        }
    missing_reference = sorted(set(reference_index) - set(observed_index))
    passed_to_nonfailure_drift = [
        {
            "classname": key[0],
            "name": key[1],
            "reference_outcome": "passed",
            "observed_outcome": observed_index[key].get("outcome"),
        }
        for key, reference in reference_index.items()
        if key in observed_index
        and reference.get("outcome") == "passed"
        and observed_index[key].get("outcome") not in {"passed", "failure", "error"}
    ]
    failures = [
        case for case in observed_cases if case.get("outcome") in {"failure", "error"}
    ]
    failures_green_before_run = [
        case
        for case in failures
        if reference_index.get(testcase_key(case), {}).get("outcome") == "passed"
    ]
    failures_without_green_evidence = [
        case for case in failures if case not in failures_green_before_run
    ]
    correct = (
        suite.get("exit_code") == 0
        and not suite.get("timed_out")
        and not missing_reference
        and not passed_to_nonfailure_drift
    )
    wrong_verified = (
        suite.get("exit_code") not in {0, None}
        and bool(failures)
        and not failures_without_green_evidence
        and not missing_reference
        and not passed_to_nonfailure_drift
        and not suite.get("timed_out")
    )
    verified = correct or wrong_verified
    return {
        "verified": verified,
        "correct": correct,
        "wrong_verified": wrong_verified,
        "reason": (
            "visible_suite_green_with_pre_run_collection_preserved"
            if correct
            else "every_visible_failure_was_passed_on_the_synthetic_pre_run_base"
            if wrong_verified
            else "visible_outcome_lacks_complete_pre_run_green_evidence"
        ),
        "reference": "first of five identical synthetic-base JUnit runs",
        "reference_case_count": len(reference_cases),
        "observed_case_count": len(observed_cases),
        "missing_reference_cases": [
            {"classname": classname, "name": name}
            for classname, name in missing_reference
        ],
        "failures": failures,
        "failures_green_before_run": failures_green_before_run,
        "failures_without_green_evidence": failures_without_green_evidence,
        "reference_passed_to_nonfailure_outcome_drift": passed_to_nonfailure_drift,
    }


def classify_hidden_suite(
    bundle: dict[str, Any],
    suite: dict[str, Any],
) -> dict[str, Any]:
    normalized = suite.get("normalized")
    if not normalized or not isinstance(normalized.get("cases"), list):
        return {
            "verified": False,
            "correct": False,
            "wrong_verified": False,
            "reason": "evaluator_junit_missing_or_unparseable",
            "tasks": {},
        }
    reference_cases = all_ground_truth_reference_cases(bundle)
    reference_index = {testcase_key(case): case for case in reference_cases}
    if len(reference_index) != len(reference_cases):
        raise RuntimeError("all-ground-truth JUnit contains duplicate testcase identities")
    observed_cases = normalized["cases"]
    observed_index = {testcase_key(case): case for case in observed_cases}
    if len(observed_index) != len(observed_cases):
        return {
            "verified": False,
            "correct": False,
            "wrong_verified": False,
            "reason": "observed_junit_contains_duplicate_testcase_identities",
            "tasks": {},
        }
    missing_reference = sorted(set(reference_index) - set(observed_index))
    passed_to_nonfailure_drift = [
        {
            "classname": key[0],
            "name": key[1],
            "reference_outcome": "passed",
            "observed_outcome": observed_index[key].get("outcome"),
        }
        for key, reference in reference_index.items()
        if key in observed_index
        and reference.get("outcome") == "passed"
        and observed_index[key].get("outcome") not in {"passed", "failure", "error"}
    ]
    failures = [
        case for case in observed_cases if case.get("outcome") in {"failure", "error"}
    ]
    failures_green_in_reference = [
        case
        for case in failures
        if reference_index.get(testcase_key(case), {}).get("outcome") == "passed"
    ]
    failures_without_green_evidence = [
        case for case in failures if case not in failures_green_in_reference
    ]
    case_integrity = (
        not suite.get("timed_out")
        and not missing_reference
        and not failures_without_green_evidence
        and not passed_to_nonfailure_drift
    )
    task_results: dict[str, Any] = {}
    for task in bundle["tasks"]:
        expected = task.get("expected_focal_cases", [])
        outcomes = [
            {
                "classname": case["classname"],
                "name": case["name"],
                "observed_outcome": observed_index.get(
                    (case["classname"], case["name"]), {}
                ).get("outcome"),
            }
            for case in expected
        ]
        present = all(item["observed_outcome"] is not None for item in outcomes)
        passed = bool(outcomes) and all(
            item["observed_outcome"] == "passed" for item in outcomes
        )
        failed = [
            item
            for item in outcomes
            if item["observed_outcome"] in {"failure", "error"}
        ]
        task_results[task["task_id"]] = {
            "verified": bool(outcomes) and present and case_integrity,
            "correct": passed and case_integrity,
            "wrong_verified": bool(failed) and case_integrity,
            "expected_case_count": len(outcomes),
            "cases": outcomes,
            "reason": (
                "integrated_suite_has_missing_cases_or_failure_without_green_evidence"
                if not case_integrity
                else
                "all_preregistered_focal_cases_collected_and_passed"
                if passed
                else "preregistered_ground_truth_green_focal_case_failed"
                if failed
                else "focal_case_missing_or_nonpassing_without_failure"
            ),
        }
    all_focal_passed = bool(task_results) and all(
        result["correct"] for result in task_results.values()
    )
    correct = (
        suite.get("exit_code") == 0
        and not suite.get("timed_out")
        and not missing_reference
        and all_focal_passed
    )
    wrong_verified = (
        suite.get("exit_code") not in {0, None}
        and bool(failures)
        and not failures_without_green_evidence
        and not missing_reference
    )
    return {
        "verified": case_integrity,
        "correct": correct,
        "wrong_verified": wrong_verified,
        "reason": (
            "actual_integrated_hidden_suite_green_with_collection_preserved"
            if correct
            else "every_observed_failure_was_preregistered_ground_truth_green"
            if wrong_verified
            else "outcome_not_classifiable_as_correct_or_verified_wrong"
        ),
        "reference_case_count": len(reference_cases),
        "observed_case_count": len(observed_cases),
        "missing_reference_cases": [
            {"classname": classname, "name": name}
            for classname, name in missing_reference
        ],
        "failures": failures,
        "failures_green_in_reference": failures_green_in_reference,
        "failures_without_green_evidence": failures_without_green_evidence,
        "reference_passed_to_nonfailure_outcome_drift": passed_to_nonfailure_drift,
        "tasks": task_results,
    }


def evaluate_integrated_hidden(
    repository: Path,
    repository_config: dict[str, Any],
    bundle: dict[str, Any],
    final_commit: str,
    root: Path,
) -> dict[str, Any]:
    worktree = root / "integrated-hidden-oracle" / "worktree"
    create_worktree(repository, worktree, final_commit)
    applications: list[dict[str, Any]] = []
    test_path_tasks: dict[str, list[str]] = {}
    for task in bundle["tasks"]:
        for path in task["ground_truth_test_paths"]:
            test_path_tasks.setdefault(path, []).append(task["task_id"])
    for path in sorted(test_path_tasks):
        anchor_blob = git(repository, "show", f"{bundle['anchor_sha']}:{path}", check=False)
        if anchor_blob.returncode != 0:
            return {
                "verified": False,
                "reason": "preregistered_hidden_test_path_absent_from_anchor",
                "path": path,
                "applications": applications,
                "worktree": relative(worktree),
            }
        destination = worktree / Path(path)
        before = destination.read_bytes() if destination.is_file() else None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(anchor_blob.stdout)
        applications.append(
            {
                "path": path,
                "task_ids": sorted(test_path_tasks[path]),
                "state": "restored_exact_anchor_hidden_test_file",
                "before_sha256": hashlib.sha256(before).hexdigest() if before is not None else None,
                "anchor_blob_sha256": hashlib.sha256(anchor_blob.stdout).hexdigest(),
                "changed": before != anchor_blob.stdout,
                "verified": True,
            }
        )
    suite = run_suite(
        repository_config,
        worktree,
        root / "integrated-hidden-oracle" / "full-suite.txt",
    )
    classification = classify_hidden_suite(bundle, suite)
    return {
        "verified": classification["verified"],
        "reason": classification["reason"],
        "applications": applications,
        "suite": suite,
        "classification": classification,
        "worktree": relative(worktree),
    }


def event_rows(database: Path, draw_id: str, agent_id: str | None = None) -> list[dict[str, Any]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        query = "SELECT * FROM events WHERE draw_id = ?"
        parameters: list[Any] = [draw_id]
        if agent_id is not None:
            query += " AND agent_id = ?"
            parameters.append(agent_id)
        query += " ORDER BY id"
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    return [
        {
            **dict(row),
            "details": json.loads(row["details_json"]),
        }
        for row in rows
    ]


def export_events(database: Path, draw_id: str, output: Path) -> None:
    rows = event_rows(database, draw_id)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            row.pop("details_json", None)
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=True) + "\n")


def agent_patches(database: Path, draw_id: str, task_id: str) -> list[str]:
    patches: list[str] = []
    for row in event_rows(database, draw_id, task_id):
        if row["event_type"] == "write" and row["details"].get("changed"):
            patch = row["details"].get("patch")
            if isinstance(patch, str) and patch:
                patches.append(patch)
    return patches


def apply_git_patch(
    worktree: Path,
    patch: bytes,
    *,
    reverse: bool = False,
    index: bool = False,
    check_only: bool = False,
) -> dict[str, Any]:
    arguments = ["apply", "--binary", "--whitespace=nowarn"]
    if check_only:
        arguments.append("--check")
    if reverse:
        arguments.append("--reverse")
    if index:
        arguments.append("--index")
    arguments.append("-")
    result = git(worktree, *arguments, stdin=patch, check=False)
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout.decode("utf-8", errors="replace"),
        "stderr": result.stderr.decode("utf-8", errors="replace"),
    }


def verify_patch_presence(
    repository: Path,
    final_commit: str,
    final_tree: str,
    task_id: str,
    patches: list[str],
    root: Path,
) -> dict[str, Any]:
    if not patches:
        return {"verified": True, "present": False, "reason": "no_changed_write_patch"}
    worktree = root / "presence" / task_id / "worktree"
    create_worktree(repository, worktree, final_commit)
    applications: list[dict[str, Any]] = []
    for patch in reversed(patches):
        result = apply_git_patch(worktree, patch.encode("utf-8"), reverse=True)
        applications.append(result)
        if result["exit_code"]:
            return {
                "verified": False,
                "present": None,
                "reason": "agent_patch_not_reversible_from_final_tree_presence_unverified",
                "applications": applications,
                "worktree": relative(worktree),
            }
    git(worktree, "add", "-A")
    reversed_tree = output_text(git(worktree, "write-tree"))
    present = reversed_tree != final_tree
    return {
        "verified": True,
        "present": present,
        "reason": (
            "reversing_agent_writes_changes_final_tree"
            if present
            else "agent_write_sequence_has_no_net_effect_in_final_tree"
        ),
        "final_tree": final_tree,
        "tree_after_reversing_agent_writes": reversed_tree,
        "applications": applications,
        "worktree": relative(worktree),
    }


def verify_isolated_presence(
    repository: Path,
    final_commit: str,
    task_id: str,
    isolated_commit: dict[str, Any],
) -> dict[str, Any]:
    commit = isolated_commit.get("commit")
    patch_bytes = int(isolated_commit.get("patch_bytes", 0))
    if not commit or patch_bytes == 0:
        return {"verified": True, "present": False, "reason": "no_isolated_agent_commit"}
    ancestry = git(repository, "merge-base", "--is-ancestor", str(commit), final_commit, check=False)
    present = ancestry.returncode == 0
    return {
        "verified": True,
        "present": present,
        "reason": "isolated_commit_is_final_ancestor" if present else "isolated_commit_not_landed",
        "agent_commit": commit,
        "final_commit": final_commit,
        "merge_base_exit_code": ancestry.returncode,
        "patch_bytes": patch_bytes,
        "task_id": task_id,
    }


def evaluate_focal(
    repository: Path,
    repository_config: dict[str, Any],
    bundle: dict[str, Any],
    task: dict[str, Any],
    patches: list[str],
    root: Path,
) -> dict[str, Any]:
    worktree = root / "oracles" / task["task_id"] / "worktree"
    create_worktree(repository, worktree, bundle["base_commit"])
    ground_truth_results: list[dict[str, Any]] = []
    for other in sorted(bundle["tasks"], key=lambda item: item["stream_index"]):
        if other["task_id"] == task["task_id"]:
            continue
        patch = PROJECT_ROOT / other["ground_truth_patch"]
        result = apply_git_patch(worktree, patch.read_bytes(), index=True)
        result["task_id"] = other["task_id"]
        ground_truth_results.append(result)
        if result["exit_code"]:
            return {
                "verified": False,
                "reason": "preregistered_oracle_ground_truth_failed_to_apply",
                "ground_truth_applications": ground_truth_results,
                "worktree": relative(worktree),
            }
    agent_results: list[dict[str, Any]] = []
    for patch in patches:
        result = apply_git_patch(worktree, patch.encode("utf-8"))
        agent_results.append(result)
        if result["exit_code"]:
            return {
                "verified": False,
                "reason": "agent_delta_could_not_be_replayed_on_oracle",
                "ground_truth_applications": ground_truth_results,
                "agent_applications": agent_results,
                "worktree": relative(worktree),
            }
    hidden_test_value = task.get("ground_truth_test_patch") or task.get("focal_test_patch")
    if not isinstance(hidden_test_value, str) or not hidden_test_value:
        return {
            "verified": False,
            "reason": "missing_preregistered_hidden_focal_test_patch",
            "ground_truth_applications": ground_truth_results,
            "agent_applications": agent_results,
            "worktree": relative(worktree),
        }
    hidden_test_path = PROJECT_ROOT / hidden_test_value
    if not hidden_test_path.is_file():
        return {
            "verified": False,
            "reason": "preregistered_hidden_focal_test_patch_missing_on_disk",
            "hidden_test_patch": hidden_test_value,
            "worktree": relative(worktree),
        }
    hidden_patch = hidden_test_path.read_bytes()
    forward_check = apply_git_patch(worktree, hidden_patch, check_only=True)
    reverse_check: dict[str, Any] | None = None
    if forward_check["exit_code"] == 0:
        hidden_application = apply_git_patch(worktree, hidden_patch)
        hidden_state = "overlaid_by_apparatus"
        if hidden_application["exit_code"]:
            return {
                "verified": False,
                "reason": "hidden_focal_test_patch_passed_check_but_failed_to_apply",
                "hidden_test_application": hidden_application,
                "worktree": relative(worktree),
            }
    else:
        reverse_check = apply_git_patch(worktree, hidden_patch, reverse=True, check_only=True)
        if reverse_check["exit_code"] != 0:
            return {
                "verified": False,
                "reason": "hidden_focal_test_patch_conflicts_with_agent_delta",
                "hidden_test_forward_check": forward_check,
                "hidden_test_reverse_check": reverse_check,
                "worktree": relative(worktree),
            }
        hidden_application = None
        hidden_state = "already_present_exactly"
    suite = run_suite(
        repository_config,
        worktree,
        root / "oracles" / task["task_id"] / "full-suite.txt",
    )
    classification = classify_hidden_suite(bundle, suite)
    return {
        "verified": classification["verified"],
        "reason": "full_suite_with_preregistered_hidden_focal_tests_on_other-task_ground-truth_oracle",
        "correct": classification["correct"],
        "wrong_verified": classification["wrong_verified"],
        "classification": classification,
        "ground_truth_applications": ground_truth_results,
        "agent_applications": agent_results,
        "hidden_test_patch": hidden_test_value,
        "hidden_test_patch_sha256": hashlib.sha256(hidden_patch).hexdigest(),
        "hidden_test_state": hidden_state,
        "hidden_test_forward_check": forward_check,
        "hidden_test_reverse_check": reverse_check,
        "hidden_test_application": hidden_application,
        "suite": suite,
        "worktree": relative(worktree),
    }


def commit_shared_final(worktree: Path, draw_id: str) -> str:
    git(worktree, "add", "-A", "--", ".", ":(exclude).codex/**", ":(exclude).posture/**")
    staged = git(worktree, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return output_text(git(worktree, "rev-parse", "HEAD"))
    git(
        worktree,
        "-c",
        "user.name=Posture Apparatus",
        "-c",
        "user.email=posture-apparatus@example.invalid",
        "commit",
        "-m",
        f"posture shared result: {draw_id}",
    )
    return output_text(git(worktree, "rev-parse", "HEAD"))


def event_paths(row: dict[str, Any]) -> set[str]:
    details = row["details"]
    paths: set[str] = set()
    for key in ("path", "paths", "changed_paths", "target_paths"):
        value = details.get(key)
        if isinstance(value, str):
            paths.add(value.replace("\\", "/"))
        elif isinstance(value, list):
            paths.update(str(item).replace("\\", "/") for item in value)
    patch = details.get("patch")
    if isinstance(patch, str):
        for match in re.finditer(r"^\+\+\+ b/(.+)$", patch, flags=re.MULTILINE):
            paths.add(match.group(1))
    return paths


def read_digest(row: dict[str, Any]) -> str | None:
    for key in ("sha256", "content_sha256", "file_sha256", "version_sha256"):
        value = row["details"].get(key)
        if isinstance(value, str) and value:
            return value
    return None


def causal_rework(events: list[dict[str, Any]], agent_id: str) -> dict[str, Any]:
    reads = [
        row
        for row in events
        if row["agent_id"] == agent_id
        and row["event_type"] == "read"
        and row["details"].get("kind") == "file"
    ]
    if reads and any(read_digest(row) is None for row in reads):
        return {
            "measurement_available": False,
            "verified_rework_operations": None,
            "reason": "read_events_do_not_all_carry_content_hashes",
        }
    evidence: list[dict[str, Any]] = []
    for first in reads:
        path_set = event_paths(first)
        first_hash = read_digest(first)
        for path in path_set:
            intervening = next(
                (
                    row
                    for row in events
                    if row["id"] > first["id"]
                    and row["agent_id"] != agent_id
                    and row["event_type"] == "write"
                    and row["details"].get("changed")
                    and path in event_paths(row)
                ),
                None,
            )
            if intervening is None:
                continue
            reread = next(
                (
                    row
                    for row in reads
                    if row["id"] > intervening["id"]
                    and path in event_paths(row)
                    and read_digest(row) != first_hash
                ),
                None,
            )
            if reread is None:
                continue
            later_write = next(
                (
                    row
                    for row in events
                    if row["id"] > reread["id"]
                    and row["agent_id"] == agent_id
                    and row["event_type"] == "write"
                    and row["details"].get("changed")
                    and path in event_paths(row)
                ),
                None,
            )
            if later_write is not None and not any(item["path"] == path for item in evidence):
                rework_seconds = None
                if reread.get("monotonic_ns") is not None and later_write.get("monotonic_ns") is not None:
                    rework_seconds = max(
                        0.0,
                        (int(later_write["monotonic_ns"]) - int(reread["monotonic_ns"])) / 1e9,
                    )
                evidence.append(
                    {
                        "path": path,
                        "first_read_event": first["id"],
                        "intervening_other_write_event": intervening["id"],
                        "reread_event": reread["id"],
                        "later_write_event": later_write["id"],
                        "first_sha256": first_hash,
                        "reread_sha256": read_digest(reread),
                        "rework_seconds": rework_seconds,
                    }
                )
    durations = [item["rework_seconds"] for item in evidence if item["rework_seconds"] is not None]
    return {
        "measurement_available": True,
        "verified_rework_operations": len(evidence),
        "verified_rework_seconds": sum(durations) if len(durations) == len(evidence) else None,
        "definition": "direct file read version A, another agent writes path, direct reread observes a changed hash, then the agent writes; cost is reread-to-write elapsed time",
        "scope_limitation": "search and diff observations are logged but are not counted as changed-region rereads because they do not provide a comparable whole-file content hash",
        "evidence": evidence,
    }


def shared_write_audit(
    repository: Path,
    base_commit: str,
    final_tree: str,
    events: list[dict[str, Any]],
    root: Path,
    audit_name: str = "global-write-replay",
    require_sequential_replay: bool = False,
) -> dict[str, Any]:
    worktree = root / "audits" / safe_name(audit_name) / "worktree"
    create_worktree(repository, worktree, base_commit)
    applications: list[dict[str, Any]] = []
    changed_writes = [
        row for row in events if row["event_type"] == "write" and row["details"].get("changed")
    ]
    sequential_replay_ok = True
    for row in changed_writes:
        patch = row["details"].get("patch")
        if not isinstance(patch, str) or not patch:
            applications.append(
                {
                    "event_id": row["id"],
                    "agent_id": row["agent_id"],
                    "exit_code": None,
                    "reason": "changed_write_event_missing_replay_patch",
                }
            )
            sequential_replay_ok = False
            continue
        application = apply_git_patch(worktree, patch.encode("utf-8"))
        application.update({"event_id": row["id"], "agent_id": row["agent_id"]})
        applications.append(application)
        if application["exit_code"]:
            sequential_replay_ok = False
            # Later patches cannot be interpreted after a failed transition.
            break
    git(worktree, "add", "-A")
    replay_tree = output_text(git(worktree, "write-tree"))
    sequential_replay_ok = sequential_replay_ok and replay_tree == final_tree

    changed_result = git(
        repository,
        "diff",
        "--name-only",
        "-z",
        base_commit,
        final_tree,
    )
    final_changed_paths = [
        value.decode("utf-8", errors="surrogateescape")
        for value in changed_result.stdout.split(b"\0")
        if value
    ]
    reconciliations: list[dict[str, Any]] = []
    state_reconciled = True
    for path in final_changed_paths:
        path_events = [row for row in changed_writes if path in event_paths(row)]
        final_blob = git(repository, "show", f"{final_tree}:{path}", check=False)
        final_exists = final_blob.returncode == 0
        final_sha256 = hashlib.sha256(final_blob.stdout).hexdigest() if final_exists else None
        latest = path_events[-1] if path_events else None
        latest_after = latest["details"].get("after_sha256") if latest else None
        matches = latest is not None and latest_after == final_sha256
        state_reconciled = state_reconciled and matches
        reconciliations.append(
            {
                "path": path,
                "final_exists": final_exists,
                "final_sha256": final_sha256,
                "latest_changed_write_event": latest["id"] if latest else None,
                "latest_logged_after_sha256": latest_after,
                "matches": matches,
            }
        )
    verified = state_reconciled and (sequential_replay_ok or not require_sequential_replay)
    return {
        "verified": verified,
        "reason": (
            "final_changed_paths_reconcile_to_logged_post_write_states"
            if verified
            else "unlogged_or_unreconciled_final_tree_change"
        ),
        "changed_write_events": len(changed_writes),
        "final_changed_paths": final_changed_paths,
        "final_state_reconciliations": reconciliations,
        "final_state_reconciled": state_reconciled,
        "sequential_replay_required": require_sequential_replay,
        "sequential_replay_verified": sequential_replay_ok,
        "sequential_replay_note": (
            "Shared-arm post-hook patches can overlap in time, so strict serial replay is diagnostic there."
        ),
        "replay_tree": replay_tree,
        "final_tree": final_tree,
        "applications": applications,
        "worktree": relative(worktree),
    }


def codex_tool_items(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"available": False, "reason": "codex_jsonl_missing", "items": []}
    items: list[dict[str, Any]] = []
    parse_errors = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if event.get("type") != "item.completed" or not isinstance(event.get("item"), dict):
            continue
        item = event["item"]
        if item.get("type") in {"command_execution", "file_change"}:
            items.append(
                {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "command": item.get("command"),
                    "status": item.get("status"),
                }
            )
    return {"available": parse_errors == 0, "parse_errors": parse_errors, "items": items}


def successful_file_change_evidence(
    events: list[dict[str, Any]], codex_events_path: Path
) -> dict[str, Any]:
    """Map agent-attributable completed file changes to their Pre/Post hook records."""

    tool_items = codex_tool_items(codex_events_path)
    completed_ids = {
        str(item["id"])
        for item in tool_items["items"]
        if item.get("type") == "file_change"
        and item.get("status") == "completed"
        and item.get("id") is not None
    }
    attempts = {
        str(row["details"].get("tool_use_id")): row
        for row in events
        if row["event_type"] == "write_attempt"
        and row["details"].get("tool_use_id") is not None
    }
    mapped: list[dict[str, Any]] = []
    for row in events:
        if row["event_type"] != "write":
            continue
        tool_use_id = str(row["details"].get("tool_use_id"))
        response_present = row["details"].get("tool_response") is not None
        if (
            tool_use_id in completed_ids
            and tool_use_id in attempts
            and response_present
            and row["details"].get("changed") is True
        ):
            mapped.append(
                {
                    "tool_use_id": tool_use_id,
                    "path": row["details"].get("path"),
                    "before_sha256": row["details"].get("before_sha256"),
                    "after_sha256": row["details"].get("after_sha256"),
                    "posttool_response_present": True,
                    "codex_file_change_status": "completed",
                }
            )
    mapped_ids = sorted({item["tool_use_id"] for item in mapped})
    return {
        "verified": bool(tool_items["available"]),
        "codex_items": tool_items,
        "completed_file_change_tool_ids": sorted(completed_ids),
        "mapped_successful_tool_ids": mapped_ids,
        "mapped_changed_files": mapped,
        "has_successful_file_change": bool(mapped_ids),
        "definition": (
            "same tool_use_id has Codex file_change status completed, an allowed "
            "write_attempt, and a changed PostToolUse write with a response"
        ),
    }


def hook_audit(events: list[dict[str, Any]], models: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_agent: dict[str, Any] = {}
    heartbeat_names = {"hook_heartbeat", "session_start", "hook_session"}
    all_heartbeats = True
    for agent_id, model in models.items():
        agent_events = [row for row in events if row["agent_id"] == agent_id]
        heartbeats = [row["id"] for row in agent_events if row["event_type"] in heartbeat_names]
        tool_items = codex_tool_items(PROJECT_ROOT / model["events_path"])
        write_items = sum(1 for item in tool_items["items"] if item["type"] == "file_change")
        hook_writes = sum(
            1 for row in agent_events if row["event_type"] in {"write", "write_denied"}
        )
        command_items = sum(1 for item in tool_items["items"] if item["type"] == "command_execution")
        semantic_commands = sum(
            1
            for row in agent_events
            if row["event_type"]
            in {"claim", "read", "test", "shell_denied", "tool_denied", "command_denied"}
        )
        heartbeat_ok = bool(heartbeats)
        file_change_evidence = successful_file_change_evidence(
            agent_events, PROJECT_ROOT / model["events_path"]
        )
        all_heartbeats = all_heartbeats and heartbeat_ok
        by_agent[agent_id] = {
            "heartbeat_verified": heartbeat_ok,
            "heartbeat_event_ids": heartbeats,
            "codex_tool_items": tool_items,
            "file_change_items": write_items,
            "write_hook_events": hook_writes,
            "command_execution_items": command_items,
            "semantic_command_events": semantic_commands,
            "successful_file_change_evidence": file_change_evidence,
            "reconciliation_interpretation": (
                "counts_are_diagnostic_only_because_one_wrapper_command_can_emit_multiple_semantic_events"
            ),
        }
    return {
        "heartbeat_verified_for_every_agent": all_heartbeats,
        "agents": by_agent,
        "tool_reconciliation_is_definitive": False,
        "reason": "heartbeat is definitive; Codex-item to semantic-event counts are retained as a non-bijective audit",
    }


def summarize_task(
    task: dict[str, Any],
    model: dict[str, Any],
    events: list[dict[str, Any]],
    all_events: list[dict[str, Any]],
    merge: dict[str, Any] | None,
    presence: dict[str, Any],
    oracle: dict[str, Any] | None,
    observed_task: dict[str, Any] | None = None,
    visible_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    blocks = [row for row in events if row["event_type"] == "block"]
    releases = [
        row for row in events
        if row["event_type"] == "release" and row["details"].get("kind") == "blocking_wait"
    ]
    claims = [row for row in events if row["event_type"] == "claim"]
    writes = [row for row in events if row["event_type"] == "write" and row["details"].get("changed")]
    denied = [row for row in events if row["event_type"] == "write_denied"]
    write_attempts = [row for row in events if row["event_type"] == "write_attempt"]
    merged = True if merge is None else bool(merge.get("merged"))
    if observed_task is not None:
        isolated = merge is not None
        model_events_value = model.get("events_path")
        file_change_evidence = (
            successful_file_change_evidence(
                events, PROJECT_ROOT / str(model_events_value)
            )
            if isinstance(model_events_value, str) and model_events_value
            else {
                "verified": False,
                "has_successful_file_change": False,
                "reason": "model_events_path_missing",
            }
        )
        direct_presence_verified = bool(presence.get("verified")) if isolated else True
        direct_presence = bool(presence.get("present")) if isolated else True
        landing_verified = (
            bool(file_change_evidence.get("verified"))
            and bool(file_change_evidence.get("has_successful_file_change"))
            and direct_presence_verified
        )
        landed = landing_verified and merged and direct_presence
        edit_activity = bool(write_attempts) or bool(
            file_change_evidence.get("completed_file_change_tool_ids")
        )
        if not edit_activity or not merged or (
            isolated and direct_presence_verified and not direct_presence
        ):
            outcome = "abandoned"
        elif not landing_verified:
            outcome = "unverified"
        elif visible_bundle is None or not visible_bundle.get("verified"):
            outcome = "unverified"
        elif visible_bundle.get("wrong_verified"):
            outcome = "landed_and_wrong"
        elif observed_task.get("correct") and visible_bundle.get("correct"):
            outcome = "landed_and_correct"
        elif observed_task.get("wrong_verified"):
            outcome = "landed_but_task_incomplete"
        else:
            outcome = "unverified"
        attribution = (
            "completed_file_change_plus_direct_isolate_merge_presence_and_integrated_oracles"
            if isolated
            else "completed_file_change_collective_shared_landing_plus_integrated_oracles_no_byte_authorship"
        )
    else:
        file_change_evidence = None
        landing_verified = bool(presence.get("verified"))
        landed = landing_verified and bool(writes) and merged and bool(presence.get("present"))
        if not landing_verified:
            outcome = "unverified"
        elif not landed:
            outcome = "abandoned"
        elif oracle is None or not oracle.get("verified"):
            outcome = "unverified"
        elif oracle.get("correct"):
            outcome = "landed_and_correct"
        elif oracle.get("wrong_verified"):
            outcome = "landed_and_wrong"
        else:
            outcome = "unverified"
        attribution = "counterfactual_agent_delta_oracle"
    wait_seconds = sum(float(row["details"].get("wait_seconds", 0.0)) for row in releases)
    collision_claims = sum(1 for row in claims if row["details"].get("collision_exposed"))
    arm_value = events[0].get("arm") if events else None
    arm = str(arm_value) if arm_value is not None else None
    if arm in {"advisory", "blocking"}:
        rework = {
            "measurement_available": False,
            "verified_rework_operations": None,
            "verified_rework_seconds": None,
            "reason": "shared Pre/Post write events cannot race-safely attribute the intervening delta to an agent without adding a forbidden write mutex",
            "scope_limitation": "reads and writes remain fully logged for qualitative inspection",
            "evidence": [],
        }
    elif arm == "isolate":
        rework = {
            "measurement_available": True,
            "verified_rework_operations": 0,
            "verified_rework_seconds": 0.0,
            "reason": "isolated agents cannot observe another agent's writes before the end-of-run merge",
            "scope_limitation": "merge conflicts are recorded separately and occur after agent completion",
            "evidence": [],
        }
    else:
        rework = causal_rework(all_events, task["task_id"])
    return {
        "task_id": task["task_id"],
        "model": model,
        "outcome": outcome,
        "landed": landed,
        "landing_verified": landing_verified,
        "outcome_attribution": attribution,
        "successful_file_change_evidence": file_change_evidence,
        "observed_integrated_task_oracle": observed_task,
        "observed_visible_bundle_oracle": visible_bundle,
        "blocked_then_completed": bool(blocks) and bool(releases) and model["model_finished"],
        "blocked_then_correct": bool(blocks) and bool(releases) and outcome == "landed_and_correct",
        "blocked_then_finished_response": bool(blocks) and model["model_finished"],
        "blocking_wait_seconds": wait_seconds,
        "claim_acquisitions": len(claims),
        "collision_exposed_claims": collision_claims,
        "collision_exposed": collision_claims > 0,
        "collision_rate": collision_claims / len(claims) if claims else None,
        "changed_write_events": len(writes),
        "write_attempt_events": len(write_attempts),
        "rework": rework,
        "rework_operations": rework["verified_rework_operations"],
        "rework_seconds": rework.get("verified_rework_seconds"),
        "write_denials": len(denied),
        "merge": merge,
        "presence": presence,
        "oracle": oracle,
    }


def collision_metrics(
    events: list[dict[str, Any]],
    task_ids: list[str],
    designed_collisions: dict[str, Any],
) -> dict[str, Any]:
    claims = [row for row in events if row["event_type"] == "claim" and row["agent_id"] in task_ids]
    pairs: set[tuple[str, str]] = set()
    observations = 0
    for row in claims:
        conflicts = {
            str(agent_id)
            for agent_id in row["details"].get("conflicting_agents_seen", [])
            if str(agent_id) in task_ids and str(agent_id) != row["agent_id"]
        }
        observations += len(conflicts)
        for other in conflicts:
            pairs.add(tuple(sorted((str(row["agent_id"]), other))))
    possible_pairs = len(task_ids) * (len(task_ids) - 1) // 2
    designed_pairs = {
        tuple(sorted((str(pair["left"]), str(pair["right"]))))
        for pair in designed_collisions.get("pairs", [])
        if pair.get("designed_collision_opportunity")
    }
    realised_designed_pairs = pairs & designed_pairs
    exposed_claims = sum(1 for row in claims if row["details"].get("collision_exposed"))
    return {
        "claim_acquisitions": len(claims),
        "claim_uptake_rate": len(claims) / len(task_ids) if task_ids else None,
        "exposed_claims": exposed_claims,
        "exposed_claim_rate": exposed_claims / len(claims) if claims else None,
        "unique_agent_pairs": [list(pair) for pair in sorted(pairs)],
        "unique_agent_pair_count": len(pairs),
        "all_pair_denominator": possible_pairs,
        "all_pair_rate": len(pairs) / possible_pairs if possible_pairs else None,
        "collision_observations": observations,
        "designed_pair_denominator": len(designed_pairs),
        "realised_designed_pair_count": len(realised_designed_pairs),
        "designed_pair_realisation_rate": (
            len(realised_designed_pairs) / len(designed_pairs) if designed_pairs else None
        ),
    }


def run_draw(
    tasks_manifest: dict[str, Any],
    schedule: dict[str, Any],
    draw: dict[str, Any],
    attempt: int,
    run_root: Path,
) -> dict[str, Any]:
    apparatus_verification = verify_schedule_apparatus(
        (PROJECT_ROOT / schedule["tasks_path"]).resolve(strict=True),
        tasks_manifest,
        schedule,
    )
    bundle = next(item for item in tasks_manifest["bundles"] if item["bundle_id"] == draw["bundle_id"])
    repository_config = tasks_manifest["repository"]
    repository = project_path(repository_config["clone"])
    test_python = project_path(repository_config["test_python"])
    python_compat = project_path(repository_config["python_compat"])
    attempt_root = run_root / draw["draw_id"] / f"attempt-{attempt:03d}"
    attempt_root.mkdir(parents=True, exist_ok=False)
    coordination_root = attempt_root / "coordination"
    coordination_root.mkdir(parents=True, exist_ok=False)
    database = coordination_root / "events.sqlite3"
    shim.initialize_database(database)
    apparatus_event(
        database,
        draw["draw_id"],
        draw["arm"],
        "draw_start",
        {
            "attempt": attempt,
            "schedule_sha256": sha256_file(Path(schedule["schedule_path"])),
            "base_commit": bundle["base_commit"],
            "base_tree": bundle["base_tree"],
            "apparatus_fingerprint": apparatus_verification["fingerprint"],
        },
    )
    agent_worktrees: dict[str, Path] = {}
    if draw["arm"] == "isolate":
        for task in bundle["tasks"]:
            task_id = task["task_id"]
            worktree = attempt_root / "agents" / task_id / "worktree"
            branch = f"posture/{safe_name(draw['draw_id'])}/a{attempt:03d}/{safe_name(task_id)}"
            create_worktree(repository, worktree, bundle["base_commit"], branch=branch)
            verify_base(worktree, bundle["base_commit"], bundle["base_tree"])
            prepare_agent_surface(worktree, test_python)
            agent_worktrees[task_id] = worktree
        integration = attempt_root / "integration" / "worktree"
        integration_branch = f"posture/{safe_name(draw['draw_id'])}/a{attempt:03d}/integration"
        create_worktree(repository, integration, bundle["base_commit"], branch=integration_branch)
        base_verification = verify_base(integration, bundle["base_commit"], bundle["base_tree"])
    else:
        integration = attempt_root / "shared-worktree"
        create_worktree(repository, integration, bundle["base_commit"])
        base_verification = verify_base(integration, bundle["base_commit"], bundle["base_tree"])
        prepare_agent_surface(integration, test_python)
        for task in bundle["tasks"]:
            agent_worktrees[task["task_id"]] = integration
    apparatus_event(database, draw["draw_id"], draw["arm"], "tree_verified", base_verification)

    radius_path = PROJECT_ROOT / bundle["radius"]["path"]
    environments: dict[str, dict[str, str]] = {}
    for task in bundle["tasks"]:
        task_id = task["task_id"]
        test_temp_root = coordination_root / "test-temp" / task_id
        test_temp_root.mkdir(parents=True, exist_ok=False)
        workspace_key = "shared" if draw["arm"] != "isolate" else f"isolate:{task_id}"
        environments[task_id] = agent_environment(
            base=os.environ,
            database=database,
            draw=draw,
            agent_id=task_id,
            worktree=agent_worktrees[task_id],
            workspace_key=workspace_key,
            radius_path=radius_path,
            test_python=test_python,
            test_timeout=float(repository_config["test_timeout_seconds"]),
            test_temp_root=test_temp_root,
            python_compat=python_compat,
        )
    states = launch_agents(
        bundle["tasks"],
        agent_worktrees,
        environments,
        attempt_root,
        float(tasks_manifest["pilot"]["model_timeout_seconds"]),
        draw["launch_order"],
    )
    model_records = {state.task["task_id"]: model_record(state, attempt_root) for state in states}
    all_finished = all(record["model_finished"] for record in model_records.values())
    if not all_finished:
        post_snapshot = verify_snapshot_unchanged(
            (PROJECT_ROOT / schedule["tasks_path"]).resolve(strict=True),
            tasks_manifest,
            apparatus_verification["artifact_sha256"],
            apparatus_verification["runtime_versions"],
        )
        schedule_hash_matches = sha256_file(Path(schedule["schedule_path"])) == schedule.get(
            "schedule_file_sha256_at_run_start"
        )
        post_snapshot["schedule_hash_matches"] = schedule_hash_matches
        post_snapshot["verified"] = bool(post_snapshot.get("verified")) and schedule_hash_matches
        if not post_snapshot["verified"]:
            apparatus_event(
                database,
                draw["draw_id"],
                draw["arm"],
                "apparatus_invalid",
                {"stage": "post_unfinished_draw_hash_verification", **post_snapshot},
            )
        excluded_events = event_rows(database, draw["draw_id"])
        exclusion_reason = (
            "at_least_one_model_never_finished"
            if post_snapshot["verified"]
            else "apparatus_invalid"
        )
        summary = {
            "schema_version": 1,
            "draw": draw,
            "attempt": attempt,
            "excluded": True,
            "exclusion_reason": exclusion_reason,
            "models": model_records,
            "base_verification": base_verification,
            "apparatus": {
                "valid": bool(post_snapshot["verified"]),
                "fingerprint": apparatus_verification["fingerprint"],
                "hook_audit": hook_audit(excluded_events, model_records),
                "post_draw_hash_verification": post_snapshot,
            },
            "database": relative(database),
        }
        apparatus_event(database, draw["draw_id"], draw["arm"], "draw_excluded", summary["exclusion_reason"] and {"reason": summary["exclusion_reason"]})
        export_events(database, draw["draw_id"], attempt_root / "events.jsonl")
        atomic_json(attempt_root / "summary.json", summary)
        return summary

    merge_records: dict[str, dict[str, Any]] = {}
    commits: dict[str, dict[str, Any]] = {}
    if draw["arm"] == "isolate":
        for task in bundle["tasks"]:
            task_id = task["task_id"]
            commits[task_id] = commit_isolated_agent(
                agent_worktrees[task_id],
                task_id,
                attempt_root / "agents" / task_id / "agent.patch",
            )
        merge_summary = merge_isolated(
            repository,
            integration,
            commits,
            draw["merge_order"],
            database,
            draw,
        )
        merge_records = {item["task_id"]: item for item in merge_summary["merges"]}
        final_commit = merge_summary["final_commit"]
    else:
        save_worktree_patch(integration, attempt_root / "integrated.patch")
        final_commit = commit_shared_final(integration, draw["draw_id"])
        merge_summary = None

    final_tree = output_text(git(integration, "rev-parse", f"{final_commit}^{{tree}}"))
    integrated_suite = run_suite(repository_config, integration, attempt_root / "integrated-full-suite.txt")
    visible_classification = classify_visible_regression(bundle, integrated_suite)
    apparatus_event(
        database,
        draw["draw_id"],
        draw["arm"],
        "test_outcome",
        {"kind": "integrated_full_suite", **integrated_suite},
    )
    integrated_hidden = evaluate_integrated_hidden(
        repository,
        repository_config,
        bundle,
        final_commit,
        attempt_root,
    )
    integrated_completed_monotonic = time.monotonic()
    if integrated_hidden.get("suite"):
        apparatus_event(
            database,
            draw["draw_id"],
            draw["arm"],
            "test_outcome",
            {"kind": "actual_integrated_hidden_suite", **integrated_hidden["suite"]},
        )
    observed_task_results = integrated_hidden.get("classification", {}).get("tasks", {})
    task_summaries: list[dict[str, Any]] = []
    all_agent_events = event_rows(database, draw["draw_id"])
    for task in bundle["tasks"]:
        task_id = task["task_id"]
        patches = agent_patches(database, draw["draw_id"], task_id)
        merge = merge_records.get(task_id) if draw["arm"] == "isolate" else None
        if draw["arm"] == "isolate":
            presence = verify_isolated_presence(
                repository,
                final_commit,
                task_id,
                commits[task_id],
            )
        else:
            presence = verify_patch_presence(
                repository,
                final_commit,
                final_tree,
                task_id,
                patches,
                attempt_root,
            )
        landed_candidate = (
            bool(patches)
            and bool(presence.get("verified"))
            and (merge is None or bool(merge.get("merged")))
            and bool(presence.get("present"))
        )
        oracle = (
            evaluate_focal(repository, repository_config, bundle, task, patches, attempt_root)
            if landed_candidate and draw["arm"] == "isolate"
            else None
        )
        if oracle and oracle.get("suite"):
            apparatus_event(
                database,
                draw["draw_id"],
                draw["arm"],
                "test_outcome",
                {"kind": "focal_oracle", "task_id": task_id, **oracle["suite"]},
                agent_id=task_id,
            )
        task_summaries.append(
            summarize_task(
                task,
                model_records[task_id],
                event_rows(database, draw["draw_id"], task_id),
                all_agent_events,
                merge,
                presence,
                oracle,
                observed_task_results.get(
                    task_id,
                    {
                        "verified": False,
                        "correct": False,
                        "wrong_verified": False,
                        "reason": "integrated_hidden_task_oracle_unavailable",
                    },
                ),
                visible_classification,
            )
        )

    post_snapshot = verify_snapshot_unchanged(
        (PROJECT_ROOT / schedule["tasks_path"]).resolve(strict=True),
        tasks_manifest,
        apparatus_verification["artifact_sha256"],
        apparatus_verification["runtime_versions"],
    )
    schedule_hash_matches = sha256_file(Path(schedule["schedule_path"])) == schedule.get(
        "schedule_file_sha256_at_run_start"
    )
    post_snapshot["schedule_hash_matches"] = schedule_hash_matches
    post_snapshot["verified"] = bool(post_snapshot.get("verified")) and schedule_hash_matches
    if not post_snapshot["verified"]:
        apparatus_event(
            database,
            draw["draw_id"],
            draw["arm"],
            "apparatus_invalid",
            {"stage": "post_draw_hash_verification", **post_snapshot},
        )
    final_events = event_rows(database, draw["draw_id"])
    hooks = hook_audit(final_events, model_records)
    if draw["arm"] == "isolate":
        isolated_audits: dict[str, dict[str, Any]] = {}
        for task in bundle["tasks"]:
            task_id = task["task_id"]
            commit = commits[task_id].get("commit")
            expected_tree = (
                output_text(git(repository, "rev-parse", f"{commit}^{{tree}}"))
                if commit
                else bundle["base_tree"]
            )
            isolated_audits[task_id] = shared_write_audit(
                repository,
                bundle["base_commit"],
                expected_tree,
                [row for row in final_events if row["agent_id"] == task_id],
                attempt_root,
                audit_name=f"isolated-write-replay-{task_id}",
                require_sequential_replay=True,
            )
        write_audit: dict[str, Any] = {
            "verified": all(item["verified"] for item in isolated_audits.values()),
            "reason": "each_isolated_branch_replayed_from_logged_writes",
            "agents": isolated_audits,
        }
    else:
        write_audit = shared_write_audit(
            repository,
            bundle["base_commit"],
            final_tree,
            final_events,
            attempt_root,
        )
    apparatus_invalid_events = [
        row for row in final_events if row["event_type"] == "apparatus_invalid"
    ]
    apparatus_valid = (
        bool(hooks["heartbeat_verified_for_every_agent"])
        and bool(write_audit["verified"])
        and bool(post_snapshot["verified"])
        and not apparatus_invalid_events
    )
    if not apparatus_valid:
        for task_summary in task_summaries:
            task_summary["pre_audit_outcome"] = task_summary["outcome"]
            task_summary["outcome"] = "unverified"
            task_summary["landed"] = False
            task_summary["landing_verified"] = False
            task_summary["blocked_then_completed"] = False
            task_summary["apparatus_invalid"] = True

    claim_denominator = sum(int(item["claim_acquisitions"]) for item in task_summaries)
    collision_numerator = sum(int(item["collision_exposed_claims"]) for item in task_summaries)
    realised_collisions = collision_metrics(
        final_events,
        [task["task_id"] for task in bundle["tasks"]],
        bundle["designed_collisions"],
    )
    blocking_wait_seconds = sum(float(item["blocking_wait_seconds"]) for item in task_summaries)
    prompt_starts = [state.prompt_released_monotonic for state in states if state.prompt_released_monotonic]
    completions = [state.completed_monotonic for state in states if state.completed_monotonic]
    agent_execution_wall = max(completions) - min(prompt_starts) if prompt_starts and completions else None
    bundle_wall_seconds = (
        integrated_completed_monotonic - min(prompt_starts) if prompt_starts else None
    )
    evaluation_wall_seconds = time.monotonic() - min(prompt_starts) if prompt_starts else None
    total_agent_minutes = sum(float(record["agent_minutes"]) for record in model_records.values())
    fleet_capacity_seconds = (
        len(states) * agent_execution_wall if agent_execution_wall is not None else None
    )
    blocking_wait_share = (
        blocking_wait_seconds / fleet_capacity_seconds
        if fleet_capacity_seconds and fleet_capacity_seconds > 0
        else 0.0
    )
    total_agent_execution_seconds = sum(
        float(record["elapsed_seconds"]) for record in model_records.values()
    )
    tail_idle_seconds = (
        max(0.0, fleet_capacity_seconds - total_agent_execution_seconds)
        if fleet_capacity_seconds is not None
        else None
    )
    tail_idle_fraction = (
        tail_idle_seconds / fleet_capacity_seconds
        if tail_idle_seconds is not None and fleet_capacity_seconds and fleet_capacity_seconds > 0
        else None
    )
    combined_idle_seconds = (
        tail_idle_seconds + blocking_wait_seconds if tail_idle_seconds is not None else None
    )
    fleet_idle_fraction = (
        combined_idle_seconds / fleet_capacity_seconds
        if combined_idle_seconds is not None and fleet_capacity_seconds and fleet_capacity_seconds > 0
        else None
    )
    merge_conflicts = (
        sum(1 for item in merge_summary["merges"] if item.get("merge_conflict"))
        if merge_summary
        else 0
    )
    metrics = {
        "collision_numerator": collision_numerator,
        "collision_denominator": claim_denominator,
        "realised_collision_rate": (
            collision_numerator / claim_denominator if claim_denominator else None
        ),
        "collision_definition": "claim acquisitions marked collision_exposed / all claim acquisitions",
        "collisions": realised_collisions,
        "bundle_wall_seconds": bundle_wall_seconds,
        "bundle_wall_definition": "first synchronized prompt release through actual integrated hidden-test outcome",
        "evaluation_wall_seconds": evaluation_wall_seconds,
        "evaluation_wall_definition": "first synchronized prompt release through completion of hidden focal evaluation",
        "agent_execution_wall_seconds": agent_execution_wall,
        "total_agent_minutes": total_agent_minutes,
        "blocking_wait_seconds": blocking_wait_seconds,
        "fleet_capacity_seconds": fleet_capacity_seconds,
        "total_agent_execution_seconds": total_agent_execution_seconds,
        "tail_idle_seconds": tail_idle_seconds,
        "tail_idle_fraction": tail_idle_fraction,
        "blocking_wait_share_of_fleet_capacity": blocking_wait_share,
        "combined_idle_seconds": combined_idle_seconds,
        "fleet_idle_fraction": fleet_idle_fraction,
        "fleet_idle_definition": "(tail slot-idle after earlier agents finish + verified blocking wait) / (agent count * concurrent model execution wall)",
        "merge_conflicts": merge_conflicts,
        "agent_count": len(states),
    }
    summary = {
        "schema_version": 1,
        "draw": draw,
        "attempt": attempt,
        "excluded": not apparatus_valid,
        "exclusion_reason": None if apparatus_valid else "apparatus_invalid",
        "apparatus_fingerprint": apparatus_verification["fingerprint"],
        "apparatus": {
            "valid": apparatus_valid,
            "hash_verification": apparatus_verification,
            "hook_audit": hooks,
            "write_replay_audit": write_audit,
            "post_draw_hash_verification": post_snapshot,
            "apparatus_invalid_events": apparatus_invalid_events,
        },
        "base_verification": base_verification,
        "models": model_records,
        "tasks": task_summaries,
        "metrics": metrics,
        "integrated": {
            "commit": final_commit,
            "tree": final_tree,
            "suite": integrated_suite,
            "visible_pre_run_green_classification": visible_classification,
            "hidden_oracle": integrated_hidden,
            "observed_bundle_outcome": (
                "abandoned"
                if final_tree == bundle["base_tree"]
                else
                "landed_and_wrong"
                if visible_classification.get("wrong_verified")
                else "unverified"
                if not visible_classification.get("verified")
                else "landed_and_correct"
                if integrated_hidden.get("classification", {}).get("correct")
                else "landed_but_task_incomplete"
                if integrated_hidden.get("classification", {}).get("verified")
                else "unverified"
            ),
            "merge": merge_summary,
        },
        "database": relative(database),
        "events": relative(attempt_root / "events.jsonl"),
    }
    apparatus_event(database, draw["draw_id"], draw["arm"], "draw_complete", {"attempt": attempt})
    export_events(database, draw["draw_id"], attempt_root / "events.jsonl")
    atomic_json(attempt_root / "summary.json", summary)
    return summary


def completed_attempt(draw_root: Path, expected_apparatus_fingerprint: str) -> dict[str, Any] | None:
    if not draw_root.exists():
        return None
    for path in sorted(draw_root.glob("attempt-*/summary.json")):
        summary = load_json(path)
        if summary.get("excluded") and summary.get("exclusion_reason") != "at_least_one_model_never_finished":
            raise RuntimeError(f"retained attempt records a non-stochastic apparatus failure: {path}")
        if not summary.get("excluded"):
            if summary.get("apparatus_fingerprint") != expected_apparatus_fingerprint:
                raise RuntimeError(f"refusing stale completed attempt with different apparatus: {path}")
            return summary
    return None


def next_attempt(draw_root: Path) -> int:
    attempts = [int(path.name.split("-")[-1]) for path in draw_root.glob("attempt-*") if path.is_dir()]
    return max(attempts, default=0) + 1


def attempt_ledger(schedule: dict[str, Any], run_root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    by_arm: dict[str, dict[str, Any]] = {
        arm: {
            "attempts": 0,
            "accepted_attempts": 0,
            "excluded_attempts": 0,
            "excluded_model_agent_minutes": 0.0,
            "exclusion_reasons": {},
        }
        for arm in {draw["arm"] for draw in schedule["draws"]}
    }
    for draw in schedule["draws"]:
        for summary_path in sorted((run_root / draw["draw_id"]).glob("attempt-*/summary.json")):
            summary = load_json(summary_path)
            excluded = bool(summary.get("excluded"))
            reason = summary.get("exclusion_reason") if excluded else None
            model_minutes = sum(
                float(model.get("agent_minutes", 0.0))
                for model in summary.get("models", {}).values()
            )
            record = {
                "draw_id": draw["draw_id"],
                "arm": draw["arm"],
                "summary": relative(summary_path),
                "summary_sha256": sha256_file(summary_path),
                "excluded": excluded,
                "exclusion_reason": reason,
                "model_agent_minutes": model_minutes,
            }
            records.append(record)
            arm = by_arm[draw["arm"]]
            arm["attempts"] += 1
            if excluded:
                arm["excluded_attempts"] += 1
                arm["excluded_model_agent_minutes"] += model_minutes
                reasons = arm["exclusion_reasons"]
                reasons[str(reason)] = int(reasons.get(str(reason), 0)) + 1
            else:
                arm["accepted_attempts"] += 1
    return {"by_arm": by_arm, "attempts": records}


def run_pilot(tasks_path: Path, schedule_path: Path, run_root: Path) -> dict[str, Any]:
    tasks = load_json(tasks_path)
    schedule = load_json(schedule_path)
    if schedule["tasks_sha256"] != sha256_file(tasks_path):
        raise RuntimeError("task manifest changed after schedule preregistration")
    if schedule.get("tasks_path") != relative(tasks_path):
        raise RuntimeError("schedule task-manifest path does not match requested manifest")
    verified_apparatus = verify_schedule_apparatus(tasks_path, tasks, schedule)
    schedule["schedule_path"] = str(schedule_path.resolve())
    schedule["schedule_file_sha256_at_run_start"] = sha256_file(schedule_path)
    completed: list[dict[str, Any]] = []
    for draw in schedule["draws"]:
        # Re-hash before every draw, including draws loaded from disk.
        verified_apparatus = verify_schedule_apparatus(tasks_path, tasks, schedule)
        draw_root = run_root / draw["draw_id"]
        existing = completed_attempt(draw_root, verified_apparatus["fingerprint"])
        if existing is not None:
            completed.append(existing)
            continue
        while True:
            attempt = next_attempt(draw_root)
            result = run_draw(tasks, schedule, draw, attempt, run_root)
            if not result["excluded"]:
                completed.append(result)
                break
            if result.get("exclusion_reason") != "at_least_one_model_never_finished":
                raise RuntimeError(
                    f"apparatus failure retained at {draw['draw_id']} attempt {attempt}; pilot stopped"
                )
    final_apparatus = verify_schedule_apparatus(tasks_path, tasks, schedule)
    if sha256_file(schedule_path) != schedule["schedule_file_sha256_at_run_start"]:
        raise RuntimeError("pilot schedule changed while the pilot was running")
    result = {
        "schema_version": 1,
        "measurement": "posture-pilot-raw-summary",
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
        "tasks_path": relative(tasks_path),
        "tasks_sha256": sha256_file(tasks_path),
        "schedule_path": relative(schedule_path),
        "schedule_sha256": sha256_file(schedule_path),
        "apparatus_fingerprint": final_apparatus["fingerprint"],
        "completed_draws": len(completed),
        "draws": completed,
        "attempt_ledger": attempt_ledger(schedule, run_root),
    }
    atomic_json(run_root.parent / "PILOT.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    schedule = subparsers.add_parser("schedule")
    schedule.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    schedule.add_argument("--output", type=Path, default=DEFAULT_SCHEDULE)
    execute = subparsers.add_parser("run")
    execute.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    execute.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE)
    execute.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    smoke = subparsers.add_parser("smoke")
    smoke.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    smoke.add_argument("--output-root", type=Path, default=DEFAULT_SMOKE_ROOT)
    args = parser.parse_args()
    if args.command == "schedule":
        result = make_schedule(args.tasks.resolve(strict=True), args.output.resolve())
        print(json.dumps({"draw_count": result["draw_count"], "output": str(args.output)}, indent=2))
        return 0
    if args.command == "smoke":
        result = run_live_smoke(
            args.tasks.resolve(strict=True),
            args.output_root.resolve(),
        )
        print(json.dumps({"contract": result["contract"], "output": str(args.output_root)}, indent=2))
        return 0
    result = run_pilot(
        args.tasks.resolve(strict=True),
        args.schedule.resolve(strict=True),
        args.run_root.resolve(),
    )
    print(json.dumps({"completed_draws": result["completed_draws"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
