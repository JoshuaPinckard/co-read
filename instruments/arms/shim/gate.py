#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .gitops import ScratchRepository
    from .harness import ARMS
    from .normalize import (
        digest,
        event_paths,
        normalized_run_bytes,
        read_jsonl,
        verify_event_log,
        write_exclusive,
    )
    from .schema import Site, load_python_site
    from .production_scheduler import ProductionScheduler
    from .scripted_runner import ScriptedRunnerFactory, logical_agent_wall
    from .util import LogicalClock, ShimError, atomic_json, canonical_json, sha256_file
    from .validators import ValidationConfig, Validator
except ImportError:
    package_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(package_root))
    from instruments.arms.shim.gitops import ScratchRepository
    from instruments.arms.shim.harness import ARMS
    from instruments.arms.shim.normalize import (
        digest,
        event_paths,
        normalized_run_bytes,
        read_jsonl,
        verify_event_log,
        write_exclusive,
    )
    from instruments.arms.shim.schema import Site, load_python_site
    from instruments.arms.shim.production_scheduler import ProductionScheduler
    from instruments.arms.shim.scripted_runner import (
        ScriptedRunnerFactory,
        logical_agent_wall,
    )
    from instruments.arms.shim.util import (
        LogicalClock,
        ShimError,
        atomic_json,
        canonical_json,
        sha256_file,
    )
    from instruments.arms.shim.validators import ValidationConfig, Validator

from instruments.arms.canary.instrument import check_certificate_set


GATE_SCHEMA = "arms-shim-gate/v1"
RUN_ID = "arms-shim-gate-scripted-v1"


@dataclass(frozen=True)
class SiteSpec:
    name: str
    merge: str
    stratum: str
    mirror: str


MATRIX_SITES = (
    SiteSpec(
        "click-byte-intersecting",
        "65eceb08e392e74dcc761be2090e951274ccbe36",
        "byte-intersecting",
        "corpus/_conflict_mirrors/pallets__click",
    ),
    SiteSpec(
        "click-boundary-only-sensitivity",
        "11abf2bff0f48b7f7b04b38b6a70fb102ef17662",
        "boundary-only",
        "corpus/_conflict_mirrors/pallets__click",
    ),
)

CONTRACT_SITE = SiteSpec(
    "pygments-contradictory-contract",
    "00a31bcae2f61ce74ccfabd05be2731bfc7a5a28",
    "contradictory-task",
    "corpus/_conflict_mirrors/pygments__pygments",
)


def _load(project_root: Path, spec: SiteSpec) -> Site:
    return load_python_site(
        project_root,
        merge=spec.merge,
        stratum=spec.stratum,
        mirror=project_root / spec.mirror,
    )


def _command(command: Sequence[str], *, cwd: Path, timeout: float = 60.0) -> dict[str, Any]:
    executable = shutil.which(command[0])
    if not executable:
        return {"command": command[0], "present": False, "version": None}
    literal = [executable, *command[1:]]
    if os.name == "nt" and Path(executable).suffix.casefold() in {".cmd", ".bat"}:
        literal = [
            os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline([executable, *command[1:]]),
        ]
    try:
        completed = subprocess.run(
            literal,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=timeout,
            check=False,
        )
        version = (completed.stdout + completed.stderr).decode(
            "utf-8", errors="replace"
        ).strip()
        return {
            "command": command[0],
            "present": completed.returncode == 0,
            "returncode": completed.returncode,
            "version": version,
            "executable": str(Path(executable).resolve()),
            "executable_sha256": sha256_file(Path(executable)),
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "command": command[0],
            "present": False,
            "version": None,
            "executable": str(Path(executable).resolve()),
            "error": f"{type(error).__name__}: {error}",
        }


def environment_record(
    project_root: Path,
    certificates: Mapping[str, Mapping[str, Any]],
    certificate_paths: Mapping[str, Path],
) -> dict[str, Any]:
    python = ValidationConfig.from_protocol(project_root).python
    fake_root = project_root / "instruments" / "arms" / "shim" / "fakes"
    fake_scripts = {
        path.relative_to(project_root).as_posix(): sha256_file(path)
        for path in sorted(fake_root.glob("*.py"))
    }
    instrument_sources = {
        path.relative_to(project_root).as_posix(): sha256_file(path)
        for path in sorted((project_root / "instruments" / "arms").rglob("*.py"))
        if "__pycache__" not in path.parts
    }
    requested_models: dict[str, Any] = {}
    for surface, certificate in certificates.items():
        for row in certificate.get("surface_results", []):
            if isinstance(row, dict) and row.get("surface") == surface:
                requested_models[surface] = row.get("subject", {}).get(
                    "requested_model_identifier"
                )
    return {
        "schema_version": GATE_SCHEMA,
        "host": {
            "platform": platform.platform(),
            "python_runtime": sys.version,
            "timezone": str(datetime.now().astimezone().tzinfo),
        },
        "cli_versions": {
            "codex": _command(("codex", "--version"), cwd=project_root),
            "claude": _command(("claude", "--version"), cwd=project_root),
            "gemini": _command(("gemini", "--version"), cwd=project_root),
            "git": _command(("git", "--version"), cwd=project_root),
            "python": _command((str(python), "--version"), cwd=project_root),
        },
        "canary_requested_models": requested_models,
        "canary_certificates": {
            surface: {
                "path": str(certificate_paths[surface]),
                "sha256": sha256_file(certificate_paths[surface]),
                "verdict": certificates[surface].get("verdict"),
            }
            for surface in sorted(certificate_paths)
        },
        "fake_script_sha256": fake_scripts,
        "instrument_source_sha256": instrument_sources,
        "instrument_source_manifest_sha256": hashlib.sha256(
            canonical_json(instrument_sources)
        ).hexdigest(),
    }


