from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from instruments.arms.shim.gitops import MergeResult
from instruments.arms.shim.harness import Attempt, PreparedTree
from instruments.arms.shim.production_scheduler import (
    DeclarationExecution,
    ProductionScheduler,
    RunnerExecution,
)
from instruments.arms.shim.schema import Side, Site
from instruments.arms.shim.util import (
    ProcessResult,
    diff_snapshots,
    sha256_bytes,
    snapshot_tree,
)


_IDENTITY = {"cli": "in-process-mock", "version": "1", "model": "none"}


class _Controller:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active: dict[str, int] = {}
        self.max_active: dict[str, int] = {}
        self.barriers: dict[str, threading.Barrier] = {}
        self.task_calls: list[tuple[str, str]] = []
        self.declaration_calls: list[tuple[str, str]] = []

    def enter_parallel(self, draw_id: str) -> None:
        with self.lock:
            self.active[draw_id] = self.active.get(draw_id, 0) + 1
            self.max_active[draw_id] = max(
                self.max_active.get(draw_id, 0), self.active[draw_id]
            )
            barrier = self.barriers.setdefault(draw_id, threading.Barrier(2))
        barrier.wait(timeout=5)

    def leave_parallel(self, draw_id: str) -> None:
        with self.lock:
            self.active[draw_id] -= 1


class _MockRunner:
    def __init__(self, side: str, controller: _Controller) -> None:
        self.side = side
        self.controller = controller

    def run_task(
        self,
        *,
        draw_id: str,
        instance_id: str,
        side: Side,
        tree: Path,
        artifact_root: Path,
        log: object,
        poll_writes: bool,
        shared_tree: bool,
    ) -> RunnerExecution:
        del shared_tree
        artifact_root.mkdir(parents=True, exist_ok=False)
        initial = snapshot_tree(tree)
        # Every arm except the sequential baseline initially dispatches a pair.
        # Retried tasks contain "retry" and must not wait for a peer.
        arm = int(draw_id.rsplit("-a", 1)[1].split("-", 1)[0])
        paired = "paircycle" in instance_id
        if paired:
            self.controller.enter_parallel(draw_id)
        started = time.monotonic()
        log.emit(
            "launch",
            principal={"side": side.label, "instance_id": instance_id},
            subject=_IDENTITY,
            detail={"kind": "in-process-dry-run", "model_call": False},
        )
        target = tree / "shared.txt"
        # Same byte coordinates with actual, mechanically different writes let
        # arm 6 reach N=3 through its normal retry tracker.
        token = ("AAAA" if side.label == "A" else "BBBB").encode("ascii")
        current = target.read_bytes()
        target.write_bytes(token + current[4:])
        after = snapshot_tree(tree)
        writes = tuple(diff_snapshots(initial, after))
        log.emit(
            "write-set",
            principal={"side": side.label, "instance_id": instance_id},
            subject=_IDENTITY,
            paths=writes,
            detail={"basis": "in-process-filesystem-snapshot", "agent_text_consulted": False},
        )
        poll_count = 0
        if poll_writes:
            log.emit(
                "poll",
                principal={"side": side.label, "instance_id": instance_id},
                subject=_IDENTITY,
                paths=writes,
                detail={"poll_index": 0, "dry_run": True},
            )
            poll_count = 1
        if paired:
            # Keep both run_task calls live after their actual writes.  A
            # scheduler that serializes before entering the runner deadlocks.
            time.sleep(0.01)
            self.controller.leave_parallel(draw_id)
        duration = 0.01 if side.label == "A" else 0.02
        finished = started + duration
        process = ProcessResult(
            argv=("in-process",),
            returncode=0,
            stdout=b"FILES-READ: ignored-by-instrument\n",
            stderr=b"",
            started_monotonic=started,
            finished_monotonic=finished,
        )
        self.controller.task_calls.append((draw_id, side.label))
        return RunnerExecution(
            process=process,
            write_records=writes,
            completion_snapshot=after,
            identity=_IDENTITY,
            poll_count=poll_count,
        )

    def declare_files(
        self,
        *,
        draw_id: str,
        instance_id: str,
        side: Side,
        tree: Path,
        artifact_root: Path,
        log: object,
    ) -> DeclarationExecution:
        del tree
        artifact_root.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()
        paths = (f"declared-{side.label}.txt",)
        payload = json.dumps(list(paths)).encode("utf-8")
        process = ProcessResult(
            argv=("in-process-declare",),
            returncode=0,
            stdout=payload,
            stderr=b"",
            started_monotonic=started,
            finished_monotonic=started + 0.001,
        )
        log.emit(
            "launch",
            principal={"side": side.label, "instance_id": instance_id},
            subject=_IDENTITY,
            detail={"kind": "preliminary-file-declaration", "model_call": False},
        )
        log.emit(
            "declare",
            principal={"side": side.label, "instance_id": instance_id},
            subject=_IDENTITY,
            detail={
                "declared_paths": list(paths),
                "valid": True,
                "error": None,
                "response_sha256": sha256_bytes(payload),
            },
        )
        self.controller.declaration_calls.append((draw_id, side.label))
        return DeclarationExecution(paths, None, process, _IDENTITY)


