"""Production-capable orchestration for the six preregistered ARMS arms.

This module deliberately contains no CLI selection at module import time and
never launches a process on its own.  A :class:`SubjectRunner` is injected for
each side/subject slot.  ``ProductionLauncherRunner`` is the adapter for the
instruction-bare launch primitive in :mod:`.production`; tests can inject an
in-process runner and exercise every scheduling branch without a model call.

The class subclasses :class:`.harness.Harness` so test-overlay preparation,
mechanical diffs, source-only commits, merge-tree, validators, harvesting,
contested-region attribution, and metric shapes remain the gated algorithms.
Only the gate's scripted-subject policy is replaced here.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .adapters import SubjectCommand, declaration_prompt, parse_declaration
from .harness import (
    ARMS,
    ESCALATION_SIDE_SWITCHES,
    MAX_OPTIMISTIC_RETRIES,
    MAX_TIMEOUT_RETRIES,
    Attempt,
    Harness,
    PreparedTree,
    _detail_validation,
)
from .production import ProductionSubjectLauncher
from .schema import Side, named_intent_paths
from .util import (
    Clock,
    EventLog,
    ProcessResult,
    RealClock,
    ShimError,
    Snapshot,
    atomic_json,
    canonical_json,
    diff_snapshots,
    finish_process,
    sha256_bytes,
    snapshot_tree,
    start_process,
    tree_path,
)
from .validators import is_test_path, test_integrity


DECLARATION_TIMEOUT_SECONDS = 2 * 60
DEFAULT_MAX_SLOT_REDRAWS = 16
DEFAULT_FULL_RETRY_SAFETY_CAP = 12


class EventSink(Protocol):
    def emit(self, op: str, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclasses.dataclass(frozen=True)
class RunnerExecution:
    """Result retained by the scheduler; paths must be filesystem-derived."""

    process: ProcessResult
    write_records: tuple[Mapping[str, Any], ...]
    completion_snapshot: Snapshot
    identity: Mapping[str, Any]
    poll_count: int = 0
    mode: str | None = None


@dataclasses.dataclass(frozen=True)
class DeclarationExecution:
    paths: tuple[str, ...]
    error: str | None
    process: ProcessResult
    identity: Mapping[str, Any]


class SubjectRunner(Protocol):
    """Selectable runner surface used by the scheduler.

    Implementations own ``launch``, ``poll``, ``write-set``, and ``declare``
    events.  Write records must come from snapshots, never subject prose.
    """

    def run_task(
        self,
        *,
        draw_id: str,
        instance_id: str,
        side: Side,
        tree: Path,
        artifact_root: Path,
        log: EventSink,
        poll_writes: bool,
        shared_tree: bool,
    ) -> RunnerExecution: ...

    def declare_files(
        self,
        *,
        draw_id: str,
        instance_id: str,
        side: Side,
        tree: Path,
        artifact_root: Path,
        log: EventSink,
    ) -> DeclarationExecution: ...


RunnerFactory = Callable[[str, int], SubjectRunner]


class _LockedEventSink:
    """Serialize concurrent runner events into the append-only EventLog."""

    def __init__(self, target: EventLog) -> None:
        self.target = target
        self._lock = threading.Lock()

    def emit(self, op: str, **kwargs: Any) -> Mapping[str, Any]:
        with self._lock:
            return self.target.emit(op, **kwargs)


class _FilteringEventSink:
    """Suppress invalid per-principal claims in a concurrently shared tree."""

    def __init__(self, target: EventSink, suppressed: Sequence[str]) -> None:
        self.target = target
        self.suppressed = frozenset(suppressed)

    def emit(self, op: str, **kwargs: Any) -> Mapping[str, Any]:
        if op in self.suppressed:
            return {}
        return self.target.emit(op, **kwargs)


class ProductionLauncherRunner:
    """Make ``ProductionSubjectLauncher`` satisfy ``SubjectRunner``.

    Task calls use the public launch primitive.  The preliminary declaration
    call reuses its fail-closed clean-room preflight and credential unwind,
    but invokes the adapter's read-only/plan declaration command.
    """

    def __init__(
        self,
        launcher: ProductionSubjectLauncher,
        *,
        declaration_timeout_seconds: float = DECLARATION_TIMEOUT_SECONDS,
    ) -> None:
        self.launcher = launcher
        self.declaration_timeout_seconds = declaration_timeout_seconds

    def run_task(
        self,
        *,
        draw_id: str,
        instance_id: str,
        side: Side,
        tree: Path,
        artifact_root: Path,
        log: EventSink,
        poll_writes: bool,
        shared_tree: bool,
    ) -> RunnerExecution:
        del shared_tree
        result = self.launcher.run(
            draw_id=draw_id,
            instance_id=instance_id,
            side=side,
            tree=tree,
            artifact_root=artifact_root,
            log=log,  # type: ignore[arg-type]
            poll_writes=poll_writes,
        )
        return RunnerExecution(
            process=result.process,
            write_records=tuple(result.write_records),
            completion_snapshot=result.completion_snapshot,
            identity=dict(result.identity),
            poll_count=result.poll_count,
            mode=f"{result.identity.get('cli')}:{result.identity.get('model')}",
        )

    def declare_files(
        self,
        *,
        draw_id: str,
        instance_id: str,
        side: Side,
        tree: Path,
        artifact_root: Path,
        log: EventSink,
    ) -> DeclarationExecution:
        # ``_preflight`` is the launcher's audited instruction-bare seam.  A
        # public declaration primitive is intentionally not duplicated in
        # production.py; this narrow wrapper keeps all clean-room checks in
        # one implementation until that API is promoted.
        artifact_root.mkdir(parents=True, exist_ok=False)
        prepared = None
        running = None
        try:
            prepared = self.launcher._preflight(  # type: ignore[attr-defined]
                draw_id=draw_id,
                instance_id=instance_id,
                tree=tree,
                artifact_root=artifact_root,
            )
            prompt = declaration_prompt(side)
            command = self.launcher.config.adapter.declaration_command(
                prompt=prompt, cwd=tree
            )
            command = SubjectCommand(command.argv, prepared.env, command.stdin)
            log.emit(
                "launch",
                principal={"side": side.label, "instance_id": instance_id},
                subject=prepared.identity,
                detail={
                    "kind": "preliminary-file-declaration",
                    "short_call": True,
                    "timeout_seconds": self.declaration_timeout_seconds,
                    "task_prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
                },
            )
            running = start_process(
                command.argv, cwd=tree, env=command.env, stdin=command.stdin
            )
            process = finish_process(running, self.declaration_timeout_seconds)
            stdout_path = artifact_root / "stdout.txt"
            stderr_path = artifact_root / "stderr.txt"
            stdout_path.write_bytes(process.stdout)
            stderr_path.write_bytes(process.stderr)
            paths, error = parse_declaration(process.stdout)
            if not process.finished or process.returncode != 0:
                error = error or "declaration process did not finish successfully"
            log.emit(
                "declare",
                principal={"side": side.label, "instance_id": instance_id},
                subject=prepared.identity,
                detail={
                    "declared_paths": list(paths),
                    "valid": error is None,
                    "error": error,
                    "response_sha256": sha256_bytes(process.stdout),
                },
            )
            return DeclarationExecution(
                paths=paths,
                error=error,
                process=process,
                identity=dict(prepared.identity),
            )
        except BaseException:
            if (
                running is not None
                and running.process is not None
                and running.process.poll() is None
            ):
                finish_process(running, 0.0)
            raise
        finally:
            if prepared is not None:
                self.launcher._cleanup_credentials(  # type: ignore[attr-defined]
                    prepared.credential_records,
                    artifact_root=artifact_root,
                    phase="declaration-unwind",
                )


@dataclasses.dataclass
class _RegionHistory:
    last_side: str
    side_sequence: list[str]
    switches: int
    last_record: Mapping[str, Any]


class _SharedDrawRedrawRequired(Exception):
    def __init__(self, attempts: Sequence[Attempt]) -> None:
        super().__init__("shared draw contains an unattributable unfinished subject")
        self.attempts = list(attempts)


class _AlternationTracker:
    """Track actual mechanically claimed baseline-coordinate regions."""

    def __init__(self, test_paths: set[str]) -> None:
        self.test_paths = test_paths
        self.histories: dict[str, _RegionHistory] = {}

    def observe(self, attempt: Attempt) -> tuple[str, _RegionHistory] | None:
        observed: set[str] = set()
        for record in attempt.records:
            path = str(record["path"])
            if path in self.test_paths or is_test_path(path):
                continue
            for region in record.get("regions", []):
                anchor = region.get("content_anchor")
                if isinstance(anchor, Mapping):
                    key = (
                        f"{path}:anchor:{anchor.get('left_sha256')}:"
                        f"{anchor.get('left_bytes')}:{anchor.get('right_sha256')}:"
                        f"{anchor.get('right_bytes')}"
                    )
                else:
                    # Backward-compatible only for retained pre-anchor logs.
                    key = (
                        f"{path}:offset:{int(region['old_start'])}:"
                        f"{int(region['old_end'])}"
                    )
                if key in observed:
                    continue
                observed.add(key)
                history = self.histories.get(key)
                if history is None:
                    history = _RegionHistory(
                        last_side=attempt.side,
                        side_sequence=[attempt.side],
                        switches=0,
                        last_record=record,
                    )
                    self.histories[key] = history
                else:
                    if history.last_side != attempt.side:
                        history.switches += 1
                        history.side_sequence.append(attempt.side)
                    history.last_side = attempt.side
                    history.last_record = record
                if history.switches >= ESCALATION_SIDE_SWITCHES:
                    return key, history
        return None


class ProductionScheduler(Harness):
    """Run one six-arm draw with selectable production subject runners."""

    def __init__(
        self,
        *,
        runner_factory: RunnerFactory,
        max_slot_redraws: int = DEFAULT_MAX_SLOT_REDRAWS,
        full_retry_safety_cap: int = DEFAULT_FULL_RETRY_SAFETY_CAP,
        event_clock_factory: Callable[[], Clock] = RealClock,
        wall_seconds_accounting: Callable[[Mapping[str, Any], float], float]
        | None = None,
        scripted_gate_policy: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if max_slot_redraws < 0:
            raise ValueError("max_slot_redraws must be nonnegative")
        if full_retry_safety_cap < ESCALATION_SIDE_SWITCHES:
            raise ValueError(
                "full_retry_safety_cap cannot be below the N=3 escalation budget"
            )
        self.runner_factory = runner_factory
        self.max_slot_redraws = max_slot_redraws
        self.full_retry_safety_cap = full_retry_safety_cap
        self.event_clock_factory = event_clock_factory
        self.wall_seconds_accounting = wall_seconds_accounting
        self.scripted_gate_policy = scripted_gate_policy
        self._history_by_terminal: dict[str, list[Attempt]] = {}
        self._shared_waste_by_terminal: dict[str, list[Attempt]] = {}
        self._active_draw_id: str | None = None
        self._last_integration_result: dict[str, Any] | None = None

    def _mode_spec(self, **kwargs: Any) -> dict[str, Any]:
        """Gate modes are inert; retained only for inherited arm call shapes."""
        return {
            "logical_seconds": 0.0,
            "requested_gate_mode_ignored": str(kwargs.get("mode", "")),
        }

    def _integration_validate(
        self,
        *,
        commit: str,
        root: Path,
        name: str,
        log: EventSink,
    ) -> dict[str, Any]:
        result = super()._integration_validate(
            commit=commit,
            root=root,
            name=name,
            log=log,  # type: ignore[arg-type]
        )
        self._last_integration_result = result
        return result

    def _finish_execution(
        self,
        *,
        prepared: PreparedTree,
        side: Side,
        execution: RunnerExecution,
        instance_id: str,
        subject_slot: int,
        timeout_retry: int,
        integration_retry: int,
        log: EventSink,
        records: Sequence[Mapping[str, Any]] | None = None,
        validate_focal: bool = True,
    ) -> Attempt:
        after = snapshot_tree(prepared.tree)
        observed = diff_snapshots(prepared.baseline, after)
        if records is None:
            mechanical = observed
            # ProductionSubjectLauncher also snapshots the tree.  Divergence
            # between its retained write event and the scheduler's independent
            # snapshot is an instrument error, never something to paper over
            # with subject output.
            retained = [dict(row) for row in execution.write_records]
            if canonical_json(retained) != canonical_json(observed):
                raise ShimError(
                    f"runner/scheduler mechanical write-set mismatch: {instance_id}"
                )
        else:
            mechanical = [dict(row) for row in records]
        protected_tests = set(self.all_test_paths)
        protected_tests.update(
            path for path in set(prepared.baseline) | set(after) if is_test_path(path)
        )
        integrity_ok, mismatches = test_integrity(
            prepared.baseline, after, tuple(sorted(protected_tests))
        )
        process = execution.process
        focal: dict[str, Any] | None = None
        if process.finished and validate_focal:
            # Runner artifacts already own stdout/stderr.  Validation evidence
            # receives a sibling directory and cannot alter attribution.
            validation_root = (
                prepared.tree.parent.parent
                / "scheduler-validation"
                / instance_id
            )
            focal = self.validator.focal(
                tree=prepared.tree,
                site=self.site,
                side=side,
                artifact_root=validation_root,
                label=f"task-{side.label}",
            )
            log.emit(
                "validate",
                principal={"side": side.label, "instance_id": instance_id},
                subject=execution.identity,
                detail={"kind": "per-side-focal", **_detail_validation(focal)},
            )
        correct = bool(
            process.finished
            and process.returncode == 0
            and integrity_ok
            and focal is not None
            and focal.get("green")
        )
        log.emit(
            "complete",
            principal={"side": side.label, "instance_id": instance_id},
            subject=execution.identity,
            detail={
                "finished": process.finished,
                "excluded": not process.finished,
                "returncode": process.returncode,
                "timed_out": process.timed_out,
                "correct": correct,
                "test_integrity_ok": integrity_ok,
                "test_mismatches": mismatches,
                "logical_seconds": process.actual_seconds,
                "timeout_retry": timeout_retry,
                "slot_redraw_index": subject_slot,
                "integration_retry": integration_retry,
                "poll_count": execution.poll_count,
            },
        )
        return Attempt(
            side=side.label,
            instance_id=instance_id,
            subject_slot=subject_slot,
            timeout_retry=timeout_retry,
            integration_retry=integration_retry,
            tree=prepared.tree,
            source_base=prepared.source_base,
            process=process,
            records=mechanical,
            finished=process.finished,
            excluded=not process.finished,
            test_integrity_ok=integrity_ok,
            test_mismatches=mismatches,
            focal=focal,
            correct=correct,
            logical_seconds=process.actual_seconds,
            mode=execution.mode
            or f"{execution.identity.get('cli')}:{execution.identity.get('model')}",
        )

    def _invoke_once(
        self,
        *,
        prepared: PreparedTree,
        side_label: str,
        attempt_root: Path,
        instance_id: str,
        subject_slot: int,
        timeout_retry: int,
        integration_retry: int,
        log: EventSink,
        shared: bool = False,
        poll_writes: bool = False,
        validate_focal: bool = True,
    ) -> Attempt:
        runner = self.runner_factory(side_label, subject_slot)
        runner_log: EventSink = log
        if shared:
            runner_log = _FilteringEventSink(log, ("poll", "write-set"))
        execution = runner.run_task(
            draw_id=self._active_draw_id or instance_id,
            instance_id=instance_id,
            side=self.site.sides[side_label],
            tree=prepared.tree,
            artifact_root=attempt_root,
            log=runner_log,
            poll_writes=poll_writes,
            shared_tree=shared,
        )
        return self._finish_execution(
            prepared=prepared,
            side=self.site.sides[side_label],
            execution=execution,
            instance_id=instance_id,
            subject_slot=subject_slot,
            timeout_retry=timeout_retry,
            integration_retry=integration_retry,
            log=log,
            records=() if shared else None,
            validate_focal=validate_focal,
        )

    def _continue_fairness(
        self,
        *,
        side_label: str,
        draw_id: str,
        attempt_root: Path,
        log: EventSink,
        prepare: Callable[[int, int], PreparedTree],
        integration_retry: int,
        history: list[Attempt],
        first_timeout_retry: int,
        shared: bool = False,
        poll_writes: bool = False,
        validate_focal: bool = True,
    ) -> tuple[Attempt, list[Attempt]]:
        slot = history[-1].subject_slot if history else 0
        timeout_retry = first_timeout_retry
        while slot <= self.max_slot_redraws:
            while timeout_retry <= MAX_TIMEOUT_RETRIES:
                instance_id = (
                    f"{draw_id}-{side_label}-s{slot}-t{timeout_retry}"
                    f"-ir{integration_retry}"
                )
                if history and timeout_retry > 0:
                    log.emit(
                        "retry",
                        principal={"side": side_label, "instance_id": instance_id},
                        detail={
                            "kind": "timeout-retry",
                            "timeout_retry": timeout_retry,
                            "max_timeout_retries": MAX_TIMEOUT_RETRIES,
                            "fresh_agent": True,
                        },
                    )
                prepared = prepare(slot, timeout_retry)
                attempt = self._invoke_once(
                    prepared=prepared,
                    side_label=side_label,
                    attempt_root=attempt_root / f"s{slot}-t{timeout_retry}",
                    instance_id=instance_id,
                    subject_slot=slot,
                    timeout_retry=timeout_retry,
                    integration_retry=integration_retry,
                    log=log,
                    shared=shared,
                    poll_writes=poll_writes,
                    validate_focal=validate_focal,
                )
                history.append(attempt)
                if attempt.finished:
                    self._history_by_terminal[attempt.instance_id] = list(history)
                    return attempt, history
                timeout_retry += 1
            slot += 1
            if slot > self.max_slot_redraws:
                break
            instance_id = f"{draw_id}-{side_label}-s{slot}-t0-ir{integration_retry}"
            log.emit(
                "retry",
                principal={"side": side_label, "instance_id": instance_id},
                detail={
                    "kind": "slot-redraw",
                    "excluded_subject_instances": [
                        row.instance_id for row in history if row.excluded
                    ],
                    "fresh_agent": True,
                    "fresh_worktree": not shared,
                    "slot_redraw_index": slot,
                },
            )
            timeout_retry = 0
        raise ShimError(
            f"subject slot redraw safety cap exhausted for {draw_id} side {side_label}"
        )

    def _run_fair_one(
        self,
        *,
        side_label: str,
        source_base: str,
        draw_id: str,
        attempt_root: Path,
        log: EventSink,
        integration_retry: int = 0,
        poll_writes: bool = False,
        validate_focal: bool = True,
    ) -> tuple[Attempt, list[Attempt]]:
        def prepare(slot: int, timeout_retry: int) -> PreparedTree:
            return self._prepare(
                source_base=source_base,
                test_sides=(side_label,),
                label=f"prod{side_label}s{slot}t{timeout_retry}",
                log=log,  # type: ignore[arg-type]
            )

        return self._continue_fairness(
            side_label=side_label,
            draw_id=draw_id,
            attempt_root=attempt_root,
            log=log,
            prepare=prepare,
            integration_retry=integration_retry,
            history=[],
            first_timeout_retry=0,
            poll_writes=poll_writes,
            validate_focal=validate_focal,
        )

    def _materialize_snapshot(self, snapshot: Snapshot, *, label: str) -> Path:
        """Freeze a subject's exact shared-tree completion view for scoring."""
        destination = self.scratch.worktree(
            self._name(label), self.site.base_commit
        )
        current = snapshot_tree(destination)
        for relative in sorted(set(current) - set(snapshot), reverse=True):
            target = tree_path(destination, relative)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        for relative, state in sorted(snapshot.items()):
            target = tree_path(destination, relative)
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            if state.kind == "symlink":
                target.symlink_to(state.data.decode("utf-8", errors="surrogateescape"))
            elif state.kind == "file":
                target.write_bytes(state.data)
                os.chmod(target, state.mode & 0o7777)
            else:
                raise ShimError(
                    f"unsupported completion snapshot kind {state.kind!r}: {relative}"
                )
        observed = snapshot_tree(destination)
        if observed != snapshot:
            raise ShimError(f"frozen completion snapshot mismatch: {label}")
        return destination

    def _run_pair(
        self,
        *,
        prepared: Mapping[str, PreparedTree],
        modes: Mapping[str, str],
        specs: Mapping[str, Mapping[str, Any]],
        attempts_root: Path,
        id_prefix: str,
        log: EventSink,
        shared: bool,
        poll: bool = False,
    ) -> tuple[dict[str, Attempt], str]:
        del modes, specs  # policy lives in the selected runners
        original_prepared = dict(prepared)
        cycle_prepared = dict(prepared)
        slots = {"A": 0, "B": 0}
        timeout_retries = {"A": 0, "B": 0}
        histories: dict[str, list[Attempt]] = {"A": [], "B": []}
        discarded_shared_cycles: list[Attempt] = []
        cycle = 0
        pair_records: list[dict[str, Any]] = []

        while True:
            executions: dict[str, RunnerExecution] = {}
            instance_ids = {
                label: (
                    f"{id_prefix}-paircycle{cycle}-{label}-s{slots[label]}-"
                    f"t{timeout_retries[label]}-ir0"
                )
                for label in ("A", "B")
            }

            def launch(label: str) -> RunnerExecution:
                runner = self.runner_factory(label, slots[label])
                runner_log: EventSink = log
                if shared:
                    runner_log = _FilteringEventSink(log, ("poll", "write-set"))
                return runner.run_task(
                    draw_id=self._active_draw_id or id_prefix,
                    instance_id=instance_ids[label],
                    side=self.site.sides[label],
                    tree=cycle_prepared[label].tree,
                    artifact_root=(
                        attempts_root
                        / f"paircycle-{cycle}"
                        / label
                        / f"s{slots[label]}-t{timeout_retries[label]}"
                    ),
                    log=runner_log,
                    poll_writes=poll,
                    shared_tree=shared,
                )

            # No scheduler start/write barrier. The gate's selectable scripted
            # runner may disclose its own fake-only write release control.
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = {
                    label: pool.submit(launch, label) for label in ("A", "B")
                }
                for label in ("A", "B"):
                    executions[label] = futures[label].result()

            if shared:
                pair_records = diff_snapshots(
                    cycle_prepared["A"].baseline,
                    snapshot_tree(cycle_prepared["A"].tree),
                )
                scoring_prepared = {
                    label: PreparedTree(
                        tree=self._materialize_snapshot(
                            executions[label].completion_snapshot,
                            label=f"sharedC{cycle}Finish{label}",
                        ),
                        source_base=cycle_prepared[label].source_base,
                        baseline=cycle_prepared[label].baseline,
                        expected_tests=cycle_prepared[label].expected_tests,
                    )
                    for label in ("A", "B")
                }
            else:
                scoring_prepared = cycle_prepared

            cycle_results = {
                label: self._finish_execution(
                    prepared=scoring_prepared[label],
                    side=self.site.sides[label],
                    execution=executions[label],
                    instance_id=instance_ids[label],
                    subject_slot=slots[label],
                    timeout_retry=timeout_retries[label],
                    integration_retry=0,
                    log=log,
                    records=() if shared else None,
                )
                for label in sorted(
                    ("A", "B"),
                    key=lambda item: (
                        executions[item].process.finished_monotonic,
                        1 if item == "B" else 0,
                    ),
                )
            }
            for label in ("A", "B"):
                histories[label].append(cycle_results[label])

            unfinished = [
                label for label in ("A", "B") if not cycle_results[label].finished
            ]
            if not unfinished:
                terminal = cycle_results
                break
            if not shared:
                terminal = {}
                for label in ("A", "B"):
                    first = cycle_results[label]
                    if first.finished:
                        terminal[label] = first
                        continue

                    def prepare_isolated(
                        slot: int, retry: int, label: str = label
                    ) -> PreparedTree:
                        return self._prepare(
                            source_base=original_prepared[label].source_base,
                            test_sides=(label,),
                            label=f"pair{label}s{slot}t{retry}",
                            log=log,  # type: ignore[arg-type]
                        )

                    terminal[label], histories[label] = self._continue_fairness(
                        side_label=label,
                        draw_id=id_prefix,
                        attempt_root=attempts_root / label,
                        log=log,
                        prepare=prepare_isolated,
                        integration_retry=0,
                        history=histories[label],
                        first_timeout_retry=1,
                        shared=False,
                        poll_writes=poll,
                    )
                break

            # Pair-only snapshots cannot subtract one unfinished principal's
            # partial writes. Discard this whole contaminated cycle and replay
            # the peer in a fresh shared tree while advancing fairness state
            # only for the unfinished subject slot.
            discarded_shared_cycles.extend(cycle_results.values())
            next_cycle = cycle + 1
            for label in ("A", "B"):
                prior_instance_id = instance_ids[label]
                if label in unfinished:
                    if timeout_retries[label] < MAX_TIMEOUT_RETRIES:
                        timeout_retries[label] += 1
                        next_instance_id = (
                            f"{id_prefix}-paircycle{next_cycle}-{label}-s{slots[label]}-"
                            f"t{timeout_retries[label]}-ir0"
                        )
                        log.emit(
                            "retry",
                            principal={
                                "side": label,
                                "instance_id": next_instance_id,
                            },
                            detail={
                                "kind": "timeout-retry",
                                "prior_instance_id": prior_instance_id,
                                "timeout_retry": timeout_retries[label],
                                "whole_pair_redraw": True,
                                "contaminated_cycle": cycle,
                            },
                        )
                    else:
                        slots[label] += 1
                        if slots[label] > self.max_slot_redraws:
                            raise ShimError(
                                f"shared slot redraw cap exhausted: {id_prefix} {label}"
                            )
                        timeout_retries[label] = 0
                        next_instance_id = (
                            f"{id_prefix}-paircycle{next_cycle}-{label}-s{slots[label]}-"
                            "t0-ir0"
                        )
                        log.emit(
                            "retry",
                            principal={
                                "side": label,
                                "instance_id": next_instance_id,
                            },
                            detail={
                                "kind": "slot-redraw",
                                "prior_instance_id": prior_instance_id,
                                "slot_redraw_index": slots[label],
                                "whole_pair_redraw": True,
                                "contaminated_cycle": cycle,
                            },
                        )
                else:
                    next_instance_id = (
                        f"{id_prefix}-paircycle{next_cycle}-{label}-s{slots[label]}-"
                        f"t{timeout_retries[label]}-ir0"
                    )
                    log.emit(
                        "retry",
                        principal={
                            "side": label,
                            "instance_id": next_instance_id,
                        },
                        detail={
                            "kind": "shared-peer-replay-after-contaminated-cycle",
                            "prior_instance_id": prior_instance_id,
                            "subject_slot": slots[label],
                            "whole_pair_redraw": True,
                            "contaminated_cycle": cycle,
                        },
                    )
            fresh = self._prepare(
                source_base=original_prepared["A"].source_base,
                test_sides=("A", "B"),
                label=f"sharedPairRedraw{next_cycle}",
                log=log,  # type: ignore[arg-type]
            )
            cycle_prepared = {"A": fresh, "B": fresh}
            cycle = next_cycle

        for label in ("A", "B"):
            self._history_by_terminal[terminal[label].instance_id] = histories[label]

        if shared:
            log.emit(
                "write-set",
                principal={
                    "scope": "shared-pair",
                    "instances": [terminal["A"].instance_id, terminal["B"].instance_id],
                },
                subject={"cli": "shared-pair", "version": "1", "model": None},
                paths=pair_records,
                detail={
                    "basis": "shared-baseline-to-both-completions",
                    "per-principal_attribution": False,
                    "agent_text_consulted": False,
                    "start_barrier": False,
                },
            )
            terminal = {
                label: dataclasses.replace(
                    terminal[label],
                    tree=cycle_prepared["A"].tree,
                    records=pair_records,
                )
                for label in ("A", "B")
            }
            if discarded_shared_cycles:
                self._shared_waste_by_terminal[
                    terminal["A"].instance_id
                ] = discarded_shared_cycles
        later = max(
            ("A", "B"),
            key=lambda label: (
                terminal[label].process.finished_monotonic,
                1 if label == "B" else 0,
            ),
        )
        return terminal, later

    def _record_attempts(self, metrics: dict[str, Any], attempts: Sequence[Attempt]) -> None:
        expanded: list[Attempt] = []
        seen: set[str] = set()
        for terminal in attempts:
            history = self._history_by_terminal.pop(terminal.instance_id, [terminal])
            for attempt in history:
                if attempt.instance_id not in seen:
                    expanded.append(attempt)
                    seen.add(attempt.instance_id)
        super()._record_attempts(metrics, expanded)
        shared_waste: dict[str, Attempt] = {}
        for terminal in attempts:
            for attempt in self._shared_waste_by_terminal.pop(
                terminal.instance_id, []
            ):
                shared_waste[attempt.instance_id] = attempt
        if shared_waste:
            retained = set(map(str, metrics.get("wasted_instance_ids", [])))
            new_ids = set(shared_waste) - retained
            metrics["wasted_compute_seconds"] += sum(
                shared_waste[instance_id].logical_seconds for instance_id in new_ids
            )
            metrics["discarded_diff_seconds"] += sum(
                shared_waste[instance_id].logical_seconds for instance_id in new_ids
            )
            retained.update(new_ids)
            metrics["wasted_instance_ids"] = sorted(retained)
            metrics["shared_contaminated_cycle_instance_ids"] = sorted(shared_waste)

    def _serial_pair(
        self,
        *,
        draw_id: str,
        root: Path,
        log: EventSink,
        a_mode: str = "",
        b_mode: str = "",
        stall_b: bool = False,
    ) -> tuple[list[Attempt], str, float]:
        del a_mode, b_mode, stall_b
        started = time.monotonic()
        a, a_history = self._run_fair_one(
            side_label="A",
            source_base=self.site.base_commit,
            draw_id=draw_id,
            attempt_root=root / "attempts" / "A",
            log=log,
        )
        a_commit = self._commit_attempt(
            a, label="prodSeqA", message=f"{draw_id} A source result"
        )
        b, b_history = self._run_fair_one(
            side_label="B",
            source_base=a_commit,
            draw_id=draw_id,
            attempt_root=root / "attempts" / "B",
            log=log,
        )
        final = self._commit_attempt(
            b, label="prodSeqB", message=f"{draw_id} B source result"
        )
        # Inherited arm code expects positions 0/1 to be the two terminal
        # completions.  ``_record_attempts`` expands their timeout histories.
        return [a, b], final, time.monotonic() - started

    def _declaration(
        self,
        *,
        side_label: str,
        prepared: PreparedTree,
        declared_paths: Sequence[str],
        root: Path,
        instance_id: str,
        log: EventSink,
    ) -> tuple[tuple[str, ...], str | None, float]:
        del declared_paths  # declarations are subject output, never gate fixtures
        runner = self.runner_factory(side_label, 0)
        result = runner.declare_files(
            draw_id=self._active_draw_id or instance_id,
            instance_id=instance_id,
            side=self.site.sides[side_label],
            tree=prepared.tree,
            artifact_root=root,
            log=log,
        )
        return result.paths, result.error, result.process.actual_seconds

    def _arm_optimistic(
        self,
        draw_id: str,
        repeat: int,
        root: Path,
        log: EventSink,
        metrics: dict[str, Any],
    ) -> None:
        del repeat
        prepared = {
            label: self._prepare(
                source_base=self.site.base_commit,
                test_sides=(label,),
                label=f"prodOpt{label}",
                log=log,  # type: ignore[arg-type]
            )
            for label in ("A", "B")
        }
        initial, later = self._run_pair(
            prepared=prepared,
            modes={},
            specs={},
            attempts_root=root / "attempts" / "initial",
            id_prefix=f"{draw_id}-initial",
            log=log,
            shared=False,
        )
        initial_histories = {
            label: list(
                self._history_by_terminal.get(
                    initial[label].instance_id, [initial[label]]
                )
            )
            for label in ("A", "B")
        }
        self._record_attempts(metrics, [initial["A"], initial["B"]])
        commits = {
            label: self._commit_attempt(
                initial[label],
                label=f"prodOpt{label}",
                message=f"{draw_id} initial {label} source result",
            )
            for label in ("A", "B")
        }
        merge = self.scratch.merge_tree(
            commits["A"], commits["B"], message=f"{draw_id} optimistic merge"
        )
        self._merge_event(merge, log, kind="optimistic-initial")  # type: ignore[arg-type]
        validation: dict[str, Any] | None = None
        if merge.clean and merge.commit:
            validation = self._integration_validate(
                commit=merge.commit, root=root, name="prodOptInitial", log=log
            )
        recovery_needed = not merge.clean or not validation or not validation["correct"]
        metrics["later_finisher"] = later
        metrics["later_finisher_instance"] = initial[later].instance_id
        discarded_ids: list[str] = []
        retry_ids: list[str] = []
        wasted_ids: set[str] = set()
        if recovery_needed:
            winner = "B" if later == "A" else "A"
            winner_commit = commits[winner]
            discarded_ids.append(initial[later].instance_id)
            wasted_ids.update(item.instance_id for item in initial_histories[later])
            metrics["discarded_diff_seconds"] += sum(
                item.logical_seconds for item in initial_histories[later]
            )
            for retry_index in range(1, MAX_OPTIMISTIC_RETRIES + 1):
                retry_draw = f"{draw_id}-{later}-integration-retry-{retry_index}"
                log.emit(
                    "retry",
                    principal={"side": later, "instance_id": retry_draw},
                    detail={
                        "kind": "optimistic-loser-retry",
                        "retry_index": retry_index,
                        "max_retries": MAX_OPTIMISTIC_RETRIES,
                        "loser_rule": "later-finisher",
                        "fresh_from_winner": True,
                    },
                )
                retry, history = self._run_fair_one(
                    side_label=later,
                    source_base=winner_commit,
                    draw_id=retry_draw,
                    attempt_root=root / "attempts" / f"retry-{retry_index}",
                    log=log,
                    integration_retry=retry_index,
                )
                self._record_attempts(metrics, [retry])
                retry_ids.append(retry.instance_id)
                retry_compute = sum(item.logical_seconds for item in history)
                metrics["retry_compute_seconds"] += retry_compute
                wasted_ids.update(item.instance_id for item in history)
                excluded_history = [item for item in history if item.excluded]
                wasted_ids.update(item.instance_id for item in excluded_history)
                metrics["discarded_diff_seconds"] += sum(
                    item.logical_seconds for item in excluded_history
                )
                candidate = self._commit_attempt(
                    retry,
                    label=f"prodOptRetry{retry_index}",
                    message=f"{draw_id} loser retry {retry_index}",
                )
                validation = self._integration_validate(
                    commit=candidate,
                    root=root,
                    name=f"prodOptRetryInt{retry_index}",
                    log=log,
                )
                if validation["correct"]:
                    break
                discarded_ids.append(retry.instance_id)
                wasted_ids.update(item.instance_id for item in history)
                metrics["discarded_diff_seconds"] += sum(
                    item.logical_seconds for item in history if not item.excluded
                )
        metrics["integration_correct"] = bool(validation and validation["correct"])
        metrics["retry_instance_ids"] = retry_ids
        metrics["discarded_instance_ids"] = discarded_ids
        metrics["wasted_instance_ids"] = sorted(wasted_ids)
        duration = {
            row["instance_id"]: row["logical_seconds"] for row in metrics["attempts"]
        }
        metrics["wasted_compute_seconds"] = sum(
            duration.get(instance_id, 0.0) for instance_id in wasted_ids
        )

    def _arm_locks(
        self,
        draw_id: str,
        repeat: int,
        root: Path,
        log: EventSink,
        metrics: dict[str, Any],
    ) -> None:
        super()._arm_locks(
            draw_id,
            repeat,
            root,
            log,  # type: ignore[arg-type]
            metrics,
        )
        if int(metrics.get("declaration_violations", 0)) > 0:
            # A field lock service would reject at the first out-of-contract
            # write. Retain the full task-attempt cost as the observable
            # counterfactual for both serialized and parallel schedules;
            # preliminary declaration calls are intentionally excluded.
            metrics["counterfactual_refusal_seconds"] = sum(
                float(row.get("logical_seconds", 0.0))
                for row in metrics.get("attempts", [])
            )

    @staticmethod
    def _next_full_retry_side(
        *, retry_index: int, later_finisher: str, previous_side: str | None
    ) -> str:
        if retry_index == 1:
            return later_finisher
        if previous_side is None:
            return later_finisher
        return "B" if previous_side == "A" else "A"

    def _arm_full(
        self,
        draw_id: str,
        repeat: int,
        root: Path,
        log: EventSink,
        metrics: dict[str, Any],
    ) -> None:
        del repeat
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

        prepared = {
            label: self._prepare(
                source_base=self.site.base_commit,
                test_sides=(label,),
                label=f"prodFull{label}",
                log=log,  # type: ignore[arg-type]
            )
            for label in ("A", "B")
        }
        initial, later = self._run_pair(
            prepared=prepared,
            modes={},
            specs={},
            attempts_root=root / "attempts" / "initial",
            id_prefix=f"{draw_id}-initial",
            log=log,
            shared=False,
            poll=True,
        )
        self._record_attempts(metrics, [initial["A"], initial["B"]])
        commits = {
            label: self._commit_attempt(
                initial[label],
                label=f"prodFull{label}",
                message=f"{draw_id} full {label}",
            )
            for label in ("A", "B")
        }
        attribution = self._contested_attribution(initial)
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
        current_commit, choices = self.scratch.harvest(
            base=self.site.base_commit,
            left=commits["A"],
            right=commits["B"],
            name=self._name("prodHarvest0"),
            message=f"{draw_id} initial harvest",
        )
        log.emit(
            "merge",
            principal="harness",
            paths=choices,
            detail={
                "kind": "full-system-harvest",
                "retry_index": 0,
                "answer_key_role": "selection-only",
                "oracle_bytes_synthesized": False,
                "source_commit": current_commit,
            },
        )
        validation = self._integration_validate(
            commit=current_commit, root=root, name="prodFullInt0", log=log
        )
        tracker = _AlternationTracker(self.all_test_paths)
        for attempt in sorted(
            initial.values(), key=lambda row: row.process.finished_monotonic
        ):
            tracker.observe(attempt)
        previous_retry_side: str | None = None
        retry_ids: list[str] = []
        discarded: set[str] = set()
        for retry_index in range(1, self.full_retry_safety_cap + 1):
            if validation["correct"]:
                break
            side_label = self._next_full_retry_side(
                retry_index=retry_index,
                later_finisher=later,
                previous_side=previous_retry_side,
            )
            previous_retry_side = side_label
            retry_draw = f"{draw_id}-{side_label}-full-retry-{retry_index}"
            log.emit(
                "retry",
                principal={"side": side_label, "instance_id": retry_draw},
                detail={
                    "kind": "full-system-region-retry",
                    "retry_index": retry_index,
                    "fresh_agent": True,
                    "evolving_integrated_base": current_commit,
                },
            )
            retry, history = self._run_fair_one(
                side_label=side_label,
                source_base=current_commit,
                draw_id=retry_draw,
                attempt_root=root / "attempts" / f"full-retry-{retry_index}",
                log=log,
                integration_retry=retry_index,
                poll_writes=True,
            )
            self._record_attempts(metrics, [retry])
            retry_ids.append(retry.instance_id)
            retry_seconds = sum(item.logical_seconds for item in history)
            metrics["retry_compute_seconds"] += retry_seconds
            candidate = self._commit_attempt(
                retry,
                label=f"prodFullRetry{retry_index}",
                message=f"{draw_id} full retry {retry_index} {side_label}",
            )
            if side_label == "A":
                left, right = candidate, current_commit
            else:
                left, right = current_commit, candidate
            current_commit, choices = self.scratch.harvest(
                base=current_commit,
                left=left,
                right=right,
                name=self._name(f"prodHarvest{retry_index}"),
                message=f"{draw_id} retry harvest {retry_index}",
            )
            log.emit(
                "merge",
                principal="harness",
                paths=choices,
                detail={
                    "kind": "full-system-harvest",
                    "retry_index": retry_index,
                    "answer_key_role": "selection-only",
                    "oracle_bytes_synthesized": False,
                    "source_commit": current_commit,
                },
            )
            escalation = tracker.observe(retry)
            if escalation is not None:
                key, region_history = escalation
                log.emit(
                    "escalate",
                    principal="harness",
                    paths=[region_history.last_record],
                    detail={
                        "kind": "N=3-region-alternation",
                        "region_key": key,
                        "side_sequence": region_history.side_sequence,
                        "side_switches": region_history.switches,
                        "budget": ESCALATION_SIDE_SWITCHES,
                        "actual_write_claims_only": True,
                    },
                )
                metrics["escalation_count"] = 1
                metrics["escalation"] = {
                    "region_key": key,
                    "side_sequence": region_history.side_sequence,
                    "side_switches": region_history.switches,
                    "budget": ESCALATION_SIDE_SWITCHES,
                }
                discarded.update(item.instance_id for item in history)
                metrics["discarded_diff_seconds"] += retry_seconds
                break
            validation = self._integration_validate(
                commit=current_commit,
                root=root,
                name=f"prodFullInt{retry_index}",
                log=log,
            )
            if validation["correct"]:
                break
            if not validation["correct"]:
                discarded.update(item.instance_id for item in history)
                metrics["discarded_diff_seconds"] += retry_seconds
        else:
            # This is an instrument safety cap, not the preregistered N=3
            # outcome.  Record it distinctly and never mislabel it as N=3.
            log.emit(
                "escalate",
                principal="harness",
                detail={
                    "kind": "instrument-full-retry-safety-cap",
                    "retry_count": self.full_retry_safety_cap,
                },
            )
            metrics["escalation_count"] = 1
            metrics["instrument_safety_cap"] = True
        metrics["integration_correct"] = bool(
            validation["correct"] and not metrics["escalation_count"]
        )
        metrics["retry_instance_ids"] = retry_ids
        metrics["discarded_instance_ids"] = sorted(discarded)
        metrics["wasted_instance_ids"] = sorted(discarded)
        duration = {
            row["instance_id"]: row["logical_seconds"] for row in metrics["attempts"]
        }
        metrics["wasted_compute_seconds"] = sum(
            duration.get(instance_id, 0.0) for instance_id in discarded
        )

    def run_draw(
        self, *, arm: int, repeat: int, root: Path, run_id: str
    ) -> dict[str, Any]:
        if arm not in ARMS:
            raise ShimError(f"unknown arm: {arm}")
        draw_id = f"{self.site.repo_slug}-{self.site.merge[:8]}-a{arm}-r{repeat}"
        root.mkdir(parents=True, exist_ok=False)
        metrics = self._base_metrics(draw_id, arm, repeat)
        self._active_draw_id = draw_id
        self._last_integration_result = None
        started = time.monotonic()
        try:
            with EventLog(
                root / "events.jsonl",
                run_id=run_id,
                draw_id=draw_id,
                site=self.site.event_identity(),
                arm=ARMS[arm],
                stratum=self.site.stratum,
                clock=self.event_clock_factory(),
            ) as raw_log:
                log = _LockedEventSink(raw_log)
                try:
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
                except _SharedDrawRedrawRequired as redraw:
                    self._record_attempts(metrics, redraw.attempts)
                    metrics["draw_status"] = "excluded-whole-draw-redraw-required"
                    metrics["redraw_required"] = True
                    metrics["redraw_scope"] = "whole-draw"
        except BaseException:
            self._active_draw_id = None
            raise
        actual_wall_seconds = time.monotonic() - started
        metrics["wall_seconds"] = (
            self.wall_seconds_accounting(metrics, actual_wall_seconds)
            if self.wall_seconds_accounting is not None
            else actual_wall_seconds
        )
        metrics["integration_validation_attempted"] = (
            self._last_integration_result is not None
        )
        if self._last_integration_result is None:
            metrics["buildability"] = None
            metrics["buildability_reason"] = "integration validation was not attempted"
        else:
            buildability = self._last_integration_result.get("buildability")
            metrics["buildability"] = (
                {
                    key: buildability.get(key)
                    for key in (
                        "oracle",
                        "scope",
                        "returncode",
                        "timed_out",
                        "launch_error",
                        "stdout_sha256",
                        "stderr_sha256",
                        "buildable",
                        "limitation",
                    )
                }
                if isinstance(buildability, Mapping)
                else None
            )
            metrics["buildability_reason"] = (
                None
                if isinstance(buildability, Mapping)
                else str(
                    self._last_integration_result.get(
                        "reason", "integration ended before the buildability oracle"
                    )
                )
            )
        terminal_by_side: dict[str, Mapping[str, Any]] = {}
        for attempt in metrics["attempts"]:
            terminal_by_side[str(attempt["side"])] = attempt
        if metrics.get("redraw_required"):
            metrics["accepted_completion_instance_ids"] = {}
            metrics["correct_completions"] = 0
        else:
            metrics["accepted_completion_instance_ids"] = {
                side: value["instance_id"]
                for side, value in sorted(terminal_by_side.items())
            }
            metrics["correct_completions"] = sum(
                bool(value.get("correct")) for value in terminal_by_side.values()
            )
        durations = {
            str(row["instance_id"]): float(row["logical_seconds"])
            for row in metrics["attempts"]
        }
        timeout_waste_ids = {
            str(row["instance_id"])
            for row in metrics["attempts"]
            if row.get("excluded")
        }
        retained_waste_ids = set(map(str, metrics.get("wasted_instance_ids", [])))
        newly_counted = timeout_waste_ids - retained_waste_ids
        metrics["wasted_compute_seconds"] += sum(
            durations.get(instance_id, 0.0) for instance_id in newly_counted
        )
        retained_waste_ids.update(timeout_waste_ids)
        metrics["wasted_instance_ids"] = sorted(retained_waste_ids)
        metrics["timeout_excluded_compute_seconds"] = sum(
            durations.get(instance_id, 0.0) for instance_id in timeout_waste_ids
        )
        metrics["timeout_excluded_instances"] = sorted(timeout_waste_ids)
        metrics["slot_redraw_instances"] = sorted(
            str(row["instance_id"])
            for row in metrics["attempts"]
            if int(row.get("slot_redraw_index", 0)) > 0
            and bool(row.get("finished"))
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
        metrics["scheduler"] = {
            "kind": "production-runner-seam",
            "scripted_gate_policy": self.scripted_gate_policy,
            "event_clock": (
                "real" if self.event_clock_factory is RealClock else "injected"
            ),
            "arm2_start_barrier": False,
            "max_timeout_retries": MAX_TIMEOUT_RETRIES,
            "max_optimistic_retries": MAX_OPTIMISTIC_RETRIES,
            "region_alternation_budget": ESCALATION_SIDE_SWITCHES,
            "full_retry_safety_cap": self.full_retry_safety_cap,
            "wall_accounting": (
                "injected" if self.wall_seconds_accounting is not None else "actual"
            ),
        }
        atomic_json(root / "metrics.json", metrics)
        self._active_draw_id = None
        return metrics
