"""Freeze and validate the reverted-commit task design before any pilot run."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from instruments.posture.gate_repository import normalized_junit
from instruments.posture.radius import FrozenCochangeRadius
from instruments.posture.task_builder import atomic_json, first_parent_diff, load_stream, paths_for_commit


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


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


def relative(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def compatibility_sitecustomize(repository: dict[str, Any]) -> Path:
    compatibility = Path(repository["python_compat"])
    if not compatibility.is_absolute():
        compatibility = PROJECT_ROOT / compatibility
    compatibility = compatibility.resolve(strict=True)
    sitecustomize = compatibility / "sitecustomize.py"
    if not sitecustomize.is_file():
        raise ValueError(f"Python compatibility layer is missing: {sitecustomize}")
    cache = compatibility / "__pycache__"
    if cache.exists():
        raise ValueError(f"loadable compatibility bytecode is forbidden: {cache}")
    return sitecustomize


def test_pythonpath(repository: dict[str, Any], worktree: Path) -> str:
    """Return the frozen compatibility layer plus this checkout's source root."""

    sitecustomize = compatibility_sitecustomize(repository)
    compatibility = sitecustomize.parent
    source_root = worktree / "src" if (worktree / "src").is_dir() else worktree
    shadows = {
        candidate.resolve()
        for candidate in (worktree / "sitecustomize.py", source_root / "sitecustomize.py")
        if candidate.is_file()
    }
    if shadows:
        raise ValueError(f"worktree shadows frozen sitecustomize.py: {sorted(map(str, shadows))}")
    return os.pathsep.join((str(compatibility), str(source_root)))


def compatibility_probe(design: dict[str, Any]) -> dict[str, Any]:
    repository = design["repository"]
    python = Path(repository["test_python"])
    if not python.is_absolute():
        python = PROJECT_ROOT / python
    python = python.resolve(strict=True)
    sitecustomize = compatibility_sitecustomize(repository)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(sitecustomize.parent)
    script = (
        "import collections, json, sitecustomize, sys; "
        "print(json.dumps({'python': sys.version, "
        "'sitecustomize': sitecustomize.__file__, "
        "'iterable_module': collections.Iterable.__module__, "
        "'iterable_name': collections.Iterable.__name__}, sort_keys=True))"
    )
    result = run([str(python), "-c", script], cwd=PROJECT_ROOT, environment=environment)
    payload = json.loads(text(result))
    if Path(payload["sitecustomize"]).resolve() != sitecustomize.resolve():
        raise RuntimeError("test interpreter loaded the wrong sitecustomize module")
    if (payload["iterable_module"], payload["iterable_name"]) != (
        "collections.abc",
        "Iterable",
    ):
        raise RuntimeError("test interpreter did not activate collections.Iterable compatibility")
    return {
        **payload,
        "sitecustomize_sha256": sha256(sitecustomize.read_bytes()),
        "PYTHONPATH": environment["PYTHONPATH"],
    }


def normalized_task_fields(task: dict[str, Any], *, bundle_id: str) -> dict[str, str]:
    """Return the preregistered prompt provenance, accepting legacy aliases.

    The normalized manifest always emits the three canonical keys.  Preparation
    fails closed when issue text or its retrieval provenance is absent; the
    commit message is independently recovered from Git during construction.
    """

    aliases = {
        "task_text": ("task_text", "issue_text", "issue_body"),
        "task_source_url": ("task_source_url", "issue_url", "source_url"),
        "task_retrieved_at": ("task_retrieved_at", "issue_retrieved_at", "retrieved_at"),
    }
    values: dict[str, str] = {}
    for canonical, candidates in aliases.items():
        value = next(
            (
                task.get(candidate)
                for candidate in candidates
                if isinstance(task.get(candidate), str) and task[candidate].strip()
            ),
            None,
        )
        if value is None:
            raise ValueError(
                f"task {task.get('task_id')!r} in {bundle_id} lacks nonempty {canonical}"
            )
        values[canonical] = value.strip()
    parsed_url = urlparse(values["task_source_url"])
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError(
            f"task {task.get('task_id')!r} in {bundle_id} has non-HTTPS task_source_url"
        )
    try:
        retrieved = dt.datetime.fromisoformat(values["task_retrieved_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"task {task.get('task_id')!r} in {bundle_id} has invalid task_retrieved_at"
        ) from exc
    if retrieved.tzinfo is None:
        raise ValueError(
            f"task {task.get('task_id')!r} in {bundle_id} has timezone-naive task_retrieved_at"
        )
    values["task_retrieved_at"] = retrieved.isoformat(timespec="seconds")
    return values


def validate_repository_gate(design: dict[str, Any], repository: Path) -> dict[str, Any]:
    gate_value = design.get("repository_gate")
    if not isinstance(gate_value, str) or not gate_value.strip():
        raise ValueError("design must provide top-level repository_gate path")
    gate_path = Path(gate_value)
    if not gate_path.is_absolute():
        gate_path = PROJECT_ROOT / gate_path
    gate_path = gate_path.resolve(strict=True)
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    if gate.get("measurement") != "posture-repository-eligibility-gate":
        raise ValueError("repository_gate is not a posture eligibility gate")
    if gate.get("status") != "eligible" or not gate.get("identical_normalized_results"):
        raise ValueError("repository_gate did not establish eligibility and determinism")
    try:
        gate_repository = Path(gate["repository"]).resolve(strict=True)
    except (KeyError, TypeError, FileNotFoundError) as exc:
        raise ValueError("repository_gate has invalid repository provenance") from exc
    if gate_repository != repository:
        raise ValueError("repository_gate was run against a different clone")
    protocol = gate.get("protocol")
    records = gate.get("runs")
    signatures = gate.get("normalized_signatures")
    if not isinstance(protocol, dict) or protocol.get("required_runs") != 5:
        raise ValueError("repository_gate did not preregister exactly five runs")
    if not isinstance(records, list) or len(records) != 5:
        raise ValueError("repository_gate must contain exactly five completed runs")
    if (
        not isinstance(signatures, list)
        or len(signatures) != 5
        or any(not isinstance(signature, str) or not signature for signature in signatures)
        or len(set(signatures)) != 1
    ):
        raise ValueError("repository_gate normalized signatures are absent or differ")
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("returncode") != 0
            or record.get("timed_out") is not False
            or record.get("normalized") is None
            or record.get("state_matches_baseline_before") is not True
            or record.get("state_matches_baseline_after") is not True
        ):
            raise ValueError("repository_gate contains an unsuccessful or state-changing run")
        elapsed = record.get("elapsed_seconds")
        if not isinstance(elapsed, (int, float)) or elapsed < 0 or elapsed > 120.0:
            raise ValueError("repository_gate contains a run over the 120-second ceiling")
        if record["normalized"].get("cases_sha256") != signatures[0]:
            raise ValueError("repository_gate run signature disagrees with its summary")
    baseline = gate.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("repository_gate lacks a baseline state")
    actual_head = text(git(repository, "rev-parse", "HEAD"))
    actual_tree = text(git(repository, "rev-parse", "HEAD^{tree}"))
    if baseline.get("head") != actual_head or baseline.get("head_tree") != actual_tree:
        raise ValueError("repository clone no longer matches its accepted gate baseline")
    return {
        "path": relative(gate_path),
        "sha256": sha256(gate_path.read_bytes()),
        "status": "eligible",
        "required_runs": 5,
        "runtime_ceiling_seconds": 120.0,
        "normalized_signature": signatures[0],
        "run_elapsed_seconds": [record["elapsed_seconds"] for record in records],
        "head": actual_head,
        "tree": actual_tree,
    }


