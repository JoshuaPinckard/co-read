"""Build the outcome-frozen Pygments causal-dependency fixture.

The protocol is frozen in ``exploratory/causal/PYGMENTS-SELECTION-RULE.md``.
Run ``inventory`` before ``verify``: inventory performs every outcome-blind
Git and patch filter, freezes the greedy path-disjoint order, and writes a
digest before any historical pytest process is started.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULE_PATH = PROJECT_ROOT / "exploratory" / "causal" / "PYGMENTS-SELECTION-RULE.md"
DEFAULT_REPO = PROJECT_ROOT / "exploratory" / "causal" / "repositories" / "pygments"
DEFAULT_LEDGER = PROJECT_ROOT / "exploratory" / "causal" / "inventory" / "pygments-candidates.json"
DEFAULT_RESULTS = PROJECT_ROOT / "exploratory" / "causal" / "verification" / "pygments-results.json"
DEFAULT_FIXTURE = PROJECT_ROOT / "fixture" / "pygments"

HEAD = "38f426a6b1cd4ffc6429f5808031b7c62ea57b1f"
CUTOFF_TIMESTAMP = 1_704_067_200  # 2024-01-01T00:00:00Z
TARGET_TASKS = 30
PROCESS_TIMEOUT_SECONDS = 120.0
SQUASH_PR_RE = re.compile(r"\(#([1-9][0-9]*)\)$")
MERGE_PR_RE = re.compile(r"^Merge pull request #([1-9][0-9]*)\b")
REVERT_RE = re.compile(r"^\s*revert\b", re.IGNORECASE)
REGULAR_MODES = {"100644", "100755"}
ALLOWED_HISTORICAL_DISTRIBUTIONS = {
    "colorama",
    "iniconfig",
    "packaging",
    "pip",
    "pluggy",
    "pygments",
    "pytest",
    "setuptools",
    "wheel",
}
EXPECTED_KEPT_PRS = (
    3225, 3217, 3209, 2969, 3215, 3216, 3214, 3213, 3211, 3206,
    3210, 3204, 3201, 3199, 3197, 3195, 3190, 3185, 3177, 3163,
    3164, 3140, 3143, 3160, 3159, 3165, 3167, 3176, 3172, 3168,
    3175, 3119, 3069, 3060, 3057, 3052, 2831, 2980, 3002, 2864,
    2972, 2789, 2750, 2755, 2595, 2631, 2619,
)
EXPECTED_GLOBAL_REVERSE_TREE = "fdba588edb787a757b5f332715bb119d1c11397a"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
    timeout: float = PROCESS_TIMEOUT_SECONDS,
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
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return run(
        ["git", "-c", "core.longpaths=true", "-c", "core.autocrlf=false", *arguments],
        cwd=repository,
        stdin=stdin,
        env=env,
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


def display_path(path: Path) -> str:
    try:
        value = path.resolve().relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        value = path.resolve()
    return value.as_posix()


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
            changes.append({"status": status, "old_path": old_path, "path": new_path})
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
        result[decode(path_raw)] = {"mode": mode, "type": object_type, "oid": oid}
    return result


def revision_tree(repository: Path, revision: str) -> str:
    return decode(git(repository, "rev-parse", f"{revision}^{{tree}}").stdout).strip()


def reduced_changes(repository: Path, parent: str, commit: str) -> list[dict[str, str]]:
    return parse_name_status(
        git(
            repository,
            "diff",
            "--name-status",
            "-z",
            "--find-renames=50%",
            "-l0",
            parent,
            commit,
            "--",
            "pygments/",
            "tests/",
        ).stdout
    )


def reduced_full_patch(repository: Path, parent: str, commit: str) -> bytes:
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
        "pygments/",
        "tests/",
    ).stdout


def reduced_test_patch(repository: Path, parent: str, commit: str) -> bytes:
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
        "tests/",
    ).stdout


def collectable_test_kind(path: str) -> str | None:
    if not path.startswith("tests/") or path.startswith("tests/contrast/"):
        return None
    pure = Path(path)
    if path.startswith("tests/examplefiles/"):
        if pure.name != "conftest.py" and not pure.name.endswith(".output"):
            return "example"
        return None
    if path.startswith("tests/snippets/"):
        return "snippet" if pure.suffix == ".txt" else None
    if pure.suffix == ".py" and (
        pure.name.startswith("test_") or pure.name.endswith("_test.py")
    ):
        return "python"
    return None


def cached_patch_sequence(
    repository: Path,
    start_treeish: str,
    operations: Sequence[tuple[bytes, bool]],
) -> dict[str, Any]:
    """Apply patches to a temporary Git index and return its exact tree."""
    temporary = Path(tempfile.mkdtemp(prefix="pygments-index-"))
    index_path = temporary / "index"
    environment = os.environ.copy()
    environment["GIT_INDEX_FILE"] = str(index_path)
    checks: list[dict[str, Any]] = []
    try:
        git(repository, "read-tree", start_treeish, env=environment)
        for position, (patch, reverse) in enumerate(operations, 1):
            arguments = ["apply", "--cached"]
            if reverse:
                arguments.append("--reverse")
            checked = git(
                repository,
                *arguments,
                "--check",
                "--binary",
                "-",
                stdin=patch,
                env=environment,
                check=False,
            )
            check_record = {
                "position": position,
                "reverse": reverse,
                "check_returncode": checked.returncode,
                "check_stderr": decode(checked.stderr),
            }
            checks.append(check_record)
            if checked.returncode != 0:
                return {"applied": False, "checks": checks, "tree": None}
            applied = git(
                repository,
                *arguments,
                "--binary",
                "-",
                stdin=patch,
                env=environment,
                check=False,
            )
            check_record["apply_returncode"] = applied.returncode
            check_record["apply_stderr"] = decode(applied.stderr)
            if applied.returncode != 0:
                return {"applied": False, "checks": checks, "tree": None}
        tree = decode(git(repository, "write-tree", env=environment).stdout).strip()
        return {"applied": True, "checks": checks, "tree": tree}
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def parse_first_parent_log(repository: Path) -> list[dict[str, Any]]:
    raw = decode(
        git(
            repository,
            "log",
            "--first-parent",
            "--format=@@@%H%x09%P%x09%ct%x09%cI%x09%s",
            HEAD,
        ).stdout
    )
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.startswith("@@@"):
            continue
        sha, parents, timestamp, committed_at, subject = line[3:].split("\t", 4)
        value = {
            "sha": sha,
            "parents": parents.split() if parents else [],
            "timestamp": int(timestamp),
            "committed_at": committed_at,
            "subject": subject,
        }
        if value["timestamp"] >= CUTOFF_TIMESTAMP:
            records.append(value)
    return records


def inventory(repository: Path, output: Path) -> dict[str, Any]:
    actual_head = decode(git(repository, "rev-parse", "HEAD").stdout).strip()
    if actual_head != HEAD:
        raise RuntimeError(f"repository HEAD {actual_head} does not match frozen {HEAD}")
    anchor_tree_oid = revision_tree(repository, HEAD)
    anchor_tree = ls_tree(repository, HEAD)
    if any(
        item["type"] != "blob" or item["mode"] not in REGULAR_MODES
        for item in anchor_tree.values()
    ):
        raise RuntimeError("frozen anchor contains a non-regular tree entry")

    records = parse_first_parent_log(repository)
    rejections: collections.Counter[str] = collections.Counter()
    decisions: list[dict[str, Any]] = []
    structurally_eligible: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    kept_paths: set[str] = set()
    seen_prs: set[int] = set()
    pr_landings = 0

    def reject(record: dict[str, Any], reason: str, **extra: Any) -> None:
        rejections[reason] += 1
        decisions.append(
            {
                "history_position": record["history_position"],
                "sha": record["sha"],
                "committed_at": record["committed_at"],
                "subject": record["subject"],
                "accepted": False,
                "reason": reason,
                **extra,
            }
        )

    for history_position, original in enumerate(records, 1):
        record = {**original, "history_position": history_position}
        parents = record["parents"]
        suffix_match = SQUASH_PR_RE.search(record["subject"])
        merge_match = MERGE_PR_RE.match(record["subject"])
        match = suffix_match or merge_match
        if (
            len(parents) not in {1, 2}
            or match is None
            or REVERT_RE.match(record["subject"]) is not None
        ):
            reject(record, "not_unambiguous_pr_landing")
            continue
        pr_landings += 1
        pr = int(match.group(1))
        if pr in seen_prs:
            reject(record, "duplicate_pr_number", pr=pr)
            continue
        seen_prs.add(pr)
        parent = parents[0]
        changes = reduced_changes(repository, parent, record["sha"])
        paths = [change["path"] for change in changes]
        if not 2 <= len(changes) <= 30:
            reject(record, "reduced_path_count_outside_2_30", pr=pr, path_count=len(changes))
            continue
        if any(change["status"][:1] not in {"A", "M"} for change in changes):
            reject(record, "reduced_status_outside_A_M", pr=pr)
            continue
        source_python_paths = [
            path for path in paths if path.startswith("pygments/") and path.endswith(".py")
        ]
        test_paths = [path for path in paths if path.startswith("tests/")]
        target_kinds = {
            path: kind
            for path in test_paths
            if (kind := collectable_test_kind(path)) is not None
        }
        test_targets = sorted(target_kinds)
        if not source_python_paths:
            reject(record, "missing_production_python", pr=pr)
            continue
        if not test_targets:
            reject(record, "missing_collectable_test_artifact", pr=pr)
            continue
        if len(test_paths) > 10:
            reject(record, "more_than_ten_test_paths", pr=pr, test_path_count=len(test_paths))
            continue
        patch = reduced_full_patch(repository, parent, record["sha"])
        overlay = reduced_test_patch(repository, parent, record["sha"])
        if not patch or not overlay:
            reject(record, "empty_reduced_patch", pr=pr)
            continue
        if len(patch) > 500 * 1024:
            reject(record, "reduced_patch_over_500_KiB", pr=pr, patch_bytes=len(patch))
            continue
        parent_tree = ls_tree(repository, parent)
        commit_tree = ls_tree(repository, record["sha"])
        objects_regular = True
        for change in changes:
            path = change["path"]
            after = commit_tree.get(path)
            before = parent_tree.get(path)
            if after is None or after["type"] != "blob" or after["mode"] not in REGULAR_MODES:
                objects_regular = False
                break
            if change["status"] == "M" and (
                before is None
                or before["type"] != "blob"
                or before["mode"] not in REGULAR_MODES
            ):
                objects_regular = False
                break
        if not objects_regular:
            reject(record, "affected_object_not_regular", pr=pr)
            continue
        reverse_check = cached_patch_sequence(repository, HEAD, [(patch, True)])
        if not reverse_check["applied"]:
            reject(
                record,
                "anchor_cached_reverse_check_failed",
                pr=pr,
                reverse_checks=reverse_check["checks"],
            )
            continue

        identity = (
            f"{len(parents)}-parent terminal '(#N)' subject"
            if suffix_match is not None
            else f"{len(parents)}-parent 'Merge pull request #N' subject"
        )
        candidate = {
            "structural_order": len(structurally_eligible) + 1,
            "history_position": history_position,
            "pr": pr,
            "task_id": f"pr-{pr}",
            "sha": record["sha"],
            "commit_tree": revision_tree(repository, record["sha"]),
            "parent": parent,
            "parent_tree": revision_tree(repository, parent),
            "timestamp": record["timestamp"],
            "committed_at": record["committed_at"],
            "subject": record["subject"],
            "pr_url": f"https://github.com/pygments/pygments/pull/{pr}",
            "pr_identity_provenance": f"{identity}; remote PR page not queried",
            "changes": changes,
            "paths": paths,
            "source_python_paths": source_python_paths,
            "test_paths": test_paths,
            "test_targets": test_targets,
            "test_target_kinds": target_kinds,
            "anchor_reverse_tree": reverse_check["tree"],
            "full_patch_bytes": len(patch),
            "full_patch_sha256": sha256_bytes(patch),
            "test_patch_bytes": len(overlay),
            "test_patch_sha256": sha256_bytes(overlay),
        }
        structurally_eligible.append(candidate)
        overlap = sorted(set(paths) & kept_paths)
        if overlap:
            reject(record, "path_overlap_with_earlier_kept", pr=pr, overlapping_paths=overlap)
            continue
        candidate["order"] = len(kept) + 1
        kept.append(candidate)
        kept_paths.update(paths)
        decisions.append(
            {
                "history_position": history_position,
                "sha": record["sha"],
                "committed_at": record["committed_at"],
                "subject": record["subject"],
                "pr": pr,
                "accepted": True,
                "reason": None,
                "candidate_order": candidate["order"],
            }
        )
        if pr_landings % 50 == 0:
            print(
                f"static inventory: {pr_landings} PR landings, {len(kept)} disjoint candidates",
                flush=True,
            )

    kept_patches = [
        reduced_full_patch(repository, candidate["parent"], candidate["sha"])
        for candidate in kept
    ]
    global_reverse = cached_patch_sequence(
        repository, HEAD, [(patch, True) for patch in kept_patches]
    )
    if not global_reverse["applied"] or global_reverse["tree"] is None:
        raise RuntimeError("global kept-candidate cached reversal failed")
    global_replay = cached_patch_sequence(
        repository,
        str(global_reverse["tree"]),
        [(patch, False) for patch in kept_patches],
    )
    if not global_replay["applied"] or global_replay["tree"] != anchor_tree_oid:
        raise RuntimeError("global kept-candidate replay did not reconstruct anchor")
    static_crosscheck = {
        "first_parent_commits": len(records),
        "pr_landings": pr_landings,
        "structurally_eligible": len(structurally_eligible),
        "kept": len(kept),
        "kept_prs": tuple(candidate["pr"] for candidate in kept),
        "combined_patch_bytes": sum(len(patch) for patch in kept_patches),
        "non_pr": rejections["not_unambiguous_pr_landing"],
        "path_count": rejections["reduced_path_count_outside_2_30"],
        "missing_target": rejections["missing_collectable_test_artifact"],
        "test_path_count": rejections["more_than_ten_test_paths"],
        "regularity": rejections["affected_object_not_regular"],
        "reverse": rejections["anchor_cached_reverse_check_failed"],
        "overlap": rejections["path_overlap_with_earlier_kept"],
    }
    expected_crosscheck = {
        "first_parent_commits": 385,
        "pr_landings": 231,
        "structurally_eligible": 71,
        "kept": 47,
        "kept_prs": EXPECTED_KEPT_PRS,
        "combined_patch_bytes": 278_612,
        "non_pr": 154,
        "path_count": 75,
        # With the literal output-only rule applied at its actual filter
        # position, four candidates that would also fail later object/reverse
        # checks are recorded here by their first rejection reason.
        "missing_target": 30,
        "test_path_count": 4,
        "regularity": 0,
        "reverse": 51,
        "overlap": 24,
    }
    if static_crosscheck != expected_crosscheck:
        raise RuntimeError(
            "static inventory differs from independently audited frozen result: "
            f"actual={static_crosscheck!r} expected={expected_crosscheck!r}"
        )
    if global_reverse["tree"] != EXPECTED_GLOBAL_REVERSE_TREE:
        raise RuntimeError(
            f"global reverse tree differs from audited tree: {global_reverse['tree']}"
        )

    value = {
        "schema_version": 1,
        "measurement": "pygments-static-candidate-ledger",
        "created_at_utc": utc_now(),
        "selection_rule": display_path(RULE_PATH),
        "selection_rule_sha256": sha256_file(RULE_PATH),
        "repository": str(repository.resolve()),
        "repository_url": "https://github.com/pygments/pygments.git",
        "head": HEAD,
        "head_tree": anchor_tree_oid,
        "history_cutoff_inclusive": "2024-01-01T00:00:00Z",
        "order": "first-parent newest-to-oldest, then greedy global path-disjoint",
        "target_tasks": TARGET_TASKS,
        "counts": {
            "first_parent_commits_scanned_in_window": len(records),
            "unambiguous_pr_landings": pr_landings,
            "unique_pr_numbers": len(seen_prs),
            "structurally_eligible_before_disjointness": len(structurally_eligible),
            "kept_path_disjoint_candidates": len(kept),
            "static_rejections_by_first_reason": dict(sorted(rejections.items())),
            "static_rejection_groups": {
                "reverse_failed": (
                    rejections["affected_object_not_regular"]
                    + rejections["anchor_cached_reverse_check_failed"]
                ),
            },
        },
        "aggregate": {
            "kept_full_patch_bytes": sum(len(patch) for patch in kept_patches),
            "kept_changed_paths": len(kept_paths),
            "kept_task_ids": [candidate["task_id"] for candidate in kept],
            "global_reverse_tree": global_reverse["tree"],
            "global_reverse_checks": global_reverse["checks"],
            "global_replay_tree": global_replay["tree"],
            "global_replay_checks": global_replay["checks"],
            "global_replay_matches_anchor": global_replay["tree"] == anchor_tree_oid,
            "independent_static_crosscheck": "matched",
        },
        "decisions": decisions,
        "candidates": kept,
    }
    atomic_json(output, value)
    digest = write_digest(output)
    print(json.dumps({"ledger": str(output), "ledger_sha256": digest, "counts": value["counts"]}, indent=2))
    return value


def extract_raw_tree(repository: Path, revision: str, destination: Path) -> None:
    """Materialize exact Git blobs, bypassing export and checkout transforms."""
    tree = ls_tree(repository, revision)
    ordered = sorted(tree.items())
    if any(
        item["type"] != "blob" or item["mode"] not in REGULAR_MODES
        for _, item in ordered
    ):
        raise RuntimeError(f"cannot extract non-regular tree {revision}")
    queries = b"".join(f"{item['oid']}\n".encode("ascii") for _, item in ordered)
    batch = git(repository, "cat-file", "--batch", stdin=queries).stdout
    destination.mkdir(parents=True, exist_ok=False)
    cursor = 0
    for relative, item in ordered:
        pure = Path(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise RuntimeError(f"unsafe tree path: {relative!r}")
        line_end = batch.find(b"\n", cursor)
        if line_end < 0:
            raise RuntimeError("truncated cat-file header")
        header = batch[cursor:line_end].decode("ascii").split()
        if len(header) != 3:
            raise RuntimeError(f"invalid cat-file header: {header!r}")
        oid, object_type, size_text = header
        size = int(size_text)
        cursor = line_end + 1
        payload = batch[cursor : cursor + size]
        cursor += size
        if cursor >= len(batch) or batch[cursor : cursor + 1] != b"\n":
            raise RuntimeError("truncated cat-file payload")
        cursor += 1
        if oid != item["oid"] or object_type != "blob" or len(payload) != size:
            raise RuntimeError(f"cat-file identity mismatch for {relative}")
        path = destination / pure
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        if item["mode"] == "100755":
            path.chmod(0o755)
    if cursor != len(batch):
        raise RuntimeError("unexpected trailing cat-file data")


def blob_oid(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def compare_directory_to_tree(
    directory: Path, expected: dict[str, dict[str, str]]
) -> tuple[bool, dict[str, Any]]:
    actual = {
        path.relative_to(directory).as_posix(): path
        for path in directory.rglob("*")
        if path.is_file()
    }
    expected_paths = set(expected)
    actual_paths = set(actual)
    missing = sorted(expected_paths - actual_paths)
    extra = sorted(actual_paths - expected_paths)
    mismatched = [
        path
        for path in sorted(expected_paths & actual_paths)
        if expected[path]["type"] != "blob"
        or blob_oid(actual[path].read_bytes()) != expected[path]["oid"]
    ]
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
    files = [
        (path.relative_to(directory).as_posix(), sha256_file(path))
        for path in sorted(item for item in directory.rglob("*") if item.is_file())
    ]
    canonical = json.dumps(files, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return {"file_count": len(files), "sha256": sha256_bytes(canonical), "files": files}


def tracked_state_matches(directory: Path, before: dict[str, Any]) -> bool:
    return all(
        (directory / relative).is_file()
        and sha256_file(directory / relative) == expected_sha
        for relative, expected_sha in before["files"]
    )


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
    checked = run([*arguments, "--check", "--binary", "-"], cwd=directory, stdin=patch)
    if checked.returncode != 0:
        return {
            "applied": False,
            "stage": "check",
            "returncode": checked.returncode,
            "stderr": decode(checked.stderr),
        }
    applied = run([*arguments, "--binary", "-"], cwd=directory, stdin=patch)
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
                "file": case.attrib.get("file"),
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
    canonical = json.dumps(cases, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
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
    targets: Sequence[str] = (),
) -> dict[str, Any]:
    evidence.parent.mkdir(parents=True, exist_ok=True)
    junit_path = evidence.with_suffix(".xml")
    if junit_path.exists():
        junit_path.unlink()
    command = [
        str(python),
        "-m",
        "pytest",
        "--ignore=tests/contrast",
        f"--junitxml={junit_path}",
        *targets,
    ]
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONPATH"] = str(arm)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PATH"] = os.pathsep.join(
        [str(python.parent), *[item for item in environment.get("PATH", "").split(os.pathsep) if item]]
    )
    before = tracked_content_state(arm)
    started = time.perf_counter()
    timed_out = False
    try:
        result = run(command, cwd=arm, env=environment)
        returncode: int | None = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as error:
        timed_out = True
        returncode = None
        stdout = error.stdout or b""
        stderr = error.stderr or b""
    elapsed = time.perf_counter() - started
    stdout_path = evidence.with_suffix(".stdout.txt")
    stderr_path = evidence.with_suffix(".stderr.txt")
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    junit = normalized_junit(junit_path) if junit_path.exists() else None
    return {
        "command": command,
        "targets": list(targets),
        "elapsed_seconds": elapsed,
        "returncode": returncode,
        "timed_out": timed_out,
        "tracked_content_before": {"file_count": before["file_count"], "sha256": before["sha256"]},
        "tracked_content_unchanged_after": tracked_state_matches(arm, before),
        "junit": junit,
        "stdout_path": display_path(stdout_path),
        "stderr_path": display_path(stderr_path),
        "junit_path": display_path(junit_path) if junit_path.exists() else None,
    }


def mapped_changed_failures(
    junit: dict[str, Any] | None, target_kinds: dict[str, str]
) -> list[dict[str, str]]:
    if junit is None:
        return []
    result: list[dict[str, str]] = []
    for case in junit["cases"]:
        if case["outcome"] not in {"failure", "error"}:
            continue
        classname = str(case.get("classname") or "").replace("\\", "/").replace("/", ".")
        name = str(case.get("name") or "").replace("\\", "/").replace("/", ".")
        file_value = str(case.get("file") or "").replace("\\", "/")
        for target, kind in target_kinds.items():
            normalized = target.replace("\\", "/")
            dotted = normalized.replace("/", ".")
            if kind == "python":
                module = dotted[:-3]
                matched = classname == module or classname.startswith(module + ".")
                # Collection errors are represented at file level and can put
                # the mangled module path in ``name`` with an empty classname.
                matched = matched or (not classname and name in {module, dotted})
                matched = matched or file_value == normalized
            elif kind in {"example", "snippet"}:
                # Pygments' custom golden items use the dotted full input path
                # (including its suffix) as the JUnit classname and an empty
                # item name. Exact matching avoids attributing an unrelated
                # golden failure with the same basename.
                mangled = dotted[:-3] if normalized.endswith(".py") else dotted
                matched = classname == mangled or classname == dotted
                matched = matched or (not classname and name in {mangled, dotted})
                matched = matched or file_value == normalized
            else:
                raise ValueError(f"unknown changed test target kind: {kind!r}")
            if matched:
                result.append(
                    {
                        "target": target,
                        "case": f"{case['classname']}::{case['name']}",
                        "outcome": str(case["outcome"]),
                    }
                )
                break
    return sorted(result, key=lambda item: (item["target"], item["case"]))


def compact_arm(arm: dict[str, Any]) -> dict[str, Any]:
    junit = arm.get("junit")
    compact_junit = None
    if junit is not None:
        compact_junit = {
            "case_count": junit["case_count"],
            "counts": junit["counts"],
            "cases_sha256": junit["cases_sha256"],
            "failing_cases": [
                case for case in junit["cases"] if case["outcome"] in {"failure", "error"}
            ],
        }
    result = {
        "command": arm["command"],
        "targets": arm["targets"],
        "elapsed_seconds": arm["elapsed_seconds"],
        "returncode": arm["returncode"],
        "timed_out": arm["timed_out"],
        "tracked_content_before": arm["tracked_content_before"],
        "tracked_content_unchanged_after": arm["tracked_content_unchanged_after"],
        "junit": compact_junit,
        "stdout_path": arm["stdout_path"],
        "stderr_path": arm["stderr_path"],
        "junit_path": arm["junit_path"],
    }
    if "mapped_changed_failures" in arm:
        result["mapped_changed_failures"] = arm["mapped_changed_failures"]
    return result


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


def arm_is_qualifying_red(arm: dict[str, Any]) -> bool:
    junit = arm.get("junit")
    return bool(
        arm.get("returncode") == 1
        and not arm.get("timed_out")
        and arm.get("tracked_content_unchanged_after")
        and junit is not None
        and (junit["counts"]["failure"] + junit["counts"]["error"]) >= 1
        and arm.get("mapped_changed_failures")
    )


def candidate_patches(
    repository: Path, candidate: dict[str, Any]
) -> tuple[bytes, bytes]:
    patch = reduced_full_patch(repository, candidate["parent"], candidate["sha"])
    overlay = reduced_test_patch(repository, candidate["parent"], candidate["sha"])
    if sha256_bytes(patch) != candidate["full_patch_sha256"]:
        raise RuntimeError(f"full patch drift for {candidate['task_id']}")
    if sha256_bytes(overlay) != candidate["test_patch_sha256"]:
        raise RuntimeError(f"test patch drift for {candidate['task_id']}")
    return patch, overlay


def screen_candidate(
    *,
    repository: Path,
    python: Path,
    candidate: dict[str, Any],
    evidence_root: Path,
    temporary_root: Path,
) -> dict[str, Any]:
    task_id = candidate["task_id"]
    record: dict[str, Any] = {
        "task_id": task_id,
        "pr": candidate["pr"],
        "sha": candidate["sha"],
        "order": candidate["order"],
        "started_at_utc": utc_now(),
        "screen_passed": False,
    }
    temporary = Path(tempfile.mkdtemp(prefix=f"screen-{task_id}-", dir=temporary_root))
    try:
        patch, overlay = candidate_patches(repository, candidate)
        expected_reverse = cached_patch_sequence(repository, HEAD, [(patch, True)])
        if (
            not expected_reverse["applied"]
            or expected_reverse["tree"] != candidate["anchor_reverse_tree"]
        ):
            record["rejection_reason"] = "static_reverse_check_drift"
            record["reverse_index"] = expected_reverse
            return record

        singleton_base = temporary / "base"
        extract_raw_tree(repository, HEAD, singleton_base)
        reverse_application = apply_patch(singleton_base, patch, reverse=True)
        record["reverse_application"] = reverse_application
        if not reverse_application["applied"]:
            record["rejection_reason"] = "anchor_reverse_patch_does_not_apply"
            return record
        reverse_matches, reverse_comparison = compare_directory_to_tree(
            singleton_base, ls_tree(repository, candidate["anchor_reverse_tree"])
        )
        record["reverse_tree_check"] = {
            "matches": reverse_matches,
            "expected_tree": candidate["anchor_reverse_tree"],
            **reverse_comparison,
        }
        if not reverse_matches:
            record["rejection_reason"] = "anchor_reverse_tree_mismatch"
            return record

        tests_only = temporary / "tests-only"
        full = temporary / "full"
        shutil.copytree(singleton_base, tests_only)
        shutil.copytree(singleton_base, full)

        overlay_application = apply_patch(tests_only, overlay)
        record["test_patch_application"] = overlay_application
        if not overlay_application["applied"]:
            record["rejection_reason"] = "targeted_test_patch_does_not_apply"
            return record
        red_arm = pytest_arm(
            python=python,
            arm=tests_only,
            evidence=evidence_root / task_id / "tests-only-targeted",
            targets=candidate["test_targets"],
        )
        red_arm["mapped_changed_failures"] = mapped_changed_failures(
            red_arm.get("junit"), candidate["test_target_kinds"]
        )
        record["tests_only_targeted"] = compact_arm(red_arm)
        if not arm_is_qualifying_red(red_arm):
            record["rejection_reason"] = "targeted_test_overlay_not_qualifying_red"
            return record

        full_application = apply_patch(full, patch)
        record["full_patch_application"] = full_application
        if not full_application["applied"]:
            record["rejection_reason"] = "targeted_full_patch_does_not_apply"
            return record
        full_matches, full_comparison = compare_directory_to_tree(
            full, ls_tree(repository, HEAD)
        )
        record["full_anchor_check"] = {
            "matches": full_matches,
            "expected_tree": revision_tree(repository, HEAD),
            **full_comparison,
        }
        if not full_matches:
            record["rejection_reason"] = "targeted_full_patch_does_not_restore_anchor"
            return record
        green_arm = pytest_arm(
            python=python,
            arm=full,
            evidence=evidence_root / task_id / "full-targeted",
            targets=candidate["test_targets"],
        )
        record["full_targeted"] = compact_arm(green_arm)
        if not arm_is_green(green_arm):
            record["rejection_reason"] = "targeted_full_patch_not_green"
            return record

        record["screen_passed"] = True
        record["rejection_reason"] = None
        return record
    except Exception:
        # Apparatus failures must abort verification. Treating one as an
        # outcome rejection would silently change the frozen candidate set.
        raise
    finally:
        record["completed_at_utc"] = utc_now()
        shutil.rmtree(temporary, ignore_errors=True)


def construct_shared_base(
    *,
    repository: Path,
    selected: Sequence[dict[str, Any]],
    destination: Path,
) -> dict[str, Any]:
    patches = [candidate_patches(repository, candidate)[0] for candidate in selected]
    reversed_index = cached_patch_sequence(
        repository, HEAD, [(patch, True) for patch in patches]
    )
    if not reversed_index["applied"] or reversed_index["tree"] is None:
        raise RuntimeError("selected patches failed cumulative cached reversal")
    shared_tree = str(reversed_index["tree"])
    replayed_index = cached_patch_sequence(
        repository, shared_tree, [(patch, False) for patch in patches]
    )
    anchor_tree = revision_tree(repository, HEAD)
    if not replayed_index["applied"] or replayed_index["tree"] != anchor_tree:
        raise RuntimeError("selected cached replay did not reconstruct the anchor tree")

    extract_raw_tree(repository, HEAD, destination)
    reverse_applications: list[dict[str, Any]] = []
    for candidate, patch in zip(selected, patches, strict=True):
        application = apply_patch(destination, patch, reverse=True)
        reverse_applications.append({"task_id": candidate["task_id"], **application})
        if not application["applied"]:
            raise RuntimeError(f"shared-base reverse failed for {candidate['task_id']}")
    base_matches, base_comparison = compare_directory_to_tree(
        destination, ls_tree(repository, shared_tree)
    )
    if not base_matches:
        raise RuntimeError(f"materialized shared base differs from index tree: {base_comparison}")

    replay = destination.parent / "replay"
    shutil.copytree(destination, replay)
    replay_applications: list[dict[str, Any]] = []
    try:
        for candidate, patch in zip(selected, patches, strict=True):
            application = apply_patch(replay, patch)
            replay_applications.append({"task_id": candidate["task_id"], **application})
            if not application["applied"]:
                raise RuntimeError(f"shared-base replay failed for {candidate['task_id']}")
        replay_matches, replay_comparison = compare_directory_to_tree(
            replay, ls_tree(repository, HEAD)
        )
        if not replay_matches:
            raise RuntimeError(f"shared-base replay differs from anchor: {replay_comparison}")
    finally:
        shutil.rmtree(replay, ignore_errors=True)
    return {
        "tree": shared_tree,
        "selected_task_ids": [candidate["task_id"] for candidate in selected],
        "cached_reverse_checks": reversed_index["checks"],
        "cached_replay_tree": replayed_index["tree"],
        "reverse_applications": reverse_applications,
        "replay_applications": replay_applications,
        "base_comparison": base_comparison,
        "replay_comparison": replay_comparison,
    }


def final_attempt(
    *,
    repository: Path,
    python: Path,
    selected: Sequence[dict[str, Any]],
    attempt_number: int,
    evidence_root: Path,
    temporary_root: Path,
) -> dict[str, Any]:
    attempt: dict[str, Any] = {
        "attempt": attempt_number,
        "started_at_utc": utc_now(),
        "selected_task_ids": [candidate["task_id"] for candidate in selected],
        "task_records": [],
        "failed_task_ids": [],
    }
    temporary = Path(tempfile.mkdtemp(prefix=f"final-{attempt_number:03d}-", dir=temporary_root))
    try:
        shared_base = temporary / "shared-base"
        construction = construct_shared_base(
            repository=repository, selected=selected, destination=shared_base
        )
        attempt["shared_base_construction"] = construction

        base_arm_dir = temporary / "base-arm"
        shutil.copytree(shared_base, base_arm_dir)
        base_arm = pytest_arm(
            python=python,
            arm=base_arm_dir,
            evidence=evidence_root / f"final-attempt-{attempt_number:03d}" / "base",
        )
        attempt["base"] = compact_arm(base_arm)
        shutil.rmtree(base_arm_dir, ignore_errors=True)
        if not arm_is_green(base_arm):
            # Replace only the first selected candidate, then rebuild and
            # rerun the entire cohort. Batch rejection could skip the first
            # ordered cohort that actually satisfies the frozen rule.
            attempt["failed_task_ids"] = [attempt["selected_task_ids"][0]]
            attempt["failure_reason"] = "shared_base_not_green"
            return attempt

        shared_tree = construction["tree"]
        failed: list[str] = []
        for candidate in selected:
            task_id = candidate["task_id"]
            record: dict[str, Any] = {"task_id": task_id, "passed": False}
            patch, overlay = candidate_patches(repository, candidate)
            tests_only = temporary / f"{task_id}-tests"
            full = temporary / f"{task_id}-full"
            try:
                shutil.copytree(shared_base, tests_only)
                overlay_application = apply_patch(tests_only, overlay)
                record["test_patch_application"] = overlay_application
                if overlay_application["applied"]:
                    red_arm = pytest_arm(
                        python=python,
                        arm=tests_only,
                        evidence=(
                            evidence_root
                            / f"final-attempt-{attempt_number:03d}"
                            / task_id
                            / "tests-only-full-suite"
                        ),
                    )
                    red_arm["mapped_changed_failures"] = mapped_changed_failures(
                        red_arm.get("junit"), candidate["test_target_kinds"]
                    )
                    record["tests_only"] = compact_arm(red_arm)
                    red_ok = arm_is_qualifying_red(red_arm)
                else:
                    red_ok = False

                shutil.copytree(shared_base, full)
                full_application = apply_patch(full, patch)
                record["full_patch_application"] = full_application
                full_tree_ok = False
                if full_application["applied"]:
                    expected_full = cached_patch_sequence(repository, shared_tree, [(patch, False)])
                    if expected_full["applied"] and expected_full["tree"] is not None:
                        full_tree_ok, comparison = compare_directory_to_tree(
                            full, ls_tree(repository, str(expected_full["tree"]))
                        )
                        record["full_tree_check"] = {
                            "matches": full_tree_ok,
                            "expected_tree": expected_full["tree"],
                            **comparison,
                        }
                if full_application["applied"] and full_tree_ok:
                    green_arm = pytest_arm(
                        python=python,
                        arm=full,
                        evidence=(
                            evidence_root
                            / f"final-attempt-{attempt_number:03d}"
                            / task_id
                            / "full-full-suite"
                        ),
                    )
                    record["full"] = compact_arm(green_arm)
                    green_ok = arm_is_green(green_arm)
                else:
                    green_ok = False

                record["passed"] = red_ok and green_ok
                if not record["passed"]:
                    reasons: list[str] = []
                    if not red_ok:
                        reasons.append("final_test_overlay_not_qualifying_red")
                    if not green_ok:
                        reasons.append("final_full_patch_not_green")
                    record["failure_reasons"] = reasons
                    failed.append(task_id)
                attempt["task_records"].append(record)
                if failed:
                    # Replacement is strictly one-at-a-time in frozen ledger
                    # order. Continuing could reject a later task whose result
                    # was contingent on this already-failed cohort member and
                    # thereby skip the first ordered passing set.
                    break
            except Exception:
                # The outer apparatus handler records a fatal attempt. Do not
                # convert tooling/filesystem/JUnit failures into task outcomes.
                raise
            finally:
                shutil.rmtree(tests_only, ignore_errors=True)
                shutil.rmtree(full, ignore_errors=True)
        attempt["failed_task_ids"] = failed
        attempt["failure_reason"] = None if not failed else "one_or_more_task_arms_failed"
        return attempt
    except Exception as error:
        attempt["failure_reason"] = "final_construction_apparatus_exception"
        attempt["exception"] = f"{type(error).__name__}: {error}"
        attempt["fatal"] = True
        return attempt
    finally:
        attempt["completed_at_utc"] = utc_now()
        shutil.rmtree(temporary, ignore_errors=True)


def historical_environment_identity(python: Path) -> dict[str, Any]:
    code = (
        "import importlib.metadata as md,json,platform,sys,pytest;"
        "print(json.dumps({'implementation':platform.python_implementation(),"
        "'version':list(sys.version_info[:3]),'pytest':pytest.__version__,"
        "'distributions':sorted((d.metadata.get('Name','').lower().replace('_','-'),d.version)"
        " for d in md.distributions())},sort_keys=True))"
    )
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment.pop("PYTHONHOME", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = run(
        [str(python), "-c", code], cwd=PROJECT_ROOT, env=environment, check=True
    )
    identity = json.loads(decode(result.stdout).strip())
    if (
        identity.get("implementation") != "CPython"
        or identity.get("version") != [3, 11, 9]
        or identity.get("pytest") != "8.4.2"
    ):
        raise RuntimeError(f"historical environment does not match frozen identity: {identity}")
    installed = {name for name, _ in identity["distributions"]}
    unexpected = sorted(installed - ALLOWED_HISTORICAL_DISTRIBUTIONS)
    required = {"pytest", "pluggy", "packaging", "iniconfig", "pygments"}
    missing = sorted(required - installed)
    if unexpected or missing:
        raise RuntimeError(
            "historical environment is not pytest-only plus transitives: "
            f"unexpected={unexpected}, missing={missing}, identity={identity}"
        )
    return identity


def partial_verification(
    *,
    output: Path,
    ledger_sha256: str,
    screening: Sequence[dict[str, Any]],
    final_attempts: Sequence[dict[str, Any]],
) -> None:
    atomic_json(
        output.with_suffix(".partial.json"),
        {
            "schema_version": 1,
            "measurement": "pygments-causal-task-verification",
            "status": "running",
            "selection_rule_sha256": sha256_file(RULE_PATH),
            "candidate_ledger_sha256": ledger_sha256,
            "screening": list(screening),
            "final_attempts": list(final_attempts),
        },
    )


def verify(
    repository: Path,
    python: Path,
    ledger_path: Path,
    output: Path,
) -> dict[str, Any]:
    ledger_bytes = ledger_path.read_bytes()
    ledger_sha = sha256_bytes(ledger_bytes)
    ledger_digest_path = ledger_path.with_suffix(ledger_path.suffix + ".sha256")
    expected_digest_record = f"{ledger_sha}  {ledger_path.name}\n"
    if not ledger_digest_path.is_file():
        raise RuntimeError("candidate ledger digest sidecar is missing")
    if ledger_digest_path.read_text(encoding="ascii") != expected_digest_record:
        raise RuntimeError("candidate ledger digest sidecar does not match ledger bytes")
    ledger = json.loads(ledger_bytes)
    rule_sha = sha256_file(RULE_PATH)
    if ledger.get("selection_rule_sha256") != rule_sha:
        raise RuntimeError("selection rule changed after static ledger freeze")
    if ledger.get("head") != HEAD or ledger.get("head_tree") != revision_tree(repository, HEAD):
        raise RuntimeError("candidate ledger anchor does not match repository")
    actual_head = decode(git(repository, "rev-parse", "HEAD").stdout).strip()
    if actual_head != HEAD:
        raise RuntimeError(f"repository moved after ledger freeze: {actual_head}")
    python = python.resolve(strict=True)
    environment_identity = historical_environment_identity(python)

    candidates = ledger["candidates"]
    occupied: set[str] = set()
    for expected_order, candidate in enumerate(candidates, 1):
        if candidate["order"] != expected_order:
            raise RuntimeError("candidate ledger order is not contiguous")
        overlap = occupied & set(candidate["paths"])
        if overlap:
            raise RuntimeError(f"candidate ledger lost disjointness: {sorted(overlap)}")
        occupied.update(candidate["paths"])

    evidence_root = output.parent / f"{output.stem}-arms"
    temporary_root = Path(tempfile.mkdtemp(prefix="blast-radius-pygments-causal-"))
    screening: list[dict[str, Any]] = []
    final_attempts: list[dict[str, Any]] = []
    final_rejections: list[dict[str, Any]] = []
    started = utc_now()
    status = "exhausted"
    accepted_task_ids: list[str] = []
    fatal_exception: str | None = None
    try:
        for candidate in candidates:
            print(
                f"screening {candidate['order']}/{len(candidates)} "
                f"{candidate['task_id']} {candidate['sha'][:10]}",
                flush=True,
            )
            try:
                record = screen_candidate(
                    repository=repository,
                    python=python,
                    candidate=candidate,
                    evidence_root=evidence_root / "screening",
                    temporary_root=temporary_root,
                )
            except Exception as error:
                status = "apparatus_error"
                fatal_exception = (
                    f"screening {candidate['task_id']}: "
                    f"{type(error).__name__}: {error}"
                )
                break
            screening.append(record)
            partial_verification(
                output=output,
                ledger_sha256=ledger_sha,
                screening=screening,
                final_attempts=final_attempts,
            )
            print(
                f"  {'provisional' if record['screen_passed'] else 'reject'}: "
                f"{record.get('rejection_reason') or 'targeted red/green'}",
                flush=True,
            )

        provisional_ids = (
            [record["task_id"] for record in screening if record["screen_passed"]]
            if status != "apparatus_error"
            else []
        )
        by_id = {candidate["task_id"]: candidate for candidate in candidates}
        rejected_final: set[str] = set()
        attempt_number = 0
        while True:
            selected_ids = [
                task_id for task_id in provisional_ids if task_id not in rejected_final
            ][:TARGET_TASKS]
            if len(selected_ids) < TARGET_TASKS:
                break
            attempt_number += 1
            print(
                f"final shared-base attempt {attempt_number}: "
                + ", ".join(selected_ids),
                flush=True,
            )
            attempt = final_attempt(
                repository=repository,
                python=python,
                selected=[by_id[task_id] for task_id in selected_ids],
                attempt_number=attempt_number,
                evidence_root=evidence_root / "final",
                temporary_root=temporary_root,
            )
            final_attempts.append(attempt)
            partial_verification(
                output=output,
                ledger_sha256=ledger_sha,
                screening=screening,
                final_attempts=final_attempts,
            )
            if attempt.get("fatal"):
                status = "apparatus_error"
                fatal_exception = str(attempt.get("exception"))
                break
            failed = list(attempt["failed_task_ids"])
            if not failed:
                accepted_task_ids = selected_ids
                status = "complete"
                break
            for task_id in failed:
                if task_id not in rejected_final:
                    rejected_final.add(task_id)
                    task_record = next(
                        (
                            item
                            for item in attempt.get("task_records", [])
                            if item["task_id"] == task_id
                        ),
                        None,
                    )
                    final_rejections.append(
                        {
                            "task_id": task_id,
                            "attempt": attempt_number,
                            "reason": attempt["failure_reason"],
                            "task_failure_reasons": (
                                task_record.get("failure_reasons", []) if task_record else []
                            ),
                        }
                    )
            print(
                f"  rejected {len(failed)} final task(s); rebuilding with next provisional",
                flush=True,
            )
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)

    screen_counts = collections.Counter(
        record["rejection_reason"] or "provisional" for record in screening
    )
    successful_attempt = final_attempts[-1] if status == "complete" else None
    value = {
        "schema_version": 1,
        "measurement": "pygments-causal-task-verification",
        "status": status,
        "selection_rule": display_path(RULE_PATH),
        "selection_rule_sha256": rule_sha,
        "candidate_ledger": display_path(ledger_path),
        "candidate_ledger_sha256": ledger_sha,
        "repository": str(repository.resolve()),
        "head": HEAD,
        "head_tree": revision_tree(repository, HEAD),
        "historical_environment": environment_identity,
        "protocol": {
            "targeted_command": "python -m pytest --ignore=tests/contrast --junitxml=<outside-arm> <changed-targets>",
            "full_command": "python -m pytest --ignore=tests/contrast --junitxml=<outside-arm>",
            "timeout_seconds": PROCESS_TIMEOUT_SECONDS,
            "PYTHONPATH": "<arm>",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_ADDOPTS": "removed",
            "PYTEST_PLUGINS": "removed",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        "target": TARGET_TASKS,
        "started_at_utc": started,
        "completed_at_utc": utc_now(),
        "counts": {
            "screened": len(screening),
            "provisional": sum(record["screen_passed"] for record in screening),
            "screening_by_result": dict(sorted(screen_counts.items())),
            "final_attempts": len(final_attempts),
            "final_rejected_unique": len({item["task_id"] for item in final_rejections}),
            "accepted": len(accepted_task_ids),
        },
        "screening": screening,
        "final_rejections": final_rejections,
        "final_attempts": final_attempts,
        "accepted_task_ids": accepted_task_ids,
        "final_shared_base": (
            {
                "tree": successful_attempt["shared_base_construction"]["tree"],
                "selected_task_ids": accepted_task_ids,
                "verification": successful_attempt["base"],
            }
            if successful_attempt is not None
            else None
        ),
        "fatal_exception": fatal_exception,
    }
    atomic_json(output, value)
    write_digest(output)
    partial_path = output.with_suffix(".partial.json")
    if partial_path.exists():
        partial_path.unlink()
    print(json.dumps({"status": status, "counts": value["counts"]}, indent=2))
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
    return raw if raw.endswith(b"\n") else raw + b"\n"


def fixture_arm_evidence(arm: dict[str, Any]) -> dict[str, Any]:
    junit = arm.get("junit")
    return {
        "command": "python -m pytest --ignore=tests/contrast --junitxml=<outside-arm>",
        "targets": arm.get("targets", []),
        "elapsed_seconds": arm["elapsed_seconds"],
        "returncode": arm["returncode"],
        "timed_out": arm["timed_out"],
        "tracked_content_before": arm["tracked_content_before"],
        "tracked_content_unchanged_after": arm["tracked_content_unchanged_after"],
        "junit": (
            {
                "case_count": junit["case_count"],
                "counts": junit["counts"],
                "cases_sha256": junit["cases_sha256"],
            }
            if junit is not None
            else None
        ),
        "mapped_changed_failures": arm.get("mapped_changed_failures", []),
    }


def emit_fixture(
    repository: Path,
    ledger_path: Path,
    results_path: Path,
    destination: Path,
) -> dict[str, Any]:
    ledger_bytes = ledger_path.read_bytes()
    ledger = json.loads(ledger_bytes)
    results = json.loads(results_path.read_text(encoding="utf-8"))
    rule_sha = sha256_file(RULE_PATH)
    if ledger.get("selection_rule_sha256") != rule_sha:
        raise RuntimeError("selection rule changed after ledger freeze")
    if results.get("selection_rule_sha256") != rule_sha:
        raise RuntimeError("results use a different selection rule")
    if results.get("candidate_ledger_sha256") != sha256_bytes(ledger_bytes):
        raise RuntimeError("results do not match candidate ledger")
    if results.get("status") != "complete" or results["counts"]["accepted"] != TARGET_TASKS:
        raise RuntimeError("refusing to emit without exactly 30 final accepted tasks")
    if destination.exists():
        raise RuntimeError(f"fixture destination already exists: {destination}")

    by_id = {candidate["task_id"]: candidate for candidate in ledger["candidates"]}
    task_ids = results["accepted_task_ids"]
    selected = [by_id[task_id] for task_id in task_ids]
    successful_attempt = results["final_attempts"][-1]
    final_records = {record["task_id"]: record for record in successful_attempt["task_records"]}
    if any(not final_records[task_id]["passed"] for task_id in task_ids):
        raise RuntimeError("successful final attempt contains a failed task")

    # `git apply` discovers a containing repository.  Building below
    # PROJECT_ROOT would make it interpret patch paths in the parent worktree
    # instead of in the plain-tree fixture.  Stage outside the repository,
    # verify there, then move the completed directory into place.
    temporary_root = Path(tempfile.mkdtemp(prefix="blast-radius-pygments-emit-"))
    staging = temporary_root / destination.name
    staging.mkdir(parents=True)
    (staging / "patches").mkdir()
    (staging / "history").mkdir()
    try:
        base_name = "base-shared"
        construction = construct_shared_base(
            repository=repository,
            selected=selected,
            destination=staging / base_name,
        )
        if construction["tree"] != results["final_shared_base"]["tree"]:
            raise RuntimeError("emitted shared base tree differs from verified tree")

        manifest_tasks: list[dict[str, Any]] = []
        for candidate in selected:
            task_id = candidate["task_id"]
            patch, overlay = candidate_patches(repository, candidate)
            patch_name = f"patches/{task_id}.patch"
            overlay_name = f"patches/{task_id}.tests.patch"
            (staging / patch_name).write_bytes(patch)
            (staging / overlay_name).write_bytes(overlay)
            record = final_records[task_id]
            manifest_tasks.append(
                {
                    "order": candidate["order"],
                    "task_id": task_id,
                    "pr": candidate["pr"],
                    "pr_url": candidate["pr_url"],
                    "sha": candidate["sha"],
                    "parent": candidate["parent"],
                    "timestamp": candidate["timestamp"],
                    "committed_at": candidate["committed_at"],
                    "subject": candidate["subject"],
                    "paths": candidate["paths"],
                    "source_python_paths": candidate["source_python_paths"],
                    "test_paths": candidate["test_paths"],
                    "test_targets": candidate["test_targets"],
                    "base": base_name,
                    "full_patch": patch_name,
                    "test_patch": overlay_name,
                    "full_patch_bytes": candidate["full_patch_bytes"],
                    "full_patch_sha256": candidate["full_patch_sha256"],
                    "test_patch_bytes": candidate["test_patch_bytes"],
                    "test_patch_sha256": candidate["test_patch_sha256"],
                    "verification": {
                        "tests_only": fixture_arm_evidence(record["tests_only"]),
                        "full": fixture_arm_evidence(record["full"]),
                        "full_tree_check": record["full_tree_check"],
                    },
                }
            )

        (staging / "history" / "HEAD.txt").write_text(
            HEAD + "\n", encoding="ascii", newline="\n"
        )
        stream = deterministic_commit_stream(repository, HEAD)
        with (staging / "history" / "commit-stream.txt.gz").open("wb") as raw_handle:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw_handle, mtime=0) as compressed:
                compressed.write(stream)
        manifest = {
            "schema_version": 1,
            "measurement": "second-repository-causal-fixture",
            "repository": "pygments/pygments",
            "repository_url": "https://github.com/pygments/pygments.git",
            "license": "BSD-2-Clause",
            "head": HEAD,
            "head_tree": ledger["head_tree"],
            "selection_rule_sha256": rule_sha,
            "candidate_ledger_sha256": sha256_bytes(ledger_bytes),
            "verification_results_sha256": sha256_file(results_path),
            "suite": {
                "command": "python -m pytest --ignore=tests/contrast",
                "disclosed_exclusion": "tests/contrast requires wcag-contrast-ratio and is excluded for pytest-only no-egress execution",
                "historical_environment": results["historical_environment"],
                "environment": {
                    "PYTHONPATH": "<arm>",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PYTEST_ADDOPTS": "removed",
                    "PYTEST_PLUGINS": "removed",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                },
            },
            "patch_semantics": {
                "pr-N.patch": "reduced first-parent pygments/ plus tests/ diff, including test hunks",
                "pr-N.tests.patch": "exact tests/ subset of the reduced diff",
            },
            "base": {
                "path": base_name,
                "tree": construction["tree"],
                "construction": "anchor with exactly the 30 listed reduced full patches reversed",
                "verification": fixture_arm_evidence(results["final_shared_base"]["verification"]),
            },
            "task_count": len(manifest_tasks),
            "tasks": manifest_tasks,
        }
        atomic_json(staging / "TASKS.json", manifest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)

    summary = {
        "destination": str(destination),
        "tasks": len(task_ids),
        "bases": 1,
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
