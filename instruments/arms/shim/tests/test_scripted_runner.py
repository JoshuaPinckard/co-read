from __future__ import annotations

import threading
import unittest

from instruments.arms.shim.scripted_runner import ScriptedInterleavingControl


class ScriptedInterleavingControlTests(unittest.TestCase):
    def test_completion_waits_for_every_ordered_pair_poll(self) -> None:
        control = ScriptedInterleavingControl(timeout_seconds=2.0)
        events: list[str] = []
        b_last_entered = threading.Event()
        release_b_last = threading.Event()
        a_completed = threading.Event()
        errors: list[BaseException] = []

        def emit(side: str, index: int) -> None:
            if side == "B" and index == 1:
                b_last_entered.set()
                if not release_b_last.wait(timeout=1.0):
                    raise AssertionError("test did not release B's final poll")
            events.append(f"{side}{index}")

        def worker_a() -> None:
            try:
                for index in range(2):
                    control.ordered_poll(
                        draw_id="draw",
                        side="A",
                        poll_index=index,
                        emit=lambda index=index: emit("A", index),
                    )
                control.wait_for_polls_complete(
                    draw_id="draw",
                    expected_poll_events=4,
                )
                events.append("write-A")
                a_completed.set()
            except BaseException as error:  # pragma: no cover - assertion aid
                errors.append(error)

        def worker_b() -> None:
            try:
                for index in range(2):
                    control.ordered_poll(
                        draw_id="draw",
                        side="B",
                        poll_index=index,
                        emit=lambda index=index: emit("B", index),
                    )
            except BaseException as error:  # pragma: no cover - assertion aid
                errors.append(error)

        a = threading.Thread(target=worker_a)
        b = threading.Thread(target=worker_b)
        a.start()
        b.start()
        self.assertTrue(b_last_entered.wait(timeout=1.0))
        self.assertFalse(a_completed.is_set())
        release_b_last.set()
        a.join(timeout=1.0)
        b.join(timeout=1.0)

        self.assertFalse(a.is_alive())
        self.assertFalse(b.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(events, ["A0", "B0", "A1", "B1", "write-A"])


if __name__ == "__main__":
    unittest.main()
