"""Existing deterministic fake scripts exposed through ``SubjectRunner``.

This is the fake-only gate policy for :class:`ProductionScheduler`.  The
production scheduler remains unaware of cheater/staller/alternator fixtures.
The runner discloses and owns the A-then-B shared-write release control needed
for byte-identical repeated gate runs; a real ``ProductionLauncherRunner`` has
no such control.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .adapters import ScriptedAdapter, declaration_prompt, parse_declaration, task_prompt
from .production_scheduler import (
    DeclarationExecution,
    EventSink,
    RunnerExecution,
    SubjectRunner,
)
from .schema import Side, Site
from .util import (
    ProcessResult,
    ShimError,
    atomic_json,
    diff_snapshots,
    finish_process,
    sha256_bytes,
    snapshot_tree,
    start_process,
)


SCRIPTED_TIMEOUT_SECONDS = 15.0
SCRIPTED_STALL_TIMEOUT_SECONDS = 1.0
SCRIPTED_POLL_SECONDS = 0.05
_DRAW = re.compile(r"-a(?P<arm>[1-6])-r(?P<repeat>[12])$")


@dataclasses.dataclass
class _ControlState:
    condition: threading.Condition = dataclasses.field(
        default_factory=lambda: threading.Condition(threading.Lock())
    )
    a_launch_emitted: bool = False
    shared_paths: dict[str, tuple[Path, Path, Path]] = dataclasses.field(
        default_factory=dict
    )
    shared_a_write_finished: bool = False
    shared_a_completion_captured: bool = False
    final_event_a_emitted: bool = False
    poll_position: int = 0


class ScriptedInterleavingControl:
    """Determinize fake observations without entering scheduler policy."""

    def __init__(self, timeout_seconds: float = SCRIPTED_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._states: dict[str, _ControlState] = {}

    def _state(self, draw_id: str) -> _ControlState:
        with self._lock:
            return self._states.setdefault(draw_id, _ControlState())

    @staticmethod
    def _wait(condition: threading.Condition, predicate: Callable[[], bool], deadline: float) -> None:
        while not predicate():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ShimError("scripted interleaving control timed out")
            condition.wait(timeout=min(0.05, remaining))

    def ordered_launch(self, draw_id: str, side: str, emit: Callable[[], None]) -> None:
        state = self._state(draw_id)
        deadline = time.monotonic() + self.timeout_seconds
        with state.condition:
            if side == "B":
                self._wait(
                    state.condition,
                    lambda: state.a_launch_emitted,
                    deadline,
                )
            emit()
            if side == "A":
                state.a_launch_emitted = True
                state.condition.notify_all()

    @staticmethod
    def _wait_file(path: Path, deadline: float) -> None:
        while not path.is_file():
            if time.monotonic() >= deadline:
                raise ShimError(f"scripted signal timed out: {path}")
            time.sleep(0.005)

    def release_shared(
        self,
        *,
        draw_id: str,
        side: str,
        ready: Path,
        release: Path,
        wrote: Path,
    ) -> None:
        state = self._state(draw_id)
        deadline = time.monotonic() + self.timeout_seconds
        with state.condition:
            state.shared_paths[side] = (ready, release, wrote)
            state.condition.notify_all()
            self._wait(
                state.condition,
                lambda: set(state.shared_paths) == {"A", "B"},
                deadline,
            )
        for item_ready, _, _ in state.shared_paths.values():
            self._wait_file(item_ready, deadline)
        if side == "A":
            release.write_bytes(b"release\n")
            self._wait_file(wrote, deadline)
            with state.condition:
                state.shared_a_write_finished = True
                state.condition.notify_all()
            return
        with state.condition:
            self._wait(
                state.condition,
                lambda: state.shared_a_completion_captured,
                deadline,
            )
        release.write_bytes(b"release\n")
        self._wait_file(wrote, deadline)

    def mark_shared_completion(self, draw_id: str, side: str) -> None:
        if side != "A":
            return
        state = self._state(draw_id)
        with state.condition:
            state.shared_a_completion_captured = True
            state.condition.notify_all()

    def ordered_poll(
        self,
        *,
        draw_id: str,
        side: str,
        poll_index: int,
        emit: Callable[[], None],
    ) -> None:
        state = self._state(draw_id)
        wanted = poll_index * 2 + (0 if side == "A" else 1)
        deadline = time.monotonic() + self.timeout_seconds
        with state.condition:
            self._wait(
                state.condition,
                lambda: state.poll_position == wanted,
                deadline,
            )
            emit()
            state.poll_position += 1
            state.condition.notify_all()

    def wait_for_polls_complete(
        self,
        *,
        draw_id: str,
        expected_poll_events: int,
    ) -> None:
        """Keep completion snapshots behind every ordered paired poll."""

        state = self._state(draw_id)
        deadline = time.monotonic() + self.timeout_seconds
        with state.condition:
            self._wait(
                state.condition,
                lambda: state.poll_position == expected_poll_events,
                deadline,
            )

    def ordered_final_event(
        self,
        *,
        draw_id: str,
        side: str,
        initial_pair: bool,
        emit: Callable[[], None],
    ) -> None:
        if not initial_pair:
            emit()
            return
        state = self._state(draw_id)
        deadline = time.monotonic() + self.timeout_seconds
        with state.condition:
            if side == "B":
                self._wait(
                    state.condition,
                    lambda: state.final_event_a_emitted,
                    deadline,
                )
            emit()
            if side == "A":
                state.final_event_a_emitted = True
                state.condition.notify_all()


def _parse_draw(draw_id: str) -> tuple[int, int]:
    match = _DRAW.search(draw_id)
    if match is None:
        raise ShimError(f"scripted runner could not parse draw identity: {draw_id}")
    return int(match.group("arm")), int(match.group("repeat"))


class ScriptedSubjectRunner(SubjectRunner):
    def __init__(
        self,
        *,
        site: Site,
        side_label: str,
        subject_slot: int,
        control: ScriptedInterleavingControl,
        fake_root: Path | None = None,
        timeout_seconds: float = SCRIPTED_TIMEOUT_SECONDS,
        poll_seconds: float = SCRIPTED_POLL_SECONDS,
    ) -> None:
        self.site = site
        self.side_label = side_label
        self.subject_slot = subject_slot
        self.control = control
        self.fake_root = fake_root or (Path(__file__).resolve().parent / "fakes")
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds

    @property
    def identity(self) -> dict[str, str]:
        return {
            "cli": "scripted-fake",
            "version": "shim-fake-v1",
            "model": "deterministic",
        }

    def _script(self, mode: str) -> Path:
        value = self.fake_root / f"{mode}.py"
        if not value.is_file():
            raise ShimError(f"scripted fake is absent: {value}")
        return value

    def _unique_path(self, side_label: str) -> str:
        side = self.site.sides[side_label]
        other = set(self.site.sides["B" if side_label == "A" else "A"].source_paths)
        candidates = [path for path in side.source_paths if path not in other]
        if not candidates:
            raise ShimError(
                f"scripted benign fixture lacks a side-unique path: {self.site.site_id} {side_label}"
            )
        return sorted(candidates)[0]

    @staticmethod
    def _retry_index(instance_id: str, marker: str) -> int | None:
        match = re.search(re.escape(marker) + r"-(\d+)", instance_id)
        return int(match.group(1)) if match else None

    def _mode(self, *, draw_id: str, instance_id: str, side: Side) -> str:
        arm, repeat = _parse_draw(draw_id)
        byte = self.site.stratum == "byte-intersecting"
        boundary = self.site.stratum in {"same-file-disjoint", "boundary-only"}
        if arm == 1:
            if byte and repeat == 2 and side.label == "A":
                return "cheater"
            if (
                not byte
                and repeat == 2
                and side.label == "B"
                and self.subject_slot == 0
            ):
                return "staller"
            return "collision" if side.label == "A" else "answer"
        if arm == 2:
            return "benign" if boundary and repeat == 1 else "collision"
        if arm == 3:
            retry = self._retry_index(instance_id, "integration-retry")
            if retry is None:
                return "collision"
            if byte and repeat == 1 and retry == 1:
                return "benign"
            return "answer"
        if arm == 4:
            if boundary:
                return "benign" if repeat == 1 else "collision"
            return "collision" if side.label == "A" else "answer"
        if arm == 5:
            if boundary:
                return "collision"
            return "collision" if side.label == "A" else "answer"
        if arm == 6:
            return "alternator" if byte and repeat == 2 else "collision"
        raise AssertionError(arm)

    def _logical_seconds(
        self, *, draw_id: str, instance_id: str, side: Side, mode: str
    ) -> float:
        arm, _ = _parse_draw(draw_id)
        if mode == "staller":
            return SCRIPTED_STALL_TIMEOUT_SECONDS
        retry = self._retry_index(instance_id, "integration-retry")
        if retry is not None:
            return 0.04 + retry * 0.01
        if self._retry_index(instance_id, "full-retry") is not None:
            return 0.03
        if arm == 6:
            return 0.09 if side.label == "A" else 0.12
        return 0.06 if side.label == "A" else 0.10

    def _alternation_token(self, instance_id: str, side: str) -> str:
        retry = self._retry_index(instance_id, "full-retry")
        if retry is None:
            return "1111" if side == "A" else "2222"
        return f"{retry + 2}" * 4

    @staticmethod
    def _control_id(draw_id: str, instance_id: str) -> str:
        cycle = re.search(r"paircycle(\d+)", instance_id)
        return f"{draw_id}:paircycle{cycle.group(1)}" if cycle else draw_id

    def _spec(
        self,
        *,
        draw_id: str,
        instance_id: str,
        side: Side,
        mode: str,
        artifact_root: Path,
        shared_tree: bool,
    ) -> dict[str, Any]:
        source_union = sorted(
            set(self.site.sides["A"].source_paths)
            | set(self.site.sides["B"].source_paths)
        )
        spec: dict[str, Any] = {
            "side": side.label,
            "source_patch": str(side.source_patch),
            "source_patch_sha256": side.source_patch_sha256,
            "source_paths": source_union,
            "test_paths": list(side.test_paths),
            "answer_commit": self.site.answer_commit,
            "logical_seconds": self._logical_seconds(
                draw_id=draw_id, instance_id=instance_id, side=side, mode=mode
            ),
            "pre_delay_seconds": 0.01 if side.label == "A" else 0.02,
            "post_delay_seconds": 0.05 if side.label == "A" else 0.10,
            "write_signal": str((artifact_root / "writes-complete.signal").resolve()),
        }
        if mode == "benign":
            spec.update(
                {
                    "benign_path": self._unique_path(side.label),
                    "benign_marker": f"# ARMS_SHIM_BENIGN_{side.label}",
                }
            )
        elif mode == "cheater":
            spec["cheat_path"] = side.test_paths[0]
        elif mode == "staller":
            spec["stall_seconds"] = 60.0
        elif mode == "alternator":
            candidates = [
                path
                for path in self.site.strict_overlap_paths
                if path not in set(side.test_paths)
            ] or list(side.source_paths)
            spec.update(
                {
                    "alternation_path": sorted(candidates)[0],
                    "alternation_start": 0,
                    "alternation_end": 4,
                    "alternation_token": self._alternation_token(
                        instance_id, side.label
                    ),
                }
            )
        if shared_tree:
            spec.update(
                {
                    "started_signal": str(
                        (artifact_root / "ready-for-write.signal").resolve()
                    ),
                    "write_release_signal": str(
                        (artifact_root / "release-write.signal").resolve()
                    ),
                    "write_release_timeout_seconds": self.timeout_seconds,
                }
            )
        return spec

    @staticmethod
    def _logical_process(result: ProcessResult, seconds: float) -> ProcessResult:
        return dataclasses.replace(
            result,
            started_monotonic=0.0,
            finished_monotonic=seconds,
        )

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
        artifact_root.mkdir(parents=True, exist_ok=False)
        mode = self._mode(draw_id=draw_id, instance_id=instance_id, side=side)
        control_id = self._control_id(draw_id, instance_id)
        spec = self._spec(
            draw_id=draw_id,
            instance_id=instance_id,
            side=side,
            mode=mode,
            artifact_root=artifact_root,
            shared_tree=shared_tree,
        )
        spec_path = artifact_root / "spec.json"
        atomic_json(spec_path, spec)
        (artifact_root / "task.txt").write_text(task_prompt(side), encoding="utf-8")
        adapter = ScriptedAdapter(self._script(mode))
        command = adapter.command_for_spec(spec_path)
        timeout = (
            SCRIPTED_STALL_TIMEOUT_SECONDS if mode == "staller" else self.timeout_seconds
        )

        def launch_event() -> None:
            log.emit(
                "launch",
                principal={"side": side.label, "instance_id": instance_id},
                subject=self.identity,
                detail={
                    "timeout_seconds": timeout,
                    "task_prompt_sha256": sha256_bytes(task_prompt(side).encode("utf-8")),
                    "script_mode": mode,
                    "scripted_write_release_control": shared_tree,
                    "model_call": False,
                },
            )

        paired_call = re.search(r"paircycle\d+", instance_id) is not None
        if paired_call:
            self.control.ordered_launch(control_id, side.label, launch_event)
        else:
            launch_event()
        baseline = snapshot_tree(tree)
        running = start_process(
            command.argv,
            cwd=tree,
            env=command.env,
            stdin=command.stdin,
        )
        if shared_tree:
            self.control.release_shared(
                draw_id=control_id,
                side=side.label,
                ready=Path(spec["started_signal"]),
                release=Path(spec["write_release_signal"]),
                wrote=Path(spec["write_signal"]),
            )
        if poll_writes:
            wrote = Path(spec["write_signal"])
            ScriptedInterleavingControl._wait_file(
                wrote, time.monotonic() + self.timeout_seconds
            )
            for poll_index in range(2):
                time.sleep(self.poll_seconds)
                paths = diff_snapshots(baseline, snapshot_tree(tree))
                emit_poll = lambda paths=paths, poll_index=poll_index: log.emit(
                        "poll",
                        principal={"side": side.label, "instance_id": instance_id},
                        subject=self.identity,
                        paths=paths,
                        detail={
                            "poll_index": poll_index,
                            "gate_poll_seconds": self.poll_seconds,
                            "production_poll_seconds": 30,
                            "scripted_scaled_poll": True,
                        },
                    )
                if paired_call:
                    self.control.ordered_poll(
                        draw_id=control_id,
                        side=side.label,
                        poll_index=poll_index,
                        emit=emit_poll,
                    )
                else:
                    emit_poll()
            if paired_call:
                self.control.wait_for_polls_complete(
                    draw_id=control_id,
                    expected_poll_events=4,
                )
        result = finish_process(running, timeout)
        completion = snapshot_tree(tree)
        if shared_tree:
            self.control.mark_shared_completion(control_id, side.label)
        records = tuple(diff_snapshots(baseline, completion))
        (artifact_root / "stdout.txt").write_bytes(result.stdout)
        (artifact_root / "stderr.txt").write_bytes(result.stderr)
        logical_seconds = float(spec["logical_seconds"])
        result = self._logical_process(result, logical_seconds)
        def final_write_event() -> None:
            log.emit(
                "write-set",
                principal={"side": side.label, "instance_id": instance_id},
                subject=self.identity,
                paths=records,
                detail={
                    "basis": "filesystem-snapshot-diff",
                    "agent_text_consulted": False,
                },
            )

        self.control.ordered_final_event(
            draw_id=control_id,
            side=side.label,
            initial_pair=paired_call,
            emit=final_write_event,
        )
        return RunnerExecution(
            process=result,
            write_records=records,
            completion_snapshot=completion,
            identity=self.identity,
            poll_count=2 if poll_writes else 0,
            mode=mode,
        )

    def _declared_paths(self, draw_id: str, side: Side) -> tuple[str, ...]:
        _arm, _repeat = _parse_draw(draw_id)
        if self.site.stratum in {"same-file-disjoint", "boundary-only"}:
            return (self._unique_path(side.label),)
        return tuple(side.source_paths)

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
        artifact_root.mkdir(parents=True, exist_ok=False)
        declared = self._declared_paths(draw_id, side)
        spec_path = artifact_root / "spec.json"
        atomic_json(spec_path, {"declared_paths": list(declared)})
        (artifact_root / "task.txt").write_text(
            declaration_prompt(side), encoding="utf-8"
        )
        adapter = ScriptedAdapter(self._script("collision"))
        command = adapter.command_for_spec(spec_path, declare=True)
        log.emit(
            "launch",
            principal={"side": side.label, "instance_id": instance_id},
            subject=self.identity,
            detail={
                "kind": "preliminary-file-declaration",
                "short_call": True,
                "model_call": False,
            },
        )
        running = start_process(command.argv, cwd=tree, env=command.env)
        actual = finish_process(running, self.timeout_seconds)
        (artifact_root / "stdout.txt").write_bytes(actual.stdout)
        (artifact_root / "stderr.txt").write_bytes(actual.stderr)
        process = self._logical_process(actual, 0.01)
        parsed, error = parse_declaration(process.stdout)
        if not process.finished or process.returncode != 0:
            error = error or "declaration process did not finish successfully"
        log.emit(
            "declare",
            principal={"side": side.label, "instance_id": instance_id},
            subject=self.identity,
            detail={
                "declared_paths": list(parsed),
                "valid": error is None,
                "error": error,
                "response_sha256": sha256_bytes(process.stdout),
            },
        )
        return DeclarationExecution(parsed, error, process, self.identity)


class ScriptedRunnerFactory:
    """One per site/scheduler; safe for the scheduler's two worker threads."""

    def __init__(
        self,
        site: Site,
        *,
        timeout_seconds: float = SCRIPTED_TIMEOUT_SECONDS,
        poll_seconds: float = SCRIPTED_POLL_SECONDS,
    ) -> None:
        self.site = site
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.control = ScriptedInterleavingControl(timeout_seconds)

    def __call__(self, side_label: str, subject_slot: int) -> ScriptedSubjectRunner:
        return ScriptedSubjectRunner(
            site=self.site,
            side_label=side_label,
            subject_slot=subject_slot,
            control=self.control,
            timeout_seconds=self.timeout_seconds,
            poll_seconds=self.poll_seconds,
        )