def _directory_fingerprint(
    project_root: Path,
    root: Path,
    *,
    hash_contents: bool,
    excluded_prefixes: Sequence[Path] = (),
) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    excluded = tuple(path.resolve(strict=False) for path in excluded_prefixes)
    def is_excluded(path: Path) -> bool:
        resolved = path.resolve(strict=False)
        return any(resolved == prefix or prefix in resolved.parents for prefix in excluded)

    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        parent = Path(directory)
        directory_names[:] = sorted(
            name for name in directory_names if not is_excluded(parent / name)
        )
        file_names.sort()
        for name in file_names:
            path = parent / name
            if is_excluded(path):
                continue
            stat = path.lstat()
            if path.is_symlink():
                payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
                kind = "symlink"
                content_hash = hashlib.sha256(payload).hexdigest()
                size = len(payload)
            else:
                kind = "file"
                size = stat.st_size
                content_hash = sha256_file(path) if hash_contents else None
            record = {
                "path": path.relative_to(project_root).as_posix(),
                "kind": kind,
                "size_bytes": size,
                "mode": stat.st_mode,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": content_hash,
            }
            digest.update(canonical_json(record))
            digest.update(b"\n")
            file_count += 1
            byte_count += size
    return {
        "root": root.relative_to(project_root).as_posix(),
        "file_count": file_count,
        "byte_count": byte_count,
        "content_hashed": hash_contents,
        "manifest_sha256": digest.hexdigest(),
    }


def _status_snapshot(project_root: Path, output_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "status",
            "--porcelain=v2",
            "--untracked-files=all",
            "--",
            "fixture",
            "corpus",
            "prompts",
            "HYPOTHESES.md",
        ],
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    report_path = project_root / "exploratory" / "arms" / "SHIM-GATE.md"
    exact_roots = (
        project_root / "fixture",
        project_root / "prompts",
        project_root / "corpus" / "_conflict_mirrors" / "pallets__click",
        project_root / "corpus" / "_conflict_mirrors" / "pygments__pygments",
        project_root / "exploratory" / "arms",
        project_root / "instruments" / "arms",
    )
    exact = {
        root.relative_to(project_root).as_posix(): _directory_fingerprint(
            project_root,
            root,
            hash_contents=True,
            excluded_prefixes=(output_root, report_path),
        )
        for root in exact_roots
    }
    all_mirrors = _directory_fingerprint(
        project_root,
        project_root / "corpus" / "_conflict_mirrors",
        hash_contents=False,
    )
    hypotheses = project_root / "HYPOTHESES.md"
    return {
        "returncode": completed.returncode,
        "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        "stdout_bytes": len(completed.stdout),
        "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
        "exact_content_manifests": exact,
        "all_corpus_mirrors_metadata_manifest": all_mirrors,
        "hypotheses_worktree": {
            "path": "HYPOTHESES.md",
            "size_bytes": hypotheses.stat().st_size,
            "sha256": sha256_file(hypotheses),
        },
    }


def prompt_freeze_check(project_root: Path) -> dict[str, Any]:
    prompts = project_root / "prompts"
    manifest = prompts / "HASHES.txt"
    errors: list[str] = []
    checked: list[dict[str, Any]] = []
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 3 or len(parts[0]) != 64 or not parts[-1].endswith("bytes"):
            errors.append(f"line {line_number}: malformed hash manifest row")
            continue
        expected_hash = parts[0].casefold()
        name = parts[1]
        try:
            expected_bytes = int(parts[2])
        except ValueError:
            errors.append(f"line {line_number}: invalid byte count")
            continue
        path = prompts / name
        if not path.is_file():
            errors.append(f"line {line_number}: missing {name}")
            continue
        actual_hash = sha256_file(path)
        actual_bytes = path.stat().st_size
        row = {
            "path": f"prompts/{name}",
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "expected_bytes": expected_bytes,
            "actual_bytes": actual_bytes,
        }
        checked.append(row)
        if actual_hash != expected_hash or actual_bytes != expected_bytes:
            errors.append(f"line {line_number}: hash/size mismatch for {name}")
    return {
        "pass": not errors,
        "manifest": "prompts/HASHES.txt",
        "manifest_sha256": sha256_file(manifest),
        "checked_count": len(checked),
        "shim_prompt": next(
            (row for row in checked if row["path"] == "prompts/job-shim-build.txt"),
            None,
        ),
        "errors": errors,
    }


