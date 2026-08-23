from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from instruments.posture import pilot
from instruments.posture import shim


def test_pilot_seals_every_imported_local_apparatus_module() -> None:
    pinned = {path.as_posix() for path in pilot.APPARATUS_FILES}
    assert {
        "instruments/posture/analyze.py",
        "instruments/posture/gate_repository.py",
        "instruments/posture/pilot.py",
        "instruments/posture/prepare.py",
        "instruments/posture/radius.py",
        "instruments/posture/shim.py",
        "instruments/posture/task_builder.py",
        "instruments/replay/common.py",
        "instruments/replay/extract.py",
        "instruments/replay/replay.py",
    }.issubset(pinned)


def event(
    event_id: int,
    agent: str,
    event_type: str,
    details: dict[str, object],
) -> dict[str, object]:
    return {
        "id": event_id,
        "agent_id": agent,
        "event_type": event_type,
        "details": details,
    }


def test_causal_rework_requires_read_write_reread_write_chain() -> None:
    events = [
        event(1, "a", "read", {"kind": "file", "path": "x.py", "sha256": "old"}),
        event(2, "b", "write", {"path": "x.py", "changed": True}),
        event(3, "a", "read", {"kind": "file", "path": "x.py", "sha256": "new"}),
        event(4, "a", "write", {"path": "x.py", "changed": True}),
    ]
    result = pilot.causal_rework(events, "a")  # type: ignore[arg-type]
    assert result["measurement_available"] is True
    assert result["verified_rework_operations"] == 1

    without_reread = pilot.causal_rework([events[0], events[1], events[3]], "a")  # type: ignore[arg-type]
    assert without_reread["verified_rework_operations"] == 0


def test_causal_rework_is_unavailable_without_read_hashes() -> None:
    result = pilot.causal_rework(
        [event(1, "a", "read", {"kind": "file", "path": "x.py"})],  # type: ignore[list-item]
        "a",
    )
    assert result["measurement_available"] is False
    assert result["verified_rework_operations"] is None


def test_isolate_does_not_count_same_relative_path_in_other_worktree_as_rework() -> None:
    rows = [
        event(1, "a", "read", {"kind": "file", "path": "x.py", "sha256": "old"}),
        event(2, "b", "write", {"path": "x.py", "changed": True}),
        event(3, "a", "read", {"kind": "file", "path": "x.py", "sha256": "new"}),
        event(4, "a", "write", {"path": "x.py", "changed": True}),
    ]
    for row in rows:
        row["arm"] = "isolate"
    result = pilot.summarize_task(
        {"task_id": "a"},
        {"model_finished": True},
        [row for row in rows if row["agent_id"] == "a"],  # type: ignore[arg-type]
        rows,  # type: ignore[arg-type]
        {"merged": True},
        {"verified": True, "present": True},
        {"verified": True, "correct": True},
    )
    assert result["rework"]["measurement_available"] is True
    assert result["rework"]["verified_rework_operations"] == 0
    assert result["rework"]["verified_rework_seconds"] == 0.0


def test_fairness_finished_response_does_not_require_zero_process_exit() -> None:
    assert pilot.model_finished_response(
        timed_out=False, turn_completed=False, final_present=True
    )


def test_successful_file_change_requires_same_completed_tool_id_and_post_response(
    tmp_path: Path,
) -> None:
    codex_events = tmp_path / "events.jsonl"
    codex_events.write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "tool-1", "type": "file_change", "status": "completed"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    rows = [
        event(1, "a", "write_attempt", {"tool_use_id": "tool-1"}),
        event(
            2,
            "a",
            "write",
            {
                "tool_use_id": "tool-1",
                "path": "x.py",
                "changed": True,
                "tool_response": {"ok": True},
            },
        ),
    ]
    evidence = pilot.successful_file_change_evidence(rows, codex_events)  # type: ignore[arg-type]
    assert evidence["has_successful_file_change"] is True
    rows[1]["details"]["tool_use_id"] = "other"  # type: ignore[index]
    mismatch = pilot.successful_file_change_evidence(rows, codex_events)  # type: ignore[arg-type]
    assert mismatch["has_successful_file_change"] is False
    assert not pilot.model_finished_response(
        timed_out=True, turn_completed=True, final_present=True
    )


def test_blocked_then_completed_requires_wait_release_and_finish_not_correctness() -> None:
    events = [
        event(1, "a", "block", {}),
        event(2, "a", "release", {"kind": "blocking_wait", "wait_seconds": 2.5}),
        event(3, "a", "claim", {"collision_exposed": True}),
        event(4, "a", "write", {"path": "x.py", "changed": True}),
    ]
    model = {"model_finished": True}
    wrong = pilot.summarize_task(
        {"task_id": "a"},
        model,
        events,  # type: ignore[arg-type]
        events,  # type: ignore[arg-type]
        None,
        {"verified": True, "present": True},
        {"verified": True, "correct": False, "wrong_verified": True},
    )
    assert wrong["outcome"] == "landed_and_wrong"
    assert wrong["blocked_then_finished_response"] is True
    assert wrong["blocked_then_completed"] is True
    assert wrong["blocked_then_correct"] is False
    assert wrong["collision_rate"] == 1.0

    correct = pilot.summarize_task(
        {"task_id": "a"},
        model,
        events,  # type: ignore[arg-type]
        events,  # type: ignore[arg-type]
        None,
        {"verified": True, "present": True},
        {"verified": True, "correct": True},
    )
    assert correct["blocked_then_completed"] is True
    assert correct["blocked_then_correct"] is True


