#!/usr/bin/env python3
"""Prepare and validate the frozen Python arms-ladder conflict sites.

The two commands are deliberately separated. ``prepare`` makes a physical,
per-worker copy of the canonical bare mirror *before the first Git command*,
allows only that owned copy to hydrate, and emits exact side/joint patches.
``run`` consumes a prepared worker copy and evaluates an index batch.  It never
opens the canonical mirror with Git.

Candidate pytest is executed only by ``run``.  Importing this module, asking
for help, and ``prepare`` do not execute pytest.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = PROJECT_ROOT / "exploratory" / "arms" / "protocol.json"
PROTOCOL_SHA256 = "fb1c0a9f9c7b48c30178d8a7e737250e18535ada63959c49c73c180505c69828"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "exploratory" / "arms"
DEFAULT_SCRATCH_ROOT = PROJECT_ROOT.parent / f"{PROJECT_ROOT.name}-arms-scratch"
WORKER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CONVENTIONAL_PYTEST_RE = re.compile(r"^test.*\.py$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SUMMARY_TOKEN_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b",
    re.IGNORECASE,
)
COLLECTION_ERROR_RE = re.compile(
    r"ERROR collecting|errors? during collection|found no collectors", re.IGNORECASE
)
IMPORT_ERROR_RE = re.compile(r"ModuleNotFoundError|ImportError|No module named", re.IGNORECASE)
WINDOWS_CHILD_PATH_BUDGET = 240
WINDOWS_TEMP_PATH_BUDGET = 160


@dataclass(frozen=True)
class RepoSpec:
    key: str
    slug: str
    repository: str
    expected_sites: int
    package: str
    pytest_prefix: tuple[str, ...] = ()

    @property
    def corpus(self) -> Path:
        return PROJECT_ROOT / "corpus" / "conflicts" / f"{self.slug}.jsonl"

    @property
    def canonical_mirror(self) -> Path:
        return PROJECT_ROOT / "corpus" / "_conflict_mirrors" / self.slug


REPOSITORIES = {
    "click": RepoSpec("click", "pallets__click", "pallets/click", 24, "click"),
    "pallets__click": RepoSpec("click", "pallets__click", "pallets/click", 24, "click"),
    "pygments": RepoSpec(
        "pygments", "pygments__pygments", "pygments/pygments", 2, "pygments",
        ("--ignore=tests/contrast",),
    ),
    "pygments__pygments": RepoSpec(
        "pygments", "pygments__pygments", "pygments/pygments", 2, "pygments",
        ("--ignore=tests/contrast",),
    ),
}


class RunnerError(RuntimeError):
    """Fail-closed apparatus or frozen-protocol error."""


class ApparatusError(RunnerError):
    """An execution-environment failure that cannot become a site verdict."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(path: Path) -> str:
    if path.is_file():
        return sha256_file(path)
    rows = [
        {"path": item.relative_to(path).as_posix(), "sha256": sha256_file(item)}
        # Match the frozen semantic harness exactly: pathlib's component-wise
        # Path ordering, not flat-string ordering (which orders '-' vs '/'
        # differently and produces a different aggregate fingerprint).
        for item in sorted(p for p in path.rglob("*") if p.is_file())
    ]
    return sha256_bytes(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("ascii"))


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8"),
    )


def write_once(path: Path, value: bytes) -> None:
    if path.exists():
        if path.read_bytes() != value:
            raise RunnerError(f"refusing to replace differing artifact: {path}")
        return
    atomic_bytes(path, value)


def load_protocol() -> tuple[dict[str, Any], str]:
    payload = PROTOCOL_PATH.read_bytes()
    observed = sha256_bytes(payload)
    if observed != PROTOCOL_SHA256:
        raise RunnerError(f"protocol hash mismatch: expected {PROTOCOL_SHA256}, observed {observed}")
    protocol = json.loads(payload)
    if protocol.get("schema_version") != 1 or protocol.get("frozen_before_candidate_pytest") is not True:
        raise RunnerError("protocol is not the frozen schema-1 candidate protocol")
    return protocol, observed