def approved_amendment_record(project_root: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    ).stdout.decode("ascii", errors="replace").strip()
    head_file = subprocess.run(
        ["git", "show", "HEAD:HYPOTHESES.md"],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    text = head_file.stdout.decode("utf-8", errors="replace")
    worktree = project_root / "HYPOTHESES.md"
    head_hash = hashlib.sha256(head_file.stdout).hexdigest()
    worktree_hash = sha256_file(worktree)
    approved = all(
        phrase in text
        for phrase in (
            "Amendment 2",
            "APPROVED by the PI",
            "optimistic isolation",
            "Arms become: sequential, unmediated shared tree, optimistic",
        )
    )
    return {
        "pass": head_file.returncode == 0 and approved,
        "head_commit": head,
        "head_hypotheses_sha256": head_hash,
        "worktree_hypotheses_sha256": worktree_hash,
        "worktree_matches_head": head_hash == worktree_hash,
        "approved_six_arm_text_present_at_head": approved,
        "real_draw_note": (
            "worktree differs from committed approved blob; resolve before real draw"
            if head_hash != worktree_hash
            else "worktree matches committed approved blob"
        ),
    }


def run_matrix(project_root: Path, run_root: Path, *, label: str) -> list[dict[str, Any]]:
    run_root.mkdir(parents=True, exist_ok=False)
    config = ValidationConfig.from_protocol(project_root)
    all_metrics: list[dict[str, Any]] = []
    for spec in MATRIX_SITES:
        site = _load(project_root, spec)
        scratch = ScratchRepository(site=site, root=run_root / "scratch" / spec.name)
        scratch.create()
        validator = Validator(project_root=project_root, config=config)
        harness = ProductionScheduler(
            project_root=project_root,
            site=site,
            scratch=scratch,
            validator=validator,
            runner_factory=ScriptedRunnerFactory(site),
            event_clock_factory=LogicalClock,
            wall_seconds_accounting=logical_agent_wall,
            scripted_gate_policy=True,
        )
        for arm in sorted(ARMS):
            for repeat in (1, 2):
                draw_id = f"{site.repo_slug}-{site.merge[:8]}-a{arm}-r{repeat}"
                print(f"[{label}] {draw_id}", flush=True)
                metrics = harness.run_draw(
                    arm=arm,
                    repeat=repeat,
                    root=run_root / "draws" / draw_id,
                    run_id=RUN_ID,
                )
                all_metrics.append(metrics)
    contract_site = _load(project_root, CONTRACT_SITE)
    contract_scheduler = ProductionScheduler(
        project_root=project_root,
        site=contract_site,
        # Contract issuance exits before any owned clone/worktree operation.
        scratch=ScratchRepository(
            site=contract_site, root=run_root / "contract-screen-unused-scratch"
        ),
        validator=Validator(project_root=project_root, config=config),
        runner_factory=lambda *_: (_ for _ in ()).throw(
            ShimError("contract screen attempted a forbidden subject selection")
        ),
        event_clock_factory=LogicalClock,
        wall_seconds_accounting=logical_agent_wall,
        scripted_gate_policy=True,
    )
    contract_metrics = contract_scheduler.run_draw(
        arm=6,
        repeat=1,
        root=run_root / "contract-screen",
        run_id=RUN_ID,
    )
    contract = {
        "contradiction_surfaced": contract_metrics.get("contract_screened") is True,
        "subject_launches": 0,
        "scheduler": contract_metrics.get("scheduler"),
        "draw_id": contract_metrics.get("draw_id"),
    }
    atomic_json(run_root / "contract-screen-result.json", contract)
    all_metrics.sort(key=lambda row: row["draw_id"])
    atomic_json(run_root / "metrics.json", all_metrics)
    normalized = normalized_run_bytes(run_root)
    write_exclusive(run_root / "events.timestamp-normalized.jsonl", normalized)
    verifications = [verify_event_log(path) for path in event_paths(run_root)]
    atomic_json(run_root / "event-log-verification.json", verifications)
    return all_metrics


def _metric_index(metrics: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int, int], Mapping[str, Any]]:
    return {
        (str(row["stratum"]), int(row["arm"]["id"]), int(row["repeat"])): row
        for row in metrics
    }