def test_unverified_presence_is_not_mislabeled_abandoned() -> None:
    result = pilot.summarize_task(
        {"task_id": "a"},
        {"model_finished": True},
        [event(1, "a", "write", {"path": "x.py", "changed": True})],  # type: ignore[list-item]
        [event(1, "a", "write", {"path": "x.py", "changed": True})],  # type: ignore[list-item]
        None,
        {"verified": False, "present": None},
        None,
    )
    assert result["outcome"] == "unverified"


def test_schedule_preregisters_launch_permutations_and_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pilot, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pilot, "pinned_artifacts", lambda *_: {"TASKS.json": "abc"})
    monkeypatch.setattr(pilot, "runtime_versions", lambda *_: {"codex_version": "fixed"})
    monkeypatch.setattr(pilot, "verify_live_smoke", lambda *_: {"contract": "fixed"})
    tasks_path = tmp_path / "TASKS.json"
    tasks = {
        "pilot": {
            "random_seed": "0x123",
            "arms": ["advisory", "blocking", "isolate"],
            "draws_per_cell": 2,
        },
        "bundles": [
            {
                "bundle_id": "b",
                "factor": "overlap",
                "tasks": [{"task_id": name} for name in ("a", "b", "c", "d", "e")],
            }
        ],
    }
    tasks_path.write_text(json.dumps(tasks), encoding="utf-8")
    schedule = pilot.make_schedule(tasks_path, tmp_path / "SCHEDULE.json")
    expected = {"a", "b", "c", "d", "e"}
    assert schedule["draw_count"] == 6
    assert schedule["apparatus_fingerprint"]
    assert all(set(draw["launch_order"]) == expected for draw in schedule["draws"])


