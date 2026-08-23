"""Aggregate the posture pilot without filling gaps in the raw measurements.

The raw pilot is deliberately retained separately.  This module reads its
accepted draw summaries and the preregistered design, applies the clean-null
collision gate, and writes a machine-readable analysis.  It does not inspect
or mutate a repository and it never runs an experimental draw.
"""

from __future__ import annotations

import argparse
import collections
import copy
import datetime as dt
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DESIGN = PROJECT_ROOT / "instruments" / "posture" / "DESIGN.json"
DEFAULT_PILOT = PROJECT_ROOT / "exploratory" / "posture" / "pilot" / "PILOT.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "exploratory" / "posture" / "ANALYSIS.json"

SUPPORTED_CLEAN_NULL_METRIC = (
    "sum of realised designed unique agent pairs divided by sum of "
    "preregistered designed pair opportunities"
)
TASK_OUTCOMES = (
    "landed_and_correct",
    "landed_and_wrong",
    "landed_but_task_incomplete",
    "abandoned",
    "unverified",
)
BUNDLE_OUTCOMES = TASK_OUTCOMES
HEADLINE_CLASSIFIABLE_TASK_OUTCOMES = {
    "landed_and_correct",
    "landed_and_wrong",
    "landed_but_task_incomplete",
    "abandoned",
}


class AnalysisError(RuntimeError):
    """The input cannot support a trustworthy analysis."""


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AnalysisError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _project_file(value: str) -> Path:
    path = Path(value)
    resolved = (PROJECT_ROOT / path).resolve(strict=True) if not path.is_absolute() else path.resolve(strict=True)
    try:
        resolved.relative_to(PROJECT_ROOT)
    except ValueError as error:
        raise AnalysisError(f"provenance path escapes the project: {value}") from error
    if not resolved.is_file():
        raise AnalysisError(f"provenance path is not a file: {resolved}")
    return resolved