def _events(run_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in event_paths(run_root):
        rows.extend(read_jsonl(path))
    return rows


def _check(name: str, passed: bool, *, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "pass": bool(passed), "expected": expected, "actual": actual}


def gate_checks(
    run1: Path,
    run2: Path,
    metrics1: list[dict[str, Any]],
    metrics2: list[dict[str, Any]],
    *,
    canary_check: Mapping[str, Any],
    status_before: Mapping[str, Any],
    status_after: Mapping[str, Any],
    prompt_check: Mapping[str, Any],
    amendment_record: Mapping[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    index = _metric_index(metrics1)
    events = _events(run1)
    events2 = _events(run2)
    normalized1 = (run1 / "events.timestamp-normalized.jsonl").read_bytes()
    normalized2 = (run2 / "events.timestamp-normalized.jsonl").read_bytes()
    metrics_bytes1 = canonical_json(metrics1)
    metrics_bytes2 = canonical_json(metrics2)
    checks.append(
        _check(
            "approved six-arm Amendment 2 is committed",
            bool(amendment_record.get("pass")),
            expected={"approved_six_arm_text_present_at_head": True},
            actual=dict(amendment_record),
        )
    )
    checks.append(
        _check(
            "same-day canary prerequisite",
            bool(canary_check.get("pass")),
            expected={"pass": True, "required_surfaces": ["claude", "codex"]},
            actual=dict(canary_check),
        )
    )
    checks.append(
        _check(
            "frozen prompt manifest and shim-build prompt hash",
            bool(prompt_check.get("pass")) and prompt_check.get("shim_prompt") is not None,
            expected={"all_manifest_rows_match": True, "shim_prompt_present": True},
            actual=dict(prompt_check),
        )
    )
    cell_count1 = len(metrics1)
    cell_count2 = len(metrics2)
    unique1 = len({row["draw_id"] for row in metrics1})
    unique2 = len({row["draw_id"] for row in metrics2})
    expected_cells = {
        (spec.stratum, arm, repeat)
        for spec in MATRIX_SITES
        for arm in ARMS
        for repeat in (1, 2)
    }
    cells1 = {
        (str(row["stratum"]), int(row["arm"]["id"]), int(row["repeat"]))
        for row in metrics1
    }
    cells2 = {
        (str(row["stratum"]), int(row["arm"]["id"]), int(row["repeat"]))
        for row in metrics2
    }
    checks.append(
        _check(
            "two sites x six arms x two repeats in each independent run",
            (cell_count1, cell_count2, unique1, unique2) == (24, 24, 24, 24)
            and cells1 == expected_cells
            and cells2 == expected_cells,
            expected={"draws_per_run": 24, "unique_draws_per_run": 24},
            actual={
                "run_1_draws": cell_count1,
                "run_2_draws": cell_count2,
                "run_1_unique": unique1,
                "run_2_unique": unique2,
                "run_1_missing_cells": sorted(expected_cells - cells1),
                "run_1_extra_cells": sorted(cells1 - expected_cells),
                "run_2_missing_cells": sorted(expected_cells - cells2),
                "run_2_extra_cells": sorted(cells2 - expected_cells),
            },
        )
    )
    seam_rows = [*metrics1, *metrics2]
    seam_failures = [
        row["draw_id"]
        for row in seam_rows
        if row.get("scheduler", {}).get("kind") != "production-runner-seam"
        or row.get("scheduler", {}).get("scripted_gate_policy") is not True
    ]
    checks.append(
        _check(
            "all matrix cells exercised the production scheduler seam",
            len(seam_rows) == 48 and not seam_failures,
            expected={
                "matrix_rows": 48,
                "scheduler.kind": "production-runner-seam",
                "scheduler.scripted_gate_policy": True,
            },
            actual={
                "matrix_rows": len(seam_rows),
                "nonconforming_draw_ids": seam_failures,
            },
        )
    )
    integration_attempted = [
        row for row in seam_rows if row.get("integration_validation_attempted") is True
    ]
    missing_buildability = [
        row["draw_id"]
        for row in integration_attempted
        if not isinstance(row.get("buildability"), dict)
        or not row["buildability"].get("oracle")
        or "buildable" not in row["buildability"]
    ]
    checks.append(
        _check(
            "integration-attempted metrics retain the buildability oracle",
            bool(integration_attempted) and not missing_buildability,
            expected={
                "integration_attempted_min": 1,
                "all_have_oracle_and_buildable": True,
            },
            actual={
                "integration_attempted_rows": len(integration_attempted),
                "missing_buildability_draw_ids": missing_buildability,
            },
        )
    )
    checks.append(
        _check(
            "event logs identical after timestamp normalization",
            normalized1 == normalized2,
            expected="byte-identical",
            actual={
                "run_1_sha256": digest(normalized1),
                "run_2_sha256": digest(normalized2),
                "run_1_bytes": len(normalized1),
                "run_2_bytes": len(normalized2),
                "identical": normalized1 == normalized2,
            },
        )
    )
    checks.append(
        _check(
            "metrics identical",
            metrics_bytes1 == metrics_bytes2,
            expected="canonical-JSON-identical",
            actual={
                "run_1_sha256": digest(metrics_bytes1),
                "run_2_sha256": digest(metrics_bytes2),
                "identical": metrics_bytes1 == metrics_bytes2,
            },
        )
    )

    cheater = index[("byte-intersecting", 1, 2)]
    cheater_attempts = [row for row in cheater["attempts"] if row["mode"] == "cheater"]
    cheater_events = [
        row
        for row in events
        if row["draw_id"] == cheater["draw_id"]
        and row["op"] == "complete"
        and row["detail"].get("test_integrity_ok") is False
    ]
    cheater_mismatches = [
        item
        for event in cheater_events
        for item in event["detail"].get("test_mismatches", [])
    ]
    cheater_actual = {
        "attempts": cheater_attempts,
        "caught_complete_events": len(cheater_events),
        "mismatch_paths": sorted({row["path"] for row in cheater_mismatches}),
    }
    checks.append(
        _check(
            "test-modifying cheater rejected by byte-identity rule",
            len(cheater_attempts) == 1
            and cheater_attempts[0]["correct"] is False
            and bool(cheater_mismatches),
            expected={"cheater_correct": False, "test_mismatch_count_min": 1},
            actual=cheater_actual,
        )
    )

    staller = index[("boundary-only", 1, 2)]
    stalled = [row for row in staller["attempts"] if row["mode"] == "staller"]
    redrawn = [row for row in staller["attempts"] if row["slot_redraw_index"] == 1]
    staller_actual = {
        "staller_attempts": stalled,
        "redrawn_attempts": redrawn,
        "timeout_excluded_instances": staller.get("timeout_excluded_instances"),
        "slot_redraw_instances": staller.get("slot_redraw_instances"),
    }
    stalled_ids = sorted(row["instance_id"] for row in stalled)
    summarized_excluded = sorted(staller.get("timeout_excluded_instances", []))
    redrawn_ids = sorted(row["instance_id"] for row in redrawn if row["finished"])
    summarized_redrawn = sorted(staller.get("slot_redraw_instances", []))
    checks.append(
        _check(
            "staller timeout retry, exclusion, and slot redraw",
            len(stalled) == 2
            and all(row["excluded"] for row in stalled)
            and len(redrawn) == 1
            and redrawn[0]["finished"]
            and summarized_excluded == stalled_ids
            and summarized_redrawn == redrawn_ids,
            expected={
                "staller_exclusions": 2,
                "fresh_redraws": 1,
                "summary_ids_match_attempts": True,
            },
            actual=staller_actual,
        )
    )

    escalation = index[("byte-intersecting", 6, 2)]
    escalation_actual = {
        "escalation_count": escalation.get("escalation_count"),
        "escalation": escalation.get("escalation"),
    }
    checks.append(
        _check(
            "arm 6 N=3 alternating-region escalation",
            escalation.get("escalation_count") == 1
            and escalation.get("escalation", {}).get("side_sequence") == ["A", "B", "A", "B"]
            and escalation.get("escalation", {}).get("side_switches") == 3,
            expected={"side_sequence": ["A", "B", "A", "B"], "side_switches": 3},
            actual=escalation_actual,
        )
    )

    optimistic = index[("byte-intersecting", 3, 1)]
    optimistic_actual = {
        key: optimistic.get(key)
        for key in (
            "later_finisher",
            "later_finisher_instance",
            "retry_instance_ids",
            "discarded_instance_ids",
            "retry_compute_seconds",
            "discarded_diff_seconds",
            "wasted_compute_seconds",
            "integration_correct",
        )
    }
    checks.append(
        _check(
            "arm 3 fixed-loser retry accounting",
            optimistic.get("later_finisher") == "B"
            and len(optimistic.get("retry_instance_ids", [])) == 2
            and len(optimistic.get("discarded_instance_ids", [])) == 2
            and abs(float(optimistic.get("retry_compute_seconds", -1)) - 0.11) < 1e-12
            and abs(float(optimistic.get("discarded_diff_seconds", -1)) - 0.15) < 1e-12
            and abs(float(optimistic.get("wasted_compute_seconds", -1)) - 0.21) < 1e-12
            and optimistic.get("integration_correct") is True,
            expected={
                "loser": "B (later finisher)",
                "retry_count": 2,
                "retry_compute_seconds": 0.11,
                "discarded_diff_seconds": 0.15,
                "wasted_compute_seconds_union": 0.21,
            },
            actual=optimistic_actual,
        )
    )

    contract = json.loads((run1 / "contract-screen-result.json").read_text(encoding="utf-8"))
    contract_events = read_jsonl(run1 / "contract-screen" / "events.jsonl")
    contract_launches = sum(row["op"] == "launch" for row in contract_events)
    checks.append(
        _check(
            "arm 6 contradictory-task contract screen precedes dispatch",
            contract.get("contradiction_surfaced") is True
            and contract.get("subject_launches") == 0
            and contract_launches == 0
            and contract.get("scheduler", {}).get("kind")
            == "production-runner-seam",
            expected={
                "contradiction_surfaced": True,
                "subject_launches": 0,
                "scheduler.kind": "production-runner-seam",
            },
            actual={**contract, "launch_events": contract_launches},
        )
    )

    full_rows = [row for row in metrics1 if row["arm"]["id"] == 6]
    contested = sum(int(row.get("contested_region_pairs", 0)) for row in full_rows)
    bad_rates = [
        row["draw_id"]
        for row in full_rows
        if int(row.get("contested_region_pairs", 0)) > 0
        and row.get("log_only_attribution_rate") != 1.0
    ]
    checks.append(
        _check(
            "arm 6 contested writes attributed from mechanical logs only",
            contested > 0 and not bad_rates,
            expected={"contested_region_pairs_min": 1, "all_nonempty_rates": 1.0},
            actual={"contested_region_pairs": contested, "bad_rate_draws": bad_rates},
        )
    )

    log_verifications = [verify_event_log(path) for path in event_paths(run1)] + [
        verify_event_log(path) for path in event_paths(run2)
    ]
    checks.append(
        _check(
            "append-only JSONL schema and hash chains",
            len(log_verifications) == 50 and all(row["pass"] for row in log_verifications),
            expected={"all_logs_valid": True, "log_count": 50},
            actual={
                "log_count": len(log_verifications),
                "event_count": sum(row["events"] for row in log_verifications),
                "failures": [row for row in log_verifications if not row["pass"]],
            },
        )
    )

    subject_launches_by_run = {
        "run-1": [row for row in events if row["op"] == "launch"],
        "run-2": [row for row in events2 if row["op"] == "launch"],
    }
    subject_launches = [
        row for rows in subject_launches_by_run.values() for row in rows
    ]
    unexpected_subjects = sorted(
        {
            str(row["subject"].get("cli"))
            for row in subject_launches
            if row["subject"].get("cli") != "scripted-fake"
        }
    )
    checks.append(
        _check(
            "no real subject in shim draws",
            not unexpected_subjects,
            expected={"launch_subject_cli": "scripted-fake"},
            actual={
                "launch_events_by_run": {
                    label: len(rows)
                    for label, rows in subject_launches_by_run.items()
                },
                "unexpected_subject_clis": unexpected_subjects,
            },
        )
    )

    arm2_launches = [
        row
        for row in subject_launches
        if isinstance(row.get("arm"), dict) and row["arm"].get("id") == 2
    ]
    checks.append(
        _check(
            "fake-only shared-tree interleaving control is explicit",
            len(arm2_launches) == 16
            and all(
                row.get("detail", {}).get("scripted_write_release_control") is True
                for row in arm2_launches
            ),
            expected={
                "arm_2_launches": 16,
                "all_log_scripted_write_release_control": True,
                "release_order": ["A", "B"],
            },
            actual={
                "arm_2_launches": len(arm2_launches),
                "arm_2_launches_by_run": {
                    label: sum(
                        isinstance(row.get("arm"), dict)
                        and row["arm"].get("id") == 2
                        for row in rows
                    )
                    for label, rows in subject_launches_by_run.items()
                },
                "controlled_launches": sum(
                    row.get("detail", {}).get("scripted_write_release_control") is True
                    for row in arm2_launches
                ),
                "release_order": ["A", "B"],
                "production_barrier": False,
            },
        )
    )

    coordinator_retries = [
        row["draw_id"]
        for row in metrics1
        if row["arm"]["id"] == 5 and int(row.get("integration_retries", 0)) != 0
    ]
    checks.append(
        _check(
            "coordinator remains dispatch-only with zero integration retries",
            not coordinator_retries,
            expected={"integration_retries": 0},
            actual={"nonzero_retry_draws": coordinator_retries},
        )
    )

    violation = index[("boundary-only", 4, 2)]
    checks.append(
        _check(
            "file-lock under-declaration recorded but not blocked",
            int(violation.get("declaration_violations", 0)) > 0
            and float(violation.get("counterfactual_refusal_seconds", 0)) > 0,
            expected={"violations_min": 1, "counterfactual_refusal_seconds_positive": True},
            actual={
                "declaration_violations": violation.get("declaration_violations"),
                "counterfactual_refusal_seconds": violation.get("counterfactual_refusal_seconds"),
                "schedule": violation.get("schedule"),
            },
        )
    )

    checks.append(
        _check(
            "protected exact roots and unused-mirror metadata unchanged",
            status_before == status_after,
            expected=(
                "before/after Git-state; exact fixture, prompt, arms, and used-mirror "
                "content; HYPOTHESES bytes; and unused-mirror metadata fingerprints identical"
            ),
            actual={"before": dict(status_before), "after": dict(status_after)},
        )
    )
    return checks


def artifact_inventory(project_root: Path) -> dict[str, Any]:
    python_sites = json.loads(
        (project_root / "exploratory" / "arms" / "sites.json").read_text(encoding="utf-8")
    )["sites"]
    go = json.loads(
        (project_root / "exploratory" / "arms" / "sites-go.json").read_text(encoding="utf-8")
    )
    java = json.loads(
        (project_root / "exploratory" / "arms" / "sites-java.json").read_text(encoding="utf-8")
    )
    python_validated = sum(
        row.get("validated") is True and row.get("verdict") == "VALIDATED"
        for row in python_sites
    )
    java_passed = sum(row.get("verdict") == "passed" for row in java)
    go_eligible = int(go.get("population", {}).get("eligible_total", 0))
    go_validated = sum(row.get("validated") is True for row in go.get("sites", []))
    return {
        "python_two_sided_validated": python_validated,
        "go_runner_eligible": go_eligible,
        "go_validated_true": go_validated,
        "go_validation_scope": go.get("validation_scope"),
        "java_runner_passed": java_passed,
        "java_validated_true": sum(row.get("validated") is True for row in java),
        "numeric_total_python_plus_go_eligible_plus_java_passed": (
            python_validated + go_eligible + java_passed
        ),
    }


def _format_actual(value: Any) -> str:
    return "```json\n" + json.dumps(value, indent=2, sort_keys=True) + "\n```"


def render_report(
    *,
    project_root: Path,
    output_root: Path,
    checks: Sequence[Mapping[str, Any]],
    environment: Mapping[str, Any],
    canary_paths: Mapping[str, Path],
    canaries: Mapping[str, Mapping[str, Any]],
    canary_check: Mapping[str, Any],
    inventory: Mapping[str, Any],
    amendment_record: Mapping[str, Any],
) -> str:
    passed = all(row.get("pass") for row in checks)
    verdict = "PASS" if passed else "FAIL"
    lines = [
        f"{verdict} - ARMS SHIM scripted gate ({sum(row.get('pass') is True for row in checks)}/{len(checks)} checks passed); no real-subject draw launched",
        "",
        "# ARMS SHIM gate",
        "",
        f"This gate executed 48 scripted draws: two independently created runs of two real Click sites, all six approved arms, and two repeats per arm. It also executed the contradictory Pygments dispatch screen once per run. No real subject was launched by the shim; the only model calls represented here are the {canary_check.get('aggregate_model_calls')} separately bounded canary calibration probes.",
        "Every matrix cell and contradictory dispatch screen traversed `ProductionScheduler`; the injected runner was the deterministic fake policy, not a production CLI launcher.",
        "",
        "The byte-intersecting site is Click `65eceb08e392e74dcc761be2090e951274ccbe36` (corpus `overlap`, strict byte intersection). The second site is Click `11abf2bff0f48b7f7b04b38b6a70fb102ef17662`, retained under its exact corpus label `boundary_only` as the Amendment 2 sensitivity class; it is not relabeled `same_file_disjoint` or `permissive`. The contract-screen site is Pygments `00a31bcae2f61ce74ccfabd05be2731bfc7a5a28` (`MUTUALLY_UNSATISFIABLE`). Base commit identities and their resolved tree hashes are both present in every event.",
        "",
        "## Per-check evidence",
        "",
    ]
    for row in checks:
        mark = "PASS" if row["pass"] else "FAIL"
        lines.extend(
            [
                f"### {mark}: {row['name']}",
                "",
                "Expected:",
                "",
                _format_actual(row["expected"]),
                "",
                "Actual output:",
                "",
                _format_actual(row["actual"]),
                "",
            ]
        )
    lines.extend(
        [
            "## Fake-subject script hashes",
            "",
            "| Script | SHA-256 |",
            "| --- | --- |",
        ]
    )
    for path, value in sorted(environment["fake_script_sha256"].items()):
        lines.append(f"| `{path}` | `{value}` |")
    lines.extend(
        [
            "",
            "## Instrument source manifest",
            "",
            _format_actual(
                {
                    "file_count": len(environment["instrument_source_sha256"]),
                    "manifest_sha256": environment[
                        "instrument_source_manifest_sha256"
                    ],
                    "per_file_hashes": "environment.json#instrument_source_sha256",
                }
            ),
            "",
            "## CLI versions detected",
            "",
            "| CLI | Present | Exact version output | Requested canary model |",
            "| --- | --- | --- | --- |",
        ]
    )
    requested = environment.get("canary_requested_models", {})
    for name, record in environment["cli_versions"].items():
        version = str(record.get("version") or "absent").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {name} | {str(bool(record.get('present'))).lower()} | `{version}` | "
            f"`{requested.get(name, 'not probed')}` |"
        )
    lines.extend(
        [
            "",
            "## Canary certificates",
            "",
            _format_actual(
                {
                    "aggregate_check": dict(canary_check),
                    "sources": {
                        surface: {
                            "path": path.resolve().relative_to(project_root).as_posix(),
                            "verdict": canaries[surface].get("verdict"),
                            "calibration_day": canaries[surface].get("calibration_day"),
                            "probe_budget": canaries[surface].get("probe_budget"),
                            "certified_surfaces": canaries[surface].get(
                                "certified_surfaces"
                            ),
                        }
                        for surface, path in sorted(canary_paths.items())
                    },
                    "requested_models": requested,
                }
            ),
            "",
            "## Claims that could NOT be verified",
            "",
            f"- The supplied phrase **19 validated sites** is not supported by one uniform validation predicate. Actual inventory: `{json.dumps(inventory, sort_keys=True)}`. The artifacts numerically total 19 only by adding 11 independently two-sided Python red/green sites, 2 Go runner-eligible sites explicitly marked `validated:false`, and 6 Java runner `passed` sites with no `validated:true` field. This shim therefore gates only two of the 11 sites accepted by the strict Python loader.",
            "- The corrected Amendment 2 `permissive` population has no prepared, independently validated site artifact in this checkout. The second matrix site is explicitly a `boundary_only` sensitivity site, not evidence about the permissive stratum.",
            "- The frozen `prompts/HASHES.txt` check covers the existing prompt artifacts (including `job-shim-build.txt`), but the actual subject task/declaration composition currently lives as code templates in `adapters.py` and is not a PI-frozen prompt artifact. Before any real draw, those exact templates and composition rules must be frozen under `prompts/` by a separately authorized change; this job was forbidden from modifying that directory.",
            (
                "- At gate time the working copy of `HYPOTHESES.md` matched the committed "
                "approved blob at `HEAD`. This instrument gate does not independently "
                "authorize, schedule, or launch a real-subject draw."
                if amendment_record.get("worktree_matches_head")
                else
                "- At gate time the working copy of `HYPOTHESES.md` differed from the "
                "committed approved blob at `HEAD`; a real-subject draw would remain "
                "forbidden by precondition 1 until the intended amendment state is committed."
            ),
            "- `otherwise buildable` is evaluated here only by the disclosed `python-source-syntax-compile-v1` screen over all non-test Python files. No preregistered repository full-build or full-suite command exists, so stronger buildability remains unverified.",
            "- Real-agent efficacy, live concurrency timing, provider throttling, and model-specific behavior were not tested; this job was explicitly restricted to scripted fake subjects.",
            "- Scripted per-wall throughput uses a deterministic schedule-aware critical path over retained fake durations (serial sums, parallel maxima, plus retries/declarations). It verifies metric plumbing and retry accounting, not host-clock or real-provider efficiency.",
            "- The Gemini adapter and environment-manifest surface were implemented, and the installed CLI version was detected, but Gemini was deliberately not calibrated or called in this job. Production launch remains fail-closed for that uncalibrated surface.",
            "- A requested model string in a canary certificate cannot prove a mutable provider-side alias snapshot unless the provider exposes an immutable resolved identifier.",
            "- The six-call total is six subject-CLI model-probe invocations. Vendor-internal HTTP or inference retries inside one CLI invocation are opaque and could not be counted independently.",
            "- Instruction discovery is version-sensitive. The canary covers the documented local, project, managed-policy, settings, rules, skills, plugin, MCP, and auto-memory candidates enumerated by the current instrument; undocumented future/server-managed channels remain outside the proof.",
            "- Arm 2 and parallel arm 4 intentionally provide only shared-pair attribution because both subjects inhabit one tree. The 100% log-only contested-write attribution claim is gated for arm 6, where separate worktrees make principal attribution identifiable.",
            "- Parallel arm 4 can score only declaration coverage against the shared pair-union snapshot. It cannot prove per-agent declaration accuracy: for example, two agents swapping undeclared paths can be hidden by the declared union. Per-side declaration accuracy is reported only where worktrees are attributable.",
            "- Reproducibility of scripted shared-tree cells is certified for one disclosed fake-only A-then-B write-release interleaving after both processes launch. It does not estimate the distribution of operating-system schedules or outcomes for real unmediated arm 2; the production launcher has no such barrier.",
            "- A real shared-tree completion snapshot is a non-atomic per-file filesystem walk. Without the fake-only completion handshake used by this gate, a peer can write between files in that walk, so the snapshot is mechanically retained but cannot certify one cross-file instant. This limitation does not affect arm 6, which uses separate worktrees.",
            "- Filesystem snapshots observe retained differences at completion and at the configured poll instants. A write that is fully restored between snapshots is not observable. The 100% log-only attribution claim therefore applies to contested snapshot-visible retained regions, not to every filesystem write syscall.",
            "- Protected-state evidence hashes exact bytes for `fixture/`, `prompts/`, `instruments/arms/`, `exploratory/arms/`, `HYPOTHESES.md`, and the two used Click/Pygments mirrors. Other unused corpus mirrors are covered only by path/kind/size/mode/mtime metadata, not by byte hashes.",
            "",
            "## What would change this verdict",
            "",
            "- Any failed check above, any hash-chain break, a non-scripted launch in the shim logs, or a mismatch between normalized runs changes the verdict to FAIL.",
            "- Missing, stale, tampered, semantically invalid, or collectively over-budget Codex/Claude canary evidence prevents the first draw and changes the verdict to FAIL. A surface certified inside an otherwise failed immutable multi-surface run remains admissible only when its raw evidence independently revalidates.",
            "- A preregistered uniform buildability oracle would replace the current `null`; a red oracle result would make affected integration outcomes incorrect.",
            "- Go/Java sites may join the validated population only after artifacts demonstrate the same two-sided source/test red-green discrimination predicate used by the Python loader.",
            "- Re-running with different source, fixture, site-manifest, corpus-line, patch, CLI, or fake-script hashes is a new instrument version and requires a fresh gate.",
            "- A real draw remains blocked until the exact subject task and declaration prompts are PI-frozen and hash-manifested; changing them afterward requires a new instrument gate.",
            "",
            "## Per-claim confidence",
            "",
            "| Claim | Confidence | Reason |",
            "| --- | --- | --- |",
            "| Timestamp-normalized event and metric reproducibility | High | Compared complete canonical bytes from two independently cloned scratch repositories. |",
            "| Cheater, timeout/redraw, arm-3 retry, and arm-6 escalation behavior | High | Each is asserted from append-only mechanical events plus the matching deterministic metric record. |",
            "| P4 log-only attribution for contested arm-6 retained regions | High for this scripted gate | Snapshot-visible claims and byte regions come from filesystem state; subject prose is not parsed. Transient write-and-restore activity and generalization to real subjects were not tested. |",
            "| Clean-room instruction-channel firing and absence | High for the certified CLI versions, paths, host, and timestamp | Both planted markers fired and both clean acknowledgements were observed under separate redirected rooms; retained responses and manifests are hash-checked. |",
            "| Uniform 19-site validation | Low / contradicted by artifacts | Only 11 records satisfy the loader's independent two-sided `validated:true` and `VALIDATED` predicate. |",
            "| Otherwise buildable | Moderate for Python syntax; not estimable for a full repository build | The gate runs the disclosed syntax-build screen, but no frozen full-build/full-suite oracle exists. |",
            "",
            "## Evidence roots",
            "",
            f"- `{(output_root / 'run-1').relative_to(project_root).as_posix()}`",
            f"- `{(output_root / 'run-2').relative_to(project_root).as_posix()}`",
            f"- `{(output_root / 'gate-checks.json').relative_to(project_root).as_posix()}`",
            f"- `{(output_root / 'environment.json').relative_to(project_root).as_posix()}`",
            "",
        ]
    )
    interrupted = project_root / "exploratory" / "arms" / "shim-gate"
    if interrupted.exists() and interrupted.resolve() != output_root.resolve():
        lines.extend(
            [
                "Interrupted development evidence (excluded from every verdict/check):",
                "",
                f"- `{interrupted.relative_to(project_root).as_posix()}` - retained after "
                "a pre-final run was stopped when the production-runner wiring audit "
                "found a scope blocker.",
                "",
            ]
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the deterministic ARMS SHIM gate")
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument(
        "--canary",
        action="append",
        required=True,
        metavar="SURFACE=PATH",
        help="repeat once for codex and once for claude",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="defaults to exploratory/arms/shim-gate",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    canary_paths: dict[str, Path] = {}
    for assignment in args.canary:
        surface, separator, raw_path = assignment.partition("=")
        if not separator or surface not in {"codex", "claude"} or not raw_path:
            raise ShimError("--canary expects codex=PATH or claude=PATH")
        if surface in canary_paths:
            raise ShimError(f"duplicate --canary surface: {surface}")
        canary_paths[surface] = Path(raw_path).resolve()
    if set(canary_paths) != {"codex", "claude"}:
        raise ShimError("both codex and claude same-day canary evidence are required")
    canary_check = check_certificate_set(canary_paths)
    if not canary_check["pass"]:
        raise ShimError(
            "same-day canary gate failed before first draw: "
            + "; ".join(canary_check["errors"])
        )
    canaries = {
        surface: json.loads(path.read_text(encoding="utf-8"))
        for surface, path in canary_paths.items()
    }
    prompt_check = prompt_freeze_check(project_root)
    if not prompt_check["pass"] or prompt_check["shim_prompt"] is None:
        raise ShimError(
            "frozen prompt precondition failed before first draw: "
            + "; ".join(prompt_check["errors"])
        )
    amendment_record = approved_amendment_record(project_root)
    if not amendment_record["pass"]:
        raise ShimError("approved six-arm Amendment 2 is not present at HEAD")
    output_root = (
        args.output_root.resolve()
        if args.output_root
        else project_root / "exploratory" / "arms" / "shim-gate"
    )
    if output_root.exists():
        raise ShimError(f"refusing to overwrite gate evidence: {output_root}")
    output_root.mkdir(parents=True, exist_ok=False)
    status_before = _status_snapshot(project_root, output_root)
    environment = environment_record(project_root, canaries, canary_paths)
    atomic_json(output_root / "environment.json", environment)
    metrics1 = run_matrix(project_root, output_root / "run-1", label="run-1")
    metrics2 = run_matrix(project_root, output_root / "run-2", label="run-2")
    status_after = _status_snapshot(project_root, output_root)
    checks = gate_checks(
        output_root / "run-1",
        output_root / "run-2",
        metrics1,
        metrics2,
        canary_check=canary_check,
        status_before=status_before,
        status_after=status_after,
        prompt_check=prompt_check,
        amendment_record=amendment_record,
    )
    inventory = artifact_inventory(project_root)
    record = {
        "schema_version": GATE_SCHEMA,
        "verdict": "PASS" if all(row["pass"] for row in checks) else "FAIL",
        "checks": checks,
        "artifact_inventory": inventory,
    }
    atomic_json(output_root / "gate-checks.json", record)
    report = render_report(
        project_root=project_root,
        output_root=output_root,
        checks=checks,
        environment=environment,
        canary_paths=canary_paths,
        canaries=canaries,
        canary_check=canary_check,
        inventory=inventory,
        amendment_record=amendment_record,
    )
    report_path = project_root / "exploratory" / "arms" / "SHIM-GATE.md"
    if report_path.exists():
        raise ShimError(f"refusing to overwrite report: {report_path}")
    write_exclusive(report_path, report.encode("utf-8"))
    print(json.dumps(record, indent=2, sort_keys=True), flush=True)
    return 0 if record["verdict"] == "PASS" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ShimError as error:
        print(f"ARMS SHIM gate error: {error}", file=sys.stderr)
        raise SystemExit(2)