def test_completed_attempt_refuses_stale_apparatus(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt-001"
    attempt.mkdir()
    (attempt / "summary.json").write_text(
        json.dumps({"excluded": False, "apparatus_fingerprint": "old"}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="stale completed attempt"):
        pilot.completed_attempt(tmp_path, "new")


def test_schedule_verification_rejects_a_mutated_draw_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pilot, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pilot, "pinned_artifacts", lambda *_: {"TASKS.json": "abc"})
    monkeypatch.setattr(pilot, "runtime_versions", lambda *_: {"codex_version": "fixed"})
    monkeypatch.setattr(pilot, "verify_live_smoke", lambda *_: {"contract": "fixed"})
    tasks_path = tmp_path / "TASKS.json"
    tasks = {
        "pilot": {
            "random_seed": "0x123",
            "arms": ["advisory", "blocking", "isolate"],
            "draws_per_cell": 1,
        },
        "bundles": [
            {
                "bundle_id": "b",
                "factor": "overlap",
                "tasks": [{"task_id": name} for name in ("a", "b", "c", "d")],
            }
        ],
    }
    tasks_path.write_text(json.dumps(tasks), encoding="utf-8")
    schedule = pilot.make_schedule(tasks_path, tmp_path / "SCHEDULE.json")
    schedule["draws"][0]["arm"] = "blocking" if schedule["draws"][0]["arm"] != "blocking" else "advisory"
    with pytest.raises(RuntimeError, match="seeded canonical plan"):
        pilot.verify_schedule_apparatus(tasks_path, tasks, schedule)


def test_live_smoke_verifier_rejects_a_legacy_partial_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks_path = tmp_path / "TASKS.json"
    tasks_path.write_text("{}", encoding="utf-8")
    summary_path = tmp_path / "summary.json"
    contract = {
        "session_start": True,
        "allowed_read": True,
        "allowed_claim": True,
        "denied_direct_shell": True,
        "changed_apply_patch": True,
        "claim_release": True,
        "finished_model": True,
        "zero_apparatus_invalid": True,
    }
    summary_path.write_text(
        json.dumps(
            {
                "apparatus_fingerprint": pilot.apparatus_fingerprint({}, {}),
                "tasks_sha256": pilot.sha256_file(tasks_path),
                "contract": contract,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pilot, "DEFAULT_SMOKE_SUMMARY", summary_path)
    with pytest.raises(RuntimeError, match="contract is incomplete"):
        pilot.verify_live_smoke(tasks_path, {}, {}, {})


def test_collision_metrics_count_unique_pairs_not_only_exposed_claims() -> None:
    events = [
        event(1, "a", "claim", {"collision_exposed": False, "conflicting_agents_seen": []}),
        event(2, "b", "claim", {"collision_exposed": True, "conflicting_agents_seen": ["a"]}),
        event(3, "c", "claim", {"collision_exposed": True, "conflicting_agents_seen": ["a", "b"]}),
    ]
    designed = {
        "pairs": [
            {"left": left, "right": right, "designed_collision_opportunity": True}
            for left, right in (("a", "b"), ("a", "c"), ("b", "c"))
        ]
    }
    result = pilot.collision_metrics(events, ["a", "b", "c"], designed)  # type: ignore[arg-type]
    assert result["exposed_claims"] == 2
    assert result["unique_agent_pair_count"] == 3
    assert result["designed_pair_realisation_rate"] == 1.0


def test_hidden_suite_classification_requires_collection_and_before_green_evidence() -> None:
    reference_cases = [
        {"classname": "tests.test_x", "name": "test_a", "outcome": "passed", "detail": None},
        {"classname": "tests.test_x", "name": "test_b", "outcome": "passed", "detail": None},
    ]
    bundle = {
        "construction": {
            "baseline_determinism": {
                "runs": [{"normalized": {"cases": reference_cases}}]
            },
            "oracle_sequence_checks": [
                {"label": "all", "pre_run_test": {"normalized": {"cases": reference_cases}}}
            ]
        },
        "tasks": [
            {
                "task_id": "a",
                "expected_focal_cases": [
                    {"classname": "tests.test_x", "name": "test_a"}
                ],
            },
            {
                "task_id": "b",
                "expected_focal_cases": [
                    {"classname": "tests.test_x", "name": "test_b"}
                ],
            },
        ],
    }
    green = pilot.classify_hidden_suite(
        bundle,
        {"exit_code": 0, "timed_out": False, "normalized": {"cases": reference_cases}},
    )
    assert green["correct"] is True
    assert green["wrong_verified"] is False

    known_failure_cases = [dict(reference_cases[0], outcome="failure"), reference_cases[1]]
    known_failure = pilot.classify_hidden_suite(
        bundle,
        {"exit_code": 1, "timed_out": False, "normalized": {"cases": known_failure_cases}},
    )
    assert known_failure["correct"] is False
    assert known_failure["wrong_verified"] is True

    unknown_failure = pilot.classify_hidden_suite(
        bundle,
        {
            "exit_code": 1,
            "timed_out": False,
            "normalized": {
                "cases": [
                    *reference_cases,
                    {"classname": "new", "name": "test_new", "outcome": "error", "detail": None},
                ]
            },
        },
    )
    assert unknown_failure["wrong_verified"] is False

    missing_case = pilot.classify_hidden_suite(
        bundle,
        {"exit_code": 0, "timed_out": False, "normalized": {"cases": reference_cases[:1]}},
    )
    assert missing_case["correct"] is False
    assert missing_case["missing_reference_cases"]

    skipped_cases = [dict(reference_cases[0], outcome="skipped"), reference_cases[1]]
    skipped_hidden = pilot.classify_hidden_suite(
        bundle,
        {"exit_code": 0, "timed_out": False, "normalized": {"cases": skipped_cases}},
    )
    assert skipped_hidden["verified"] is False
    assert skipped_hidden["correct"] is False
    assert skipped_hidden["reference_passed_to_nonfailure_outcome_drift"]

    skipped_visible = pilot.classify_visible_regression(
        bundle,
        {"exit_code": 0, "timed_out": False, "normalized": {"cases": skipped_cases}},
    )
    assert skipped_visible["verified"] is False
    assert skipped_visible["correct"] is False
    assert skipped_visible["reference_passed_to_nonfailure_outcome_drift"]


def test_project_path_resolves_relative_manifest_values_from_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "env" / "python.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"placeholder")
    monkeypatch.setattr(pilot, "PROJECT_ROOT", tmp_path)
    assert pilot.project_path("env/python.exe") == executable.resolve()


def test_release_agent_uses_the_actual_lock_free_database_schema(tmp_path: Path) -> None:
    database = tmp_path / "coordination.sqlite3"
    shim.initialize_database(database)
    with shim.database_connection(database) as connection:
        connection.execute(
            """
            INSERT INTO claims
                (draw_id, agent_id, arm, claims_json, radius_json,
                 acquired_monotonic_ns, acquired_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "draw",
                "agent",
                "advisory",
                json.dumps([{"path": "x.py", "start": 0, "end": 10}]),
                "[]",
                time.monotonic_ns(),
                "2026-08-23T00:00:00+00:00",
            ),
        )

    pilot.release_agent(database, "draw", "advisory", "agent", "model_finished")

    with shim.database_connection(database) as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) FROM claims WHERE draw_id = ? AND agent_id = ?",
            ("draw", "agent"),
        ).fetchone()[0]
        releases = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'release'"
        ).fetchone()[0]
    assert remaining == 0
    assert releases == 1
