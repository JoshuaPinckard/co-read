#!/usr/bin/env python3
"""Measure clean textual merges that fail a repository's test suite.

The harness is intentionally repository-agnostic, but this experiment invokes it
only for the two determinism-gated repositories named in SEMANTIC.md.  It never
fetches.  Every Git command runs with GIT_NO_LAZY_FETCH=1, and merge-tree is run
only against a disposable copy of the source clone.

Phases are separate so that the mandatory ten-merge pilot can finish before a
budget-derived cap is frozen:

    census      full anchored merge census and both-touched analysis
    replay      resumable parent/parent/mechanical-merge suite replay
    freeze-cap  derive a conservative cap from the first ten replay records
    summarize   aggregate replay outcomes and byte distances

All JSON writes are atomic.  Test attempts retain stdout, stderr, JUnit XML, an
exit-status file, a pytest-summary file, and metadata below the requested raw
artifact root.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import difflib
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_ANCHORS = {
    "pallets/click": "2c8cd3ac958a7eb316d67f2d316c27086c4c0369",
    "pygments/pygments": "38f426a6b1cd4ffc6429f5808031b7c62ea57b1f",
}
COLLECTION_PATTERNS = (
    re.compile(r"ERROR collecting", re.IGNORECASE),
    re.compile(r"errors? during collection", re.IGNORECASE),
    re.compile(r"Interrupted:\s*\d+ errors? during collection", re.IGNORECASE),
    re.compile(r"found no collectors", re.IGNORECASE),
)
IMPORT_PATTERNS = (
    re.compile(r"ModuleNotFoundError"),
    re.compile(r"ImportError"),
    re.compile(r"No module named ['\"]"),
)
SUMMARY_PATTERN = re.compile(
    r"(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)|no tests ran)",
    re.IGNORECASE,
)


class CommandFailure(RuntimeError):
    """A subprocess failed in a phase where failure is an instrument error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.partial")
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    last_error: PermissionError | None = None
    for _attempt in range(20):
        try:
            temporary.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(temporary, path)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(0.1)
    raise last_error or PermissionError(f"could not atomically write {path}")


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        shell=False,
        check=False,
    )


def run_suite_process(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **options,
        )
    except OSError as error:
        return {
            "exit_code": None,
            "stdout": b"",
            "stderr": str(error).encode("utf-8", errors="replace"),
            "timed_out": False,
            "timeout_detail": None,
            "termination": None,
            "launch_error": repr(error),
        }
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return {
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
            "timeout_detail": None,
            "termination": None,
            "launch_error": None,
        }
    except subprocess.TimeoutExpired as error:
        if os.name == "nt":
            termination_result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                check=False,
            )
            termination = {
                "command": ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                "exit_code": termination_result.returncode,
                "stdout": termination_result.stdout.decode("utf-8", errors="replace"),
                "stderr": termination_result.stderr.decode("utf-8", errors="replace"),
            }
        else:
            os.killpg(process.pid, signal.SIGKILL)
            termination = {"signal": "SIGKILL", "process_group": process.pid}
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                stdout, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired as final_error:
                stdout = final_error.stdout or b""
                stderr = final_error.stderr or b""
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                termination["final_communicate_timed_out"] = True
        return {
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "timeout_detail": str(error),
            "termination": termination,
            "launch_error": None,
        }


def git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment


