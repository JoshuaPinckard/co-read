"""Build the Corpus-50 replay report without changing the replay harness.

The script consumes the frozen selection manifest and ledger plus the existing
per-repository replay JSON files.  It is intentionally safe to run while the
corpus is still in progress: incomplete inputs produce an unmistakable draft,
not a fifty-member verdict.

Typical final invocation::

    python analysis/corpus50_report.py

An explicit legacy manifest can be supplied to exercise the analysis against
the first ten stored results while the fifty-member run is incomplete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "corpus" / "CORPUS-50.json"
DEFAULT_LEDGER = ROOT / "corpus" / "CORPUS-50-LEDGER.jsonl"
DEFAULT_RESULTS = ROOT / "exploratory" / "language-hole" / "results"
DEFAULT_STREAMS = ROOT / "exploratory" / "language-hole" / "streams"
DEFAULT_RUN_STATE = ROOT / "exploratory" / "language-hole" / "corpus-50-run.json"
DEFAULT_OUTPUT = ROOT / "exploratory" / "language-hole" / "CORPUS-50.md"
DEFAULT_PLOT_PREFIX = ROOT / "exploratory" / "language-hole" / "CORPUS-50-recall-vs-ground-truth"
RULE_PATH = ROOT / "corpus" / "CORPUS-50-RULE.md"

EXPECTED_RULE_ID = "C50-2026-08-23-v1"
BASELINE_P1 = 0.500
CAP_REACHABLE_THRESHOLD = 20_000
CAP_REPLAY_COUNT = 5_000
STRESS_KEYS = ("config", "catalog", "import", "low_author", "non_english")

BASE_LANGUAGE_QUOTAS = {
    "C/C++": 4,
    "JVM": 4,
    "JS/TS": 5,
    "Python": 5,
    "Go": 4,
    "Rust": 4,
    ".NET": 3,
    "Ruby/PHP": 3,
    "Other/no-code": 3,
}
BASE_LAYOUT_QUOTAS = {
    "artifact/config/docs": 6,
    "manifest monorepo": 8,
    "multi-module tree": 9,
    "single-package tree": 12,
}

MODEL_LABELS = {
    "cochange_time_decayed": "Co-change, time-decayed",
    "cochange_plain_confidence": "Co-change, plain confidence",
    "path_name_similarity": "Path/name similarity",
    "popularity_control": "Popularity control",
    "random_draw": "Random draw",
}

TERMINAL_FAILURE_WORDS = {"failed", "failure", "error", "denied", "aborted"}


class InputError(RuntimeError):
    """Raised when an input would make a rendered number untrustworthy."""


@dataclass(frozen=True)
class Member:
    order: int
    slug: str
    name: str
    raw: Mapping[str, Any]


@dataclass
class LedgerSummary:
    records: list[dict[str, Any]] = field(default_factory=list)
    outcome_counts: Counter[str] = field(default_factory=Counter)
    reason_counts: Counter[str] = field(default_factory=Counter)
    event_counts: Counter[str] = field(default_factory=Counter)
    linkage_errors: list[str] = field(default_factory=list)
    digest_errors: list[str] = field(default_factory=list)


@dataclass
class Observation:
    member: Member
    status: str
    failure_stage: str | None = None
    failure: str | None = None
    result: dict[str, Any] | None = None
    stream: dict[str, Any] | None = None
    mean_ground_truth_size: float | None = None
    capped: bool = False
    cap_reason: str | None = None
    validation_notes: list[str] = field(default_factory=list)

    @property
    def successful(self) -> bool:
        return self.status == "ok" and self.result is not None

    @property
    def comparable(self) -> bool:
        return self.successful and not self.capped


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InputError(f"required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"expected a JSON object in {path}")
    return value


def _coerce_member(raw: Mapping[str, Any], fallback_slug: str, fallback_order: int) -> Member:
    slug = str(raw.get("slug") or fallback_slug).strip()
    name = str(raw.get("name") or slug.replace("__", "/", 1)).strip()
    if not slug or not name:
        raise InputError(f"manifest member at position {fallback_order} lacks slug/name")
    order_value = raw.get("selection_order", raw.get("order", fallback_order))
    try:
        order = int(order_value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"invalid selection order for {name}: {order_value!r}") from exc
    return Member(order=order, slug=slug, name=name, raw=raw)


def load_manifest(path: Path) -> tuple[dict[str, Any], list[Member]]:
    manifest = load_json(path)
    raw_members = manifest.get("members")
    if raw_members is None:
        # Compatibility is useful for validating the analysis on CORPUS.json.
        raw_members = manifest.get("repositories")

    members: list[Member] = []
    if isinstance(raw_members, list):
        for index, raw in enumerate(raw_members, start=1):
            if not isinstance(raw, dict):
                raise InputError(f"manifest member {index} is not an object")
            members.append(_coerce_member(raw, str(raw.get("slug") or ""), index))
    elif isinstance(raw_members, dict):
        order = manifest.get("repository_order")
        keys: list[str]
        if isinstance(order, list):
            ordered = [str(item) for item in order if str(item) in raw_members]
            keys = ordered + sorted(set(raw_members) - set(ordered))
        else:
            keys = sorted(str(key) for key in raw_members)
        for index, slug in enumerate(keys, start=1):
            raw = raw_members[slug]
            if not isinstance(raw, dict):
                raise InputError(f"manifest member {slug} is not an object")
            members.append(_coerce_member(raw, slug, index))
    else:
        raise InputError(f"manifest {path} has neither members[] nor repositories{{}}")

    members.sort(key=lambda item: (item.order, item.slug))
    slugs = [item.slug for item in members]
    names = [item.name.casefold() for item in members]
    if len(set(slugs)) != len(slugs):
        raise InputError("manifest contains duplicate slugs")
    if len(set(names)) != len(names):
        raise InputError("manifest contains duplicate case-folded names")
    return manifest, members


def _canonical_ledger_digest(record: Mapping[str, Any]) -> str:
    """Digest the record using the ledger writer's canonical JSON convention."""

    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    # The ledger specification includes the physical JSONL newline in the
    # digest preimage.
    encoded = (encoded + "\n").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_ledger(path: Path) -> LedgerSummary:
    summary = LedgerSummary()
    if not path.exists():
        summary.linkage_errors.append(f"selection ledger is missing: {path}")
        return summary

    previous_sha: str | None = None
    seen_event_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError(f"invalid ledger JSON at {path}:{line_number}: {exc}") from exc
        if not isinstance(record, dict):
            raise InputError(f"ledger record at {path}:{line_number} is not an object")
        summary.records.append(record)

        event_id = str(record.get("event_id") or "")
        if not event_id:
            summary.linkage_errors.append(f"line {line_number}: missing event_id")
        elif event_id in seen_event_ids:
            summary.linkage_errors.append(f"line {line_number}: duplicate event_id {event_id}")
        seen_event_ids.add(event_id)

        event_type = str(record.get("event_type") or "unknown")
        summary.event_counts[event_type] += 1
        outcome = record.get("outcome")
        if isinstance(outcome, dict):
            status = str(outcome.get("status") or "unknown")
            reason = str(outcome.get("reason") or "unspecified")
            summary.outcome_counts[status] += 1
            if status.casefold() not in {"ok", "selected", "accepted", "success"}:
                summary.reason_counts[reason] += 1

        recorded_previous = record.get("previous_record_sha256")
        if previous_sha is None:
            if recorded_previous not in (None, "", "0" * 64):
                summary.linkage_errors.append(
                    f"line {line_number}: first previous_record_sha256 is not empty"
                )
        elif recorded_previous != previous_sha:
            summary.linkage_errors.append(
                f"line {line_number}: previous_record_sha256 does not match line {line_number - 1}"
            )

        recorded_sha = record.get("record_sha256")
        if not isinstance(recorded_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded_sha):
            summary.linkage_errors.append(f"line {line_number}: invalid record_sha256")
            previous_sha = None
        else:
            expected_sha = _canonical_ledger_digest(record)
            if expected_sha != recorded_sha:
                summary.digest_errors.append(
                    f"line {line_number}: content digest differs under canonical JSON verification"
                )
            previous_sha = recorded_sha
    return summary


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return load_json(path)


