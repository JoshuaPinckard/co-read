from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
import instruments.posture.analyze as analyze_module

from instruments.posture.analyze import (
    AnalysisError,
    SUPPORTED_CLEAN_NULL_METRIC,
    analyze,
    analyze_files,
)


ARMS = ["advisory", "blocking", "isolate"]
BUNDLES = ["overlap", "control"]


def design() -> dict:
    return {
        "pilot": {
            "arms": ARMS,
            "draws_per_cell": 1,
            "clean_null_rule": {
                "scope": "accepted overlap-bundle draws pooled across the three arms",
                "metric": SUPPORTED_CLEAN_NULL_METRIC,
                "near_zero_below": 0.1,
                "arm_specific_warning_below": 0.1,
                "claim_uptake_reported_separately": True,
                "decision": "stop below 0.10",
            },
        },
        "bundles": [
            {
                "bundle_id": "overlap",
                "expected_pairs_with_collision": 1,
                "tasks": [{"task_id": "o1"}, {"task_id": "o2"}],
            },
            {
                "bundle_id": "control",
                "expected_pairs_with_collision": 0,
                "tasks": [{"task_id": "c1"}, {"task_id": "c2"}],
            },
        ],
        "interpretations": {
            "signal_limitation": "co-change overlaps reads by 7.8%, below a popularity null",
            "headline_event_unit": "observed integrated bundle landing",
            "bundle_population": "one fixed bundle per condition",
        },
    }


def task_row(
    task_id: str,
    arm: str,
    *,
    outcome: str = "landed_and_correct",
    blocked: bool = False,
) -> dict:
    rework_available = arm == "isolate"
    return {
        "task_id": task_id,
        "outcome": outcome,
        "model": {"model_finished": True},
        "blocked_then_completed": blocked,
        "blocking_wait_seconds": 5.0 if blocked else 0.0,
        "rework": {
            "measurement_available": rework_available,
            "verified_rework_operations": 0 if rework_available else None,
            "verified_rework_seconds": 0.0 if rework_available else None,
            "reason": None if rework_available else "shared attribution unavailable",
        },
    }


def draw(
    arm: str,
    bundle: str,
    *,
    collision_pairs: int,
    minutes: float | None,
    bundle_outcome: str = "landed_and_correct",
    task_wrong: bool = False,
) -> dict:
    prefix = "o" if bundle == "overlap" else "c"
    outcomes = ["landed_and_wrong", "landed_and_correct"] if task_wrong else [
        "landed_and_correct",
        "landed_and_correct",
    ]
    rows = [
        task_row(
            f"{prefix}{index + 1}",
            arm,
            outcome=outcomes[index],
            blocked=arm == "blocking" and index == 0,
        )
        for index in range(2)
    ]
    merge_conflicts = 1 if arm == "isolate" and bundle == "overlap" else 0
    return {
        "draw": {
            "draw_id": f"{bundle}-{arm}",
            "arm": arm,
            "bundle_id": bundle,
        },
        "excluded": False,
        "apparatus": {"valid": True},
        "tasks": rows,
        "metrics": {
            "total_agent_minutes": minutes,
            "bundle_wall_seconds": 60.0,
            "evaluation_wall_seconds": 70.0,
            "agent_execution_wall_seconds": 50.0,
            "blocking_wait_seconds": 5.0 if arm == "blocking" else 0.0,
            "fleet_capacity_seconds": 100.0,
            "combined_idle_seconds": 20.0,
            "fleet_idle_fraction": 0.2,
            "merge_conflicts": merge_conflicts,
            "collisions": {
                "realised_designed_pair_count": collision_pairs,
                "designed_pair_denominator": 1 if bundle == "overlap" else 0,
                "claim_acquisitions": 2,
            },
        },
        "integrated": {"observed_bundle_outcome": bundle_outcome},
    }


def raw_pilot(*, collision_pairs: int = 1, prevention: bool = True) -> dict:
    draws = []
    for arm in ARMS:
        for bundle in BUNDLES:
            advisory_wrong = prevention and arm == "advisory" and bundle == "overlap"
            draws.append(
                draw(
                    arm,
                    bundle,
                    collision_pairs=collision_pairs if bundle == "overlap" else 0,
                    minutes={"advisory": 2.0, "blocking": 3.0, "isolate": 4.0}[arm],
                    bundle_outcome="landed_and_wrong"
                    if advisory_wrong
                    else "landed_and_correct",
                    task_wrong=advisory_wrong,
                )
            )
    excluded_attempt = {
        "draw_id": "overlap-advisory",
        "arm": "advisory",
        "excluded": True,
        "exclusion_reason": "at_least_one_model_never_finished",
        "model_agent_minutes": 1.25,
    }
    return {
        "completed_draws": len(draws),
        "draws": draws,
        "attempt_ledger": {
            "by_arm": {arm: {} for arm in ARMS},
            "attempts": [excluded_attempt],
        },
    }