class _TimeoutThenRedrawRunner(_MockRunner):
    def __init__(self, side: str, slot: int, controller: _Controller) -> None:
        super().__init__(side, controller)
        self.slot = slot

    def run_task(self, **kwargs: object) -> RunnerExecution:
        result = super().run_task(**kwargs)  # type: ignore[arg-type]
        side = kwargs["side"]
        if isinstance(side, Side) and side.label == "B" and self.slot == 0:
            process = dataclasses.replace(
                result.process,
                returncode=None,
                timed_out=True,
            )
            return dataclasses.replace(result, process=process)
        return result


class _OverlappingUnderdeclareRunner(_MockRunner):
    def declare_files(self, **kwargs: object) -> DeclarationExecution:
        result = super().declare_files(**kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(result, paths=("declared-shared.txt",))


class _Validator:
    def focal(self, **_: object) -> dict[str, object]:
        return {
            "green": True,
            "returncode": 0,
            "timed_out": False,
            "test_integrity_after_validation": True,
            "targets": ["tests/test_shared.py"],
        }


class _Scratch:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.counter = 0

    def _commit(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def merge_tree(self, left: str, right: str, *, message: str) -> MergeResult:
        del left, right, message
        return MergeResult(True, self._commit("merge"), "f" * 40, 0, b"tree\n", b"")

    def worktree(self, name: str, _commit: str) -> Path:
        tree = self.root / "frozen" / name
        tree.mkdir(parents=True, exist_ok=False)
        tree.joinpath("shared.txt").write_text("0000 base\n", encoding="utf-8")
        tests = tree / "tests" / "test_shared.py"
        tests.parent.mkdir(parents=True)
        tests.write_text("# frozen\n", encoding="utf-8")
        return tree

    def harvest(self, **_: object) -> tuple[str, list[dict[str, object]]]:
        return self._commit("harvest"), []

    def tracked_paths(self, _commit: str) -> set[str]:
        return {"shared.txt", "declared-A.txt", "declared-B.txt"}


class _DryScheduler(ProductionScheduler):
    """Keep orchestration real while replacing only git/pytest I/O."""

    def _prepare(
        self,
        *,
        source_base: str,
        test_sides: tuple[str, ...],
        label: str,
        log: object,
    ) -> PreparedTree:
        tree = self.scratch.root / "trees" / self._name(label)
        tree.mkdir(parents=True, exist_ok=False)
        tree.joinpath("shared.txt").write_text("0000 base\n", encoding="utf-8")
        tests = tree / "tests" / "test_shared.py"
        tests.parent.mkdir(parents=True)
        tests.write_text("# frozen\n", encoding="utf-8")
        baseline = snapshot_tree(tree)
        log.emit(
            "merge",
            principal="harness",
            detail={"kind": "dry-test-overlay", "sides": list(test_sides)},
        )
        return PreparedTree(tree, source_base, baseline, {})

    def _commit_attempt(self, attempt: Attempt, *, label: str, message: str) -> str:
        del label, message
        value = self.scratch._commit("source")
        attempt.source_commit = value
        return value

    def _integration_validate(
        self, *, commit: str, root: Path, name: str, log: object
    ) -> dict[str, object]:
        del commit, root, name
        draw = self._active_draw_id or ""
        counts = getattr(self, "_dry_integration_counts", {})
        count = counts.get(draw, 0) + 1
        counts[draw] = count
        self._dry_integration_counts = counts
        arm = int(draw.rsplit("-a", 1)[1].split("-", 1)[0])
        correct = not (arm == 6 or (arm == 3 and count < 3))
        log.emit(
            "validate",
            principal="harness",
            detail={"kind": "dry-integration", "correct": correct, "count": count},
        )
        result = {
            "correct": correct,
            "buildability": {
                "oracle": "dry-build-v1",
                "scope": "dry",
                "returncode": 0,
                "timed_out": False,
                "launch_error": None,
                "stdout_sha256": "0" * 64,
                "stderr_sha256": "0" * 64,
                "buildable": True,
                "limitation": "dry",
                "actual_seconds": time.monotonic(),
            },
        }
        self._last_integration_result = result
        return result


def _site(root: Path) -> Site:
    manifest = root / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    patch = root / "unused.patch"
    patch.write_text("", encoding="utf-8")
    digest = sha256_bytes(b"")
    sides = {
        label: Side(
            label=label,
            source_name=label,
            parent=label * 40,
            intent_subject=f"repair {label}",
            intent_body="",
            source_patch=patch,
            source_patch_sha256=digest,
            test_patch=patch,
            test_patch_sha256=digest,
            source_paths=("shared.txt",),
            test_paths=("tests/test_shared.py",),
            focal_targets=("tests/test_shared.py",),
        )
        for label in ("A", "B")
    }
    return Site(
        repo="dry/repo",
        repo_slug="dry__repo",
        merge="1" * 40,
        base_commit="2" * 40,
        base_tree="3" * 40,
        answer_commit="4" * 40,
        answer_tree="5" * 40,
        stratum="byte-intersecting",
        mined_class="overlap",
        strict_overlap_paths=("shared.txt",),
        corpus_line=1,
        joint_status="JOINT_GREEN",
        mirror=root,
        manifest=manifest,
        sides=sides,
    )


class ProductionSchedulerDryRunTests(unittest.TestCase):
    def test_all_six_arms_are_wired_without_process_or_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = _Controller()
            scratch = _Scratch(root / "scratch")
            scheduler = _DryScheduler(
                project_root=root,
                site=_site(root),
                scratch=scratch,  # type: ignore[arg-type]
                validator=_Validator(),  # type: ignore[arg-type]
                runner_factory=lambda side, _slot: _MockRunner(side, controller),
                full_retry_safety_cap=6,
            )
            results: dict[int, dict[str, object]] = {}
            # Any accidental process path makes the test fail immediately.
            with mock.patch(
                "subprocess.Popen", side_effect=AssertionError("process call forbidden")
            ):
                for arm in range(1, 7):
                    results[arm] = scheduler.run_draw(
                        arm=arm,
                        repeat=1,
                        root=root / "draws" / f"arm-{arm}",
                        run_id="dry-run",
                    )

            arm2_draw = str(results[2]["draw_id"])
            self.assertEqual(controller.max_active[arm2_draw], 2)
            self.assertEqual(results[2]["shared_write_attribution"], "pair-only")
            self.assertEqual(len(controller.declaration_calls), 4)
            self.assertEqual(results[4]["schedule"], "parallel-shared")
            self.assertEqual(results[5]["schedule"], "parallel-isolated")
            self.assertEqual(results[5]["integration_retries"], 0)
            self.assertEqual(len(results[3]["retry_instance_ids"]), 2)
            self.assertTrue(results[3]["integration_correct"])
            self.assertGreater(results[3]["wasted_compute_seconds"], 0)
            self.assertEqual(results[6]["escalation_count"], 1)
            self.assertEqual(results[6]["escalation"]["side_switches"], 3)
            self.assertFalse(results[6]["integration_correct"])

            arm6_events = [
                json.loads(line)
                for line in (root / "draws" / "arm-6" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertTrue(any(row["op"] == "poll" for row in arm6_events))
            escalations = [row for row in arm6_events if row["op"] == "escalate"]
            self.assertEqual(escalations[-1]["detail"]["kind"], "N=3-region-alternation")
            self.assertTrue(escalations[-1]["detail"]["actual_write_claims_only"])
            arm2_events = [
                json.loads(line)
                for line in (root / "draws" / "arm-2" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            pair_write = [
                row
                for row in arm2_events
                if row["op"] == "write-set"
                and row["principal"].get("scope") == "shared-pair"
            ]
            self.assertEqual(len(pair_write), 1)
            self.assertFalse(pair_write[0]["detail"]["start_barrier"])
            self.assertNotIn("actual_seconds", results[1]["buildability"])

    def test_timeout_retry_then_slot_redraw_retains_and_accounts_every_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = _Controller()
            scheduler = _DryScheduler(
                project_root=root,
                site=_site(root),
                scratch=_Scratch(root / "scratch"),  # type: ignore[arg-type]
                validator=_Validator(),  # type: ignore[arg-type]
                runner_factory=lambda side, slot: _TimeoutThenRedrawRunner(
                    side, slot, controller
                ),
            )
            with mock.patch(
                "subprocess.Popen", side_effect=AssertionError("process call forbidden")
            ):
                metrics = scheduler.run_draw(
                    arm=1,
                    repeat=1,
                    root=root / "draw",
                    run_id="dry-fairness",
                )

            side_b = [row for row in metrics["attempts"] if row["side"] == "B"]
            self.assertEqual(len(side_b), 3)
            self.assertEqual([row["timeout_retry"] for row in side_b], [0, 1, 0])
            self.assertEqual([row["slot_redraw_index"] for row in side_b], [0, 0, 1])
            self.assertEqual([row["excluded"] for row in side_b], [True, True, False])
            expected_agent_seconds = sum(
                float(row["logical_seconds"]) for row in metrics["attempts"]
            )
            self.assertAlmostEqual(metrics["agent_seconds"], expected_agent_seconds)
            timeout_seconds = sum(
                float(row["logical_seconds"]) for row in side_b if row["excluded"]
            )
            self.assertAlmostEqual(
                metrics["timeout_excluded_compute_seconds"], timeout_seconds
            )
            self.assertAlmostEqual(metrics["wasted_compute_seconds"], timeout_seconds)
            self.assertEqual(len(metrics["wasted_instance_ids"]), 2)
            events = [
                json.loads(line)
                for line in (root / "draw" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            retry_kinds = [
                row["detail"]["kind"] for row in events if row["op"] == "retry"
            ]
            self.assertEqual(retry_kinds, ["timeout-retry", "slot-redraw"])

    def test_shared_timeout_discards_contaminated_pairs_and_redraws_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = _Controller()
            scheduler = _DryScheduler(
                project_root=root,
                site=_site(root),
                scratch=_Scratch(root / "scratch"),  # type: ignore[arg-type]
                validator=_Validator(),  # type: ignore[arg-type]
                runner_factory=lambda side, slot: _TimeoutThenRedrawRunner(
                    side, slot, controller
                ),
            )
            with mock.patch(
                "subprocess.Popen", side_effect=AssertionError("process call forbidden")
            ):
                metrics = scheduler.run_draw(
                    arm=2,
                    repeat=1,
                    root=root / "draw",
                    run_id="dry-shared-fairness",
                )

            self.assertEqual(len(metrics["attempts"]), 6)
            side_b = [row for row in metrics["attempts"] if row["side"] == "B"]
            self.assertEqual(
                [row["slot_redraw_index"] for row in side_b], [0, 0, 1]
            )
            self.assertEqual(
                [row["excluded"] for row in side_b], [True, True, False]
            )
            discarded = metrics["shared_contaminated_cycle_instance_ids"]
            self.assertEqual(len(discarded), 4)
            self.assertEqual(set(discarded), set(metrics["wasted_instance_ids"]))
            self.assertTrue(metrics["integration_correct"])
            self.assertEqual(len(metrics["accepted_completion_instance_ids"]), 2)
            events = [
                json.loads(line)
                for line in (root / "draw" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            retry_kinds = [
                row["detail"]["kind"] for row in events if row["op"] == "retry"
            ]
            self.assertEqual(
                retry_kinds,
                [
                    "shared-peer-replay-after-contaminated-cycle",
                    "timeout-retry",
                    "shared-peer-replay-after-contaminated-cycle",
                    "slot-redraw",
                ],
            )

    def test_serial_arm4_violation_has_counterfactual_refusal_cost(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = _Controller()
            scheduler = _DryScheduler(
                project_root=root,
                site=_site(root),
                scratch=_Scratch(root / "scratch"),  # type: ignore[arg-type]
                validator=_Validator(),  # type: ignore[arg-type]
                runner_factory=lambda side, _slot: _OverlappingUnderdeclareRunner(
                    side, controller
                ),
            )
            with mock.patch(
                "subprocess.Popen", side_effect=AssertionError("process call forbidden")
            ):
                metrics = scheduler.run_draw(
                    arm=4,
                    repeat=1,
                    root=root / "draw",
                    run_id="dry-arm4-serial",
                )
            self.assertEqual(metrics["schedule"], "serialize")
            self.assertEqual(metrics["declaration_violations"], 2)
            task_seconds = sum(
                float(row["logical_seconds"]) for row in metrics["attempts"]
            )
            self.assertAlmostEqual(
                metrics["counterfactual_refusal_seconds"], task_seconds
            )


if __name__ == "__main__":
    unittest.main()