def _validate_provenance(design_path: Path, pilot: Mapping[str, Any]) -> dict[str, Any]:
    """Prove analysis is using the design and code sealed before any outcomes."""

    tasks_value = pilot.get("tasks_path")
    schedule_value = pilot.get("schedule_path")
    if not isinstance(tasks_value, str) or not isinstance(schedule_value, str):
        raise AnalysisError("PILOT.json lacks sealed tasks_path or schedule_path provenance")
    tasks_path = _project_file(tasks_value)
    schedule_path = _project_file(schedule_value)
    if _sha256(tasks_path) != pilot.get("tasks_sha256"):
        raise AnalysisError("TASKS.json hash differs from PILOT.json")
    if _sha256(schedule_path) != pilot.get("schedule_sha256"):
        raise AnalysisError("SCHEDULE.json hash differs from PILOT.json")
    tasks = _load_json(tasks_path)
    schedule = _load_json(schedule_path)
    design_hash = _sha256(design_path)
    if tasks.get("design_sha256") != design_hash:
        raise AnalysisError("current DESIGN.json differs from the task-construction design")
    if schedule.get("tasks_sha256") != pilot.get("tasks_sha256"):
        raise AnalysisError("schedule, pilot, and task-manifest hashes disagree")
    if schedule.get("apparatus_fingerprint") != pilot.get("apparatus_fingerprint"):
        raise AnalysisError("schedule and pilot apparatus fingerprints disagree")
    scheduled_draws = schedule.get("draws")
    pilot_draws = pilot.get("draws")
    if not isinstance(scheduled_draws, list) or not isinstance(pilot_draws, list):
        raise AnalysisError("schedule or pilot lacks its draw list")
    if len(scheduled_draws) != len(pilot_draws):
        raise AnalysisError("accepted pilot draw count differs from the sealed schedule")
    for index, (scheduled, summary) in enumerate(zip(scheduled_draws, pilot_draws)):
        if not isinstance(scheduled, Mapping) or not isinstance(summary, Mapping):
            raise AnalysisError(f"malformed scheduled/accepted draw at index {index}")
        if summary.get("draw") != scheduled:
            raise AnalysisError(
                f"accepted draw metadata differs from the sealed schedule at index {index}"
            )
        apparatus = summary.get("apparatus")
        if summary.get("excluded") is not False or not isinstance(apparatus, Mapping) or apparatus.get("valid") is not True:
            raise AnalysisError(
                f"accepted draw at index {index} lacks excluded=false and apparatus.valid=true"
            )
    ledger = pilot.get("attempt_ledger")
    attempt_records = ledger.get("attempts") if isinstance(ledger, Mapping) else None
    if not isinstance(attempt_records, list):
        raise AnalysisError("PILOT.json lacks its retained attempt-summary ledger")
    scheduled_by_id = {
        str(draw.get("draw_id")): draw
        for draw in scheduled_draws
        if isinstance(draw, Mapping) and isinstance(draw.get("draw_id"), str)
    }
    accepted_by_id: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for record in attempt_records:
        if not isinstance(record, Mapping) or not isinstance(record.get("summary"), str):
            raise AnalysisError("attempt ledger contains a malformed record")
        draw_id = record.get("draw_id")
        if not isinstance(draw_id, str) or draw_id not in scheduled_by_id:
            raise AnalysisError(f"attempt ledger references an unscheduled draw: {draw_id}")
        summary_path = _project_file(str(record["summary"]))
        if _sha256(summary_path) != record.get("summary_sha256"):
            raise AnalysisError(f"retained attempt summary hash changed: {summary_path}")
        retained = _load_json(summary_path)
        if retained.get("draw") != scheduled_by_id[draw_id]:
            raise AnalysisError(f"retained attempt draw differs from schedule: {draw_id}")
        excluded = retained.get("excluded")
        reason = retained.get("exclusion_reason")
        if (
            record.get("excluded") is not excluded
            or record.get("exclusion_reason") != reason
            or record.get("arm") != scheduled_by_id[draw_id].get("arm")
        ):
            raise AnalysisError(f"attempt ledger metadata differs from summary: {summary_path}")
        if excluded is True:
            if reason != "at_least_one_model_never_finished":
                raise AnalysisError(
                    f"non-fairness exclusion present in completed pilot: {summary_path}"
                )
        elif excluded is False:
            accepted_by_id[draw_id].append(retained)
        else:
            raise AnalysisError(f"retained attempt lacks an explicit exclusion flag: {summary_path}")
    pilot_by_id = {
        str(summary.get("draw", {}).get("draw_id")): summary
        for summary in pilot_draws
        if isinstance(summary, Mapping) and isinstance(summary.get("draw"), Mapping)
    }
    if set(accepted_by_id) != set(scheduled_by_id) or set(pilot_by_id) != set(scheduled_by_id):
        raise AnalysisError("accepted retained summaries do not cover every scheduled slot")
    for draw_id in scheduled_by_id:
        if len(accepted_by_id[draw_id]) != 1:
            raise AnalysisError(f"scheduled slot has other than one accepted summary: {draw_id}")
        if accepted_by_id[draw_id][0] != pilot_by_id[draw_id]:
            raise AnalysisError(f"PILOT draw differs from retained accepted summary: {draw_id}")
    artifacts = schedule.get("artifact_sha256")
    if not isinstance(artifacts, Mapping):
        raise AnalysisError("SCHEDULE.json lacks its sealed artifact hash map")
    required_artifacts = {
        "instruments/posture/analyze.py",
        str(tasks.get("design_path", "")),
    }
    if not required_artifacts.issubset(set(artifacts)):
        raise AnalysisError("schedule did not seal analyze.py and the construction design")
    changed: list[str] = []
    for value, expected_hash in artifacts.items():
        if not isinstance(value, str) or not isinstance(expected_hash, str):
            raise AnalysisError("schedule artifact hash map is malformed")
        try:
            current_hash = _sha256(_project_file(value))
        except (AnalysisError, OSError):
            changed.append(value)
            continue
        if current_hash != expected_hash:
            changed.append(value)
    if changed:
        raise AnalysisError(f"sealed analysis/apparatus artifacts changed: {sorted(changed)}")
    smoke = schedule.get("live_hook_smoke")
    if not isinstance(smoke, Mapping) or not isinstance(smoke.get("path"), str):
        raise AnalysisError("schedule lacks sealed live-smoke provenance")
    smoke_path = _project_file(str(smoke["path"]))
    if _sha256(smoke_path) != smoke.get("sha256"):
        raise AnalysisError("live-smoke evidence changed after schedule creation")
    for draw in pilot.get("draws", []):
        if not isinstance(draw, Mapping):
            raise AnalysisError("PILOT.json contains a malformed draw")
        if draw.get("apparatus_fingerprint") != pilot.get("apparatus_fingerprint"):
            raise AnalysisError("an accepted draw has a different apparatus fingerprint")
    return {
        "tasks_path": str(tasks_path),
        "tasks_sha256": _sha256(tasks_path),
        "schedule_path": str(schedule_path),
        "schedule_sha256": _sha256(schedule_path),
        "design_sha256": design_hash,
        "apparatus_fingerprint": pilot.get("apparatus_fingerprint"),
        "sealed_artifact_count": len(artifacts),
        "live_smoke_path": str(smoke_path),
        "live_smoke_sha256": _sha256(smoke_path),
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=False, ensure_ascii=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _numeric_summary(values: Iterable[Any], expected_observations: int) -> dict[str, Any]:
    observed = [number for value in values if (number := _number(value)) is not None]
    count = len(observed)
    return {
        "expected_observations": expected_observations,
        "observation_count": count,
        "missing_count": max(0, expected_observations - count),
        "complete": count == expected_observations,
        "sum": sum(observed) if observed else None,
        "mean": sum(observed) / count if observed else None,
        "minimum": min(observed) if observed else None,
        "maximum": max(observed) if observed else None,
    }


def _rate(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _draw_identity(summary: Mapping[str, Any]) -> tuple[str, str, str]:
    draw = summary.get("draw")
    if not isinstance(draw, Mapping):
        raise AnalysisError("accepted draw summary has no draw object")
    draw_id = draw.get("draw_id")
    arm = draw.get("arm")
    bundle_id = draw.get("bundle_id")
    if not all(isinstance(value, str) and value for value in (draw_id, arm, bundle_id)):
        raise AnalysisError("accepted draw is missing draw_id, arm, or bundle_id")
    return str(draw_id), str(arm), str(bundle_id)


def _tasks(draw: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    value = draw.get("tasks")
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        return None
    return value


def _metric(draw: Mapping[str, Any], name: str) -> Any:
    metrics = draw.get("metrics")
    return metrics.get(name) if isinstance(metrics, Mapping) else None


def _collision_metric(draw: Mapping[str, Any], name: str) -> Any:
    metrics = draw.get("metrics")
    collisions = metrics.get("collisions") if isinstance(metrics, Mapping) else None
    return collisions.get(name) if isinstance(collisions, Mapping) else None


def _integrated_outcome(draw: Mapping[str, Any]) -> str | None:
    integrated = draw.get("integrated")
    outcome = integrated.get("observed_bundle_outcome") if isinstance(integrated, Mapping) else None
    return str(outcome) if isinstance(outcome, str) else None


def _base_completion(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    task_rows: list[Mapping[str, Any]] = []
    draws_missing_tasks = 0
    all_correct_observations: list[bool] = []
    for draw in draws:
        rows = _tasks(draw)
        if rows is None:
            draws_missing_tasks += 1
            continue
        task_rows.extend(rows)
        if rows:
            outcomes = [row.get("outcome") for row in rows]
            all_correct_observations.append(all(value == "landed_and_correct" for value in outcomes))

    correct = sum(row.get("outcome") == "landed_and_correct" for row in task_rows)
    finished_values = [
        row.get("model", {}).get("model_finished")
        if isinstance(row.get("model"), Mapping)
        else None
        for row in task_rows
    ]
    finished = sum(value is True for value in finished_values)
    unfinished = sum(value is False for value in finished_values)
    missing_finished = len(finished_values) - finished - unfinished
    return {
        "accepted_draws": len(draws),
        "draws_with_task_records": len(draws) - draws_missing_tasks,
        "draws_missing_task_records": draws_missing_tasks,
        "task_observations": len(task_rows),
        "landed_and_correct_count": correct,
        "landed_and_correct_rate": _rate(correct, len(task_rows)),
        "all_tasks_correct_draw_count": sum(all_correct_observations),
        "all_tasks_correct_draw_rate": _rate(
            sum(all_correct_observations), len(all_correct_observations)
        ),
        "finished_model_response_count": finished,
        "unfinished_model_response_count": unfinished,
        "missing_model_finished_count": missing_finished,
        "finished_model_response_rate": _rate(finished, len(task_rows))
        if missing_finished == 0
        else None,
    }


def _task_outcomes(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [row for draw in draws if (items := _tasks(draw)) is not None for row in items]
    counts = {outcome: 0 for outcome in TASK_OUTCOMES}
    unexpected: collections.Counter[str] = collections.Counter()
    missing = 0
    for row in rows:
        outcome = row.get("outcome")
        if outcome in counts:
            counts[str(outcome)] += 1
        elif isinstance(outcome, str):
            unexpected[outcome] += 1
        else:
            missing += 1

    blocked_flags = [row.get("blocked_then_completed") for row in rows]
    blocked_count = sum(value is True for value in blocked_flags)
    blocked_flag_missing = sum(not isinstance(value, bool) for value in blocked_flags)
    completed_waits = [
        row.get("blocking_wait_seconds")
        for row in rows
        if row.get("blocked_then_completed") is True
    ]
    all_waits = [row.get("blocking_wait_seconds") for row in rows]
    return {
        "task_observations": len(rows),
        "outcome_counts": counts,
        "outcome_missing_count": missing,
        "unexpected_outcome_counts": dict(sorted(unexpected.items())),
        "blocked_then_completed_count": blocked_count,
        "blocked_then_completed_flag_missing_count": blocked_flag_missing,
        "blocked_then_completed_wait_seconds": _numeric_summary(
            completed_waits, blocked_count
        ),
        "all_task_blocking_wait_seconds": _numeric_summary(all_waits, len(rows)),
    }


def _rework(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [row for draw in draws if (items := _tasks(draw)) is not None for row in items]
    available: list[Mapping[str, Any]] = []
    unavailable_reasons: collections.Counter[str] = collections.Counter()
    unavailable = 0
    unknown = 0
    unavailable_with_numeric_values = 0
    for row in rows:
        rework = row.get("rework")
        if not isinstance(rework, Mapping):
            unknown += 1
            continue
        status = rework.get("measurement_available")
        if status is True:
            available.append(rework)
        elif status is False:
            unavailable += 1
            reason = rework.get("reason")
            unavailable_reasons[str(reason) if reason is not None else "unspecified"] += 1
            if _number(rework.get("verified_rework_operations")) is not None or _number(
                rework.get("verified_rework_seconds")
            ) is not None:
                unavailable_with_numeric_values += 1
        else:
            unknown += 1

    operations = [item.get("verified_rework_operations") for item in available]
    seconds = [item.get("verified_rework_seconds") for item in available]
    operation_numbers = [
        number for item in operations if (number := _number(item)) is not None
    ]
    return {
        "task_observations": len(rows),
        "measurement_available_count": len(available),
        "measurement_unavailable_count": unavailable,
        "measurement_status_unknown_count": unknown,
        "unavailable_reason_counts": dict(sorted(unavailable_reasons.items())),
        "unavailable_records_with_numeric_values": unavailable_with_numeric_values,
        "verified_rework_operations": _numeric_summary(operations, len(available)),
        "verified_rework_seconds": _numeric_summary(seconds, len(available)),
        "tasks_with_verified_rework": sum(value > 0 for value in operation_numbers),
    }


def _bundle_outcomes(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = {outcome: 0 for outcome in BUNDLE_OUTCOMES}
    unexpected: collections.Counter[str] = collections.Counter()
    missing = 0
    for draw in draws:
        outcome = _integrated_outcome(draw)
        if outcome in counts:
            counts[str(outcome)] += 1
        elif outcome is None:
            missing += 1
        else:
            unexpected[outcome] += 1
    unclassifiable_for_headline = (
        missing + sum(unexpected.values()) + counts["unverified"]
    )
    return {
        "accepted_draws": len(draws),
        "outcome_counts": counts,
        "missing_outcome_count": missing,
        "unexpected_outcome_counts": dict(sorted(unexpected.items())),
        "observed_landed_and_wrong_count": counts["landed_and_wrong"],
        "headline_wrong_count_complete": unclassifiable_for_headline == 0,
        "headline_unclassifiable_count": unclassifiable_for_headline,
    }


def _performance(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "total_agent_minutes",
        "bundle_wall_seconds",
        "evaluation_wall_seconds",
        "agent_execution_wall_seconds",
        "blocking_wait_seconds",
        "fleet_capacity_seconds",
        "combined_idle_seconds",
        "fleet_idle_fraction",
        "merge_conflicts",
    )
    summaries = {
        field: _numeric_summary([_metric(draw, field) for draw in draws], len(draws))
        for field in fields
    }
    idle = summaries["combined_idle_seconds"]
    capacity = summaries["fleet_capacity_seconds"]
    weighted_idle = None
    weighted_reason = None
    if not idle["complete"] or not capacity["complete"]:
        weighted_reason = "one_or_more_draws_missing_combined_idle_or_fleet_capacity"
    elif capacity["sum"] is None or capacity["sum"] <= 0:
        weighted_reason = "fleet_capacity_sum_not_positive"
    else:
        weighted_idle = idle["sum"] / capacity["sum"]
    summaries["capacity_weighted_fleet_idle_fraction"] = {
        "value": weighted_idle,
        "available": weighted_idle is not None,
        "unavailable_reason": weighted_reason,
    }
    return summaries


def _raw_collision_summary(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "realised_designed_pair_count": _numeric_summary(
            [_collision_metric(draw, "realised_designed_pair_count") for draw in draws],
            len(draws),
        ),
        "designed_pair_denominator_from_raw_summary": _numeric_summary(
            [_collision_metric(draw, "designed_pair_denominator") for draw in draws],
            len(draws),
        ),
        "claim_acquisitions": _numeric_summary(
            [_collision_metric(draw, "claim_acquisitions") for draw in draws], len(draws)
        ),
    }


def _summarize_group(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "accepted_draw_count": len(draws),
        "draw_ids": [_draw_identity(draw)[0] for draw in draws],
        "base_task_completion": _base_completion(draws),
        "task_outcomes": _task_outcomes(draws),
        "rework": _rework(draws),
        "observed_bundle_outcomes": _bundle_outcomes(draws),
        "performance": _performance(draws),
        "raw_collision_metrics": _raw_collision_summary(draws),
    }


def _collision_rollup(
    draws: Sequence[Mapping[str, Any]],
    bundle_design: Mapping[str, Mapping[str, Any]],
    threshold: float,
) -> dict[str, Any]:
    designed_denominator = 0
    observed_numerators: list[float] = []
    missing_numerator_draws: list[str] = []
    invalid_numerator_draws: list[str] = []
    raw_denominator_mismatches: list[dict[str, Any]] = []
    claim_values: list[float] = []
    missing_claim_draws: list[str] = []
    task_slots = 0
    for draw in draws:
        draw_id, _, bundle_id = _draw_identity(draw)
        bundle = bundle_design[bundle_id]
        denominator_value = bundle.get("expected_pairs_with_collision")
        denominator = _number(denominator_value)
        if denominator is None or denominator <= 0 or not denominator.is_integer():
            raise AnalysisError(
                f"collision-eligible bundle {bundle_id} has invalid expected pair count"
            )
        denominator_int = int(denominator)
        designed_denominator += denominator_int

        raw_denominator = _number(_collision_metric(draw, "designed_pair_denominator"))
        if raw_denominator is not None and raw_denominator != denominator_int:
            raw_denominator_mismatches.append(
                {
                    "draw_id": draw_id,
                    "preregistered": denominator_int,
                    "raw_summary": raw_denominator,
                }
            )

        numerator = _number(_collision_metric(draw, "realised_designed_pair_count"))
        if numerator is None:
            missing_numerator_draws.append(draw_id)
        elif not numerator.is_integer() or numerator < 0 or numerator > denominator_int:
            invalid_numerator_draws.append(draw_id)
        else:
            observed_numerators.append(numerator)

        tasks = bundle.get("tasks")
        if not isinstance(tasks, list):
            raise AnalysisError(f"bundle {bundle_id} has no preregistered task list")
        task_slots += len(tasks)
        claims = _number(_collision_metric(draw, "claim_acquisitions"))
        if claims is None or claims < 0:
            missing_claim_draws.append(draw_id)
        else:
            claim_values.append(claims)

    complete = (
        not missing_numerator_draws
        and not invalid_numerator_draws
        and not raw_denominator_mismatches
    )
    realised_sum = sum(observed_numerators) if observed_numerators else None
    rate = (
        realised_sum / designed_denominator
        if complete and realised_sum is not None and designed_denominator > 0
        else None
    )
    claims_complete = not missing_claim_draws
    claims_sum = sum(claim_values) if claim_values else None
    claim_rate = (
        claims_sum / task_slots
        if claims_complete and claims_sum is not None and task_slots > 0
        else None
    )
    return {
        "accepted_overlap_draws": len(draws),
        "preregistered_designed_pair_opportunities": designed_denominator,
        "realised_designed_pairs_observed_sum": realised_sum,
        "numerator_observation_count": len(observed_numerators),
        "missing_numerator_draw_ids": missing_numerator_draws,
        "invalid_numerator_draw_ids": invalid_numerator_draws,
        "raw_denominator_mismatches": raw_denominator_mismatches,
        "rate": rate,
        "near_zero": rate < threshold if rate is not None else None,
        "passes_threshold": rate >= threshold if rate is not None else None,
        "claim_uptake": {
            "claim_acquisitions_observed_sum": claims_sum,
            "preregistered_task_slots": task_slots,
            "claims_per_task_slot": claim_rate,
            "observation_complete": claims_complete,
            "missing_claim_draw_ids": missing_claim_draws,
        },
    }


def _clean_null_gate(
    design: Mapping[str, Any],
    accepted: Sequence[Mapping[str, Any]],
    bundle_design: Mapping[str, Mapping[str, Any]],
    pilot_complete: bool,
) -> dict[str, Any]:
    pilot = design.get("pilot")
    if not isinstance(pilot, Mapping):
        raise AnalysisError("DESIGN has no pilot object")
    rule = pilot.get("clean_null_rule")
    if not isinstance(rule, Mapping):
        raise AnalysisError("DESIGN has no pilot.clean_null_rule")
    if rule.get("metric") != SUPPORTED_CLEAN_NULL_METRIC:
        raise AnalysisError("unsupported clean-null metric; refusing to reinterpret the design")
    threshold = _number(rule.get("near_zero_below"))
    arm_threshold = _number(rule.get("arm_specific_warning_below"))
    if threshold is None or arm_threshold is None:
        raise AnalysisError("clean-null thresholds must be finite numbers")

    arms_value = pilot.get("arms")
    if not isinstance(arms_value, list) or not all(isinstance(arm, str) for arm in arms_value):
        raise AnalysisError("DESIGN pilot arms are invalid")
    arms = [str(arm) for arm in arms_value]
    overlap_bundles = {
        bundle_id
        for bundle_id, bundle in bundle_design.items()
        if (_number(bundle.get("expected_pairs_with_collision")) or 0) > 0
    }
    overlap_draws = [
        draw for draw in accepted if _draw_identity(draw)[2] in overlap_bundles
    ]
    pooled = _collision_rollup(overlap_draws, bundle_design, threshold)
    by_arm: dict[str, Any] = {}
    for arm in arms:
        arm_draws = [draw for draw in overlap_draws if _draw_identity(draw)[1] == arm]
        result = _collision_rollup(arm_draws, bundle_design, arm_threshold)
        result["individually_comparable"] = result["passes_threshold"] is True
        by_arm[arm] = result

    pooled_passes = pooled["passes_threshold"] is True
    all_arms_comparable = all(by_arm[arm]["individually_comparable"] for arm in arms)
    blocking_advisory_comparable = all(
        arm in by_arm and by_arm[arm]["individually_comparable"]
        for arm in ("advisory", "blocking")
    )
    if pooled["rate"] is None:
        verdict = "collision_rate_unavailable"
    elif pooled["near_zero"]:
        verdict = "measured_no_contention_clean_null"
    else:
        verdict = "contention_observed"
    return {
        "verdict": verdict,
        "scope": rule.get("scope"),
        "metric": rule.get("metric"),
        "near_zero_below": threshold,
        "arm_specific_warning_below": arm_threshold,
        "overlap_bundle_ids": sorted(overlap_bundles),
        "pooled": pooled,
        "by_arm": by_arm,
        "pilot_complete": pilot_complete,
        "all_arms_individually_comparable": all_arms_comparable,
        "blocking_vs_advisory_individually_comparable": blocking_advisory_comparable,
        "all_posture_comparisons_allowed": (
            pooled_passes and pilot_complete and all_arms_comparable
        ),
        "blocking_vs_advisory_exchange_allowed": (
            pooled_passes and pilot_complete and blocking_advisory_comparable
        ),
        "decision_text_from_design": rule.get("decision"),
    }


def _completeness(
    design: Mapping[str, Any],
    accepted: Sequence[Mapping[str, Any]],
    bundle_design: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    pilot = design["pilot"]
    arms = [str(arm) for arm in pilot["arms"]]
    expected_per_cell_value = pilot.get("draws_per_cell")
    if not isinstance(expected_per_cell_value, int) or isinstance(expected_per_cell_value, bool):
        raise AnalysisError("DESIGN pilot.draws_per_cell must be an integer")
    expected_per_cell = expected_per_cell_value
    counts: collections.Counter[tuple[str, str]] = collections.Counter(
        (_draw_identity(draw)[2], _draw_identity(draw)[1]) for draw in accepted
    )
    cells: list[dict[str, Any]] = []
    for bundle_id in bundle_design:
        for arm in arms:
            observed = counts[(bundle_id, arm)]
            cells.append(
                {
                    "bundle_id": bundle_id,
                    "arm": arm,
                    "expected_accepted_draws": expected_per_cell,
                    "observed_accepted_draws": observed,
                    "complete": observed == expected_per_cell,
                }
            )
    expected_total = expected_per_cell * len(arms) * len(bundle_design)
    return {
        "complete": all(cell["complete"] for cell in cells),
        "expected_accepted_draws": expected_total,
        "observed_accepted_draws": len(accepted),
        "cells": cells,
    }


def _comparison_input(draws: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    exposure = collections.Counter(_draw_identity(draw)[2] for draw in draws)
    minute_values = [_number(_metric(draw, "total_agent_minutes")) for draw in draws]
    minutes_complete = all(value is not None for value in minute_values)
    task_lists = [_tasks(draw) for draw in draws]
    outcomes = [
        row.get("outcome")
        for rows in task_lists
        if rows is not None
        for row in rows
    ]
    headline_complete = all(rows is not None for rows in task_lists) and all(
        outcome in HEADLINE_CLASSIFIABLE_TASK_OUTCOMES for outcome in outcomes
    )
    return {
        "accepted_draws": len(draws),
        "exposure_by_bundle": dict(sorted(exposure.items())),
        "total_agent_minutes": sum(value for value in minute_values if value is not None)
        if minutes_complete and minute_values
        else None,
        "agent_minutes_complete": minutes_complete and bool(minute_values),
        "task_observations": len(outcomes),
        "observed_task_wrong_landings": sum(
            outcome == "landed_and_wrong" for outcome in outcomes
        ),
        "wrong_landing_count_complete": headline_complete,
        "unclassifiable_task_outcomes": sum(
            outcome not in HEADLINE_CLASSIFIABLE_TASK_OUTCOMES for outcome in outcomes
        ),
    }


def _exchange_rate(
    draws: Sequence[Mapping[str, Any]],
    gate_allowed: bool,
    *,
    scope: str,
) -> dict[str, Any]:
    advisory_draws = [draw for draw in draws if _draw_identity(draw)[1] == "advisory"]
    blocking_draws = [draw for draw in draws if _draw_identity(draw)[1] == "blocking"]
    advisory = _comparison_input(advisory_draws)
    blocking = _comparison_input(blocking_draws)
    exposure_equal = advisory["exposure_by_bundle"] == blocking["exposure_by_bundle"]
    output: dict[str, Any] = {
        "scope": scope,
        "computed": False,
        "agent_minutes_per_observed_wrong_landing_prevented": None,
        "advisory": advisory,
        "blocking": blocking,
        "equal_bundle_exposure": exposure_equal,
        "additional_blocking_agent_minutes": None,
        "observed_wrong_landing_reduction": None,
        "unavailable_reason": None,
        "verdict": None,
    }
    if not gate_allowed:
        output["unavailable_reason"] = "collision_gate_or_required_arm_comparability_failed"
        output["verdict"] = "not_estimable_given_collision_gate"
        return output
    if not exposure_equal or not advisory_draws or not blocking_draws:
        output["unavailable_reason"] = "advisory_and_blocking_exposure_not_equal"
        output["verdict"] = "not_estimable_unequal_exposure"
        return output
    if not advisory["agent_minutes_complete"] or not blocking["agent_minutes_complete"]:
        output["unavailable_reason"] = "one_or_more_agent_minute_measurements_missing"
        output["verdict"] = "not_estimable_missing_agent_minutes"
        return output
    if not advisory["wrong_landing_count_complete"] or not blocking["wrong_landing_count_complete"]:
        output["unavailable_reason"] = "one_or_more_task_outcomes_unclassifiable"
        output["verdict"] = "not_estimable_unclassifiable_wrong_landings"
        return output

    additional = blocking["total_agent_minutes"] - advisory["total_agent_minutes"]
    reduction = (
        advisory["observed_task_wrong_landings"]
        - blocking["observed_task_wrong_landings"]
    )
    output["additional_blocking_agent_minutes"] = additional
    output["observed_wrong_landing_reduction"] = reduction
    if reduction <= 0:
        output["unavailable_reason"] = "blocking_did_not_reduce_observed_wrong_landings"
        output["verdict"] = (
            "blocking_has_no_defence_at_any_price_on_observed_wrong_landings"
        )
        return output
    output["computed"] = True
    output["agent_minutes_per_observed_wrong_landing_prevented"] = additional / reduction
    output["verdict"] = "exchange_rate_observed"
    return output


def _retry_ledger(pilot: Mapping[str, Any]) -> dict[str, Any]:
    ledger = pilot.get("attempt_ledger")
    if not isinstance(ledger, Mapping):
        return {
            "available": False,
            "unavailable_reason": "raw PILOT.json has no attempt_ledger object",
            "excluded_attempt_count": None,
            "raw_attempt_ledger": None,
        }
    attempts = ledger.get("attempts")
    attempt_rows = attempts if isinstance(attempts, list) else []
    excluded = [
        copy.deepcopy(item)
        for item in attempt_rows
        if isinstance(item, Mapping) and item.get("excluded") is True
    ]
    return {
        "available": isinstance(attempts, list),
        "unavailable_reason": None
        if isinstance(attempts, list)
        else "attempt_ledger.attempts is missing or not a list",
        "excluded_attempt_count": len(excluded) if isinstance(attempts, list) else None,
        "excluded_attempts": excluded if isinstance(attempts, list) else None,
        "raw_attempt_ledger": copy.deepcopy(dict(ledger)),
    }


def analyze(design: Mapping[str, Any], pilot: Mapping[str, Any]) -> dict[str, Any]:
    """Return an analysis dictionary from preregistration and raw pilot data."""

    bundles_value = design.get("bundles")
    if not isinstance(bundles_value, list) or not bundles_value:
        raise AnalysisError("DESIGN has no bundles")
    bundle_design: dict[str, Mapping[str, Any]] = {}
    for bundle in bundles_value:
        if not isinstance(bundle, Mapping) or not isinstance(bundle.get("bundle_id"), str):
            raise AnalysisError("DESIGN contains an invalid bundle")
        bundle_id = str(bundle["bundle_id"])
        if bundle_id in bundle_design:
            raise AnalysisError(f"duplicate DESIGN bundle_id: {bundle_id}")
        bundle_design[bundle_id] = bundle

    pilot_design = design.get("pilot")
    if not isinstance(pilot_design, Mapping):
        raise AnalysisError("DESIGN has no pilot object")
    arms_value = pilot_design.get("arms")
    if not isinstance(arms_value, list) or not all(isinstance(arm, str) for arm in arms_value):
        raise AnalysisError("DESIGN pilot arms are invalid")
    arms = [str(arm) for arm in arms_value]

    raw_draws = pilot.get("draws")
    if not isinstance(raw_draws, list):
        raise AnalysisError("PILOT has no draws list")
    if not all(isinstance(draw, Mapping) for draw in raw_draws):
        raise AnalysisError("PILOT draws must be objects")
    accepted = [draw for draw in raw_draws if draw.get("excluded") is not True]
    embedded_excluded = [draw for draw in raw_draws if draw.get("excluded") is True]

    seen: set[str] = set()
    for draw in accepted:
        draw_id, arm, bundle_id = _draw_identity(draw)
        if draw_id in seen:
            raise AnalysisError(f"duplicate accepted draw_id: {draw_id}")
        seen.add(draw_id)
        if arm not in arms:
            raise AnalysisError(f"accepted draw {draw_id} has unregistered arm {arm}")
        if bundle_id not in bundle_design:
            raise AnalysisError(f"accepted draw {draw_id} has unregistered bundle {bundle_id}")
        if draw.get("excluded") is not False:
            raise AnalysisError(f"accepted draw {draw_id} is not explicitly marked excluded=false")
        apparatus = draw.get("apparatus")
        if not isinstance(apparatus, Mapping) or apparatus.get("valid") is not True:
            raise AnalysisError(
                f"draw {draw_id} lacks an explicit apparatus.valid=true record"
            )
        rows = _tasks(draw)
        if rows is None:
            raise AnalysisError(f"accepted draw {draw_id} has no task records")
        observed_task_ids = [row.get("task_id") for row in rows]
        expected_task_ids = [
            task.get("task_id") for task in bundle_design[bundle_id].get("tasks", [])
        ]
        if (
            not all(isinstance(task_id, str) and task_id for task_id in observed_task_ids)
            or len(set(observed_task_ids)) != len(observed_task_ids)
            or observed_task_ids != expected_task_ids
        ):
            raise AnalysisError(
                f"accepted draw {draw_id} task IDs/order differ from the preregistered bundle"
            )

    completeness = _completeness(design, accepted, bundle_design)
    collision_gate = _clean_null_gate(
        design, accepted, bundle_design, bool(completeness["complete"])
    )

    by_arm = {
        arm: _summarize_group(
            [draw for draw in accepted if _draw_identity(draw)[1] == arm]
        )
        for arm in arms
    }
    by_bundle = {
        bundle_id: _summarize_group(
            [draw for draw in accepted if _draw_identity(draw)[2] == bundle_id]
        )
        for bundle_id in bundle_design
    }
    by_arm_and_bundle = {
        arm: {
            bundle_id: _summarize_group(
                [
                    draw
                    for draw in accepted
                    if _draw_identity(draw)[1] == arm
                    and _draw_identity(draw)[2] == bundle_id
                ]
            )
            for bundle_id in bundle_design
        }
        for arm in arms
    }

    base_task_completion = {
        "by_arm": {arm: by_arm[arm]["base_task_completion"] for arm in arms},
        "by_bundle": {
            bundle_id: by_bundle[bundle_id]["base_task_completion"]
            for bundle_id in bundle_design
        },
        "by_arm_and_bundle": {
            arm: {
                bundle_id: by_arm_and_bundle[arm][bundle_id]["base_task_completion"]
                for bundle_id in bundle_design
            }
            for arm in arms
        },
    }
    outcome_table = {
        "by_arm": {
            arm: {
                "task_outcomes": by_arm[arm]["task_outcomes"],
                "rework": by_arm[arm]["rework"],
                "observed_bundle_outcomes": by_arm[arm]["observed_bundle_outcomes"],
            }
            for arm in arms
        },
        "by_arm_and_bundle": {
            arm: {
                bundle_id: {
                    "task_outcomes": by_arm_and_bundle[arm][bundle_id]["task_outcomes"],
                    "rework": by_arm_and_bundle[arm][bundle_id]["rework"],
                    "observed_bundle_outcomes": by_arm_and_bundle[arm][bundle_id][
                        "observed_bundle_outcomes"
                    ],
                }
                for bundle_id in bundle_design
            }
            for arm in arms
        },
    }

    gate_allowed = bool(collision_gate["blocking_vs_advisory_exchange_allowed"])
    overlap_bundle_ids = set(collision_gate["overlap_bundle_ids"])
    overlap_accepted = [
        draw for draw in accepted if _draw_identity(draw)[2] in overlap_bundle_ids
    ]
    exchange = {
        "headline_overall": _exchange_rate(
            overlap_accepted,
            gate_allowed,
            scope="preregistered overlap bundles only",
        ),
        "by_bundle": {
            bundle_id: _exchange_rate(
                [draw for draw in accepted if _draw_identity(draw)[2] == bundle_id],
                gate_allowed,
                scope=f"bundle:{bundle_id}",
            )
            for bundle_id in bundle_design
        },
        "event_unit": (
            "per-task landed-and-wrong events; visible failing testcase identities must be "
            "pre-run green, while task landing proof is retained separately"
        ),
    }

    overlap_by_arm = {
        arm: _summarize_group(
            [draw for draw in overlap_accepted if _draw_identity(draw)[1] == arm]
        )
        for arm in arms
    }
    axis_inputs = {
        arm: _comparison_input(
            [draw for draw in overlap_accepted if _draw_identity(draw)[1] == arm]
        )
        for arm in arms
    }
    isolate_input = axis_inputs.get("isolate", {})
    advisory_input = axis_inputs.get("advisory", {})
    isolate_minutes_delta = (
        isolate_input.get("total_agent_minutes") - advisory_input.get("total_agent_minutes")
        if isinstance(isolate_input.get("total_agent_minutes"), (int, float))
        and isinstance(advisory_input.get("total_agent_minutes"), (int, float))
        else None
    )
    isolate_wrong_delta = (
        isolate_input.get("observed_task_wrong_landings")
        - advisory_input.get("observed_task_wrong_landings")
        if isolate_input.get("wrong_landing_count_complete") is True
        and advisory_input.get("wrong_landing_count_complete") is True
        else None
    )
    posture_axes = {
        "scope": "preregistered overlap bundles only",
        "event_unit": "per-task landed-and-wrong events",
        "all_posture_comparisons_allowed": collision_gate[
            "all_posture_comparisons_allowed"
        ],
        "by_arm": {
            arm: {
                "collision": collision_gate["by_arm"][arm],
                "task_outcomes": overlap_by_arm[arm]["task_outcomes"],
                "wrong_task_landings": axis_inputs[arm][
                    "observed_task_wrong_landings"
                ],
                "wrong_landing_count_complete": axis_inputs[arm][
                    "wrong_landing_count_complete"
                ],
                "agent_minutes": overlap_by_arm[arm]["performance"]["total_agent_minutes"],
                "bundle_wall_seconds": overlap_by_arm[arm]["performance"]["bundle_wall_seconds"],
                "fleet_idle_fraction": overlap_by_arm[arm]["performance"]["fleet_idle_fraction"],
                "capacity_weighted_fleet_idle_fraction": overlap_by_arm[arm]["performance"][
                    "capacity_weighted_fleet_idle_fraction"
                ],
                "merge_conflicts": overlap_by_arm[arm]["performance"]["merge_conflicts"],
            }
            for arm in arms
        },
        "isolate_relative_to_advisory": {
            "additional_agent_minutes": isolate_minutes_delta,
            "difference_in_wrong_task_landings": isolate_wrong_delta,
        },
        "isolate_merge_conflicts": (
            overlap_by_arm["isolate"]["performance"]["merge_conflicts"]
            if "isolate" in overlap_by_arm
            else {
                "expected_observations": 0,
                "observation_count": 0,
                "missing_count": 0,
                "complete": True,
                "sum": None,
                "mean": None,
                "minimum": None,
                "maximum": None,
            }
        ),
    }

    pilot_completed_value = pilot.get("completed_draws")
    completed_draws_matches = (
        pilot_completed_value == len(accepted)
        if isinstance(pilot_completed_value, int) and not isinstance(pilot_completed_value, bool)
        else None
    )
    data_quality = {
        "pilot_completeness": completeness,
        "raw_pilot_completed_draws": pilot_completed_value,
        "raw_pilot_completed_draws_matches_accepted_list": completed_draws_matches,
        "excluded_summaries_embedded_in_pilot_draws": len(embedded_excluded),
        "accepted_draws_with_explicitly_invalid_apparatus": [
            _draw_identity(draw)[0]
            for draw in accepted
            if isinstance(draw.get("apparatus"), Mapping)
            and draw.get("apparatus", {}).get("valid") is False
        ],
    }

    # Keep the clean-null decision literally first: downstream report generation
    # must encounter the validity gate before any posture outcome comparison.
    return {
        "collision_gate": collision_gate,
        "base_task_completion": base_task_completion,
        "outcome_table": outcome_table,
        "exchange_rate": exchange,
        "posture_axes": posture_axes,
        "aggregates": {
            "by_arm": by_arm,
            "by_bundle": by_bundle,
            "by_arm_and_bundle": by_arm_and_bundle,
        },
        "retry_ledger": _retry_ledger(pilot),
        "data_quality": data_quality,
        "schema_version": 1,
        "measurement": "posture-pilot-analysis",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
        "interpretation_limits": {
            "signal": design.get("interpretations", {}).get("signal_limitation")
            if isinstance(design.get("interpretations"), Mapping)
            else None,
            "headline_event_unit": design.get("interpretations", {}).get(
                "headline_event_unit"
            )
            if isinstance(design.get("interpretations"), Mapping)
            else None,
            "bundle_population": design.get("interpretations", {}).get(
                "bundle_population"
            )
            if isinstance(design.get("interpretations"), Mapping)
            else None,
        },
    }


def analyze_files(
    design_path: Path,
    pilot_path: Path,
    output_path: Path,
    *,
    verify_provenance: bool = True,
) -> dict[str, Any]:
    design_path = design_path.resolve()
    pilot_path = pilot_path.resolve()
    output_path = output_path.resolve()
    design_bytes = design_path.read_bytes()
    pilot_bytes = pilot_path.read_bytes()
    design = json.loads(design_bytes.decode("utf-8"))
    pilot = json.loads(pilot_bytes.decode("utf-8"))
    if not isinstance(design, dict) or not isinstance(pilot, dict):
        raise AnalysisError("design and pilot inputs must be JSON objects")
    provenance = _validate_provenance(design_path, pilot) if verify_provenance else None
    result = analyze(design, pilot)
    if design_path.read_bytes() != design_bytes or pilot_path.read_bytes() != pilot_bytes:
        raise AnalysisError("design or pilot changed while analysis was running")
    if verify_provenance and _validate_provenance(design_path, pilot) != provenance:
        raise AnalysisError("sealed provenance changed while analysis was running")
    result["sources"] = {
        "design_path": str(design_path),
        "design_sha256": _sha256_bytes(design_bytes),
        "pilot_path": str(pilot_path),
        "pilot_sha256": _sha256_bytes(pilot_bytes),
        "sealed_provenance": provenance,
    }
    _atomic_json(output_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", type=Path, default=DEFAULT_DESIGN)
    parser.add_argument("--pilot", type=Path, default=DEFAULT_PILOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = analyze_files(args.design, args.pilot, args.output)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "collision_verdict": result["collision_gate"]["verdict"],
                "exchange_rate_computed": result["exchange_rate"]["headline_overall"][
                    "computed"
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