def _sha256_and_length(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


def _resolved_recorded_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _number(value: Any, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{description} is not numeric: {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise InputError(f"{description} is not finite: {value!r}")
    return result


def _integer(value: Any, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{description} is not an integer: {value!r}")
    return value


def _close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def validate_success_result(member: Member, result: dict[str, Any]) -> float:
    """Validate sufficient statistics and return query-weighted mean GT size."""

    if result.get("status") != "ok":
        raise InputError(f"internal error: asked to validate non-success result for {member.name}")
    repository = result.get("repository")
    if not isinstance(repository, dict) or repository.get("slug") != member.slug:
        raise InputError(f"result identity mismatch for {member.name}")

    coverage = result.get("coverage")
    commits = result.get("eligible_commits")
    models = result.get("models")
    if not isinstance(coverage, dict) or not isinstance(commits, list) or not isinstance(models, dict):
        raise InputError(f"result schema is incomplete for {member.name}")

    coverage_queries = _integer(coverage.get("query_count"), f"{member.name} coverage.query_count")
    coverage_eligible = _integer(
        coverage.get("eligible_commit_count"), f"{member.name} coverage.eligible_commit_count"
    )
    if coverage_queries <= 0:
        raise InputError(f"successful result has no queries for {member.name}")
    if coverage_eligible != len(commits):
        raise InputError(
            f"eligible commit count mismatch for {member.name}: {coverage_eligible} vs {len(commits)}"
        )

    query_sum = 0
    ground_truth_sum = 0
    per_model: dict[str, Counter[str]] = {key: Counter() for key in MODEL_LABELS}
    per_model_float: dict[str, defaultdict[str, float]] = {
        key: defaultdict(float) for key in MODEL_LABELS
    }
    seen_shas: set[str] = set()
    for position, commit in enumerate(commits):
        if not isinstance(commit, dict):
            raise InputError(f"eligible commit {position} is not an object for {member.name}")
        sha = str(commit.get("sha") or "")
        if not sha or sha in seen_shas:
            raise InputError(f"missing/duplicate eligible commit SHA for {member.name}: {sha!r}")
        seen_shas.add(sha)
        query_count = _integer(commit.get("query_count"), f"{member.name} commit query_count")
        eligible_count = _integer(
            commit.get("eligible_file_count"), f"{member.name} eligible_file_count"
        )
        if query_count != eligible_count or eligible_count < 2:
            raise InputError(
                f"query/eligible-file identity fails for {member.name} at {sha}: "
                f"{query_count} vs {eligible_count}"
            )
        query_sum += query_count
        # Every eligible file is a seed; its ground truth is the other k - 1 files.
        ground_truth_sum += query_count * (eligible_count - 1)

        commit_models = commit.get("models")
        if not isinstance(commit_models, dict):
            raise InputError(f"commit model statistics missing for {member.name} at {sha}")
        for model_key in MODEL_LABELS:
            stats = commit_models.get(model_key)
            if not isinstance(stats, dict):
                raise InputError(f"{model_key} commit statistics missing for {member.name} at {sha}")
            for key in ("empty_queries", "p1_hits", "p10_hits"):
                value = _integer(stats.get(key), f"{member.name} {model_key} {key}")
                if value < 0:
                    raise InputError(f"negative {key} for {member.name} at {sha}")
                per_model[model_key][key] += value
            for key in ("r10_sum", "r20_sum"):
                value = _number(stats.get(key), f"{member.name} {model_key} {key}")
                per_model_float[model_key][key] += value

    if query_sum != coverage_queries:
        raise InputError(
            f"query total mismatch for {member.name}: coverage={coverage_queries}, commits={query_sum}"
        )

    for model_key in MODEL_LABELS:
        top = models.get(model_key)
        if not isinstance(top, dict):
            raise InputError(f"top-level {model_key} metrics missing for {member.name}")
        expected = {
            "queries": float(coverage_queries),
            "empty_queries": float(per_model[model_key]["empty_queries"]),
            "p1_hits": float(per_model[model_key]["p1_hits"]),
            "p10_hits": float(per_model[model_key]["p10_hits"]),
            "r10_sum": per_model_float[model_key]["r10_sum"],
            "r20_sum": per_model_float[model_key]["r20_sum"],
            "p_at_1": per_model[model_key]["p1_hits"] / coverage_queries,
            "p_at_10": per_model[model_key]["p10_hits"] / (10 * coverage_queries),
            "r_at_10": per_model_float[model_key]["r10_sum"] / coverage_queries,
            "r_at_20": per_model_float[model_key]["r20_sum"] / coverage_queries,
            "empty_radius_rate": per_model[model_key]["empty_queries"] / coverage_queries,
        }
        for key, expected_value in expected.items():
            actual_value = _number(top.get(key), f"{member.name} {model_key} {key}")
            if not _close(actual_value, expected_value):
                raise InputError(
                    f"metric reconstruction failed for {member.name} {model_key} {key}: "
                    f"stored={actual_value}, reconstructed={expected_value}"
                )
        for key in ("median_query_microseconds", "min_query_microseconds", "max_query_microseconds"):
            if key in top:
                value = _number(top[key], f"{member.name} {model_key} {key}")
                if value < 0:
                    raise InputError(f"negative timing for {member.name} {model_key}")

    return ground_truth_sum / coverage_queries


def _terminal_failure_from_member(member: Member) -> tuple[str, str] | None:
    for stage_key, label in (
        ("replay_status", "replay"),
        ("extraction_status", "extraction"),
        ("extract_status", "extraction"),
        ("clone_status", "clone"),
        ("run_status", "run"),
    ):
        status = member.raw.get(stage_key)
        if status is None:
            continue
        normalized = str(status).casefold()
        if any(word in normalized for word in TERMINAL_FAILURE_WORDS):
            reason = str(
                member.raw.get(f"{label}_failure")
                or member.raw.get("failure")
                or f"manifest records {stage_key}={status}"
            )
            return label, reason
    return None


def _run_state_record(
    run_state: Mapping[str, Any] | None, member: Member
) -> Mapping[str, Any]:
    if not isinstance(run_state, Mapping):
        return {}
    repositories = run_state.get("repositories")
    if not isinstance(repositories, Mapping):
        return {}
    record = repositories.get(member.slug)
    return record if isinstance(record, Mapping) else {}


def _terminal_failure_from_run_state(
    run_state: Mapping[str, Any] | None, member: Member
) -> tuple[str, str] | None:
    record = _run_state_record(run_state, member)
    stages = record.get("stages")
    if not isinstance(stages, Mapping):
        return None
    for stage in ("clone", "extract", "replay"):
        stage_record = stages.get(stage)
        if not isinstance(stage_record, Mapping):
            continue
        status = str(stage_record.get("status") or "").casefold()
        if status == "failed" or any(word in status for word in TERMINAL_FAILURE_WORDS):
            failure = str(
                stage_record.get("failure")
                or stage_record.get("failure_type")
                or f"run state records {stage} status={status}"
            )
            return stage, failure
    return None


def collect_observations(
    members: Sequence[Member],
    results_dir: Path,
    streams_dir: Path,
    run_state: Mapping[str, Any] | None = None,
) -> list[Observation]:
    observations: list[Observation] = []
    successful_hashes: set[str] = set()

    for member in members:
        result = _read_optional_json(results_dir / f"{member.slug}.json")
        stream = _read_optional_json(streams_dir / f"{member.slug}.meta.json")
        state_record = _run_state_record(run_state, member)
        state_cap = state_record.get("cap") if isinstance(state_record.get("cap"), Mapping) else {}
        observation = Observation(member=member, status="pending", result=result, stream=stream)

        if isinstance(result, dict) and isinstance(result.get("repository"), dict):
            result_slug = result["repository"].get("slug")
            if result_slug and result_slug != member.slug:
                raise InputError(f"result identity mismatch for {member.name}: {result_slug!r}")
        if isinstance(stream, dict) and isinstance(stream.get("repository"), dict):
            stream_slug = stream["repository"].get("slug")
            if stream_slug and stream_slug != member.slug:
                raise InputError(f"stream identity mismatch for {member.name}: {stream_slug!r}")

        if result is not None and result.get("status") == "ok":
            observation.mean_ground_truth_size = validate_success_result(member, result)
            observation.status = "ok"
            if stream is None:
                observation.validation_notes.append(
                    "successful result lacks stream metadata; extraction and cap provenance are unavailable"
                )
            elif stream.get("status") != "ok":
                raise InputError(
                    f"successful replay conflicts with stream status for {member.name}: "
                    f"{stream.get('status')!r}"
                )
            implementation = result.get("implementation")
            harness_hash = (
                implementation.get("harness_sha256")
                if isinstance(implementation, dict)
                else None
            )
            if not isinstance(harness_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", harness_hash
            ):
                observation.validation_notes.append(
                    "successful result lacks a valid replay harness SHA-256"
                )
            else:
                successful_hashes.add(harness_hash)
        elif result is not None:
            observation.status = "failed"
            recorded_stage = result.get("failure_stage")
            if recorded_stage:
                observation.failure_stage = str(recorded_stage)
            elif stream is not None and stream.get("status") != "ok":
                observation.failure_stage = "extraction"
            else:
                observation.failure_stage = "replay"
            observation.failure = str(result.get("failure") or f"result status={result.get('status')}")
        elif stream is not None and stream.get("status") != "ok":
            observation.status = "failed"
            observation.failure_stage = "extraction"
            observation.failure = str(stream.get("failure") or f"stream status={stream.get('status')}")
        else:
            recorded_failure = _terminal_failure_from_run_state(run_state, member)
            if recorded_failure is None:
                recorded_failure = _terminal_failure_from_member(member)
            if recorded_failure:
                observation.status = "failed"
                observation.failure_stage, observation.failure = recorded_failure

        coverage = result.get("coverage", {}) if isinstance(result, dict) else {}
        capped_signals = [
            bool(member.raw.get("capped")),
            bool(stream.get("capped")) if isinstance(stream, dict) else False,
            bool(coverage.get("left_truncated")) if isinstance(coverage, dict) else False,
            bool(state_cap.get("applied")) if isinstance(state_cap, Mapping) else False,
        ]
        observation.capped = any(capped_signals)
        cap_reasons = [
            member.raw.get("cap_reason"),
            stream.get("cap_reason") if isinstance(stream, dict) else None,
            result.get("protocol", {}).get("cap")
            if isinstance(result, dict) and isinstance(result.get("protocol"), dict)
            else None,
            (
                f"run-state cap: reachable history {state_cap.get('reachable_commit_count')} > "
                f"{state_cap.get('threshold_reachable_commits')}; most recent "
                f"{state_cap.get('replay_commits')} replayed, learned indexes start empty"
                if isinstance(state_cap, Mapping) and state_cap.get("applied")
                else None
            ),
        ]
        observation.cap_reason = next((str(value) for value in cap_reasons if value), None)
        if observation.capped and not observation.cap_reason:
            observation.validation_notes.append("cap is flagged but no cap reason was logged")

        reachable_sources = {
            "manifest": member.raw.get("reachable_commit_count"),
            "stream": stream.get("reachable_commit_count") if isinstance(stream, dict) else None,
            "run state": (
                state_cap.get("reachable_commit_count")
                if isinstance(state_cap, Mapping)
                else None
            ),
        }
        reachable_values = {
            label: value
            for label, value in reachable_sources.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if len(set(reachable_values.values())) > 1:
            observation.validation_notes.append(
                "reachable-commit counts disagree across provenance: "
                + ", ".join(f"{label}={value}" for label, value in reachable_values.items())
            )
        reachable = next(iter(reachable_values.values()), None)
        if reachable is not None:
            expected_cap = reachable > CAP_REACHABLE_THRESHOLD
            if observation.capped != expected_cap:
                observation.validation_notes.append(
                    f"cap decision is {observation.capped!r}, expected {expected_cap!r} from "
                    f"reachable count {reachable:,} and threshold {CAP_REACHABLE_THRESHOLD:,}"
                )
            for label, value in (
                ("manifest", member.raw.get("capped")),
                ("stream", stream.get("capped") if isinstance(stream, dict) else None),
                (
                    "result coverage",
                    coverage.get("left_truncated") if isinstance(coverage, dict) else None,
                ),
                (
                    "run state",
                    state_cap.get("applied") if isinstance(state_cap, Mapping) else None,
                ),
            ):
                if value is not None and value is not expected_cap:
                    observation.validation_notes.append(
                        f"{label} cap flag is {value!r}, expected {expected_cap!r}"
                    )

        first_parent_sources = {
            "manifest": member.raw.get("first_parent_commit_count"),
            "stream": (
                stream.get("first_parent_commit_count") if isinstance(stream, dict) else None
            ),
            "result coverage": (
                coverage.get("first_parent_commits_at_head")
                if isinstance(coverage, dict)
                else None
            ),
        }
        first_parent_values = {
            label: value
            for label, value in first_parent_sources.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if len(set(first_parent_values.values())) > 1:
            observation.validation_notes.append(
                "first-parent counts disagree across provenance: "
                + ", ".join(
                    f"{label}={value}" for label, value in first_parent_values.items()
                )
            )
        first_parent = next(iter(first_parent_values.values()), None)

        if observation.successful and reachable is not None:
            expected_cap = reachable > CAP_REACHABLE_THRESHOLD
            replayed = coverage.get("commits_replayed")
            expected_replayed = CAP_REPLAY_COUNT if expected_cap else first_parent
            if replayed != expected_replayed:
                observation.validation_notes.append(
                    f"result replays {replayed!r}, expected {expected_replayed!r} from the cap decision"
                )
            left_truncated = coverage.get("left_truncated")
            if left_truncated is not expected_cap:
                observation.validation_notes.append(
                    f"coverage.left_truncated is {left_truncated!r}, expected {expected_cap!r}"
                )
            protocol = result.get("protocol")
            protocol_cap = protocol.get("cap") if isinstance(protocol, dict) else None
            if expected_cap:
                normalized_cap = str(protocol_cap or "").casefold()
                if not (
                    "learned indexes start empty" in normalized_cap
                    or "learned indexes started empty" in normalized_cap
                ):
                    observation.validation_notes.append(
                        "capped result does not attest that learned indexes start empty"
                    )

        if isinstance(state_cap, Mapping) and state_cap:
            expected_cap = bool(reachable is not None and reachable > CAP_REACHABLE_THRESHOLD)
            expected_state = {
                "threshold_reachable_commits": CAP_REACHABLE_THRESHOLD,
                "replay_commits": CAP_REPLAY_COUNT if expected_cap else first_parent,
                "left_truncated": expected_cap,
                "learned_indexes_start_empty": expected_cap,
                "non_comparable_for_warm_history_claims": expected_cap,
            }
            for key, expected in expected_state.items():
                if state_cap.get(key) != expected:
                    observation.validation_notes.append(
                        f"run-state cap.{key} is {state_cap.get(key)!r}, expected {expected!r}"
                    )

        manifest_head = member.raw.get("head") or member.raw.get("resolved_head_sha")
        result_head = result.get("source_head_sha") if isinstance(result, dict) else None
        stream_head = stream.get("source_head_sha") if isinstance(stream, dict) else None
        if observation.successful and not result_head:
            observation.validation_notes.append("successful result lacks source_head_sha")
        if observation.successful and not stream_head:
            observation.validation_notes.append("successful stream lacks source_head_sha")
        heads = {str(value) for value in (manifest_head, result_head, stream_head) if value}
        if len(heads) > 1:
            raise InputError(f"source HEAD mismatch for {member.name}: {sorted(heads)}")

        observations.append(observation)

    if len(successful_hashes) > 1:
        raise InputError(f"refusing to mix replay harness hashes: {sorted(successful_hashes)}")
    return observations


def quantile(values: Sequence[float], probability: float) -> float | None:
    """Hyndman-Fan type 7 quantile, matching NumPy's default linear method."""

    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * probability
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right):
        raise ValueError("correlation inputs differ in length")
    if len(left) < 2:
        return None
    left_mean = math.fsum(left) / len(left)
    right_mean = math.fsum(right) / len(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    numerator = math.fsum(a * b for a, b in zip(left_delta, right_delta))
    left_ss = math.fsum(value * value for value in left_delta)
    right_ss = math.fsum(value * value for value in right_delta)
    if left_ss == 0 or right_ss == 0:
        return None
    return numerator / math.sqrt(left_ss * right_ss)


def _average_ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        for position in range(start, end):
            ranks[order[position]] = average_rank
        start = end
    return ranks


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right):
        raise ValueError("correlation inputs differ in length")
    if len(left) < 2:
        return None
    return pearson(_average_ranks(left), _average_ranks(right))


def _result_model(observation: Observation, model_key: str) -> Mapping[str, Any]:
    assert observation.result is not None
    models = observation.result["models"]
    return models[model_key]


def _model_metric(observation: Observation, model_key: str, metric_key: str) -> float:
    return float(_result_model(observation, model_key)[metric_key])


def _p1_values(observations: Iterable[Observation]) -> list[float]:
    return [
        _model_metric(item, "cochange_time_decayed", "p_at_1")
        for item in observations
        if item.successful
    ]


def _fmt(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _fmt_signed(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def _multiple(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}×"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100.0 * value:.1f}%"


def _integer_text(value: Any) -> str:
    return f"{value:,}" if isinstance(value, int) else "—"


def _escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _human_bytes(value: Any) -> str:
    if not isinstance(value, int) or value < 0:
        return "not recorded"
    gib = value / (1024**3)
    return f"{gib:g} GiB ({value:,} bytes)"


def _scope_name(manifest: Mapping[str, Any], member_count: int) -> str:
    rule_id = str(manifest.get("rule_id") or EXPECTED_RULE_ID)
    supplied = manifest.get("scope_name")
    required_prefix = f"50 repositories drawn under Rule {rule_id}"
    if isinstance(supplied, str) and supplied.startswith(required_prefix):
        return supplied
    if member_count == 50:
        return required_prefix
    return (
        f"the incomplete {member_count}-member draft for the planned 50 repositories "
        f"drawn under Rule {rule_id}"
    )


def _display_listing_dates(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{key}: {value[key]}" for key in sorted(value))
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value) if value not in (None, "") else "not recorded"


def _shape(member: Member) -> str:
    parts: list[str] = []
    stress_key = member.raw.get("stress_key")
    if stress_key:
        parts.append(f"stress={stress_key}")
    language = member.raw.get("language_stratum") or member.raw.get("primary_language")
    if language:
        parts.append(str(language))
    layout = member.raw.get("layout_stratum")
    if layout:
        parts.append(str(layout))
    expected = member.raw.get("expected_stress") or member.raw.get("axis")
    if expected and not parts:
        parts.append(str(expected))
    return "; ".join(parts) if parts else "shape not recorded"


def _stratum_counts(members: Sequence[Member], key: str) -> Counter[str]:
    return Counter(str(member.raw.get(key) or "not recorded") for member in members)


def _status_counts(observations: Sequence[Observation]) -> Counter[str]:
    return Counter(item.status for item in observations)


def _metadata_completeness(manifest: Mapping[str, Any], members: Sequence[Member]) -> list[str]:
    issues: list[str] = []
    if len(members) != 50:
        issues.append(f"manifest contains {len(members)} members, not 50")
    if [member.order for member in members] != list(range(1, len(members) + 1)):
        issues.append("selection_order is not the contiguous 1-based manifest order")
    if manifest.get("rule_id") != EXPECTED_RULE_ID:
        issues.append(
            f"rule_id is {manifest.get('rule_id')!r}, expected {EXPECTED_RULE_ID!r}"
        )
    if not manifest.get("seed"):
        issues.append("seed is not recorded in the manifest")
    if not manifest.get("listing_dates"):
        issues.append("listing_dates are not recorded in the manifest")
    if manifest.get("disk_cap_bytes") != 20 * 1024**3:
        issues.append("manifest does not record the fixed 20 GiB disk cap")

    cohorts = Counter(str(member.raw.get("cohort") or "not recorded") for member in members)
    expected_cohorts = {"retained_anchor": 10, "stress": 5, "base": 35}
    for cohort, expected in expected_cohorts.items():
        if cohorts[cohort] != expected:
            issues.append(f"cohort {cohort} has {cohorts[cohort]} members, expected {expected}")

    for member in members:
        if member.raw.get("selection_status") != "selected":
            issues.append(f"{member.name} is not marked selection_status=selected")
        if not (member.raw.get("head") or member.raw.get("resolved_head_sha")):
            issues.append(f"{member.name} lacks a frozen HEAD")
        first_parent_count = member.raw.get("first_parent_commit_count")
        if not isinstance(first_parent_count, int) or first_parent_count < 500:
            issues.append(f"{member.name} lacks a valid >=500 first-parent commit count")
        if not isinstance(member.raw.get("reachable_commit_count"), int):
            issues.append(f"{member.name} lacks reachable_commit_count for the cap decision")
        if not member.raw.get("language_stratum"):
            issues.append(f"{member.name} lacks language_stratum")
        if not member.raw.get("layout_stratum"):
            issues.append(f"{member.name} lacks layout_stratum")

    stress_counts = Counter(
        str(member.raw.get("stress_key") or "not recorded")
        for member in members
        if member.raw.get("cohort") == "stress"
    )
    expected_stress_counts = Counter({key: 1 for key in STRESS_KEYS})
    if stress_counts != expected_stress_counts:
        issues.append(
            "stress members do not realise exactly one each of " + ", ".join(STRESS_KEYS)
        )
    return issues


def _git_blob_digest(path: Path, recorded: str) -> str | None:
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", recorded):
        return None
    # The selector freezes `git hash-object --path=...`, so reproduce Git's
    # text normalization rather than hashing the CRLF working-tree bytes.
    payload = path.read_bytes().replace(b"\r\n", b"\n")
    preimage = b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
    algorithm = hashlib.sha1 if len(recorded) == 40 else hashlib.sha256
    return algorithm(preimage).hexdigest()


def _selection_provenance_issues(
    manifest: Mapping[str, Any],
    members: Sequence[Member],
    ledger: LedgerSummary,
    ledger_path: Path,
) -> list[str]:
    """Validate the selector's durable cross-file attestation before finalisation."""

    issues: list[str] = []
    recorded = manifest.get("selection_provenance")
    if not isinstance(recorded, Mapping):
        return ["manifest lacks selection_provenance"]

    provenance_path = _resolved_recorded_path(recorded.get("path"))
    provenance: Mapping[str, Any] | None = None
    if provenance_path is None:
        issues.append("selection_provenance.path is missing")
    elif not provenance_path.exists():
        issues.append(f"selection provenance file is missing: {provenance_path}")
    else:
        try:
            digest, length = _sha256_and_length(provenance_path)
        except OSError as exc:
            issues.append(f"selection provenance file cannot be read: {exc}")
        else:
            if recorded.get("sha256") != digest:
                issues.append("selection provenance SHA-256 differs from the manifest attestation")
            if recorded.get("byte_length") != length:
                issues.append("selection provenance byte length differs from the manifest attestation")
            try:
                loaded = json.loads(provenance_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                issues.append(f"selection provenance JSON cannot be read: {exc}")
            else:
                if isinstance(loaded, dict):
                    provenance = loaded
                else:
                    issues.append("selection provenance JSON is not an object")

    recorded_ledger_path = _resolved_recorded_path(recorded.get("ledger_path"))
    if recorded_ledger_path is None:
        issues.append("selection_provenance.ledger_path is missing")
    elif not _same_path(recorded_ledger_path, ledger_path):
        issues.append(
            "selection provenance ledger path does not identify the ledger used by this report"
        )
    if not ledger_path.exists():
        issues.append(f"selection ledger is missing for raw-file attestation: {ledger_path}")
    else:
        try:
            ledger_digest, ledger_length = _sha256_and_length(ledger_path)
        except OSError as exc:
            issues.append(f"selection ledger cannot be read for attestation: {exc}")
        else:
            if recorded.get("ledger_sha256") != ledger_digest:
                issues.append("current selection-ledger SHA-256 differs from the final attestation")
            if recorded.get("ledger_byte_length") != ledger_length:
                issues.append("current selection-ledger byte length differs from the final attestation")

    verification = recorded.get("ledger_verification")
    if not isinstance(verification, Mapping):
        issues.append("selection provenance lacks ledger_verification")
    else:
        last_sha = ledger.records[-1].get("record_sha256") if ledger.records else None
        expected_verification = {
            "valid": not ledger.linkage_errors and not ledger.digest_errors,
            "records": len(ledger.records),
            "last_record_sha256": last_sha,
        }
        for key, expected in expected_verification.items():
            if verification.get(key) != expected:
                issues.append(
                    f"attested ledger_verification.{key} is {verification.get(key)!r}, "
                    f"expected {expected!r}"
                )
    wrong_rule_records = [
        str(item.get("event_id") or "unknown")
        for item in ledger.records
        if item.get("rule_id") != manifest.get("rule_id")
    ]
    if wrong_rule_records:
        issues.append(
            "selection-ledger records have a missing/wrong rule_id: "
            + ", ".join(wrong_rule_records[:10])
        )

    if provenance is None:
        return issues
    if provenance.get("rule_id") != manifest.get("rule_id"):
        issues.append("selection provenance rule_id differs from the manifest")
    if provenance.get("ledger_verification") != recorded.get("ledger_verification"):
        issues.append(
            "selection provenance ledger_verification differs from its manifest copy"
        )

    stress_evidence = provenance.get("stress")
    stress_members = {
        str(member.raw.get("stress_key")): member
        for member in members
        if member.raw.get("cohort") == "stress"
    }
    if not isinstance(stress_evidence, Mapping) or set(stress_evidence) != set(STRESS_KEYS):
        issues.append("selection provenance does not attest all five fixed stress keys")
    else:
        for key in STRESS_KEYS:
            evidence = stress_evidence.get(key)
            member = stress_members.get(key)
            if not isinstance(evidence, Mapping) or member is None:
                issues.append(f"stress attestation cannot be reconciled for {key}")
                continue
            if evidence.get("repo_id") != member.raw.get("repo_id"):
                issues.append(f"stress attestation repo_id differs for {key}")
            rank = evidence.get("candidate_order")
            if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
                issues.append(f"stress attestation has invalid deterministic rank for {key}")
            else:
                if evidence.get("prior_rejection_count") != rank - 1:
                    issues.append(f"stress attestation rejection prefix differs for {key}")
                terminal_count = evidence.get("terminal_event_count")
                if (
                    not isinstance(terminal_count, int)
                    or isinstance(terminal_count, bool)
                    or terminal_count < rank
                ):
                    issues.append(f"stress attestation terminal-event count is invalid for {key}")

    base_evidence = provenance.get("base")
    manifest_base_ids = [
        member.raw.get("repo_id") for member in members if member.raw.get("cohort") == "base"
    ]
    if not isinstance(base_evidence, Mapping):
        issues.append("selection provenance lacks base-selection evidence")
    else:
        if base_evidence.get("selected_count") != 35:
            issues.append("selection provenance does not attest 35 base additions")
        if base_evidence.get("selected_repo_ids") != manifest_base_ids:
            issues.append("base selected_repo_ids differ between provenance and manifest order")

    anchor_evidence = provenance.get("retained_anchors")
    verified_anchors = (
        anchor_evidence.get("verified") if isinstance(anchor_evidence, Mapping) else None
    )
    manifest_anchors = {
        member.name: member for member in members if member.raw.get("cohort") == "retained_anchor"
    }
    if not isinstance(verified_anchors, Mapping) or set(verified_anchors) != set(
        manifest_anchors
    ):
        issues.append("retained-anchor attestation names differ from the manifest")
    else:
        for name, member in manifest_anchors.items():
            evidence = verified_anchors.get(name)
            if not isinstance(evidence, Mapping):
                issues.append(f"retained-anchor attestation is malformed for {name}")
                continue
            expected = {
                "head": member.raw.get("head") or member.raw.get("resolved_head_sha"),
                "first_parent_commit_count": member.raw.get("first_parent_commit_count"),
                "reachable_commit_count": member.raw.get("reachable_commit_count"),
            }
            observed = {key: evidence.get(key) for key in expected}
            if observed != expected:
                issues.append(f"retained-anchor freeze evidence differs for {name}")

    attested_rule = provenance.get("rule_freeze")
    manifest_rule = manifest.get("rule_freeze_provenance")
    if not isinstance(attested_rule, Mapping) or manifest_rule != attested_rule:
        issues.append("rule_freeze_provenance differs from the selection attestation")
    else:
        rule_path = _resolved_recorded_path(attested_rule.get("rule_path"))
        if rule_path is None or not _same_path(rule_path, RULE_PATH):
            issues.append("rule-freeze evidence does not identify CORPUS-50-RULE.md")
        elif not rule_path.exists():
            issues.append("the attested rule file is missing")
        else:
            recorded_blob = str(attested_rule.get("git_blob") or "")
            actual_blob = _git_blob_digest(rule_path, recorded_blob)
            if actual_blob != recorded_blob:
                issues.append("current rule content differs from the attested frozen Git blob")
        if attested_rule.get("committed_before_selection_verified") is not True:
            issues.append("rule-freeze evidence does not verify recording before selection")
        for key in ("git_commit", "committed_at", "first_selection_event_at_utc"):
            if not attested_rule.get(key):
                issues.append(f"rule-freeze evidence lacks {key}")
    return issues


def _relative_link(from_path: Path, target: Path) -> str:
    relative = os.path.relpath(target, start=from_path.parent)
    return Path(relative).as_posix()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _plot_points(observations: Sequence[Observation]) -> list[Observation]:
    return [
        item
        for item in observations
        if item.successful and item.mean_ground_truth_size is not None
    ]


def write_scatter_plot(observations: Sequence[Observation], prefix: Path) -> tuple[Path, Path]:
    """Write deterministic-shape PNG and SVG versions of the requested scatter."""

    try:
        import matplotlib

        matplotlib.use("Agg")
        matplotlib.rcParams["svg.hashsalt"] = "blast-radius-corpus-50-v1"
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise InputError("matplotlib is required to render the Corpus-50 plots") from exc

    points = _plot_points(observations)
    figure, axis = plt.subplots(figsize=(8.4, 5.3), constrained_layout=True)
    uncapped = [item for item in points if not item.capped]
    capped = [item for item in points if item.capped]

    def values(items: Sequence[Observation]) -> tuple[list[float], list[float]]:
        return (
            [float(item.mean_ground_truth_size) for item in items],
            [_model_metric(item, "cochange_time_decayed", "r_at_10") for item in items],
        )

    if uncapped:
        x_values, y_values = values(uncapped)
        axis.scatter(
            x_values,
            y_values,
            s=42,
            color="#176B87",
            alpha=0.82,
            edgecolor="white",
            linewidth=0.5,
            label="full-history result",
        )
    if capped:
        x_values, y_values = values(capped)
        axis.scatter(
            x_values,
            y_values,
            s=64,
            marker="^",
            facecolor="none",
            edgecolor="#B23A48",
            linewidth=1.4,
            label="left-truncated (non-comparable)",
        )
    if not points:
        axis.text(0.5, 0.5, "No successful replay results yet", ha="center", va="center")

    axis.set_xscale("log")
    axis.set_xlabel("Query-weighted mean ground-truth set size (log scale)")
    axis.set_ylabel("Time-decayed co-change R@10")
    axis.set_title("Recall@10 and ground-truth set size")
    axis.grid(True, which="both", alpha=0.22, linewidth=0.7)
    axis.set_ylim(bottom=0)
    if uncapped or capped:
        axis.legend(frameon=False)

    png_path = prefix.with_suffix(".png")
    svg_path = prefix.with_suffix(".svg")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    for output_path, file_format in ((png_path, "png"), (svg_path, "svg")):
        with tempfile.NamedTemporaryFile(dir=output_path.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            figure.savefig(
                temporary,
                format=file_format,
                dpi=180 if file_format == "png" else None,
                metadata={"Date": None, "Creator": "analysis/corpus50_report.py"},
            )
            os.replace(temporary, output_path)
        finally:
            if temporary.exists():
                temporary.unlink()
    plt.close(figure)
    return png_path, svg_path


def _correlations(points: Sequence[Observation]) -> dict[str, float | None]:
    gt = [float(item.mean_ground_truth_size) for item in points]
    recall = [_model_metric(item, "cochange_time_decayed", "r_at_10") for item in points]
    return {
        "pearson_raw": pearson(gt, recall),
        "pearson_log": pearson([math.log(value) for value in gt], recall),
        "spearman": spearman(gt, recall),
    }


def _lift_values(item: Observation) -> tuple[float | None, float]:
    cochange = _model_metric(item, "cochange_time_decayed", "r_at_10")
    popularity = _model_metric(item, "popularity_control", "r_at_10")
    ratio = None if popularity == 0 else cochange / popularity
    return ratio, cochange - popularity


def _rank_terciles(points: Sequence[Observation]) -> list[tuple[str, list[Observation]]]:
    ordered = sorted(points, key=lambda item: (float(item.mean_ground_truth_size), item.member.name))
    first = len(ordered) // 3
    second = 2 * len(ordered) // 3
    return [
        ("Smallest third", ordered[:first]),
        ("Middle third", ordered[first:second]),
        ("Largest third", ordered[second:]),
    ]


def _render_distribution_row(label: str, observations: Sequence[Observation]) -> str:
    values = _p1_values(observations)
    above = sum(value > BASELINE_P1 for value in values)
    return (
        f"| {label} | {len(values)} | {_fmt(quantile(values, 0.0))} | "
        f"{_fmt(quantile(values, 0.25))} | {_fmt(quantile(values, 0.5))} | "
        f"{_fmt(quantile(values, 0.75))} | {_fmt(quantile(values, 1.0))} | "
        f"{above}/{len(values) if values else 0} |"
    )


def _render_strata(lines: list[str], members: Sequence[Member], key: str, label: str) -> None:
    all_counts = _stratum_counts(members, key)
    base_counts = _stratum_counts(
        [member for member in members if member.raw.get("cohort") == "base"], key
    )
    quotas = BASE_LANGUAGE_QUOTAS if key == "language_stratum" else BASE_LAYOUT_QUOTAS
    lines.extend(
        [
            f"### Realised {label} strata",
            "",
            "| Stratum | All selected members | Seeded base additions | Base target | Deviation |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for stratum in sorted(set(all_counts) | set(quotas)):
        target = quotas.get(stratum)
        deviation = None if target is None else base_counts[stratum] - target
        lines.append(
            f"| {_escape(stratum)} | {all_counts[stratum]} | {base_counts[stratum]} | "
            f"{target if target is not None else '—'} | "
            f"{_fmt_signed(float(deviation), 0) if deviation is not None else '—'} |"
        )
    lines.append("")


def _render_stress_strata(lines: list[str], members: Sequence[Member]) -> None:
    counts = Counter(
        str(member.raw.get("stress_key") or "not recorded")
        for member in members
        if member.raw.get("cohort") == "stress"
    )
    lines.extend(
        [
            "### Realised fixed stress-shape strata",
            "",
            "| Stress key | Realised members | Fixed target |",
            "|---|---:|---:|",
        ]
    )
    for key in sorted(set(counts) | set(STRESS_KEYS)):
        lines.append(f"| {_escape(key)} | {counts[key]} | {1 if key in STRESS_KEYS else '—'} |")
    lines.append("")


def _anchor_correction(observations: Sequence[Observation]) -> str:
    anchors = [
        item
        for item in observations
        if item.member.raw.get("cohort") == "retained_anchor" and item.successful
    ]
    uncapped = [item for item in anchors if not item.capped]
    all_values = _p1_values(anchors)
    uncapped_values = _p1_values(uncapped)
    if not all_values:
        return (
            "The ten-member premise cannot yet be checked from the retained anchors because none "
            "has a successful stored replay."
        )
    all_above = sum(value > BASELINE_P1 for value in all_values)
    uncapped_above = sum(value > BASELINE_P1 for value in uncapped_values)
    correction = (
        f"**Correction to the ten-member premise:** the {len(all_values)} measured retained "
        f"anchors span **{min(all_values):.3f}–{max(all_values):.3f}**, with "
        f"**{all_above}/{len(all_values)}** strictly above 0.500."
    )
    if uncapped_values:
        correction += (
            f" The {len(uncapped_values)} full-history retained anchors span "
            f"**{min(uncapped_values):.3f}–{max(uncapped_values):.3f}**, with "
            f"**{uncapped_above}/{len(uncapped_values)}** above 0.500. These are different "
            "analysis sets; the full-history range must not be combined with the all-anchor count."
        )
    return correction


def _render_progress(
    lines: list[str], observations: Sequence[Observation], completeness_issues: Sequence[str]
) -> None:
    counts = _status_counts(observations)
    lines.extend(
        [
            "## Run progress and report status",
            "",
            "| Selected members | Successful replays | Recorded terminal failures | Pending/missing |",
            "|---:|---:|---:|---:|",
            f"| {len(observations)} | {counts['ok']} | {counts['failed']} | {counts['pending']} |",
            "",
        ]
    )
    if completeness_issues:
        lines.append(
            "**This is an incomplete draft, not a 50-member verdict.** The following conditions prevent finalisation:"
        )
        lines.append("")
        for issue in completeness_issues:
            lines.append(f"- {_escape(issue)}")
        lines.append("")
    else:
        lines.append(
            "All 50 selected members have a successful replay or an explicit terminal failure; the report is complete with respect to the recorded run."
        )
        lines.append("")


def render_report(
    manifest: Mapping[str, Any],
    members: Sequence[Member],
    ledger: LedgerSummary,
    observations: Sequence[Observation],
    output_path: Path,
    ledger_path: Path,
    png_path: Path,
    svg_path: Path,
) -> tuple[str, bool]:
    rule_id = str(manifest.get("rule_id") or EXPECTED_RULE_ID)
    scope = _scope_name(manifest, len(members))
    status_counts = _status_counts(observations)
    metadata_issues = _metadata_completeness(manifest, members)
    completeness_issues = list(metadata_issues)
    completeness_issues.extend(
        _selection_provenance_issues(manifest, members, ledger, ledger_path)
    )
    if status_counts["pending"]:
        completeness_issues.append(f"{status_counts['pending']} selected members are pending or missing results")
    if not ledger.records:
        completeness_issues.append("selection ledger has no records")
    completeness_issues.extend(ledger.linkage_errors)
    completeness_issues.extend(ledger.digest_errors)
    for item in observations:
        completeness_issues.extend(f"{item.member.name}: {note}" for note in item.validation_notes)
    complete = not completeness_issues

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    lines: list[str] = [
        "# Corpus-50 co-change replay",
        "",
        f"Generated at `{now}` by `analysis/corpus50_report.py`.",
        "",
        f"Scope: **{_escape(scope)}**. Every aggregate and verdict below is limited to that named scope.",
        "",
    ]
    _render_progress(lines, observations, completeness_issues)

    selection_attestation = manifest.get("selection_provenance")
    selection_attestation_text = (
        f"`{_escape(selection_attestation.get('path'))}`; SHA-256 "
        f"`{_escape(selection_attestation.get('sha256'))}`; "
        f"{_integer_text(selection_attestation.get('byte_length'))} bytes"
        if isinstance(selection_attestation, Mapping)
        else "not recorded"
    )
    lines.extend(
        [
            "## Frozen sampling rule and realised corpus",
            "",
            f"- Rule: **{_escape(rule_id)}** ([frozen rule]({_relative_link(output_path, RULE_PATH)}))",
            f"- Seed: `{_escape(manifest.get('seed') or 'not recorded')}`",
            f"- Dated listings: {_escape(_display_listing_dates(manifest.get('listing_dates')))}",
            f"- Combined acquisition-and-artifact disk cap: **{_human_bytes(manifest.get('disk_cap_bytes'))}**",
            f"- Selection ledger: [{ledger_path.name}]({_relative_link(output_path, ledger_path)})",
            f"- Final selection attestation: {selection_attestation_text}",
            "",
            "The frozen rule retains ten named anchors at their verified heads; selects one member for each of the five fixed stress keys (`config`, `catalog`, `import`, `low_author`, and `non_english`); and selects 35 base additions from dated public GitHub activity listings. A candidate must clone publicly, have at least 500 first-parent commits at its frozen default-branch HEAD, and receive recorded primary-language and layout strata. Within each frame, immutable GitHub IDs are ordered by `SHA256(UTF8(seed + NUL + frame key + NUL + decimal GitHub ID))`; the base solver then follows the frozen language/layout margins and documented exhausted-frame fallback. Candidate screening failures remain in the selection ledger; after final selection, extraction or replay failures remain members rather than being replaced.",
            "",
        ]
    )
    cohort_counts = Counter(str(member.raw.get("cohort") or "not recorded") for member in members)
    lines.extend(["| Cohort | Realised members |", "|---|---:|"])
    for cohort, count in sorted(cohort_counts.items()):
        lines.append(f"| {_escape(cohort)} | {count} |")
    lines.append("")
    _render_strata(lines, members, "language_stratum", "primary-language")
    _render_strata(lines, members, "layout_stratum", "layout")
    _render_stress_strata(lines, members)

    lines.extend(
        [
            "### Selection-ledger accounting",
            "",
            f"The ledger contains **{len(ledger.records):,}** chained events. Linkage errors: **{len(ledger.linkage_errors)}**; canonical-content digest errors: **{len(ledger.digest_errors)}**.",
            "",
            "| Outcome status | Events |",
            "|---|---:|",
        ]
    )
    if ledger.outcome_counts:
        for status, count in sorted(ledger.outcome_counts.items()):
            lines.append(f"| {_escape(status)} | {count:,} |")
    else:
        lines.append("| no recorded outcomes | 0 |")
    lines.extend(["", "Most frequent recorded non-success reasons:", ""])
    if ledger.reason_counts:
        for reason, count in ledger.reason_counts.most_common(20):
            lines.append(f"- {count:,} × {_escape(reason)}")
    else:
        lines.append("- None recorded.")
    lines.append("")

    successful = [item for item in observations if item.successful]
    comparable = [item for item in observations if item.comparable]
    points_all = _plot_points(observations)
    points_comparable = [item for item in points_all if not item.capped]

    lines.extend(
        [
            "## Direct answers",
            "",
            "### 1. Does co-change precision-at-one still transfer?",
            "",
            _anchor_correction(observations),
            "",
            "The baseline comparison is strict: “above baseline” means P@1 > 0.500; equality is not counted.",
            "",
            "| Analysis set | Measured n | Minimum | Q1 | Median | Q3 | Maximum | Above 0.500 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
            _render_distribution_row("All successful members", successful),
            _render_distribution_row("Full-history comparable members", comparable),
            "",
        ]
    )
    all_p1 = _p1_values(successful)
    above_count = sum(value > BASELINE_P1 for value in all_p1)
    if complete:
        lines.append(
            f"Within **{_escape(scope)}**, {above_count} of the 50 selected members have a measured P@1 strictly above the JavaScript baseline ({above_count}/{len(all_p1)} among members with a successful replay); {status_counts['failed']} selected members have no P@1 because their recorded run failed."
        )
        lines.append("")
        lines.append(
            f"**Scoped answer:** the measured distribution has {above_count}/{len(all_p1)} successful members above baseline. The frozen rule did not preregister a corpus-level pass count, so this distribution and exact count—not a post-hoc binary threshold—are the answer within the named scope; they do not establish a universal transfer rate."
        )
    else:
        lines.append(
            f"Interim only: {above_count}/{len(all_p1)} currently measured members are strictly above 0.500. This count cannot answer the 50-member question while the report is incomplete."
        )
    lines.append("")

    all_corr = _correlations(points_all)
    comparable_corr = _correlations(points_comparable)
    lines.extend(
        [
            "### 2. Does recall-at-ten still track mean ground-truth set size?",
            "",
            f"![R@10 versus query-weighted mean ground-truth set size]({_relative_link(output_path, png_path)})",
            "",
            f"[SVG version of the scatter plot]({_relative_link(output_path, svg_path)}). Capped members are hollow triangles and are non-comparable for a warm-history claim.",
            "",
            "No correlation statistic was preregistered in the frozen selection rule. All three below are therefore **descriptive**. Spearman’s rank correlation is foregrounded because the claimed relationship is monotone and the attainable recall at fixed K is nonlinear; Pearson on raw and natural-log ground-truth size are sensitivity descriptions, not alternative opportunities to select the largest coefficient.",
            "",
            "| Analysis set | n | Pearson r (raw mean GT) | Pearson r (ln mean GT) | Spearman ρ |",
            "|---|---:|---:|---:|---:|",
            f"| All successful members | {len(points_all)} | {_fmt(all_corr['pearson_raw'])} | {_fmt(all_corr['pearson_log'])} | {_fmt(all_corr['spearman'])} |",
            f"| Full-history comparable members | {len(points_comparable)} | {_fmt(comparable_corr['pearson_raw'])} | {_fmt(comparable_corr['pearson_log'])} | {_fmt(comparable_corr['spearman'])} |",
            "",
            "Every recall value is paired with its query-weighted mean ground-truth set size in the next table. For an eligible commit with k claimable files, the replay emits k seed queries and each query has k−1 targets; mean GT is therefore `Σ k(k−1) / Σ k`, not an unweighted mean over commits.",
            "",
            "| Member | Shape | Mean GT per query | Co-change P@1 | Co-change R@10 | Popularity R@10 | R@10 lift (ratio) | R@10 difference | History |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in observations:
        if not item.successful:
            lines.append(
                f"| {_escape(item.member.name)} | {_escape(_shape(item.member))} | — | — | — | — | — | — | {item.status} |"
            )
            continue
        cochange_p1 = _model_metric(item, "cochange_time_decayed", "p_at_1")
        cochange_r10 = _model_metric(item, "cochange_time_decayed", "r_at_10")
        popularity_r10 = _model_metric(item, "popularity_control", "r_at_10")
        lift_ratio, lift_difference = _lift_values(item)
        history = "**left-truncated; non-comparable†**" if item.capped else "full replay window"
        lines.append(
            f"| {_escape(item.member.name)} | {_escape(_shape(item.member))} | "
            f"{_fmt(item.mean_ground_truth_size, 1)} | {_fmt(cochange_p1)} | "
            f"{_fmt(cochange_r10)} | {_fmt(popularity_r10)} | {_multiple(lift_ratio)} | "
            f"{_fmt_signed(lift_difference)} | {history} |"
        )
    lines.extend(
        [
            "",
            "† The live-file universe starts at the replay-window boundary, but learned indexes start empty. These rows do not estimate the same warm-history quantity as uncapped rows.",
            "",
        ]
    )
    if complete:
        primary_rho = comparable_corr["spearman"]
        if primary_rho is None:
            comparable_gt = [float(item.mean_ground_truth_size) for item in points_comparable]
            comparable_r10 = [
                _model_metric(item, "cochange_time_decayed", "r_at_10")
                for item in points_comparable
            ]
            reason = (
                "fewer than two full-history successful members are available"
                if len(points_comparable) < 2
                else "mean GT or R@10 has zero variance among the full-history successful members"
                if len(set(comparable_gt)) < 2 or len(set(comparable_r10)) < 2
                else "the coefficient is undefined"
            )
            lines.append(f"The monotone direction cannot be assessed because {reason}.")
        elif primary_rho < 0:
            lines.append(
                f"The full-history Spearman estimate is negative (ρ = **{_fmt(primary_rho)}**), so its observed direction matches the proposed inverse relationship. No minimum magnitude or uncertainty criterion was preregistered, so sign alone does not verify that recall ‘tracks’ mean GT."
            )
        elif primary_rho > 0:
            lines.append(
                f"The full-history Spearman estimate is positive (ρ = **{_fmt(primary_rho)}**), so its observed direction runs against the proposed inverse relationship. This is a descriptive estimate, not an inferential rejection."
            )
        else:
            lines.append(
                "The full-history Spearman estimate is zero; the proposed inverse direction is not visible descriptively, but no inferential rejection is claimed."
            )
    else:
        lines.append("No 50-member recall/ground-truth tracking verdict is issued while the report is incomplete.")
    lines.append("")
    lift_points = [item for item in points_comparable if _lift_values(item)[0] is not None]
    undefined_lift = [item for item in points_comparable if _lift_values(item)[0] is None]
    lines.extend(
        [
            "### 3. Does lift over popularity vary systematically with commit size?",
            "",
            f"The original finding defined lift as `co-change R@10 / popularity R@10`; that definition is retained. The ratio is defined for {len(lift_points)}/{len(points_comparable)} full-history successful members. “Small”, “middle”, and “large” below are post-specified rank thirds of that same ratio-defined analysis set, ordered by query-weighted mean GT. This avoids choosing cut points after seeing lift, but it is descriptive rather than inferential. Ties at a boundary break by member name.",
            "",
            "| Mean-GT rank third | n | Mean-GT range | Mean lift | Median lift | Mean R@10 difference |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    if undefined_lift:
        lines.append(
            "Undefined lift ratio because popularity R@10 is zero: "
            + ", ".join(_escape(item.member.name) for item in undefined_lift)
            + ". These members are excluded from every ratio summary below; their R@10 differences remain in the per-member table."
        )
        lines.append("")
    terciles = _rank_terciles(lift_points)
    tercile_means: list[float] = []
    tercile_medians: list[float] = []
    for label, group in terciles:
        ground_truth = [float(item.mean_ground_truth_size) for item in group]
        ratios = [float(_lift_values(item)[0]) for item in group]
        differences = [_lift_values(item)[1] for item in group]
        mean_ratio = statistics.fmean(ratios) if ratios else None
        median_ratio = statistics.median(ratios) if ratios else None
        if mean_ratio is not None:
            tercile_means.append(mean_ratio)
        if median_ratio is not None:
            tercile_medians.append(median_ratio)
        gt_range = (
            f"{min(ground_truth):.1f}–{max(ground_truth):.1f}" if ground_truth else "—"
        )
        lines.append(
            f"| {label} | {len(group)} | {gt_range} | {_multiple(mean_ratio)} | "
            f"{_multiple(median_ratio)} | "
            f"{_fmt_signed(statistics.fmean(differences) if differences else None)} |"
        )

    lift_gt = [float(item.mean_ground_truth_size) for item in lift_points]
    lift_ratios = [float(_lift_values(item)[0]) for item in lift_points]
    lift_spearman = spearman(lift_gt, lift_ratios)
    lift_pearson_log = pearson([math.log(value) for value in lift_gt], lift_ratios)
    monotone_means = len(tercile_means) == 3 and tercile_means[0] < tercile_means[1] < tercile_means[2]
    monotone_medians = (
        len(tercile_medians) == 3 and tercile_medians[0] < tercile_medians[1] < tercile_medians[2]
    )
    lines.extend(
        [
            "",
            f"Across the {len(lift_points)} ratio-defined full-history successful members, lift versus mean GT has Spearman ρ = **{_fmt(lift_spearman)}** and Pearson r versus ln(mean GT) = **{_fmt(lift_pearson_log)}**. The rank-third means are {'strictly increasing' if monotone_means else 'not strictly increasing'} and the medians are {'strictly increasing' if monotone_medians else 'not strictly increasing'}.",
            "",
        ]
    )
    if complete:
        if monotone_means and monotone_medians and lift_spearman is not None and lift_spearman > 0:
            lines.append(
                "The requested direction appears descriptively by all three stated summaries within the named scope. This does not identify commit size as the cause."
            )
        else:
            lines.append(
                "The requested direction does not appear cleanly by all three stated summaries within the named scope."
            )
    else:
        lines.append("No 50-member lift verdict is issued while the run is incomplete.")
    lines.append("")

    below = [
        item
        for item in successful
        if _model_metric(item, "cochange_time_decayed", "p_at_1") < BASELINE_P1
    ]
    lines.extend(
        [
            "### 4. Which shapes break the mechanism, if any?",
            "",
            "The named falsifier is time-decayed co-change P@1 falling materially below 0.500. The prior report did not define a numerical materiality margin, so this report exposes every value below 0.500 and does not invent a post-hoc threshold.",
            "",
            "| Member below 0.500 | Shape | Mean GT per query | P@1 | Difference from 0.500 | History |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    if below:
        for item in sorted(
            below, key=lambda value: _model_metric(value, "cochange_time_decayed", "p_at_1")
        ):
            p1 = _model_metric(item, "cochange_time_decayed", "p_at_1")
            history = "left-truncated†" if item.capped else "full replay window"
            lines.append(
                f"| {_escape(item.member.name)} | {_escape(_shape(item.member))} | "
                f"{_fmt(item.mean_ground_truth_size, 1)} | {_fmt(p1)} | "
                f"{_fmt_signed(p1 - BASELINE_P1)} | {history} |"
            )
    else:
        lines.append("| None among successful results | — | — | — | — | — |")
    lines.append("")
    if complete and not below:
        lines.append("The numerical part of the falsifier is not triggered within the named scope.")
    elif complete:
        lines.append(
            f"{len(below)} measured members fall below 0.500. Their exact shapes and margins are above; whether a margin is “material” remains unverified without a prespecified margin or whole-commit interval."
        )
    else:
        lines.append("No shape-level falsifier verdict is issued while the report remains incomplete.")
    lines.append("")

    lines.extend(
        [
            "## All per-member model metrics",
            "",
            "P@K uses fixed denominator K, missing ranks count as misses, and recall is averaged per seed query. Timings cover ranked-list production on this machine and are diagnostic. Every recall cell shares its row with the same member’s query-weighted mean GT.",
            "",
            "| Member | Mean GT per query | Model | P@1 | P@10 | R@10 | R@20 | Empty radius | Median query µs | History/status |",
            "|---|---:|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in observations:
        if not item.successful:
            failure = _escape(item.failure or item.status)
            lines.append(
                f"| {_escape(item.member.name)} | — | **{item.status.upper()}** | — | — | — | — | — | — | {failure} |"
            )
            continue
        history = "left-truncated†" if item.capped else "full replay window"
        for model_key, label in MODEL_LABELS.items():
            model = _result_model(item, model_key)
            lines.append(
                f"| {_escape(item.member.name)} | {_fmt(item.mean_ground_truth_size, 1)} | {label} | "
                f"{_fmt(float(model['p_at_1']))} | {_fmt(float(model['p_at_10']))} | "
                f"{_fmt(float(model['r_at_10']))} | {_fmt(float(model['r_at_20']))} | "
                f"{_pct(float(model['empty_radius_rate']))} | "
                f"{_fmt(float(model.get('median_query_microseconds')), 1) if model.get('median_query_microseconds') is not None else '—'} | "
                f"{history} |"
            )
    lines.append("")

    lines.extend(
        [
            "## Coverage, caps, and failures",
            "",
            "| Member | Reachable commits | First-parent commits | Replayed | Eligible commits | Queries | Mean GT per query | Largest query-commit share | Cap/status |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in observations:
        result = item.result or {}
        coverage = result.get("coverage", {}) if isinstance(result.get("coverage"), dict) else {}
        stream = item.stream or {}
        reachable = stream.get("reachable_commit_count", item.member.raw.get("reachable_commit_count"))
        first_parent = stream.get(
            "first_parent_commit_count", item.member.raw.get("first_parent_commit_count")
        )
        if item.successful:
            cap_status = "left-truncated†" if item.capped else "uncapped"
        elif item.status == "failed":
            cap_status = f"**{_escape(item.failure_stage or 'run')} failed:** {_escape(item.failure or '')}"
        else:
            cap_status = "pending/missing"
        largest_share = coverage.get("largest_query_commit_share")
        lines.append(
            f"| {_escape(item.member.name)} | {_integer_text(reachable)} | "
            f"{_integer_text(first_parent)} | {_integer_text(coverage.get('commits_replayed'))} | "
            f"{_integer_text(coverage.get('eligible_commit_count'))} | "
            f"{_integer_text(coverage.get('query_count'))} | {_fmt(item.mean_ground_truth_size, 1)} | "
            f"{_pct(float(largest_share)) if isinstance(largest_share, (int, float)) else '—'} | {cap_status} |"
        )
    lines.extend(["", "### Applied-cap log", ""])
    capped = [item for item in observations if item.capped]
    if capped:
        lines.extend(["| Member | Logged reason | Comparability |", "|---|---|---|"])
        for item in capped:
            lines.append(
                f"| {_escape(item.member.name)} | {_escape(item.cap_reason or 'MISSING CAP REASON')} | "
                "Non-comparable for warm-history conclusions |"
            )
    else:
        lines.append("No applied cap is currently recorded.")
    lines.append("")

    failures = [item for item in observations if item.status == "failed"]
    lines.extend(["### Selected-member failures", ""])
    if failures:
        lines.extend(["| Member | Failed stage | Recorded result |", "|---|---|---|"])
        for item in failures:
            lines.append(
                f"| {_escape(item.member.name)} | {_escape(item.failure_stage or 'unknown')} | "
                f"{_escape(item.failure or 'no reason recorded')} |"
            )
    else:
        lines.append("No selected-member terminal failure is currently recorded.")
    lines.append("")

    lines.extend(["## Per-claim confidence", ""])
    if successful:
        lines.append(
            f"- **Stored metric values for the {len(successful)} successful members — high confidence.** Per-commit query totals and sufficient statistics reconstruct every reported P@1, P@10, R@10, R@20, and empty-radius value; source identities and the common replay-harness hash are checked before rendering."
        )
    else:
        lines.append("- **Stored metric values — no confidence assessment available.** No replay has succeeded.")
    if complete:
        lines.extend(
            [
                f"- **P@1 distribution and baseline count within {scope} — moderate confidence.** The distribution directly describes the fixed selected members, but terminal failures have no metric, the mixture includes ten certainty anchors, no corpus-level transfer threshold was preregistered, and no whole-commit uncertainty interval is computed here.",
                "- **R@10 association with mean GT — moderate confidence as a descriptive association.** Mean GT is reconstructed exactly from each query-producing commit and rank/raw/log correlations are shown, but no correlation statistic was preregistered and capped cold-start windows are excluded from the comparable-history sensitivity row.",
                "- **Popularity-lift ordering by commit size — low-to-moderate confidence.** The original lift definition is retained and rank thirds are deterministic, but the grouping and summaries were specified for this extension rather than preregistered, and shape, language, team practice, vendor updates, and commit size remain confounded.",
                "- **Shape-level mechanism breaks — low confidence unless a large below-baseline margin is visible on an uncapped member.** Exact P@1 deltas are measured, but “materially below” has no prespecified numerical margin and this report does not add a post-hoc one.",
            ]
        )
    else:
        lines.append(
            f"- **All 50-member central claims — not yet assessable.** {len(successful)}/50 planned members currently have successful metrics and {status_counts['pending']} remain pending/missing; partial distributions are progress diagnostics only."
        )
    lines.append("")

    lines.extend(
        [
            "## Claims that could NOT be verified",
            "",
        ]
    )
    if not complete:
        lines.append(
            "- The four requested 50-member verdicts could not be verified because the manifest, ledger, stage outcomes, or replay outputs are incomplete as enumerated under run progress."
        )
    if failures:
        lines.append(
            f"- Model performance could not be verified for {len(failures)} selected members with terminal failures; those failures remain denominator-visible rather than being omitted or replaced."
        )
    rule_freeze = manifest.get("rule_freeze_provenance")
    if isinstance(rule_freeze, Mapping):
        timing_detail = (
            f" The recorded commit timestamp (`{_escape(rule_freeze.get('committed_at') or 'not recorded')}`) "
            f"follows the recorded acquisition-ledger interval "
            f"(`{_escape(rule_freeze.get('first_acquisition_event_at_utc') or 'not recorded')}` to "
            f"`{_escape(rule_freeze.get('last_acquisition_event_at_utc') or 'not recorded')}`), so the stronger "
            "pre-acquisition freeze claim is unverified."
            if rule_freeze.get("committed_before_frame_acquisition_verified") is False
            else " The stronger pre-acquisition ordering is not claimed without independently witnessed timing evidence."
        )
    else:
        timing_detail = " The stronger pre-acquisition ordering is also unverified."
    lines.append(
        "- Rule timing is supported by matching Git blob identity plus local committer and ledger-event timestamp ordering before selection; this supports pre-selection recording but does not independently prove when recording occurred."
        + timing_detail
    )
    lines.extend(
        [
            "- A population prevalence or language-causal claim could not be verified. The design combines retained certainty anchors with seeded draws from dated activity and stress frames; it is not a probability sample of all public GitHub projects.",
            "- A numerical interpretation of “materially below 0.500” could not be verified because no materiality margin was specified before seeing the expanded results.",
            "- Statistical uncertainty for the 50-member contrasts could not be verified here. Queries from one commit are dependent; naive query-level intervals would be invalid, and no whole-commit bootstrap was run by this script.",
            "- Full-warm-history behavior for capped members could not be verified. Their learned indexes start empty at the left boundary, so those rows are explicitly non-comparable for warm-history conclusions.",
            "- The JavaScript comparator’s R@10 cannot support a cross-member recall comparison because its mean ground-truth set size was not reported. This report therefore does not repeat that recall number without its missing denominator context.",
            "- Commit size as a cause of recall or popularity lift could not be verified; project shape, vendor/generated bulk changes, team practice, age, and language are observationally confounded.",
            "",
            "## What would change this verdict",
            "",
            "- Recording all 50 selected members in the manifest, then completing each with either a validated replay result or a durable stage failure, would replace this draft’s progress diagnostics with the scoped 50-member verdicts.",
            "- One or more full-history, uncapped stress-shape members with P@1 clearly below 0.500—and a prespecified materiality margin plus whole-commit interval wholly below it—would trigger the named mechanism falsifier.",
            "- Replaying capped members with their full prior history could remove a left-truncation artifact; a material reversal would change any scale or shape conclusion that currently depends on those rows.",
            "- A preregistered whole-commit or time-block bootstrap that places the recall/mean-GT association or popularity-lift ordering near zero would weaken the corresponding descriptive claim.",
            "- A second dated frame drawn under the same deterministic rule, or a genuine probability sample with systematic collapse outside the present frame, would change the external-validity verdict.",
            "- An independently timestamped copy of the exact rule that predates frame acquisition would verify the stronger pre-acquisition freeze claim; the present local Git and ledger timestamps do not.",
            "- Recovering the JavaScript baseline’s per-query ground-truth sizes would make its recall comparator interpretable and could change the claim about numeric recall transfer.",
            "",
        ]
    )
    return "\n".join(lines), complete


def build_report(
    manifest_path: Path,
    ledger_path: Path,
    results_dir: Path,
    streams_dir: Path,
    output_path: Path,
    plot_prefix: Path,
    run_state_path: Path | None = None,
) -> bool:
    manifest, members = load_manifest(manifest_path)
    ledger = load_ledger(ledger_path)
    run_state = (
        _read_optional_json(run_state_path)
        if run_state_path is not None and run_state_path.exists()
        else None
    )
    if run_state is not None:
        state_rule = run_state.get("rule_id")
        if state_rule and state_rule != manifest.get("rule_id"):
            raise InputError(
                f"run-state rule_id {state_rule!r} does not match manifest {manifest.get('rule_id')!r}"
            )
    observations = collect_observations(members, results_dir, streams_dir, run_state)
    png_path, svg_path = write_scatter_plot(observations, plot_prefix)
    markdown, complete = render_report(
        manifest, members, ledger, observations, output_path, ledger_path, png_path, svg_path
    )
    atomic_write_text(output_path, markdown)
    return complete


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--streams-dir", type=Path, default=DEFAULT_STREAMS)
    parser.add_argument(
        "--run-state",
        type=Path,
        default=DEFAULT_RUN_STATE,
        help="optional durable runner state used for failures that could not write downstream artifacts",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--plot-prefix", type=Path, default=DEFAULT_PLOT_PREFIX)
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="write the report, then exit 2 if any finalisation condition is unmet",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        complete = build_report(
            manifest_path=args.manifest.resolve(),
            ledger_path=args.ledger.resolve(),
            results_dir=args.results_dir.resolve(),
            streams_dir=args.streams_dir.resolve(),
            output_path=args.output.resolve(),
            plot_prefix=args.plot_prefix.resolve(),
            run_state_path=args.run_state.resolve(),
        )
    except InputError as exc:
        print(f"corpus50_report: {exc}", file=sys.stderr)
        return 1
    state = "complete" if complete else "INCOMPLETE DRAFT"
    print(f"wrote {args.output} ({state})")
    if args.require_complete and not complete:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