def load_sites(spec: RepoSpec, protocol: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with spec.corpus.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            row = json.loads(line)
            if row.get("evaluation_status") == "conflicted" and row.get("both_sides_touched_tests") is True:
                row["corpus_line"] = line_number
                rows.append(row)
    frozen_count = int(protocol["population"][spec.repository])
    if len(rows) != spec.expected_sites or len(rows) != frozen_count:
        raise RunnerError(
            f"{spec.repository}: expected {spec.expected_sites} sites, observed {len(rows)}"
        )
    return rows


def select_batch(rows: Sequence[dict[str, Any]], start: int, stop: int | None) -> list[tuple[int, dict[str, Any]]]:
    actual_stop = len(rows) if stop is None else stop
    if start < 0 or actual_stop < start or actual_stop > len(rows):
        raise RunnerError(f"invalid half-open index batch [{start}, {actual_stop}) for {len(rows)} rows")
    return list(enumerate(rows[start:actual_stop], start=start))


def filesystem_manifest(root: Path) -> dict[str, Any]:
    entries: list[tuple[str, int, str]] = []
    total = 0
    for path in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
        size = path.stat().st_size
        total += size
        entries.append((path.relative_to(root).as_posix(), size, sha256_file(path)))
    canonical = json.dumps(entries, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return {"file_count": len(entries), "logical_bytes": total, "manifest_sha256": sha256_bytes(canonical)}


def worker_paths(spec: RepoSpec, worker_id: str, scratch_root: Path) -> dict[str, Path]:
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if (
        not WORKER_RE.fullmatch(worker_id)
        or len(worker_id) > 64
        or worker_id in {".", ".."}
        or worker_id.endswith((".", " "))
        or worker_id.upper().split(".", 1)[0] in reserved
    ):
        raise RunnerError("worker id must contain only letters, digits, dot, underscore, or hyphen")
    approved = DEFAULT_SCRATCH_ROOT.resolve()
    if scratch_root.resolve() != approved:
        raise RunnerError(f"scratch root is not the frozen task root: {scratch_root}")
    repo_root = (approved / spec.slug).resolve()
    if repo_root.parent != approved:
        raise RunnerError(f"repository scratch child is redirected outside the frozen root: {repo_root}")
    root = (repo_root / worker_id).resolve()
    if root.parent != repo_root:
        raise RunnerError(f"worker path escapes repository scratch root: {root}")
    result = {
        "root": root,
        "mirror": root / "mirror.git",
        "copy_partial": root / "mirror.git.copying",
        "worktrees": root / "worktrees",
        "indexes": root / "indexes",
        "marker": root / "worker.json",
    }
    for name, child in result.items():
        if name == "root":
            continue
        resolved = child.resolve()
        if resolved.parent != root:
            raise RunnerError(f"worker child {name} is redirected outside its owned root: {resolved}")
    return result


def prepare_physical_copy(spec: RepoSpec, worker_id: str, scratch_root: Path, protocol_sha: str) -> dict[str, Path]:
    """Copy first.  This function intentionally invokes no Git command."""
    paths = worker_paths(spec, worker_id, scratch_root)
    marker = paths["marker"]
    if marker.is_file():
        value = json.loads(marker.read_text(encoding="utf-8"))
        expected = {"repository": spec.repository, "worker_id": worker_id, "protocol_sha256": protocol_sha}
        if any(value.get(key) != expected_value for key, expected_value in expected.items()):
            raise RunnerError(f"worker marker mismatch: {marker}")
        if not paths["mirror"].is_dir():
            raise RunnerError(f"worker marker exists without physical mirror: {paths['mirror']}")
        return paths
    if paths["mirror"].exists() or paths["copy_partial"].exists():
        raise RunnerError(f"unclaimed or incomplete worker copy exists under {paths['root']}")
    if not spec.canonical_mirror.is_dir():
        raise RunnerError(f"canonical bare mirror is absent: {spec.canonical_mirror}")
    paths["root"].mkdir(parents=True, exist_ok=True)
    rechecked = worker_paths(spec, worker_id, scratch_root)
    if any(rechecked[name].resolve() != path.resolve() for name, path in paths.items()):
        raise RunnerError("worker hierarchy changed while its owned root was being created")
    before = filesystem_manifest(spec.canonical_mirror)
    shutil.copytree(spec.canonical_mirror, paths["copy_partial"], copy_function=shutil.copy2)
    copied = filesystem_manifest(paths["copy_partial"])
    after = filesystem_manifest(spec.canonical_mirror)
    if before != after or copied != after:
        raise RunnerError("canonical mirror changed during copy or physical copy differs")
    paths["copy_partial"].rename(paths["mirror"])
    paths["worktrees"].mkdir()
    paths["indexes"].mkdir()
    atomic_json(
        marker,
        {
            "schema_version": SCHEMA_VERSION,
            "owner": Path(__file__).name,
            "repository": spec.repository,
            "worker_id": worker_id,
            "protocol_sha256": protocol_sha,
            "canonical_mirror": str(spec.canonical_mirror.resolve()),
            "owned_mirror": str(paths["mirror"].resolve()),
            "physical_copy": True,
            "canonical_snapshot_before": before,
            "canonical_snapshot_after": after,
            "owned_snapshot_before_git": copied,
            "copied_at_utc": utc_now(),
        },
    )
    return paths


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    timed_out: bool
    launch_error: str | None = None


def process_environment(*, allow_git_hydration: bool) -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("GIT_"):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    if not allow_git_hydration:
        environment["GIT_NO_LAZY_FETCH"] = "1"
    return environment


def run_process(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], input_bytes: bytes | None = None,
    timeout: float = 120.0,
) -> ProcessResult:
    started = time.monotonic()
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(
            list(argv), cwd=cwd, env=dict(env), stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **options,
        )
    except OSError as error:
        return ProcessResult(tuple(map(str, argv)), None, b"", str(error).encode(), time.monotonic() - started, False, repr(error))
    try:
        stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
        return ProcessResult(tuple(map(str, argv)), process.returncode, stdout, stderr, time.monotonic() - started, False)
    except subprocess.TimeoutExpired as error:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return ProcessResult(tuple(map(str, argv)), None, stdout or error.stdout or b"", stderr or error.stderr or b"", time.monotonic() - started, True)


def run_git(
    owned_mirror: Path, arguments: Sequence[str], *, allow_hydration: bool, check: bool = True,
    input_bytes: bytes | None = None, extra_env: Mapping[str, str] | None = None,
) -> ProcessResult:
    resolved = owned_mirror.resolve()
    protected_roots = [
        (PROJECT_ROOT / "corpus" / "_conflict_mirrors").resolve(),
        (PROJECT_ROOT / "corpus" / "_clones").resolve(),
    ]
    if any(resolved == root or root in resolved.parents for root in protected_roots):
        raise RunnerError(f"refusing Git operation in protected corpus source: {resolved}")
    env = process_environment(allow_git_hydration=allow_hydration)
    if extra_env:
        env.update(extra_env)
    result = run_process(
        [
            "git", "--git-dir", str(resolved), "-c", "core.autocrlf=false",
            "-c", f"core.attributesFile={os.devnull}", "-c", f"core.hooksPath={os.devnull}",
            *arguments,
        ],
        cwd=PROJECT_ROOT, env=env, input_bytes=input_bytes, timeout=300.0,
    )
    if check and (result.timed_out or result.returncode != 0):
        raise RunnerError(
            f"git {' '.join(arguments)} failed ({result.returncode}): {result.stderr.decode(errors='replace')[-2000:]}"
        )
    return result


def git_text(mirror: Path, arguments: Sequence[str], *, hydrate: bool = False, extra_env: Mapping[str, str] | None = None) -> str:
    return run_git(mirror, arguments, allow_hydration=hydrate, extra_env=extra_env).stdout.decode("utf-8", errors="replace").strip()


def decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def changed_paths(mirror: Path, base: str, parent: str) -> list[str]:
    payload = run_git(
        mirror,
        [
            "--literal-pathspecs", "diff", "--name-only", "-z", "--no-renames",
            "--no-ext-diff", "--no-textconv", base, parent, "--",
        ],
        allow_hydration=True,
    ).stdout
    return sorted({decode_path(item) for item in payload.split(b"\0") if item})


def path_chunks(paths: Sequence[str], budget: int = 12000) -> Iterator[list[str]]:
    chunk: list[str] = []
    length = 0
    for path in paths:
        cost = len(path) + 3
        if chunk and length + cost > budget:
            yield chunk
            chunk, length = [], 0
        chunk.append(path)
        length += cost
    if chunk:
        yield chunk


def diff_paths(mirror: Path, base: str, target: str, paths: Sequence[str]) -> bytes:
    output = bytearray()
    for chunk in path_chunks(sorted(paths)):
        output.extend(
            run_git(
                mirror,
                [
                    "--literal-pathspecs", "diff", "--binary", "--full-index", "--no-renames",
                    "--no-ext-diff", "--no-textconv", base, target, "--", *chunk,
                ],
                allow_hydration=True,
            ).stdout
        )
    return bytes(output)


def verify_index_patch_union(
    mirror: Path, indexes: Path, label: str, base: str, parent: str, patches: Sequence[bytes]
) -> str:
    index = indexes / f"{label}-{time.time_ns()}.index"
    environment = {"GIT_INDEX_FILE": str(index)}
    try:
        git_text(mirror, ["read-tree", base], extra_env=environment)
        for patch in patches:
            if patch:
                run_git(
                    mirror, ["apply", "--cached", "--whitespace=nowarn", "-"], allow_hydration=False,
                    input_bytes=patch, extra_env=environment,
                )
        actual = git_text(mirror, ["write-tree"], extra_env=environment)
        expected = git_text(mirror, ["rev-parse", f"{parent}^{{tree}}"])
        if actual != expected:
            raise RunnerError(f"patch union for {label} yielded {actual}, expected {expected}")
        return actual
    finally:
        with contextlib.suppress(FileNotFoundError):
            index.unlink()


def verify_index_patch_paths(
    mirror: Path, indexes: Path, label: str, base: str, target: str,
    patch: bytes, paths: Sequence[str],
) -> dict[str, Any]:
    """Prove a restricted patch gives the target tree's exact modes/OIDs on its path set."""
    index = indexes / f"{label}-{time.time_ns()}.index"
    environment = {"GIT_INDEX_FILE": str(index)}
    try:
        git_text(mirror, ["read-tree", base], extra_env=environment)
        if patch:
            run_git(
                mirror, ["apply", "--cached", "--whitespace=nowarn", "-"],
                allow_hydration=False, input_bytes=patch, extra_env=environment,
            )
        actual_tree = git_text(mirror, ["write-tree"], extra_env=environment)
        expected_changed_payload = bytearray()
        for chunk in path_chunks(sorted(paths)):
            expected_changed_payload.extend(
                run_git(
                    mirror,
                    [
                        "--literal-pathspecs", "diff", "--name-only", "-z", "--no-renames",
                        "--no-ext-diff", "--no-textconv", base, target, "--", *chunk,
                    ],
                    allow_hydration=False,
                ).stdout
            )
        expected_changed_paths = sorted(
            {decode_path(item) for item in bytes(expected_changed_payload).split(b"\0") if item}
        )
        actual_changed_payload = run_git(
            mirror,
            [
                "--literal-pathspecs", "diff", "--name-only", "-z", "--no-renames",
                "--no-ext-diff", "--no-textconv", base, actual_tree, "--",
            ],
            allow_hydration=False,
        ).stdout
        actual_changed_paths = sorted(
            {decode_path(item) for item in actual_changed_payload.split(b"\0") if item}
        )
        outside_declared_paths = sorted(set(actual_changed_paths) - set(paths))
        if actual_changed_paths != expected_changed_paths or outside_declared_paths:
            raise RunnerError(
                f"restricted patch for {label} changed the wrong path set: "
                f"expected={expected_changed_paths}, actual={actual_changed_paths}, "
                f"outside_declared={outside_declared_paths}"
            )
        actual_entries = bytearray()
        target_entries = bytearray()
        for chunk in path_chunks(sorted(paths)):
            actual_entries.extend(
                run_git(
                    mirror,
                    ["--literal-pathspecs", "ls-tree", "-r", "-z", "--full-tree", actual_tree, "--", *chunk],
                    allow_hydration=False,
                ).stdout
            )
            target_entries.extend(
                run_git(
                    mirror,
                    ["--literal-pathspecs", "ls-tree", "-r", "-z", "--full-tree", target, "--", *chunk],
                    allow_hydration=False,
                ).stdout
            )
        if actual_entries != target_entries:
            raise RunnerError(f"restricted patch for {label} did not reproduce target path modes/OIDs")
        return {
            "verified": True,
            "base": base,
            "target": target,
            "actual_tree": actual_tree,
            "path_count": len(set(paths)),
            "declared_paths": sorted(set(paths)),
            "expected_changed_paths": expected_changed_paths,
            "actual_changed_paths": actual_changed_paths,
            "outside_declared_paths": outside_declared_paths,
            "changed_paths_match_exactly": True,
            "no_outside_path_changed": True,
            "actual_entries_sha256": sha256_bytes(bytes(actual_entries)),
            "target_entries_sha256": sha256_bytes(bytes(target_entries)),
            "modes_and_oids_match": True,
        }
    finally:
        with contextlib.suppress(FileNotFoundError):
            index.unlink()


MERGE_TREE_CONFIG = [
    "-c", "advice.submoduleMergeConflict=false", "-c", "core.quotePath=true",
    "-c", "merge.conflictStyle=merge", "-c", "merge.renormalize=false",
    "-c", "merge.renames=true", "-c", "merge.directoryRenames=conflict",
    "-c", "merge.renameLimit=7000", "-c", "diff.renames=false", "-c", "diff.algorithm=myers",
]


def canonical_merge_tree(mirror: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    result = run_git(
        mirror,
        [*MERGE_TREE_CONFIG, "merge-tree", "--write-tree", "-z", "--messages", "-Xfind-renames=50%", *row["parents"]],
        allow_hydration=True, check=False,
    )
    fields = result.stdout.split(b"\0")
    tree = fields[0].decode("ascii", errors="replace") if fields and fields[0] else None
    observed_sha = sha256_bytes(result.stdout)
    if result.returncode != 1 or result.stderr or tree != row["result_tree"]:
        raise RunnerError(
            f"canonical merge-tree mismatch for {row['merge']}: exit={result.returncode}, tree={tree}"
        )
    if observed_sha != row["merge_tree_output_sha256"]:
        raise RunnerError(
            f"canonical merge-tree byte hash mismatch for {row['merge']}: {observed_sha}"
        )
    return {
        "exit_code": result.returncode,
        "result_tree": tree,
        "stdout_sha256": observed_sha,
        "stderr_sha256": sha256_bytes(result.stderr),
        "matches_canonical_row": True,
    }


def materialize_base(mirror: Path, worktrees: Path, base: str) -> None:
    destination = worktrees / f"prepare-{base[:12]}-{time.time_ns()}"
    run_git(
        mirror, ["worktree", "add", "--detach", "--force", str(destination), base],
        allow_hydration=True,
    )
    try:
        probe = run_process(
            ["git", "-C", str(destination), "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            env=process_environment(allow_git_hydration=False),
            timeout=60.0,
        )
        if probe.returncode != 0 or probe.timed_out:
            raise RunnerError(
                "base worktree identity probe failed: "
                + probe.stderr.decode("utf-8", errors="replace")[-2000:]
            )
        actual = probe.stdout.decode("ascii", errors="replace").strip()
        if actual != base:
            raise RunnerError(f"base materialization mismatch: {actual} != {base}")
    finally:
        run_git(mirror, ["worktree", "remove", "--force", str(destination)], allow_hydration=False)
        run_git(mirror, ["worktree", "prune"], allow_hydration=False)


def patch_record(path: Path, payload: bytes) -> dict[str, Any]:
    return {"path": path.relative_to(PROJECT_ROOT).as_posix(), "sha256": sha256_bytes(payload), "bytes": len(payload)}


def prepare_site(
    spec: RepoSpec, index: int, row: Mapping[str, Any], paths: Mapping[str, Path], output_root: Path,
    protocol_sha: str, runner_sha: str,
) -> dict[str, Any]:
    mirror = paths["mirror"]
    materialize_base(mirror, paths["worktrees"], row["merge_base"])
    merge_tree = canonical_merge_tree(mirror, row)
    verify_direct_hierarchy(output_root, ["patches", spec.slug, row["merge"]], "patch output")
    patch_root = output_root / "patches" / spec.slug / row["merge"]
    sides: dict[str, Any] = {}
    source_union: set[str] = set()
    for side_number, side_name in enumerate(("parent1", "parent2")):
        parent = row["parents"][side_number]
        changed = changed_paths(mirror, row["merge_base"], parent)
        tests = sorted(set(row["diffs"][side_name]["test_files"]))
        if not set(tests).issubset(changed):
            raise RunnerError(f"{row['merge']} {side_name}: canonical test files are not a diff subset")
        sources = sorted(set(changed) - set(tests))
        if not tests or not sources:
            raise RunnerError(f"{row['merge']} {side_name}: empty source/test split")
        source_union.update(sources)
        source_payload = diff_paths(mirror, row["merge_base"], parent, sources)
        test_payload = diff_paths(mirror, row["merge_base"], parent, tests)
        source_path = patch_root / f"{side_name}-source.patch"
        test_path = patch_root / f"{side_name}-test.patch"
        write_once(source_path, source_payload)
        write_once(test_path, test_payload)
        reconstructed = verify_index_patch_union(
            mirror, paths["indexes"], f"{row['merge']}-{side_name}", row["merge_base"], parent,
            [source_payload, test_payload],
        )
        sides[side_name] = {
            "parent": parent,
            "changed_paths": changed,
            "test_paths": tests,
            "source_paths": sources,
            "source_patch": patch_record(source_path, source_payload),
            "test_patch": patch_record(test_path, test_payload),
            "reconstructed_parent_tree": reconstructed,
        }
    source_conflicts = sorted(set(row["conflicted_paths"]) & source_union)
    joint: dict[str, Any] = {"source_paths": sorted(source_union), "source_conflict_paths": source_conflicts}
    if source_conflicts:
        joint.update({"constructible": False, "reason": "canonical textual conflict intersects source union", "patch": None})
    else:
        payload = diff_paths(mirror, row["merge_base"], row["result_tree"], sorted(source_union))
        verification = verify_index_patch_paths(
            mirror, paths["indexes"], f"{row['merge']}-joint-source",
            row["merge_base"], row["result_tree"], payload, sorted(source_union),
        )
        target = patch_root / "joint-source.patch"
        write_once(target, payload)
        joint.update(
            {
                "constructible": True,
                "reason": None,
                "patch": patch_record(target, payload),
                "index_verification": verification,
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "protocol_sha256": protocol_sha,
        "runner_sha256": runner_sha,
        "repository": spec.repository,
        "repo_slug": spec.slug,
        "site_index": index,
        "corpus_line": row["corpus_line"],
        "merge": row["merge"],
        "base": row["merge_base"],
        "parents": row["parents"],
        "result_tree": row["result_tree"],
        "conflicted_paths": row["conflicted_paths"],
        "canonical_merge_tree": merge_tree,
        "sides": sides,
        "joint_source": joint,
        "prepared_at_utc": utc_now(),
    }
    atomic_json(patch_root / "manifest.json", manifest)
    return manifest


def normalized_junit(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    cases: list[dict[str, str]] = []
    for case in root.iter("testcase"):
        outcome = "passed"
        detail_type = ""
        detail_message = ""
        for tag in ("failure", "error", "skipped"):
            detail = case.find(tag)
            if detail is not None:
                outcome = tag
                detail_type = detail.attrib.get("type", "")
                detail_message = detail.attrib.get("message", "")
                break
        identity = f"{case.attrib.get('classname', '')}::{case.attrib.get('name', '')}"
        cases.append(
            {
                "identity": identity,
                "outcome": outcome,
                "detail_type": detail_type,
                "detail_message": detail_message,
            }
        )
    cases.sort(
        key=lambda item: (
            item["identity"], item["outcome"], item["detail_type"], item["detail_message"],
        )
    )
    counts = {key: 0 for key in ("passed", "failure", "error", "skipped")}
    for case in cases:
        counts[case["outcome"]] += 1
    canonical = json.dumps(cases, sort_keys=True, separators=(",", ":")).encode("ascii")
    return {
        "case_count": len(cases),
        "counts": counts,
        "cases": cases,
        "case_outcome_signature_sha256": sha256_bytes(canonical),
        "failing_identities": sorted(case["identity"] for case in cases if case["outcome"] in {"failure", "error"}),
        "passed_identities": sorted(case["identity"] for case in cases if case["outcome"] == "passed"),
    }


def summary_counts(payload: str) -> dict[str, int]:
    lines = [line for line in ANSI_RE.sub("", payload).splitlines() if SUMMARY_TOKEN_RE.search(line)]
    if not lines:
        return {}
    result: dict[str, int] = {}
    for match in SUMMARY_TOKEN_RE.finditer(lines[-1]):
        key = match.group("kind").lower()
        key = "error" if key in {"error", "errors"} else "warning" if key in {"warning", "warnings"} else key
        result[key] = int(match.group("count"))
    return result


def child_path_budget(paths: Mapping[str, Path]) -> dict[str, Any]:
    lengths = {label: len(str(path.resolve())) for label, path in paths.items()}
    longest = max(lengths.values(), default=0)
    record = {
        "policy": "windows-child-path-budget-v1",
        "limit": WINDOWS_CHILD_PATH_BUDGET,
        "lengths": lengths,
        "longest": longest,
        "ok": sys.platform != "win32" or longest <= WINDOWS_CHILD_PATH_BUDGET,
    }
    if not record["ok"]:
        raise ApparatusError(
            f"child-facing path exceeds the frozen Windows budget of "
            f"{WINDOWS_CHILD_PATH_BUDGET} characters: {lengths}"
        )
    return record


@contextlib.contextmanager
def short_attempt_temp(
    worktree: Path, attempt_root: Path,
) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Create, attest, and remove one isolated short execution-temp root."""
    worktree_root = worktree.resolve().parent
    scratch = DEFAULT_SCRATCH_ROOT.resolve()
    if len(worktree_root.parents) < 3 or worktree_root.parents[2] != scratch:
        raise ApparatusError(f"attempt worktree is outside the frozen scratch hierarchy: {worktree}")
    worker_root = worktree_root.parent
    temp_parent = worker_root / "t"
    temp_parent.mkdir(parents=False, exist_ok=True)
    resolved_parent = temp_parent.resolve()
    if resolved_parent.parent != worker_root or os.path.normcase(str(resolved_parent)) != os.path.normcase(
        str(temp_parent.absolute())
    ):
        raise ApparatusError(f"worker temporary root is redirected: {temp_parent} -> {resolved_parent}")
    token = sha256_bytes(str(attempt_root.resolve()).encode("utf-8"))[:16]
    temporary = temp_parent / token
    if temporary.exists() or temporary.is_symlink():
        raise ApparatusError(f"attempt temporary root is not fresh: {temporary}")
    if sys.platform == "win32" and len(str(temporary.absolute())) > WINDOWS_TEMP_PATH_BUDGET:
        raise ApparatusError(
            f"attempt temporary root exceeds the frozen Windows budget of "
            f"{WINDOWS_TEMP_PATH_BUDGET} characters: {temporary}"
        )
    temporary.mkdir(parents=False, exist_ok=False)
    if temporary.resolve().parent != resolved_parent or any(temporary.iterdir()):
        raise ApparatusError(f"attempt temporary root failed its empty/containment gate: {temporary}")
    record: dict[str, Any] = {
        "policy": "worker-short-isolated-temp-v1",
        "path": str(temporary.resolve()),
        "path_length": len(str(temporary.resolve())),
        "path_budget": WINDOWS_TEMP_PATH_BUDGET,
        "token": token,
        "attempt_root_sha256": sha256_bytes(str(attempt_root.resolve()).encode("utf-8")),
        "initially_empty": True,
        "present_before_cleanup": None,
        "manifest_before_cleanup": None,
        "cleanup_ok": None,
        "cleanup_error": None,
        "body_error": None,
    }
    try:
        yield temporary.resolve(), record
    except BaseException as error:
        record["body_error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        cleanup_error: str | None = None
        try:
            record["present_before_cleanup"] = temporary.exists()
            if temporary.exists():
                resolved_temporary = temporary.resolve()
                if (
                    temporary.is_symlink()
                    or not temporary.is_dir()
                    or resolved_temporary.parent != resolved_parent
                    or os.path.normcase(str(resolved_temporary)) != os.path.normcase(str(temporary.absolute()))
                ):
                    raise RuntimeError(
                        f"refusing cleanup of redirected/non-directory temporary root: "
                        f"{temporary} -> {resolved_temporary}"
                    )
                record["manifest_before_cleanup"] = filesystem_manifest(temporary)
                shutil.rmtree(temporary)
            else:
                record["manifest_before_cleanup"] = {
                    "file_count": 0,
                    "logical_bytes": 0,
                    "manifest_sha256": sha256_bytes(b"[]"),
                }
            if temporary.exists() or temporary.is_symlink():
                raise RuntimeError(f"temporary root survived cleanup: {temporary}")
        except Exception as error:
            cleanup_error = f"{type(error).__name__}: {error}"
        record["cleanup_error"] = cleanup_error
        record["cleanup_ok"] = cleanup_error is None
        atomic_json(attempt_root / "temp-evidence.json", record)
        if cleanup_error is not None:
            raise ApparatusError(f"attempt temporary-root cleanup failed: {cleanup_error}")


@contextlib.contextmanager
def suite_environment(
    worktree: Path, spec: RepoSpec, protocol: Mapping[str, Any], *, attempt_root: Path | None = None,
) -> Iterator[tuple[dict[str, str], Path, dict[str, Any] | None]]:
    source_root = worktree / "src" if (worktree / "src").is_dir() else worktree
    python_paths = [source_root]
    if (source_root / "sitecustomize.py").exists():
        raise RunnerError("candidate tree shadows the frozen Python startup policy")
    if spec.key == "click":
        compat = PROJECT_ROOT / protocol["environment"]["click_compat_root"]
        python_paths.append(compat)
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("GIT_"):
            environment.pop(key, None)
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG_TEMPROOT", "PYTHONHOME"):
        environment.pop(key, None)
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join(str(path) for path in python_paths),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    if attempt_root is None:
        yield environment, source_root, None
        return
    with short_attempt_temp(worktree, attempt_root) as (temporary, evidence):
        environment["TEMP"] = str(temporary)
        environment["TMP"] = str(temporary)
        environment["TMPDIR"] = str(temporary)
        yield environment, source_root, evidence


def runtime_fingerprints(protocol: Mapping[str, Any]) -> dict[str, str]:
    frozen = protocol["environment"]
    python = Path(frozen["python"])
    if not python.is_file():
        raise RunnerError("frozen Python executable is absent")
    root = python.parent.parent
    return {
        "python_sha256": sha256_file(python),
        "python_site_packages_sha256": fingerprint(root / "Lib" / "site-packages"),
        "python_venv_config_sha256": fingerprint(root / "pyvenv.cfg"),
        "python_environment_manifest_sha256": fingerprint(root / "SEMANTIC-ENVIRONMENT.json"),
        "click_compat_sha256": fingerprint(PROJECT_ROOT / frozen["click_compat_root"]),
    }


def runtime_probe_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith("PYTEST_") or key.startswith("GIT_") or key in {
            "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT",
        }:
            environment.pop(key, None)
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
            "LANG": "C",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    return environment


def verify_runtime(protocol: Mapping[str, Any]) -> dict[str, Any]:
    frozen = protocol["environment"]
    python = Path(frozen["python"])
    before = runtime_fingerprints(protocol)
    for key, observed in before.items():
        if observed != frozen[key]:
            raise RunnerError(f"frozen environment mismatch for {key}: {observed}")
    probe_environment = runtime_probe_environment()
    version = run_process([str(python), "--version"], cwd=PROJECT_ROOT, env=probe_environment, timeout=30)
    pytest = run_process(
        [str(python), "-m", "pytest", "--version"], cwd=PROJECT_ROOT,
        env=probe_environment, timeout=30,
    )
    if version.returncode != 0 or frozen["python_version"] not in (version.stdout + version.stderr).decode(errors="replace"):
        raise RunnerError("frozen Python version probe failed")
    if pytest.returncode != 0 or frozen["pytest_version"] not in (pytest.stdout + pytest.stderr).decode(errors="replace"):
        raise RunnerError("frozen pytest version probe failed")
    after = runtime_fingerprints(protocol)
    if after != before:
        raise RunnerError(f"frozen runtime changed during version probes: before={before}, after={after}")
    return {
        **before,
        "python": str(python),
        "python_version_output": (version.stdout + version.stderr).decode().strip(),
        "pytest_version_output": (pytest.stdout + pytest.stderr).decode().strip(),
        "fingerprints_before_probes": before,
        "fingerprints_after_probes": after,
        "integrity_unchanged_during_probes": True,
        "probe_policy": {
            "PYTHONPATH": "removed",
            "PYTHONHOME": "removed",
            "PYTEST_*": "removed before fixed PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
        },
    }


def display_path(path: Path) -> str:
    """Use repository-relative artifact paths where possible, absolute otherwise."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def verify_direct_hierarchy(root: Path, components: Sequence[str], label: str) -> Path:
    current = root.resolve()
    if current != DEFAULT_OUTPUT_ROOT.resolve():
        raise RunnerError(f"{label} root is not the frozen exploratory/arms output root: {current}")
    for component in components:
        candidate = (current / component).resolve()
        if candidate.parent != current:
            raise RunnerError(f"{label} hierarchy is redirected at {component!r}: {candidate}")
        current = candidate
    return current


def canonical_sha(value: Any) -> str:
    return sha256_bytes(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    )


def normalized_nodeid(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def target_leaf_coverage(
    targets: Sequence[str], nodeids: Sequence[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Map each frozen collector target only to itself or its ``target::`` leaves."""
    leaves = sorted({normalized_nodeid(nodeid) for nodeid in nodeids})
    coverage: dict[str, dict[str, Any]] = {}
    for raw_target in targets:
        target = normalized_nodeid(raw_target)
        matches = [nodeid for nodeid in leaves if nodeid == target or nodeid.startswith(target + "::")]
        coverage[raw_target] = {"normalized_target": target, "leaf_count": len(matches), "leaf_nodeids": matches}
    uncovered = sorted(target for target, evidence in coverage.items() if evidence["leaf_count"] == 0)
    return coverage, uncovered


def safe_tree_path(tree: Path, value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts or "\\" in value:
        raise RunnerError(f"unsafe repository-relative path: {value!r}")
    candidate = tree.joinpath(*pure.parts).resolve()
    root = tree.resolve()
    if candidate != root and root not in candidate.parents:
        raise RunnerError(f"repository path escapes attempt tree: {value!r}")
    return candidate


def tree_file_manifest(tree: Path) -> dict[str, str]:
    """Hash the candidate tree, including the worktree identity file, but no outside raw data."""
    result: dict[str, str] = {}
    for candidate in sorted(p for p in tree.rglob("*") if p.is_file()):
        relative = candidate.relative_to(tree).as_posix()
        if relative == ".pytest_cache" or relative.startswith(".pytest_cache/"):
            continue
        result[relative] = sha256_file(candidate)
    return result


def selected_file_manifest(tree: Path, paths: Sequence[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in sorted(set(paths)):
        candidate = safe_tree_path(tree, value)
        if candidate.is_file():
            result[value] = {"kind": "file", "sha256": sha256_file(candidate), "bytes": candidate.stat().st_size}
        elif candidate.is_dir():
            result[value] = {"kind": "directory", "sha256": fingerprint(candidate)}
        else:
            result[value] = {"kind": "missing"}
    return result


def resolve_patch_record(record: Mapping[str, Any], output_root: Path) -> Path:
    stored = Path(str(record["path"]))
    candidate = stored if stored.is_absolute() else PROJECT_ROOT / stored
    resolved = candidate.resolve()
    allowed = (output_root / "patches").resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise RunnerError(f"patch manifest points outside the task patch root: {resolved}")
    if not resolved.is_file():
        raise RunnerError(f"prepared patch is absent: {resolved}")
    observed = sha256_file(resolved)
    if observed != record.get("sha256") or resolved.stat().st_size != record.get("bytes"):
        raise RunnerError(f"prepared patch hash/size mismatch: {resolved}")
    return resolved


def load_prepared_site(
    spec: RepoSpec, index: int, row: Mapping[str, Any], output_root: Path, protocol_sha: str,
    runner_sha: str,
) -> tuple[dict[str, Any], str]:
    verify_direct_hierarchy(output_root, ["patches", spec.slug, row["merge"]], "patch input")
    path = output_root / "patches" / spec.slug / row["merge"] / "manifest.json"
    if not path.is_file():
        raise RunnerError(f"site has not been prepared: {path}")
    payload = path.read_bytes()
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError(f"prepared site manifest is invalid JSON: {path}: {error}") from error
    expected = {
        "schema_version": SCHEMA_VERSION,
        "protocol_sha256": protocol_sha,
        "runner_sha256": runner_sha,
        "repository": spec.repository,
        "repo_slug": spec.slug,
        "site_index": index,
        "corpus_line": row["corpus_line"],
        "merge": row["merge"],
        "base": row["merge_base"],
        "parents": row["parents"],
        "result_tree": row["result_tree"],
        "conflicted_paths": row["conflicted_paths"],
    }
    mismatches = {
        key: {"expected": value, "observed": manifest.get(key)}
        for key, value in expected.items() if manifest.get(key) != value
    }
    if mismatches:
        raise RunnerError(f"prepared site manifest mismatch at {path}: {mismatches}")
    for side_name in ("parent1", "parent2"):
        side = manifest.get("sides", {}).get(side_name)
        if not isinstance(side, dict):
            raise RunnerError(f"prepared site lacks {side_name}: {path}")
        resolve_patch_record(side["source_patch"], output_root)
        resolve_patch_record(side["test_patch"], output_root)
    joint = manifest.get("joint_source", {})
    if joint.get("constructible"):
        resolve_patch_record(joint["patch"], output_root)
        verification = joint.get("index_verification", {})
        if not verification.get("verified") or not verification.get("modes_and_oids_match"):
            raise RunnerError(f"constructible joint patch lacks dynamic index verification: {path}")
    elif joint.get("patch") is not None:
        raise RunnerError(f"source-conflicted site unexpectedly records a joint patch: {path}")
    return manifest, sha256_bytes(payload)


@contextlib.contextmanager
def fresh_base_tree(
    mirror: Path, worktree_root: Path, base: str, label: str,
) -> Iterator[Path]:
    owned_worktrees = worktree_root.resolve()
    if len(owned_worktrees.parents) < 3 or owned_worktrees.parents[2] != DEFAULT_SCRATCH_ROOT.resolve():
        raise RunnerError(f"worktree root is outside the frozen scratch hierarchy: {owned_worktrees}")
    nonce = time.time_ns()
    token = sha256_bytes(f"{base}\0{label}\0{os.getpid()}\0{nonce}".encode("utf-8"))[:20]
    destination = owned_worktrees / f"{base[:8]}-w{token}"
    if destination.resolve().parent != owned_worktrees:
        raise RunnerError(f"fresh worktree path is redirected outside its owned root: {destination}")
    if destination.exists():
        raise RunnerError(f"fresh worktree destination unexpectedly exists: {destination}")
    run_git(
        mirror, ["worktree", "add", "--detach", "--force", str(destination), base],
        allow_hydration=False,
    )
    try:
        probe = run_process(
            ["git", "-C", str(destination), "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
            env=process_environment(allow_git_hydration=False), timeout=60.0,
        )
        actual = probe.stdout.decode("ascii", errors="replace").strip()
        if probe.timed_out or probe.returncode != 0 or actual != base:
            raise RunnerError(f"fresh worktree identity mismatch for {label}: {actual!r} != {base}")
        yield destination
    finally:
        removed = run_git(
            mirror, ["worktree", "remove", "--force", str(destination)],
            allow_hydration=False, check=False,
        )
        pruned = run_git(mirror, ["worktree", "prune"], allow_hydration=False, check=False)
        if removed.returncode != 0 or removed.timed_out or pruned.returncode != 0 or pruned.timed_out:
            raise RunnerError(
                f"failed to clean fresh worktree {destination}: "
                f"remove={removed.returncode}, prune={pruned.returncode}"
            )


def apply_patch_to_tree(tree: Path, patch: Path, raw_root: Path, ordinal: int) -> dict[str, Any]:
    raw_root.mkdir(parents=True, exist_ok=True)
    result = run_process(
        [
            "git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
            "-c", f"core.hooksPath={os.devnull}", "-C", str(tree),
            "apply", "--binary", "--whitespace=nowarn", str(patch.resolve()),
        ],
        cwd=PROJECT_ROOT, env=process_environment(allow_git_hydration=False), timeout=300.0,
    )
    stdout_path = raw_root / f"apply-{ordinal:02d}.stdout.txt"
    stderr_path = raw_root / f"apply-{ordinal:02d}.stderr.txt"
    stdout_path.write_bytes(result.stdout)
    stderr_path.write_bytes(result.stderr)
    return {
        "patch": display_path(patch),
        "patch_sha256": sha256_file(patch),
        "argv": list(result.argv),
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
        "launch_error": result.launch_error,
        "elapsed_seconds": result.elapsed_seconds,
        "stdout": display_path(stdout_path),
        "stderr": display_path(stderr_path),
        "ok": result.returncode == 0 and not result.timed_out and result.launch_error is None,
    }


def internal_pytest(pytest_args: Sequence[str], output: Path) -> int:
    """Run pytest in its frozen subprocess and capture actual leaf IDs/outcomes."""
    import pytest

    class Recorder:
        def __init__(self) -> None:
            self.nodeids: list[str] = []
            self.phases: dict[str, dict[str, dict[str, Any]]] = {}
            self.collection_errors: list[dict[str, str]] = []
            self.internal_errors: list[str] = []

        def pytest_collection_finish(self, session: Any) -> None:
            self.nodeids = sorted({normalized_nodeid(item.nodeid) for item in session.items})

        def pytest_collectreport(self, report: Any) -> None:
            if report.failed:
                self.collection_errors.append(
                    {"nodeid": normalized_nodeid(report.nodeid), "longrepr": str(report.longrepr)}
                )

        def pytest_internalerror(self, excrepr: Any, excinfo: Any) -> None:
            self.internal_errors.append(str(excrepr))

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item: Any, call: Any) -> Iterator[None]:
            outcome = yield
            report = outcome.get_result()
            nodeid = normalized_nodeid(report.nodeid)
            exception: dict[str, Any] | None = None
            if report.failed and call.excinfo is not None:
                error_type = call.excinfo.type
                try:
                    is_import_error = issubclass(error_type, ImportError)
                except TypeError:
                    is_import_error = False
                exception = {
                    "type": getattr(error_type, "__name__", str(error_type)),
                    "qualified_type": (
                        f"{getattr(error_type, '__module__', '')}."
                        f"{getattr(error_type, '__qualname__', getattr(error_type, '__name__', str(error_type)))}"
                    ).lstrip("."),
                    "message": str(call.excinfo.value),
                    "longrepr": str(report.longrepr),
                    "is_import_error": is_import_error,
                }
            self.phases.setdefault(nodeid, {})[report.when] = {
                "outcome": report.outcome,
                "wasxfail": str(report.wasxfail) if hasattr(report, "wasxfail") else None,
                "exception": exception,
            }

    recorder = Recorder()
    launch_exception: str | None = None
    try:
        exit_code = int(pytest.main(list(pytest_args), plugins=[recorder]))
    except BaseException as error:  # preserve apparatus evidence even for pytest/plugin crashes
        exit_code = 3
        launch_exception = repr(error)

    outcomes: dict[str, str] = {}
    details: dict[str, Any] = {}
    all_nodeids = sorted(set(recorder.nodeids) | set(recorder.phases))
    for nodeid in all_nodeids:
        phases = recorder.phases.get(nodeid, {})
        failed = [when for when, report in phases.items() if report["outcome"] == "failed"]
        if failed:
            outcome = "failure" if failed == ["call"] else "error"
        else:
            skipped = [report for report in phases.values() if report["outcome"] == "skipped"]
            call = phases.get("call")
            if skipped:
                outcome = "xfailed" if any(report.get("wasxfail") for report in skipped) else "skipped"
            elif call and call["outcome"] == "passed":
                outcome = "xpassed" if call.get("wasxfail") else "passed"
            else:
                outcome = "not_run"
        outcomes[nodeid] = outcome
        details[nodeid] = phases
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    failed_runtest_exceptions = [
        {"nodeid": nodeid, "phase": phase, **exception}
        for nodeid, phases in details.items()
        for phase, phase_record in phases.items()
        if isinstance((exception := phase_record.get("exception")), dict)
    ]
    import_error_failure_nodeids = sorted(
        {
            detail["nodeid"] for detail in failed_runtest_exceptions
            if detail.get("is_import_error") is True
        }
    )
    normalized_leaf_results = [
        {"nodeid": nodeid, "outcome": outcomes[nodeid]} for nodeid in sorted(outcomes)
    ]
    record = {
        "exit_code": exit_code,
        "nodeids": recorder.nodeids,
        "nodeid_count": len(recorder.nodeids),
        "leaf_outcomes": outcomes,
        "leaf_outcome_counts": counts,
        "normalized_leaf_results": normalized_leaf_results,
        "leaf_outcome_signature_sha256": canonical_sha(normalized_leaf_results),
        "phase_reports": details,
        "failing_nodeids": sorted(
            nodeid for nodeid, outcome in outcomes.items() if outcome in {"failure", "error"}
        ),
        "passed_nodeids": sorted(nodeid for nodeid, outcome in outcomes.items() if outcome == "passed"),
        "failed_runtest_exceptions": failed_runtest_exceptions,
        "import_error_failure_nodeids": import_error_failure_nodeids,
        "collection_errors": recorder.collection_errors,
        "internal_errors": recorder.internal_errors,
        "launch_exception": launch_exception,
    }
    atomic_json(output, record)
    return exit_code


def cmd_internal_pytest(args: argparse.Namespace) -> int:
    pytest_args = list(args.pytest_args)
    if pytest_args and pytest_args[0] == "--":
        pytest_args.pop(0)
    allowed = verify_direct_hierarchy(DEFAULT_OUTPUT_ROOT, ["raw"], "internal pytest output")
    output = args.output.resolve()
    if output == allowed or allowed not in output.parents:
        raise RunnerError(f"internal pytest output escapes the frozen raw root: {output}")
    return internal_pytest(pytest_args, output)


def environment_probe(
    worktree: Path, spec: RepoSpec, protocol: Mapping[str, Any], raw_root: Path,
) -> dict[str, Any]:
    raw_root.mkdir(parents=True, exist_ok=True)
    code = (
        "import importlib,importlib.util,json,pathlib;"
        f"m=importlib.import_module({spec.package!r});"
        "s=importlib.util.find_spec('sitecustomize');"
        f"print(json.dumps({{'module':{spec.package!r},'module_file':str(pathlib.Path(m.__file__).resolve()),"
        "'sitecustomize_file':str(pathlib.Path(s.origin).resolve()) if s and s.origin else None}))"
    )
    with suite_environment(worktree, spec, protocol, attempt_root=raw_root) as (
        environment, source_root, temp_evidence,
    ):
        budget = child_path_budget(
            {
                "python": Path(protocol["environment"]["python"]),
                "source_root": source_root,
                "temporary_root": Path(environment["TEMP"]),
                "worktree": worktree,
            }
        )
        result = run_process(
            [str(protocol["environment"]["python"]), "-c", code], cwd=worktree,
            env=environment, timeout=float(protocol["environment"]["timeout_seconds"]),
        )
    stdout_path = raw_root / "stdout.txt"
    stderr_path = raw_root / "stderr.txt"
    stdout_path.write_bytes(result.stdout)
    stderr_path.write_bytes(result.stderr)
    payload: dict[str, Any] | None = None
    if result.returncode == 0 and not result.timed_out:
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
    module_inside = False
    if payload and payload.get("module_file"):
        module_path = Path(payload["module_file"]).resolve()
        root = worktree.resolve()
        module_inside = module_path == root or root in module_path.parents
    compat_exact = True
    if spec.key == "click":
        expected = (PROJECT_ROOT / protocol["environment"]["click_compat_root"] / "sitecustomize.py").resolve()
        compat_exact = bool(payload and payload.get("sitecustomize_file")) and Path(
            payload["sitecustomize_file"]
        ).resolve() == expected
    record = {
        "argv": list(result.argv),
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
        "launch_error": result.launch_error,
        "elapsed_seconds": result.elapsed_seconds,
        "payload": payload,
        "source_root": str(source_root.resolve()),
        "child_path_budget": budget,
        "temp_evidence": temp_evidence,
        "module_inside_attempt": module_inside,
        "click_compat_exact": compat_exact,
        "stdout": display_path(stdout_path),
        "stderr": display_path(stderr_path),
        "ok": (
            result.returncode == 0 and not result.timed_out and result.launch_error is None
            and module_inside and compat_exact
        ),
    }
    atomic_json(raw_root / "result.json", record)
    return record


def require_completed_environment_probe(probe: Mapping[str, Any]) -> None:
    """Keep probe process starvation/launch failure out of site verdicts."""
    if probe.get("timed_out") or probe.get("launch_error") is not None:
        raise ApparatusError(
            "environment probe apparatus failure: "
            f"timed_out={probe.get('timed_out')}, launch_error={probe.get('launch_error')!r}"
        )


def load_leaf_record(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def pytest_subprocess_args(
    protocol: Mapping[str, Any], spec: RepoSpec, output: Path, targets: Sequence[str], *, collect_only: bool,
    junit: Path | None = None,
) -> list[str]:
    pytest_args = ["--color=no", "-q", *spec.pytest_prefix]
    if collect_only:
        pytest_args.append("--collect-only")
    if junit is not None:
        pytest_args.append(f"--junitxml={junit.resolve()}")
    pytest_args.extend(targets)
    return [
        str(protocol["environment"]["python"]), str(Path(__file__).resolve()),
        "internal-pytest", "--output", str(output.resolve()), "--", *pytest_args,
    ]


def raw_process_artifacts(result: ProcessResult, raw_root: Path) -> dict[str, Any]:
    stdout_path = raw_root / "stdout.txt"
    stderr_path = raw_root / "stderr.txt"
    stdout_path.write_bytes(result.stdout)
    stderr_path.write_bytes(result.stderr)
    return {
        "argv": list(result.argv),
        "exit_code": result.returncode,
        "timed_out": result.timed_out,
        "launch_error": result.launch_error,
        "elapsed_seconds": result.elapsed_seconds,
        "stdout": display_path(stdout_path),
        "stderr": display_path(stderr_path),
    }


def failed_attempt_record(
    raw_root: Path, *, kind: str, targets: Sequence[str], error: str,
    patch_applications: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    raw_root.mkdir(parents=True, exist_ok=True)
    coverage, uncovered = target_leaf_coverage(targets, [])
    record = {
        "kind": kind,
        "targets": list(targets),
        "executed": False,
        "setup_error": error,
        "patch_applications": list(patch_applications),
        "exit_code": None,
        "timed_out": False,
        "counts": {"pytest_summary": {}, "junit": {}, "leaf_outcomes": {}},
        "leaf_nodeids": [],
        "target_leaf_coverage": coverage,
        "uncovered_targets": uncovered,
        "all_targets_have_leaves": not uncovered and bool(targets),
        "normalized_leaf_results": [],
        "leaf_outcome_signature_sha256": None,
        "failing_nodeids": [],
        "passed_nodeids": [],
        "failed_runtest_exceptions": [],
        "normalized_failure_details": [],
        "import_error_failure_nodeids": [],
        "green": False,
        "qualifying_red": False,
    }
    atomic_json(raw_root / "result.json", record)
    return record


def run_collection_attempt(
    *, worktree: Path, spec: RepoSpec, targets: Sequence[str], raw_root: Path,
    protocol: Mapping[str, Any], patch_applications: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    raw_root.mkdir(parents=True, exist_ok=True)
    record_path = raw_root / "leaf-record.json"
    try:
        with suite_environment(worktree, spec, protocol, attempt_root=raw_root) as (
            environment, source_root, temp_evidence,
        ):
            before = tree_file_manifest(worktree)
            probe = environment_probe(worktree, spec, protocol, raw_root / "environment-probe")
            require_completed_environment_probe(probe)
            budget_paths = {
                "python": Path(protocol["environment"]["python"]),
                "runner": Path(__file__),
                "source_root": source_root,
                "temporary_root": Path(environment["TEMP"]),
                "worktree": worktree,
                "leaf_record": record_path,
            }
            budget_paths.update(
                {
                    f"target_{ordinal}": safe_tree_path(
                        worktree, normalized_nodeid(target).split("::", 1)[0]
                    )
                    for ordinal, target in enumerate(targets, 1)
                }
            )
            budget = child_path_budget(budget_paths)
            command = pytest_subprocess_args(protocol, spec, record_path, targets, collect_only=True)
            result = run_process(
                command, cwd=worktree, env=environment,
                timeout=float(protocol["environment"]["timeout_seconds"]),
            )
            process_record = raw_process_artifacts(result, raw_root)
            after = tree_file_manifest(worktree)
    except ApparatusError:
        raise
    except RunnerError as error:
        return failed_attempt_record(
            raw_root, kind="collection", targets=targets, error=str(error),
            patch_applications=patch_applications,
        )
    leaf = load_leaf_record(record_path)
    nodeids = sorted(set((leaf or {}).get("nodeids", [])))
    coverage, uncovered = target_leaf_coverage(targets, nodeids)
    collection_errors = (leaf or {}).get("collection_errors", [])
    internal_errors = (leaf or {}).get("internal_errors", [])
    launch_exception = (leaf or {}).get("launch_exception")
    leaf_outcomes = dict((leaf or {}).get("leaf_outcomes", {}))
    normalized_leaf_results = (leaf or {}).get("normalized_leaf_results", [])
    expected_leaf_results = [
        {"nodeid": nodeid, "outcome": leaf_outcomes[nodeid]} for nodeid in sorted(leaf_outcomes)
    ]
    leaf_signature = (leaf or {}).get("leaf_outcome_signature_sha256")
    leaf_signature_valid = bool(
        normalized_leaf_results == expected_leaf_results
        and leaf_signature == canonical_sha(expected_leaf_results)
    )
    record = {
        "kind": "collection",
        "targets": list(targets),
        "executed": True,
        "patch_applications": list(patch_applications),
        **process_record,
        "source_root": str(source_root.resolve()),
        "child_path_budget": budget,
        "temp_evidence": temp_evidence,
        "environment_probe": probe,
        "leaf_record": display_path(record_path) if record_path.is_file() else None,
        "leaf_nodeids": nodeids,
        "leaf_count": len(nodeids),
        "target_leaf_coverage": coverage,
        "uncovered_targets": uncovered,
        "all_targets_have_leaves": bool(targets) and not uncovered,
        "normalized_leaf_results": normalized_leaf_results,
        "leaf_outcome_signature_sha256": leaf_signature,
        "leaf_outcome_signature_valid": leaf_signature_valid,
        "collection_errors": collection_errors,
        "internal_errors": internal_errors,
        "internal_launch_exception": launch_exception,
        "files_unchanged": before == after,
        "tree_before_sha256": canonical_sha(before),
        "tree_after_sha256": canonical_sha(after),
    }
    record["ok"] = bool(
        result.returncode == 0 and not result.timed_out and result.launch_error is None
        and leaf is not None and nodeids and bool(targets) and not uncovered
        and leaf_signature_valid
        and not collection_errors and not internal_errors
        and launch_exception is None and probe["ok"] and before == after
    )
    atomic_json(raw_root / "result.json", record)
    return record


def run_pytest_attempt(
    *, worktree: Path, spec: RepoSpec, targets: Sequence[str], raw_root: Path,
    protocol: Mapping[str, Any], patch_applications: Sequence[Mapping[str, Any]] = (),
    expected_leaf_nodeids: Sequence[str] | None = None,
    expected_overlay_manifest: Mapping[str, Any] | None = None,
    overlay_paths: Sequence[str] = (),
    required_pass_nodeids: Sequence[str] = (),
) -> dict[str, Any]:
    raw_root.mkdir(parents=True, exist_ok=True)
    record_path = raw_root / "leaf-record.json"
    junit_path = raw_root / "junit.xml"
    try:
        with suite_environment(worktree, spec, protocol, attempt_root=raw_root) as (
            environment, source_root, temp_evidence,
        ):
            overlay_manifest = selected_file_manifest(worktree, overlay_paths) if overlay_paths else None
            before = tree_file_manifest(worktree)
            probe = environment_probe(worktree, spec, protocol, raw_root / "environment-probe")
            require_completed_environment_probe(probe)
            budget_paths = {
                "python": Path(protocol["environment"]["python"]),
                "runner": Path(__file__),
                "source_root": source_root,
                "temporary_root": Path(environment["TEMP"]),
                "worktree": worktree,
                "leaf_record": record_path,
                "junit": junit_path,
            }
            budget_paths.update(
                {
                    f"target_{ordinal}": safe_tree_path(
                        worktree, normalized_nodeid(target).split("::", 1)[0]
                    )
                    for ordinal, target in enumerate(targets, 1)
                }
            )
            budget = child_path_budget(budget_paths)
            command = pytest_subprocess_args(
                protocol, spec, record_path, targets, collect_only=False, junit=junit_path,
            )
            result = run_process(
                command, cwd=worktree, env=environment,
                timeout=float(protocol["environment"]["timeout_seconds"]),
            )
            process_record = raw_process_artifacts(result, raw_root)
            after = tree_file_manifest(worktree)
    except ApparatusError:
        raise
    except RunnerError as error:
        return failed_attempt_record(
            raw_root, kind="pytest", targets=targets, error=str(error),
            patch_applications=patch_applications,
        )
    leaf = load_leaf_record(record_path)
    nodeids = sorted(set((leaf or {}).get("nodeids", [])))
    coverage, uncovered = target_leaf_coverage(targets, nodeids)
    leaf_outcomes = dict((leaf or {}).get("leaf_outcomes", {}))
    failing_nodeids = sorted((leaf or {}).get("failing_nodeids", []))
    passed_nodeids = sorted((leaf or {}).get("passed_nodeids", []))
    junit = normalized_junit(junit_path)
    combined = (result.stdout + b"\n" + result.stderr).decode("utf-8", errors="replace")
    summary = summary_counts(combined)
    expected_leaves = sorted(set(expected_leaf_nodeids)) if expected_leaf_nodeids is not None else None
    leaf_match = expected_leaves is None or nodeids == expected_leaves
    overlay_match = expected_overlay_manifest is None or overlay_manifest == dict(expected_overlay_manifest)
    required_pass = sorted(set(required_pass_nodeids))
    required_pass_outcomes = {nodeid: leaf_outcomes.get(nodeid, "absent") for nodeid in required_pass}
    required_pass_ok = all(outcome == "passed" for outcome in required_pass_outcomes.values())
    collection_errors = (leaf or {}).get("collection_errors", [])
    internal_errors = (leaf or {}).get("internal_errors", [])
    launch_exception = (leaf or {}).get("launch_exception")
    failed_runtest_exceptions = (leaf or {}).get("failed_runtest_exceptions", [])
    import_error_failure_nodeids = sorted((leaf or {}).get("import_error_failure_nodeids", []))
    normalized_failure_details = sorted(
        (
            {
                "nodeid": normalized_nodeid(str(detail.get("nodeid", ""))),
                "phase": str(detail.get("phase", "")),
                "qualified_type": str(detail.get("qualified_type") or detail.get("type") or ""),
                "message": str(detail.get("message", "")),
                "is_import_error": detail.get("is_import_error") is True,
            }
            for detail in failed_runtest_exceptions
            if isinstance(detail, Mapping)
        ),
        key=lambda detail: (
            detail["nodeid"], detail["phase"], detail["qualified_type"], detail["message"],
            detail["is_import_error"],
        ),
    )
    normalized_leaf_results = (leaf or {}).get("normalized_leaf_results", [])
    leaf_signature = (leaf or {}).get("leaf_outcome_signature_sha256")
    expected_leaf_results = [
        {"nodeid": nodeid, "outcome": leaf_outcomes[nodeid]} for nodeid in sorted(leaf_outcomes)
    ]
    leaf_signature_valid = bool(
        normalized_leaf_results == expected_leaf_results
        and leaf_signature == canonical_sha(expected_leaf_results)
    )
    junit_counts = (junit or {}).get("counts", {})
    record = {
        "kind": "pytest",
        "targets": list(targets),
        "executed": True,
        "patch_applications": list(patch_applications),
        **process_record,
        "source_root": str(source_root.resolve()),
        "child_path_budget": budget,
        "temp_evidence": temp_evidence,
        "environment_probe": probe,
        "leaf_record": display_path(record_path) if record_path.is_file() else None,
        "leaf_nodeids": nodeids,
        "leaf_count": len(nodeids),
        "target_leaf_coverage": coverage,
        "uncovered_targets": uncovered,
        "all_targets_have_leaves": bool(targets) and not uncovered,
        "leaf_outcomes": leaf_outcomes,
        "normalized_leaf_results": normalized_leaf_results,
        "leaf_outcome_signature_sha256": leaf_signature,
        "leaf_outcome_signature_valid": leaf_signature_valid,
        "failing_nodeids": failing_nodeids,
        "passed_nodeids": passed_nodeids,
        "failed_runtest_exceptions": failed_runtest_exceptions,
        "normalized_failure_details": normalized_failure_details,
        "import_error_failure_nodeids": import_error_failure_nodeids,
        "collection_errors": collection_errors,
        "internal_errors": internal_errors,
        "internal_launch_exception": launch_exception,
        "junit": junit,
        "junit_path": display_path(junit_path) if junit_path.is_file() else None,
        "pytest_summary_counts": summary,
        "counts": {
            "pytest_summary": summary,
            "junit": junit_counts,
            "leaf_outcomes": (leaf or {}).get("leaf_outcome_counts", {}),
        },
        "files_unchanged": before == after,
        "tree_before_sha256": canonical_sha(before),
        "tree_after_sha256": canonical_sha(after),
        "expected_leaf_nodeids": expected_leaves,
        "leaf_nodeids_match_overlay": leaf_match,
        "overlay_file_manifest": overlay_manifest,
        "overlay_files_match_frozen": overlay_match,
        "required_pass_nodeids": required_pass,
        "required_pass_outcomes": required_pass_outcomes,
        "required_pass_nodeids_passed": required_pass_ok,
    }
    common_ok = bool(
        not result.timed_out and result.launch_error is None and leaf is not None and nodeids
        and bool(targets) and not uncovered and leaf_signature_valid
        and not collection_errors and not internal_errors and launch_exception is None
        and probe["ok"] and before == after and leaf_match and overlay_match
        and junit is not None
    )
    # normalized_junit stores case_count beside counts; require at least one emitted case.
    has_junit_cases = bool(junit and junit.get("case_count", 0) > 0)
    no_junit_failures = junit_counts.get("failure", 0) == 0 and junit_counts.get("error", 0) == 0
    record["green"] = bool(
        common_ok and has_junit_cases and result.returncode == 0 and no_junit_failures
        and required_pass_ok
    )
    record["qualifying_red"] = bool(
        common_ok and has_junit_cases and result.returncode == 1
        and junit_counts.get("failure", 0) + junit_counts.get("error", 0) > 0
        and bool(failing_nodeids) and not import_error_failure_nodeids
    )
    record["normalized_attempt_signature_sha256"] = canonical_sha(
        {
            "executed": True,
            "exit_code": result.returncode,
            "timed_out": result.timed_out,
            "leaf_nodeids": nodeids,
            "leaf_outcomes": leaf_outcomes,
            "target_leaf_coverage": coverage,
            "uncovered_targets": uncovered,
            "leaf_outcome_signature": leaf_signature,
            "normalized_failure_details": normalized_failure_details,
            "import_error_failure_nodeids": import_error_failure_nodeids,
            "collection_error_nodeids": sorted(
                str(item.get("nodeid", "")) for item in collection_errors
            ),
            "internal_error_count": len(internal_errors),
            "junit_signature": (junit or {}).get("case_outcome_signature_sha256"),
            "environment_ok": probe["ok"],
            "files_unchanged": before == after,
            "leaf_match": leaf_match,
            "overlay_match": overlay_match,
        }
    )
    atomic_json(raw_root / "result.json", record)
    return record


def all_patch_applications_ok(records: Sequence[Mapping[str, Any]]) -> bool:
    return all(record.get("ok") is True for record in records)


def run_collection_state(
    *, mirror: Path, worktree_root: Path, base: str, label: str, spec: RepoSpec,
    targets: Sequence[str], patches: Sequence[Path], raw_root: Path,
    protocol: Mapping[str, Any], manifest_paths: Sequence[str] = (),
) -> dict[str, Any]:
    with fresh_base_tree(mirror, worktree_root, base, label) as tree:
        applications: list[dict[str, Any]] = []
        for ordinal, patch in enumerate(patches, 1):
            application = apply_patch_to_tree(tree, patch, raw_root / "patches", ordinal)
            applications.append(application)
            if not application["ok"]:
                return failed_attempt_record(
                    raw_root, kind="collection", targets=targets,
                    error=f"patch application {ordinal} failed", patch_applications=applications,
                )
        state_manifest = selected_file_manifest(tree, manifest_paths) if manifest_paths else None
        record = run_collection_attempt(
            worktree=tree, spec=spec, targets=targets, raw_root=raw_root,
            protocol=protocol, patch_applications=applications,
        )
        record["state_file_manifest"] = state_manifest
        atomic_json(raw_root / "result.json", record)
        return record


def run_pytest_state(
    *, mirror: Path, worktree_root: Path, base: str, label: str, spec: RepoSpec,
    targets: Sequence[str], patches: Sequence[Path], raw_root: Path,
    protocol: Mapping[str, Any], expected_leaf_nodeids: Sequence[str] | None = None,
    expected_overlay_manifest: Mapping[str, Any] | None = None,
    overlay_paths: Sequence[str] = (), required_pass_nodeids: Sequence[str] = (),
) -> dict[str, Any]:
    with fresh_base_tree(mirror, worktree_root, base, label) as tree:
        applications: list[dict[str, Any]] = []
        for ordinal, patch in enumerate(patches, 1):
            application = apply_patch_to_tree(tree, patch, raw_root / "patches", ordinal)
            applications.append(application)
            if not application["ok"]:
                return failed_attempt_record(
                    raw_root, kind="pytest", targets=targets,
                    error=f"patch application {ordinal} failed", patch_applications=applications,
                )
        return run_pytest_attempt(
            worktree=tree, spec=spec, targets=targets, raw_root=raw_root,
            protocol=protocol, patch_applications=applications,
            expected_leaf_nodeids=expected_leaf_nodeids,
            expected_overlay_manifest=expected_overlay_manifest,
            overlay_paths=overlay_paths, required_pass_nodeids=required_pass_nodeids,
        )


def attempt_failure_reasons(record: Mapping[str, Any], desired: str) -> list[str]:
    reasons: list[str] = []
    if not record.get("executed"):
        reasons.append(f"not executed: {record.get('setup_error', 'unknown setup error')}")
        return reasons
    if record.get("timed_out"):
        reasons.append("pytest timed out")
    if record.get("launch_error"):
        reasons.append(f"process launch error: {record['launch_error']}")
    if not record.get("environment_probe", {}).get("ok"):
        reasons.append("candidate import/environment probe failed")
    if record.get("collection_errors"):
        nodes = sorted(
            str(item.get("nodeid", "<unknown>"))
            for item in record.get("collection_errors", []) if isinstance(item, dict)
        )
        reasons.append(f"pytest collection error at {nodes}")
    if record.get("internal_errors") or record.get("internal_launch_exception"):
        reasons.append("pytest internal/plugin error")
    if record.get("leaf_record") is None:
        reasons.append("pytest leaf recorder artifact is missing or invalid")
    if not record.get("leaf_nodeids"):
        reasons.append("pytest collected zero focal leaf items")
    uncovered = list(record.get("uncovered_targets", []))
    if uncovered:
        reasons.append(f"fixed collector targets with zero mapped leaves: {uncovered}")
    if record.get("leaf_outcome_signature_valid") is False:
        reasons.append("normalized leaf nodeid/outcome evidence is missing or hash-invalid")
    junit = record.get("junit")
    if not isinstance(junit, dict):
        reasons.append("normalized JUnit evidence is missing or invalid")
        junit_counts: Mapping[str, Any] = {}
    else:
        junit_counts = junit.get("counts", {}) if isinstance(junit.get("counts"), dict) else {}
        if junit.get("case_count", 0) <= 0:
            reasons.append("JUnit emitted zero testcase records")
    if record.get("files_unchanged") is False:
        reasons.append("candidate tree changed during pytest")
    if record.get("leaf_nodeids_match_overlay") is False:
        reasons.append("collected leaf ids differ from the frozen test-overlay leaves")
    if record.get("overlay_files_match_frozen") is False:
        reasons.append("test-overlay files differ from the frozen red overlay")
    if desired == "red":
        if record.get("exit_code") != 1:
            reasons.append(f"pytest exit was {record.get('exit_code')}, expected test-failure exit 1")
        if not record.get("failing_nodeids"):
            reasons.append("no test-level failing leaf identity was recorded")
        if junit_counts.get("failure", 0) + junit_counts.get("error", 0) <= 0:
            reasons.append("JUnit recorded no test-level failure or error")
        import_failures = list(record.get("import_error_failure_nodeids", []))
        if import_failures:
            reasons.append(
                "test execution raised ImportError/ModuleNotFoundError rather than a qualifying behavioral red: "
                f"{import_failures}"
            )
    elif desired == "green":
        if record.get("exit_code") != 0:
            reasons.append(f"pytest exit was {record.get('exit_code')}, expected 0")
        if junit_counts.get("failure", 0) or junit_counts.get("error", 0):
            reasons.append(
                "JUnit green predicate failed: "
                f"failure={junit_counts.get('failure', 0)}, error={junit_counts.get('error', 0)}"
            )
        missing = {
            nodeid: outcome for nodeid, outcome in record.get("required_pass_outcomes", {}).items()
            if outcome != "passed"
        }
        if missing:
            reasons.append(f"red failing leaves did not pass in green: {missing}")
    return reasons or [f"attempt did not satisfy {desired} predicate"]


def focal_selection(
    *, spec: RepoSpec, side_name: str, side_manifest: Mapping[str, Any], mirror: Path,
    worktree_root: Path, base: str, test_patch: Path, raw_root: Path,
    protocol: Mapping[str, Any], protocol_sha: str,
) -> dict[str, Any]:
    test_paths = sorted(set(side_manifest["test_paths"]))
    conventional = sorted(path for path in test_paths if CONVENTIONAL_PYTEST_RE.fullmatch(PurePosixPath(path).name))
    nonconventional = sorted(set(test_paths) - set(conventional))
    probes: list[dict[str, Any]] = []
    mapped_nonconventional: list[str] = []
    unmapped: list[dict[str, Any]] = []
    for ordinal, path in enumerate(nonconventional, 1):
        probe_root = raw_root / "nonconventional" / f"{ordinal:03d}-{sha256_bytes(path.encode('utf-8'))[:12]}"
        probe = run_collection_state(
            mirror=mirror, worktree_root=worktree_root, base=base,
            label=f"{side_name}-map-{ordinal}", spec=spec, targets=[path],
            patches=[test_patch], raw_root=probe_root, protocol=protocol,
            manifest_paths=[path],
        )
        exists = (probe.get("state_file_manifest") or {}).get(path, {}).get("kind") != "missing"
        mapped = bool(probe.get("ok") and probe.get("leaf_nodeids"))
        mapping_reason = None
        if mapped:
            mapped_nonconventional.append(path)
        else:
            if not probe.get("executed"):
                mapping_reason = probe.get("setup_error", "collection was not executed")
            elif not exists:
                mapping_reason = "path missing after complete test patch"
            elif probe.get("collection_errors"):
                mapping_reason = "explicit pytest collection error"
            elif probe.get("uncovered_targets"):
                mapping_reason = "explicit target yielded zero path-prefix-mapped pytest items"
            elif probe.get("exit_code") == 5 or not probe.get("leaf_nodeids"):
                mapping_reason = "explicit target yielded zero pytest items"
            else:
                mapping_reason = f"explicit target was not collectable (exit {probe.get('exit_code')})"
            unmapped.append({"path": path, "reason": mapping_reason, "raw": display_path(probe_root / "result.json")})
        probes.append(
            {
                "path": path,
                "mapped_directly": mapped,
                "mapping_reason": mapping_reason,
                "collected_leaf_nodeids": probe.get("leaf_nodeids", []),
                "target_leaf_coverage": probe.get("target_leaf_coverage", {}),
                "uncovered_targets": probe.get("uncovered_targets", []),
                "raw": display_path(probe_root / "result.json"),
            }
        )

    direct_targets = sorted(set(conventional) | set(mapped_nonconventional))
    if direct_targets:
        overlay = run_collection_state(
            mirror=mirror, worktree_root=worktree_root, base=base,
            label=f"{side_name}-overlay-collect", spec=spec, targets=direct_targets,
            patches=[test_patch], raw_root=raw_root / "overlay-collection",
            protocol=protocol, manifest_paths=test_paths,
        )
    else:
        overlay = failed_attempt_record(
            raw_root / "overlay-collection", kind="collection", targets=[],
            error="no direct focal collector target was fixed by the frozen rule",
        )
        overlay["state_file_manifest"] = None
        atomic_json(raw_root / "overlay-collection" / "result.json", overlay)

    overlay_manifest = overlay.get("state_file_manifest") or {}
    missing_conventional = sorted(
        target for target in conventional if overlay_manifest.get(target, {}).get("kind") != "file"
    )
    reasons: list[str] = []
    if not direct_targets:
        reasons.append("zero direct focal collector targets")
    if missing_conventional:
        reasons.append(f"conventional test modules missing after overlay: {missing_conventional}")
    if overlay.get("uncovered_targets"):
        reasons.append(
            "fixed overlay collector targets with zero mapped leaves: "
            f"{overlay['uncovered_targets']}"
        )
    if not overlay.get("ok"):
        reasons.append("combined test-overlay focal collection failed or yielded zero items")
    record = {
        "rule_fixed_by_protocol_sha256": protocol_sha,
        "rule": protocol["focal_selection_rule"],
        "side": side_name,
        "test_patch_paths": test_paths,
        "conventional_whole_module_targets": conventional,
        "nonconventional_explicit_probes": probes,
        "unmapped_support_paths": unmapped,
        "focal_node_ids": direct_targets,
        "focal_collector_targets": direct_targets,
        "overlay_leaf_node_ids": overlay.get("leaf_nodeids", []),
        "overlay_target_leaf_coverage": overlay.get("target_leaf_coverage", {}),
        "overlay_uncovered_targets": overlay.get("uncovered_targets", []),
        "overlay_test_file_manifest": overlay_manifest,
        "overlay_collection": overlay,
        "missing_conventional_targets": missing_conventional,
        "ok": not reasons,
        "failure_reasons": reasons,
    }
    atomic_json(raw_root / "result.json", record)
    return record


def not_run_check(reason: str) -> dict[str, Any]:
    return {
        "outcome": "NOT_RUN",
        "passed": False,
        "reason": reason,
        "counts": [],
    }


def validate_side(
    *, spec: RepoSpec, side_name: str, site_manifest: Mapping[str, Any], mirror: Path,
    worktree_root: Path, output_root: Path, raw_root: Path,
    protocol: Mapping[str, Any], protocol_sha: str,
) -> dict[str, Any]:
    side_manifest = site_manifest["sides"][side_name]
    source_patch = resolve_patch_record(side_manifest["source_patch"], output_root)
    test_patch = resolve_patch_record(side_manifest["test_patch"], output_root)
    base = site_manifest["base"]
    focal = focal_selection(
        spec=spec, side_name=side_name, side_manifest=side_manifest, mirror=mirror,
        worktree_root=worktree_root, base=base, test_patch=test_patch,
        raw_root=raw_root / "focal-selection", protocol=protocol, protocol_sha=protocol_sha,
    )
    if not focal["ok"]:
        reason = "; ".join(focal["failure_reasons"])
        return {
            "side": side_name,
            "parent": side_manifest["parent"],
            "source_patch": side_manifest["source_patch"],
            "test_patch": side_manifest["test_patch"],
            "focal_selection": focal,
            "checks": {
                "base_determinism": not_run_check(f"focal selection failed: {reason}"),
                "red_test_patch_only": not_run_check(f"focal selection failed: {reason}"),
                "green_source_and_test": not_run_check(f"focal selection failed: {reason}"),
            },
            "validated": False,
            "failure_reasons": [f"focal selection: {reason}"],
        }

    targets = focal["focal_collector_targets"]
    base_attempts: list[dict[str, Any]] = []
    for attempt_number in range(1, 6):
        base_attempts.append(
            run_pytest_state(
                mirror=mirror, worktree_root=worktree_root, base=base,
                label=f"{side_name}-base-{attempt_number}", spec=spec, targets=targets,
                patches=[], raw_root=raw_root / "base" / f"attempt-{attempt_number}",
                protocol=protocol,
            )
        )
    base_junit_signatures = [
        (attempt.get("junit") or {}).get("case_outcome_signature_sha256") for attempt in base_attempts
    ]
    base_leaf_signatures = [
        attempt.get("leaf_outcome_signature_sha256") for attempt in base_attempts
    ]
    base_all_green = all(attempt.get("green") is True for attempt in base_attempts)
    base_deterministic = bool(
        base_all_green
        and base_junit_signatures[0] is not None and len(set(base_junit_signatures)) == 1
        and base_leaf_signatures[0] is not None and len(set(base_leaf_signatures)) == 1
    )
    base_reasons: list[str] = []
    if not base_all_green:
        failed_numbers = [index for index, attempt in enumerate(base_attempts, 1) if not attempt.get("green")]
        base_reasons.append(f"untouched B was not pytest-green in attempts {failed_numbers}")
        for attempt_number in failed_numbers:
            details = attempt_failure_reasons(base_attempts[attempt_number - 1], "green")
            base_reasons.append(f"base attempt {attempt_number}: {'; '.join(details)}")
    if base_all_green:
        if any(signature is None for signature in base_junit_signatures):
            base_reasons.append(f"a normalized JUnit signature is missing: {base_junit_signatures}")
        elif len(set(base_junit_signatures)) != 1:
            base_reasons.append(f"five normalized JUnit signatures differed: {base_junit_signatures}")
        if any(signature is None for signature in base_leaf_signatures):
            base_reasons.append(f"a rich leaf nodeid/outcome signature is missing: {base_leaf_signatures}")
        elif len(set(base_leaf_signatures)) != 1:
            base_reasons.append(
                "five rich leaf nodeid/outcome signatures differed "
                f"(passed/xpassed and skipped/xfailed remain distinct): {base_leaf_signatures}"
            )
    base_check = {
        "outcome": "PASS" if base_deterministic else "FAIL",
        "passed": base_deterministic,
        "attempt_count": 5,
        "normalized_junit_signatures": base_junit_signatures,
        "normalized_leaf_outcome_signatures": base_leaf_signatures,
        "counts": [attempt.get("counts", {}) for attempt in base_attempts],
        "attempts": base_attempts,
        "failure_reasons": base_reasons,
    }
    if not base_deterministic:
        reason = "; ".join(base_reasons)
        return {
            "side": side_name,
            "parent": side_manifest["parent"],
            "source_patch": side_manifest["source_patch"],
            "test_patch": side_manifest["test_patch"],
            "focal_selection": focal,
            "checks": {
                "base_determinism": base_check,
                "red_test_patch_only": not_run_check(f"base determinism precondition failed: {reason}"),
                "green_source_and_test": not_run_check(f"base determinism precondition failed: {reason}"),
            },
            "validated": False,
            "failure_reasons": [f"base determinism: {reason}"],
        }

    overlay_leaves = focal["overlay_leaf_node_ids"]
    overlay_manifest = focal["overlay_test_file_manifest"]
    overlay_paths = side_manifest["test_paths"]
    red = run_pytest_state(
        mirror=mirror, worktree_root=worktree_root, base=base, label=f"{side_name}-red",
        spec=spec, targets=targets, patches=[test_patch], raw_root=raw_root / "red",
        protocol=protocol, expected_leaf_nodeids=overlay_leaves,
        expected_overlay_manifest=overlay_manifest, overlay_paths=overlay_paths,
    )
    red_passed = red.get("qualifying_red") is True
    red_check = {
        "outcome": "PASS" if red_passed else "FAIL",
        "passed": red_passed,
        "counts": red.get("counts", {}),
        "failing_node_ids": red.get("failing_nodeids", []),
        "attempt": red,
        "failure_reasons": [] if red_passed else attempt_failure_reasons(red, "red"),
    }
    red_failures = red.get("failing_nodeids", []) if red_passed else []
    green = run_pytest_state(
        mirror=mirror, worktree_root=worktree_root, base=base, label=f"{side_name}-green",
        spec=spec, targets=targets, patches=[source_patch, test_patch], raw_root=raw_root / "green",
        protocol=protocol, expected_leaf_nodeids=overlay_leaves,
        expected_overlay_manifest=overlay_manifest, overlay_paths=overlay_paths,
        required_pass_nodeids=red_failures,
    )
    green_passed = green.get("green") is True
    green_check = {
        "outcome": "PASS" if green_passed else "FAIL",
        "passed": green_passed,
        "counts": green.get("counts", {}),
        "attempt": green,
        "red_failing_node_outcomes": green.get("required_pass_outcomes", {}),
        "failure_reasons": [] if green_passed else attempt_failure_reasons(green, "green"),
    }
    failures: list[str] = []
    if not red_passed:
        failures.append("red test-patch-only check: " + "; ".join(red_check["failure_reasons"]))
    if not green_passed:
        failures.append("green source+test check: " + "; ".join(green_check["failure_reasons"]))
    return {
        "side": side_name,
        "parent": side_manifest["parent"],
        "source_patch": side_manifest["source_patch"],
        "test_patch": side_manifest["test_patch"],
        "focal_selection": focal,
        "checks": {
            "base_determinism": base_check,
            "red_test_patch_only": red_check,
            "green_source_and_test": green_check,
        },
        "validated": base_deterministic and red_passed and green_passed,
        "failure_reasons": failures,
    }


def joint_attempt_signature(record: Mapping[str, Any]) -> str | None:
    if not record.get("executed") or not all_patch_applications_ok(record.get("patch_applications", [])):
        return None
    if (
        record.get("timed_out") or record.get("launch_error")
        or record.get("internal_errors") or record.get("internal_launch_exception")
        or not record.get("environment_probe", {}).get("ok")
        or record.get("files_unchanged") is not True
        or record.get("overlay_files_match_frozen") is not True
    ):
        return None
    return record.get("normalized_attempt_signature_sha256")


def evaluate_joint_sources(
    *, spec: RepoSpec, site_manifest: Mapping[str, Any], sides: Mapping[str, Mapping[str, Any]],
    mirror: Path, worktree_root: Path, output_root: Path, raw_root: Path,
    protocol: Mapping[str, Any], site_validated: bool,
) -> dict[str, Any]:
    joint_manifest = site_manifest["joint_source"]
    if not joint_manifest.get("constructible"):
        return {
            "status": "NOT_CONSTRUCTIBLE_TEXTUAL_SOURCE_CONFLICT",
            "constructible": False,
            "source_conflict_paths": joint_manifest.get("source_conflict_paths", []),
            "reason": joint_manifest.get("reason") or "textual source conflict",
            "mutually_unsatisfiable": None,
            "attempts": {},
        }
    if not site_validated:
        return {
            "status": "NOT_RUN_SITE_NOT_VALIDATED",
            "constructible": True,
            "source_conflict_paths": [],
            "reason": "joint contradictory-task classification is restricted to independently validated tasks",
            "mutually_unsatisfiable": None,
            "attempts": {},
        }
    joint_patch = resolve_patch_record(joint_manifest["patch"], output_root)
    first: dict[str, dict[str, Any]] = {}
    for side_name in ("parent1", "parent2"):
        side = sides[side_name]
        prepared_side = site_manifest["sides"][side_name]
        test_patch = resolve_patch_record(prepared_side["test_patch"], output_root)
        focal = side["focal_selection"]
        red_failures = side["checks"]["red_test_patch_only"]["failing_node_ids"]
        first[side_name] = run_pytest_state(
            mirror=mirror, worktree_root=worktree_root, base=site_manifest["base"],
            label=f"joint-{side_name}-first", spec=spec,
            targets=focal["focal_collector_targets"], patches=[joint_patch, test_patch],
            raw_root=raw_root / side_name / "attempt-1", protocol=protocol,
            expected_leaf_nodeids=focal["overlay_leaf_node_ids"],
            expected_overlay_manifest=focal["overlay_test_file_manifest"],
            overlay_paths=prepared_side["test_paths"], required_pass_nodeids=red_failures,
        )
    if all(attempt.get("green") is True for attempt in first.values()):
        return {
            "status": "JOINTLY_SATISFIABLE",
            "constructible": True,
            "source_conflict_paths": [],
            "reason": "both focal sets passed with the exact joint source patch",
            "mutually_unsatisfiable": False,
            "rerun_triggered": False,
            "attempts": {name: [attempt] for name, attempt in first.items()},
            "counts": {name: [attempt.get("counts", {})] for name, attempt in first.items()},
        }

    second: dict[str, dict[str, Any]] = {}
    for side_name in ("parent1", "parent2"):
        side = sides[side_name]
        prepared_side = site_manifest["sides"][side_name]
        test_patch = resolve_patch_record(prepared_side["test_patch"], output_root)
        focal = side["focal_selection"]
        red_failures = side["checks"]["red_test_patch_only"]["failing_node_ids"]
        second[side_name] = run_pytest_state(
            mirror=mirror, worktree_root=worktree_root, base=site_manifest["base"],
            label=f"joint-{side_name}-rerun", spec=spec,
            targets=focal["focal_collector_targets"], patches=[joint_patch, test_patch],
            raw_root=raw_root / side_name / "attempt-2", protocol=protocol,
            expected_leaf_nodeids=focal["overlay_leaf_node_ids"],
            expected_overlay_manifest=focal["overlay_test_file_manifest"],
            overlay_paths=prepared_side["test_paths"], required_pass_nodeids=red_failures,
        )
    stability: dict[str, str] = {}
    for side_name in ("parent1", "parent2"):
        initial = first[side_name]
        rerun = second[side_name]
        if initial.get("green") and rerun.get("green"):
            first_signature = joint_attempt_signature(initial)
            second_signature = joint_attempt_signature(rerun)
            if first_signature is not None and first_signature == second_signature:
                stability[side_name] = "STABLE_GREEN"
            else:
                stability[side_name] = "UNVERIFIED_GREEN_RERUN_DISAGREED"
        elif initial.get("qualifying_red") and rerun.get("qualifying_red"):
            first_signature = joint_attempt_signature(initial)
            second_signature = joint_attempt_signature(rerun)
            if first_signature is not None and first_signature == second_signature:
                stability[side_name] = "STABLE_QUALIFYING_RED"
            else:
                stability[side_name] = "UNVERIFIED_RED_RERUN_DISAGREED"
        else:
            stability[side_name] = "UNVERIFIED_NON_TEST_FAILURE_OR_RERUN_DISAGREEMENT"
    verified_states = {"STABLE_GREEN", "STABLE_QUALIFYING_RED"}
    contradictory = bool(
        all(state in verified_states for state in stability.values())
        and "STABLE_QUALIFYING_RED" in stability.values()
    )
    return {
        "status": "MUTUALLY_UNSATISFIABLE" if contradictory else "UNVERIFIED_JOINT_OUTCOME",
        "constructible": True,
        "source_conflict_paths": [],
        "reason": (
            "at least one focal set had a stable qualifying test-level red under the exact joint sources"
            if contradictory else "the immediate identical rerun did not verify a stable joint outcome"
        ),
        "mutually_unsatisfiable": contradictory if contradictory else None,
        "rerun_triggered": True,
        "stability": stability,
        "attempts": {name: [first[name], second[name]] for name in first},
        "counts": {
            name: [first[name].get("counts", {}), second[name].get("counts", {})] for name in first
        },
    }


def validate_site(
    *, spec: RepoSpec, index: int, row: Mapping[str, Any], manifest: Mapping[str, Any],
    manifest_sha: str, paths: Mapping[str, Path], output_root: Path, run_root: Path,
    protocol: Mapping[str, Any], protocol_sha: str, runtime: Mapping[str, Any],
    runner_sha: str, worker_id: str, preparation_record: Mapping[str, Any],
) -> dict[str, Any]:
    sides: dict[str, dict[str, Any]] = {}
    for side_name in ("parent1", "parent2"):
        print(f"  SIDE {side_name}", flush=True)
        sides[side_name] = validate_side(
            spec=spec, side_name=side_name, site_manifest=manifest,
            mirror=paths["mirror"], worktree_root=paths["worktrees"], output_root=output_root,
            raw_root=run_root / "sides" / side_name, protocol=protocol, protocol_sha=protocol_sha,
        )
    site_validated = all(side["validated"] for side in sides.values())
    both_nonprobe_green = bool(
        all(side["checks"]["red_test_patch_only"].get("attempt", {}).get("green") is True for side in sides.values())
        and all(side["checks"]["green_source_and_test"].get("passed") is True for side in sides.values())
    )
    if site_validated:
        verdict = "VALIDATED"
        rejection_reasons: list[str] = []
    elif both_nonprobe_green:
        verdict = "REJECTED_NON_PROBE"
        rejection_reasons = [
            "both sides' test-patch-only focal sets were green and both source+test sets were green; no red discriminated either task"
        ]
    else:
        verdict = "REJECTED"
        rejection_reasons = [
            f"{side_name}: {reason}"
            for side_name, side in sides.items() for reason in side["failure_reasons"]
        ]
    joint = evaluate_joint_sources(
        spec=spec, site_manifest=manifest, sides=sides, mirror=paths["mirror"],
        worktree_root=paths["worktrees"], output_root=output_root,
        raw_root=run_root / "joint", protocol=protocol, site_validated=site_validated,
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "complete": True,
        "protocol_sha256": protocol_sha,
        "runner_sha256": runner_sha,
        "prepared_manifest_sha256": manifest_sha,
        "repository": spec.repository,
        "repo_slug": spec.slug,
        "site_index": index,
        "corpus_line": row["corpus_line"],
        "merge": row["merge"],
        "parents": row["parents"],
        "base": row["merge_base"],
        "worker_id": worker_id,
        "preparation_record": dict(preparation_record),
        "runtime_preflight": dict(runtime),
        "raw_run_root": display_path(run_root),
        "sides": sides,
        "joint_source_check": joint,
        "verdict": verdict,
        "validated": site_validated,
        "rejection_reasons": rejection_reasons,
        "completed_at_utc": utc_now(),
    }
    return result


def preparation_record_path(
    output_root: Path, spec: RepoSpec, worker_id: str, start: int, stop: int,
) -> Path:
    return output_root / "preparations" / spec.slug / worker_id / f"{start:03d}-{stop:03d}.json"


def completed_preparation_for_site(
    *, output_root: Path, spec: RepoSpec, worker_id: str, index: int, merge: str,
    protocol_sha: str, runner_sha: str, marker: Path, mirror: Path, manifest_sha: str,
) -> dict[str, Any]:
    root = output_root / "preparations" / spec.slug / worker_id
    marker_sha = sha256_file(marker)
    matches: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            try:
                payload = path.read_bytes()
                record = json.loads(payload)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            expected = {
                "schema_version": SCHEMA_VERSION,
                "complete": True,
                "protocol_sha256": protocol_sha,
                "runner_sha256_start": runner_sha,
                "runner_sha256_end": runner_sha,
                "runner_unchanged_at_end": True,
                "repository": spec.repository,
                "worker_id": worker_id,
                "worker_copy": str(mirror.resolve()),
                "worker_marker_sha256": marker_sha,
            }
            if any(record.get(key) != value for key, value in expected.items()):
                continue
            site_match = next(
                (
                    site for site in record.get("sites", [])
                    if site.get("index") == index and site.get("merge") == merge
                    and site.get("manifest_sha256") == manifest_sha
                ),
                None,
            )
            if site_match is not None:
                matches.append(
                    {
                        "path": display_path(path),
                        "sha256": sha256_bytes(payload),
                        "index_start": record.get("index_start"),
                        "index_stop": record.get("index_stop"),
                    }
                )
    if len(matches) != 1:
        raise RunnerError(
            f"{spec.repository} index {index} merge {merge}: expected exactly one completed "
            f"preparation record for this worker/runner/manifest, found {len(matches)}"
        )
    return matches[0]


def cmd_prepare(args: argparse.Namespace) -> int:
    runner_path = Path(__file__).resolve()
    runner_bytes = runner_path.read_bytes()
    runner_sha = sha256_bytes(runner_bytes)
    protocol, protocol_sha = load_protocol()
    spec = REPOSITORIES[args.repo]
    rows = load_sites(spec, protocol)
    selected = select_batch(rows, args.index_start, args.index_stop)
    actual_stop = len(rows) if args.index_stop is None else args.index_stop
    verify_direct_hierarchy(args.output_root, ["patches", spec.slug], "patch output")
    verify_direct_hierarchy(
        args.output_root, ["preparations", spec.slug, args.worker_id], "preparation output",
    )
    paths = prepare_physical_copy(spec, args.worker_id, args.scratch_root, protocol_sha)
    manifests: list[dict[str, Any]] = []
    for index, row in selected:
        print(f"PREPARE {spec.slug} index={index} merge={row['merge']}", flush=True)
        manifest = prepare_site(
            spec, index, row, paths, args.output_root, protocol_sha, runner_sha,
        )
        manifest_path = args.output_root / "patches" / spec.slug / row["merge"] / "manifest.json"
        manifests.append(
            {
                "index": manifest["site_index"],
                "merge": manifest["merge"],
                "manifest": display_path(manifest_path),
                "manifest_sha256": sha256_file(manifest_path),
            }
        )
    runner_sha_end = sha256_file(runner_path)
    if runner_sha_end != runner_sha:
        raise RunnerError(
            f"runner changed during prepare: startup {runner_sha}, end {runner_sha_end}; "
            "no completed preparation record was emitted"
        )
    record = {
        "schema_version": SCHEMA_VERSION,
        "complete": True,
        "protocol_sha256": protocol_sha,
        "runner_sha256_start": runner_sha,
        "runner_sha256_end": runner_sha_end,
        "runner_unchanged_at_end": True,
        "repository": spec.repository,
        "worker_id": args.worker_id,
        "worker_copy": str(paths["mirror"].resolve()),
        "worker_marker": display_path(paths["marker"]),
        "worker_marker_sha256": sha256_file(paths["marker"]),
        "index_start": args.index_start,
        "index_stop": actual_stop,
        "sites": manifests,
        "completed_at_utc": utc_now(),
    }
    record_path = preparation_record_path(
        args.output_root, spec, args.worker_id, args.index_start, actual_stop,
    )
    write_once(
        record_path,
        (json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8"),
    )
    final_runner_sha = sha256_file(runner_path)
    if final_runner_sha != runner_sha:
        raise RunnerError(
            f"runner changed while publishing preparation record: startup {runner_sha}, final {final_runner_sha}"
        )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    runner_path = Path(__file__).resolve()
    runner_bytes = runner_path.read_bytes()
    runner_sha = sha256_bytes(runner_bytes)
    protocol, protocol_sha = load_protocol()
    spec = REPOSITORIES[args.repo]
    rows = load_sites(spec, protocol)
    selected = select_batch(rows, args.index_start, args.index_stop)
    actual_stop = len(rows) if args.index_stop is None else args.index_stop
    verify_direct_hierarchy(args.output_root, ["patches", spec.slug], "patch input")
    verify_direct_hierarchy(
        args.output_root, ["preparations", spec.slug, args.worker_id], "preparation input",
    )
    verify_direct_hierarchy(args.output_root, ["raw", spec.slug], "raw output")
    paths = worker_paths(spec, args.worker_id, args.scratch_root)
    if not paths["marker"].is_file() or not paths["mirror"].is_dir():
        raise RunnerError("run requires a completed physical-copy prepare phase")
    try:
        marker = json.loads(paths["marker"].read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RunnerError(f"worker marker is unreadable or invalid: {paths['marker']}: {error}") from error
    expected_marker = {
        "schema_version": SCHEMA_VERSION,
        "repository": spec.repository,
        "worker_id": args.worker_id,
        "protocol_sha256": protocol_sha,
        "owned_mirror": str(paths["mirror"].resolve()),
        "physical_copy": True,
    }
    marker_mismatches = {
        key: {"expected": value, "observed": marker.get(key)}
        for key, value in expected_marker.items() if marker.get(key) != value
    }
    if marker_mismatches:
        raise RunnerError(f"worker marker failed run preflight: {marker_mismatches}")
    # This is deliberately before the batch loop, including an empty batch.
    batch_runtime_preflight = verify_runtime(protocol)
    completed: list[dict[str, Any]] = []
    resumed: list[dict[str, Any]] = []
    apparatus_errors: list[dict[str, Any]] = []
    pending_publications: list[tuple[Path, bytes, dict[str, Any]]] = []
    for index, row in selected:
        merge = row["merge"]
        verify_direct_hierarchy(args.output_root, ["raw", spec.slug, merge], "site raw output")
        final_path = args.output_root / "raw" / spec.slug / merge / "result.json"
        try:
            manifest, manifest_sha = load_prepared_site(
                spec, index, row, args.output_root, protocol_sha, runner_sha,
            )
            preparation = completed_preparation_for_site(
                output_root=args.output_root, spec=spec, worker_id=args.worker_id,
                index=index, merge=merge, protocol_sha=protocol_sha, runner_sha=runner_sha,
                marker=paths["marker"], mirror=paths["mirror"], manifest_sha=manifest_sha,
            )
            if final_path.is_file():
                try:
                    existing = json.loads(final_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RunnerError(f"existing result is unreadable or invalid: {final_path}: {error}") from error
                resume_expected = {
                    "complete": True,
                    "protocol_sha256": protocol_sha,
                    "runner_sha256": runner_sha,
                    "prepared_manifest_sha256": manifest_sha,
                    "repository": spec.repository,
                    "merge": merge,
                    "site_index": index,
                    "worker_id": args.worker_id,
                }
                resume_mismatches = {
                    key: {"expected": value, "observed": existing.get(key)}
                    for key, value in resume_expected.items() if existing.get(key) != value
                }
                if resume_mismatches:
                    raise RunnerError(
                        f"existing result is not resumable under this apparatus: {resume_mismatches}"
                    )
                if not args.resume:
                    raise RunnerError(f"completed result exists and --no-resume was requested: {final_path}")
                print(f"RESUME {spec.slug} index={index} merge={merge} verdict={existing.get('verdict')}", flush=True)
                resumed.append({"index": index, "merge": merge, "verdict": existing.get("verdict")})
                continue
            verify_direct_hierarchy(
                args.output_root, ["raw", spec.slug, merge, "runs"], "site run output",
            )
            site_runtime_preflight = verify_runtime(protocol)
            if site_runtime_preflight != batch_runtime_preflight:
                raise RunnerError("runtime integrity differs between batch and site preflight")
            # Keep every child-facing evidence path below the classic Windows
            # path boundary. Full worker/runner/time provenance is stored in
            # the result body, so the directory token only needs uniqueness.
            run_id = f"r{time.time_ns():x}"
            run_root = args.output_root / "raw" / spec.slug / merge / "runs" / run_id
            run_root.mkdir(parents=True, exist_ok=False)
            print(f"RUN {spec.slug} index={index} merge={merge}", flush=True)
            result = validate_site(
                spec=spec, index=index, row=row, manifest=manifest, manifest_sha=manifest_sha,
                paths=paths, output_root=args.output_root, run_root=run_root,
                protocol=protocol, protocol_sha=protocol_sha, runtime=site_runtime_preflight,
                runner_sha=runner_sha, worker_id=args.worker_id, preparation_record=preparation,
            )
            site_runtime_postflight = verify_runtime(protocol)
            if site_runtime_postflight != site_runtime_preflight:
                raise RunnerError(
                    "runtime integrity drifted during site execution; raw attempts were retained but "
                    "no generation/final result was published"
                )
            result["runtime_postflight"] = site_runtime_postflight
            observed_runner_sha = sha256_file(runner_path)
            if observed_runner_sha != runner_sha:
                raise RunnerError(
                    f"runner changed during site execution: startup {runner_sha}, observed {observed_runner_sha}; "
                    "the generation was retained but no final result was published"
                )
            final_payload = (
                json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
            ).encode("utf-8")
            atomic_bytes(run_root / "site-result.json", final_payload)
            pending_publications.append(
                (final_path, final_payload, {"index": index, "merge": merge, "verdict": result["verdict"]})
            )
        except RunnerError as error:
            apparatus_errors.append({"index": index, "merge": merge, "error": str(error)})
            print(f"APPARATUS_ERROR {spec.slug} index={index} merge={merge}: {error}", file=sys.stderr, flush=True)
            break
    batch_runtime_postflight = verify_runtime(protocol)
    if batch_runtime_postflight != batch_runtime_preflight:
        raise RunnerError(
            "runtime integrity drifted during the batch; staged generation evidence was retained but "
            "no new final result was published"
        )
    runner_sha_end = sha256_file(runner_path)
    if runner_sha_end != runner_sha:
        raise RunnerError(f"runner changed during run: startup {runner_sha}, end {runner_sha_end}")
    if not apparatus_errors:
        for final_path, final_payload, summary in pending_publications:
            write_once(final_path, final_payload)
            completed.append(summary)
            print(
                f"RESULT {spec.slug} index={summary['index']} merge={summary['merge']} "
                f"verdict={summary['verdict']}",
                flush=True,
            )
    elif completed:
        raise RunnerError("incomplete batch unexpectedly published a top-level site result")
    batch = {
        "schema_version": SCHEMA_VERSION,
        "complete": not apparatus_errors,
        "protocol_sha256": protocol_sha,
        "runner_sha256_start": runner_sha,
        "runner_sha256_end": runner_sha_end,
        "runtime_preflight": batch_runtime_preflight,
        "runtime_postflight": batch_runtime_postflight,
        "repository": spec.repository,
        "worker_id": args.worker_id,
        "index_start": args.index_start,
        "index_stop": actual_stop,
        "completed": completed,
        "staged_not_published": (
            [summary for _, _, summary in pending_publications] if apparatus_errors else []
        ),
        "resumed": resumed,
        "apparatus_errors": apparatus_errors,
        "completed_at_utc": utc_now(),
    }
    verify_direct_hierarchy(args.output_root, ["raw", spec.slug, "_batches"], "batch raw output")
    batch_root = args.output_root / "raw" / spec.slug / "_batches"
    batch_path = batch_root / (
        f"{args.worker_id}-{args.index_start:03d}-{actual_stop:03d}-{time.time_ns()}.json"
    )
    write_once(
        batch_path,
        (json.dumps(batch, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8"),
    )
    return 2 if apparatus_errors else 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    subparsers = value.add_subparsers(dest="command", required=True)
    for command, function in (("prepare", cmd_prepare), ("run", cmd_run)):
        sub = subparsers.add_parser(command)
        sub.add_argument("--repo", required=True, choices=sorted(REPOSITORIES))
        sub.add_argument("--worker-id", required=True)
        sub.add_argument("--index-start", type=int, default=0, help="inclusive zero-based census index")
        sub.add_argument("--index-stop", type=int, help="exclusive zero-based census index")
        sub.add_argument("--scratch-root", type=Path, default=DEFAULT_SCRATCH_ROOT)
        sub.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
        if command == "run":
            sub.add_argument(
                "--resume", action=argparse.BooleanOptionalAction, default=True,
                help="reuse exact completed site results (default); --no-resume fails closed",
            )
        sub.set_defaults(function=function)
    internal = subparsers.add_parser("internal-pytest", help=argparse.SUPPRESS)
    internal.add_argument("--output", type=Path, required=True)
    internal.add_argument("pytest_args", nargs=argparse.REMAINDER)
    internal.set_defaults(function=cmd_internal_pytest)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "internal-pytest":
            return int(args.function(args))
        args.scratch_root = args.scratch_root.resolve()
        args.output_root = args.output_root.resolve()
        if args.output_root != DEFAULT_OUTPUT_ROOT.resolve():
            raise RunnerError(
                f"output root is not the frozen exploratory/arms root: {args.output_root}"
            )
        if args.scratch_root != DEFAULT_SCRATCH_ROOT.resolve():
            raise RunnerError(
                f"scratch root is not the frozen task scratch root: {args.scratch_root}"
            )
        return int(args.function(args))
    except RunnerError as error:
        print(f"validate_sites.py: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
