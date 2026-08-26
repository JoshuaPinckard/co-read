#!/usr/bin/env python3
"""Render the frozen Python arms-ladder site-validation results.

This is a read-only reducer until its final, write-once publication step.  It
does not import the validation runner, invoke Git, open a corpus mirror, or run
pytest.  It refuses to publish unless the exact Click/Pygments census,
preparations, patches, manifests, and complete raw results reconcile under one
frozen protocol and runner hash.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARMS_ROOT = PROJECT_ROOT / "exploratory" / "arms"
PROTOCOL_PATH = ARMS_ROOT / "protocol.json"
RUNNER_PATH = ARMS_ROOT / "validate_sites.py"
MARKDOWN_PATH = ARMS_ROOT / "SITES.md"
JSON_PATH = ARMS_ROOT / "sites.json"
SCRATCH_ROOT = PROJECT_ROOT.parent / f"{PROJECT_ROOT.name}-arms-scratch"
FROZEN_PROTOCOL_SHA256 = "fb1c0a9f9c7b48c30178d8a7e737250e18535ada63959c49c73c180505c69828"
FROZEN_RUNNER_SHA256 = "e04bc99409e584636ce8cb81cc22f6a7ca9c917f9ec9e46ab68a4c2328b6b6c6"

IGNORED_RAW_ADMIN_NAMES = frozenset({"_batches", "_invalidated", "_quarantine"})
IGNORED_RAW_ADMIN_PREFIXES = ("_invalidated-", "_quarantine-")

PRIMARY_VERDICTS = ("VALIDATED", "REJECTED_NON_PROBE", "REJECTED")
JOINT_STATUSES = (
    "JOINTLY_SATISFIABLE",
    "MUTUALLY_UNSATISFIABLE",
    "UNVERIFIED_JOINT_OUTCOME",
    "NOT_CONSTRUCTIBLE_TEXTUAL_SOURCE_CONFLICT",
    "NOT_RUN_SITE_NOT_VALIDATED",
)
CHECK_NAMES = ("base_determinism", "red_test_patch_only", "green_source_and_test")
SIDE_NAMES = ("parent1", "parent2")
HEX_16_RE = re.compile(r"^[0-9a-f]{16}$")
HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class RepoSpec:
    repository: str
    slug: str
    expected_sites: int

    @property
    def corpus(self) -> Path:
        return PROJECT_ROOT / "corpus" / "conflicts" / f"{self.slug}.jsonl"


REPOSITORIES = (
    RepoSpec("pallets/click", "pallets__click", 24),
    RepoSpec("pygments/pygments", "pygments__pygments", 2),
)


class RenderError(RuntimeError):
    """A completeness, provenance, schema, or write-once assertion failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RenderError(message)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RenderError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def decode_json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RenderError(f"invalid JSON in {label}: {error}") from error


def read_json(path: Path) -> tuple[dict[str, Any], bytes]:
    require(path.is_file(), f"required JSON file is absent: {path}")
    payload = path.read_bytes()
    value = decode_json(payload, str(path))
    require(isinstance(value, dict), f"expected a JSON object: {path}")
    return value, payload


def as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    require(isinstance(value, dict), f"{label} must be an object")
    return value


def as_list(value: Any, label: str) -> list[Any]:
    require(isinstance(value, list), f"{label} must be an array")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def canonical_value_sha256(value: Any) -> str:
    return sha256_bytes(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    )


