"""Unit tests for the posture claim and hook shim."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from instruments.posture import shim


class ShimFixture:
    def __init__(self, root: Path, arm: str, agent_id: str) -> None:
        self.root = root
        self.worktree = root / "worktree"
        self.worktree.mkdir(exist_ok=True)
        (self.worktree / "a.py").write_text("alpha\n", encoding="utf-8")
        (self.worktree / "b.py").write_text("beta\n", encoding="utf-8")
        self.database = root / "events.sqlite3"
        self.python_compat = root / "python39-compat"
        self.python_compat.mkdir(exist_ok=True)
        (self.python_compat / "sitecustomize.py").write_text(
            "# test compatibility layer\n", encoding="utf-8"
        )
        self.radius = root / "radius.json"
        self.radius.write_text(
            json.dumps(
                {
                    "top_k": 3,
                    "threshold": 0.1,
                    "files": {
                        "a.py": [{"path": "b.py", "score": 0.5}],
                        "b.py": [],
                    },
                    "provenance": {"test": True},
                }
            ),
            encoding="utf-8",
        )
        self.arm = arm
        self.agent_id = agent_id

    def environment(self) -> dict[str, str]:
        return {
            "POSTURE_DB": str(self.database),
            "POSTURE_DRAW_ID": "draw-1",
            "POSTURE_AGENT_ID": self.agent_id,
            "POSTURE_ARM": self.arm,
            "POSTURE_WORKSPACE_KEY": "shared",
            "POSTURE_WORKTREE": str(self.worktree),
            "POSTURE_RADIUS": str(self.radius),
            "POSTURE_TEST_PYTHON": sys.executable,
            "POSTURE_TEST_TEMP_ROOT": str(self.root),
            "POSTURE_PYTHON_COMPAT": str(self.python_compat),
        }

    @contextlib.contextmanager
    def context(self):
        with mock.patch.dict(os.environ, self.environment(), clear=False):
            yield shim.Context()


class IntervalTests(unittest.TestCase):
    def test_half_open_adjacency_is_not_a_conflict(self) -> None:
        self.assertFalse(
            shim.intervals_intersect(
                {"path": "a", "start": 0, "end": 5},
                {"path": "a", "start": 5, "end": 10},
            )
        )
        self.assertTrue(
            shim.intervals_intersect(
                {"path": "a", "start": 0, "end": 6},
                {"path": "a", "start": 5, "end": 10},
            )
        )

    def test_invalid_empty_claim_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            shim.validate_interval(2, 2)


class ClaimTests(unittest.TestCase):
    def test_wildcard_claim_is_unbounded_for_task_lifetime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ShimFixture(Path(directory), "advisory", "agent")
            with fixture.context() as context:
                claims = shim.parse_claim_specs(context, ["a.py:*"])
            self.assertEqual(claims, [{"path": "a.py", "start": 0, "end": shim.MAX_BYTE}])

    def test_advisory_reports_but_does_not_withhold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = ShimFixture(root, "advisory", "left")
            right = ShimFixture(root, "advisory", "right")
            a_size = (left.worktree / "a.py").stat().st_size
            b_size = (left.worktree / "b.py").stat().st_size
            with left.context() as left_context:
                shim.acquire_claims(left_context, [{"path": "a.py", "start": 0, "end": a_size}])
            with right.context() as right_context:
                result = shim.acquire_claims(right_context, [{"path": "b.py", "start": 0, "end": b_size}])
            self.assertTrue(result["collision_exposed"])
            self.assertLess(result["wait_seconds"], 0.25)

    def test_claim_cannot_be_replaced_or_narrowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ShimFixture(Path(directory), "advisory", "agent")
            with fixture.context() as context:
                shim.acquire_claims(context, [{"path": "a.py", "start": 0, "end": shim.MAX_BYTE}])
                with self.assertRaisesRegex(ValueError, "task-lifetime"):
                    shim.acquire_claims(context, [{"path": "b.py", "start": 0, "end": shim.MAX_BYTE}])

    def test_blocking_waits_until_live_radius_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = ShimFixture(root, "blocking", "left")
            right = ShimFixture(root, "blocking", "right")
            a_size = (left.worktree / "a.py").stat().st_size
            b_size = (left.worktree / "b.py").stat().st_size
            with left.context() as left_context:
                shim.acquire_claims(left_context, [{"path": "a.py", "start": 0, "end": a_size}])

            result: dict[str, object] = {}

            def acquire_right() -> None:
                with right.context() as right_context:
                    result.update(
                        shim.acquire_claims(
                            right_context,
                            [{"path": "b.py", "start": 0, "end": b_size}],
                        )
                    )

            thread = threading.Thread(target=acquire_right)
            thread.start()
            time.sleep(0.25)
            self.assertTrue(thread.is_alive())
            with left.context() as left_context:
                shim.release_claims(left_context, reason="test")
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertTrue(result["collision_exposed"])
            self.assertGreaterEqual(float(result["wait_seconds"]), 0.2)


class HookTests(unittest.TestCase):
    def test_write_requires_a_whole_file_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ShimFixture(Path(directory), "advisory", "agent")
            with fixture.context() as context:
                payload = {
                    "tool_name": "apply_patch",
                    "tool_use_id": "tool-1",
                    "tool_input": {
                        "command": "*** Begin Patch\n*** Update File: a.py\n@@\n-alpha\n+omega\n*** End Patch"
                    },
                }
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    shim.hook_pre(context, payload)
                decision = json.loads(output.getvalue())
                self.assertEqual(
                    decision["hookSpecificOutput"]["permissionDecision"], "deny"
                )

                shim.acquire_claims(context, [{"path": "a.py", "start": 0, "end": shim.MAX_BYTE}])
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    shim.hook_pre(context, payload)
                decision = json.loads(output.getvalue())
                self.assertEqual(
                    decision["hookSpecificOutput"]["permissionDecision"], "allow"
                )

    def test_post_write_records_exact_before_after_patch_without_a_mutex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ShimFixture(Path(directory), "advisory", "agent")
            with fixture.context() as context:
                shim.acquire_claims(context, [{"path": "a.py", "start": 0, "end": shim.MAX_BYTE}])
                payload = {
                    "tool_name": "apply_patch",
                    "tool_use_id": "tool-2",
                    "tool_input": {
                        "command": "*** Begin Patch\n*** Update File: a.py\n@@\n-alpha\n+omega\n*** End Patch"
                    },
                }
                with contextlib.redirect_stdout(io.StringIO()):
                    shim.hook_pre(context, payload)
                (fixture.worktree / "a.py").write_text("omega\n", encoding="utf-8")
                shim.hook_post(context, {**payload, "tool_response": {"ok": True}})

            connection = sqlite3.connect(fixture.database)
            try:
                connection.row_factory = sqlite3.Row
                write = connection.execute(
                    "SELECT details_json FROM events WHERE event_type = 'write'"
                ).fetchone()
                mutex_events = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type LIKE 'write_mutex%'"
                ).fetchone()[0]
            finally:
                connection.close()
            details = json.loads(write["details_json"])
            self.assertTrue(details["changed"])
            self.assertIn("-alpha", details["patch"])
            self.assertIn("+omega", details["patch"])
            self.assertEqual(mutex_events, 0)

    def test_move_requires_claims_for_source_and_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ShimFixture(Path(directory), "advisory", "agent")
            with fixture.context() as context:
                shim.acquire_claims(context, [{"path": "a.py", "start": 0, "end": shim.MAX_BYTE}])
                payload = {
                    "tool_name": "apply_patch",
                    "tool_use_id": "move-1",
                    "tool_input": {
                        "command": "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n-alpha\n+alpha\n*** End Patch"
                    },
                }
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    shim.hook_pre(context, payload)
            self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_only_the_measured_wrapper_is_allowed_through_bash(self) -> None:
        self.assertTrue(shim.safe_wrapper_command("python .posture/agent_tool.py read a.py"))
        self.assertTrue(
            shim.safe_wrapper_command(
                f"{Path(sys.executable).resolve()} .posture/agent_tool.py read a.py",
                Path(sys.executable),
            )
        )
        self.assertTrue(
            shim.safe_wrapper_command(
                f'"{Path(sys.executable).resolve()}" .posture/agent_tool.py read a.py',
                Path(sys.executable),
            )
        )
        self.assertFalse(
            shim.safe_wrapper_command("python .posture/agent_tool.py read a.py", Path(sys.executable))
        )
        self.assertFalse(shim.safe_wrapper_command("Get-Content a.py"))
        bypasses = [
            "python .posture/agent_tool.py read a.py; Remove-Item a.py",
            "python .posture/agent_tool.py list $(Set-Content a.py bad)",
            "python .posture/agent_tool.py list & Set-Content a.py bad",
            "Set-Content a.py bad # python .posture/agent_tool.py list",
            "python .posture/agent_tool.py read a.py > stolen.txt",
            "python .posture/agent_tool.py read a.py`nSet-Content a.py bad",
        ]
        for command in bypasses:
            with self.subTest(command=command):
                self.assertFalse(shim.safe_wrapper_command(command))

    def test_session_start_records_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ShimFixture(Path(directory), "advisory", "agent")
            payload = {
                "session_id": "session-1",
                "cwd": str(fixture.worktree),
                "model": "gpt-5.6-sol",
                "permission_mode": "never",
                "source": "startup",
            }
            output = io.StringIO()
            with fixture.context() as context, contextlib.redirect_stdout(output):
                shim.hook_session(context, payload)
            self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["hookEventName"], "SessionStart")
            connection = sqlite3.connect(fixture.database)
            try:
                count = connection.execute("SELECT COUNT(*) FROM events WHERE event_type='session_start'").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)


class TestCommandValidationTests(unittest.TestCase):
    def test_only_repository_test_nodeids_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ShimFixture(Path(directory), "advisory", "agent")
            tests = fixture.worktree / "tests"
            tests.mkdir()
            (tests / "test_example.py").write_text("def test_ok(): pass\n", encoding="utf-8")
            self.assertEqual(
                shim.validate_pytest_nodeids(fixture.worktree, ["tests/test_example.py::test_ok"]),
                ["tests/test_example.py::test_ok"],
            )
            for value in ["--junitxml=outside.xml", "../test.py", "src/a.py", "tests/*.py"]:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    shim.validate_pytest_nodeids(fixture.worktree, [value])


if __name__ == "__main__":
    unittest.main()