def git_result(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return run_process(
        ["git", "-c", "core.longpaths=true", *arguments],
        cwd=repository,
        env=git_environment(),
        input_bytes=input_bytes,
        timeout=timeout,
    )


def git_bytes(repository: Path, *arguments: str, timeout: float | None = None) -> bytes:
    result = git_result(repository, *arguments, timeout=timeout)
    if result.returncode != 0:
        raise CommandFailure(
            f"git {' '.join(arguments)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace').strip()}"
        )
    return result.stdout


def git_text(repository: Path, *arguments: str, timeout: float | None = None) -> str:
    return git_bytes(repository, *arguments, timeout=timeout).decode(
        "utf-8", errors="replace"
    ).strip()


def decode_path(payload: bytes) -> str:
    return payload.decode("utf-8", errors="surrogateescape")


def nul_paths(payload: bytes) -> list[str]:
    return [decode_path(item) for item in payload.split(b"\0") if item]


def commit_field(repository: Path, commit: str, format_string: str) -> str:
    return git_text(repository, "show", "-s", f"--format={format_string}", commit)


def changed_paths(repository: Path, base: str, side: str) -> list[str]:
    # Exact path identity is deliberate.  --no-renames prevents machine/config
    # rename heuristics from changing the denominator.
    payload = git_bytes(
        repository,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        base,
        side,
        "--",
    )
    return sorted(set(nul_paths(payload)))


def merge_tree(
    repository: Path, parent1: str, parent2: str
) -> tuple[int, str | None, list[str], str]:
    result = git_result(
        repository,
        "merge-tree",
        "--write-tree",
        "--name-only",
        "-z",
        "--no-messages",
        parent1,
        parent2,
    )
    tokens = [item for item in result.stdout.split(b"\0") if item]
    tree = tokens[0].decode("ascii", errors="replace") if tokens else None
    paths = [decode_path(item) for item in tokens[1:]]
    stderr = result.stderr.decode("utf-8", errors="replace")
    return result.returncode, tree, paths, stderr


def repository_preflight(repository: Path, anchor: str) -> dict[str, Any]:
    anchor_commit = git_text(repository, "rev-parse", f"{anchor}^{{commit}}")
    anchor_tree = git_text(repository, "rev-parse", f"{anchor}^{{tree}}")
    shallow = git_text(repository, "rev-parse", "--is-shallow-repository")
    object_scan = git_result(
        repository, "rev-list", "--objects", "--missing=print", anchor_commit
    )
    if object_scan.returncode != 0:
        raise CommandFailure(
            "anchored object scan failed with lazy fetching disabled: "
            + object_scan.stderr.decode("utf-8", errors="replace")
        )
    missing = [
        line.decode("ascii", errors="replace")
        for line in object_scan.stdout.splitlines()
        if line.startswith(b"?")
    ]
    fsck = git_result(repository, "fsck", "--full", "--no-dangling", timeout=300)
    version = run_process(["git", "--version"], cwd=repository)
    git_directory = Path(git_text(repository, "rev-parse", "--git-dir"))
    if not git_directory.is_absolute():
        git_directory = (repository / git_directory).resolve()
    alternates_path = git_directory / "objects" / "info" / "alternates"
    alternates = (
        [line for line in alternates_path.read_text(encoding="utf-8").splitlines() if line]
        if alternates_path.is_file()
        else []
    )
    return {
        "repository_copy": str(repository),
        "anchor": anchor_commit,
        "anchor_tree": anchor_tree,
        "head_at_preflight": git_text(repository, "rev-parse", "HEAD"),
        "is_shallow_repository": shallow == "true",
        "origin_url": optional_git_config(repository, "remote.origin.url"),
        "partial_clone_filter": optional_git_config(
            repository, "remote.origin.partialclonefilter"
        ),
        "promisor": optional_git_config(repository, "remote.origin.promisor"),
        "alternates_file": str(alternates_path),
        "alternates": alternates,
        "git_version": version.stdout.decode("utf-8", errors="replace").strip(),
        "git_version_exit": version.returncode,
        "lazy_fetch_disabled": True,
        "missing_anchored_objects": missing,
        "missing_anchored_object_count": len(missing),
        "fsck_exit": fsck.returncode,
        "fsck_stdout": fsck.stdout.decode("utf-8", errors="replace"),
        "fsck_stderr": fsck.stderr.decode("utf-8", errors="replace"),
    }


def optional_git_config(repository: Path, key: str) -> str | None:
    result = git_result(repository, "config", "--get", key)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise CommandFailure(f"git config --get {key} failed: {result.returncode}")
    return result.stdout.decode("utf-8", errors="replace").strip()


def validate_experiment_copy(repository: Path, name: str, anchor: str) -> dict[str, Any]:
    expected_anchor = ALLOWED_ANCHORS.get(name)
    if expected_anchor is None:
        raise ValueError(f"repository is outside the frozen two-repository scope: {name}")
    resolved_anchor = git_text(repository, "rev-parse", f"{anchor}^{{commit}}")
    if resolved_anchor != expected_anchor:
        raise ValueError(
            f"anchor for {name} must be frozen gated commit {expected_anchor}, got {resolved_anchor}"
        )
    corpus_root = (PROJECT_ROOT / "corpus" / "_clones").resolve(strict=True)
    resolved_repository = repository.resolve(strict=True)
    if resolved_repository == corpus_root or corpus_root in resolved_repository.parents:
        raise ValueError("refusing to mutate or run merge-tree in corpus/_clones")
    marker = resolved_repository / ".semantic-merge-copy.json"
    if not marker.is_file():
        raise ValueError(f"scratch copy marker is absent: {marker}")
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    if marker_payload.get("repository") != name or marker_payload.get("anchor") != expected_anchor:
        raise ValueError("scratch copy marker does not match repository and anchor")
    return marker_payload


def claim_raw_root(root: Path, *, name: str, anchor: str, state_path: Path) -> Path:
    resolved = root.resolve()
    corpus_root = (PROJECT_ROOT / "corpus" / "_clones").resolve(strict=True)
    fixture_root = (PROJECT_ROOT / "fixture").resolve(strict=True)
    for forbidden in (corpus_root, fixture_root):
        if resolved == forbidden or forbidden in resolved.parents:
            raise ValueError(f"raw run root is inside protected source area: {resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    marker = resolved / ".semantic-run-root.json"
    expected = {
        "schema_version": SCHEMA_VERSION,
        "owner": "semantic_merge_replay.py",
        "repository": name,
        "anchor": anchor,
        "state_path": str(state_path.resolve()),
    }
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != expected:
            raise ValueError(f"raw root belongs to another run: {marker}")
    else:
        atomic_json(marker, expected)
    return resolved


def ancestry_records(repository: Path, anchor: str) -> tuple[int, list[dict[str, Any]]]:
    rows = git_text(repository, "rev-list", "--parents", anchor).splitlines()
    log_payload = git_bytes(
        repository,
        "log",
        "-z",
        "--no-decorate",
        "--format=%H%x00%ct%x00%cI%x00%T%x00%s",
        anchor,
    )
    tokens = log_payload.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    if len(tokens) % 5:
        raise CommandFailure("could not parse batched anchored commit metadata")
    metadata: dict[str, dict[str, Any]] = {}
    for index in range(0, len(tokens), 5):
        commit = tokens[index].decode("ascii")
        metadata[commit] = {
            "committer_epoch": int(tokens[index + 1].decode("ascii")),
            "committer_iso": tokens[index + 2].decode("utf-8", errors="replace"),
            "actual_tree": tokens[index + 3].decode("ascii"),
            "subject": tokens[index + 4].decode("utf-8", errors="replace"),
        }
    merges: list[dict[str, Any]] = []
    for row in rows:
        fields = row.split()
        if len(fields) < 3:
            continue
        commit, parents = fields[0], fields[1:]
        fields_for_commit = metadata.get(commit)
        if fields_for_commit is None:
            raise CommandFailure(f"batched metadata omitted reachable commit {commit}")
        merges.append(
            {
                "commit": commit,
                "parents": parents,
                "parent_count": len(parents),
                **fields_for_commit,
                "parent_subjects": [
                    metadata.get(parent, {}).get("subject", "") for parent in parents
                ],
            }
        )
    # "Newest" is explicitly committer time, with full OID as deterministic
    # tie-breaker.  This is frozen before tests and does not inspect outcomes.
    merges.sort(key=lambda item: (-item["committer_epoch"], item["commit"]))
    for ordinal, item in enumerate(merges, start=1):
        item["newest_first_ordinal"] = ordinal
    return len(rows), merges


def analyze_census_merge(repository: Path, item: dict[str, Any]) -> dict[str, Any]:
    record = dict(item)
    parents = record["parents"]
    if len(parents) > 2:
        record["classification"] = "octopus_skipped"
        return record

    parent1, parent2 = parents
    bases = git_text(repository, "merge-base", "--all", parent1, parent2).splitlines()
    base = git_text(repository, "merge-base", parent1, parent2)
    record["merge_base"] = base
    record["all_merge_bases"] = bases
    record["multiple_merge_bases"] = len(bases) > 1
    if base in (parent1, parent2):
        record["classification"] = "fast_forwardable_two_parent_skipped"
        record["fast_forwardable_parent"] = "parent1" if base == parent1 else "parent2"
        record["both_sides_touched_paths"] = []
        record["both_sides_touched"] = []
        return record

    paths1 = changed_paths(repository, base, parent1)
    paths2 = changed_paths(repository, base, parent2)
    both = sorted(set(paths1).intersection(paths2))
    record["changed_paths_parent1"] = paths1
    record["changed_paths_parent2"] = paths2
    record["both_sides_touched_paths"] = both
    exit_code, mechanical_tree, conflicted_paths, stderr = merge_tree(
        repository, parent1, parent2
    )
    record["merge_tree_exit"] = exit_code
    record["mechanical_tree"] = mechanical_tree
    record["conflicted_paths"] = sorted(set(conflicted_paths))
    record["merge_tree_stderr"] = stderr
    conflicted_set = set(conflicted_paths)
    if exit_code == 0:
        if not mechanical_tree:
            raise CommandFailure(f"clean merge-tree returned no tree for {record['commit']}")
        record["classification"] = "clean"
        record["developer_intervention_proxy"] = mechanical_tree != record["actual_tree"]
        record["both_sides_touched"] = [
            {"path": path, "textually_conflicted": path in conflicted_set}
            for path in both
        ]
    elif exit_code == 1:
        record["classification"] = "conflicted"
        record["developer_intervention_proxy"] = None
        record["both_sides_touched"] = [
            {"path": path, "textually_conflicted": path in conflicted_set}
            for path in both
        ]
    else:
        record["classification"] = "merge_tree_error"
        record["developer_intervention_proxy"] = None
        record["both_sides_touched"] = [
            {"path": path, "textually_conflicted": None} for path in both
        ]
    return record


def census_command(args: argparse.Namespace) -> int:
    repository = args.repo.resolve(strict=True)
    output = args.output.resolve()
    copy_marker = validate_experiment_copy(repository, args.name, args.anchor)
    preflight = repository_preflight(repository, args.anchor)
    if preflight["is_shallow_repository"]:
        raise CommandFailure("source copy is shallow")
    if preflight["missing_anchored_object_count"]:
        raise CommandFailure("source copy is missing anchored objects")
    if preflight["fsck_exit"] != 0:
        raise CommandFailure("git fsck failed on source copy")
    if preflight["alternates"]:
        raise CommandFailure("scratch copy depends on Git object alternates")

    reachable_count, merges = ancestry_records(repository, preflight["anchor"])
    records: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for record in executor.map(
            lambda candidate: analyze_census_merge(repository, candidate), merges
        ):
            records.append(record)
            if len(records) % 25 == 0 or len(records) == len(merges):
                partial = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "running",
                    "repository": args.name,
                    "preflight": preflight,
                    "reachable_commit_count": reachable_count,
                    "merges": records,
                }
                atomic_json(output.with_suffix(output.suffix + ".partial.json"), partial)

    classifications: dict[str, int] = {}
    both_units = 0
    conflicted_units = 0
    unknown_both_units = 0
    for record in records:
        key = record["classification"]
        classifications[key] = classifications.get(key, 0) + 1
        for touched in record.get("both_sides_touched", []):
            if touched["textually_conflicted"] is None:
                unknown_both_units += 1
            else:
                both_units += 1
                conflicted_units += int(touched["textually_conflicted"])
    clean_records = [item for item in records if item["classification"] == "clean"]
    intervention = sum(
        bool(item["developer_intervention_proxy"]) for item in clean_records
    )
    merge_stream = "\n".join(item["commit"] for item in records).encode("ascii")
    result = {
        "schema_version": SCHEMA_VERSION,
        "measurement": "silent-semantic-breakage-merge-census",
        "status": "complete",
        "repository": args.name,
        "preflight": preflight,
        "source_copy_provenance": args.source_copy_provenance,
        "source_copy_marker": copy_marker,
        "instrument": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_bytes(Path(__file__).resolve().read_bytes()),
            "schema_version": SCHEMA_VERSION,
        },
        "scope": {
            "history": "all commits reachable from frozen gated anchor",
            "ref_exclusions": "all refs other than the frozen anchor",
            "ordering": "committer epoch descending, full OID ascending tie-break",
            "rename_policy": "exact paths from git diff --no-renames",
            "fast_forward_policy": (
                "skip recorded two-parent commits whose merge base equals either parent; "
                "true fast-forward integration events leave no merge commit and are unobservable"
            ),
            "over_block_unit": "one exact (merge commit, path) pair",
        },
        "reachable_commit_count": reachable_count,
        "merge_stream_sha256": sha256_bytes(merge_stream),
        "counts": {
            "total_merge_commits": len(records),
            "two_parent_merge_commits": sum(item["parent_count"] == 2 for item in records),
            "octopus_merge_commits": sum(item["parent_count"] > 2 for item in records),
            **classifications,
            "divergent_two_parent": sum(
                item["classification"]
                in ("clean", "conflicted", "merge_tree_error")
                for item in records
            ),
            "both_sides_touched_units": both_units,
            "both_sides_touched_textually_conflicted_units": conflicted_units,
            "both_sides_touched_clean_units": both_units - conflicted_units,
            "both_sides_touched_unknown_units": unknown_both_units,
            "clean_developer_intervention_proxy": intervention,
            "clean_mechanical_matches_recorded": len(clean_records) - intervention,
        },
        "merges": records,
        "completed_at_utc": utc_now(),
    }
    atomic_json(output, result)
    partial_path = output.with_suffix(output.suffix + ".partial.json")
    if partial_path.exists():
        partial_path.unlink()
    print(json.dumps(result["counts"], indent=2, sort_keys=True))
    return 0