def validate_design(
    design: dict[str, Any],
    repository: Path,
    commits_by_sha: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    pilot = design.get("pilot")
    if not isinstance(pilot, dict):
        raise ValueError("design pilot configuration is missing")
    arms = pilot.get("arms")
    if not isinstance(arms, list) or arms != ["advisory", "blocking", "isolate"]:
        raise ValueError("pilot arms must be exactly advisory, blocking, isolate in that order")
    if pilot.get("draws_per_cell") != 5:
        raise ValueError("pilot draws_per_cell must be exactly 5")
    concurrent_agents = pilot.get("concurrent_agents_per_draw")
    if (
        isinstance(concurrent_agents, bool)
        or not isinstance(concurrent_agents, int)
        or not 4 <= concurrent_agents <= 6
    ):
        raise ValueError("pilot concurrent_agents_per_draw must be an integer from 4 to 6")
    model_timeout = pilot.get("model_timeout_seconds")
    if (
        isinstance(model_timeout, bool)
        or not isinstance(model_timeout, (int, float))
        or model_timeout <= 0
    ):
        raise ValueError("pilot model_timeout_seconds must be positive")
    random_seed = pilot.get("random_seed")
    try:
        parsed_seed = int(str(random_seed), 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("pilot random_seed must be parseable by int(value, 0)") from exc
    if parsed_seed < 0:
        raise ValueError("pilot random_seed must be nonnegative")
    pytest_arguments = design.get("repository", {}).get("pytest_arguments")
    if not isinstance(pytest_arguments, list) or any(
        not isinstance(argument, str) for argument in pytest_arguments
    ):
        raise ValueError("repository pytest_arguments must be a list of strings")
    if any(argument.startswith("--junitxml") for argument in pytest_arguments):
        raise ValueError("pytest_arguments must not override preparation JUnit evidence")
    bundles = design.get("bundles")
    if not isinstance(bundles, list) or not bundles:
        raise ValueError("design must contain task bundles")
    conditions: list[str] = []
    normalized_tasks: dict[tuple[str, str], dict[str, str]] = {}
    seen_bundle_ids: set[str] = set()
    for bundle in bundles:
        if not isinstance(bundle, dict):
            raise ValueError("bundle entries must be objects")
        bundle_id = bundle.get("bundle_id")
        if not isinstance(bundle_id, str) or not bundle_id or bundle_id in seen_bundle_ids:
            raise ValueError("bundle_id values must be nonempty and unique")
        seen_bundle_ids.add(bundle_id)
        tasks = bundle.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != concurrent_agents:
            raise ValueError(
                f"bundle {bundle_id} must contain exactly {concurrent_agents} concurrent tasks"
            )
        expected_pairs = bundle.get("expected_pairs_with_collision")
        if not isinstance(expected_pairs, int) or expected_pairs < 0:
            raise ValueError(
                f"bundle {bundle_id} must preregister expected_pairs_with_collision"
            )
        conditions.append("independent-control" if expected_pairs == 0 else "overlapping")
        task_ids: set[str] = set()
        anchor_value = bundle.get("anchor_sha")
        anchor = (
            anchor_value
            if isinstance(anchor_value, str) and anchor_value in commits_by_sha
            else text(git(repository, "rev-parse", f"{anchor_value}^{{commit}}"))
        )
        anchor_stream_commit = commits_by_sha.get(anchor)
        if anchor_stream_commit is None:
            raise ValueError(f"bundle {bundle_id} anchor is absent from replay stream")
        for task in tasks:
            if not isinstance(task, dict):
                raise ValueError(f"bundle {bundle_id} task entries must be objects")
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id or task_id in task_ids:
                raise ValueError(f"bundle {bundle_id} task_id values must be nonempty and unique")
            task_ids.add(task_id)
            normalized_tasks[(bundle_id, task_id)] = normalized_task_fields(
                task, bundle_id=bundle_id
            )
            try:
                task_sha_value = task["sha"]
            except (KeyError, TypeError) as exc:
                raise ValueError(f"task {task_id} in {bundle_id} lacks commit SHA") from exc
            commit_sha = (
                task_sha_value
                if isinstance(task_sha_value, str) and task_sha_value in commits_by_sha
                else text(git(repository, "rev-parse", f"{task_sha_value}^{{commit}}"))
            )
            stream_commit = commits_by_sha.get(commit_sha)
            if stream_commit is None:
                raise ValueError(f"task {task_id} in {bundle_id} is absent from replay stream")
            if stream_commit["index"] > anchor_stream_commit["index"]:
                raise ValueError(
                    f"task {task_id} in {bundle_id} is not on anchor's first-parent ancestry"
                )
            if not stream_commit.get("parents"):
                raise ValueError(f"task {task_id} in {bundle_id} is a root commit")
            changes = stream_commit.get("changes")
            if not isinstance(changes, list) or not changes:
                raise ValueError(f"task {task_id} in {bundle_id} has no changed paths")
            if any(change.get("status") != "M" for change in changes):
                raise ValueError(
                    f"task {task_id} in {bundle_id} is not compatible M-only ground truth"
                )
            paths = paths_for_commit(stream_commit)
            if not any(path.startswith("tests/") for path in paths):
                raise ValueError(f"task {task_id} in {bundle_id} lacks historical test hunks")
            for path in paths:
                pure = PurePosixPath(path)
                if (
                    not path
                    or "\\" in path
                    or pure.is_absolute()
                    or ".." in pure.parts
                    or pure.as_posix() != path
                ):
                    raise ValueError(
                        f"task {task_id} in {bundle_id} has unsafe claim path {path!r}"
                    )
    if "overlapping" not in conditions or "independent-control" not in conditions:
        raise ValueError("design must contain both overlapping and independent-control bundles")
    return {"conditions": conditions, "normalized_tasks": normalized_tasks}


def validate_first_parent_stream(commits: list[dict[str, Any]]) -> None:
    previous_sha: str | None = None
    for expected_index, commit in enumerate(commits):
        if commit.get("index") != expected_index:
            raise ValueError("replay extraction stream indexes are not contiguous")
        parents = commit.get("parents")
        if not isinstance(parents, list):
            raise ValueError("replay extraction stream has invalid parent metadata")
        if previous_sha is not None and (not parents or parents[0] != previous_sha):
            raise ValueError("replay extraction stream is not a continuous first-parent chain")
        previous_sha = commit.get("sha")


def commit_parent(repository: Path, sha: str) -> str:
    parents = text(git(repository, "rev-list", "--parents", "-n", "1", sha)).split()
    if len(parents) < 2:
        raise ValueError(f"root commit cannot be a task: {sha}")
    return parents[1]


def parent_count(repository: Path, sha: str) -> int:
    return len(text(git(repository, "rev-list", "--parents", "-n", "1", sha)).split()) - 1


def create_worktree(repository: Path, path: Path, commit: str) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to replace preparation evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repository, "worktree", "add", "--detach", str(path), commit)


def revert_task(worktree: Path, sha: str) -> None:
    arguments = ["revert", "--no-commit"]
    if parent_count(worktree, sha) > 1:
        arguments.extend(["-m", "1"])
    arguments.append(sha)
    git(worktree, *arguments)


def deterministic_commit(repository: Path, tree: str, parent: str, message: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Posture Apparatus",
            "GIT_AUTHOR_EMAIL": "posture-apparatus@example.invalid",
            "GIT_COMMITTER_NAME": "Posture Apparatus",
            "GIT_COMMITTER_EMAIL": "posture-apparatus@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        }
    )
    result = run(
        ["git", "commit-tree", tree, "-p", parent],
        cwd=repository,
        stdin=(message.rstrip() + "\n").encode("utf-8"),
        environment=environment,
    )
    return text(result)


def apply_patch(worktree: Path, patch: bytes, *, index: bool) -> subprocess.CompletedProcess[bytes]:
    arguments = ["apply", "--binary", "--whitespace=nowarn"]
    if index:
        arguments.append("--index")
    arguments.append("-")
    return git(worktree, *arguments, stdin=patch, check=False)


def tests_only_diff(repository: Path, parent: str, sha: str) -> bytes:
    return git(
        repository,
        "diff",
        "--binary",
        "--full-index",
        "--find-renames=50%",
        "-l0",
        parent,
        sha,
        "--",
        "tests/",
    ).stdout


def apply_records(worktree: Path, tasks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    applications: list[dict[str, Any]] = []
    for task in sorted(tasks, key=lambda item: item["stream_index"]):
        patch = Path(task["ground_truth_patch_absolute"]).read_bytes()
        result = apply_patch(worktree, patch, index=True)
        applications.append(
            {
                "task_id": task["task_id"],
                "exit_code": result.returncode,
                "stderr": result.stderr.decode("utf-8", errors="replace"),
            }
        )
        if result.returncode:
            break
    return applications


def test_command(design: dict[str, Any], worktree: Path, output: Path) -> dict[str, Any]:
    repository = design["repository"]
    python = Path(repository["test_python"])
    if not python.is_absolute():
        python = PROJECT_ROOT / python
    python = python.resolve(strict=True)
    junit_path = output.with_suffix(".junit.xml")
    arguments = [
        str(python),
        "-m",
        "pytest",
        *repository["pytest_arguments"],
        f"--junitxml={junit_path}",
    ]
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTEST_ADDOPTS"] = ""
    environment["PYTHONPATH"] = test_pythonpath(repository, worktree)
    output.parent.mkdir(parents=True, exist_ok=True)
    before = {
        "index_tree": text(git(worktree, "write-tree")),
        "tracked_status": text(git(worktree, "status", "--porcelain", "--untracked-files=no")),
        "tracked_diff_sha256": sha256(git(worktree, "diff", "--binary", "--no-ext-diff").stdout),
    }
    started = dt.datetime.now(dt.timezone.utc)
    result = run(
        arguments,
        cwd=worktree,
        environment=environment,
        check=False,
        timeout=float(repository["test_timeout_seconds"]),
    )
    completed = dt.datetime.now(dt.timezone.utc)
    payload = result.stdout + result.stderr
    output.write_bytes(payload)
    after = {
        "index_tree": text(git(worktree, "write-tree")),
        "tracked_status": text(git(worktree, "status", "--porcelain", "--untracked-files=no")),
        "tracked_diff_sha256": sha256(git(worktree, "diff", "--binary", "--no-ext-diff").stdout),
    }
    if after != before:
        raise RuntimeError(f"test suite changed tracked state in {worktree}")
    normalized = normalized_junit(junit_path) if junit_path.exists() else None
    normalized_summary = (
        {
            "counts": normalized["counts"],
            "case_count": normalized["case_count"],
            "cases_sha256": normalized["cases_sha256"],
            "cases": normalized["cases"],
        }
        if normalized is not None
        else None
    )
    return {
        "command": arguments,
        "exit_code": result.returncode,
        "started_at_utc": started.isoformat(timespec="milliseconds"),
        "completed_at_utc": completed.isoformat(timespec="milliseconds"),
        "elapsed_seconds": (completed - started).total_seconds(),
        "output_sha256": sha256(payload),
        "output_path": output.relative_to(PROJECT_ROOT).as_posix(),
        "junit_path": junit_path.relative_to(PROJECT_ROOT).as_posix() if junit_path.exists() else None,
        "junit_sha256": sha256(junit_path.read_bytes()) if junit_path.exists() else None,
        "normalized": normalized_summary,
        "PYTHONPATH": environment["PYTHONPATH"],
        "tracked_state_before": before,
        "tracked_state_after": after,
    }


def validate_sequence(
    repository: Path,
    preparation_root: Path,
    bundle_id: str,
    label: str,
    base_commit: str,
    tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    path = preparation_root / bundle_id / "checks" / label
    create_worktree(repository, path, base_commit)
    applications = apply_records(path, tasks)
    tree = text(git(path, "write-tree")) if all(item["exit_code"] == 0 for item in applications) else None
    return {
        "label": label,
        "tasks": [task["task_id"] for task in sorted(tasks, key=lambda item: item["stream_index"])],
        "applications": applications,
        "result_tree": tree,
        "all_applied": bool(applications) and all(item["exit_code"] == 0 for item in applications),
        "worktree": path.relative_to(PROJECT_ROOT).as_posix(),
    }


def validate_base_determinism(
    design: dict[str, Any],
    worktree: Path,
    artifact_root: Path,
    bundle_id: str,
) -> dict[str, Any]:
    records = [
        test_command(
            design,
            worktree,
            artifact_root / bundle_id / f"baseline-test-{index}.txt",
        )
        for index in range(1, 6)
    ]
    signatures = [
        record["normalized"]["cases_sha256"] if record["normalized"] else None
        for record in records
    ]
    if any(record["exit_code"] != 0 for record in records):
        raise RuntimeError(f"reverted baseline suite failed for {bundle_id}")
    if any(record["elapsed_seconds"] > 120.0 for record in records):
        raise RuntimeError(f"reverted baseline suite exceeded 120 seconds for {bundle_id}")
    if any(signature is None for signature in signatures) or len(set(signatures)) != 1:
        raise RuntimeError(f"reverted baseline suite is not deterministic for {bundle_id}")
    return {
        "required_runs": 5,
        "identical_normalized_results": True,
        "normalized_signature": signatures[0],
        "runs": records,
    }


def validate_focal_oracle(
    design: dict[str, Any],
    repository: Path,
    preparation_root: Path,
    artifact_root: Path,
    bundle_id: str,
    base_commit: str,
    focal: dict[str, Any],
    other_tasks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prove historical focal tests discriminate source absence from full GT."""

    prefix = f"focal-{focal['task_id']}"
    red_path = preparation_root / bundle_id / "checks" / f"{prefix}-hidden-red"
    create_worktree(repository, red_path, base_commit)
    red_prefix_applications = apply_records(red_path, other_tasks)
    if not red_prefix_applications or not all(
        item["exit_code"] == 0 for item in red_prefix_applications
    ):
        raise RuntimeError(f"other ground truths failed before hidden oracle {bundle_id}/{focal['task_id']}")
    source_absent_green = test_command(
        design,
        red_path,
        artifact_root / bundle_id / f"{prefix}-source-absent-green.txt",
    )
    if source_absent_green["exit_code"] != 0:
        raise RuntimeError(
            f"base plus other ground truths is not green for {bundle_id}/{focal['task_id']}"
        )
    test_patch = Path(focal["ground_truth_test_patch_absolute"]).read_bytes()
    hidden_application = apply_patch(red_path, test_patch, index=True)
    if hidden_application.returncode:
        raise RuntimeError(
            f"hidden historical tests do not apply after other ground truths for "
            f"{bundle_id}/{focal['task_id']}: "
            + hidden_application.stderr.decode("utf-8", errors="replace")
        )
    hidden_red = test_command(
        design,
        red_path,
        artifact_root / bundle_id / f"{prefix}-hidden-red.txt",
    )
    hidden_counts = (hidden_red.get("normalized") or {}).get("counts", {})
    observed_failures = int(hidden_counts.get("failure", 0)) + int(hidden_counts.get("error", 0))
    if hidden_red["exit_code"] == 0 or observed_failures < 1:
        raise RuntimeError(
            f"historical tests do not discriminate the missing focal change for "
            f"{bundle_id}/{focal['task_id']}"
        )

    green_path = preparation_root / bundle_id / "checks" / f"{prefix}-full-green"
    create_worktree(repository, green_path, base_commit)
    green_prefix_applications = apply_records(green_path, other_tasks)
    if not green_prefix_applications or not all(
        item["exit_code"] == 0 for item in green_prefix_applications
    ):
        raise RuntimeError(f"other ground truths failed before full oracle {bundle_id}/{focal['task_id']}")
    full_application = apply_patch(
        green_path,
        Path(focal["ground_truth_patch_absolute"]).read_bytes(),
        index=True,
    )
    if full_application.returncode:
        raise RuntimeError(
            f"full focal GT does not apply after other ground truths for "
            f"{bundle_id}/{focal['task_id']}: "
            + full_application.stderr.decode("utf-8", errors="replace")
        )
    full_green = test_command(
        design,
        green_path,
        artifact_root / bundle_id / f"{prefix}-full-green.txt",
    )
    if full_green["exit_code"] != 0:
        raise RuntimeError(f"full focal GT is not green for {bundle_id}/{focal['task_id']}")
    hidden_failure_cases = [
        {
            "classname": case["classname"],
            "name": case["name"],
            "hidden_outcome": case["outcome"],
            "ground_truth_outcome": "passed",
        }
        for case in hidden_red["normalized"]["cases"]
        if case["outcome"] in {"failure", "error"}
    ]
    full_green_index = {
        (case["classname"], case["name"]): case["outcome"]
        for case in full_green["normalized"]["cases"]
    }
    not_ground_truth_green = [
        case
        for case in hidden_failure_cases
        if full_green_index.get((case["classname"], case["name"])) != "passed"
    ]
    if not hidden_failure_cases or not_ground_truth_green:
        raise RuntimeError(
            f"hidden failing cases lack exact ground-truth-green identities for "
            f"{bundle_id}/{focal['task_id']}: {not_ground_truth_green}"
        )
    return {
        "task_id": focal["task_id"],
        "precondition": "same synthetic base plus every other bundle ground truth",
        "other_ground_truth_order": [
            task["task_id"] for task in sorted(other_tasks, key=lambda item: item["stream_index"])
        ],
        "red_branch": {
            "worktree": relative(red_path),
            "other_ground_truth_applications": red_prefix_applications,
            "source_absent_suite_green": source_absent_green,
            "hidden_test_patch_application": {
                "exit_code": hidden_application.returncode,
                "stderr": hidden_application.stderr.decode("utf-8", errors="replace"),
            },
            "hidden_tests_suite_red": hidden_red,
            "observed_failure_or_error_count": observed_failures,
        },
        "green_branch": {
            "worktree": relative(green_path),
            "other_ground_truth_applications": green_prefix_applications,
            "full_ground_truth_application": {
                "exit_code": full_application.returncode,
                "stderr": full_application.stderr.decode("utf-8", errors="replace"),
            },
            "full_ground_truth_suite_green": full_green,
        },
        "discriminating": True,
        "expected_focal_cases": hidden_failure_cases,
    }


def collision_design(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    pairs: list[dict[str, Any]] = []
    degrees = {task["task_id"]: 0 for task in tasks}
    for left, right in itertools.combinations(tasks, 2):
        shared = sorted(set(left["ground_truth_paths"]) & set(right["ground_truth_paths"]))
        if shared:
            degrees[left["task_id"]] += 1
            degrees[right["task_id"]] += 1
        pairs.append(
            {
                "left": left["task_id"],
                "right": right["task_id"],
                "shared_ground_truth_paths": shared,
                "designed_collision_opportunity": bool(shared),
            }
        )
    return {
        "claim_policy": "one atomic task-lifetime claim set; writes require whole-file [0,2**63-1) claims",
        "pairs_total": len(pairs),
        "pairs_with_opportunity": sum(pair["designed_collision_opportunity"] for pair in pairs),
        "task_overlap_degree": degrees,
        "pairs": pairs,
    }


def build_bundle(
    design: dict[str, Any],
    bundle: dict[str, Any],
    commits_by_sha: dict[str, dict[str, Any]],
    preparation_root: Path,
    artifact_root: Path,
) -> dict[str, Any]:
    repository = Path(design["repository"]["clone"]).resolve(strict=True)
    bundle_id = bundle["bundle_id"]
    anchor_sha = text(git(repository, "rev-parse", f"{bundle['anchor_sha']}^{{commit}}"))
    anchor_tree = text(git(repository, "rev-parse", f"{anchor_sha}^{{tree}}"))
    worktree = preparation_root / bundle_id / "reverted-base"
    create_worktree(repository, worktree, anchor_sha)
    task_artifact_root = artifact_root / bundle_id / "ground-truth"
    task_artifact_root.mkdir(parents=True, exist_ok=True)
    task_records: list[dict[str, Any]] = []
    for task_design in bundle["tasks"]:
        sha = text(git(repository, "rev-parse", f"{task_design['sha']}^{{commit}}"))
        stream_commit = commits_by_sha.get(sha)
        if stream_commit is None:
            raise ValueError(f"task is absent from replay extraction stream: {sha}")
        patch = first_parent_diff(repository, stream_commit)
        patch_path = task_artifact_root / f"{task_design['task_id']}.patch"
        patch_path.write_bytes(patch)
        parent = commit_parent(repository, sha)
        test_patch = tests_only_diff(repository, parent, sha)
        if not test_patch:
            raise ValueError(
                f"task {task_design['task_id']} in {bundle_id} has empty historical test patch"
            )
        test_patch_path = task_artifact_root / f"{task_design['task_id']}.tests.patch"
        test_patch_path.write_bytes(test_patch)
        prompt_provenance = normalized_task_fields(task_design, bundle_id=bundle_id)
        commit_message = text(git(repository, "show", "-s", "--format=%B", sha))
        if not commit_message:
            raise ValueError(f"task {task_design['task_id']} in {bundle_id} has empty commit message")
        task_records.append(
            {
                **task_design,
                **prompt_provenance,
                # Retained for the current pilot prompt reader; canonical
                # provenance is always available under task_* above.
                "issue_text": prompt_provenance["task_text"],
                "issue_url": prompt_provenance["task_source_url"],
                "issue_retrieved_at": prompt_provenance["task_retrieved_at"],
                "sha": sha,
                "first_parent": parent,
                "stream_index": stream_commit["index"],
                "timestamp": stream_commit["timestamp"],
                "ground_truth_paths": list(paths_for_commit(stream_commit)),
                "ground_truth_patch": patch_path.relative_to(PROJECT_ROOT).as_posix(),
                "ground_truth_patch_absolute": str(patch_path),
                "ground_truth_patch_sha256": sha256(patch),
                "ground_truth_patch_bytes": len(patch),
                "ground_truth_test_paths": sorted(
                    path for path in paths_for_commit(stream_commit) if path.startswith("tests/")
                ),
                "ground_truth_test_patch": test_patch_path.relative_to(PROJECT_ROOT).as_posix(),
                "ground_truth_test_patch_absolute": str(test_patch_path),
                "ground_truth_test_patch_sha256": sha256(test_patch),
                "ground_truth_test_patch_bytes": len(test_patch),
                "message": commit_message,
            }
        )
    if len({task["sha"] for task in task_records}) != len(task_records):
        raise ValueError(f"duplicate task in bundle {bundle_id}")
    for task in sorted(task_records, key=lambda item: item["stream_index"], reverse=True):
        revert_task(worktree, task["sha"])
    if text(git(worktree, "diff", "--name-only", "--diff-filter=U")):
        raise RuntimeError(f"unresolved cumulative revert in {bundle_id}")
    base_tree = text(git(worktree, "write-tree"))
    base_commit = deterministic_commit(repository, base_tree, anchor_sha, f"posture synthetic base: {bundle_id}")
    for task in task_records:
        for changed_path in task["ground_truth_paths"]:
            entry = text(git(repository, "ls-tree", base_commit, "--", changed_path)).split()
            if not entry or entry[0] not in {"100644", "100755"}:
                raise RuntimeError(
                    f"whole-file claim path {changed_path!r} is absent or irregular in {bundle_id} base"
                )
    reference = f"refs/posture/bases/{bundle_id}"
    current = text(git(repository, "rev-parse", "--verify", reference, check=False))
    if current and current != base_commit:
        raise RuntimeError(f"existing {reference} disagrees with prepared base")
    git(repository, "update-ref", reference, base_commit)

    # Check each task against the same all-reverted tree.  This prevents a
    # temporally stacked series from masquerading as independent concurrent
    # tasks merely because the cumulative reverts happened to be clean.
    individual_checks: list[dict[str, Any]] = []
    for task in task_records:
        result = git(
            worktree,
            "apply",
            "--check",
            "--binary",
            "--whitespace=nowarn",
            "-",
            stdin=Path(task["ground_truth_patch_absolute"]).read_bytes(),
            check=False,
        )
        individual_checks.append(
            {
                "task_id": task["task_id"],
                "applies_to_common_base": result.returncode == 0,
                "stderr": result.stderr.decode("utf-8", errors="replace"),
            }
        )
    if not all(check["applies_to_common_base"] for check in individual_checks):
        raise RuntimeError(f"at least one task ground truth does not independently apply in {bundle_id}")

    sequences: list[dict[str, Any]] = []
    sequences.append(
        validate_sequence(repository, preparation_root, bundle_id, "all", base_commit, task_records)
    )
    for focal in task_records:
        sequences.append(
            validate_sequence(
                repository,
                preparation_root,
                bundle_id,
                f"oracle-without-{focal['task_id']}",
                base_commit,
                [task for task in task_records if task["task_id"] != focal["task_id"]],
            )
        )
    if not all(sequence["all_applied"] for sequence in sequences):
        raise RuntimeError(f"an evaluator oracle subset failed to apply in {bundle_id}")
    if sequences[0]["result_tree"] != anchor_tree:
        raise RuntimeError(f"all ground truths do not reconstruct anchor tree for {bundle_id}")
    for sequence in sequences:
        oracle_output = (
            artifact_root
            / bundle_id
            / f"{sequence['label']}-pre-run-test.txt"
        )
        sequence["pre_run_test"] = test_command(
            design,
            PROJECT_ROOT / sequence["worktree"],
            oracle_output,
        )
        if sequence["pre_run_test"]["exit_code"] != 0:
            raise RuntimeError(
                f"pre-run evaluator oracle suite failed for {bundle_id}/{sequence['label']}"
            )

    focal_oracles = [
        validate_focal_oracle(
            design,
            repository,
            preparation_root,
            artifact_root,
            bundle_id,
            base_commit,
            focal,
            [task for task in task_records if task["task_id"] != focal["task_id"]],
        )
        for focal in task_records
    ]
    focal_oracle_by_task = {oracle["task_id"]: oracle for oracle in focal_oracles}
    for task in task_records:
        task["expected_focal_cases"] = focal_oracle_by_task[task["task_id"]][
            "expected_focal_cases"
        ]
    baseline_determinism = validate_base_determinism(
        design,
        worktree,
        artifact_root,
        bundle_id,
    )
    collision = collision_design(task_records)
    expected_pairs = bundle["expected_pairs_with_collision"]
    if collision["pairs_with_opportunity"] != expected_pairs:
        raise RuntimeError(
            f"designed collision count changed for {bundle_id}: "
            f"{collision['pairs_with_opportunity']} != {expected_pairs}"
        )
    required_seed = bundle.get("required_shared_seed_path")
    if required_seed is not None and any(
        required_seed not in pair["shared_ground_truth_paths"]
        for pair in collision["pairs"]
    ):
        raise RuntimeError(
            f"bundle {bundle_id} is not a clique on preregistered seed path {required_seed}"
        )
    degrees = collision["task_overlap_degree"]
    if expected_pairs == 0:
        if any(degree != 0 for degree in degrees.values()):
            raise RuntimeError(f"independent control bundle {bundle_id} contains overlap")
        collision_condition = "independent-control"
    else:
        if any(degree < 1 for degree in degrees.values()):
            raise RuntimeError(f"overlap bundle {bundle_id} contains an independent task")
        collision_condition = "overlapping"
    for task in task_records:
        task.pop("ground_truth_patch_absolute", None)
        task.pop("ground_truth_test_patch_absolute", None)
    return {
        "bundle_id": bundle_id,
        "factor": bundle["factor"],
        "collision_condition": collision_condition,
        "anchor_sha": anchor_sha,
        "anchor_tree": anchor_tree,
        "base_commit": base_commit,
        "base_tree": base_tree,
        "base_ref": reference,
        "tasks": task_records,
        "designed_collisions": collision,
        "construction": {
            "cumulative_revert_order": [
                task["task_id"] for task in sorted(task_records, key=lambda item: item["stream_index"], reverse=True)
            ],
            "individual_ground_truth_checks": individual_checks,
            "oracle_sequence_checks": sequences,
            "focal_hidden_oracles": focal_oracles,
            "all_ground_truth_tree_equals_anchor": True,
            "baseline_determinism": baseline_determinism,
            "evidence_worktree": worktree.relative_to(PROJECT_ROOT).as_posix(),
        },
    }


def build_radius(
    design: dict[str, Any],
    bundle: dict[str, Any],
    artifact_root: Path,
    *,
    cutoff_index: int,
    cutoff_sha: str,
) -> dict[str, Any]:
    configuration = design["radius"]
    stream = Path(configuration["stream"])
    if not stream.is_absolute():
        stream = PROJECT_ROOT / stream
    metadata_value = configuration.get("metadata")
    metadata = Path(metadata_value) if metadata_value is not None else None
    if metadata is not None and not metadata.is_absolute():
        metadata = PROJECT_ROOT / metadata
    radius = FrozenCochangeRadius.from_stream(
        stream.resolve(strict=True),
        cutoff_index=cutoff_index,
        expected_cutoff_sha=cutoff_sha,
        top_k=configuration["top_k"],
        threshold=configuration["threshold"],
        threshold_inclusive=configuration["threshold_inclusive"],
        decayed=configuration["decayed"],
        metadata_path=metadata.resolve(strict=True) if metadata is not None else None,
    )
    required_paths = sorted(
        {
            path
            for task in bundle["tasks"]
            for path in task["ground_truth_paths"]
        }
    )
    missing_required_paths = sorted(set(required_paths) - set(radius.live_paths))
    if missing_required_paths:
        raise RuntimeError(
            f"bundle {bundle['bundle_id']} has ground-truth claim paths absent from "
            f"its frozen co-change universe: {missing_required_paths}"
        )
    files = {
        path: [candidate.as_dict() for candidate in radius.radius_for(path).candidates]
        for path in radius.live_paths
    }
    payload = {
        "schema_version": 1,
        "top_k": radius.top_k,
        "threshold": radius.threshold,
        "threshold_inclusive": radius.threshold_inclusive,
        "files": files,
        "bundle_freeze": {
            "policy": "one cutoff per bundle, immediately before that bundle's oldest selected task",
            "cutoff_index": cutoff_index,
            "first_excluded_sha": cutoff_sha,
            "required_ground_truth_paths": required_paths,
            "missing_required_ground_truth_paths": [],
        },
        "provenance": radius.provenance,
    }
    path = artifact_root / bundle["bundle_id"] / "radius.json"
    atomic_json(path, payload)
    return {
        "path": path.relative_to(PROJECT_ROOT).as_posix(),
        "sha256": sha256(path.read_bytes()),
        "cutoff_index": cutoff_index,
        "first_excluded_sha": cutoff_sha,
        "ground_truth_path_coverage_verified": True,
        "provenance": radius.provenance,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", type=Path, default=PROJECT_ROOT / "instruments" / "posture" / "DESIGN.json")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "exploratory" / "posture" / "TASKS.json")
    parser.add_argument(
        "--preparation-root",
        type=Path,
        default=PROJECT_ROOT / "exploratory" / "posture" / "preparation",
        help="Fresh evidence-worktree root; a rejected preparation can be retained by selecting a new root.",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=PROJECT_ROOT / "exploratory" / "posture" / "task-artifacts",
        help="Fresh ground-truth and oracle artifact root.",
    )
    args = parser.parse_args()
    design = json.loads(args.design.read_text(encoding="utf-8"))
    compatibility_before = compatibility_probe(design)
    repository = Path(design["repository"]["clone"]).resolve(strict=True)
    header, commits = load_stream(design["repository"]["slug"])
    validate_first_parent_stream(commits)
    commits_by_sha = {commit["sha"]: commit for commit in commits}
    repository_gate = validate_repository_gate(design, repository)
    if header.get("source_head_sha") != repository_gate["head"]:
        raise ValueError("replay extraction stream and accepted repository gate HEAD disagree")
    validation = validate_design(design, repository, commits_by_sha)
    preparation_root = args.preparation_root.resolve()
    artifact_root = args.artifact_root.resolve()
    for root, label in (
        (preparation_root, "preparation root"),
        (artifact_root, "artifact root"),
    ):
        try:
            root.relative_to(PROJECT_ROOT)
        except ValueError as exc:
            raise ValueError(f"{label} must remain inside the project workspace: {root}") from exc
        if root.exists():
            raise FileExistsError(f"{label} must be fresh; refusing to reuse {root}")
    bundles = [
        build_bundle(design, bundle, commits_by_sha, preparation_root, artifact_root)
        for bundle in design["bundles"]
    ]
    bundle_freezes: dict[str, dict[str, Any]] = {}
    for bundle in bundles:
        oldest = min(bundle["tasks"], key=lambda task: task["stream_index"])
        cutoff_index = oldest["stream_index"]
        cutoff_sha = oldest["sha"]
        bundle["radius"] = build_radius(
            design,
            bundle,
            artifact_root,
            cutoff_index=cutoff_index,
            cutoff_sha=cutoff_sha,
        )
        bundle_freezes[bundle["bundle_id"]] = {
            "policy": "fold commits strictly before this bundle's oldest selected task",
            "cutoff_index": cutoff_index,
            "first_excluded_sha": cutoff_sha,
            "ground_truth_path_coverage_verified": True,
        }
    compatibility_after = compatibility_probe(design)
    if compatibility_after != compatibility_before:
        raise RuntimeError("Python compatibility layer changed during task construction")
    prepared_repository = dict(design["repository"])
    prepared_repository["python_compat_sitecustomize_sha256"] = compatibility_before[
        "sitecustomize_sha256"
    ]
    prepared_repository["python_compat_activation"] = compatibility_before
    output = {
        "schema_version": 1,
        "measurement": "posture-preregistered-task-construction",
        "prepared_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
        "design_path": args.design.resolve().relative_to(PROJECT_ROOT).as_posix(),
        "design_sha256": sha256(args.design.read_bytes()),
        "repository": prepared_repository,
        "repository_gate": repository_gate,
        "design_validation": {
            "arms": ["advisory", "blocking", "isolate"],
            "draws_per_cell": 5,
            "concurrent_agents_per_draw": design["pilot"]["concurrent_agents_per_draw"],
            "model_timeout_seconds": design["pilot"]["model_timeout_seconds"],
            "random_seed": design["pilot"]["random_seed"],
            "bundle_conditions": validation["conditions"],
            "tasks_per_bundle_range": [4, 6],
            "tasks_per_bundle_exact": design["pilot"]["concurrent_agents_per_draw"],
            "task_prompt_provenance_required": [
                "task_text",
                "task_source_url",
                "task_retrieved_at",
            ],
        },
        "source_stream_head": header["source_head_sha"],
        "radius_bundle_freezes": bundle_freezes,
        "pilot": design["pilot"],
        "interpretations": design["interpretations"],
        "bundles": bundles,
    }
    atomic_json(args.output.resolve(), output)
    print(json.dumps({"output": str(args.output), "bundles": [bundle["bundle_id"] for bundle in bundles]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
