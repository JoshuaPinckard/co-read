from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapters import ScriptedAdapter, parse_declaration, task_prompt
from .gitops import MergeResult, ScratchRepository, source_paths_from_records
from .schema import Side, Site, named_intent_paths
from .util import (
    EventLog,
    LogicalClock,
    ProcessResult,
    ShimError,
    Snapshot,
    atomic_json,
    canonical_json,
    diff_snapshots,
    finish_process,
    regions_overlap,
    sha256_bytes,
    snapshot_tree,
    start_process,
    tree_path,
)
from .validators import Validator, is_test_path, selected_manifest, test_integrity


ARMS = {
    1: {"id": 1, "name": "sequential"},
    2: {"id": 2, "name": "unmediated-shared"},
    3: {"id": 3, "name": "optimistic-isolation"},
    4: {"id": 4, "name": "file-locks"},
    5: {"id": 5, "name": "coordinator"},
    6: {"id": 6, "name": "full-system"},
}

PRODUCTION_TIMEOUT_SECONDS = 20 * 60
PRODUCTION_POLL_SECONDS = 30
MAX_TIMEOUT_RETRIES = 1
MAX_OPTIMISTIC_RETRIES = 2
ESCALATION_SIDE_SWITCHES = 3
GATE_STALL_TIMEOUT_SECONDS = 1.0


@dataclasses.dataclass
class PreparedTree:
    tree: Path
    source_base: str
    baseline: Snapshot
    expected_tests: dict[str, dict[str, Any]]


@dataclasses.dataclass
class Attempt:
    side: str
    instance_id: str
    subject_slot: int
    timeout_retry: int
    integration_retry: int
    tree: Path
    source_base: str
    process: ProcessResult
    records: list[dict[str, Any]]
    finished: bool
    excluded: bool
    test_integrity_ok: bool
    test_mismatches: list[dict[str, Any]]
    focal: dict[str, Any] | None
    correct: bool
    logical_seconds: float
    mode: str
    source_commit: str | None = None


def _detail_validation(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "green": bool(record.get("green")),
        "returncode": record.get("returncode"),
        "timed_out": bool(record.get("timed_out")),
        "test_integrity_after_validation": record.get("test_integrity_after_validation"),
        "targets": list(record.get("targets", [])),
    }