def logical_agent_wall(metrics: Mapping[str, Any], _actual_seconds: float) -> float:
    """Gate-only deterministic critical-path wall; never used for real draws."""
    attempts = [
        row for row in metrics.get("attempts", []) if isinstance(row, Mapping)
    ]
    attempt_total = sum(float(row.get("logical_seconds", 0.0)) for row in attempts)
    declaration_seconds = max(
        0.0, float(metrics.get("agent_seconds", 0.0)) - attempt_total
    )
    arm_value = metrics.get("arm", {})
    arm = int(arm_value.get("id", 0)) if isinstance(arm_value, Mapping) else 0
    schedule = str(metrics.get("schedule", ""))
    if arm == 1 or schedule == "serialize":
        return declaration_seconds + attempt_total

    def parallel_cycles(rows: list[Mapping[str, Any]]) -> float:
        cycles: dict[str, dict[str, float]] = {}
        leftovers: dict[str, float] = {"A": 0.0, "B": 0.0}
        for row in rows:
            instance = str(row.get("instance_id", ""))
            match = re.search(r"paircycle(\d+)", instance)
            side = str(row.get("side", ""))
            seconds = float(row.get("logical_seconds", 0.0))
            if match:
                cycle = cycles.setdefault(match.group(1), {"A": 0.0, "B": 0.0})
                cycle[side] = cycle.get(side, 0.0) + seconds
            else:
                leftovers[side] = leftovers.get(side, 0.0) + seconds
        return sum(max(values.values()) for values in cycles.values()) + max(
            leftovers.values()
        )

    if arm in {2, 4, 5}:
        return declaration_seconds + parallel_cycles(attempts)
    if arm in {3, 6}:
        initial = [
            row for row in attempts if int(row.get("integration_retry", 0)) == 0
        ]
        retries = [
            row for row in attempts if int(row.get("integration_retry", 0)) > 0
        ]
        return (
            declaration_seconds
            + parallel_cycles(initial)
            + sum(float(row.get("logical_seconds", 0.0)) for row in retries)
        )
    return declaration_seconds + attempt_total