def test_clean_null_gate_is_first_and_suppresses_exchange() -> None:
    result = analyze(design(), raw_pilot(collision_pairs=0, prevention=True))

    assert next(iter(result)) == "collision_gate"
    assert result["collision_gate"]["verdict"] == "measured_no_contention_clean_null"
    assert result["collision_gate"]["pooled"]["rate"] == 0.0
    assert result["exchange_rate"]["headline_overall"]["computed"] is False
    assert (
        result["exchange_rate"]["headline_overall"]["unavailable_reason"]
        == "collision_gate_or_required_arm_comparability_failed"
    )


def test_complete_analysis_computes_exchange_and_all_required_measures() -> None:
    result = analyze(design(), raw_pilot(collision_pairs=1, prevention=True))

    gate = result["collision_gate"]
    assert gate["pooled"]["rate"] == 1.0
    assert gate["pooled"]["preregistered_designed_pair_opportunities"] == 3
    assert gate["pooled"]["claim_uptake"]["claims_per_task_slot"] == 1.0
    assert gate["blocking_vs_advisory_exchange_allowed"] is True

    completion = result["base_task_completion"]["by_arm"]["advisory"]
    assert completion["task_observations"] == 4
    assert completion["landed_and_correct_count"] == 3
    assert completion["landed_and_correct_rate"] == 0.75

    blocking = result["outcome_table"]["by_arm"]["blocking"]
    assert blocking["task_outcomes"]["blocked_then_completed_count"] == 2
    assert blocking["task_outcomes"]["blocked_then_completed_wait_seconds"]["sum"] == 10.0
    assert blocking["rework"]["measurement_available_count"] == 0
    assert blocking["rework"]["verified_rework_seconds"]["sum"] is None

    isolate = result["outcome_table"]["by_arm"]["isolate"]
    assert isolate["rework"]["measurement_available_count"] == 4
    assert isolate["rework"]["verified_rework_operations"]["sum"] == 0.0
    assert result["posture_axes"]["isolate_merge_conflicts"]["sum"] == 1.0
    assert result["posture_axes"]["scope"] == "preregistered overlap bundles only"
    assert result["posture_axes"]["isolate_relative_to_advisory"] == {
        "additional_agent_minutes": 2.0,
        "difference_in_wrong_task_landings": -1,
    }

    exchange = result["exchange_rate"]["headline_overall"]
    assert exchange["computed"] is True
    assert exchange["additional_blocking_agent_minutes"] == 1.0
    assert exchange["observed_wrong_landing_reduction"] == 1
    assert exchange["agent_minutes_per_observed_wrong_landing_prevented"] == 1.0

    assert result["retry_ledger"]["excluded_attempt_count"] == 1
    assert result["retry_ledger"]["excluded_attempts"][0]["model_agent_minutes"] == 1.25
    assert result["data_quality"]["pilot_completeness"]["complete"] is True


def test_blocking_with_no_wrong_reduction_has_no_exchange_rate() -> None:
    result = analyze(design(), raw_pilot(collision_pairs=1, prevention=False))
    exchange = result["exchange_rate"]["headline_overall"]

    assert exchange["computed"] is False
    assert exchange["observed_wrong_landing_reduction"] == 0
    assert exchange["agent_minutes_per_observed_wrong_landing_prevented"] is None
    assert exchange["verdict"] == (
        "blocking_has_no_defence_at_any_price_on_observed_wrong_landings"
    )


def test_missing_control_metric_remains_missing_but_does_not_enter_exchange() -> None:
    pilot = raw_pilot(collision_pairs=1, prevention=True)
    target = next(
        item
        for item in pilot["draws"]
        if item["draw"]["arm"] == "blocking" and item["draw"]["bundle_id"] == "control"
    )
    target["metrics"]["total_agent_minutes"] = None

    result = analyze(design(), pilot)
    blocking_minutes = result["aggregates"]["by_arm"]["blocking"]["performance"][
        "total_agent_minutes"
    ]
    exchange = result["exchange_rate"]["headline_overall"]
    assert blocking_minutes["missing_count"] == 1
    assert blocking_minutes["complete"] is False
    assert exchange["blocking"]["total_agent_minutes"] == 3.0
    assert exchange["computed"] is True