def tracked_state(worktree: Path) -> dict[str, str]:
    diff = git_bytes(worktree, "diff", "--binary", "--no-ext-diff")
    return {
        "head": git_text(worktree, "rev-parse", "HEAD"),
        "head_tree": git_text(worktree, "rev-parse", "HEAD^{tree}"),
        "index_tree": git_text(worktree, "write-tree"),
        "tracked_status": git_text(
            worktree, "status", "--porcelain", "--untracked-files=no"
        ),
        "tracked_diff_sha256": sha256_bytes(diff),
    }


def normalized_junit(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    cases: list[dict[str, str | None]] = []
    failing: list[dict[str, str | None]] = []
    for case in root.iter("testcase"):
        outcome = "passed"
        detail: str | None = None
        for tag in ("failure", "error", "skipped"):
            child = case.find(tag)
            if child is not None:
                outcome = tag
                detail = child.attrib.get("type") or child.attrib.get("message")
                break
        item = {
            "classname": case.attrib.get("classname", ""),
            "name": case.attrib.get("name", ""),
            "outcome": outcome,
            "detail": detail,
        }
        cases.append(item)
        if outcome in ("failure", "error"):
            failing.append(item)
    cases.sort(
        key=lambda item: (
            str(item["classname"]),
            str(item["name"]),
            str(item["outcome"]),
            str(item["detail"]),
        )
    )
    failing.sort(
        key=lambda item: (
            str(item["classname"]),
            str(item["name"]),
            str(item["outcome"]),
            str(item["detail"]),
        )
    )
    counts = {key: 0 for key in ("passed", "failure", "error", "skipped")}
    for case in cases:
        counts[str(case["outcome"])] += 1
    canonical = json.dumps(cases, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {
        "counts": counts,
        "case_count": len(cases),
        "cases_sha256": sha256_bytes(canonical.encode("ascii")),
        "failing_cases": failing,
    }


def pytest_summary(payload: str) -> str | None:
    candidates = [line.strip() for line in payload.splitlines() if SUMMARY_PATTERN.search(line)]
    return candidates[-1] if candidates else None


def classify_suite(
    *,
    exit_code: int | None,
    timed_out: bool,
    payload: str,
    junit: dict[str, Any] | None,
    tracked_unchanged: bool,
) -> str:
    if timed_out:
        return "timeout"
    if exit_code == 0:
        if junit is None:
            return "green_without_junit_infrastructure_error"
        if junit["counts"]["failure"] or junit["counts"]["error"]:
            return "zero_exit_with_red_junit_infrastructure_error"
        return "green"
    if any(pattern.search(payload) for pattern in COLLECTION_PATTERNS):
        if any(pattern.search(payload) for pattern in IMPORT_PATTERNS):
            return "collection_import_error"
        return "collection_error"
    if any(pattern.search(payload) for pattern in IMPORT_PATTERNS):
        return "import_error"
    if exit_code == 1:
        return "test_failure"
    if exit_code == 2:
        return "interrupted"
    if exit_code == 3 or "INTERNALERROR" in payload:
        return "pytest_internal_error"
    if exit_code == 4:
        return "pytest_usage_error"
    if exit_code == 5:
        return "no_tests_collected"
    return "process_error"


def safe_worktree_root(
    root: Path, repository: Path, *, repository_name: str, anchor: str
) -> Path:
    resolved = root.resolve()
    repository_resolved = repository.resolve()
    if (
        resolved == repository_resolved
        or resolved in repository_resolved.parents
        or repository_resolved in resolved.parents
    ):
        raise ValueError("worktree root and repository copy must not contain each other")
    resolved.mkdir(parents=True, exist_ok=True)
    marker = resolved / ".semantic-merge-worktrees.json"
    expected_payload = {
        "schema_version": SCHEMA_VERSION,
        "owner": "semantic_merge_replay.py",
        "repository": repository_name,
        "anchor": anchor,
        "repository_copy": str(repository_resolved),
    }
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != expected_payload:
            raise ValueError(f"worktree root belongs to another run: {marker}")
    else:
        atomic_json(marker, expected_payload)
    return resolved


def remove_worktree(repository: Path, worktree: Path, root: Path) -> dict[str, Any]:
    resolved = worktree.resolve()
    if resolved.parent != root.resolve():
        raise ValueError(f"refusing cleanup outside owned worktree root: {resolved}")
    git_pointer = resolved / ".git"
    repository_git_dir = Path(git_text(repository, "rev-parse", "--git-dir"))
    if not repository_git_dir.is_absolute():
        repository_git_dir = (repository / repository_git_dir).resolve()
    owned = False
    if git_pointer.is_file():
        pointer_text = git_pointer.read_text(encoding="utf-8", errors="replace")
        owned = str(repository_git_dir).casefold().replace("\\", "/") in pointer_text.casefold().replace("\\", "/")
    if resolved.exists() and not owned:
        raise ValueError(f"refusing to remove unowned worktree path: {resolved}")
    result = git_result(repository, "worktree", "remove", "--force", str(resolved))
    for _attempt in range(10):
        if not resolved.exists():
            break
        time.sleep(0.2)
        result = git_result(repository, "worktree", "remove", "--force", str(resolved))
    fallback = False
    if resolved.exists():
        fallback = True
        shutil.rmtree(resolved)
    git_result(repository, "worktree", "prune")
    return {
        "git_worktree_remove_exit": result.returncode,
        "git_worktree_remove_stderr": result.stderr.decode("utf-8", errors="replace"),
        "filesystem_fallback_used": fallback,
        "removed": not resolved.exists(),
    }


def add_fresh_worktree(
    repository: Path, root: Path, name: str, commit: str
) -> Path:
    worktree = root / name
    if worktree.exists():
        remove_worktree(repository, worktree, root)
    result = git_result(
        repository,
        "worktree",
        "add",
        "--detach",
        "--force",
        str(worktree),
        commit,
    )
    if result.returncode != 0:
        raise CommandFailure(
            f"could not create worktree {worktree}: "
            + result.stderr.decode("utf-8", errors="replace")
        )
    return worktree


def synthetic_mechanical_commit(
    repository: Path, merge_commit: str, mechanical_tree: str
) -> str:
    raw = git_bytes(repository, "cat-file", "commit", merge_commit)
    lines = raw.splitlines(keepends=True)
    if not lines or not lines[0].startswith(b"tree "):
        raise CommandFailure(f"malformed commit object: {merge_commit}")
    ending = b"\r\n" if lines[0].endswith(b"\r\n") else b"\n"
    lines[0] = b"tree " + mechanical_tree.encode("ascii") + ending
    replacement = b"".join(lines)
    result = git_result(
        repository,
        "hash-object",
        "-t",
        "commit",
        "-w",
        "--stdin",
        input_bytes=replacement,
    )
    if result.returncode != 0:
        raise CommandFailure(
            "could not write synthetic mechanical commit: "
            + result.stderr.decode("utf-8", errors="replace")
        )
    return result.stdout.decode("ascii").strip()


def suite_environment(worktree: Path, compat_root: Path | None) -> tuple[dict[str, str], str]:
    source_root = worktree / "src" if (worktree / "src").is_dir() else worktree
    python_paths = [source_root]
    if compat_root is not None:
        if (source_root / "sitecustomize.py").exists():
            raise CommandFailure("checkout shadows the frozen compatibility sitecustomize")
        python_paths.append(compat_root)
    environment = os.environ.copy()
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONHOME"):
        environment.pop(key, None)
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in python_paths)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONHASHSEED"] = "0"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment, str(source_root)


def recover_completed_attempt(
    *,
    repository: Path,
    worktree_root: Path,
    worktree: Path,
    attempt_root: Path,
    merge_commit: str,
    checkout_commit: str,
    role: str,
    attempt: int,
    protocol_sha256: str,
) -> dict[str, Any] | None:
    if not attempt_root.exists():
        return None
    result_path = attempt_root / "result.json"
    if result_path.is_file():
        try:
            record = json.loads(result_path.read_text(encoding="utf-8"))
            expected = {
                "merge_commit": merge_commit,
                "checkout_commit": checkout_commit,
                "role": role,
                "attempt": attempt,
                "protocol_sha256": protocol_sha256,
            }
            if any(record.get(key) != value for key, value in expected.items()):
                raise ValueError("attempt identity mismatch")
            expected_tree = git_text(repository, "rev-parse", f"{checkout_commit}^{{tree}}")
            if record.get("checkout_tree") != expected_tree:
                raise ValueError("attempt checkout tree mismatch")
            for artifact, digest_key in (("stdout", "stdout_sha256"), ("stderr", "stderr_sha256")):
                path = Path(record["artifacts"][artifact])
                if path.resolve().parent != attempt_root.resolve():
                    raise ValueError(f"attempt {artifact} path escapes attempt root")
                if not path.is_file() or sha256_bytes(path.read_bytes()) != record[digest_key]:
                    raise ValueError(f"attempt {artifact} integrity mismatch")
            for artifact in ("exit_status", "summary_line"):
                path = Path(record["artifacts"][artifact])
                if path.resolve().parent != attempt_root.resolve() or not path.is_file():
                    raise ValueError(f"attempt {artifact} is absent")
            junit_path_value = record["artifacts"].get("junit")
            if junit_path_value:
                junit_path = Path(junit_path_value)
                if junit_path.resolve().parent != attempt_root.resolve() or not junit_path.is_file() or sha256_bytes(junit_path.read_bytes()) != record.get(
                    "junit_sha256"
                ):
                    raise ValueError("attempt JUnit integrity mismatch")
            if worktree.exists():
                record["worktree_cleanup"] = remove_worktree(
                    repository, worktree, worktree_root
                )
                atomic_json(result_path, record)
            if not record.get("worktree_cleanup", {}).get("removed"):
                raise ValueError("completed attempt lacks a successful cleanup record")
            record["recovered_from_raw_checkpoint"] = True
            return record
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            # Preserve the invalid/incomplete evidence instead of overwriting it.
            pass
    quarantine = attempt_root.with_name(
        attempt_root.name + f".incomplete-{time.time_ns()}"
    )
    os.replace(attempt_root, quarantine)
    atomic_json(
        quarantine / "RECOVERY.json",
        {
            "status": "preserved_incomplete_attempt",
            "original_path": str(attempt_root),
            "quarantine_path": str(quarantine),
            "recovered_at_utc": utc_now(),
        },
    )
    return None


def run_suite_attempt(
    *,
    repository: Path,
    worktree_root: Path,
    raw_merge_root: Path,
    merge_commit: str,
    checkout_commit: str,
    role: str,
    attempt: int,
    python: Path,
    compat_root: Path | None,
    pytest_arguments: Sequence[str],
    timeout_seconds: float,
    protocol_sha256: str,
) -> dict[str, Any]:
    label = f"{merge_commit[:12]}-{role}-attempt-{attempt}"
    attempt_root = raw_merge_root / f"{role}-attempt-{attempt}"
    worktree = worktree_root / label
    recovered = recover_completed_attempt(
        repository=repository,
        worktree_root=worktree_root,
        worktree=worktree,
        attempt_root=attempt_root,
        merge_commit=merge_commit,
        checkout_commit=checkout_commit,
        role=role,
        attempt=attempt,
        protocol_sha256=protocol_sha256,
    )
    if recovered is not None:
        return recovered
    attempt_root.mkdir(parents=True)
    worktree = add_fresh_worktree(repository, worktree_root, label, checkout_commit)
    stdout_path = attempt_root / "stdout.txt"
    stderr_path = attempt_root / "stderr.txt"
    junit_path = attempt_root / "junit.xml"
    exit_path = attempt_root / "exit-status.txt"
    summary_path = attempt_root / "summary-line.txt"
    result_path = attempt_root / "result.json"
    try:
        before = tracked_state(worktree)
        environment, source_root = suite_environment(worktree, compat_root)
        command = [
            str(python),
            "-m",
            "pytest",
            *pytest_arguments,
            f"--junitxml={junit_path}",
        ]
        started_utc = utc_now()
        started = time.perf_counter()
        execution = run_suite_process(
            command,
            cwd=worktree,
            env=environment,
            timeout=timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        exit_code = execution["exit_code"]
        stdout = execution["stdout"]
        stderr = execution["stderr"]
        timed_out = bool(execution["timed_out"])
        stdout_path.write_bytes(stdout)
        stderr_path.write_bytes(stderr)
        if timed_out:
            raw_exit = "TIMEOUT\n"
        elif execution["launch_error"]:
            raw_exit = "PROCESS_LAUNCH_ERROR\n"
        else:
            raw_exit = f"{exit_code}\n"
        exit_path.write_text(raw_exit, encoding="ascii", newline="\n")
        payload_text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
        summary = pytest_summary(payload_text)
        summary_path.write_text(
            (summary or "NO PYTEST SUMMARY FOUND") + "\n", encoding="utf-8"
        )
        after = tracked_state(worktree)
        tracked_unchanged = before == after
        junit = normalized_junit(junit_path)
        classification = classify_suite(
            exit_code=exit_code,
            timed_out=timed_out,
            payload=payload_text,
            junit=junit,
            tracked_unchanged=tracked_unchanged,
        )
        normalized_failure_text = payload_text
        for value in (str(worktree), str(attempt_root), str(junit_path)):
            normalized_failure_text = normalized_failure_text.replace(value, "<PATH>")
            normalized_failure_text = normalized_failure_text.replace(
                value.replace("\\", "/"), "<PATH>"
            )
        normalized_failure_text = re.sub(
            r"\b\d+(?:\.\d+)?(?:ms|s| seconds?)\b", "<DURATION>", normalized_failure_text
        )
        normalized_failure_text = re.sub(
            r"(?i)(?:[A-Z]:)?[^\r\n\s]*[\\/]pytest-of-[^\\/\s]+[\\/]pytest-\d+",
            "<PYTEST_TEMP_ROOT>",
            normalized_failure_text,
        )
        normalized_failure_text = re.sub(
            r"\x1b\[[0-9;]*m", "", normalized_failure_text
        )
        failure_text_signature = (
            sha256_bytes(normalized_failure_text.encode("utf-8"))
            if normalized_failure_text.strip()
            else None
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "merge_commit": merge_commit,
            "role": role,
            "attempt": attempt,
            "protocol_sha256": protocol_sha256,
            "checkout_commit": checkout_commit,
            "checkout_tree": before["head_tree"],
            "command": command,
            "cwd": str(worktree),
            "environment": {
                key: environment[key]
                for key in (
                    "PYTHONPATH",
                    "PYTHONDONTWRITEBYTECODE",
                    "PYTHONNOUSERSITE",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
                    "PYTHONHASHSEED",
                    "GIT_NO_LAZY_FETCH",
                )
            },
            "source_root": source_root,
            "started_at_utc": started_utc,
            "completed_at_utc": utc_now(),
            "elapsed_seconds": elapsed,
            "timeout_seconds": timeout_seconds,
            "timed_out": timed_out,
            "timeout_detail": execution["timeout_detail"],
            "timeout_termination": execution["termination"],
            "process_launch_error": execution["launch_error"],
            "exit_code": exit_code,
            "pytest_summary_line": summary,
            "classification": classification,
            "before": before,
            "after": after,
            "tracked_state_unchanged": tracked_unchanged,
            "stdout_sha256": sha256_bytes(stdout),
            "stderr_sha256": sha256_bytes(stderr),
            "junit_sha256": sha256_bytes(junit_path.read_bytes()) if junit_path.exists() else None,
            "failure_text_signature_sha256": failure_text_signature,
            "junit": junit,
            "artifacts": {
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
                "junit": str(junit_path) if junit_path.exists() else None,
                "exit_status": str(exit_path),
                "summary_line": str(summary_path),
            },
        }
        atomic_json(result_path, record)
    finally:
        cleanup = remove_worktree(repository, worktree, worktree_root)
    if not cleanup["removed"]:
        raise CommandFailure(f"worktree cleanup failed: {worktree}")
    record["worktree_cleanup"] = cleanup
    atomic_json(result_path, record)
    return record


def compare_attempts(first: dict[str, Any], second: dict[str, Any]) -> str:
    first_junit = first.get("junit") or {}
    second_junit = second.get("junit") or {}
    first_text_signature = first.get("failure_text_signature_sha256")
    second_text_signature = second.get("failure_text_signature_sha256")
    first_has_cases = int(first_junit.get("case_count", 0)) > 0
    second_has_cases = int(second_junit.get("case_count", 0)) > 0
    if (
        first.get("exit_code") != second.get("exit_code")
        or first.get("timed_out") != second.get("timed_out")
        or first.get("classification") != second.get("classification")
    ):
        return "disagree"
    if first_has_cases and second_has_cases:
        return (
            "agree"
            if first_junit.get("cases_sha256") == second_junit.get("cases_sha256")
            else "disagree"
        )
    if first_has_cases != second_has_cases:
        return "disagree"
    if first_text_signature is None or second_text_signature is None:
        return "unverifiable"
    return "agree" if first_text_signature == second_text_signature else "disagree"


def test_runtime_snapshot(python: Path) -> dict[str, Any]:
    commands = {
        "python_version": [str(python), "--version"],
        "pytest_version": [str(python), "-m", "pytest", "--version"],
        "pip_freeze": [str(python), "-m", "pip", "freeze", "--all"],
    }
    snapshot: dict[str, Any] = {
        "python": str(python),
        "python_sha256": sha256_bytes(python.read_bytes()),
    }
    for key, command in commands.items():
        result = run_process(command, cwd=python.parent)
        snapshot[key] = {
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
        }
    return snapshot


def file_or_directory_fingerprint(path: Path | None) -> str | None:
    if path is None:
        return None
    if path.is_file():
        return sha256_bytes(path.read_bytes())
    entries: list[dict[str, str]] = []
    for candidate in sorted(item for item in path.rglob("*") if item.is_file()):
        entries.append(
            {
                "path": candidate.relative_to(path).as_posix(),
                "sha256": sha256_bytes(candidate.read_bytes()),
            }
        )
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("ascii"))


def replay_protocol(
    *,
    name: str,
    repository: Path,
    anchor: str,
    census_path: Path,
    census_sha: str,
    raw_repository_root: Path,
    worktree_root: Path,
    python: Path,
    compat_root: Path | None,
    pytest_arguments: Sequence[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    runtime = test_runtime_snapshot(python)
    if runtime["python_version"]["exit_code"] != 0:
        raise CommandFailure("test interpreter failed its version preflight")
    if runtime["pytest_version"]["exit_code"] != 0:
        raise CommandFailure("pytest is unavailable from the frozen test interpreter")
    environment_root = python.parent.parent
    environment_site_packages = environment_root / "Lib" / "site-packages"
    if not environment_site_packages.is_dir():
        raise CommandFailure("isolated test environment site-packages is absent")
    if not (environment_root / "pyvenv.cfg").is_file() or not (
        environment_root / "SEMANTIC-ENVIRONMENT.json"
    ).is_file():
        raise CommandFailure("test environment config or semantic manifest is absent")
    return {
        "instrument_path": str(Path(__file__).resolve()),
        "instrument_sha256": sha256_bytes(Path(__file__).resolve().read_bytes()),
        "instrument_schema_version": SCHEMA_VERSION,
        "repository": name,
        "repository_copy": str(repository),
        "anchor": anchor,
        "census_path": str(census_path),
        "census_sha256": census_sha,
        "raw_repository_root": str(raw_repository_root),
        "worktree_root": str(worktree_root),
        "python": str(python),
        "python_sha256": sha256_bytes(python.read_bytes()),
        "python_environment_root": str(environment_root),
        "python_site_packages": str(environment_site_packages),
        "python_site_packages_sha256": file_or_directory_fingerprint(
            environment_site_packages
        ),
        "python_venv_config_sha256": file_or_directory_fingerprint(
            environment_root / "pyvenv.cfg"
        ),
        "python_environment_manifest_sha256": file_or_directory_fingerprint(
            environment_root / "SEMANTIC-ENVIRONMENT.json"
        ),
        "compat_root": str(compat_root) if compat_root else None,
        "compat_sha256": file_or_directory_fingerprint(compat_root),
        "pytest_arguments": list(pytest_arguments),
        "timeout_seconds": timeout_seconds,
        "environment_policy": {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "PYTEST_ADDOPTS": "removed",
            "PYTEST_PLUGINS": "removed",
            "PYTHONHOME": "removed",
        },
        "runtime": runtime,
    }


def json_fingerprint(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("ascii"))


@contextlib.contextmanager
def exclusive_state_lock(state_path: Path) -> Iterable[None]:
    lock_path = state_path.with_name(state_path.name + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise CommandFailure(f"replay state is already locked: {lock_path}") from error
    try:
        os.write(
            descriptor,
            json.dumps({"pid": os.getpid(), "created_at_utc": utc_now()}).encode("ascii"),
        )
        os.close(descriptor)
        descriptor = -1
        yield
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def replay_one_merge(
    *,
    repository: Path,
    merge: dict[str, Any],
    worktree_root: Path,
    raw_repository_root: Path,
    python: Path,
    compat_root: Path | None,
    pytest_arguments: Sequence[str],
    timeout_seconds: float,
    protocol_sha256: str,
    existing_record: dict[str, Any] | None,
    progress_callback: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    checkpoint_clock = time.perf_counter()
    commit = merge["commit"]
    parent1, parent2 = merge["parents"]
    mechanical_tree = merge["mechanical_tree"]
    if mechanical_tree == merge["actual_tree"]:
        mechanical_commit = commit
        materialization = "recorded merge commit (tree exactly matches mechanical tree)"
    else:
        mechanical_commit = synthetic_mechanical_commit(
            repository, commit, mechanical_tree
        )
        materialization = "synthetic commit preserving recorded metadata and parents"
    raw_merge_root = raw_repository_root / "merges" / commit
    raw_merge_root.mkdir(parents=True, exist_ok=True)
    if existing_record is None:
        record: dict[str, Any] = {
            "merge_commit": commit,
            "newest_first_ordinal": merge["newest_first_ordinal"],
            "subject": merge["subject"],
            "parents": merge["parents"],
            "parent_subjects": merge["parent_subjects"],
            "merge_base": merge["merge_base"],
            "mechanical_tree": mechanical_tree,
            "actual_tree": merge["actual_tree"],
            "mechanical_commit": mechanical_commit,
            "materialization": materialization,
            "developer_intervention_proxy": merge["developer_intervention_proxy"],
            "both_sides_touched_paths": merge["both_sides_touched_paths"],
            "started_at_utc": utc_now(),
            "wall_seconds_so_far": 0.0,
            "runs": {"parent1": [], "parent2": [], "merged": []},
        }
    else:
        record = existing_record
        if record["merge_commit"] != commit:
            raise ValueError("in-progress checkpoint belongs to another merge")

    def checkpoint() -> None:
        nonlocal checkpoint_clock
        now = time.perf_counter()
        record["wall_seconds_so_far"] = float(record.get("wall_seconds_so_far", 0.0)) + (
            now - checkpoint_clock
        )
        checkpoint_clock = now
        record["updated_at_utc"] = utc_now()
        atomic_json(raw_merge_root / "merge-result.partial.json", record)
        progress_callback(record)

    checkpoint()

    if record["runs"]["parent1"]:
        parent1_run = record["runs"]["parent1"][0]
    else:
        parent1_run = run_suite_attempt(
            repository=repository,
            worktree_root=worktree_root,
            raw_merge_root=raw_merge_root,
            merge_commit=commit,
            checkout_commit=parent1,
            role="parent1",
            attempt=1,
            python=python,
            compat_root=compat_root,
            pytest_arguments=pytest_arguments,
            timeout_seconds=timeout_seconds,
            protocol_sha256=protocol_sha256,
        )
        record["runs"]["parent1"].append(parent1_run)
        checkpoint()
    if record["runs"]["parent2"]:
        parent2_run = record["runs"]["parent2"][0]
    else:
        parent2_run = run_suite_attempt(
            repository=repository,
            worktree_root=worktree_root,
            raw_merge_root=raw_merge_root,
            merge_commit=commit,
            checkout_commit=parent2,
            role="parent2",
            attempt=1,
            python=python,
            compat_root=compat_root,
            pytest_arguments=pytest_arguments,
            timeout_seconds=timeout_seconds,
            protocol_sha256=protocol_sha256,
        )
        record["runs"]["parent2"].append(parent2_run)
        checkpoint()
    parent_failures = [
        (role, run["classification"])
        for role, run in (("parent1", parent1_run), ("parent2", parent2_run))
        if run["classification"] != "green"
    ]
    if parent_failures:
        record["outcome"] = "excluded_parent_not_green"
        record["exclusion_reasons"] = [
            f"{role}:{classification}" for role, classification in parent_failures
        ]
    else:
        if record["runs"]["merged"]:
            merged_first = record["runs"]["merged"][0]
        else:
            merged_first = run_suite_attempt(
                repository=repository,
                worktree_root=worktree_root,
                raw_merge_root=raw_merge_root,
                merge_commit=commit,
                checkout_commit=mechanical_commit,
                role="merged",
                attempt=1,
                python=python,
                compat_root=compat_root,
                pytest_arguments=pytest_arguments,
                timeout_seconds=timeout_seconds,
                protocol_sha256=protocol_sha256,
            )
            record["runs"]["merged"].append(merged_first)
            checkpoint()
        if merged_first["classification"] == "green":
            record["outcome"] = "evaluated_green"
        else:
            if len(record["runs"]["merged"]) >= 2:
                merged_second = record["runs"]["merged"][1]
            else:
                merged_second = run_suite_attempt(
                    repository=repository,
                    worktree_root=worktree_root,
                    raw_merge_root=raw_merge_root,
                    merge_commit=commit,
                    checkout_commit=mechanical_commit,
                    role="merged",
                    attempt=2,
                    python=python,
                    compat_root=compat_root,
                    pytest_arguments=pytest_arguments,
                    timeout_seconds=timeout_seconds,
                    protocol_sha256=protocol_sha256,
                )
                record["runs"]["merged"].append(merged_second)
                checkpoint()
            comparison = compare_attempts(merged_first, merged_second)
            record["merged_attempt_comparison"] = comparison
            if comparison == "disagree":
                record["outcome"] = "flaky_excluded"
                record["exclusion_reasons"] = ["merged_attempts_disagreed"]
            elif comparison == "unverifiable":
                record["outcome"] = "excluded_merged_infrastructure"
                record["exclusion_reasons"] = [
                    "merged_attempts_had_no_comparable_failure_evidence"
                ]
            elif merged_first["exit_code"] is None or merged_first["classification"] in {
                "tracked_mutation",
                "green_without_junit_infrastructure_error",
                "zero_exit_with_red_junit_infrastructure_error",
            }:
                record["outcome"] = "excluded_merged_infrastructure"
                record["exclusion_reasons"] = [
                    f"stable_merged:{merged_first['classification']}"
                ]
            else:
                # Literal outcome rule: with two green parents, a stable red
                # mechanical merge is a breakage.  This includes collection,
                # import, usage/config, internal, and no-tests exits caused by
                # the combined tree.
                record["outcome"] = "silent_semantic_breakage"
    record["completed_at_utc"] = utc_now()
    checkpoint()
    record["wall_seconds"] = float(record["wall_seconds_so_far"])
    atomic_json(raw_merge_root / "merge-result.json", record)
    partial = raw_merge_root / "merge-result.partial.json"
    if partial.exists():
        partial.unlink()
    return record


def replay_command(args: argparse.Namespace) -> int:
    repository = args.repo.resolve(strict=True)
    validate_experiment_copy(repository, args.name, args.anchor)
    census_path = args.census.resolve(strict=True)
    state_path = args.state.resolve()
    raw_repository_root = claim_raw_root(
        args.raw_root.resolve(),
        name=args.name,
        anchor=args.anchor,
        state_path=state_path,
    )
    python = args.python.resolve(strict=True)
    compat_root = args.compat_root.resolve(strict=True) if args.compat_root else None
    worktree_root = safe_worktree_root(
        args.worktree_root.resolve(),
        repository,
        repository_name=args.name,
        anchor=args.anchor,
    )
    census = json.loads(census_path.read_text(encoding="utf-8"))
    if census.get("status") != "complete":
        raise ValueError("census is not complete")
    if census["preflight"]["anchor"] != git_text(repository, "rev-parse", args.anchor):
        raise ValueError("repository copy no longer resolves the frozen anchor")
    clean = [item for item in census["merges"] if item["classification"] == "clean"]
    if args.phase == "pilot":
        if args.limit != 10:
            raise ValueError("mandatory pilot phase requires exactly --limit 10")
        if args.cap is not None:
            raise ValueError("pilot phase must run before a cap file exists")
        cap_payload = None
    else:
        if args.cap is None:
            raise ValueError("capped phase requires --cap")
        cap_path = args.cap.resolve(strict=True)
        cap_payload = json.loads(cap_path.read_text(encoding="utf-8"))
        if cap_payload.get("status") != "frozen":
            raise ValueError("cap is not frozen")
        if cap_payload.get("census_sha256") != sha256_bytes(census_path.read_bytes()):
            raise ValueError("cap belongs to a different census")
        if Path(cap_payload["pilot_state_path"]).resolve() != state_path:
            raise ValueError("cap belongs to a different replay state")
        if args.limit != int(cap_payload["frozen_cap"]):
            raise ValueError("capped replay limit must equal the frozen cap")
    selected = clean[: args.limit]
    selected_ids = [item["commit"] for item in selected]
    census_sha = sha256_bytes(census_path.read_bytes())
    protocol = replay_protocol(
        name=args.name,
        repository=repository,
        anchor=args.anchor,
        census_path=census_path,
        census_sha=census_sha,
        raw_repository_root=raw_repository_root,
        worktree_root=worktree_root,
        python=python,
        compat_root=compat_root,
        pytest_arguments=args.pytest_argument,
        timeout_seconds=args.timeout,
    )
    protocol_sha = json_fingerprint(protocol)
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["census_sha256"] != census_sha:
            raise ValueError("replay state belongs to a different census")
        previous = state["selected_merge_commits"]
        if selected_ids[: len(previous)] != previous:
            raise ValueError("new replay limit is not a prefix extension of frozen selection")
        if state.get("protocol_sha256") != protocol_sha:
            raise ValueError("replay protocol/environment changed since prior phase")
        record_ids = [record["merge_commit"] for record in state["records"]]
        if record_ids != previous[: len(record_ids)]:
            raise ValueError("completed replay records are not the frozen selection prefix")
        if args.phase == "pilot" and (len(previous) != 10 or len(record_ids) > 10):
            raise ValueError("pilot state contains work beyond the mandatory ten merges")
        state["selected_merge_commits"] = selected_ids
        state["requested_limit"] = args.limit
        state["active_phase"] = args.phase
    else:
        if args.phase != "pilot":
            raise ValueError("a capped run cannot start without a completed pilot state")
        state = {
            "schema_version": SCHEMA_VERSION,
            "measurement": "silent-semantic-breakage-replay",
            "status": "running",
            "repository": args.name,
            "repository_copy": str(repository),
            "anchor": args.anchor,
            "census_path": str(census_path),
            "census_sha256": census_sha,
            "selection_rule": "first N mechanically clean divergent merges in frozen newest-first census",
            "requested_limit": args.limit,
            "selected_merge_commits": selected_ids,
            "pytest_arguments": list(args.pytest_argument),
            "timeout_seconds": args.timeout,
            "compat_root": str(compat_root) if compat_root else None,
            "protocol": protocol,
            "protocol_sha256": protocol_sha,
            "active_phase": args.phase,
            "records": [],
            "started_at_utc": utc_now(),
        }
    state["status"] = "running"
    state["updated_at_utc"] = utc_now()
    atomic_json(state_path, state)
    completed = {record["merge_commit"] for record in state["records"]}
    for merge in selected:
        if merge["commit"] in completed:
            continue
        in_progress = state.get("in_progress")
        if in_progress is not None and in_progress["merge_commit"] != merge["commit"]:
            raise ValueError("in-progress checkpoint is not the next selected merge")

        def checkpoint_progress(progress: dict[str, Any]) -> None:
            state["in_progress"] = progress
            state["status"] = "running"
            state["updated_at_utc"] = utc_now()
            atomic_json(state_path, state)

        record = replay_one_merge(
            repository=repository,
            merge=merge,
            worktree_root=worktree_root,
            raw_repository_root=raw_repository_root,
            python=python,
            compat_root=compat_root,
            pytest_arguments=args.pytest_argument,
            timeout_seconds=args.timeout,
            protocol_sha256=protocol_sha,
            existing_record=in_progress,
            progress_callback=checkpoint_progress,
        )
        state["records"].append(record)
        state.pop("in_progress", None)
        state["updated_at_utc"] = utc_now()
        atomic_json(state_path, state)
        print(
            json.dumps(
                {
                    "merge": record["merge_commit"],
                    "outcome": record["outcome"],
                    "wall_seconds": round(record["wall_seconds"], 3),
                    "completed": len(state["records"]),
                    "limit": args.limit,
                },
                sort_keys=True,
            ),
            flush=True,
        )
    state["status"] = (
        "complete"
        if all(commit in {item["merge_commit"] for item in state["records"]} for commit in selected_ids)
        else "running"
    )
    state["completed_at_utc"] = utc_now()
    state["completed_phase"] = args.phase
    atomic_json(state_path, state)
    return 0


def freeze_cap_command(args: argparse.Namespace) -> int:
    census_path = args.census.resolve(strict=True)
    state_path = args.state.resolve(strict=True)
    census = json.loads(census_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    output = args.output.resolve()
    if output.exists():
        raise ValueError(f"refusing to overwrite frozen cap: {output}")
    if state["census_sha256"] != sha256_bytes(census_path.read_bytes()):
        raise ValueError("pilot state and census do not match")
    if state.get("status") != "complete" or state.get("completed_phase") != "pilot":
        raise ValueError("pilot replay state is not complete")
    if state.get("in_progress") is not None:
        raise ValueError("pilot still has an in-progress merge")
    if len(state["selected_merge_commits"]) != args.pilot_size:
        raise ValueError("pilot selection contains work beyond or below the fixed pilot")
    records = state["records"]
    if len(records) != args.pilot_size:
        raise ValueError(
            f"pilot requires {args.pilot_size} completed merges, found {len(records)}"
        )
    if args.allocation_seconds * 2 > args.total_budget_seconds:
        raise ValueError("per-repository allocations would exceed the total budget")
    walls = [float(record["wall_seconds"]) for record in records]
    if any(value <= 0 for value in walls):
        raise ValueError("pilot wall times must be positive")
    usable = args.allocation_seconds * (1.0 - args.reserve_fraction)
    attempt_elapsed = [
        float(run["elapsed_seconds"])
        for record in records
        for runs in record["runs"].values()
        for run in runs
    ]
    if not attempt_elapsed:
        raise ValueError("pilot contains no suite attempts")
    overhead_per_attempt = []
    for record in records:
        runs = [run for role_runs in record["runs"].values() for run in role_runs]
        if runs:
            overhead_per_attempt.append(
                max(
                    0.0,
                    (float(record["wall_seconds"]) - sum(float(run["elapsed_seconds"]) for run in runs))
                    / len(runs),
                )
            )
    maximum_attempt = max(attempt_elapsed)
    maximum_overhead_per_attempt = max(overhead_per_attempt, default=0.0)
    # A green merge costs three attempts; a red merge costs four because its
    # merged tree must be rerun.  Four worst observed attempts makes the cap
    # independent of how many parents happened to be green in the pilot.
    conservative_cost = 4 * (maximum_attempt + maximum_overhead_per_attempt)
    clean_count = int(census["counts"].get("clean", 0))
    computed = int(usable // conservative_cost)
    cap = min(clean_count, max(args.pilot_size, computed))
    sorted_walls = sorted(walls)
    middle = len(sorted_walls) // 2
    median = (
        sorted_walls[middle]
        if len(sorted_walls) % 2
        else (sorted_walls[middle - 1] + sorted_walls[middle]) / 2
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "measurement": "silent-semantic-breakage-cap-freeze",
        "status": "frozen",
        "repository": census["repository"],
        "census_path": str(census_path),
        "census_sha256": state["census_sha256"],
        "pilot_state_path": str(state_path),
        "pilot_size": args.pilot_size,
        "pilot_merge_commits": [record["merge_commit"] for record in records],
        "pilot_wall_seconds": walls,
        "pilot_wall_seconds_mean": sum(walls) / len(walls),
        "pilot_wall_seconds_median": median,
        "pilot_wall_seconds_max": max(walls),
        "pilot_observed_merge_wall_seconds_max": max(walls),
        "pilot_suite_attempt_seconds_max": maximum_attempt,
        "pilot_overhead_per_attempt_seconds_max": maximum_overhead_per_attempt,
        "conservative_four_attempt_merge_seconds": conservative_cost,
        "total_budget_seconds": args.total_budget_seconds,
        "per_repository_allocation_seconds": args.allocation_seconds,
        "reserve_fraction": args.reserve_fraction,
        "usable_allocation_seconds": usable,
        "cap_formula": (
            "min(clean_count, max(pilot_size, floor(usable_allocation / "
            "(4 * (maximum_pilot_attempt_wall + maximum_pilot_overhead_per_attempt)))))"
        ),
        "outcome_blind": (
            "cap uses attempt timings, fixed four-attempt worst-case branching, and clean-census "
            "count; semantic outcomes are not inputs"
        ),
        "clean_qualifying_merges": clean_count,
        "computed_cap_before_clean_limit": max(args.pilot_size, computed),
        "frozen_cap": cap,
        "clean_merges_beyond_cap_not_evaluated": max(0, clean_count - cap),
        "window_bias": "most-recent clean merges under frozen committer-time ordering",
        "frozen_at_utc": utc_now(),
    }
    atomic_json(output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def blob_at_path(repository: Path, commit: str, path: str) -> bytes | None:
    specification = f"{commit}:{path}"
    exists = git_result(repository, "cat-file", "-e", specification)
    if exists.returncode != 0:
        return None
    return git_bytes(repository, "cat-file", "blob", specification)


def line_offsets(payload: bytes) -> tuple[list[bytes], list[int]]:
    lines = payload.splitlines(keepends=True)
    offsets = [0]
    total = 0
    for line in lines:
        total += len(line)
        offsets.append(total)
    return lines, offsets


def byte_edit_spans(base: bytes | None, side: bytes | None) -> dict[str, Any]:
    if base is None:
        if side is None or not side:
            return {"spans": [], "exact": True, "reason": "path_absent_or_empty"}
        return {
            "spans": [[0, 0]],
            "exact": True,
            "reason": "file_addition_at_base_anchor_zero",
        }
    if side is None:
        return {
            "spans": [[0, len(base)]],
            "exact": True,
            "reason": "file_deletion",
        }
    if base == side:
        return {"spans": [], "exact": True, "reason": "no_blob_byte_change"}

    base_lines, base_offsets = line_offsets(base)
    side_lines, _ = line_offsets(side)
    matcher = difflib.SequenceMatcher(None, base_lines, side_lines, autojunk=False)
    spans: list[list[int]] = []
    exact = True
    fallback_blocks = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        start = base_offsets[i1]
        end = base_offsets[i2]
        if tag == "insert":
            spans.append([start, start])
            continue
        if tag == "delete":
            spans.append([start, end])
            continue
        base_chunk = base[start:end]
        side_chunk = b"".join(side_lines[j1:j2])
        # Refine ordinary replace blocks to exact byte spans.  The bounded
        # fallback avoids pathological quadratic behavior on generated files.
        if (
            len(base_chunk) <= 100_000
            and len(side_chunk) <= 100_000
            and len(base_chunk) * len(side_chunk) <= 50_000_000
        ):
            byte_matcher = difflib.SequenceMatcher(
                None, base_chunk, side_chunk, autojunk=False
            )
            for byte_tag, bi1, bi2, _bj1, _bj2 in byte_matcher.get_opcodes():
                if byte_tag == "equal":
                    continue
                if byte_tag == "insert":
                    spans.append([start + bi1, start + bi1])
                else:
                    spans.append([start + bi1, start + bi2])
        else:
            spans.append([start, end])
            exact = False
            fallback_blocks += 1
    spans.sort(key=lambda item: (item[0], item[1]))
    return {
        "spans": spans,
        "exact": exact,
        "reason": "base-coordinate byte spans from line then byte SequenceMatcher",
        "fallback_replace_blocks": fallback_blocks,
    }


def span_gap(left: Sequence[int], right: Sequence[int]) -> int:
    left_start, left_end = int(left[0]), int(left[1])
    right_start, right_end = int(right[0]), int(right[1])
    if left_end < right_start:
        return right_start - left_end
    if right_end < left_start:
        return left_start - right_end
    return 0


def nearest_edit_distance(repository: Path, merge: dict[str, Any]) -> dict[str, Any]:
    shared = list(merge.get("both_sides_touched_paths", []))
    if not shared:
        return {
            "category": "cross_file_no_shared_textual_coordinate",
            "minimum_byte_gap": None,
            "interpretation": "no textual granularity confined to one file could overlap",
            "paths": [],
        }
    base = merge["merge_base"]
    parent1, parent2 = merge["parents"]
    path_records: list[dict[str, Any]] = []
    nearest: dict[str, Any] | None = None
    for path in shared:
        base_blob = blob_at_path(repository, base, path)
        side1 = blob_at_path(repository, parent1, path)
        side2 = blob_at_path(repository, parent2, path)
        edits1 = byte_edit_spans(base_blob, side1)
        edits2 = byte_edit_spans(base_blob, side2)
        pairs: list[dict[str, Any]] = []
        for span1 in edits1["spans"]:
            for span2 in edits2["spans"]:
                gap = span_gap(span1, span2)
                pairs.append({"gap": gap, "parent1_span": span1, "parent2_span": span2})
        path_nearest = min(pairs, key=lambda item: (item["gap"], item["parent1_span"], item["parent2_span"])) if pairs else None
        path_record = {
            "path": path,
            "base_blob_bytes": len(base_blob) if base_blob is not None else None,
            "parent1_edits": edits1,
            "parent2_edits": edits2,
            "nearest": path_nearest,
        }
        path_records.append(path_record)
        if path_nearest is not None:
            candidate = {"path": path, **path_nearest}
            if nearest is None or (
                candidate["gap"], candidate["path"], candidate["parent1_span"], candidate["parent2_span"]
            ) < (
                nearest["gap"], nearest["path"], nearest["parent1_span"], nearest["parent2_span"]
            ):
                nearest = candidate
    if nearest is None:
        return {
            "category": "shared_path_without_blob_byte_edit",
            "minimum_byte_gap": None,
            "interpretation": "shared exact path changed only in metadata or an unsupported blob state",
            "paths": path_records,
        }
    exact = all(
        bool(path[side]["exact"])
        for path in path_records
        for side in ("parent1_edits", "parent2_edits")
    )
    return {
        "category": "shared_path_base_byte_coordinate",
        "minimum_byte_gap": nearest["gap"],
        "nearest": nearest,
        "exact": exact,
        "interpretation": (
            "minimum gap between any concurrent edit spans in the merge; this is not proof "
            "that the nearest pair caused the failing test"
        ),
        "paths": path_records,
    }


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> list[float] | None:
    if total == 0:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * ((proportion * (1 - proportion) / total + z * z / (4 * total * total)) ** 0.5)
        / denominator
    )
    return [max(0.0, center - margin), min(1.0, center + margin)]


def summarize_command(args: argparse.Namespace) -> int:
    repository = args.repo.resolve(strict=True)
    census_path = args.census.resolve(strict=True)
    state_path = args.state.resolve(strict=True)
    cap_path = args.cap.resolve(strict=True)
    census = json.loads(census_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    cap = json.loads(cap_path.read_text(encoding="utf-8"))
    census_sha = sha256_bytes(census_path.read_bytes())
    if state["census_sha256"] != census_sha:
        raise ValueError("replay state and census do not match")
    if cap.get("status") != "frozen" or cap.get("census_sha256") != census_sha:
        raise ValueError("cap is not frozen for this census")
    if Path(cap["pilot_state_path"]).resolve() != state_path:
        raise ValueError("cap and replay state do not match")
    if cap.get("repository") != census.get("repository") or state.get(
        "repository"
    ) != census.get("repository"):
        raise ValueError("repository identity differs across census, cap, and state")
    if state.get("status") != "complete" or state.get("completed_phase") != "capped":
        raise ValueError("capped replay is not complete")
    if state.get("in_progress") is not None:
        raise ValueError("replay has an in-progress merge")
    frozen_cap = int(cap["frozen_cap"])
    records = state["records"]
    if len(records) != frozen_cap:
        raise ValueError("completed replay record count does not equal frozen cap")
    clean_ids = [
        item["commit"] for item in census["merges"] if item["classification"] == "clean"
    ][:frozen_cap]
    record_ids = [record["merge_commit"] for record in records]
    if record_ids != clean_ids or state["selected_merge_commits"] != clean_ids:
        raise ValueError("replay records are not the frozen newest-first clean prefix")
    outcomes: dict[str, int] = {}
    exclusions: list[dict[str, Any]] = []
    breakages: list[dict[str, Any]] = []
    for record in records:
        outcome = record["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if outcome.startswith("excluded_") or outcome == "flaky_excluded":
            exclusions.append(
                {
                    "merge_commit": record["merge_commit"],
                    "subject": record["subject"],
                    "outcome": outcome,
                    "reasons": record.get("exclusion_reasons", []),
                    "parent_summaries": {
                        role: runs[0].get("pytest_summary_line") if runs else None
                        for role, runs in record["runs"].items()
                        if role in ("parent1", "parent2")
                    },
                    "attempts": {
                        role: [
                            {
                                "attempt": run["attempt"],
                                "exit_code": run["exit_code"],
                                "classification": run["classification"],
                                "summary": run.get("pytest_summary_line"),
                            }
                            for run in runs
                        ]
                        for role, runs in record["runs"].items()
                    },
                }
            )
        if outcome == "silent_semantic_breakage":
            merged_run = record["runs"]["merged"][0]
            breakages.append(
                {
                    "merge_commit": record["merge_commit"],
                    "subject": record["subject"],
                    "parents": record["parents"],
                    "parent_subjects": record["parent_subjects"],
                    "merged_classification": merged_run["classification"],
                    "merged_summary_lines": [
                        run.get("pytest_summary_line") for run in record["runs"]["merged"]
                    ],
                    "failing_cases": (merged_run.get("junit") or {}).get("failing_cases", []),
                    "developer_intervention_proxy": record["developer_intervention_proxy"],
                    "byte_distance": nearest_edit_distance(repository, record),
                }
            )
    evaluated = outcomes.get("evaluated_green", 0) + outcomes.get(
        "silent_semantic_breakage", 0
    )
    broken = outcomes.get("silent_semantic_breakage", 0)
    both = int(census["counts"]["both_sides_touched_units"])
    over_block = int(census["counts"]["both_sides_touched_clean_units"])
    result = {
        "schema_version": SCHEMA_VERSION,
        "measurement": "silent-semantic-breakage-summary",
        "repository": census["repository"],
        "rates": {
            "file_granularity_over_block": {
                "numerator": over_block,
                "denominator": both,
                "rate": over_block / both if both else None,
            },
            "silent_semantic_breakage": {
                "numerator": broken,
                "denominator": evaluated,
                "rate": broken / evaluated if evaluated else None,
            },
        },
        "census_counts": census["counts"],
        "replay": {
            "frozen_cap": cap["frozen_cap"],
            "records_completed": len(records),
            "clean_qualifying_merges": cap["clean_qualifying_merges"],
            "clean_merges_beyond_cap_not_evaluated": cap[
                "clean_merges_beyond_cap_not_evaluated"
            ],
            "outcomes": outcomes,
            "green_green_parents": sum(
                outcome != "excluded_parent_not_green" for outcome in (r["outcome"] for r in records)
            ),
            "evaluated_denominator": evaluated,
            "flaky_count": outcomes.get("flaky_excluded", 0),
            "parent_exclusion_count": outcomes.get("excluded_parent_not_green", 0),
            "merged_infrastructure_exclusion_count": outcomes.get(
                "excluded_merged_infrastructure", 0
            ),
            "exclusion_count": len(exclusions),
            "exclusion_rate": len(exclusions) / len(records) if records else None,
            "total_wall_seconds": sum(float(record["wall_seconds"]) for record in records),
        },
        "developer_intervention": {
            "numerator": census["counts"]["clean_developer_intervention_proxy"],
            "denominator": census["counts"]["clean"],
            "interpretation": (
                "tree inequality is evidence of intervention but can also reflect historical "
                "Git strategy/config differences"
            ),
        },
        "exclusions": exclusions,
        "silent_breakages": breakages,
        "generated_at_utc": utc_now(),
    }
    atomic_json(args.output.resolve(), result)
    print(json.dumps(result["rates"], indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    census = subparsers.add_parser("census", help="build full anchored merge census")
    census.add_argument("--repo", required=True, type=Path)
    census.add_argument("--anchor", required=True)
    census.add_argument("--name", required=True)
    census.add_argument("--output", required=True, type=Path)
    census.add_argument("--source-copy-provenance", required=True)
    census.add_argument("--jobs", type=int, default=8)
    census.set_defaults(function=census_command)

    replay = subparsers.add_parser("replay", help="run or resume semantic test replays")
    replay.add_argument("--repo", required=True, type=Path)
    replay.add_argument("--anchor", required=True)
    replay.add_argument("--name", required=True)
    replay.add_argument("--census", required=True, type=Path)
    replay.add_argument("--state", required=True, type=Path)
    replay.add_argument("--raw-root", required=True, type=Path)
    replay.add_argument("--worktree-root", required=True, type=Path)
    replay.add_argument("--python", required=True, type=Path)
    replay.add_argument("--compat-root", type=Path)
    replay.add_argument("--pytest-argument", action="append", default=[])
    replay.add_argument("--timeout", type=float, default=120.0)
    replay.add_argument("--limit", type=int, required=True)
    replay.add_argument("--phase", choices=("pilot", "capped"), required=True)
    replay.add_argument("--cap", type=Path)
    replay.set_defaults(function=replay_command)

    cap = subparsers.add_parser("freeze-cap", help="freeze cap from pilot wall times")
    cap.add_argument("--census", required=True, type=Path)
    cap.add_argument("--state", required=True, type=Path)
    cap.add_argument("--output", required=True, type=Path)
    cap.add_argument("--pilot-size", type=int, default=10)
    cap.add_argument("--total-budget-seconds", type=float, default=14_400.0)
    cap.add_argument("--allocation-seconds", type=float, default=7_200.0)
    cap.add_argument("--reserve-fraction", type=float, default=0.10)
    cap.set_defaults(function=freeze_cap_command)

    summary = subparsers.add_parser("summarize", help="aggregate rates and details")
    summary.add_argument("--repo", required=True, type=Path)
    summary.add_argument("--census", required=True, type=Path)
    summary.add_argument("--state", required=True, type=Path)
    summary.add_argument("--cap", required=True, type=Path)
    summary.add_argument("--output", required=True, type=Path)
    summary.set_defaults(function=summarize_command)
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    if arguments.command == "replay":
        with exclusive_state_lock(arguments.state.resolve()):
            return int(arguments.function(arguments))
    return int(arguments.function(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