def normalized_node_id(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def project_relative(path: Path) -> str:
    resolved = path.resolve()
    root = PROJECT_ROOT.resolve()
    require(resolved == root or root in resolved.parents, f"artifact is outside project: {resolved}")
    return resolved.relative_to(root).as_posix()


def resolve_recorded_path(value: Any, allowed_root: Path, label: str) -> Path:
    require(isinstance(value, str) and value, f"{label} path must be a nonempty string")
    require("\\" not in value, f"{label} path is not slash-normalized: {value!r}")
    pure = PurePosixPath(value)
    require(not pure.is_absolute() and ".." not in pure.parts, f"unsafe {label} path: {value!r}")
    candidate = PROJECT_ROOT.joinpath(*pure.parts).resolve()
    allowed = allowed_root.resolve()
    require(candidate != allowed and allowed in candidate.parents, f"{label} escapes {allowed_root}: {value}")
    return candidate


def verify_file_record(
    record_value: Any, expected_path: Path, allowed_root: Path, label: str,
) -> dict[str, Any]:
    record = dict(as_mapping(record_value, label))
    observed_path = resolve_recorded_path(record.get("path"), allowed_root, label)
    require(observed_path == expected_path.resolve(), f"{label} path mismatch: {observed_path} != {expected_path}")
    require(observed_path.is_file(), f"{label} file is absent: {observed_path}")
    payload = observed_path.read_bytes()
    require(record.get("bytes") == len(payload), f"{label} byte count mismatch")
    require(record.get("sha256") == sha256_bytes(payload), f"{label} SHA-256 mismatch")
    require(record.get("path") == project_relative(expected_path), f"{label} path is not canonical")
    return record


def verify_all_hashed_file_records(value: Any, label: str, seen: set[tuple[str, str, int]]) -> None:
    if isinstance(value, dict):
        if (
            isinstance(value.get("path"), str)
            and isinstance(value.get("sha256"), str)
            and HEX_64_RE.fullmatch(value["sha256"]) is not None
            and type(value.get("bytes")) is int
            and value["bytes"] >= 0
        ):
            identity = (value["path"], value["sha256"], value["bytes"])
            if identity not in seen:
                path = resolve_recorded_path(value["path"], PROJECT_ROOT, f"{label} hashed file")
                require(path.is_file(), f"{label} hashed file is absent: {path}")
                payload = path.read_bytes()
                require(len(payload) == value["bytes"], f"{label} hashed file byte count changed: {path}")
                require(sha256_bytes(payload) == value["sha256"], f"{label} hashed file changed: {path}")
                seen.add(identity)
        for key, child in value.items():
            verify_all_hashed_file_records(child, f"{label}.{key}", seen)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            verify_all_hashed_file_records(child, f"{label}[{index}]", seen)


def load_protocol() -> tuple[dict[str, Any], str]:
    protocol, payload = read_json(PROTOCOL_PATH)
    protocol_sha = sha256_bytes(payload)
    require(protocol_sha == FROZEN_PROTOCOL_SHA256, "frozen protocol SHA-256 mismatch")
    require(protocol.get("schema_version") == SCHEMA_VERSION, "unsupported protocol schema")
    require(protocol.get("frozen_before_candidate_pytest") is True, "protocol was not outcome-frozen")
    population = as_mapping(protocol.get("population"), "protocol.population")
    for spec in REPOSITORIES:
        require(population.get(spec.repository) == spec.expected_sites, f"protocol census mismatch for {spec.repository}")
    apparatus = as_mapping(protocol.get("apparatus_path_policy"), "protocol.apparatus_path_policy")
    require(
        set(apparatus) == {
            "execution_temp", "windows_temp_path_budget", "windows_child_path_budget",
            "child_path_gate", "compact_paths", "batch_publication",
        },
        "protocol apparatus-path policy keys mismatch",
    )
    require(
        type(apparatus.get("windows_temp_path_budget")) is int
        and apparatus["windows_temp_path_budget"] == 160,
        "protocol Windows temporary-path budget mismatch",
    )
    require(
        type(apparatus.get("windows_child_path_budget")) is int
        and apparatus["windows_child_path_budget"] == 240,
        "protocol Windows child-path budget mismatch",
    )
    environment = as_mapping(protocol.get("environment"), "protocol.environment")
    policy = as_mapping(environment.get("policy"), "protocol.environment.policy")
    require(
        policy.get("PYTEST_DEBUG_TEMPROOT") == "removed"
        and policy.get("TEMP/TMP/TMPDIR")
        == "the same per-process worker-owned isolated short temporary root",
        "protocol isolated-temporary environment policy mismatch",
    )
    return protocol, protocol_sha


def load_census(spec: RepoSpec) -> list[dict[str, Any]]:
    require(spec.corpus.is_file(), f"canonical census is absent: {spec.corpus}")
    rows: list[dict[str, Any]] = []
    with spec.corpus.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            value = decode_json(raw_line, f"{spec.corpus}:{line_number}")
            require(isinstance(value, dict), f"census row is not an object: {spec.corpus}:{line_number}")
            if value.get("evaluation_status") == "conflicted" and value.get("both_sides_touched_tests") is True:
                row = dict(value)
                row["corpus_line"] = line_number
                rows.append(row)
    require(len(rows) == spec.expected_sites, f"{spec.repository}: expected {spec.expected_sites} census rows, found {len(rows)}")
    merges = [row.get("merge") for row in rows]
    require(all(isinstance(merge, str) and len(merge) == 40 for merge in merges), f"{spec.repository}: invalid merge identity")
    require(len(set(merges)) == len(merges), f"{spec.repository}: duplicate census merge")
    return rows


def validate_preparation(
    result: Mapping[str, Any], spec: RepoSpec, row: Mapping[str, Any],
    protocol_sha: str, runner_sha: str, manifest_sha: str,
) -> dict[str, Any]:
    reference = as_mapping(result.get("preparation_record"), "result.preparation_record")
    path = resolve_recorded_path(reference.get("path"), ARMS_ROOT / "preparations", "preparation record")
    preparation, payload = read_json(path)
    require(reference.get("sha256") == sha256_bytes(payload), "preparation record SHA-256 mismatch")
    expected = {
        "schema_version": SCHEMA_VERSION,
        "complete": True,
        "protocol_sha256": protocol_sha,
        "runner_sha256_start": runner_sha,
        "runner_sha256_end": runner_sha,
        "runner_unchanged_at_end": True,
        "repository": spec.repository,
        "worker_id": result.get("worker_id"),
    }
    for key, expected_value in expected.items():
        require(preparation.get(key) == expected_value, f"preparation {key} mismatch for {row['merge']}")
    matches = [
        site for site in as_list(preparation.get("sites"), "preparation.sites")
        if isinstance(site, dict)
        and site.get("index") == result.get("site_index")
        and site.get("merge") == row.get("merge")
        and site.get("manifest_sha256") == manifest_sha
        and site.get("manifest") == project_relative(
            ARMS_ROOT / "patches" / spec.slug / str(row["merge"]) / "manifest.json"
        )
    ]
    require(len(matches) == 1, f"preparation does not uniquely cover {row['merge']}")
    require(reference.get("path") == project_relative(path), "preparation path is not canonical")
    require(reference.get("index_start") == preparation.get("index_start"), "preparation start index mismatch")
    require(reference.get("index_stop") == preparation.get("index_stop"), "preparation stop index mismatch")
    return {"path": project_relative(path), "sha256": sha256_bytes(payload)}


def validate_manifest(
    spec: RepoSpec, index: int, row: Mapping[str, Any], protocol_sha: str, runner_sha: str,
) -> tuple[dict[str, Any], str]:
    patch_root = ARMS_ROOT / "patches" / spec.slug / str(row["merge"])
    manifest_path = patch_root / "manifest.json"
    manifest, payload = read_json(manifest_path)
    manifest_sha = sha256_bytes(payload)
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
    for key, expected_value in expected.items():
        require(manifest.get(key) == expected_value, f"manifest {key} mismatch for {row['merge']}")

    sides = as_mapping(manifest.get("sides"), "manifest.sides")
    require(set(sides) == set(SIDE_NAMES), f"manifest side keys mismatch for {row['merge']}")
    for side_number, side_name in enumerate(SIDE_NAMES):
        side = as_mapping(sides[side_name], f"manifest.{side_name}")
        tests = sorted(set(as_list(side.get("test_paths"), f"manifest.{side_name}.test_paths")))
        canonical_tests = sorted(set(row["diffs"][side_name]["test_files"]))
        changed = sorted(set(as_list(side.get("changed_paths"), f"manifest.{side_name}.changed_paths")))
        sources = sorted(set(as_list(side.get("source_paths"), f"manifest.{side_name}.source_paths")))
        require(side.get("parent") == row["parents"][side_number], f"{side_name} parent mismatch")
        require(tests == canonical_tests and tests, f"{side_name} test split mismatch")
        require(set(tests).issubset(changed), f"{side_name} tests are not changed-path subset")
        require(sources == sorted(set(changed) - set(tests)) and sources, f"{side_name} source split mismatch")
        verify_file_record(
            side.get("source_patch"), patch_root / f"{side_name}-source.patch",
            ARMS_ROOT / "patches", f"{side_name} source patch",
        )
        verify_file_record(
            side.get("test_patch"), patch_root / f"{side_name}-test.patch",
            ARMS_ROOT / "patches", f"{side_name} test patch",
        )

    joint = as_mapping(manifest.get("joint_source"), "manifest.joint_source")
    source_union = sorted(set(sides["parent1"]["source_paths"]) | set(sides["parent2"]["source_paths"]))
    source_conflicts = sorted(set(row["conflicted_paths"]) & set(source_union))
    require(joint.get("source_paths") == source_union, "joint source-union mismatch")
    require(joint.get("source_conflict_paths") == source_conflicts, "joint source-conflict mismatch")
    require(
        isinstance(joint.get("constructible"), bool)
        and joint.get("constructible") == (not bool(source_conflicts)),
        "joint constructibility mismatch",
    )
    if source_conflicts:
        require(joint.get("patch") is None, "textually conflicting joint source unexpectedly has a patch")
    else:
        verify_file_record(
            joint.get("patch"), patch_root / "joint-source.patch",
            ARMS_ROOT / "patches", "joint source patch",
        )
    return manifest, manifest_sha


def verify_embedded_json(
    value: Any, expected_path: Path, allowed_root: Path, label: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    embedded = dict(as_mapping(value, label))
    require(not expected_path.is_symlink(), f"{label} JSON is a symlink: {expected_path}")
    lexical = Path(os.path.abspath(expected_path))
    path = expected_path.resolve()
    require(
        os.path.normcase(str(path)) == os.path.normcase(str(lexical)),
        f"{label} JSON ancestry is redirected: {expected_path} -> {path}",
    )
    allowed = allowed_root.resolve()
    require(path != allowed and allowed in path.parents, f"{label} path escapes {allowed_root}: {path}")
    require(path.parent == expected_path.parent.resolve(), f"{label} path is redirected: {expected_path}")
    observed, payload = read_json(path)
    require(observed == embedded, f"{label} raw/embedded JSON mismatch")
    return embedded, {
        "path": project_relative(path),
        "sha256": sha256_bytes(payload),
        "bytes": len(payload),
    }


def expected_worker_root(spec: RepoSpec, worker_id: str) -> Path:
    require(isinstance(worker_id, str) and worker_id, "result worker id must be a nonempty string")
    repo_root = (SCRATCH_ROOT / spec.slug).resolve()
    worker_root = (repo_root / worker_id).resolve()
    require(worker_root.parent == repo_root, f"worker id escapes scratch repository root: {worker_id!r}")
    return worker_root


def validate_temp_evidence(
    value: Any, *, attempt_root: Path, spec: RepoSpec, worker_id: str,
    protocol: Mapping[str, Any], label: str,
) -> dict[str, Any]:
    evidence, evidence_file = verify_embedded_json(
        value, attempt_root / "temp-evidence.json", ARMS_ROOT / "raw", f"{label}.temp_evidence",
    )
    require(
        set(evidence) == {
            "policy", "path", "path_length", "path_budget", "token",
            "attempt_root_sha256", "initially_empty", "present_before_cleanup",
            "manifest_before_cleanup", "cleanup_ok", "cleanup_error", "body_error",
        },
        f"{label} temporary evidence keys mismatch",
    )
    apparatus = as_mapping(protocol.get("apparatus_path_policy"), "protocol.apparatus_path_policy")
    budget = apparatus["windows_temp_path_budget"]
    token = evidence.get("token")
    attempt_sha = evidence.get("attempt_root_sha256")
    require(evidence.get("policy") == "worker-short-isolated-temp-v1", f"{label} temporary policy mismatch")
    require(isinstance(token, str) and HEX_16_RE.fullmatch(token) is not None, f"{label} temporary token is invalid")
    require(isinstance(attempt_sha, str) and HEX_64_RE.fullmatch(attempt_sha) is not None, f"{label} attempt-root hash is invalid")
    expected_attempt_sha = sha256_bytes(str(attempt_root.resolve()).encode("utf-8"))
    require(attempt_sha == expected_attempt_sha and token == expected_attempt_sha[:16], f"{label} temporary token/root hash mismatch")
    raw_path = evidence.get("path")
    require(isinstance(raw_path, str) and raw_path, f"{label} temporary path is absent")
    temporary = Path(raw_path)
    require(temporary.is_absolute(), f"{label} temporary path is not absolute")
    resolved_temporary = temporary.resolve()
    require(raw_path == str(resolved_temporary), f"{label} temporary path is not canonical")
    worker_root = expected_worker_root(spec, worker_id)
    temp_parent = (worker_root / "t").resolve()
    require(temp_parent.parent == worker_root, f"{label} worker temporary parent is redirected")
    expected_temporary = (temp_parent / token).resolve()
    require(expected_temporary.parent == temp_parent, f"{label} temporary token path is redirected")
    require(resolved_temporary == expected_temporary, f"{label} temporary path is outside its worker root")
    require(
        type(evidence.get("path_length")) is int
        and evidence["path_length"] == len(raw_path)
        and evidence["path_length"] <= budget,
        f"{label} temporary path length/budget mismatch",
    )
    require(evidence.get("path_budget") == budget, f"{label} temporary budget mismatch")
    require(evidence.get("initially_empty") is True, f"{label} temporary root was not initially empty")
    require(type(evidence.get("present_before_cleanup")) is bool, f"{label} pre-cleanup presence flag is invalid")
    manifest = as_mapping(evidence.get("manifest_before_cleanup"), f"{label}.manifest_before_cleanup")
    require(set(manifest) == {"file_count", "logical_bytes", "manifest_sha256"}, f"{label} temporary manifest keys mismatch")
    require(
        type(manifest.get("file_count")) is int and manifest["file_count"] >= 0
        and type(manifest.get("logical_bytes")) is int and manifest["logical_bytes"] >= 0
        and isinstance(manifest.get("manifest_sha256"), str)
        and HEX_64_RE.fullmatch(manifest["manifest_sha256"]) is not None,
        f"{label} temporary manifest is invalid",
    )
    if manifest["file_count"] == 0:
        require(
            manifest["logical_bytes"] == 0
            and manifest["manifest_sha256"] == sha256_bytes(b"[]"),
            f"{label} empty temporary manifest mismatch",
        )
    if evidence["present_before_cleanup"] is False:
        require(manifest["file_count"] == 0, f"{label} absent temporary root has a nonempty manifest")
    require(
        evidence.get("cleanup_ok") is True and evidence.get("cleanup_error") is None
        and evidence.get("body_error") is None,
        f"{label} temporary cleanup/body evidence is not clean",
    )
    require(
        not resolved_temporary.exists() and not resolved_temporary.is_symlink(),
        f"{label} temporary root survived cleanup: {resolved_temporary}",
    )
    result = dict(evidence)
    result["evidence_file"] = evidence_file
    return result


def attempt_worktree(source_root_value: Any, spec: RepoSpec, worker_id: str, label: str) -> tuple[Path, Path]:
    require(isinstance(source_root_value, str) and source_root_value, f"{label} source root is absent")
    source_root = Path(source_root_value)
    require(source_root.is_absolute(), f"{label} source root is not absolute")
    resolved_source = source_root.resolve()
    require(source_root_value == str(resolved_source), f"{label} source root is not canonical")
    worktree = resolved_source.parent if resolved_source.name == "src" else resolved_source
    worker_root = expected_worker_root(spec, worker_id)
    worktrees = (worker_root / "worktrees").resolve()
    require(worktrees.parent == worker_root, f"{label} worktree parent is redirected")
    require(worktree.parent == worktrees, f"{label} worktree is outside its worker root")
    require(resolved_source in {worktree, (worktree / 'src').resolve()}, f"{label} source/worktree relation is invalid")
    return resolved_source, worktree


def safe_target_path(worktree: Path, target: str, label: str) -> Path:
    normalized = normalized_node_id(target).split("::", 1)[0]
    pure = PurePosixPath(normalized)
    require(normalized and not pure.is_absolute() and ".." not in pure.parts, f"unsafe {label} target: {target!r}")
    target_path = worktree.joinpath(*pure.parts).resolve()
    require(target_path != worktree and worktree in target_path.parents, f"{label} target escapes worktree: {target!r}")
    return target_path


def validate_child_path_budget(
    value: Any, *, kind: str, targets: Sequence[str], attempt_root: Path,
    source_root_value: Any, temp_evidence: Mapping[str, Any], spec: RepoSpec,
    worker_id: str, protocol: Mapping[str, Any], label: str,
) -> dict[str, Any]:
    record = dict(as_mapping(value, f"{label}.child_path_budget"))
    require(set(record) == {"policy", "limit", "lengths", "longest", "ok"}, f"{label} child-path record keys mismatch")
    limit = as_mapping(protocol["apparatus_path_policy"], "protocol.apparatus_path_policy")[
        "windows_child_path_budget"
    ]
    require(record.get("policy") == "windows-child-path-budget-v1", f"{label} child-path policy mismatch")
    require(record.get("limit") == limit, f"{label} child-path limit mismatch")
    source_root, worktree = attempt_worktree(source_root_value, spec, worker_id, label)
    expected_paths: dict[str, Path] = {
        "python": Path(protocol["environment"]["python"]),
        "source_root": source_root,
        "temporary_root": Path(str(temp_evidence["path"])),
        "worktree": worktree,
    }
    if kind in {"collection", "pytest"}:
        expected_paths.update({"runner": RUNNER_PATH, "leaf_record": attempt_root / "leaf-record.json"})
        for ordinal, target in enumerate(targets, 1):
            expected_paths[f"target_{ordinal}"] = safe_target_path(worktree, target, label)
    if kind == "pytest":
        expected_paths["junit"] = attempt_root / "junit.xml"
    require(kind in {"environment_probe", "collection", "pytest"}, f"{label} has unsupported child kind {kind!r}")
    expected_lengths = {name: len(str(path.resolve())) for name, path in expected_paths.items()}
    lengths = as_mapping(record.get("lengths"), f"{label}.child_path_budget.lengths")
    require(
        all(isinstance(name, str) and type(length) is int and length >= 0 for name, length in lengths.items()),
        f"{label} child-path lengths are invalid",
    )
    require(dict(lengths) == expected_lengths, f"{label} child-path lengths mismatch")
    longest = max(expected_lengths.values(), default=0)
    require(record.get("longest") == longest, f"{label} longest child path mismatch")
    require(record.get("ok") is True and longest <= limit, f"{label} child-path gate did not pass")
    return record


def validate_environment_probe(
    value: Any, *, probe_root: Path, attempt_source_root: Any, spec: RepoSpec,
    worker_id: str, protocol: Mapping[str, Any], label: str,
) -> dict[str, Any]:
    probe, raw_result = verify_embedded_json(
        value, probe_root / "result.json", ARMS_ROOT / "raw", f"{label}.environment_probe",
    )
    require(probe.get("timed_out") is False, f"{label} published environment probe timed out")
    require(probe.get("launch_error") is None, f"{label} published environment probe had a launch error")
    require(probe.get("source_root") == attempt_source_root, f"{label} environment-probe source root mismatch")
    temp = validate_temp_evidence(
        probe.get("temp_evidence"), attempt_root=probe_root, spec=spec, worker_id=worker_id,
        protocol=protocol, label=f"{label}.environment_probe",
    )
    budget = validate_child_path_budget(
        probe.get("child_path_budget"), kind="environment_probe", targets=[],
        attempt_root=probe_root, source_root_value=probe.get("source_root"), temp_evidence=temp,
        spec=spec, worker_id=worker_id, protocol=protocol,
        label=f"{label}.environment_probe",
    )
    for stream_name in ("stdout", "stderr"):
        expected = (probe_root / f"{stream_name}.txt").resolve()
        require(expected.is_file(), f"{label} environment-probe {stream_name} is absent")
        require(probe.get(stream_name) == project_relative(expected), f"{label} environment-probe {stream_name} path mismatch")
    stdout_payload = (probe_root / "stdout.txt").read_bytes()
    try:
        decoded_payload = json.loads(stdout_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded_payload = None
    require(decoded_payload == probe.get("payload"), f"{label} environment-probe stdout/payload mismatch")
    payload = as_mapping(probe.get("payload"), f"{label}.environment_probe.payload")
    require(payload.get("module") == spec.slug.split("__", 1)[-1], f"{label} environment-probe module mismatch")
    source_root, worktree = attempt_worktree(probe.get("source_root"), spec, worker_id, label)
    module_file = payload.get("module_file")
    require(isinstance(module_file, str) and Path(module_file).is_absolute(), f"{label} module path is invalid")
    resolved_module = Path(module_file).resolve()
    expected_module_inside = resolved_module == worktree or worktree in resolved_module.parents
    require(
        probe.get("module_inside_attempt") is expected_module_inside,
        f"{label} environment-probe module containment flag mismatch",
    )
    expected_compat_exact = True
    if spec.slug == "pallets__click":
        compat = (
            PROJECT_ROOT / str(protocol["environment"]["click_compat_root"]) / "sitecustomize.py"
        ).resolve()
        sitecustomize_file = payload.get("sitecustomize_file")
        expected_compat_exact = bool(sitecustomize_file) and Path(str(sitecustomize_file)).resolve() == compat
    require(
        probe.get("click_compat_exact") is expected_compat_exact,
        f"{label} environment-probe compatibility flag mismatch",
    )
    expected_ok = bool(
        probe.get("exit_code") == 0 and probe.get("timed_out") is False
        and probe.get("launch_error") is None and expected_module_inside and expected_compat_exact
    )
    require(probe.get("ok") is expected_ok, f"{label} environment-probe ok flag mismatch")
    summary = dict(probe)
    summary["temp_evidence"] = temp
    summary["child_path_budget"] = budget
    summary["raw_result"] = raw_result
    return summary


def attempt_summary(
    value: Any, *, raw_root: Path, spec: RepoSpec, worker_id: str,
    protocol: Mapping[str, Any], label: str,
) -> dict[str, Any]:
    attempt = as_mapping(value, "pytest attempt")
    attempt, raw_result = verify_embedded_json(
        attempt, raw_root / "result.json", ARMS_ROOT / "raw", f"{label}.attempt",
    )
    require(type(attempt.get("executed")) is bool, f"{label} executed flag is invalid")
    kind = attempt.get("kind")
    require(kind in {"collection", "pytest"}, f"{label} attempt kind is invalid")
    targets = as_list(attempt.get("targets"), f"{label}.targets")
    require(all(isinstance(item, str) for item in targets), f"{label} attempt targets must be strings")
    apparatus_budget: dict[str, Any] | None = None
    temp_evidence: dict[str, Any] | None = None
    environment_probe: dict[str, Any] | None = None
    if attempt["executed"]:
        temp_evidence = validate_temp_evidence(
            attempt.get("temp_evidence"), attempt_root=raw_root, spec=spec,
            worker_id=worker_id, protocol=protocol, label=label,
        )
        apparatus_budget = validate_child_path_budget(
            attempt.get("child_path_budget"), kind=str(kind), targets=targets,
            attempt_root=raw_root, source_root_value=attempt.get("source_root"),
            temp_evidence=temp_evidence, spec=spec, worker_id=worker_id,
            protocol=protocol, label=label,
        )
        environment_probe = validate_environment_probe(
            attempt.get("environment_probe"), probe_root=raw_root / "environment-probe",
            attempt_source_root=attempt.get("source_root"), spec=spec, worker_id=worker_id,
            protocol=protocol, label=label,
        )
        require(
            temp_evidence["token"] != environment_probe["temp_evidence"]["token"],
            f"{label} outer/probe temporary roots are not distinct",
        )
    else:
        require(
            all(key not in attempt for key in ("temp_evidence", "child_path_budget", "environment_probe")),
            f"{label} unexecuted attempt carries child apparatus evidence",
        )
    counts = as_mapping(attempt.get("counts", {}), "pytest attempt counts")
    for count_kind in ("pytest_summary", "junit", "leaf_outcomes"):
        count_map = as_mapping(counts.get(count_kind, {}), f"pytest attempt counts.{count_kind}")
        require(
            all(isinstance(key, str) and type(count) is int and count >= 0 for key, count in count_map.items()),
            f"pytest attempt counts.{count_kind} contains an invalid count",
        )
    leaf_node_ids = list(attempt.get("leaf_nodeids", []))
    require(all(isinstance(item, str) for item in targets + leaf_node_ids), "pytest attempt targets/node IDs must be strings")
    normalized_leaves = sorted({normalized_node_id(item) for item in leaf_node_ids})
    expected_coverage: dict[str, dict[str, Any]] = {}
    for raw_target in targets:
        normalized_target = normalized_node_id(raw_target)
        matches = [
            nodeid for nodeid in normalized_leaves
            if nodeid == normalized_target or nodeid.startswith(normalized_target + "::")
        ]
        expected_coverage[raw_target] = {
            "normalized_target": normalized_target,
            "leaf_count": len(matches),
            "leaf_nodeids": matches,
        }
    expected_uncovered = sorted(
        target for target, evidence in expected_coverage.items() if evidence["leaf_count"] == 0
    )
    require(attempt.get("target_leaf_coverage", {}) == expected_coverage, "pytest attempt target coverage mismatch")
    require(attempt.get("uncovered_targets", []) == expected_uncovered, "pytest attempt uncovered-target mismatch")
    expected_all_covered = bool(targets) and not expected_uncovered
    require(
        isinstance(attempt.get("all_targets_have_leaves"), bool)
        and attempt.get("all_targets_have_leaves") == expected_all_covered,
        "pytest attempt all-targets-covered flag mismatch",
    )
    normalized_leaf_results = list(attempt.get("normalized_leaf_results", []))
    require(
        all(
            isinstance(item, dict) and isinstance(item.get("nodeid"), str)
            and isinstance(item.get("outcome"), str)
            for item in normalized_leaf_results
        ),
        "pytest attempt normalized leaf evidence is malformed",
    )
    require(
        normalized_leaf_results
        == sorted(normalized_leaf_results, key=lambda item: (item["nodeid"], item["outcome"])),
        "pytest attempt normalized leaf evidence is not sorted",
    )
    leaf_outcomes = dict(attempt.get("leaf_outcomes", {}))
    if "leaf_outcomes" in attempt:
        expected_leaf_results = [
            {"nodeid": nodeid, "outcome": leaf_outcomes[nodeid]} for nodeid in sorted(leaf_outcomes)
        ]
        require(normalized_leaf_results == expected_leaf_results, "pytest attempt normalized leaf results mismatch")
    else:
        expected_leaf_results = normalized_leaf_results
    if kind == "pytest" and attempt["executed"]:
        expected_leaf_counts: dict[str, int] = {}
        for outcome in leaf_outcomes.values():
            require(isinstance(outcome, str), f"{label} leaf outcome is invalid")
            expected_leaf_counts[outcome] = expected_leaf_counts.get(outcome, 0) + 1
        require(
            dict(as_mapping(counts.get("leaf_outcomes"), f"{label}.counts.leaf_outcomes"))
            == expected_leaf_counts,
            f"{label} leaf-outcome counts mismatch",
        )
        require(
            dict(as_mapping(attempt.get("pytest_summary_counts"), f"{label}.pytest_summary_counts"))
            == dict(as_mapping(counts.get("pytest_summary"), f"{label}.counts.pytest_summary")),
            f"{label} pytest-summary counts mismatch",
        )
        junit_record = as_mapping(attempt.get("junit"), f"{label}.junit")
        require(
            dict(as_mapping(junit_record.get("counts"), f"{label}.junit.counts"))
            == dict(as_mapping(counts.get("junit"), f"{label}.counts.junit")),
            f"{label} JUnit counts mismatch",
        )
    leaf_signature = attempt.get("leaf_outcome_signature_sha256")
    computed_leaf_signature_valid = bool(
        normalized_leaf_results == expected_leaf_results
        and leaf_signature == canonical_value_sha256(expected_leaf_results)
    )
    if attempt.get("executed") is True:
        require(
            attempt.get("leaf_outcome_signature_valid") is computed_leaf_signature_valid,
            "pytest attempt leaf-signature-valid flag mismatch",
        )
    else:
        require(not normalized_leaf_results and leaf_signature is None, "unexecuted attempt has leaf-signature evidence")
    failed_exceptions = list(attempt.get("failed_runtest_exceptions", []))
    normalized_failure_details = sorted(
        (
            {
                "nodeid": normalized_node_id(str(detail.get("nodeid", ""))),
                "phase": str(detail.get("phase", "")),
                "qualified_type": str(detail.get("qualified_type") or detail.get("type") or ""),
                "message": str(detail.get("message", "")),
                "is_import_error": detail.get("is_import_error") is True,
            }
            for detail in failed_exceptions if isinstance(detail, dict)
        ),
        key=lambda detail: (
            detail["nodeid"], detail["phase"], detail["qualified_type"], detail["message"],
            detail["is_import_error"],
        ),
    )
    require(
        attempt.get("normalized_failure_details", []) == normalized_failure_details,
        "pytest attempt normalized failure details mismatch",
    )
    import_error_node_ids = sorted(
        {detail["nodeid"] for detail in normalized_failure_details if detail["is_import_error"]}
    )
    require(
        attempt.get("import_error_failure_nodeids", []) == import_error_node_ids,
        "pytest attempt ImportError identity mismatch",
    )
    normalized_attempt_signature = attempt.get("normalized_attempt_signature_sha256")
    junit_payload = attempt.get("junit") if isinstance(attempt.get("junit"), dict) else {}
    if kind == "pytest" and attempt.get("executed") is True:
        signature_probe = attempt.get("environment_probe") if isinstance(attempt.get("environment_probe"), dict) else {}
        expected_attempt_signature = canonical_value_sha256(
            {
                "executed": True,
                "exit_code": attempt.get("exit_code"),
                "timed_out": attempt.get("timed_out"),
                "leaf_nodeids": leaf_node_ids,
                "leaf_outcomes": leaf_outcomes,
                "target_leaf_coverage": expected_coverage,
                "uncovered_targets": expected_uncovered,
                "leaf_outcome_signature": leaf_signature,
                "normalized_failure_details": normalized_failure_details,
                "import_error_failure_nodeids": import_error_node_ids,
                "collection_error_nodeids": sorted(
                    str(item.get("nodeid", ""))
                    for item in attempt.get("collection_errors", []) if isinstance(item, dict)
                ),
                "internal_error_count": len(attempt.get("internal_errors", [])),
                "junit_signature": junit_payload.get("case_outcome_signature_sha256"),
                "environment_ok": signature_probe.get("ok"),
                "files_unchanged": attempt.get("files_unchanged"),
                "leaf_match": attempt.get("leaf_nodeids_match_overlay"),
                "overlay_match": attempt.get("overlay_files_match_frozen"),
            }
        )
        require(normalized_attempt_signature == expected_attempt_signature, "pytest attempt normalized signature mismatch")
    summary = {
        "kind": kind,
        "targets": targets,
        "executed": attempt.get("executed") is True,
        "ok": attempt.get("ok") is True,
        "exit_code": attempt.get("exit_code"),
        "timed_out": attempt.get("timed_out") is True,
        "launch_error": attempt.get("launch_error"),
        "setup_error": attempt.get("setup_error"),
        "counts": dict(counts),
        "leaf_node_ids": leaf_node_ids,
        "target_leaf_coverage": expected_coverage,
        "uncovered_targets": expected_uncovered,
        "all_targets_have_leaves": bool(targets) and not expected_uncovered,
        "normalized_leaf_results": normalized_leaf_results,
        "leaf_outcome_signature_sha256": leaf_signature,
        "leaf_outcome_signature_valid": computed_leaf_signature_valid,
        "junit_case_outcome_signature_sha256": junit_payload.get("case_outcome_signature_sha256"),
        "leaf_outcomes": leaf_outcomes,
        "failing_node_ids": list(attempt.get("failing_nodeids", [])),
        "passed_node_ids": list(attempt.get("passed_nodeids", [])),
        "failed_runtest_exceptions": failed_exceptions,
        "normalized_failure_details": normalized_failure_details,
        "import_error_failure_node_ids": import_error_node_ids,
        "collection_errors": list(attempt.get("collection_errors", [])),
        "internal_errors": list(attempt.get("internal_errors", [])),
        "internal_launch_exception": attempt.get("internal_launch_exception"),
        "files_unchanged": attempt.get("files_unchanged"),
        "environment_probe_ok": bool(environment_probe and environment_probe.get("ok") is True),
        "environment_probe": environment_probe,
        "child_path_budget": apparatus_budget,
        "temp_evidence": temp_evidence,
        "expected_leaf_node_ids": attempt.get("expected_leaf_nodeids"),
        "leaf_node_ids_match_overlay": attempt.get("leaf_nodeids_match_overlay"),
        "overlay_files_match_frozen": attempt.get("overlay_files_match_frozen"),
        "required_pass_node_ids": list(attempt.get("required_pass_nodeids", [])),
        "required_pass_outcomes": dict(attempt.get("required_pass_outcomes", {})),
        "required_pass_node_ids_passed": attempt.get("required_pass_nodeids_passed"),
        "green": attempt.get("green") is True,
        "qualifying_red": attempt.get("qualifying_red") is True,
        "normalized_attempt_signature_sha256": normalized_attempt_signature,
        "raw_result": raw_result,
    }
    return summary


def check_reasons(check: Mapping[str, Any]) -> list[str]:
    reasons = check.get("failure_reasons")
    if isinstance(reasons, list):
        return [str(reason) for reason in reasons]
    if check.get("reason"):
        return [str(check["reason"])]
    return []


def validate_and_summarize_focal(
    focal_value: Any, manifest_side: Mapping[str, Any], protocol: Mapping[str, Any],
    protocol_sha: str, label: str, *, raw_root: Path, spec: RepoSpec, worker_id: str,
) -> dict[str, Any]:
    focal, focal_raw_result = verify_embedded_json(
        focal_value, raw_root / "result.json", ARMS_ROOT / "raw", label,
    )
    tests = sorted(set(manifest_side["test_paths"]))
    require(focal.get("rule_fixed_by_protocol_sha256") == protocol_sha, f"{label} protocol hash mismatch")
    require(focal.get("rule") == protocol["focal_selection_rule"], f"{label} rule mismatch")
    require(focal.get("test_patch_paths") == tests, f"{label} test paths mismatch")
    targets = as_list(focal.get("focal_node_ids"), f"{label}.focal_node_ids")
    require(targets == sorted(set(targets)), f"{label} focal IDs are not sorted unique")
    require(focal.get("focal_collector_targets") == targets, f"{label} focal target alias mismatch")
    conventional = as_list(focal.get("conventional_whole_module_targets"), f"{label}.conventional")
    probes = as_list(focal.get("nonconventional_explicit_probes"), f"{label}.probes")
    require(all(isinstance(target, str) for target in conventional), f"{label} has an invalid conventional target")
    require(all(isinstance(probe, dict) and isinstance(probe.get("path"), str) for probe in probes), f"{label} has an invalid nonconventional probe")
    probe_paths = [probe["path"] for probe in probes]
    require(conventional == sorted(set(conventional)), f"{label} conventional targets are not sorted unique")
    require(probe_paths == sorted(set(probe_paths)), f"{label} nonconventional paths are not sorted unique")
    require(sorted(set(conventional) | set(probe_paths)) == tests, f"{label} does not account for every test-patch path")
    mapped_nonconventional = [probe["path"] for probe in probes if probe.get("mapped_directly") is True]
    require(targets == sorted(set(conventional) | set(mapped_nonconventional)), f"{label} direct-target mapping mismatch")
    probe_attempts: dict[str, dict[str, Any]] = {}
    probe_roots: dict[str, Path] = {}
    for ordinal, probe in enumerate(probes, 1):
        probe_path = probe["path"]
        probe_root = raw_root / "nonconventional" / (
            f"{ordinal:03d}-{sha256_bytes(probe_path.encode('utf-8'))[:12]}"
        )
        probe_roots[probe_path] = probe_root
        expected_raw = probe_root / "result.json"
        require(probe.get("raw") == project_relative(expected_raw), f"{label} probe raw path mismatch")
        raw_probe, _ = read_json(expected_raw)
        full_attempt = attempt_summary(
            raw_probe, raw_root=probe_root, spec=spec, worker_id=worker_id,
            protocol=protocol, label=f"{label}.probe[{probe_path}]",
        )
        require(full_attempt["kind"] == "collection", f"{label} nonconventional probe is not a collection")
        require(full_attempt["targets"] == [probe_path], f"{label} nonconventional probe target mismatch")
        collected = list(probe.get("collected_leaf_nodeids", []))
        require(all(isinstance(nodeid, str) for nodeid in collected), f"{label} probe has an invalid leaf ID")
        require(collected == full_attempt["leaf_node_ids"], f"{label} probe leaf summary mismatch")
        normalized_target = normalized_node_id(probe["path"])
        mapped_leaves = [
            nodeid for nodeid in (normalized_node_id(item) for item in collected)
            if nodeid == normalized_target or nodeid.startswith(normalized_target + "::")
        ]
        expected_probe_coverage = {
            probe["path"]: {
                "normalized_target": normalized_target,
                "leaf_count": len(mapped_leaves),
                "leaf_nodeids": mapped_leaves,
            }
        }
        expected_probe_uncovered = [] if mapped_leaves else [probe["path"]]
        require(probe.get("target_leaf_coverage") == expected_probe_coverage, f"{label} probe coverage mismatch")
        require(probe.get("uncovered_targets") == expected_probe_uncovered, f"{label} probe uncovered-target mismatch")
        require(full_attempt["target_leaf_coverage"] == expected_probe_coverage, f"{label} raw probe coverage mismatch")
        require(full_attempt["uncovered_targets"] == expected_probe_uncovered, f"{label} raw probe uncovered-target mismatch")
        expected_mapped = bool(full_attempt["ok"] and full_attempt["leaf_node_ids"])
        require(probe.get("mapped_directly") is expected_mapped, f"{label} probe mapped flag mismatch")
        if probe.get("mapped_directly") is True:
            require(mapped_leaves and probe.get("mapping_reason") is None, f"{label} mapped probe lacks target-owned leaves")
        else:
            require(probe.get("mapping_reason"), f"{label} unmapped probe lacks a reason")
        probe_attempts[probe_path] = full_attempt
    unmapped = as_list(focal.get("unmapped_support_paths"), f"{label}.unmapped_support_paths")
    require(all(isinstance(item, dict) and isinstance(item.get("path"), str) for item in unmapped), f"{label} has an invalid unmapped-support record")
    unmapped_paths = sorted(item["path"] for item in unmapped)
    expected_unmapped = sorted(probe["path"] for probe in probes if probe.get("mapped_directly") is not True)
    require(unmapped_paths == expected_unmapped, f"{label} unmapped support accounting mismatch")
    for item in unmapped:
        require(isinstance(item, dict) and item.get("reason"), f"{label} has an unmapped path without a reason")
        matching_probe = next(probe for probe in probes if probe["path"] == item["path"])
        require(item.get("reason") == matching_probe.get("mapping_reason"), f"{label} unmapped-support reason mismatch")
        require(
            item.get("raw") == project_relative(probe_roots[item["path"]] / "result.json"),
            f"{label} unmapped-support raw path mismatch",
        )
    overlay_leaves = as_list(focal.get("overlay_leaf_node_ids"), f"{label}.overlay_leaf_node_ids")
    require(overlay_leaves == sorted(set(overlay_leaves)), f"{label} overlay leaves are not sorted unique")
    overlay_collection = attempt_summary(
        focal.get("overlay_collection"), raw_root=raw_root / "overlay-collection",
        spec=spec, worker_id=worker_id, protocol=protocol,
        label=f"{label}.overlay_collection",
    )
    require(overlay_collection["targets"] == targets, f"{label} overlay collector targets mismatch")
    require(overlay_collection["leaf_node_ids"] == overlay_leaves, f"{label} overlay leaf evidence mismatch")
    require(focal.get("overlay_target_leaf_coverage") == overlay_collection["target_leaf_coverage"], f"{label} overlay target-coverage alias mismatch")
    require(focal.get("overlay_uncovered_targets") == overlay_collection["uncovered_targets"], f"{label} overlay uncovered-target alias mismatch")
    focal_ok = focal.get("ok") is True
    reasons = [str(reason) for reason in as_list(focal.get("failure_reasons"), f"{label}.failure_reasons")]
    require(
        (not focal_ok) or (
            bool(targets) and bool(overlay_leaves) and not reasons
            and overlay_collection["ok"] and overlay_collection["all_targets_have_leaves"]
            and overlay_collection["leaf_outcome_signature_valid"]
        ),
        f"{label} successful selection is incomplete",
    )
    require(focal_ok or bool(reasons), f"{label} failed without a recorded reason")
    return {
        "outcome": "PASS" if focal_ok else "FAIL",
        "conventional_whole_module_targets": list(conventional),
        "nonconventional_explicit_probes": [
            {
                "path": probe.get("path"),
                "mapped_directly": probe.get("mapped_directly") is True,
                "mapping_reason": probe.get("mapping_reason"),
                "collected_leaf_node_ids": list(probe.get("collected_leaf_nodeids", [])),
                "target_leaf_coverage": dict(probe.get("target_leaf_coverage", {})),
                "uncovered_targets": list(probe.get("uncovered_targets", [])),
                "attempt": probe_attempts[probe["path"]],
            }
            for probe in probes if isinstance(probe, dict)
        ],
        "test_patch_paths": tests,
        "focal_node_ids": list(targets),
        "overlay_leaf_node_ids": list(overlay_leaves),
        "overlay_target_leaf_coverage": dict(focal.get("overlay_target_leaf_coverage", {})),
        "overlay_uncovered_targets": list(focal.get("overlay_uncovered_targets", [])),
        "overlay_collection": overlay_collection,
        "unmapped_support_paths": [
            {"path": item.get("path"), "reason": item.get("reason")} for item in unmapped
        ],
        "missing_conventional_targets": list(focal.get("missing_conventional_targets", [])),
        "failure_reasons": reasons,
        "raw_result": focal_raw_result,
    }


def validate_and_summarize_checks(
    checks_value: Any, focal_ok: bool, label: str, *, raw_side_root: Path,
    spec: RepoSpec, worker_id: str, protocol: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    checks = as_mapping(checks_value, label)
    require(set(checks) == set(CHECK_NAMES), f"{label} check keys mismatch")
    summaries: dict[str, Any] = {}
    passed: dict[str, bool] = {}
    for check_name in CHECK_NAMES:
        check = as_mapping(checks[check_name], f"{label}.{check_name}")
        outcome = check.get("outcome")
        require(outcome in {"PASS", "FAIL", "NOT_RUN"}, f"invalid {label}.{check_name} outcome")
        check_passed = check.get("passed") is True
        require(check_passed == (outcome == "PASS"), f"{label}.{check_name} passed/outcome mismatch")
        summary: dict[str, Any] = {
            "outcome": outcome,
            "passed": check_passed,
            "counts": check.get("counts", [] if outcome == "NOT_RUN" else {}),
            "failure_reasons": check_reasons(check),
        }
        if check_name == "base_determinism":
            attempts = [
                attempt_summary(
                    item, raw_root=raw_side_root / "base" / f"attempt-{attempt_number}",
                    spec=spec, worker_id=worker_id, protocol=protocol,
                    label=f"{label}.base.attempt-{attempt_number}",
                )
                for attempt_number, item in enumerate(check.get("attempts", []), 1)
            ]
            if focal_ok:
                require(check.get("attempt_count") == 5 and len(attempts) == 5, f"{label} base does not contain five attempts")
            else:
                require(outcome == "NOT_RUN" and not attempts, f"{label} base ran after failed focal selection")
            if attempts:
                require(check.get("counts") == [attempt["counts"] for attempt in attempts], f"{label} base count summary mismatch")
            junit_signatures = list(check.get("normalized_junit_signatures", []))
            leaf_signatures = list(check.get("normalized_leaf_outcome_signatures", []))
            if attempts:
                require(
                    junit_signatures == [item["junit_case_outcome_signature_sha256"] for item in attempts],
                    f"{label} base JUnit signature summary mismatch",
                )
                require(
                    leaf_signatures == [item["leaf_outcome_signature_sha256"] for item in attempts],
                    f"{label} base leaf signature summary mismatch",
                )
            if check_passed:
                require(
                    all(
                        item["green"] and item["all_targets_have_leaves"]
                        and item["leaf_outcome_signature_valid"]
                        for item in attempts
                    ),
                    f"{label} passing base includes an invalid attempt",
                )
                require(
                    len(junit_signatures) == 5 and junit_signatures[0] is not None
                    and len(set(junit_signatures)) == 1,
                    f"{label} passing base JUnit evidence is not deterministic",
                )
                require(
                    len(leaf_signatures) == 5 and leaf_signatures[0] is not None
                    and len(set(leaf_signatures)) == 1
                    and leaf_signatures == [item["leaf_outcome_signature_sha256"] for item in attempts],
                    f"{label} passing base rich leaf evidence is not deterministic",
                )
            summary.update({
                "attempt_count": check.get("attempt_count", 0),
                "normalized_junit_signatures": junit_signatures,
                "normalized_leaf_outcome_signatures": leaf_signatures,
                "attempts": attempts,
            })
        else:
            attempt_value = check.get("attempt")
            attempt_root = raw_side_root / (
                "red" if check_name == "red_test_patch_only" else "green"
            )
            attempt = (
                attempt_summary(
                    attempt_value, raw_root=attempt_root, spec=spec, worker_id=worker_id,
                    protocol=protocol, label=f"{label}.{check_name}",
                )
                if isinstance(attempt_value, dict) else None
            )
            summary["attempt"] = attempt
            if attempt is not None:
                require(check.get("counts") == attempt["counts"], f"{label}.{check_name} count summary mismatch")
            if outcome == "PASS":
                require(attempt is not None, f"{label}.{check_name} passed without an attempt")
            if check_name == "red_test_patch_only":
                if check_passed:
                    require(
                        attempt is not None and attempt["qualifying_red"]
                        and attempt["all_targets_have_leaves"]
                        and attempt["leaf_outcome_signature_valid"]
                        and not attempt["import_error_failure_node_ids"],
                        f"{label} red pass is not qualifying",
                    )
                summary["failing_node_ids"] = list(check.get("failing_node_ids", []))
            else:
                if check_passed:
                    require(
                        attempt is not None and attempt["green"]
                        and attempt["all_targets_have_leaves"]
                        and attempt["leaf_outcome_signature_valid"],
                        f"{label} green pass is not green",
                    )
                summary["red_failing_node_outcomes"] = dict(check.get("red_failing_node_outcomes", {}))
        summaries[check_name] = summary
        passed[check_name] = check_passed

    if focal_ok and not passed["base_determinism"]:
        require(
            summaries["red_test_patch_only"]["outcome"] == "NOT_RUN"
            and summaries["green_source_and_test"]["outcome"] == "NOT_RUN",
            f"{label} ran downstream checks after base rejection",
        )
    side_validated = focal_ok and all(passed.values())
    return summaries, side_validated


def derive_failed_checks(side_name: str, side: Mapping[str, Any]) -> list[dict[str, Any]]:
    focal = as_mapping(side["focal_selection"], "side.focal_selection")
    if focal["outcome"] == "FAIL":
        return [{"side": side_name, "check": "focal_selection", "reasons": list(focal["failure_reasons"])}]
    checks = as_mapping(side["checks"], "side.checks")
    base = as_mapping(checks["base_determinism"], "side.base")
    if base["outcome"] == "FAIL":
        return [{"side": side_name, "check": "base_determinism", "reasons": list(base["failure_reasons"])}]
    failures: list[dict[str, Any]] = []
    for name in ("red_test_patch_only", "green_source_and_test"):
        check = as_mapping(checks[name], f"side.{name}")
        if check["outcome"] == "FAIL":
            failures.append({"side": side_name, "check": name, "reasons": list(check["failure_reasons"])})
    return failures


def validate_joint(
    joint_value: Any, manifest_joint: Mapping[str, Any], site_validated: bool, label: str,
    *, raw_root: Path, spec: RepoSpec, worker_id: str, protocol: Mapping[str, Any],
) -> dict[str, Any]:
    joint = as_mapping(joint_value, label)
    status = joint.get("status")
    require(status in JOINT_STATUSES, f"invalid joint status for {label}: {status}")
    constructible = manifest_joint.get("constructible") is True
    require(joint.get("constructible") is constructible, f"joint constructibility mismatch for {label}")
    require(joint.get("source_conflict_paths", []) == manifest_joint.get("source_conflict_paths", []), f"joint conflict paths mismatch for {label}")
    flag = joint.get("mutually_unsatisfiable")
    if not constructible:
        require(status == "NOT_CONSTRUCTIBLE_TEXTUAL_SOURCE_CONFLICT" and flag is None, f"bad textual-conflict disposition for {label}")
    elif not site_validated:
        require(status == "NOT_RUN_SITE_NOT_VALIDATED" and flag is None, f"bad rejected-site joint disposition for {label}")
    elif status == "JOINTLY_SATISFIABLE":
        require(flag is False, f"jointly satisfiable flag mismatch for {label}")
    elif status == "MUTUALLY_UNSATISFIABLE":
        require(flag is True, f"mutual-unsat flag mismatch for {label}")
    else:
        require(status == "UNVERIFIED_JOINT_OUTCOME" and flag is None, f"unverified joint flag mismatch for {label}")

    raw_attempts = as_mapping(joint.get("attempts", {}), f"{label}.attempts")
    attempts: dict[str, list[dict[str, Any]]] = {}
    for side_name in SIDE_NAMES:
        side_attempts = raw_attempts.get(side_name, [])
        attempts[side_name] = [
            attempt_summary(
                item, raw_root=raw_root / side_name / f"attempt-{attempt_number}",
                spec=spec, worker_id=worker_id, protocol=protocol,
                label=f"{label}.{side_name}.attempt-{attempt_number}",
            )
            for attempt_number, item in enumerate(side_attempts, 1)
        ]
    if status == "JOINTLY_SATISFIABLE":
        require(all(len(attempts[name]) == 1 and attempts[name][0]["green"] for name in SIDE_NAMES), f"bad satisfiable attempts for {label}")
    if status in {"MUTUALLY_UNSATISFIABLE", "UNVERIFIED_JOINT_OUTCOME"}:
        require(all(len(attempts[name]) == 2 for name in SIDE_NAMES), f"joint rerun evidence missing for {label}")
    stability = dict(joint.get("stability", {}))
    if status in {"MUTUALLY_UNSATISFIABLE", "UNVERIFIED_JOINT_OUTCOME"}:
        derived_stability: dict[str, str] = {}
        for side_name in SIDE_NAMES:
            initial, rerun = attempts[side_name]
            signatures = [
                initial["normalized_attempt_signature_sha256"],
                rerun["normalized_attempt_signature_sha256"],
            ]
            stable_signature = signatures[0] is not None and signatures[0] == signatures[1]
            if initial["green"] and rerun["green"]:
                derived_stability[side_name] = (
                    "STABLE_GREEN" if stable_signature else "UNVERIFIED_GREEN_RERUN_DISAGREED"
                )
            elif initial["qualifying_red"] and rerun["qualifying_red"]:
                derived_stability[side_name] = (
                    "STABLE_QUALIFYING_RED" if stable_signature
                    else "UNVERIFIED_RED_RERUN_DISAGREED"
                )
            else:
                derived_stability[side_name] = "UNVERIFIED_NON_TEST_FAILURE_OR_RERUN_DISAGREEMENT"
        require(stability == derived_stability, f"joint stability derivation mismatch for {label}")
        verified = {"STABLE_GREEN", "STABLE_QUALIFYING_RED"}
        contradictory = bool(
            all(state in verified for state in stability.values())
            and "STABLE_QUALIFYING_RED" in stability.values()
        )
        require(
            (status == "MUTUALLY_UNSATISFIABLE") is contradictory,
            f"joint contradictory status derivation mismatch for {label}",
        )
    expected_counts = {name: [attempt["counts"] for attempt in attempts[name]] for name in SIDE_NAMES}
    if any(attempts.values()):
        require(joint.get("counts") == expected_counts, f"joint count summary mismatch for {label}")
    return {
        "status": status,
        "constructible": constructible,
        "source_conflict_paths": list(joint.get("source_conflict_paths", [])),
        "mutually_unsatisfiable": flag,
        "rerun_triggered": joint.get("rerun_triggered", False),
        "stability": stability,
        "attempts": attempts,
        "counts": expected_counts if any(attempts.values()) else {},
        "reason": joint.get("reason"),
    }


def validate_site_result(
    spec: RepoSpec, index: int, row: Mapping[str, Any], protocol: Mapping[str, Any],
    protocol_sha: str, runner_sha: str,
) -> dict[str, Any]:
    result_path = ARMS_ROOT / "raw" / spec.slug / str(row["merge"]) / "result.json"
    result, result_payload = read_json(result_path)
    expected = {
        "schema_version": SCHEMA_VERSION,
        "complete": True,
        "protocol_sha256": protocol_sha,
        "runner_sha256": runner_sha,
        "repository": spec.repository,
        "repo_slug": spec.slug,
        "site_index": index,
        "corpus_line": row["corpus_line"],
        "merge": row["merge"],
        "parents": row["parents"],
        "base": row["merge_base"],
    }
    for key, expected_value in expected.items():
        require(result.get(key) == expected_value, f"result {key} mismatch for {row['merge']}")
    require(result.get("verdict") in PRIMARY_VERDICTS, f"unknown primary verdict for {row['merge']}")
    worker_id = result.get("worker_id")
    expected_worker_root(spec, worker_id)

    manifest, manifest_sha = validate_manifest(spec, index, row, protocol_sha, runner_sha)
    require(result.get("prepared_manifest_sha256") == manifest_sha, f"result manifest hash mismatch for {row['merge']}")
    preparation = validate_preparation(result, spec, row, protocol_sha, runner_sha, manifest_sha)

    raw_run_root = resolve_recorded_path(result.get("raw_run_root"), ARMS_ROOT / "raw", "raw run root")
    expected_run_parent = (ARMS_ROOT / "raw" / spec.slug / str(row["merge"]) / "runs").resolve()
    require(raw_run_root.parent == expected_run_parent, f"raw run root hierarchy mismatch for {row['merge']}")
    require(
        raw_run_root.is_dir() and re.fullmatch(r"r[0-9a-f]+", raw_run_root.name) is not None,
        f"raw run root is not a compact final-runner generation: {raw_run_root}",
    )
    generation_result = raw_run_root / "site-result.json"
    require(generation_result.is_file(), f"generation result is absent for {row['merge']}")
    require(generation_result.read_bytes() == result_payload, f"published/generation result mismatch for {row['merge']}")

    runtime = as_mapping(result.get("runtime_preflight"), "result.runtime_preflight")
    runtime_postflight = as_mapping(result.get("runtime_postflight"), "result.runtime_postflight")
    require(runtime_postflight == runtime, f"runtime pre/postflight mismatch for {row['merge']}")
    expected_fingerprints = {
        key: protocol["environment"][key] for key in (
            "python_sha256", "python_site_packages_sha256", "python_venv_config_sha256",
            "python_environment_manifest_sha256", "click_compat_sha256",
        )
    }
    require(runtime.get("fingerprints_before_probes") == expected_fingerprints, f"runtime before-probe fingerprints mismatch for {row['merge']}")
    require(runtime.get("fingerprints_after_probes") == expected_fingerprints, f"runtime after-probe fingerprints mismatch for {row['merge']}")
    require(runtime.get("integrity_unchanged_during_probes") is True, f"runtime probe-integrity flag mismatch for {row['merge']}")
    require(
        runtime.get("probe_policy") == {
            "PYTHONPATH": "removed",
            "PYTHONHOME": "removed",
            "PYTEST_*": "removed before fixed PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONHASHSEED": "0",
        },
        f"runtime probe policy mismatch for {row['merge']}",
    )
    for key in (
        "python_sha256", "python_site_packages_sha256", "python_venv_config_sha256",
        "python_environment_manifest_sha256", "click_compat_sha256",
    ):
        require(runtime.get(key) == protocol["environment"][key], f"runtime {key} mismatch for {row['merge']}")
    require(
        isinstance(runtime.get("python"), str)
        and Path(runtime["python"]).resolve() == Path(protocol["environment"]["python"]).resolve(),
        f"runtime Python path mismatch for {row['merge']}",
    )
    require(protocol["environment"]["python_version"] in str(runtime.get("python_version_output", "")), f"runtime Python version mismatch for {row['merge']}")
    require(protocol["environment"]["pytest_version"] in str(runtime.get("pytest_version_output", "")), f"runtime pytest version mismatch for {row['merge']}")

    result_sides = as_mapping(result.get("sides"), "result.sides")
    manifest_sides = as_mapping(manifest.get("sides"), "manifest.sides")
    require(set(result_sides) == set(SIDE_NAMES), f"result side keys mismatch for {row['merge']}")
    side_summaries: dict[str, Any] = {}
    failed_checks: list[dict[str, Any]] = []
    for side_name in SIDE_NAMES:
        side = as_mapping(result_sides[side_name], f"result.{side_name}")
        manifest_side = as_mapping(manifest_sides[side_name], f"manifest.{side_name}")
        raw_side_root = raw_run_root / "sides" / side_name
        require(side.get("parent") == manifest_side.get("parent"), f"result {side_name} parent mismatch")
        require(side.get("source_patch") == manifest_side.get("source_patch"), f"result {side_name} source-patch mismatch")
        require(side.get("test_patch") == manifest_side.get("test_patch"), f"result {side_name} test-patch mismatch")
        focal = validate_and_summarize_focal(
            side.get("focal_selection"), manifest_side, protocol, protocol_sha,
            f"{row['merge']}.{side_name}.focal_selection",
            raw_root=raw_side_root / "focal-selection", spec=spec, worker_id=worker_id,
        )
        checks, derived_validated = validate_and_summarize_checks(
            side.get("checks"), focal["outcome"] == "PASS", f"{row['merge']}.{side_name}.checks",
            raw_side_root=raw_side_root, spec=spec, worker_id=worker_id, protocol=protocol,
        )
        require(side.get("validated") is derived_validated, f"result {side_name} validated flag mismatch")
        side_summary = {
            "parent": side["parent"],
            "source_patch": dict(side["source_patch"]),
            "test_patch": dict(side["test_patch"]),
            "test_patch_paths": list(manifest_side["test_paths"]),
            "source_paths": list(manifest_side["source_paths"]),
            "focal_node_ids": list(focal["focal_node_ids"]),
            "overlay_leaf_node_ids": list(focal["overlay_leaf_node_ids"]),
            "unmapped_support_paths": list(focal["unmapped_support_paths"]),
            "focal_selection": focal,
            "checks": checks,
            "validated": derived_validated,
            "failure_reasons": list(side.get("failure_reasons", [])),
        }
        side_summaries[side_name] = side_summary
        failed_checks.extend(derive_failed_checks(side_name, side_summary))

    site_validated = all(side_summaries[name]["validated"] for name in SIDE_NAMES)
    require(result.get("validated") is site_validated, f"site validated flag mismatch for {row['merge']}")
    red_attempts = [
        side_summaries[name]["checks"]["red_test_patch_only"].get("attempt")
        for name in SIDE_NAMES
    ]
    both_nonprobe_green = all(
        isinstance(attempt, dict) and attempt.get("green") is True for attempt in red_attempts
    ) and all(
        side_summaries[name]["checks"]["green_source_and_test"]["passed"] is True
        for name in SIDE_NAMES
    )
    expected_verdict = "VALIDATED" if site_validated else "REJECTED_NON_PROBE" if both_nonprobe_green else "REJECTED"
    require(result.get("verdict") == expected_verdict, f"primary verdict derivation mismatch for {row['merge']}")
    rejection_reasons = list(result.get("rejection_reasons", []))
    require((expected_verdict == "VALIDATED") == (not rejection_reasons), f"rejection reason cardinality mismatch for {row['merge']}")

    manifest_joint = as_mapping(manifest.get("joint_source"), "manifest.joint_source")
    joint = validate_joint(
        result.get("joint_source_check"), manifest_joint, site_validated, str(row["merge"]),
        raw_root=raw_run_root / "joint", spec=spec, worker_id=worker_id, protocol=protocol,
    )

    return {
        "repo": spec.repository,
        "repo_slug": spec.slug,
        "site_index": index,
        "corpus_line": row["corpus_line"],
        "merge": row["merge"],
        "parents": list(row["parents"]),
        "base": row["merge_base"],
        "sides": side_summaries,
        "joint_source": dict(manifest_joint),
        "joint_source_check": joint,
        "verdict": expected_verdict,
        "validated": site_validated,
        "failed_checks": failed_checks,
        "rejection_reasons": rejection_reasons,
        "worker_id": worker_id,
        "completed_at_utc": result.get("completed_at_utc"),
        "evidence": {
            "prepared_manifest": project_relative(ARMS_ROOT / "patches" / spec.slug / str(row["merge"]) / "manifest.json"),
            "prepared_manifest_sha256": manifest_sha,
            "preparation_record": preparation,
            "raw_result": project_relative(result_path),
            "raw_result_sha256": sha256_bytes(result_payload),
            "raw_run_root": project_relative(raw_run_root),
        },
        "runtime_preflight": dict(runtime),
        "runtime_postflight": dict(runtime_postflight),
    }


def assert_result_population(spec: RepoSpec, expected_merges: set[str]) -> None:
    root = ARMS_ROOT / "raw" / spec.slug
    require(root.is_dir(), f"raw result root is absent: {root}")
    observed = {
        path.parent.name for path in root.glob("*/result.json")
        if path.parent.name not in IGNORED_RAW_ADMIN_NAMES
        and not path.parent.name.startswith(IGNORED_RAW_ADMIN_PREFIXES)
    }
    require(observed == expected_merges, f"{spec.repository} final-result population mismatch: missing={sorted(expected_merges-observed)}, extra={sorted(observed-expected_merges)}")


def validate_batch_summary_entry(value: Any, label: str) -> dict[str, Any]:
    entry = dict(as_mapping(value, label))
    require(set(entry) == {"index", "merge", "verdict"}, f"{label} keys mismatch")
    require(type(entry.get("index")) is int and entry["index"] >= 0, f"{label} index is invalid")
    require(
        isinstance(entry.get("merge"), str) and HEX_40_RE.fullmatch(entry["merge"]) is not None,
        f"{label} merge is invalid",
    )
    require(entry.get("verdict") in PRIMARY_VERDICTS, f"{label} verdict is invalid")
    return entry


def validate_clean_batch_provenance(
    spec: RepoSpec, sites: Sequence[dict[str, Any]], protocol_sha: str, runner_sha: str,
) -> dict[str, Any]:
    batch_root = ARMS_ROOT / "raw" / spec.slug / "_batches"
    require(batch_root.is_dir(), f"clean batch ledger root is absent: {batch_root}")
    site_by_identity = {(site["site_index"], site["merge"]): site for site in sites}
    require(len(site_by_identity) == len(sites), f"duplicate site identity before batch reconciliation: {spec.repository}")
    clean_batches: list[dict[str, Any]] = []
    ignored_diagnostics: list[dict[str, Any]] = []
    for path in sorted(batch_root.glob("*.json"), key=lambda item: item.name):
        display = path.absolute().relative_to(PROJECT_ROOT.absolute()).as_posix()
        payload: bytes | None = None
        try:
            require(not path.is_symlink(), f"administrative batch record is a symlink: {path}")
            payload = path.read_bytes()
            decoded = decode_json(payload, str(path))
            batch = dict(as_mapping(decoded, str(path)))
        except (OSError, RenderError) as error:
            ignored_diagnostics.append(
                {
                    "path": display,
                    "sha256": sha256_bytes(payload) if payload is not None else None,
                    "bytes": len(payload) if payload is not None else None,
                    "ignored_reason": str(error),
                }
            )
            continue
        assert payload is not None
        identity_hashes = (
            batch.get("protocol_sha256"), batch.get("runner_sha256_start"),
            batch.get("runner_sha256_end"),
        )
        if batch.get("complete") is not True or identity_hashes != (
            protocol_sha, runner_sha, runner_sha,
        ):
            ignored_diagnostics.append(
                {
                    "path": display,
                    "sha256": sha256_bytes(payload),
                    "bytes": len(payload),
                    "complete": batch.get("complete") is True,
                    "protocol_sha256": batch.get("protocol_sha256"),
                    "runner_sha256_start": batch.get("runner_sha256_start"),
                    "runner_sha256_end": batch.get("runner_sha256_end"),
                }
            )
            continue
        require(
            set(batch) == {
                "schema_version", "complete", "protocol_sha256", "runner_sha256_start",
                "runner_sha256_end", "runtime_preflight", "runtime_postflight",
                "repository", "worker_id", "index_start", "index_stop", "completed",
                "staged_not_published", "resumed", "apparatus_errors", "completed_at_utc",
            },
            f"clean batch ledger keys mismatch: {path}",
        )
        require(batch.get("schema_version") == SCHEMA_VERSION, f"clean batch schema mismatch: {path}")
        require(batch.get("repository") == spec.repository, f"clean batch repository mismatch: {path}")
        worker_id = batch.get("worker_id")
        expected_worker_root(spec, worker_id)
        start = batch.get("index_start")
        stop = batch.get("index_stop")
        require(
            type(start) is int and type(stop) is int
            and 0 <= start <= stop <= spec.expected_sites,
            f"clean batch range is invalid: {path}",
        )
        require(
            re.fullmatch(
                rf"{re.escape(worker_id)}-{start:03d}-{stop:03d}-[0-9]+\.json",
                path.name,
            ) is not None,
            f"clean batch filename/range mismatch: {path}",
        )
        runtime_preflight = dict(as_mapping(batch.get("runtime_preflight"), f"{path}.runtime_preflight"))
        runtime_postflight = dict(as_mapping(batch.get("runtime_postflight"), f"{path}.runtime_postflight"))
        require(runtime_postflight == runtime_preflight, f"clean batch runtime pre/postflight mismatch: {path}")
        require(batch.get("apparatus_errors") == [], f"clean batch contains apparatus errors: {path}")
        require(batch.get("staged_not_published") == [], f"clean batch contains unpublished staging: {path}")
        require(isinstance(batch.get("completed_at_utc"), str), f"clean batch completion time is invalid: {path}")
        completed = [
            validate_batch_summary_entry(item, f"{path}.completed[{ordinal}]")
            for ordinal, item in enumerate(as_list(batch.get("completed"), f"{path}.completed"))
        ]
        resumed = [
            validate_batch_summary_entry(item, f"{path}.resumed[{ordinal}]")
            for ordinal, item in enumerate(as_list(batch.get("resumed"), f"{path}.resumed"))
        ]
        for category, entries in (("completed", completed), ("resumed", resumed)):
            identities = [(entry["index"], entry["merge"]) for entry in entries]
            require(len(identities) == len(set(identities)), f"duplicate {category} identity in {path}")
            require(
                all(start <= entry["index"] < stop for entry in entries),
                f"{category} entry falls outside batch range: {path}",
            )
        require(
            not ({(entry['index'], entry['merge']) for entry in completed}
                 & {(entry['index'], entry['merge']) for entry in resumed}),
            f"batch identity is both completed and resumed: {path}",
        )
        for category, entries in (("completed", completed), ("resumed", resumed)):
            require(
                [entry["index"] for entry in entries] == sorted(entry["index"] for entry in entries),
                f"{category} entries are not in census order: {path}",
            )
        expected_slice = {
            identity for identity in site_by_identity if start <= identity[0] < stop
        }
        observed_slice = {
            (entry["index"], entry["merge"]) for entry in completed + resumed
        }
        require(observed_slice == expected_slice, f"clean batch does not exactly cover its census slice: {path}")
        clean_batches.append(
            {
                "path": project_relative(path),
                "sha256": sha256_bytes(payload),
                "bytes": len(payload),
                "worker_id": worker_id,
                "index_start": start,
                "index_stop": stop,
                "runtime_preflight": runtime_preflight,
                "completed": completed,
                "resumed": resumed,
                "completed_at_utc": batch["completed_at_utc"],
            }
        )
    require(clean_batches, f"no clean current-apparatus batch ledgers found for {spec.repository}")

    completed_matches: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for batch in clean_batches:
        for category in ("completed", "resumed"):
            for entry in batch[category]:
                identity = (entry["index"], entry["merge"])
                require(identity in site_by_identity, f"clean batch references a non-census final site: {identity}")
                site = site_by_identity[identity]
                require(entry["verdict"] == site["verdict"], f"clean batch verdict mismatch for {identity}")
                require(batch["worker_id"] == site["worker_id"], f"clean batch worker mismatch for {identity}")
                require(
                    batch["runtime_preflight"] == site["runtime_preflight"],
                    f"clean batch/site runtime mismatch for {identity}",
                )
                if category == "completed":
                    completed_matches.setdefault(identity, []).append(batch)
    for identity, site in site_by_identity.items():
        matches = completed_matches.get(identity, [])
        require(len(matches) == 1, f"final site lacks unique clean publication ledger: {identity}; found {len(matches)}")
        batch = matches[0]
        site["evidence"]["batch_record"] = {
            "path": batch["path"], "sha256": batch["sha256"], "bytes": batch["bytes"],
        }
    return {
        "clean_records": [
            {
                key: batch[key] for key in (
                    "path", "sha256", "bytes", "worker_id", "index_start", "index_stop",
                    "completed_at_utc",
                )
            }
            for batch in clean_batches
        ],
        "ignored_administrative_records": ignored_diagnostics,
    }


def summarize_population(sites: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for spec in REPOSITORIES:
        rows = [site for site in sites if site["repo"] == spec.repository]
        verdict_counts = {verdict: sum(site["verdict"] == verdict for site in rows) for verdict in PRIMARY_VERDICTS}
        joint_counts = {status: sum(site["joint_source_check"]["status"] == status for site in rows) for status in JOINT_STATUSES}
        require(sum(verdict_counts.values()) == spec.expected_sites, f"verdict counts do not reconcile for {spec.repository}")
        require(sum(joint_counts.values()) == spec.expected_sites, f"joint counts do not reconcile for {spec.repository}")
        result[spec.repository] = {
            "census": spec.expected_sites,
            "attempted": len(rows),
            "validated": verdict_counts["VALIDATED"],
            "verdict_counts": verdict_counts,
            "joint_status_counts": joint_counts,
        }
    return result


def escape_cell(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("|", "&#124;").replace("\r", " ").replace("\n", " ")


def code_cell(value: Any) -> str:
    return f"`{escape_cell(value).replace('`', '&#96;')}`"


def map_counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "{}"
    return "{" + ", ".join(f"{escape_cell(key)}={escape_cell(value[key])}" for key in sorted(value)) + "}"


def attempt_markdown(attempt: Mapping[str, Any] | None) -> str:
    if attempt is None or not attempt.get("executed"):
        reason = (attempt or {}).get("setup_error") or "not run"
        return f"not executed; counts unavailable; {escape_cell(reason)}"
    parts = [f"exit={escape_cell(attempt.get('exit_code'))}"]
    counts = attempt.get("counts", {})
    if isinstance(counts, dict):
        parts.extend(
            (
                "pytest=" + map_counts(counts.get("pytest_summary", {})),
                "junit=" + map_counts(counts.get("junit", {})),
                "leaf=" + map_counts(counts.get("leaf_outcomes", {})),
            )
        )
    if attempt.get("timed_out"):
        parts.append("TIMED OUT")
    if attempt.get("launch_error"):
        parts.append("launch error=" + escape_cell(attempt["launch_error"]))
    if attempt.get("collection_errors"):
        nodes = [item.get("nodeid", "<unknown>") for item in attempt["collection_errors"] if isinstance(item, dict)]
        parts.append("collection errors=" + escape_cell(nodes))
    if attempt.get("uncovered_targets"):
        parts.append("targets with zero leaves=" + escape_cell(attempt["uncovered_targets"]))
    if attempt.get("import_error_failure_node_ids"):
        parts.append("runtest ImportError leaves=" + escape_cell(attempt["import_error_failure_node_ids"]))
    if attempt.get("executed") and not attempt.get("leaf_outcome_signature_valid"):
        parts.append("rich leaf signature unavailable/invalid")
    if attempt.get("internal_errors") or attempt.get("internal_launch_exception"):
        parts.append("internal/plugin error")
    if attempt.get("files_unchanged") is False:
        parts.append("candidate tree changed")
    return "; ".join(parts)


def reason_markdown(reasons: Sequence[Any]) -> str:
    return "; ".join(escape_cell(reason) for reason in reasons) if reasons else ""


def focal_markdown(side: Mapping[str, Any]) -> str:
    targets = side["focal_node_ids"]
    parts = [f"targets: {', '.join(code_cell(target) for target in targets) if targets else 'none'}"]
    parts.append(f"overlay leaves: {len(side['overlay_leaf_node_ids'])}")
    if side["unmapped_support_paths"]:
        support = ", ".join(
            f"{code_cell(item['path'])} ({escape_cell(item['reason'])})"
            for item in side["unmapped_support_paths"]
        )
        parts.append("support/unmapped: " + support)
    focal = side["focal_selection"]
    if focal["outcome"] == "FAIL":
        parts.append("FAIL: " + reason_markdown(focal["failure_reasons"]))
    return "<br>".join(parts)


def check_markdown(check: Mapping[str, Any], base: bool = False) -> str:
    parts = [escape_cell(check["outcome"])]
    if base and check.get("attempts"):
        for number, attempt in enumerate(check["attempts"], 1):
            parts.append(f"#{number}: {attempt_markdown(attempt)}")
        for signature_label, signatures in (
            ("JUnit", check.get("normalized_junit_signatures", [])),
            ("rich leaf nodeid/outcome", check.get("normalized_leaf_outcome_signatures", [])),
        ):
            if signatures:
                signature_state = (
                    "identical" if all(signature is not None for signature in signatures) and len(set(signatures)) == 1
                    else "unavailable" if any(signature is None for signature in signatures)
                    else "DIFFER"
                )
                parts.append(signature_label + " signatures: " + signature_state)
    elif not base:
        parts.append(attempt_markdown(check.get("attempt")))
        if check.get("failing_node_ids"):
            parts.append("failing leaves: " + ", ".join(code_cell(item) for item in check["failing_node_ids"]))
        if check.get("red_failing_node_outcomes"):
            parts.append("red-leaf outcomes: " + map_counts(check["red_failing_node_outcomes"]))
    elif check["outcome"] == "NOT_RUN":
        parts.append("counts unavailable")
    if check.get("failure_reasons"):
        parts.append("reason: " + reason_markdown(check["failure_reasons"]))
    return "<br>".join(parts)


def verdict_markdown(site: Mapping[str, Any]) -> str:
    parts = [code_cell(site["verdict"])]
    for failure in site["failed_checks"]:
        parts.append(
            f"{escape_cell(failure['side'])}.{escape_cell(failure['check'])}: "
            + reason_markdown(failure["reasons"])
        )
    if site["rejection_reasons"]:
        parts.append("site: " + reason_markdown(site["rejection_reasons"]))
    return "<br>".join(parts)


def joint_attempts_markdown(joint: Mapping[str, Any], side_name: str) -> str:
    attempts = joint["attempts"].get(side_name, [])
    if not attempts:
        return "not run; counts unavailable"
    return "<br>".join(f"#{number}: {attempt_markdown(attempt)}" for number, attempt in enumerate(attempts, 1))


def merge_names(sites: Sequence[Mapping[str, Any]]) -> str:
    if not sites:
        return "none"
    return ", ".join(f"{site['repo']} {site['merge']}" for site in sites)


def render_markdown(
    protocol: Mapping[str, Any], protocol_sha: str, runner_sha: str,
    renderer_sha: str, population: Mapping[str, Any], sites: Sequence[Mapping[str, Any]],
) -> str:
    click = population["pallets/click"]
    pygments = population["pygments/pygments"]
    lines = [
        f"pallets/click: {click['validated']} validated / {click['attempted']} attempted; "
        f"pygments/pygments: {pygments['validated']} validated / {pygments['attempted']} attempted.",
        "",
        "# Python arms-ladder site validation",
        "",
        "This is the complete current-environment census of the 24 Click and 2 Pygments conflicted merges where both sides touched test files. The amended sizing decision and MINING.md census value of 2 control here; the earlier rough Pygments estimate of 3 in the realism note is not used. This is Python site-validation evidence only: it does not complete Phase 0 overall and it does not license an arms draw.",
        "",
        "## Frozen focal-selection rule",
        "",
    ]
    for key, value in protocol["focal_selection_rule"].items():
        lines.append(f"- **{escape_cell(key)}:** {escape_cell(value)}")
    lines.extend(
        [
            "",
            "`focal_node_ids` are the sorted, frozen collector targets (normally whole changed pytest modules). Collected overlay leaf IDs are separate evidence. A target missing or noncollecting at untouched B rejects the side; no target is dropped in response to an outcome.",
            "",
            "## Patch and runtime protocol",
            "",
            f"- Protocol SHA-256: `{protocol_sha}`.",
            f"- Validator SHA-256 shared by every result: `{runner_sha}`.",
            f"- Interpreter: `{escape_cell(protocol['environment']['python'])}`; CPython `{escape_cell(protocol['environment']['python_version'])}`; pytest `{escape_cell(protocol['environment']['pytest_version'])}`; timeout `{escape_cell(protocol['environment']['timeout_seconds'])}` seconds.",
            f"- Environment hashes: Python `{escape_cell(protocol['environment']['python_sha256'])}`; site-packages `{escape_cell(protocol['environment']['python_site_packages_sha256'])}`; venv config `{escape_cell(protocol['environment']['python_venv_config_sha256'])}`; environment manifest `{escape_cell(protocol['environment']['python_environment_manifest_sha256'])}`.",
            f"- Import-path rule: {escape_cell(protocol['environment']['python_path_rule'])}.",
            f"- Click compatibility root: `{escape_cell(protocol['environment']['click_compat_root'])}`, SHA-256 `{escape_cell(protocol['environment']['click_compat_sha256'])}`; Pygments adds `--ignore=tests/contrast`.",
            "- Frozen environment policy: `" + escape_cell(json.dumps(protocol["environment"]["policy"], sort_keys=True, separators=(",", ":"))) + "`.",
            f"- Isolated execution-temp policy: {escape_cell(protocol['apparatus_path_policy']['execution_temp'])}",
            f"- Windows path gates: temporary roots at most `{escape_cell(protocol['apparatus_path_policy']['windows_temp_path_budget'])}` characters; all child-facing paths at most `{escape_cell(protocol['apparatus_path_policy']['windows_child_path_budget'])}` characters. {escape_cell(protocol['apparatus_path_policy']['child_path_gate'])}",
            f"- Batch publication: {escape_cell(protocol['apparatus_path_policy']['batch_publication'])}",
            "- The reducer reconciled each embedded executed attempt, nested environment probe, path-budget record, and temporary cleanup attestation with its standalone raw JSON. Every final site also has one unique clean current-apparatus batch publication record.",
            "- Each arm starts from a fresh exported base. Source and test patches are never layered on a prior outcome arm. Plugin autoload and user-site loading are disabled.",
            "- Base PASS requires five untouched-B runs that are all green, cover every frozen collector target, and have identical normalized JUnit and rich leaf nodeid/outcome signatures.",
            "- Red PASS requires exit 1 with at least one test-level failure/error, complete target coverage, and no collection, internal/plugin, usage, or runtest ImportError failure. Green PASS requires exit 0 with the exact frozen overlay leaves/files and every red failing leaf present as passed.",
            "- Count notation preserves all three raw maps: pytest terminal-summary counts, normalized JUnit counts, and recorder leaf-outcome counts. `NOT_RUN` means counts are unavailable, never zero.",
            "",
        ]
    )

    for spec in REPOSITORIES:
        lines.extend(
            [
                f"## Per-site results: `{spec.repository}`",
                "",
                "| Merge | Parents | Base | P1 focal | P1 base x5 | P1 red | P1 green | P2 focal | P2 base x5 | P2 red | P2 green | Verdict |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for site in (item for item in sites if item["repo"] == spec.repository):
            p1 = site["sides"]["parent1"]
            p2 = site["sides"]["parent2"]
            cells = [
                code_cell(site["merge"]),
                code_cell(site["parents"][0]) + "<br>" + code_cell(site["parents"][1]),
                code_cell(site["base"]),
                focal_markdown(p1),
                check_markdown(p1["checks"]["base_determinism"], base=True),
                check_markdown(p1["checks"]["red_test_patch_only"]),
                check_markdown(p1["checks"]["green_source_and_test"]),
                focal_markdown(p2),
                check_markdown(p2["checks"]["base_determinism"], base=True),
                check_markdown(p2["checks"]["red_test_patch_only"]),
                check_markdown(p2["checks"]["green_source_and_test"]),
                verdict_markdown(site),
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    contradictory = [site for site in sites if site["joint_source_check"]["status"] == "MUTUALLY_UNSATISFIABLE"]
    textual = [site for site in sites if site["joint_source_check"]["status"] == "NOT_CONSTRUCTIBLE_TEXTUAL_SOURCE_CONFLICT"]
    unverified = [site for site in sites if site["joint_source_check"]["status"] == "UNVERIFIED_JOINT_OUTCOME"]
    joint_not_run = [site for site in sites if site["joint_source_check"]["status"] == "NOT_RUN_SITE_NOT_VALIDATED"]
    satisfiable = [site for site in sites if site["joint_source_check"]["status"] == "JOINTLY_SATISFIABLE"]
    constructible_validated = [site for site in sites if site["validated"] and site["joint_source_check"]["constructible"]]
    lines.extend(
        [
            "## Joint-source and contradictory-task subset",
            "",
            "The prescribed construction first intersects canonical textual conflict paths with both source-path sets. A nonempty intersection is not constructible this way. Otherwise the joint source patch is the exact B-to-canonical-result-tree diff restricted to the source union; each side is evaluated on a fresh base with that patch plus its own test patch. A non-green first result triggers an immediate identical rerun for both sides, and contradiction requires at least one stable qualifying test-level red while both sides have stable verified outcomes.",
            "",
            f"Operationally mutually unsatisfiable: **{len(contradictory)} / {len(constructible_validated)} constructible validated sites**. Names: {escape_cell(merge_names(contradictory))}.",
            "",
            f"Jointly satisfiable: {len(satisfiable)}. Names: {escape_cell(merge_names(satisfiable))}.",
            "",
            f"Textually nonconstructible: {len(textual)}. Names: {escape_cell(merge_names(textual))}.",
            "",
            f"Unverified joint outcome: {len(unverified)}. Names: {escape_cell(merge_names(unverified))}.",
            "",
            f"Joint check not run because the site was not independently validated: {len(joint_not_run)}. Names: {escape_cell(merge_names(joint_not_run))}.",
            "",
            "| Repository | Merge | Source-conflict paths | P1 joint attempt(s) | P2 joint attempt(s) | Stability | Status and reason |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for site in sites:
        joint = site["joint_source_check"]
        paths = ", ".join(code_cell(path) for path in joint["source_conflict_paths"]) or "none"
        stability = map_counts(joint["stability"])
        lines.append(
            "| " + " | ".join(
                (
                    code_cell(site["repo"]), code_cell(site["merge"]), paths,
                    joint_attempts_markdown(joint, "parent1"),
                    joint_attempts_markdown(joint, "parent2"),
                    stability,
                    code_cell(joint["status"]) + ": " + escape_cell(joint.get("reason") or ""),
                )
            ) + " |"
        )

    lines.extend(
        [
            "",
            "## Claims that could NOT be verified",
            "",
            "- This report does not verify full-suite behavioral equivalence; green is limited to each frozen focal subset.",
            "- Five identical base runs do not prove that a rare flake cannot occur outside those five executions.",
            "- A rejection under the current gated environment does not prove that the historical task was intrinsically impossible.",
            f"- Mutual satisfiability could not be classified by the prescribed construction for the {len(textual)} textual-source-conflict site(s), the {len(joint_not_run)} independently rejected site(s), or the {len(unverified)} unstable joint outcome(s).",
            "- Operational mutual unsatisfiability under the exact joint patch does not prove that no alternative implementation or conflict resolution could satisfy both contracts.",
            "- Developer intent, historical dependency/platform identity, and behavior outside the changed-test focal targets were not reconstructed.",
            "- This Python subset does not complete Phase 0: the approved design still requires the Go/Java runners, shim/event log, N=3 escalation machinery, canary build and calibration, frozen prompt hashes, and the remaining clean-room preconditions.",
            "- No agent subjects or arms were run, so P2, P3, P4, P6, collision-rate, throughput, livelock, attribution, and fairness claims were not tested.",
            "",
            "## What would change this verdict",
            "",
            "- A preregistered amended historical environment could change collection, import, or base-gate exclusions; it would be a new protocol rather than an adaptive rewrite of this result.",
            "- Stronger outcome-frozen focal tests that produce a qualifying test-only red could turn a non-probe into a candidate under a new validation round.",
            "- A source+test run that preserves the exact frozen overlay leaves and turns every red failing leaf into a pass would change a green-check rejection.",
            "- An immediate identical rerun that agrees would resolve an unverified joint outcome.",
            "- A separately frozen, auditable resolution policy would be required to construct a joint check for textual source conflicts; the current report does not guess one.",
            "- Go/Java site validation and every remaining HYPOTHESES.md precondition are required before this evidence can support the full Phase 0 surviving-site amendment or a pilot launch.",
            "",
            "## Per-claim confidence",
            "",
            "| Claim | Confidence | Reason |",
            "|---|---|---|",
            "| The Python census is 24 Click and 2 Pygments sites | High | Every canonical JSONL row was identity-reconciled under the exact frozen predicate; missing, duplicate, or extra final results prevent publication. |",
            f"| Exactly {click['validated'] + pygments['validated']} sites satisfy the two-sided validation predicate | High for these executions; environment-conditional | Every verdict is recomputed from both sides' focal selection, five base attempts, red, green, hashes, and raw counts. Historical or future environments may differ. |",
            "| Five-run base subsets were deterministic where reported PASS | Medium-high | All five normalized JUnit signatures and green predicates agree, which detects observed instability but not rare flakes. |",
            f"| The operational contradictory subset contains {len(contradictory)} site(s) | High for stable constructible cases; no claim for withheld cases | The exact joint-source patch, qualifying test-level red predicate, and immediate rerun rule are enforced. Textual conflicts, rejected sites, and disagreeing reruns remain explicitly unclassified. |",
            "| These results generalize beyond the selected Python repositories or focal tests | Low / unsupported | The population is a purposive two-repository Python subset evaluated under one modern gated environment. |",
            "| The approved arms-ladder hypotheses are confirmed | Not evaluated | This report contains no agent draw or arms outcome. |",
            "",
            "## Evidence and provenance",
            "",
            f"- Renderer: `exploratory/arms/render_results.py`, SHA-256 `{renderer_sha}`.",
            f"- Frozen protocol: `exploratory/arms/protocol.json`, SHA-256 `{protocol_sha}`.",
            f"- Validator shared by every included result: `exploratory/arms/validate_sites.py`, SHA-256 `{runner_sha}`.",
            "- Per-site patch manifests are under `exploratory/arms/patches/<repo>/<merge>/manifest.json`; complete final records and all raw pytest/JUnit/temp evidence are under `exploratory/arms/raw/<repo>/<merge>/`; clean publication ledgers are under each repository's `_batches/` administrative directory.",
            "- `sites.json` contains full focal leaf IDs, patch paths and hashes, six check summaries and counts, exact rejection reasons, joint attempts, and source evidence paths.",
            "",
        ]
    )
    return "\n".join(lines)


def write_once(path: Path, payload: bytes) -> None:
    if path.exists():
        require(path.is_file() and not path.is_symlink(), f"refusing redirected output: {path}")
        require(path.read_bytes() == payload, f"refusing to replace differing existing result document: {path}")
        return
    temporary = path.with_name(path.name + f".render-{os.getpid()}.tmp")
    require(not temporary.exists(), f"temporary output already exists: {temporary}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_outputs() -> tuple[dict[str, Any], str]:
    renderer_sha = sha256_file(Path(__file__).resolve())
    protocol, protocol_sha = load_protocol()
    runner_sha = sha256_file(RUNNER_PATH)
    require(
        runner_sha == FROZEN_RUNNER_SHA256,
        f"validator hash is not the frozen candidate: expected {FROZEN_RUNNER_SHA256}, found {runner_sha}",
    )
    sites: list[dict[str, Any]] = []
    batch_provenance: dict[str, Any] = {}
    runtime_reference: dict[str, Any] | None = None
    for spec in REPOSITORIES:
        rows = load_census(spec)
        assert_result_population(spec, {str(row["merge"]) for row in rows})
        for index, row in enumerate(rows):
            site = validate_site_result(spec, index, row, protocol, protocol_sha, runner_sha)
            runtime = site["runtime_preflight"]
            if runtime_reference is None:
                runtime_reference = runtime
            else:
                require(runtime == runtime_reference, f"runtime preflight differs at {site['merge']}")
            sites.append(site)
        repo_sites = [site for site in sites if site["repo"] == spec.repository]
        batch_provenance[spec.repository] = validate_clean_batch_provenance(
            spec, repo_sites, protocol_sha, runner_sha,
        )
    require(len(sites) == 26, f"expected 26 complete site rows, found {len(sites)}")
    require(len({(site["repo"], site["merge"]) for site in sites}) == 26, "duplicate final site identity")
    population = summarize_population(sites)
    output = {
        "schema_version": SCHEMA_VERSION,
        "kind": "python_arms_ladder_site_validation",
        "protocol": {
            "path": project_relative(PROTOCOL_PATH),
            "sha256": protocol_sha,
            "frozen_before_candidate_pytest": True,
        },
        "runner": {"path": project_relative(RUNNER_PATH), "sha256": runner_sha},
        "renderer": {"path": project_relative(Path(__file__).resolve()), "sha256": renderer_sha},
        "population": {
            "predicate": protocol["population"]["predicate"],
            "repositories": population,
            "attempted_total": 26,
            "validated_total": sum(site["validated"] for site in sites),
        },
        "patch_rule": protocol["patch_rule"],
        "focal_selection_rule": protocol["focal_selection_rule"],
        "apparatus_path_policy": protocol["apparatus_path_policy"],
        "environment": protocol["environment"],
        "batch_provenance": batch_provenance,
        "verdict_taxonomy": list(PRIMARY_VERDICTS),
        "joint_status_taxonomy": list(JOINT_STATUSES),
        "sites": sites,
    }
    markdown = render_markdown(protocol, protocol_sha, runner_sha, renderer_sha, population, sites)
    hashed_evidence_seen: set[tuple[str, str, int]] = set()
    for site in sites:
        evidence = site["evidence"]
        require(
            sha256_file(PROJECT_ROOT / evidence["raw_result"]) == evidence["raw_result_sha256"],
            f"raw result changed while reducing evidence: {site['merge']}",
        )
        require(
            sha256_file(PROJECT_ROOT / evidence["prepared_manifest"])
            == evidence["prepared_manifest_sha256"],
            f"prepared manifest changed while reducing evidence: {site['merge']}",
        )
        preparation = evidence["preparation_record"]
        require(
            sha256_file(PROJECT_ROOT / preparation["path"]) == preparation["sha256"],
            f"preparation record changed while reducing evidence: {site['merge']}",
        )
        batch_record = evidence["batch_record"]
        require(
            sha256_file(PROJECT_ROOT / batch_record["path"]) == batch_record["sha256"],
            f"clean batch record changed while reducing evidence: {site['merge']}",
        )
        verify_all_hashed_file_records(site, f"site[{site['repo']}:{site['merge']}]", hashed_evidence_seen)
    require(sha256_file(Path(__file__).resolve()) == renderer_sha, "renderer changed while reducing evidence")
    require(sha256_file(PROTOCOL_PATH) == protocol_sha, "protocol changed while reducing evidence")
    require(sha256_file(RUNNER_PATH) == runner_sha, "validator changed while reducing evidence")
    return output, markdown


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument(
        "--check-only", action="store_true",
        help="validate and render in memory without publishing SITES.md or sites.json",
    )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        output, markdown = build_outputs()
        json_payload = canonical_json_bytes(output)
        markdown_payload = markdown.encode("utf-8")
        if not args.check_only:
            # Check every existing destination before creating either missing file.
            for path, payload in ((MARKDOWN_PATH, markdown_payload), (JSON_PATH, json_payload)):
                if path.exists():
                    require(path.is_file() and not path.is_symlink(), f"refusing redirected output: {path}")
                    require(path.read_bytes() == payload, f"refusing to replace differing existing result document: {path}")
            write_once(MARKDOWN_PATH, markdown_payload)
            write_once(JSON_PATH, json_payload)
        print(
            json.dumps(
                {
                    "check_only": args.check_only,
                    "sites": len(output["sites"]),
                    "validated": output["population"]["validated_total"],
                    "SITES_md_sha256": sha256_bytes(markdown_payload),
                    "sites_json_sha256": sha256_bytes(json_payload),
                },
                sort_keys=True,
            )
        )
        return 0
    except RenderError as error:
        print(f"render_results.py: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
