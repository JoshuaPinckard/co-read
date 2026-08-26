from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from instruments.arms.shim.normalize import normalize_rows, verify_event_log
from instruments.arms.shim.util import (
    EventLog,
    LogicalClock,
    canonical_json,
    sha256_bytes,
)


class NormalizeTests(unittest.TestCase):
    def test_timestamp_normalization_recomputes_chain(self) -> None:
        base = {
            "schema_version": "arms-event/v1",
            "sequence": 0,
            "run_id": "r",
            "draw_id": "d",
            "site": {},
            "arm": {},
            "stratum": "s",
            "principal": "h",
            "subject": {},
            "op": "complete",
            "paths": [],
            "timestamp_utc": "2026-01-01T00:00:00Z",
            "monotonic_ns": 999,
            "detail": {},
            "previous_event_sha256": "0" * 64,
            "event_sha256": "discarded",
        }
        other = dict(base)
        other["timestamp_utc"] = "2030-01-01T00:00:00Z"
        other["monotonic_ns"] = 1
        self.assertEqual(normalize_rows([base]), normalize_rows([other]))

    def test_event_log_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with EventLog(
                path,
                run_id="r",
                draw_id="d",
                site={},
                arm={},
                stratum="s",
                clock=LogicalClock(),
            ) as log:
                log.emit("launch", principal="h")
                log.emit("complete", principal="h")
            result = verify_event_log(path)
            self.assertTrue(result["pass"], result)
            self.assertEqual(result["events"], 2)

    def test_rehashed_semantically_invalid_event_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with EventLog(
                path,
                run_id="r",
                draw_id="d",
                site={},
                arm={},
                stratum="s",
                clock=LogicalClock(),
            ) as log:
                log.emit("complete", principal="h")
            row = json.loads(path.read_text(encoding="utf-8"))
            row["schema_version"] = "invented-schema"
            row.pop("event_sha256")
            row["event_sha256"] = sha256_bytes(canonical_json(row))
            path.write_bytes(canonical_json(row) + b"\n")
            result = verify_event_log(path)
            self.assertFalse(result["pass"], result)
            self.assertTrue(
                any("schema_version" in error for error in result["errors"]), result
            )


if __name__ == "__main__":
    unittest.main()