class Harness:
    def __init__(
        self,
        *,
        project_root: Path,
        site: Site,
        scratch: ScratchRepository,
        validator: Validator,
        gate_timeout_seconds: float = 60.0,
        gate_poll_seconds: float = 0.35,
    ) -> None:
        self.project_root = project_root.resolve()
        self.site = site
        self.scratch = scratch
        self.validator = validator
        self.gate_timeout_seconds = gate_timeout_seconds
        self.gate_poll_seconds = gate_poll_seconds
        self.fake_root = Path(__file__).resolve().parent / "fakes"
        self._worktree_counter = 0

    @property
    def all_test_paths(self) -> set[str]:
        return set(self.site.sides["A"].test_paths) | set(self.site.sides["B"].test_paths)

    def _name(self, label: str) -> str:
        self._worktree_counter += 1
        clean = "".join(character for character in label if character.isalnum())[:10]
        return f"w{self._worktree_counter:04d}{clean}"

    def _prepare(
        self,
        *,
        source_base: str,
        test_sides: Sequence[str],
        label: str,
        log: EventLog,
    ) -> PreparedTree:
        tree = self.scratch.worktree(self._name(label), source_base)
        applied: list[dict[str, Any]] = []
        for side_label in test_sides:
            side = self.site.sides[side_label]
            ok, stdout, stderr = self.scratch.apply_patch(tree, side.test_patch)
            applied.append(
                {
                    "side": side_label,
                    "patch_sha256": side.test_patch_sha256,
                    "ok": ok,
                    "stdout_sha256": sha256_bytes(stdout),
                    "stderr_sha256": sha256_bytes(stderr),
                }
            )
            if not ok:
                log.emit(
                    "merge",
                    principal="harness",
                    detail={"kind": "test-overlay", "applied": applied, "ok": False},
                )
                raise ShimError(f"test patch for side {side_label} did not apply in {label}")
        baseline = snapshot_tree(tree)
        expected_tests = {
            side_label: selected_manifest(baseline, self.site.sides[side_label].test_paths)
            for side_label in test_sides
        }
        log.emit(
            "merge",
            principal="harness",
            detail={"kind": "test-overlay", "applied": applied, "ok": True},
        )
        return PreparedTree(tree, source_base, baseline, expected_tests)

    def _fake_spec(
        self,
        *,
        side: Side,
        mode: str,
        logical_seconds: float,
        declared_paths: Sequence[str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec: dict[str, Any] = {
            "side": side.label,
            "source_patch": str(side.source_patch),
            "source_patch_sha256": side.source_patch_sha256,
            "source_paths": list(sorted(set(self.site.sides["A"].source_paths) | set(self.site.sides["B"].source_paths))),
            "test_paths": list(side.test_paths),
            "declared_paths": list(declared_paths if declared_paths is not None else side.source_paths),
            "answer_commit": self.site.answer_commit,
            "logical_seconds": logical_seconds,
            # Keep the finish gap much larger than Windows process-start
            # jitter so the fixed later-finisher rule is reproducible.
            "pre_delay_seconds": 0.02 if side.label == "A" else 0.10,
            "post_delay_seconds": 0.10 if side.label == "A" else 2.50,
        }
        if mode == "cheater":
            spec["cheat_path"] = side.test_paths[0]
        if extra:
            spec.update(dict(extra))
        return spec

    def _script(self, mode: str) -> Path:
        path = self.fake_root / f"{mode}.py"
        if not path.is_file():
            raise ShimError(f"fake subject script is absent: {path}")
        return path

    def _start_fake(
        self,
        *,
        prepared: PreparedTree,
        side: Side,
        mode: str,
        spec: Mapping[str, Any],
        attempt_root: Path,
        instance_id: str,
        log: EventLog,
        timeout_seconds: float | None = None,
    ) -> tuple[ScriptedAdapter, Any]:
        attempt_root.mkdir(parents=True, exist_ok=False)
        spec_path = attempt_root / "spec.json"
        materialized_spec = dict(spec)
        materialized_spec["write_signal"] = str(
            (attempt_root / "writes-complete.signal").resolve()
        )
        atomic_json(spec_path, materialized_spec)
        (attempt_root / "task.txt").write_text(task_prompt(side), encoding="utf-8")
        adapter = ScriptedAdapter(self._script(mode))
        command = adapter.command_for_spec(spec_path)
        effective_timeout = self.gate_timeout_seconds if timeout_seconds is None else timeout_seconds
        log.emit(
            "launch",
            principal={"side": side.label, "instance_id": instance_id},
            subject=adapter.identity,
            detail={
                "timeout_seconds": effective_timeout,
                "production_timeout_seconds": PRODUCTION_TIMEOUT_SECONDS,
                "task_prompt_sha256": sha256_bytes(task_prompt(side).encode("utf-8")),
                "script_mode": mode,
                "scripted_write_release_control": bool(
                    spec.get("write_release_signal")
                ),
            },
        )
        running = start_process(
            command.argv,
            cwd=prepared.tree,
            env=command.env,
            stdin=command.stdin,
        )
        return adapter, running

    def _finish_attempt(
        self,
        *,
        prepared: PreparedTree,
        side: Side,
        mode: str,
        spec: Mapping[str, Any],
        attempt_root: Path,
        instance_id: str,
        subject_slot: int,
        timeout_retry: int,
        integration_retry: int,
        adapter: ScriptedAdapter,
        process: ProcessResult,
        log: EventLog,
        records: list[dict[str, Any]] | None = None,
        shared_pair: bool = False,
        validate_focal: bool = True,
    ) -> Attempt:
        after = snapshot_tree(prepared.tree)
        mechanical = records if records is not None else diff_snapshots(prepared.baseline, after)
        (attempt_root / "stdout.txt").write_bytes(process.stdout)
        (attempt_root / "stderr.txt").write_bytes(process.stderr)
        if not shared_pair:
            log.emit(
                "write-set",
                principal={"side": side.label, "instance_id": instance_id},
                subject=adapter.identity,
                paths=mechanical,
                detail={"basis": "post-test-patch-baseline-to-completion"},
            )
        protected_tests = set(self.all_test_paths)
        protected_tests.update(
            path for path in set(prepared.baseline) | set(after) if is_test_path(path)
        )
        integrity_ok, mismatches = test_integrity(
            prepared.baseline, after, tuple(sorted(protected_tests))
        )
        finished = process.finished
        excluded = not finished
        focal: dict[str, Any] | None = None
        if finished and validate_focal:
            focal = self.validator.focal(
                tree=prepared.tree,
                site=self.site,
                side=side,
                artifact_root=attempt_root,
                label=f"task-{side.label}",
            )
            log.emit(
                "validate",
                principal={"side": side.label, "instance_id": instance_id},
                subject=adapter.identity,
                detail={"kind": "per-side-focal", **_detail_validation(focal)},
            )
        correct = bool(
            finished
            and process.returncode == 0
            and integrity_ok
            and focal is not None
            and focal.get("green")
        )
        result_record = {
            "instance_id": instance_id,
            "side": side.label,
            "mode": mode,
            "returncode": process.returncode,
            "timed_out": process.timed_out,
            "launch_error": process.launch_error,
            "finished": finished,
            "excluded": excluded,
            "correct": correct,
            "test_integrity_ok": integrity_ok,
            "test_mismatches": mismatches,
            "logical_seconds": float(spec["logical_seconds"]),
            "actual_seconds": process.actual_seconds,
            "stdout_sha256": sha256_bytes(process.stdout),
            "stderr_sha256": sha256_bytes(process.stderr),
            "write_paths": [row["path"] for row in mechanical],
        }
        atomic_json(attempt_root / "result.json", result_record)
        log.emit(
            "complete",
            principal={"side": side.label, "instance_id": instance_id},
            subject=adapter.identity,
            detail={
                "finished": finished,
                "excluded": excluded,
                "returncode": process.returncode,
                "timed_out": process.timed_out,
                "correct": correct,
                "test_integrity_ok": integrity_ok,
                "test_mismatches": mismatches,
                "logical_seconds": float(spec["logical_seconds"]),
                "timeout_retry": timeout_retry,
                "slot_redraw_index": subject_slot,
                "integration_retry": integration_retry,
            },
        )
        return Attempt(
            side.label,
            instance_id,
            subject_slot,
            timeout_retry,
            integration_retry,
            prepared.tree,
            prepared.source_base,
            process,
            mechanical,
            finished,
            excluded,
            integrity_ok,
            mismatches,
            focal,
            correct,
            float(spec["logical_seconds"]),
            mode,
        )

    def _run_one(
        self,
        *,
        prepared: PreparedTree,
        side_label: str,
        mode: str,
        spec: Mapping[str, Any],
        attempt_root: Path,
        instance_id: str,
        log: EventLog,
        subject_slot: int = 0,
        timeout_retry: int = 0,
        integration_retry: int = 0,
        validate_focal: bool = True,
        timeout_seconds: float | None = None,
    ) -> Attempt:
        side = self.site.sides[side_label]
        adapter, running = self._start_fake(
            prepared=prepared,
            side=side,
            mode=mode,
            spec=spec,
            attempt_root=attempt_root,
            instance_id=instance_id,
            log=log,
            timeout_seconds=timeout_seconds,
        )
        process = finish_process(
            running,
            self.gate_timeout_seconds if timeout_seconds is None else timeout_seconds,
        )
        return self._finish_attempt(
            prepared=prepared,
            side=side,
            mode=mode,
            spec=spec,
            attempt_root=attempt_root,
            instance_id=instance_id,
            subject_slot=subject_slot,
            timeout_retry=timeout_retry,
            integration_retry=integration_retry,
            adapter=adapter,
            process=process,
            log=log,
            validate_focal=validate_focal,
        )

    def _run_pair(
        self,
        *,
        prepared: Mapping[str, PreparedTree],
        modes: Mapping[str, str],
        specs: Mapping[str, Mapping[str, Any]],
        attempts_root: Path,
        id_prefix: str,
        log: EventLog,
        shared: bool,
        poll: bool = False,
    ) -> tuple[dict[str, Attempt], str]:
        adapters: dict[str, ScriptedAdapter] = {}
        running: dict[str, Any] = {}
        roots: dict[str, Path] = {}
        for label in ("A", "B"):
            roots[label] = attempts_root / label
            launch_spec = dict(specs[label])
            if shared:
                launch_spec.update(
                    {
                        "started_signal": str(
                            (roots[label] / "ready-for-write.signal").resolve()
                        ),
                        "write_release_signal": str(
                            (roots[label] / "release-write.signal").resolve()
                        ),
                        "write_release_timeout_seconds": self.gate_timeout_seconds,
                    }
                )
            adapter, process = self._start_fake(
                prepared=prepared[label],
                side=self.site.sides[label],
                mode=modes[label],
                spec=launch_spec,
                attempt_root=roots[label],
                instance_id=f"{id_prefix}-{label}-i0",
                log=log,
            )
            adapters[label] = adapter
            running[label] = process
        if shared:
            ready = {
                label: roots[label] / "ready-for-write.signal" for label in ("A", "B")
            }
            deadline = time.monotonic() + self.gate_timeout_seconds
            while not all(path.is_file() for path in ready.values()):
                if time.monotonic() >= deadline:
                    missing = [label for label, path in ready.items() if not path.is_file()]
                    raise ShimError(
                        "shared fake subjects did not reach the write barrier: "
                        + ", ".join(missing)
                    )
                time.sleep(0.01)
            # Both subject processes are live. Release one deterministic fake
            # interleaving so repeated gate runs test identical machinery
            # despite host scheduling noise. This barrier is absent from the
            # production subject launcher and is disclosed in launch events.
            for label in ("A", "B"):
                (roots[label] / "release-write.signal").write_bytes(b"release\n")
                wrote = roots[label] / "writes-complete.signal"
                write_deadline = time.monotonic() + self.gate_timeout_seconds
                while not wrote.is_file():
                    if time.monotonic() >= write_deadline:
                        raise ShimError(
                            f"shared fake subject {label} did not finish its write phase"
                        )
                    time.sleep(0.01)
        if poll:
            # Gate-time scaling exercises the production 30-second poll path.
            # Each fake signals after its deterministic writes but before its
            # scripted completion delay. Waiting for both signals keeps the
            # sampled filesystem state reproducible without deriving any
            # attribution from subject output.
            signal_paths = {
                label: roots[label] / "writes-complete.signal" for label in ("A", "B")
            }
            signal_deadline = time.monotonic() + self.gate_timeout_seconds
            while not all(path.is_file() for path in signal_paths.values()):
                if time.monotonic() >= signal_deadline:
                    missing = [
                        label for label, path in signal_paths.items() if not path.is_file()
                    ]
                    raise ShimError(
                        "scripted polling did not observe write-complete signals for "
                        + ", ".join(missing)
                    )
                time.sleep(min(0.01, self.gate_poll_seconds))
            for poll_index in range(2):
                time.sleep(self.gate_poll_seconds)
                for label in ("A", "B"):
                    current = snapshot_tree(prepared[label].tree)
                    paths = diff_snapshots(prepared[label].baseline, current)
                    log.emit(
                        "poll",
                        principal={"side": label, "instance_id": f"{id_prefix}-{label}-i0"},
                        subject=adapters[label].identity,
                        paths=paths,
                        detail={
                            "poll_index": poll_index,
                            "gate_poll_seconds": self.gate_poll_seconds,
                            "production_poll_seconds": PRODUCTION_POLL_SECONDS,
                        },
                    )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                label: pool.submit(finish_process, running[label], self.gate_timeout_seconds)
                for label in ("A", "B")
            }
            processes = {label: futures[label].result() for label in ("A", "B")}
        later = max(
            ("A", "B"),
            key=lambda label: (processes[label].finished_monotonic, 1 if label == "B" else 0),
        )
        pair_records: list[dict[str, Any]] | None = None
        if shared:
            pair_records = diff_snapshots(prepared["A"].baseline, snapshot_tree(prepared["A"].tree))
            log.emit(
                "write-set",
                principal={
                    "scope": "shared-pair",
                    "instances": [f"{id_prefix}-A-i0", f"{id_prefix}-B-i0"],
                },
                subject={"cli": "scripted-fake", "version": "shim-fake-v1", "model": "deterministic"},
                paths=pair_records,
                detail={
                    "basis": "shared-post-test-patch-baseline-to-both-completions",
                    "per-principal_attribution": False,
                },
            )
        attempts: dict[str, Attempt] = {}
        for label in sorted(("A", "B"), key=lambda item: processes[item].finished_monotonic):
            attempts[label] = self._finish_attempt(
                prepared=prepared[label],
                side=self.site.sides[label],
                mode=modes[label],
                spec=specs[label],
                attempt_root=roots[label],
                instance_id=f"{id_prefix}-{label}-i0",
                subject_slot=0,
                timeout_retry=0,
                integration_retry=0,
                adapter=adapters[label],
                process=processes[label],
                log=log,
                records=pair_records if shared else None,
                shared_pair=shared,
            )
        return attempts, later

    def _commit_attempt(self, attempt: Attempt, *, label: str, message: str) -> str:
        excluded = set(self.all_test_paths)
        excluded.update(
            row["path"]
            for row in attempt.records
            if is_test_path(row["path"])
        )
        commit = self.scratch.commit_task_sources(
            task_tree=attempt.tree,
            source_base=attempt.source_base,
            changed_paths=source_paths_from_records(attempt.records),
            excluded_test_paths=excluded,
            name=self._name(label),
            message=message,
        )
        attempt.source_commit = commit
        return commit

    def _integration_validate(
        self,
        *,
        commit: str,
        root: Path,
        name: str,
        log: EventLog,
    ) -> dict[str, Any]:
        result = self.validator.integration(
            scratch=self.scratch,
            site=self.site,
            source_commit=commit,
            artifact_root=root,
            name_prefix=self._name(name),
        )
        log.emit(
            "validate",
            principal="harness",
            detail={
                "kind": "integration-two-oracle-views",
                "source_commit": commit,
                "side_A_green": bool(result["sides"].get("A", {}).get("green")),
                "side_B_green": bool(result["sides"].get("B", {}).get("green")),
                "test_files_byte_identical": result["test_files_byte_identical_on_final_tree"],
                "otherwise_buildable": result["otherwise_buildable"],
                "buildability_oracle": (
                    result.get("buildability", {}).get("oracle")
                    if isinstance(result.get("buildability"), dict)
                    else None
                ),
                "correct": result["correct"],
            },
        )
        return result

    def _merge_event(self, merge: MergeResult, log: EventLog, *, kind: str) -> None:
        log.emit(
            "merge",
            principal="harness",
            detail={
                "kind": kind,
                "mechanism": "git merge-tree --write-tree",
                "clean": merge.clean,
                "tree": merge.tree,
                "commit": merge.commit,
                "returncode": merge.returncode,
                "stdout_sha256": sha256_bytes(merge.stdout),
                "stderr_sha256": sha256_bytes(merge.stderr),
            },
        )

    def _base_metrics(self, draw_id: str, arm: int, repeat: int) -> dict[str, Any]:
        return {
            "draw_id": draw_id,
            "site": self.site.site_id,
            "arm": ARMS[arm],
            "repeat": repeat,
            "stratum": self.site.stratum,
            "attempts": [],
            "agent_seconds": 0.0,
            "wall_seconds": 0.0,
            "correct_attempts": 0,
            "correct_completions": 0,
            "integration_correct": False,
            "retry_compute_seconds": 0.0,
            "discarded_diff_seconds": 0.0,
            "wasted_compute_seconds": 0.0,
            "escalation_count": 0,
            "declaration_violations": 0,
            "log_only_attribution_rate": None,
            "buildability": None,
        }

    def _record_attempts(self, metrics: dict[str, Any], attempts: Sequence[Attempt]) -> None:
        for attempt in attempts:
            metrics["attempts"].append(
                {
                    "instance_id": attempt.instance_id,
                    "side": attempt.side,
                    "mode": attempt.mode,
                    "finished": attempt.finished,
                    "excluded": attempt.excluded,
                    "correct": attempt.correct,
                    "logical_seconds": attempt.logical_seconds,
                    "timeout_retry": attempt.timeout_retry,
                    "slot_redraw_index": attempt.subject_slot,
                    "integration_retry": attempt.integration_retry,
                }
            )
            metrics["agent_seconds"] += attempt.logical_seconds
            if attempt.correct:
                metrics["correct_attempts"] += 1

    def _benign_path(self, side_label: str) -> str:
        side = self.site.sides[side_label]
        other = set(self.site.sides["B" if side_label == "A" else "A"].source_paths)
        candidates = [
            path for path in side.source_paths if path not in other and path.endswith(".py")
        ]
        if not candidates:
            candidates = [path for path in side.source_paths if path not in other]
        if not candidates:
            raise ShimError(f"no side-unique benign path for {self.site.site_id} {side_label}")
        return sorted(candidates)[0]

    def _mode_spec(
        self,
        *,
        side_label: str,
        mode: str,
        logical_seconds: float,
        declared_paths: Sequence[str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        combined = dict(extra or {})
        if mode == "benign":
            combined.update(
                {
                    "benign_path": self._benign_path(side_label),
                    "benign_marker": f"# ARMS_SHIM_BENIGN_{side_label}",
                }
            )
        return self._fake_spec(
            side=self.site.sides[side_label],
            mode=mode,
            logical_seconds=logical_seconds,
            declared_paths=declared_paths,
            extra=combined,
        )

    def _declaration(
        self,
        *,
        side_label: str,
        prepared: PreparedTree,
        declared_paths: Sequence[str],
        root: Path,
        instance_id: str,
        log: EventLog,
    ) -> tuple[tuple[str, ...], str | None, float]:
        root.mkdir(parents=True, exist_ok=False)
        spec = self._mode_spec(
            side_label=side_label,
            mode="collision",
            logical_seconds=0.01,
            declared_paths=declared_paths,
        )
        spec_path = root / "spec.json"
        atomic_json(spec_path, spec)
        adapter = ScriptedAdapter(self._script("collision"))
        command = adapter.command_for_spec(spec_path, declare=True)
        log.emit(
            "launch",
            principal={"side": side_label, "instance_id": instance_id},
            subject=adapter.identity,
            detail={"kind": "preliminary-file-declaration", "short_call": True},
        )
        running = start_process(command.argv, cwd=prepared.tree, env=command.env)
        process = finish_process(running, self.gate_timeout_seconds)
        (root / "stdout.txt").write_bytes(process.stdout)
        (root / "stderr.txt").write_bytes(process.stderr)
        parsed, error = parse_declaration(process.stdout)
        if not process.finished or process.returncode != 0:
            error = error or "declaration process did not finish successfully"
        log.emit(
            "declare",
            principal={"side": side_label, "instance_id": instance_id},
            subject=adapter.identity,
            detail={
                "declared_paths": list(parsed),
                "valid": error is None,
                "error": error,
                "response_sha256": sha256_bytes(process.stdout),
            },
        )
        return parsed, error, 0.01

    def _serial_pair(
        self,
        *,
        draw_id: str,
        root: Path,
        log: EventLog,
        a_mode: str = "collision",
        b_mode: str = "answer",
        stall_b: bool = False,
    ) -> tuple[list[Attempt], str, float]:
        attempts: list[Attempt] = []
        a_prepared = self._prepare(
            source_base=self.site.base_commit, test_sides=("A",), label="seqA", log=log
        )
        a_spec = self._mode_spec(side_label="A", mode=a_mode, logical_seconds=0.06)
        a_attempt = self._run_one(
            prepared=a_prepared,
            side_label="A",
            mode=a_mode,
            spec=a_spec,
            attempt_root=root / "attempts" / "A",
            instance_id=f"{draw_id}-A-i0",
            log=log,
        )
        attempts.append(a_attempt)
        a_commit = self._commit_attempt(
            a_attempt, label="seqAsrc", message=f"{draw_id} A source result"
        )
        if not stall_b:
            b_prepared = self._prepare(
                source_base=a_commit, test_sides=("B",), label="seqB", log=log
            )
            b_spec = self._mode_spec(side_label="B", mode=b_mode, logical_seconds=0.10)
            b_attempt = self._run_one(
                prepared=b_prepared,
                side_label="B",
                mode=b_mode,
                spec=b_spec,
                attempt_root=root / "attempts" / "B",
                instance_id=f"{draw_id}-B-i0",
                log=log,
            )
            attempts.append(b_attempt)
            final = self._commit_attempt(
                b_attempt, label="seqBsrc", message=f"{draw_id} B source result"
            )
            return attempts, final, 0.16

        # Fairness calibration: a no-write staller times out, retries once in a
        # fresh tree, is excluded, and the slot is redrawn fresh.
        for timeout_retry in range(MAX_TIMEOUT_RETRIES + 1):
            prepared = self._prepare(
                source_base=a_commit,
                test_sides=("B",),
                label=f"stallB{timeout_retry}",
                log=log,
            )
            spec = self._mode_spec(
                side_label="B",
                mode="staller",
                logical_seconds=GATE_STALL_TIMEOUT_SECONDS,
                extra={"stall_seconds": 60.0},
            )
            if timeout_retry:
                log.emit(
                    "retry",
                    principal={"side": "B", "instance_id": f"{draw_id}-B-i{timeout_retry}"},
                    detail={"kind": "timeout-retry", "retry": timeout_retry},
                )
            stalled = self._run_one(
                prepared=prepared,
                side_label="B",
                mode="staller",
                spec=spec,
                attempt_root=root / "attempts" / f"B-timeout-{timeout_retry}",
                instance_id=f"{draw_id}-B-i{timeout_retry}",
                log=log,
                timeout_retry=timeout_retry,
                timeout_seconds=GATE_STALL_TIMEOUT_SECONDS,
            )
            attempts.append(stalled)
            if stalled.finished:
                raise ShimError("scripted staller unexpectedly finished")
        log.emit(
            "retry",
            principal={"side": "B", "instance_id": f"{draw_id}-B-redraw-i0"},
            detail={
                "kind": "slot-redraw",
                "excluded_subject_instances": [f"{draw_id}-B-i0", f"{draw_id}-B-i1"],
                "fresh_worktree": True,
            },
        )
        redraw_prepared = self._prepare(
            source_base=a_commit, test_sides=("B",), label="redrawB", log=log
        )
        redraw_spec = self._mode_spec(side_label="B", mode="answer", logical_seconds=0.10)
        redraw = self._run_one(
            prepared=redraw_prepared,
            side_label="B",
            mode="answer",
            spec=redraw_spec,
            attempt_root=root / "attempts" / "B-redraw",
            instance_id=f"{draw_id}-B-redraw-i0",
            subject_slot=1,
            log=log,
        )
        attempts.append(redraw)
        final = self._commit_attempt(
            redraw, label="redrawBsrc", message=f"{draw_id} B redrawn source result"
        )
        wall = 0.06 + 2 * GATE_STALL_TIMEOUT_SECONDS + 0.10
        return attempts, final, wall

    def _arm_sequential(
        self, draw_id: str, repeat: int, root: Path, log: EventLog, metrics: dict[str, Any]
    ) -> None:
        a_mode = "cheater" if self.site.stratum == "byte-intersecting" and repeat == 2 else "collision"
        # The gate always has one byte-intersecting site and one site from a
        # different stratum.  Put the timeout/redraw calibration on that other
        # site rather than coupling it to the (currently empty) strict
        # same-file-disjoint population.
        stall_b = self.site.stratum != "byte-intersecting" and repeat == 2
        attempts, final, wall = self._serial_pair(
            draw_id=draw_id,
            root=root,
            log=log,
            a_mode=a_mode,
            b_mode="answer",
            stall_b=stall_b,
        )
        self._record_attempts(metrics, attempts)
        metrics["wall_seconds"] = wall
        integration = self._integration_validate(
            commit=final, root=root, name="seqint", log=log
        )
        metrics["integration_correct"] = integration["correct"]
        metrics["timeout_excluded_instances"] = [
            attempt.instance_id for attempt in attempts if attempt.excluded
        ]
        metrics["slot_redraw_instances"] = [
            attempt.instance_id for attempt in attempts if attempt.subject_slot > 0
        ]

    def _arm_shared(
        self, draw_id: str, repeat: int, root: Path, log: EventLog, metrics: dict[str, Any]
    ) -> None:
        prepared_one = self._prepare(
            source_base=self.site.base_commit,
            test_sides=("A", "B"),
            label="shared",
            log=log,
        )
        prepared = {"A": prepared_one, "B": prepared_one}
        benign = self.site.stratum in {"same-file-disjoint", "boundary-only"} and repeat == 1
        modes = {"A": "benign" if benign else "collision", "B": "benign" if benign else "collision"}
        specs = {
            label: self._mode_spec(
                side_label=label,
                mode=modes[label],
                logical_seconds=0.06 if label == "A" else 0.10,
            )
            for label in ("A", "B")
        }
        attempts, _later = self._run_pair(
            prepared=prepared,
            modes=modes,
            specs=specs,
            attempts_root=root / "attempts",
            id_prefix=draw_id,
            log=log,
            shared=True,
        )
        self._record_attempts(metrics, [attempts["A"], attempts["B"]])
        metrics["wall_seconds"] = max(specs["A"]["logical_seconds"], specs["B"]["logical_seconds"])
        combined = self._commit_attempt(
            attempts["A"], label="sharedsrc", message=f"{draw_id} shared source result"
        )
        log.emit(
            "merge",
            principal="harness",
            detail={"kind": "shared-tree-no-merge", "source_commit": combined},
        )
        integration = self._integration_validate(
            commit=combined, root=root, name="sharedint", log=log
        )
        metrics["integration_correct"] = integration["correct"]
        metrics["shared_write_attribution"] = "pair-only"

    def _arm_optimistic(
        self, draw_id: str, repeat: int, root: Path, log: EventLog, metrics: dict[str, Any]
    ) -> None:
        prepared = {
            label: self._prepare(
                source_base=self.site.base_commit,
                test_sides=(label,),
                label=f"opt{label}",
                log=log,
            )
            for label in ("A", "B")
        }
        modes = {"A": "collision", "B": "collision"}
        specs = {
            "A": self._mode_spec(side_label="A", mode="collision", logical_seconds=0.06),
            "B": self._mode_spec(side_label="B", mode="collision", logical_seconds=0.10),
        }
        initial, later = self._run_pair(
            prepared=prepared,
            modes=modes,
            specs=specs,
            attempts_root=root / "attempts" / "initial",
            id_prefix=f"{draw_id}-initial",
            log=log,
            shared=False,
        )
        initial_attempts = [initial["A"], initial["B"]]
        self._record_attempts(metrics, initial_attempts)
        commits = {
            label: self._commit_attempt(
                initial[label],
                label=f"opt{label}src",
                message=f"{draw_id} initial {label} source result",
            )
            for label in ("A", "B")
        }
        merge = self.scratch.merge_tree(
            commits["A"], commits["B"], message=f"{draw_id} optimistic merge"
        )
        self._merge_event(merge, log, kind="optimistic-initial")
        current_validation: dict[str, Any] | None = None
        if merge.clean and merge.commit:
            current_validation = self._integration_validate(
                commit=merge.commit, root=root, name="optinitial", log=log
            )
        recovery_needed = not merge.clean or not current_validation or not current_validation["correct"]
        metrics["later_finisher"] = later
        metrics["later_finisher_instance"] = initial[later].instance_id
        metrics["wall_seconds"] = max(specs["A"]["logical_seconds"], specs["B"]["logical_seconds"])
        final_commit = merge.commit if merge.clean else None
        discarded_ids: list[str] = []
        retry_ids: list[str] = []
        wasted_ids: set[str] = set()
        if recovery_needed:
            winner = "B" if later == "A" else "A"
            winner_commit = commits[winner]
            discarded_ids.append(initial[later].instance_id)
            metrics["discarded_diff_seconds"] += initial[later].logical_seconds
            wasted_ids.add(initial[later].instance_id)
            for retry_index in range(1, MAX_OPTIMISTIC_RETRIES + 1):
                special_wrong = (
                    self.site.stratum == "byte-intersecting" and repeat == 1 and retry_index == 1
                )
                mode = "benign" if special_wrong else "answer"
                instance_id = f"{draw_id}-{later}-integration-retry-{retry_index}"
                log.emit(
                    "retry",
                    principal={"side": later, "instance_id": instance_id},
                    detail={
                        "kind": "optimistic-loser-retry",
                        "retry_index": retry_index,
                        "max_retries": MAX_OPTIMISTIC_RETRIES,
                        "loser_rule": "later-finisher",
                        "fresh_from_winner": True,
                    },
                )
                retry_prepared = self._prepare(
                    source_base=winner_commit,
                    test_sides=(later,),
                    label=f"optretry{retry_index}",
                    log=log,
                )
                logical = 0.04 + retry_index * 0.01
                retry_spec = self._mode_spec(
                    side_label=later, mode=mode, logical_seconds=logical
                )
                retry_attempt = self._run_one(
                    prepared=retry_prepared,
                    side_label=later,
                    mode=mode,
                    spec=retry_spec,
                    attempt_root=root / "attempts" / f"retry-{retry_index}",
                    instance_id=instance_id,
                    log=log,
                    integration_retry=retry_index,
                )
                self._record_attempts(metrics, [retry_attempt])
                retry_ids.append(instance_id)
                wasted_ids.add(instance_id)
                metrics["retry_compute_seconds"] += logical
                metrics["wall_seconds"] += logical
                candidate = self._commit_attempt(
                    retry_attempt,
                    label=f"optretrysrc{retry_index}",
                    message=f"{draw_id} loser retry {retry_index} source result",
                )
                validation = self._integration_validate(
                    commit=candidate,
                    root=root,
                    name=f"optretryint{retry_index}",
                    log=log,
                )
                final_commit = candidate
                current_validation = validation
                if validation["correct"]:
                    break
                discarded_ids.append(instance_id)
                metrics["discarded_diff_seconds"] += logical
        metrics["integration_correct"] = bool(current_validation and current_validation["correct"])
        metrics["retry_instance_ids"] = retry_ids
        metrics["discarded_instance_ids"] = discarded_ids
        metrics["wasted_instance_ids"] = sorted(wasted_ids)
        duration_by_id = {
            row["instance_id"]: row["logical_seconds"] for row in metrics["attempts"]
        }
        metrics["wasted_compute_seconds"] = sum(duration_by_id[item] for item in wasted_ids)

    def _declaration_plan(self, side_label: str, repeat: int) -> tuple[str, ...]:
        if self.site.stratum in {"same-file-disjoint", "boundary-only"}:
            if repeat == 1:
                return (self._benign_path(side_label),)
            # The second file-lock repeat deliberately under-declares to test
            # non-blocking violation accounting.
            if repeat == 2:
                return (self._benign_path(side_label),)
        return self.site.sides[side_label].source_paths

    def _declarations(
        self,
        *,
        draw_id: str,
        repeat: int,
        root: Path,
        log: EventLog,
        metrics: dict[str, Any],
    ) -> tuple[dict[str, tuple[str, ...]], bool]:
        declared: dict[str, tuple[str, ...]] = {}
        invalid = False
        for label in ("A", "B"):
            preview = self._prepare(
                source_base=self.site.base_commit,
                test_sides=(label,),
                label=f"decl{label}",
                log=log,
            )
            paths, error, seconds = self._declaration(
                side_label=label,
                prepared=preview,
                declared_paths=self._declaration_plan(label, repeat),
                root=root / "declarations" / label,
                instance_id=f"{draw_id}-{label}-declare",
                log=log,
            )
            declared[label] = paths
            invalid = invalid or error is not None
            metrics["agent_seconds"] += seconds
        return declared, invalid

    def _declaration_accuracy(
        self,
        *,
        declared: Mapping[str, Sequence[str]],
        attempts: Mapping[str, Attempt],
        shared: bool,
        metrics: dict[str, Any],
        log: EventLog,
    ) -> None:
        if shared:
            actual_paths = {row["path"] for row in attempts["A"].records}
            declared_union = set(declared["A"]) | set(declared["B"])
            misses = sorted(actual_paths - declared_union)
            metrics["declaration_violations"] = len(misses)
            metrics["declaration_accuracy"] = {
                "scope": "shared-pair-union",
                "declared_union": sorted(declared_union),
                "actual_union": sorted(actual_paths),
                "missed_paths": misses,
                "per_side_attribution": False,
            }
            log.emit(
                "validate",
                principal="harness",
                detail={"kind": "declaration-accuracy", **metrics["declaration_accuracy"]},
            )
            return
        records: dict[str, Any] = {}
        total = 0
        for label in ("A", "B"):
            actual_paths = {row["path"] for row in attempts[label].records}
            declared_set = set(declared[label])
            misses = sorted(actual_paths - declared_set)
            total += len(misses)
            records[label] = {
                "declared": sorted(declared_set),
                "actual": sorted(actual_paths),
                "missed_paths": misses,
                "precision": (
                    len(actual_paths & declared_set) / len(declared_set) if declared_set else None
                ),
                "recall": (
                    len(actual_paths & declared_set) / len(actual_paths) if actual_paths else 1.0
                ),
            }
        metrics["declaration_violations"] = total
        metrics["declaration_accuracy"] = {"scope": "per-side", "sides": records}
        log.emit(
            "validate",
            principal="harness",
            detail={"kind": "declaration-accuracy", **metrics["declaration_accuracy"]},
        )

    def _arm_locks(
        self, draw_id: str, repeat: int, root: Path, log: EventLog, metrics: dict[str, Any]
    ) -> None:
        declared, invalid = self._declarations(
            draw_id=draw_id, repeat=repeat, root=root, log=log, metrics=metrics
        )
        overlap = bool(set(declared["A"]) & set(declared["B"]))
        serialize = invalid or overlap
        metrics["schedule"] = "serialize" if serialize else "parallel-shared"
        log.emit(
            "merge",
            principal="harness",
            detail={
                "kind": "file-lock-schedule",
                "serialize": serialize,
                "invalid_declaration": invalid,
                "declared_overlap": sorted(set(declared["A"]) & set(declared["B"])),
            },
        )
        if serialize:
            attempts_list, final, wall = self._serial_pair(
                draw_id=draw_id, root=root, log=log, a_mode="collision", b_mode="answer"
            )
            attempts = {"A": attempts_list[0], "B": attempts_list[-1]}
            self._record_attempts(metrics, attempts_list)
            metrics["wall_seconds"] = wall + 0.02
            integration = self._integration_validate(
                commit=final, root=root, name="lockint", log=log
            )
            metrics["integration_correct"] = integration["correct"]
            self._declaration_accuracy(
                declared=declared, attempts=attempts, shared=False, metrics=metrics, log=log
            )
        else:
            prepared_one = self._prepare(
                source_base=self.site.base_commit,
                test_sides=("A", "B"),
                label="lockshared",
                log=log,
            )
            benign = repeat == 1
            modes = {label: "benign" if benign else "collision" for label in ("A", "B")}
            specs = {
                label: self._mode_spec(
                    side_label=label,
                    mode=modes[label],
                    logical_seconds=0.06 if label == "A" else 0.10,
                    declared_paths=declared[label],
                )
                for label in ("A", "B")
            }
            attempts, _ = self._run_pair(
                prepared={"A": prepared_one, "B": prepared_one},
                modes=modes,
                specs=specs,
                attempts_root=root / "attempts",
                id_prefix=draw_id,
                log=log,
                shared=True,
            )
            self._record_attempts(metrics, [attempts["A"], attempts["B"]])
            metrics["wall_seconds"] = 0.12
            combined = self._commit_attempt(
                attempts["A"], label="locksrc", message=f"{draw_id} lock shared source"
            )
            integration = self._integration_validate(
                commit=combined, root=root, name="lockint", log=log
            )
            metrics["integration_correct"] = integration["correct"]
            self._declaration_accuracy(
                declared=declared, attempts=attempts, shared=True, metrics=metrics, log=log
            )
            if metrics["declaration_violations"]:
                metrics["counterfactual_refusal_seconds"] = sum(
                    attempt.logical_seconds for attempt in attempts.values()
                )

    def _arm_coordinator(
        self, draw_id: str, repeat: int, root: Path, log: EventLog, metrics: dict[str, Any]
    ) -> None:
        declared, invalid = self._declarations(
            draw_id=draw_id, repeat=repeat, root=root, log=log, metrics=metrics
        )
        tracked = self.scratch.tracked_paths(self.site.base_commit)
        predicted: dict[str, set[str]] = {}
        named: dict[str, tuple[str, ...]] = {}
        for label in ("A", "B"):
            named[label] = named_intent_paths(self.site.sides[label], tracked)
            predicted[label] = set(declared[label]) | set(named[label])
        overlap = predicted["A"] & predicted["B"]
        serialize = invalid or bool(overlap)
        metrics["schedule"] = "serialize" if serialize else "parallel-isolated"
        log.emit(
            "merge",
            principal="harness",
            detail={
                "kind": "coordinator-dispatch-schedule",
                "serialize": serialize,
                "declared_plus_intent_overlap": sorted(overlap),
                "intent_named_paths": {key: list(value) for key, value in named.items()},
                "dispatch_only": True,
            },
        )
        if serialize:
            attempts_list, final, wall = self._serial_pair(
                draw_id=draw_id, root=root, log=log, a_mode="collision", b_mode="answer"
            )
            attempts = {"A": attempts_list[0], "B": attempts_list[-1]}
            self._record_attempts(metrics, attempts_list)
            metrics["wall_seconds"] = wall + 0.02
            integration = self._integration_validate(
                commit=final, root=root, name="coordint", log=log
            )
            metrics["integration_correct"] = integration["correct"]
            self._declaration_accuracy(
                declared=declared, attempts=attempts, shared=False, metrics=metrics, log=log
            )
            return
        prepared = {
            label: self._prepare(
                source_base=self.site.base_commit,
                test_sides=(label,),
                label=f"coord{label}",
                log=log,
            )
            for label in ("A", "B")
        }
        modes = {"A": "collision", "B": "collision"}
        specs = {
            "A": self._mode_spec(side_label="A", mode="collision", logical_seconds=0.06),
            "B": self._mode_spec(side_label="B", mode="collision", logical_seconds=0.10),
        }
        attempts, _ = self._run_pair(
            prepared=prepared,
            modes=modes,
            specs=specs,
            attempts_root=root / "attempts",
            id_prefix=draw_id,
            log=log,
            shared=False,
        )
        self._record_attempts(metrics, [attempts["A"], attempts["B"]])
        metrics["wall_seconds"] = 0.12
        commits = {
            label: self._commit_attempt(
                attempts[label], label=f"coord{label}src", message=f"{draw_id} coord {label}"
            )
            for label in ("A", "B")
        }
        merge = self.scratch.merge_tree(
            commits["A"], commits["B"], message=f"{draw_id} coordinator merge"
        )
        self._merge_event(merge, log, kind="coordinator-zero-retry")
        if merge.clean and merge.commit:
            integration = self._integration_validate(
                commit=merge.commit, root=root, name="coordint", log=log
            )
            metrics["integration_correct"] = integration["correct"]
        metrics["integration_retries"] = 0
        self._declaration_accuracy(
            declared=declared, attempts=attempts, shared=False, metrics=metrics, log=log
        )

    def _contested_attribution(self, attempts: Mapping[str, Attempt]) -> dict[str, Any]:
        contested: list[dict[str, Any]] = []
        for left in attempts["A"].records:
            if left["path"] in self.all_test_paths:
                continue
            for right in attempts["B"].records:
                if right["path"] != left["path"] or right["path"] in self.all_test_paths:
                    continue
                for left_region in left["regions"]:
                    for right_region in right["regions"]:
                        if regions_overlap(left_region, right_region):
                            contested.append(
                                {
                                    "path": left["path"],
                                    "A_region": left_region,
                                    "B_region": right_region,
                                    "A_instance": attempts["A"].instance_id,
                                    "B_instance": attempts["B"].instance_id,
                                }
                            )
        attributed = sum(
            1 for item in contested if item.get("A_instance") and item.get("B_instance")
        )
        return {
            "contested_region_pairs": len(contested),
            "attributed_region_pairs": attributed,
            "rate": attributed / len(contested) if contested else None,
            "records": contested,
        }

    def _arm6_alternation(
        self, draw_id: str, root: Path, log: EventLog, metrics: dict[str, Any]
    ) -> None:
        candidate_paths = [
            path for path in self.site.strict_overlap_paths if path not in self.all_test_paths
        ]
        if not candidate_paths:
            candidate_paths = list(self.site.sides["A"].source_paths)
        path = sorted(candidate_paths)[0]
        sides = ["A", "B", "A", "B"]
        # Adjacent tokens intentionally share no bytes. That keeps the
        # mechanically refined diff region at the same [0, 4) interval across
        # every evolving retry base, which is the condition this escalation
        # fixture is meant to exercise.
        tokens = ["1111", "2222", "3333", "4444"]
        attempts: list[Attempt] = []
        observed_sides: list[str] = []
        switches = 0
        region_key: str | None = None
        current_base = self.site.base_commit
        last_validation: dict[str, Any] | None = None
        for index, (side_label, token) in enumerate(zip(sides, tokens), start=1):
            prepared = self._prepare(
                source_base=current_base,
                test_sides=(side_label,),
                label=f"alt{index}",
                log=log,
            )
            target = tree_path(prepared.tree, path)
            if len(target.read_bytes()) < 4:
                raise ShimError(f"alternation path is too short: {path}")
            spec = self._mode_spec(
                side_label=side_label,
                mode="alternator",
                logical_seconds=0.03,
                extra={
                    "alternation_path": path,
                    "alternation_start": 0,
                    "alternation_end": 4,
                    "alternation_token": token,
                },
            )
            instance_id = f"{draw_id}-{side_label}-alt-{index}"
            if index > 1:
                log.emit(
                    "retry",
                    principal={"side": side_label, "instance_id": instance_id},
                    detail={"kind": "full-system-region-retry", "retry_index": index - 1},
                )
            attempt = self._run_one(
                prepared=prepared,
                side_label=side_label,
                mode="alternator",
                spec=spec,
                attempt_root=root / "attempts" / f"alternation-{index}",
                instance_id=instance_id,
                log=log,
                integration_retry=index - 1,
                validate_focal=False,
            )
            attempts.append(attempt)
            candidate = self._commit_attempt(
                attempt,
                label=f"altresult{index}",
                message=f"{draw_id} evolving alternation {index} {side_label}",
            )
            log.emit(
                "merge",
                principal="harness",
                detail={
                    "kind": "full-system-evolving-retry-base",
                    "retry_index": index - 1,
                    "prior_source_commit": current_base,
                    "candidate_source_commit": candidate,
                    "answer_key_bytes_used": False,
                },
            )
            last_validation = self._integration_validate(
                commit=candidate,
                root=root,
                name=f"altint{index}",
                log=log,
            )
            current_base = candidate
            path_record = next(row for row in attempt.records if row["path"] == path)
            region = path_record["regions"][0]
            current_key = f"{path}:{region['old_start']}:{region['old_end']}"
            if region_key is None:
                region_key = current_key
            if current_key != region_key:
                raise ShimError(f"alternation region drifted: {current_key} != {region_key}")
            if observed_sides and observed_sides[-1] != side_label:
                switches += 1
            observed_sides.append(side_label)
            if switches >= ESCALATION_SIDE_SWITCHES:
                log.emit(
                    "escalate",
                    principal="harness",
                    paths=[path_record],
                    detail={
                        "kind": "N=3-region-alternation",
                        "region_key": region_key,
                        "side_sequence": observed_sides,
                        "side_switches": switches,
                        "budget": ESCALATION_SIDE_SWITCHES,
                    },
                )
                break
        self._record_attempts(metrics, attempts)
        metrics["wall_seconds"] = sum(attempt.logical_seconds for attempt in attempts)
        metrics["escalation_count"] = 1 if switches >= ESCALATION_SIDE_SWITCHES else 0
        metrics["escalation"] = {
            "region_key": region_key,
            "side_sequence": observed_sides,
            "side_switches": switches,
            "budget": ESCALATION_SIDE_SWITCHES,
        }
        metrics["integration_correct"] = bool(
            last_validation and last_validation.get("correct")
        )
        attributed_switches = sum(
            1
            for left, right in zip(attempts, attempts[1:])
            if left.side != right.side and left.instance_id and right.instance_id
        )
        metrics["contested_region_pairs"] = switches
        metrics["log_only_attribution_rate"] = (
            attributed_switches / switches if switches else None
        )
        metrics["retry_compute_seconds"] = sum(
            attempt.logical_seconds for attempt in attempts[1:]
        )
        metrics["discarded_diff_seconds"] = sum(
            attempt.logical_seconds for attempt in attempts
        )
        metrics["wasted_compute_seconds"] = metrics["discarded_diff_seconds"]

    def _arm_full(
        self, draw_id: str, repeat: int, root: Path, log: EventLog, metrics: dict[str, Any]
    ) -> None:
        contradictory = self.site.joint_status == "MUTUALLY_UNSATISFIABLE"
        log.emit(
            "validate",
            principal="harness",
            detail={
                "kind": "dispatch-contract-screen",
                "joint_status": self.site.joint_status,
                "contradictory": contradictory,
                "before_subject_launch": True,
            },
        )
        if contradictory:
            log.emit(
                "escalate",
                principal="harness",
                detail={"kind": "contract-issuance-contradiction", "subject_launches": 0},
            )
            metrics["escalation_count"] = 1
            metrics["contract_screened"] = True
            return
        if self.site.stratum == "byte-intersecting" and repeat == 2:
            self._arm6_alternation(draw_id, root, log, metrics)
            return
        prepared = {
            label: self._prepare(
                source_base=self.site.base_commit,
                test_sides=(label,),
                label=f"full{label}",
                log=log,
            )
            for label in ("A", "B")
        }
        modes = {"A": "collision", "B": "collision"}
        specs = {
            "A": self._mode_spec(side_label="A", mode="collision", logical_seconds=0.09),
            "B": self._mode_spec(side_label="B", mode="collision", logical_seconds=0.12),
        }
        attempts, _ = self._run_pair(
            prepared=prepared,
            modes=modes,
            specs=specs,
            attempts_root=root / "attempts",
            id_prefix=draw_id,
            log=log,
            shared=False,
            poll=True,
        )
        self._record_attempts(metrics, [attempts["A"], attempts["B"]])
        metrics["wall_seconds"] = 0.12
        commits = {
            label: self._commit_attempt(
                attempts[label], label=f"full{label}src", message=f"{draw_id} full {label}"
            )
            for label in ("A", "B")
        }
        attribution = self._contested_attribution(attempts)
        metrics["log_only_attribution_rate"] = attribution["rate"]
        metrics["contested_region_pairs"] = attribution["contested_region_pairs"]
        log.emit(
            "validate",
            principal="harness",
            detail={
                "kind": "log-only-contested-write-attribution",
                "contested_region_pairs": attribution["contested_region_pairs"],
                "attributed_region_pairs": attribution["attributed_region_pairs"],
                "rate": attribution["rate"],
                "agent_text_consulted": False,
            },
        )
        harvested, choices = self.scratch.harvest(
            base=self.site.base_commit,
            left=commits["A"],
            right=commits["B"],
            name=self._name("harvest"),
            message=f"{draw_id} harvested produced blobs",
        )
        log.emit(
            "merge",
            principal="harness",
            paths=choices,
            detail={
                "kind": "full-system-harvest",
                "answer_key_role": "selection-only",
                "oracle_bytes_synthesized": False,
                "source_commit": harvested,
            },
        )
        integration = self._integration_validate(
            commit=harvested, root=root, name="fullint", log=log
        )
        metrics["integration_correct"] = integration["correct"]
        metrics["harvest_choices"] = choices

    def run_draw(self, *, arm: int, repeat: int, root: Path, run_id: str) -> dict[str, Any]:
        if arm not in ARMS:
            raise ShimError(f"unknown arm: {arm}")
        draw_id = f"{self.site.repo_slug}-{self.site.merge[:8]}-a{arm}-r{repeat}"
        root.mkdir(parents=True, exist_ok=False)
        metrics = self._base_metrics(draw_id, arm, repeat)
        with EventLog(
            root / "events.jsonl",
            run_id=run_id,
            draw_id=draw_id,
            site=self.site.event_identity(),
            arm=ARMS[arm],
            stratum=self.site.stratum,
            clock=LogicalClock(),
        ) as log:
            if arm == 1:
                self._arm_sequential(draw_id, repeat, root, log, metrics)
            elif arm == 2:
                self._arm_shared(draw_id, repeat, root, log, metrics)
            elif arm == 3:
                self._arm_optimistic(draw_id, repeat, root, log, metrics)
            elif arm == 4:
                self._arm_locks(draw_id, repeat, root, log, metrics)
            elif arm == 5:
                self._arm_coordinator(draw_id, repeat, root, log, metrics)
            elif arm == 6:
                self._arm_full(draw_id, repeat, root, log, metrics)
        # A draw contains exactly two agent tasks. Integration retries replace
        # a side's earlier completion; they do not create additional task
        # completions. Retain the attempt-level count separately, and compute
        # the primary completion numerator from each side's terminal accepted
        # instance (excluded non-finishers naturally count as incorrect).
        terminal_by_side: dict[str, Mapping[str, Any]] = {}
        for attempt_record in metrics["attempts"]:
            terminal_by_side[str(attempt_record["side"])] = attempt_record
        accepted = {
            side: record["instance_id"] for side, record in sorted(terminal_by_side.items())
        }
        metrics["accepted_completion_instance_ids"] = accepted
        metrics["correct_completions"] = sum(
            bool(record.get("correct")) for record in terminal_by_side.values()
        )
        metrics["agent_minutes"] = metrics["agent_seconds"] / 60.0
        metrics["correct_completions_per_agent_minute"] = (
            metrics["correct_completions"] / metrics["agent_minutes"]
            if metrics["agent_minutes"]
            else None
        )
        metrics["correct_completions_per_wall_hour"] = (
            metrics["correct_completions"] / (metrics["wall_seconds"] / 3600.0)
            if metrics["wall_seconds"]
            else None
        )
        atomic_json(root / "metrics.json", metrics)
        return metrics


def contract_screen_only(
    *, site: Site, root: Path, run_id: str = "shim-contract-screen"
) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=False)
    arm = ARMS[6]
    draw_id = f"{site.repo_slug}-{site.merge[:8]}-contract-screen"
    with EventLog(
        root / "events.jsonl",
        run_id=run_id,
        draw_id=draw_id,
        site=site.event_identity(),
        arm=arm,
        stratum=site.stratum,
        clock=LogicalClock(),
    ) as log:
        contradiction = site.joint_status == "MUTUALLY_UNSATISFIABLE"
        log.emit(
            "validate",
            principal="harness",
            detail={
                "kind": "dispatch-contract-screen",
                "joint_status": site.joint_status,
                "contradictory": contradiction,
                "before_subject_launch": True,
            },
        )
        if contradiction:
            log.emit(
                "escalate",
                principal="harness",
                detail={"kind": "contract-issuance-contradiction", "subject_launches": 0},
            )
    record = {
        "site": site.site_id,
        "joint_status": site.joint_status,
        "contradiction_surfaced": contradiction,
        "subject_launches": 0,
    }
    atomic_json(root / "result.json", record)
    return record