def test_missing_overlap_metric_blocks_exchange() -> None:
    pilot = raw_pilot(collision_pairs=1, prevention=True)
    target = next(
        item
        for item in pilot["draws"]
        if item["draw"]["arm"] == "blocking" and item["draw"]["bundle_id"] == "overlap"
    )
    target["metrics"]["total_agent_minutes"] = None
    result = analyze(design(), pilot)
    exchange = result["exchange_rate"]["headline_overall"]
    assert exchange["computed"] is False
    assert exchange["unavailable_reason"] == "one_or_more_agent_minute_measurements_missing"


def test_control_wrong_events_do_not_enter_headline_exchange() -> None:
    pilot = raw_pilot(collision_pairs=1, prevention=True)
    control_advisory = next(
        item
        for item in pilot["draws"]
        if item["draw"]["arm"] == "advisory" and item["draw"]["bundle_id"] == "control"
    )
    control_advisory["tasks"][0]["outcome"] = "landed_and_wrong"
    control_advisory["integrated"]["observed_bundle_outcome"] = "landed_and_wrong"
    control_advisory["metrics"]["total_agent_minutes"] = 1000.0
    exchange = analyze(design(), pilot)["exchange_rate"]["headline_overall"]
    assert exchange["observed_wrong_landing_reduction"] == 1
    assert exchange["additional_blocking_agent_minutes"] == 1.0


def test_missing_collision_numerator_cannot_pass_clean_null() -> None:
    pilot = raw_pilot(collision_pairs=1, prevention=True)
    pilot["draws"][0]["metrics"]["collisions"].pop("realised_designed_pair_count")
    result = analyze(design(), pilot)

    assert result["collision_gate"]["pooled"]["rate"] is None
    assert result["collision_gate"]["pooled"]["passes_threshold"] is None
    assert result["exchange_rate"]["headline_overall"]["computed"] is False


def test_raw_collision_denominator_mismatch_cannot_pass_gate() -> None:
    pilot = raw_pilot(collision_pairs=1, prevention=True)
    pilot["draws"][0]["metrics"]["collisions"]["designed_pair_denominator"] = 2
    result = analyze(design(), pilot)

    assert result["collision_gate"]["pooled"]["rate"] is None
    assert result["collision_gate"]["pooled"]["raw_denominator_mismatches"] == [
        {
            "draw_id": "overlap-advisory",
            "preregistered": 1,
            "raw_summary": 2.0,
        }
    ]
    assert result["exchange_rate"]["headline_overall"]["computed"] is False


def test_analysis_rejects_missing_or_duplicate_preregistered_task_rows() -> None:
    missing = raw_pilot(collision_pairs=1, prevention=True)
    missing["draws"][0]["tasks"] = missing["draws"][0]["tasks"][:1]
    with pytest.raises(AnalysisError, match="task IDs/order"):
        analyze(design(), missing)

    duplicate = raw_pilot(collision_pairs=1, prevention=True)
    duplicate["draws"][0]["tasks"][1]["task_id"] = duplicate["draws"][0]["tasks"][0]["task_id"]
    with pytest.raises(AnalysisError, match="task IDs/order"):
        analyze(design(), duplicate)


def test_analysis_requires_explicit_accepted_and_valid_apparatus_flags() -> None:
    pilot = raw_pilot(collision_pairs=1, prevention=True)
    pilot["draws"][0].pop("excluded")
    with pytest.raises(AnalysisError, match="excluded=false"):
        analyze(design(), pilot)

    pilot = raw_pilot(collision_pairs=1, prevention=True)
    pilot["draws"][0]["apparatus"].pop("valid")
    with pytest.raises(AnalysisError, match="apparatus.valid=true"):
        analyze(design(), pilot)


