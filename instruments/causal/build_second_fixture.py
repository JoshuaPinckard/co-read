"""Build and verify the outcome-frozen second causal fixture.

The selection protocol is frozen in
``exploratory/causal/PLUGGY-SELECTION-RULE.md``.
Run ``inventory`` before ``verify`` so the complete ordered candidate ledger
and its digest exist before any historical pytest outcome is observed.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULE_PATH = PROJECT_ROOT / "exploratory" / "causal" / "PLUGGY-SELECTION-RULE.md"
DEFAULT_REPO = PROJECT_ROOT / "exploratory" / "causal" / "repositories" / "pytest-dev-pluggy"
DEFAULT_LEDGER = PROJECT_ROOT / "exploratory" / "causal" / "inventory" / "pluggy-candidates.json"
DEFAULT_RESULTS = PROJECT_ROOT / "exploratory" / "causal" / "verification" / "pluggy-results.json"
DEFAULT_FIXTURE = PROJECT_ROOT / "fixture" / "pluggy"
HEAD = "e382e72789f8d791991c489d4322aa04e660b952"
LOWER_EXCLUSIVE = "a878c473a66c2574615d943d78e3af67fe995169"
HISTORY_HEAD = "5c16e15a963d5e66f37d05b1ccfb90adf71e8e0f"
SQUASH_PR_RE = re.compile(r"\(#([1-9][0-9]*)\)$")
MERGE_PR_RE = re.compile(r"^Merge pull request #([1-9][0-9]*)\b")
REGULAR_MODES = {"100644", "100755"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        input=stdin,
        env=env,
        timeout=timeout,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )


def git(
    repository: Path,
    *arguments: str,
    stdin: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return run(
        ["git", "-c", "core.longpaths=true", *arguments],
        cwd=repository,
        stdin=stdin,
        check=check,
    )


def decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def write_digest(path: Path) -> str:
    digest = sha256_file(path)
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="ascii", newline="\n"
    )
    return digest


def parse_name_status(raw: bytes) -> list[dict[str, str]]:
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        status = decode(fields[index])
        index += 1
        kind = status[:1]
        if kind in {"R", "C"}:
            if index + 1 >= len(fields):
                raise ValueError("truncated rename/copy name-status record")
            old_path = decode(fields[index])
            new_path = decode(fields[index + 1])
            index += 2
            changes.append(
                {"status": status, "old_path": old_path, "path": new_path}
            )
        else:
            if index >= len(fields):
                raise ValueError("truncated name-status record")
            changes.append({"status": status, "path": decode(fields[index])})
            index += 1
    return changes


def ls_tree(repository: Path, revision: str) -> dict[str, dict[str, str]]:
    raw = git(repository, "ls-tree", "-r", "-z", revision).stdout
    result: dict[str, dict[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path_raw = record.split(b"\t", 1)
        mode, object_type, oid = decode(metadata).split(" ", 2)
        path = decode(path_raw)
        result[path] = {"mode": mode, "type": object_type, "oid": oid}
    return result


def full_patch(repository: Path, parent: str, commit: str) -> bytes:
    return git(
        repository,
        "diff",
        "--binary",
        "--full-index",
        "--find-renames=50%",
        "-l0",
        parent,
        commit,
        "--",
    ).stdout


def test_patch(repository: Path, parent: str, commit: str) -> bytes:
    return git(
        repository,
        "diff",
        "--binary",
        "--full-index",
        "--find-renames=50%",
        "-l0",
        parent,
        commit,
        "--",
        "testing/",
    ).stdout


def inventory(repository: Path, output: Path) -> dict[str, Any]:
    actual_head = decode(git(repository, "rev-parse", "HEAD").stdout).strip()
    if actual_head != HEAD:
        raise RuntimeError(f"repository HEAD {actual_head} does not match frozen {HEAD}")

    log = decode(
        git(
            repository,
            "log",
            "--first-parent",
            "--diff-merges=first-parent",
            "--find-renames=50%",
            "-l0",
            "--format=@@@%H%x09%P%x09%ct%x09%cI%x09%s",
            "--name-status",
            f"{LOWER_EXCLUSIVE}..{HISTORY_HEAD}",
        ).stdout
    )
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in log.splitlines():
        if not line:
            continue
        if line.startswith("@@@"):
            if current is not None:
                records.append(current)
            sha, parents, timestamp, committed_at, subject = line[3:].split("\t", 4)
            current = {
                "sha": sha,
                "parents": parents.split() if parents else [],
                "timestamp": int(timestamp),
                "committed_at": committed_at,
                "subject": subject,
                "changes": [],
            }
            continue
        if current is None or "\t" not in line:
            continue
        fields = line.split("\t")
        status = fields[0]
        if status[:1] in {"R", "C"}:
            current["changes"].append(
                {"status": status, "old_path": fields[-2], "path": fields[-1]}
            )
        else:
            current["changes"].append({"status": status, "path": fields[-1]})
    if current is not None:
        records.append(current)

    rejection_counts: collections.Counter[str] = collections.Counter()
    eligible: list[dict[str, Any]] = []
    seen_prs: set[int] = set()
    pr_landings = 0

    for position, record in enumerate(records, 1):
        parents = record["parents"]
        suffix_match = SQUASH_PR_RE.search(record["subject"])
        merge_match = MERGE_PR_RE.match(record["subject"])
        match = suffix_match or merge_match
        if (
            len(parents) not in {1, 2}
            or match is None
            or record["subject"].startswith("Revert ")
        ):
            rejection_counts["not_unambiguous_pr_landing"] += 1
            continue
        pr_pattern = (
            f"{len(parents)}-parent terminal '(#N)' subject"
            if suffix_match is not None
            else f"{len(parents)}-parent 'Merge pull request #N' subject"
        )
        pr_landings += 1
        if pr_landings % 50 == 0:
            print(
                f"static inventory: {pr_landings} PR landings inspected, "
                f"{len(eligible)} eligible so far",
                flush=True,
            )
        parent = parents[0]
        pr = int(match.group(1))
        changes = record["changes"]
        if not (2 <= len(changes) <= 40):
            rejection_counts["path_count_outside_2_40"] += 1
            continue
        if any(change["status"][:1] not in {"A", "M", "D", "R"} for change in changes):
            rejection_counts["status_outside_A_M_D_R"] += 1
            continue
        source_paths = [
            change["path"]
            for change in changes
            if change["status"][:1] in {"A", "M"}
            and change["path"].startswith("src/pluggy/")
            and change["path"].endswith(".py")
        ]
        python_test_paths = [
            change["path"]
            for change in changes
            if change["status"][:1] in {"A", "M"}
            and change["path"].startswith("testing/")
            and change["path"].endswith(".py")
        ]
        if not source_paths or not python_test_paths:
            rejection_counts["missing_source_or_python_test"] += 1
            continue
        commit_tree = ls_tree(repository, record["sha"])
        paths = [change["path"] for change in changes]
        if any(
            change["path"] not in commit_tree
            or commit_tree[change["path"]]["type"] != "blob"
            or commit_tree[change["path"]]["mode"] not in REGULAR_MODES
            for change in changes
            if change["status"][:1] != "D"
        ):
            rejection_counts["changed_path_not_regular"] += 1
            continue
        changed_test_paths = [path for path in paths if path.startswith("testing/")]
        if len(changed_test_paths) > 12:
            rejection_counts["more_than_twelve_test_paths"] += 1
            continue
        patch = full_patch(repository, parent, record["sha"])
        if len(patch) > 200 * 1024:
            rejection_counts["patch_over_200_KiB"] += 1
            continue
        if pr in seen_prs:
            rejection_counts["duplicate_pr_number"] += 1
            continue
        parent_tree = ls_tree(repository, parent)
        if any(
            item["type"] != "blob" or item["mode"] not in REGULAR_MODES
            for item in parent_tree.values()
        ):
            rejection_counts["base_contains_nonregular_entry"] += 1
            continue
        overlay = test_patch(repository, parent, record["sha"])
        if not overlay:
            raise AssertionError("eligible candidate unexpectedly has empty test patch")
        seen_prs.add(pr)
        eligible.append(
            {
                "order": len(eligible) + 1,
                "history_position": position,
                "pr": pr,
                "task_id": f"pr-{pr}",
                "sha": record["sha"],
                "commit_tree": decode(
                    git(repository, "rev-parse", f"{record['sha']}^{{tree}}").stdout
                ).strip(),
                "parent": parent,
                "parent_tree": decode(
                    git(repository, "rev-parse", f"{parent}^{{tree}}").stdout
                ).strip(),
                "timestamp": record["timestamp"],
                "committed_at": record["committed_at"],
                "subject": record["subject"],
                "pr_url": f"https://github.com/pytest-dev/pluggy/pull/{pr}",
                "pr_identity_provenance": f"{pr_pattern}; remote PR page not queried",
                "paths": paths,
                "source_python_paths": source_paths,
                "test_paths": changed_test_paths,
                "python_test_paths": python_test_paths,
                "full_patch_bytes": len(patch),
                "full_patch_sha256": sha256_bytes(patch),
                "test_patch_bytes": len(overlay),
                "test_patch_sha256": sha256_bytes(overlay),
            }
        )

    rule_sha = sha256_file(RULE_PATH)
    value = {
        "schema_version": 1,
        "measurement": "second-repository-static-candidate-ledger",
        "created_at_utc": utc_now(),
        "selection_rule": str(RULE_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "selection_rule_sha256": rule_sha,
        "repository": str(repository.resolve()),
        "repository_url": "https://github.com/pytest-dev/pluggy.git",
        "head": HEAD,
        "head_tree": decode(git(repository, "rev-parse", f"{HEAD}^{{tree}}").stdout).strip(),
        "history_lower_exclusive": LOWER_EXCLUSIVE,
        "history_head": HISTORY_HEAD,
        "order": "first-parent newest-to-oldest",
        "dynamic_budget": len(eligible),
        "target_tasks": 30,
        "counts": {
            "first_parent_commits_scanned_in_window": len(records),
            "unambiguous_pr_landings": pr_landings,
            "structurally_eligible": len(eligible),
            "static_rejections_by_first_reason": dict(sorted(rejection_counts.items())),
        },
        "candidates": eligible,
    }
    atomic_json(output, value)
    digest = write_digest(output)
    print(
        json.dumps(
            {
                "ledger": str(output),
                "ledger_sha256": digest,
                "counts": value["counts"],
            },
            indent=2,
        )
    )
    return value


def safe_extract_archive(repository: Path, revision: str, destination: Path) -> None:
    # A causal base must be the exact Git tree, independent of export-ignore,
    # export-subst, or platform newline conversion. Read raw blobs directly.
    tree = ls_tree(repository, revision)
    ordered = sorted(tree.items())
    queries = b"".join(f"{item['oid']}\n".encode("ascii") for _, item in ordered)
    batch = git(repository, "cat-file", "--batch", stdin=queries).stdout
    destination.mkdir(parents=True, exist_ok=False)
    cursor = 0
    for relative, item in ordered:
        if item["type"] != "blob" or item["mode"] not in REGULAR_MODES:
            raise RuntimeError(f"non-regular tree entry: {relative} {item}")
        pure = Path(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise RuntimeError(f"unsafe tree path: {relative!r}")
        line_end = batch.find(b"\n", cursor)
        if line_end < 0:
            raise RuntimeError("truncated cat-file batch header")
        header = batch[cursor:line_end].decode("ascii").split()
        if len(header) != 3:
            raise RuntimeError(f"invalid cat-file header: {header!r}")
        oid, object_type, size_text = header
        size = int(size_text)
        cursor = line_end + 1
        payload = batch[cursor : cursor + size]
        cursor += size
        if cursor >= len(batch) or batch[cursor : cursor + 1] != b"\n":
            raise RuntimeError("truncated cat-file batch payload")
        cursor += 1
        if oid != item["oid"] or object_type != "blob" or len(payload) != size:
            raise RuntimeError(f"cat-file identity mismatch for {relative}")
        path = destination / pure
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        if item["mode"] == "100755":
            path.chmod(0o755)
    if cursor != len(batch):
        raise RuntimeError("unexpected trailing cat-file batch data")


def blob_oid(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def compare_directory_to_tree(
    directory: Path, expected: dict[str, dict[str, str]]
) -> tuple[bool, dict[str, Any]]:
    actual_paths = {
        path.relative_to(directory).as_posix(): path
        for path in directory.rglob("*")
        if path.is_file()
    }
    expected_paths = set(expected)
    missing = sorted(expected_paths - set(actual_paths))
    extra = sorted(set(actual_paths) - expected_paths)
    mismatched: list[str] = []
    for path in sorted(expected_paths & set(actual_paths)):
        if expected[path]["type"] != "blob":
            mismatched.append(path)
            continue
        if blob_oid(actual_paths[path].read_bytes()) != expected[path]["oid"]:
            mismatched.append(path)
    details = {
        "expected_paths": len(expected_paths),
        "actual_paths": len(actual_paths),
        "missing": missing,
        "extra": extra,
        "blob_mismatches": mismatched,
        "expected_modes_sha256": sha256_bytes(
            "".join(
                f"{path}\0{expected[path]['mode']}\0" for path in sorted(expected)
            ).encode("utf-8")
        ),
    }
    return not missing and not extra and not mismatched, details


def tracked_content_state(directory: Path) -> dict[str, Any]:
    files: list[tuple[str, str]] = []
    for path in sorted((item for item in directory.rglob("*") if item.is_file())):
        relative = path.relative_to(directory).as_posix()
        files.append((relative, sha256_file(path)))
    canonical = json.dumps(files, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return {
        "file_count": len(files),
        "sha256": sha256_bytes(canonical),
        "files": files,
    }


def tracked_state_matches(directory: Path, before: dict[str, Any]) -> bool:
    for relative, expected_sha in before["files"]:
        path = directory / relative
        if not path.is_file() or sha256_file(path) != expected_sha:
            return False
    return True


def apply_patch(directory: Path, patch: bytes, *, reverse: bool = False) -> dict[str, Any]:
    arguments = [
        "git",
        "-c",
        "core.longpaths=true",
        "-c",
        "core.autocrlf=false",
        "apply",
    ]
    if reverse:
        arguments.append("--reverse")
    arguments.extend(["--check", "--binary", "-"])
    checked = run(arguments, cwd=directory, stdin=patch)
    if checked.returncode != 0:
        return {
            "applied": False,
            "stage": "check",
            "returncode": checked.returncode,
            "stderr": decode(checked.stderr),
        }
    arguments.remove("--check")
    applied = run(arguments, cwd=directory, stdin=patch)
    return {
        "applied": applied.returncode == 0,
        "stage": "apply",
        "returncode": applied.returncode,
        "stderr": decode(applied.stderr),
    }


def normalized_junit(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    cases: list[dict[str, str | None]] = []
    for case in root.iter("testcase"):
        outcome = "passed"
        detail: str | None = None
        for tag in ("failure", "error", "skipped"):
            child = case.find(tag)
            if child is not None:
                outcome = tag
                detail = child.attrib.get("type") or child.attrib.get("message")
                break
        cases.append(
            {
                "classname": case.attrib.get("classname", ""),
                "name": case.attrib.get("name", ""),
                "outcome": outcome,
                "detail": detail,
            }
        )
    cases.sort(
        key=lambda item: (
            str(item["classname"]),
            str(item["name"]),
            str(item["outcome"]),
        )
    )
    counts = {key: 0 for key in ("passed", "failure", "error", "skipped")}
    for case in cases:
        counts[str(case["outcome"])] += 1
    canonical = json.dumps(
        cases, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return {
        "case_count": len(cases),
        "counts": counts,
        "cases_sha256": sha256_bytes(canonical.encode("ascii")),
        "cases": cases,
    }


def pytest_arm(
    *,
    python: Path,
    arm: Path,
    evidence: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    evidence.parent.mkdir(parents=True, exist_ok=True)
    junit_path = evidence.with_suffix(".xml")
    command = [str(python), "-m", "pytest", f"--junitxml={junit_path}"]
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment["PYTHONPATH"] = str(arm / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    path_entries = [
        entry
        for entry in environment.get("PATH", "").split(os.pathsep)
        if entry
    ]
    # Put the frozen environment first while leaving ordinary system commands
    # available.
    environment["PATH"] = os.pathsep.join([str(python.parent), *path_entries])
    before = tracked_content_state(arm)
    started = time.perf_counter()
    timed_out = False
    try:
        result = run(
            command,
            cwd=arm,
            env=environment,
            timeout=timeout_seconds,
        )
        returncode: int | None = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        returncode = None
        stdout = error.stdout or b""
        stderr = error.stderr or b""
    elapsed = time.perf_counter() - started
    evidence.with_suffix(".stdout.txt").write_bytes(stdout)
    evidence.with_suffix(".stderr.txt").write_bytes(stderr)
    normalized = normalized_junit(junit_path) if junit_path.exists() else None
    return {
        "command": command,
        "elapsed_seconds": elapsed,
        "returncode": returncode,
        "timed_out": timed_out,
        "tracked_content_before": {
            "file_count": before["file_count"],
            "sha256": before["sha256"],
        },
        "tracked_content_unchanged_after": tracked_state_matches(arm, before),
        "junit": normalized,
        "stdout_path": str(evidence.with_suffix(".stdout.txt").relative_to(PROJECT_ROOT)).replace(
            "\\", "/"
        ),
        "stderr_path": str(evidence.with_suffix(".stderr.txt").relative_to(PROJECT_ROOT)).replace(
            "\\", "/"
        ),
        "junit_path": (
            str(junit_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
            if junit_path.exists()
            else None
        ),
    }


def changed_test_failure_modules(
    junit: dict[str, Any] | None, python_test_paths: Iterable[str]
) -> list[str]:
    if junit is None:
        return []
    modules = {path[:-3].replace("/", ".") for path in python_test_paths}
    matched: list[str] = []
    for case in junit["cases"]:
        if case["outcome"] not in {"failure", "error"}:
            continue
        classname = str(case["classname"])
        if any(classname == module or classname.startswith(module + ".") for module in modules):
            matched.append(f"{classname}::{case['name']}")
    return sorted(matched)


def arm_is_green(arm: dict[str, Any]) -> bool:
    junit = arm.get("junit")
    return bool(
        arm.get("returncode") == 0
        and not arm.get("timed_out")
        and arm.get("tracked_content_unchanged_after")
        and junit is not None
        and junit["counts"]["failure"] == 0
        and junit["counts"]["error"] == 0
    )


def verify_candidate(
    *,
    repository: Path,
    python: Path,
    candidate: dict[str, Any],
    evidence_root: Path,
    temporary_root: Path,
) -> dict[str, Any]:
    pr = candidate["pr"]
    task_id = candidate["task_id"]
    parent = candidate["parent"]
    commit = candidate["sha"]
    patch = full_patch(repository, parent, commit)
    overlay = test_patch(repository, parent, commit)
    if sha256_bytes(patch) != candidate["full_patch_sha256"]:
        raise RuntimeError(f"full patch drift for {task_id}")
    if sha256_bytes(overlay) != candidate["test_patch_sha256"]:
        raise RuntimeError(f"test patch drift for {task_id}")
    parent_tree = ls_tree(repository, parent)
    commit_tree = ls_tree(repository, commit)
    candidate_evidence = evidence_root / task_id
    candidate_evidence.mkdir(parents=True, exist_ok=True)

    record: dict[str, Any] = {
        "task_id": task_id,
        "pr": pr,
        "sha": commit,
        "parent": parent,
        "started_at_utc": utc_now(),
        "accepted": False,
    }
    temporary = Path(tempfile.mkdtemp(prefix=f"a{pr}-", dir=temporary_root))
    try:
        base = temporary / "b"
        tests_only = temporary / "t"
        full = temporary / "f"
        reverse = temporary / "r"
        safe_extract_archive(repository, parent, base)
        base_matches, base_comparison = compare_directory_to_tree(base, parent_tree)
        record["base_tree_check"] = {
            "matches_parent": base_matches,
            **base_comparison,
        }
        if not base_matches:
            record["rejection_reason"] = "base_export_tree_mismatch"
            return record

        safe_extract_archive(repository, commit, reverse)
        reverse_application = apply_patch(reverse, patch, reverse=True)
        reverse_matches = False
        reverse_comparison: dict[str, Any] | None = None
        if reverse_application["applied"]:
            reverse_matches, reverse_comparison = compare_directory_to_tree(
                reverse, parent_tree
            )
        record["reverse_construction_check"] = {
            "application": reverse_application,
            "matches_parent": reverse_matches,
            "comparison": reverse_comparison,
        }
        if not reverse_application["applied"] or not reverse_matches:
            record["rejection_reason"] = "reverse_patch_does_not_reconstruct_parent"
            return record

        shutil.copytree(base, tests_only)
        shutil.copytree(base, full)

        base_arm = pytest_arm(
            python=python,
            arm=base,
            evidence=candidate_evidence / "base",
            timeout_seconds=120.0,
        )
        record["base"] = base_arm
        if not arm_is_green(base_arm):
            record["rejection_reason"] = "base_not_green"
            return record

        overlay_application = apply_patch(tests_only, overlay)
        record["test_patch_application"] = overlay_application
        if not overlay_application["applied"]:
            record["rejection_reason"] = "test_patch_does_not_apply"
            return record
        test_arm = pytest_arm(
            python=python,
            arm=tests_only,
            evidence=candidate_evidence / "tests-only",
            timeout_seconds=120.0,
        )
        matched_failures = changed_test_failure_modules(
            test_arm.get("junit"), candidate["python_test_paths"]
        )
        test_arm["changed_test_failures"] = matched_failures
        record["tests_only"] = test_arm
        junit = test_arm.get("junit")
        red_ok = bool(
            test_arm["returncode"] == 1
            and not test_arm["timed_out"]
            and test_arm["tracked_content_unchanged_after"]
            and junit is not None
            and (junit["counts"]["failure"] + junit["counts"]["error"]) >= 1
            and matched_failures
        )
        if not red_ok:
            record["rejection_reason"] = "test_overlay_not_qualifying_red"
            return record

        full_application = apply_patch(full, patch)
        record["full_patch_application"] = full_application
        if not full_application["applied"]:
            record["rejection_reason"] = "full_patch_does_not_apply"
            return record
        full_matches, full_comparison = compare_directory_to_tree(full, commit_tree)
        record["full_tree_check"] = {
            "matches_commit": full_matches,
            **full_comparison,
        }
        if not full_matches:
            record["rejection_reason"] = "full_patch_tree_mismatch"
            return record
        full_arm = pytest_arm(
            python=python,
            arm=full,
            evidence=candidate_evidence / "full",
            timeout_seconds=120.0,
        )
        record["full"] = full_arm
        if not arm_is_green(full_arm):
            record["rejection_reason"] = "full_patch_not_green"
            return record

        record["accepted"] = True
        record["rejection_reason"] = None
        return record
    except Exception as error:
        record["rejection_reason"] = "apparatus_exception"
        record["exception"] = f"{type(error).__name__}: {error}"
        return record
    finally:
        record["completed_at_utc"] = utc_now()
        shutil.rmtree(temporary, ignore_errors=True)


def verify(
    repository: Path,
    python: Path,
    ledger_path: Path,
    output: Path,
    *,
    target: int,
    budget: int,
) -> dict[str, Any]:
    ledger_bytes = ledger_path.read_bytes()
    ledger = json.loads(ledger_bytes)
    rule_sha = sha256_file(RULE_PATH)
    if ledger["selection_rule_sha256"] != rule_sha:
        raise RuntimeError("selection rule changed after ledger freeze")
    if ledger["head"] != HEAD:
        raise RuntimeError("candidate ledger has wrong source HEAD")
    actual_head = decode(git(repository, "rev-parse", "HEAD").stdout).strip()
    if actual_head != HEAD:
        raise RuntimeError("repository moved after gate/ledger")
    python = python.resolve(strict=True)
    evidence_root = output.parent / f"{output.stem}-arms"
    # Arms must live outside PROJECT_ROOT. Otherwise `git apply` discovers the
    # Blast-Radius parent repository and treats pluggy's patch paths as outside
    # the current subdirectory instead of editing the arm.
    temporary_root = Path(tempfile.gettempdir()) / "blast-radius-pluggy-causal"
    temporary_root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    accepted = 0
    started = utc_now()
    candidates = ledger["candidates"][:budget]
    for candidate in candidates:
        if accepted >= target:
            break
        print(
            f"examining {candidate['order']}/{min(len(candidates), budget)} "
            f"{candidate['task_id']} {candidate['sha'][:10]}",
            flush=True,
        )
        record = verify_candidate(
            repository=repository,
            python=python,
            candidate=candidate,
            evidence_root=evidence_root,
            temporary_root=temporary_root,
        )
        records.append(record)
        if record["accepted"]:
            accepted += 1
        partial = {
            "schema_version": 1,
            "measurement": "second-repository-causal-task-verification",
            "status": "running",
            "selection_rule_sha256": rule_sha,
            "candidate_ledger_sha256": sha256_bytes(ledger_bytes),
            "target": target,
            "budget": budget,
            "started_at_utc": started,
            "examined": len(records),
            "accepted": accepted,
            "records": records,
        }
        atomic_json(output.with_suffix(".partial.json"), partial)
        print(
            f"  {'ACCEPT' if record['accepted'] else 'reject'}: "
            f"{record.get('rejection_reason') or 'green-red-green'} "
            f"({accepted}/{target})",
            flush=True,
        )

    counts = collections.Counter(
        record["rejection_reason"] or "accepted" for record in records
    )
    value = {
        "schema_version": 1,
        "measurement": "second-repository-causal-task-verification",
        "status": "complete" if accepted >= target else "exhausted",
        "selection_rule_sha256": rule_sha,
        "candidate_ledger": str(ledger_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "candidate_ledger_sha256": sha256_bytes(ledger_bytes),
        "repository": str(repository.resolve()),
        "head": HEAD,
        "python": str(python),
        "python_version": decode(
            run([str(python), "--version"], cwd=PROJECT_ROOT, check=True).stdout
            or run([str(python), "--version"], cwd=PROJECT_ROOT, check=True).stderr
        ).strip(),
        "protocol": {
            "pytest_command": "python -m pytest --junitxml=<outside-arm>",
            "timeout_seconds": 120.0,
            "PYTHONPATH": "<arm>/src",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_ADDOPTS": "removed",
            "PATH": "frozen venv first",
        },
        "target": target,
        "budget": budget,
        "started_at_utc": started,
        "completed_at_utc": utc_now(),
        "counts": {
            "examined": len(records),
            "accepted": accepted,
            "by_result": dict(sorted(counts.items())),
        },
        "records": records,
    }
    atomic_json(output, value)
    write_digest(output)
    partial_path = output.with_suffix(".partial.json")
    if partial_path.exists():
        partial_path.unlink()
    try:
        temporary_root.rmdir()
    except OSError:
        pass
    print(json.dumps({"status": value["status"], "counts": value["counts"]}, indent=2))
    return value


def deterministic_commit_stream(repository: Path, revision: str) -> bytes:
    raw = git(
        repository,
        "log",
        "--first-parent",
        "--no-renames",
        "--format=C|%H|%ct",
        "--name-status",
        revision,
    ).stdout
    if not raw.endswith(b"\n"):
        raw += b"\n"
    return raw


def compact_arm_evidence(arm: dict[str, Any]) -> dict[str, Any]:
    """Keep reproducible counts and hashes without embedding every testcase."""
    junit = arm.get("junit")
    compact_junit = None
    if junit is not None:
        compact_junit = {
            "case_count": junit["case_count"],
            "counts": junit["counts"],
            "cases_sha256": junit["cases_sha256"],
        }
    value = {
        "command": "python -m pytest --junitxml=<outside-arm>",
        "elapsed_seconds": arm["elapsed_seconds"],
        "returncode": arm["returncode"],
        "timed_out": arm["timed_out"],
        "tracked_content_before": arm["tracked_content_before"],
        "tracked_content_unchanged_after": arm[
            "tracked_content_unchanged_after"
        ],
        "junit": compact_junit,
    }
    if "changed_test_failures" in arm:
        value["changed_test_failures"] = arm["changed_test_failures"]
    return value


def emit_fixture(
    repository: Path,
    ledger_path: Path,
    results_path: Path,
    destination: Path,
) -> dict[str, Any]:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    results = json.loads(results_path.read_text(encoding="utf-8"))
    if results["status"] != "complete" or results["counts"]["accepted"] != 30:
        raise RuntimeError("refusing to emit without exactly 30 accepted tasks")
    if destination.exists():
        raise RuntimeError(f"fixture destination already exists: {destination}")
    candidates = {candidate["task_id"]: candidate for candidate in ledger["candidates"]}
    accepted_records = [record for record in results["records"] if record["accepted"]]
    staging = destination.parent / f".{destination.name}.building"
    if staging.exists():
        raise RuntimeError(f"stale fixture staging directory exists: {staging}")
    staging.mkdir(parents=True)
    (staging / "patches").mkdir()
    (staging / "history").mkdir()
    manifest_tasks: list[dict[str, Any]] = []
    try:
        for record in accepted_records:
            candidate = candidates[record["task_id"]]
            base_name = f"base-{record['task_id']}"
            base_path = staging / base_name
            safe_extract_archive(repository, candidate["parent"], base_path)
            patch = full_patch(repository, candidate["parent"], candidate["sha"])
            overlay = test_patch(repository, candidate["parent"], candidate["sha"])
            patch_path = staging / "patches" / f"{record['task_id']}.patch"
            overlay_path = staging / "patches" / f"{record['task_id']}.tests.patch"
            patch_path.write_bytes(patch)
            overlay_path.write_bytes(overlay)
            base_matches, comparison = compare_directory_to_tree(
                base_path, ls_tree(repository, candidate["parent"])
            )
            if not base_matches:
                raise RuntimeError(
                    f"emitted base mismatch for {record['task_id']}: {comparison}"
                )
            manifest_tasks.append(
                {
                    **candidate,
                    "base": base_name,
                    "full_patch": f"patches/{record['task_id']}.patch",
                    "test_patch": f"patches/{record['task_id']}.tests.patch",
                    "verification": {
                        "base": compact_arm_evidence(record["base"]),
                        "tests_only": compact_arm_evidence(record["tests_only"]),
                        "full": compact_arm_evidence(record["full"]),
                        "reverse_construction_check": record[
                            "reverse_construction_check"
                        ],
                        "full_tree_check": record["full_tree_check"],
                    },
                }
            )

        (staging / "history" / "HEAD.txt").write_text(
            HEAD + "\n", encoding="ascii", newline="\n"
        )
        stream = deterministic_commit_stream(repository, HEAD)
        with (staging / "history" / "commit-stream.txt.gz").open("wb") as raw_handle:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=raw_handle, mtime=0
            ) as compressed:
                compressed.write(stream)
        manifest = {
            "schema_version": 1,
            "measurement": "second-repository-causal-fixture",
            "repository": "pytest-dev/pluggy",
            "repository_url": "https://github.com/pytest-dev/pluggy.git",
            "head": HEAD,
            "head_tree": ledger["head_tree"],
            "selection_rule_sha256": results["selection_rule_sha256"],
            "candidate_ledger_sha256": results["candidate_ledger_sha256"],
            "verification_results_sha256": sha256_file(results_path),
            "patch_semantics": {
                "pr-N.patch": "full first-parent diff including test hunks",
                "pr-N.tests.patch": "testing/ path subset only",
            },
            "task_count": len(manifest_tasks),
            "tasks": manifest_tasks,
        }
        atomic_json(staging / "TASKS.json", manifest)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    summary = {
        "destination": str(destination),
        "tasks": len(manifest_tasks),
        "bases": len(list(destination.glob("base-*"))),
        "patch_files": len(list((destination / "patches").glob("*.patch"))),
        "manifest_sha256": sha256_file(destination / "TASKS.json"),
    }
    print(json.dumps(summary, indent=2))
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    subparsers = parser.add_subparsers(dest="action", required=True)

    inventory_parser = subparsers.add_parser("inventory")
    inventory_parser.add_argument("--output", type=Path, default=DEFAULT_LEDGER)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--python", type=Path, required=True)
    verify_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    verify_parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS)
    verify_parser.add_argument("--target", type=int, default=30)
    verify_parser.add_argument("--budget", type=int, default=200)

    emit_parser = subparsers.add_parser("emit")
    emit_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    emit_parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    emit_parser.add_argument("--destination", type=Path, default=DEFAULT_FIXTURE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = args.repo.resolve(strict=True)
    if args.action == "inventory":
        inventory(repository, args.output.resolve())
        return 0
    if args.action == "verify":
        value = verify(
            repository,
            args.python,
            args.ledger.resolve(strict=True),
            args.output.resolve(),
            target=args.target,
            budget=args.budget,
        )
        return 0 if value["status"] == "complete" else 1
    if args.action == "emit":
        emit_fixture(
            repository,
            args.ledger.resolve(strict=True),
            args.results.resolve(strict=True),
            args.destination.resolve(),
        )
        return 0
    raise AssertionError(args.action)


if __name__ == "__main__":
    raise SystemExit(main())