def test_analyze_files_writes_machine_readable_output(tmp_path: Path) -> None:
    design_path = tmp_path / "DESIGN.json"
    pilot_path = tmp_path / "PILOT.json"
    output_path = tmp_path / "ANALYSIS.json"
    design_path.write_text(json.dumps(design()), encoding="utf-8")
    pilot_path.write_text(json.dumps(raw_pilot()), encoding="utf-8")

    result = analyze_files(
        design_path, pilot_path, output_path, verify_provenance=False
    )
    written = json.loads(output_path.read_text(encoding="utf-8"))

    assert written["collision_gate"] == result["collision_gate"]
    assert written["sources"]["design_path"] == str(design_path.resolve())
    assert len(written["sources"]["pilot_sha256"]) == 64


def test_analyze_files_fails_closed_without_sealed_provenance(tmp_path: Path) -> None:
    design_path = tmp_path / "DESIGN.json"
    pilot_path = tmp_path / "PILOT.json"
    design_path.write_text(json.dumps(design()), encoding="utf-8")
    pilot_path.write_text(json.dumps(raw_pilot()), encoding="utf-8")
    with pytest.raises(AnalysisError, match="sealed tasks_path"):
        analyze_files(design_path, pilot_path, tmp_path / "ANALYSIS.json")


def test_provenance_reconciles_pilot_with_retained_accepted_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(analyze_module, "PROJECT_ROOT", tmp_path)
    design_path = tmp_path / "instruments" / "posture" / "DESIGN.json"
    analyzer_path = tmp_path / "instruments" / "posture" / "analyze.py"
    tasks_path = tmp_path / "exploratory" / "posture" / "TASKS.json"
    schedule_path = tmp_path / "exploratory" / "posture" / "pilot" / "SCHEDULE.json"
    smoke_path = tmp_path / "exploratory" / "posture" / "smoke" / "summary.json"
    attempt_path = tmp_path / "exploratory" / "posture" / "pilot" / "runs" / "d" / "attempt-001" / "summary.json"
    for path in (design_path, analyzer_path, tasks_path, schedule_path, smoke_path, attempt_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text("{}", encoding="utf-8")
    analyzer_path.write_text("sealed analyzer", encoding="utf-8")
    smoke_path.write_text("{}", encoding="utf-8")
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    tasks_path.write_text(
        json.dumps(
            {
                "design_path": "instruments/posture/DESIGN.json",
                "design_sha256": sha(design_path),
            }
        ),
        encoding="utf-8",
    )
    scheduled = {"draw_id": "d", "arm": "advisory", "bundle_id": "overlap"}
    accepted = {
        "draw": scheduled,
        "excluded": False,
        "exclusion_reason": None,
        "apparatus_fingerprint": "fp",
        "apparatus": {"valid": True},
        "tasks": [{"task_id": "t", "outcome": "landed_and_correct"}],
        "metrics": {"total_agent_minutes": 1.0},
    }
    attempt_path.write_text(json.dumps(accepted), encoding="utf-8")
    artifacts = {
        "instruments/posture/analyze.py": sha(analyzer_path),
        "instruments/posture/DESIGN.json": sha(design_path),
        "exploratory/posture/TASKS.json": sha(tasks_path),
    }
    schedule_path.write_text(
        json.dumps(
            {
                "tasks_sha256": sha(tasks_path),
                "apparatus_fingerprint": "fp",
                "artifact_sha256": artifacts,
                "live_hook_smoke": {
                    "path": "exploratory/posture/smoke/summary.json",
                    "sha256": sha(smoke_path),
                },
                "draws": [scheduled],
            }
        ),
        encoding="utf-8",
    )
    pilot = {
        "tasks_path": "exploratory/posture/TASKS.json",
        "tasks_sha256": sha(tasks_path),
        "schedule_path": "exploratory/posture/pilot/SCHEDULE.json",
        "schedule_sha256": sha(schedule_path),
        "apparatus_fingerprint": "fp",
        "draws": [accepted],
        "attempt_ledger": {
            "attempts": [
                {
                    "draw_id": "d",
                    "arm": "advisory",
                    "summary": "exploratory/posture/pilot/runs/d/attempt-001/summary.json",
                    "summary_sha256": sha(attempt_path),
                    "excluded": False,
                    "exclusion_reason": None,
                }
            ]
        },
    }
    analyze_module._validate_provenance(design_path, pilot)
    edited = json.loads(json.dumps(pilot))
    edited["draws"][0]["metrics"]["total_agent_minutes"] = 99.0
    with pytest.raises(AnalysisError, match="differs from retained accepted summary"):
        analyze_module._validate_provenance(design_path, edited)
