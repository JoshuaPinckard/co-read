#!/usr/bin/env python3
"""Resume the frozen Corpus-50 screening procedure.

This is a thin command-line adapter around the already-preregistered selection
operations in :mod:`analysis.corpus50`.  It adds no selection rule: it merely
walks the frozen candidate orders sequentially, persists one outcome at a time,
and asks the existing exact-margin solver whether the base sample is complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from analysis import corpus50
except ImportError:  # Direct execution places analysis/ rather than its parent on sys.path.
    import corpus50  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FRAME_ROOT = Path(r"D:\Blast-Radius-C50")
DEFAULT_LEDGER = PROJECT_ROOT / "corpus" / "CORPUS-50-LEDGER.jsonl"
RULE_FREEZE_COMMIT = "4b1d5defee41fa3934f994cae7fc03d0b57b079e"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    return corpus50.read_jsonl(path) if path.exists() else []


def _atomic_json(path: Path, value: Any) -> None:
    corpus50.atomic_write_json(path, value)


def _verify_frozen_frames(frame_root: Path) -> dict[str, Any]:
    """Fail closed if a ranked frame differs from its verified construction manifest."""
    manifests = frame_root / "manifests"
    base_manifest = corpus50.read_json(manifests / "base-active.json")
    stress_manifest = corpus50.read_json(manifests / "stress-frames.json")
    if not isinstance(base_manifest, dict) or not isinstance(stress_manifest, dict):
        raise corpus50.Corpus50Error("selection frame manifests must be JSON objects")
    if (
        base_manifest.get("rule_id") != corpus50.RULE_ID
        or base_manifest.get("seed") != corpus50.SEED
        or base_manifest.get("listing_date") != corpus50.BASE_LISTING_DATE
        or base_manifest.get("complete") is not True
    ):
        raise corpus50.Corpus50Error("base frame manifest does not match the frozen rule")
    if (
        stress_manifest.get("rule_id") != corpus50.RULE_ID
        or stress_manifest.get("seed") != corpus50.SEED
        or stress_manifest.get("listing_date") != corpus50.STRESS_LISTING_DATE
        or stress_manifest.get("complete") is not True
    ):
        raise corpus50.Corpus50Error("stress frame manifest does not match the frozen rule")

    verified: dict[str, Any] = {}
    base_path = frame_root / "frames" / "base-active.jsonl"
    base_digest, base_length = corpus50.sha256_file(base_path)
    if (
        base_digest != base_manifest.get("sha256")
        or base_length != int(base_manifest.get("byte_length", -1))
    ):
        raise corpus50.Corpus50Error("base-active.jsonl fails its frozen SHA-256/length")
    verified["base"] = {"path": str(base_path), "sha256": base_digest, "byte_length": base_length}

    stress_frames = stress_manifest.get("frames")
    if not isinstance(stress_frames, dict):
        raise corpus50.Corpus50Error("stress frame manifest lacks frames{}")
    for key in corpus50.STRESS_KEYS:
        expected = stress_frames.get(key)
        if not isinstance(expected, dict):
            raise corpus50.Corpus50Error(f"stress frame manifest lacks {key}")
        path = frame_root / "frames" / f"stress-{key}.jsonl"
        digest, length = corpus50.sha256_file(path)
        if digest != expected.get("sha256") or length != int(expected.get("byte_length", -1)):
            raise corpus50.Corpus50Error(
                f"stress-{key}.jsonl fails its frozen SHA-256/length"
            )
        verified[key] = {"path": str(path), "sha256": digest, "byte_length": length}

    evidence = {
        "schema_version": corpus50.SCHEMA_VERSION,
        "rule_id": corpus50.RULE_ID,
        "seed": corpus50.SEED,
        "verified_at_utc": corpus50.utc_now(),
        "verified": verified,
    }
    corpus50.atomic_write_json(
        manifests / "selection-frame-verification.json", evidence
    )
    return evidence


def _terminal_ranks(
    ledger: Path, *, cohort: str, stress_key: str | None = None
) -> set[int]:
    if not ledger.exists():
        return set()
    corpus50.verify_hash_chain(ledger)
    completed: set[int] = set()
    for record in corpus50.read_jsonl(ledger):
        candidate = record.get("candidate")
        outcome = record.get("outcome")
        if not isinstance(candidate, dict) or not isinstance(outcome, dict):
            continue
        if cohort == "base" and record.get("event_type") == "base_candidate_removed_as_stress":
            if outcome.get("status") != "excluded":
                continue
        else:
            if record.get("event_type") != "candidate_screened":
                continue
            if candidate.get("cohort") != cohort:
                continue
            if stress_key is not None and candidate.get("stress_key") != stress_key:
                continue
            terminal = {"rejected", "selected"} if cohort == "stress" else {"rejected", "eligible"}
            if outcome.get("status") not in terminal:
                continue
        field = "candidate_order" if cohort == "stress" else "base_rank"
        if candidate.get(field) is not None:
            completed.add(int(candidate[field]))
    return completed


def _terminal_candidate_outcomes(
    ledger: Path, *, cohort: str, stress_key: str | None = None
) -> dict[int, tuple[str, int]]:
    """Return rank -> (terminal status, repository id) for transaction recovery."""
    if not ledger.exists():
        return {}
    corpus50.verify_hash_chain(ledger)
    terminal: dict[int, tuple[str, int]] = {}
    for record in corpus50.read_jsonl(ledger):
        if record.get("event_type") != "candidate_screened":
            continue
        candidate = record.get("candidate")
        outcome = record.get("outcome")
        if not isinstance(candidate, dict) or not isinstance(outcome, dict):
            continue
        if candidate.get("cohort") != cohort:
            continue
        if stress_key is not None and candidate.get("stress_key") != stress_key:
            continue
        statuses = {"rejected", "selected"} if cohort == "stress" else {"rejected", "eligible"}
        status = str(outcome.get("status"))
        if status not in statuses or candidate.get("repo_id") is None:
            continue
        field = "candidate_order" if cohort == "stress" else "base_rank"
        if candidate.get(field) is not None:
            terminal[int(candidate[field])] = (status, int(candidate["repo_id"]))
    return terminal


def _selected_stress(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("stress_key")
        if key in corpus50.STRESS_KEYS:
            selected[str(key)] = row
    return selected


def _validate_selected_clone(row: dict[str, Any]) -> None:
    name = str(row.get("name") or row.get("clone_name"))
    repository = PROJECT_ROOT / "corpus" / "_clones" / corpus50.slug_for_name(name)
    if not repository.exists():
        raise corpus50.Corpus50Error(f"selected clone is missing: {repository}")
    measured = corpus50.measure_repository(repository)
    classification = measured["classification"]
    expected = {
        "head": row.get("head"),
        "first_parent_commit_count": row.get("first_parent_commit_count"),
        "reachable_commit_count": row.get("reachable_commit_count"),
        "primary_language": row.get("primary_language"),
        "language_stratum": row.get("language_stratum"),
        "layout_stratum": row.get("layout_stratum"),
        "tracked_path_count": row.get("tracked_path_count"),
        "source_path_count": row.get("source_path_count"),
    }
    observed = {
        "head": measured.get("head"),
        "first_parent_commit_count": measured.get("first_parent_commit_count"),
        "reachable_commit_count": measured.get("reachable_commit_count"),
        "primary_language": classification.get("primary_language"),
        "language_stratum": classification.get("language_stratum"),
        "layout_stratum": classification.get("layout_stratum"),
        "tracked_path_count": classification.get("tracked_path_count"),
        "source_path_count": classification.get("source_path_count"),
    }
    if observed != expected:
        raise corpus50.Corpus50Error(
            f"selected clone drift/classification mismatch for {name}: "
            f"expected={expected!r}, observed={observed!r}"
        )


def _validate_anchor_freeze() -> dict[str, Any]:
    """Require the ten retained anchors to equal their original frozen run."""
    prior_path = PROJECT_ROOT / "corpus" / "CORPUS.json"
    prior = corpus50.read_json(prior_path)
    repositories = prior.get("repositories") if isinstance(prior, dict) else None
    if not isinstance(repositories, dict):
        raise corpus50.Corpus50Error(
            "the original ten-member corpus manifest is missing repositories{}"
        )
    by_name: dict[str, dict[str, Any]] = {}
    for value in repositories.values():
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            raise corpus50.Corpus50Error("invalid row in the original corpus manifest")
        folded = str(value["name"]).casefold()
        if folded in by_name:
            raise corpus50.Corpus50Error(
                f"duplicate retained-anchor name in original manifest: {value['name']}"
            )
        by_name[folded] = value
    expected_names = {name.casefold() for name in corpus50.RETAINED_ANCHORS}
    if set(by_name) != expected_names:
        raise corpus50.Corpus50Error(
            "the original corpus manifest does not contain exactly the ten retained anchors"
        )

    verified: dict[str, Any] = {}
    for name in corpus50.RETAINED_ANCHORS:
        frozen = by_name[name.casefold()]
        if frozen.get("status") != "ok":
            raise corpus50.Corpus50Error(f"retained anchor was not frozen successfully: {name}")
        repository = (
            PROJECT_ROOT / "corpus" / "_clones" / corpus50.slug_for_name(name)
        )
        if not repository.exists():
            raise corpus50.Corpus50Error(f"retained anchor clone is missing: {repository}")
        measured = corpus50.measure_repository(repository)
        expected = {
            "head": str(frozen.get("resolved_head_sha", "")).casefold(),
            "first_parent_commit_count": int(frozen.get("first_parent_commit_count", -1)),
            "reachable_commit_count": int(frozen.get("reachable_commit_count", -1)),
        }
        observed = {
            "head": str(measured.get("head", "")).casefold(),
            "first_parent_commit_count": int(
                measured.get("first_parent_commit_count", -1)
            ),
            "reachable_commit_count": int(measured.get("reachable_commit_count", -1)),
        }
        if observed != expected:
            raise corpus50.Corpus50Error(
                f"retained anchor drifted from the original frozen run: {name}; "
                f"expected={expected!r}, observed={observed!r}"
            )
        verified[name] = observed
    digest, length = corpus50.sha256_file(prior_path)
    return {
        "manifest_path": str(prior_path.resolve()),
        "manifest_sha256": digest,
        "manifest_byte_length": length,
        "verified": verified,
    }


def _validate_rule_freeze(frame_root: Path, selection_ledger: Path) -> dict[str, Any]:
    """Record exactly what the durable timestamps prove about rule timing."""
    rule_relative = "corpus/CORPUS-50-RULE.md"
    resolved_commit = corpus50.git_text(
        PROJECT_ROOT, ["rev-parse", "--verify", f"{RULE_FREEZE_COMMIT}^{{commit}}"]
    ).strip()
    if resolved_commit.casefold() != RULE_FREEZE_COMMIT:
        raise corpus50.Corpus50Error("the recorded rule-freeze commit is unavailable")
    frozen_blob = corpus50.git_text(
        PROJECT_ROOT, ["rev-parse", f"{RULE_FREEZE_COMMIT}:{rule_relative}"]
    ).strip()
    current_blob = corpus50.git_text(
        PROJECT_ROOT,
        ["hash-object", f"--path={rule_relative}", str(PROJECT_ROOT / rule_relative)],
    ).strip()
    if current_blob != frozen_blob:
        raise corpus50.Corpus50Error(
            "current Corpus-50 rule differs from its pre-selection frozen Git blob"
        )
    committed_at = corpus50.git_text(
        PROJECT_ROOT, ["show", "-s", "--format=%cI", RULE_FREEZE_COMMIT]
    ).strip()

    acquisition_ledger = frame_root / "manifests" / "acquisitions.jsonl"
    acquisition_verification = corpus50.verify_hash_chain(acquisition_ledger)
    acquisitions = corpus50.read_jsonl(acquisition_ledger)
    selections = corpus50.read_jsonl(selection_ledger)
    acquisition_times = [
        str(record.get("recorded_at_utc"))
        for record in acquisitions
        if record.get("recorded_at_utc")
    ]
    selection_times = [
        str(record.get("recorded_at_utc"))
        for record in selections
        if record.get("recorded_at_utc")
    ]
    if not acquisition_times or not selection_times:
        raise corpus50.Corpus50Error(
            "rule-freeze provenance requires acquisition and selection timestamps"
        )

    def instant(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )

    committed_instant = instant(committed_at)
    first_acquisition = min(acquisition_times, key=instant)
    last_acquisition = max(acquisition_times, key=instant)
    first_selection = min(selection_times, key=instant)
    if committed_instant >= instant(first_selection):
        raise corpus50.Corpus50Error(
            "the durable rule commit does not predate candidate screening/selection"
        )
    before_acquisition = committed_instant < instant(first_acquisition)
    return {
        "rule_path": str((PROJECT_ROOT / rule_relative).resolve()),
        "git_commit": RULE_FREEZE_COMMIT,
        "git_blob": frozen_blob,
        "committed_at": committed_at,
        "first_acquisition_event_at_utc": first_acquisition,
        "last_acquisition_event_at_utc": last_acquisition,
        "first_selection_event_at_utc": first_selection,
        "committed_before_selection_verified": True,
        "committed_before_frame_acquisition_verified": before_acquisition,
        "acquisition_ledger_verification": acquisition_verification,
        "interpretation": (
            "Durable Git evidence verifies the written rule before candidate screening and "
            "selection. It does not verify the stronger pre-acquisition claim."
            if not before_acquisition
            else "Durable Git evidence verifies the written rule before acquisition and selection."
        ),
    }


def _recompute_stress_predicate(
    row: dict[str, Any], key: str, frame_root: Path
) -> dict[str, Any]:
    """Re-run the frozen structural predicate at the selected clone's frozen HEAD."""
    name = str(row.get("name") or row.get("clone_name"))
    repository = PROJECT_ROOT / "corpus" / "_clones" / corpus50.slug_for_name(name)
    stored = row.get("predicate_result")
    if not isinstance(stored, dict):
        raise corpus50.Corpus50Error(f"stress {key} has no stored predicate object")

    if key in {"config", "catalog"}:
        paths = [entry.path for entry in corpus50.list_tree(repository)]
        recomputed = (
            corpus50.evaluate_config_predicate(paths)
            if key == "config"
            else corpus50.evaluate_catalog_predicate(paths)
        )
    elif key == "import":
        recomputed = corpus50.evaluate_import_predicate(repository)
    elif key == "low_author":
        recomputed = corpus50.evaluate_low_author_predicate(repository)
    else:
        guard = corpus50.DiskGuard(
            frame_root,
            (
                PROJECT_ROOT / "corpus" / "_clones",
                PROJECT_ROOT / "exploratory" / "language-hole",
            ),
        )
        recomputed = corpus50.scan_non_english_identifiers(
            repository, disk_guard=guard
        )
        machine_fields = (
            "predicate",
            "repository_head",
            "repository_path",
            "machine_pass",
            "machine_candidate_token_count",
            "source_blob_count_scanned",
            "invalid_utf8_paths",
            "evidence",
            "review_constraint",
        )
        stored_machine = {field: stored.get(field) for field in machine_fields}
        recomputed_machine = {field: recomputed.get(field) for field in machine_fields}
        if corpus50.canonical_json_bytes(stored_machine) != corpus50.canonical_json_bytes(
            recomputed_machine
        ):
            raise corpus50.Corpus50Error(
                "selected non-English machine evidence does not reproduce at the frozen HEAD"
            )
        evidence_by_token = {
            item.get("token"): item
            for item in recomputed.get("evidence", [])
            if isinstance(item, dict) and isinstance(item.get("token"), str)
        }
        accepted = stored.get("accepted_evidence")
        if not isinstance(accepted, list):
            raise corpus50.Corpus50Error(
                "selected non-English evidence lacks reviewed accepted_evidence[]"
            )
        accepted_tokens = [
            item.get("token") for item in accepted if isinstance(item, dict)
        ]
        expected_accepted = [evidence_by_token.get(token) for token in accepted_tokens]
        if (
            len(accepted_tokens) < 10
            or len(accepted_tokens) != len(set(accepted_tokens))
            or any(item is None for item in expected_accepted)
            or corpus50.canonical_json_bytes(accepted)
            != corpus50.canonical_json_bytes(expected_accepted)
            or stored.get("accepted_token_count") != len(accepted_tokens)
            or stored.get("requires_human_review") is not False
            or stored.get("passed") is not True
        ):
            raise corpus50.Corpus50Error(
                "selected non-English human review does not reduce the reproduced machine evidence"
            )
        return {
            "predicate": key,
            "recomputed": True,
            "machine_candidate_token_count": recomputed.get(
                "machine_candidate_token_count"
            ),
            "accepted_token_count": len(accepted_tokens),
        }

    if corpus50.canonical_json_bytes(recomputed) != corpus50.canonical_json_bytes(stored):
        raise corpus50.Corpus50Error(
            f"selected stress predicate does not reproduce at the frozen HEAD: {key}"
        )
    if recomputed.get("passed") is not True:
        raise corpus50.Corpus50Error(f"recomputed stress predicate does not pass: {key}")
    return {"predicate": key, "recomputed": True}


def _validate_selection_provenance(
    frame_root: Path, ledger_path: Path, *, require_base: bool
) -> dict[str, Any]:
    """Reconstruct the deterministic selection from frames, ledger, and solver inputs."""
    frame_evidence = _verify_frozen_frames(frame_root)
    ledger_evidence = corpus50.verify_hash_chain(ledger_path)
    records = corpus50.read_jsonl(ledger_path)
    stress_path = frame_root / "frames" / "stress-selected.jsonl"
    stress_rows = _read_rows(stress_path)
    stress_by_key = _selected_stress(stress_rows)
    if len(stress_rows) != len(stress_by_key):
        raise corpus50.Corpus50Error("stress-selected contains duplicate stress keys")
    selected_keys = [str(row.get("stress_key")) for row in stress_rows]
    expected_keys = list(corpus50.STRESS_KEYS[: len(stress_rows)])
    if selected_keys != expected_keys:
        raise corpus50.Corpus50Error(
            "selected stress slots are not the fixed deterministic prefix"
        )
    selected_repo_ids = [int(row["repo_id"]) for row in stress_rows]
    if len(selected_repo_ids) != len(set(selected_repo_ids)):
        raise corpus50.Corpus50Error("stress-selected repeats an immutable repository id")
    if require_base and len(stress_rows) != len(corpus50.STRESS_KEYS):
        raise corpus50.Corpus50Error("base provenance requires all five stress slots")

    def candidate_matches(
        actual: dict[str, Any], expected: dict[str, Any], rank_field: str
    ) -> bool:
        actual_name = actual.get("name") or actual.get("clone_name")
        expected_name = expected.get("clone_name") or expected.get("name")
        return (
            int(actual.get("repo_id", -1)) == int(expected.get("repo_id", -2))
            and int(actual.get(rank_field, -1)) == int(expected.get(rank_field, -2))
            and str(actual.get("priority_key", ""))
            == str(expected.get("priority_key", ""))
            and str(actual_name).casefold() == str(expected_name).casefold()
        )

    prior_ids: list[int] = []
    stress_checks: dict[str, Any] = {}
    for key in corpus50.STRESS_KEYS:
        row = stress_by_key.get(key)
        if row is None:
            if require_base:
                raise corpus50.Corpus50Error(f"missing selected stress key: {key}")
            break
        order = corpus50.stress_candidate_order(
            frame_root, key, excluded_repo_ids=prior_ids
        )
        rank = int(row.get("candidate_order", 0))
        if rank < 1 or rank > len(order) or not candidate_matches(
            row, order[rank - 1], "candidate_order"
        ):
            raise corpus50.Corpus50Error(
                f"selected stress member {key} is not at its recorded deterministic rank"
            )
        terminal: dict[int, list[dict[str, Any]]] = {}
        for record in records:
            if record.get("event_type") != "candidate_screened":
                continue
            candidate = record.get("candidate")
            outcome = record.get("outcome")
            if not isinstance(candidate, dict) or not isinstance(outcome, dict):
                continue
            if candidate.get("cohort") != "stress" or candidate.get("stress_key") != key:
                continue
            if outcome.get("status") in {"rejected", "selected"}:
                recorded_rank = int(candidate.get("candidate_order", -1))
                if recorded_rank < 1 or recorded_rank > len(order):
                    raise corpus50.Corpus50Error(
                        f"stress {key} ledger has an out-of-frame terminal rank"
                    )
                if not candidate_matches(
                    candidate, order[recorded_rank - 1], "candidate_order"
                ):
                    raise corpus50.Corpus50Error(
                        f"stress {key} ledger candidate differs from frozen rank {recorded_rank}"
                    )
                terminal.setdefault(recorded_rank, []).append(record)
        later = sorted(recorded_rank for recorded_rank in terminal if recorded_rank > rank)
        if later:
            raise corpus50.Corpus50Error(
                f"stress {key} has terminal outcomes after its first selection: {later[:20]}"
            )
        for candidate_rank in range(1, rank + 1):
            events = terminal.get(candidate_rank, [])
            if not events:
                raise corpus50.Corpus50Error(
                    f"stress {key} lacks a terminal outcome at rank {candidate_rank}"
                )
            expected_status = "selected" if candidate_rank == rank else "rejected"
            signatures = {
                (
                    str(item.get("outcome", {}).get("status")),
                    int(item.get("candidate", {}).get("repo_id", -1)),
                )
                for item in events
            }
            expected_signature = {
                (expected_status, int(order[candidate_rank - 1]["repo_id"]))
            }
            if signatures != expected_signature:
                raise corpus50.Corpus50Error(
                    f"stress {key} has conflicting terminal history at rank {candidate_rank}"
                )
        selected_events = terminal[rank]
        expected_measurements = {
            "head": str(row.get("head", "")).casefold(),
            "first_parent_commit_count": int(row.get("first_parent_commit_count", -1)),
            "reachable_commit_count": int(row.get("reachable_commit_count", -1)),
            "primary_language": row.get("primary_language"),
            "language_stratum": row.get("language_stratum"),
            "layout_stratum": row.get("layout_stratum"),
        }
        for item in selected_events:
            measurements = item.get("measurements", {})
            observed_measurements = {
                "head": str(measurements.get("head", "")).casefold(),
                "first_parent_commit_count": int(
                    measurements.get("first_parent_commit_count", -1)
                ),
                "reachable_commit_count": int(
                    measurements.get("reachable_commit_count", -1)
                ),
                "primary_language": measurements.get("primary_language"),
                "language_stratum": measurements.get("language_stratum"),
                "layout_stratum": measurements.get("layout_stratum"),
            }
            if observed_measurements != expected_measurements:
                raise corpus50.Corpus50Error(
                    f"stress {key} selected row differs from its ledger measurements"
                )
            ledger_predicate = item.get("artifacts", {}).get("predicate_result")
            if corpus50.canonical_json_bytes(ledger_predicate) != corpus50.canonical_json_bytes(
                row.get("predicate_result")
            ):
                raise corpus50.Corpus50Error(
                    f"stress {key} selected predicate differs from its ledger evidence"
                )
        predicate = row.get("predicate_result")
        if (
            row.get("stress_predicate_passed") is not True
            or not isinstance(predicate, dict)
            or predicate.get("passed") is not True
        ):
            raise corpus50.Corpus50Error(f"stress {key} has no passing predicate evidence")
        predicate_artifact = row.get("predicate_artifact")
        if predicate_artifact is not None:
            if not isinstance(predicate_artifact, dict):
                raise corpus50.Corpus50Error(f"stress {key} has invalid predicate artifact")
            artifact_path = Path(str(predicate_artifact.get("path", "")))
            digest, length = corpus50.sha256_file(artifact_path)
            if (
                digest != predicate_artifact.get("sha256")
                or length != int(predicate_artifact.get("byte_length", -1))
            ):
                raise corpus50.Corpus50Error(
                    f"stress {key} reviewed predicate artifact fails SHA-256/length"
                )
        predicate_recheck = _recompute_stress_predicate(row, key, frame_root)
        _validate_selected_clone(row)
        prior_ids.append(int(row["repo_id"]))
        stress_checks[key] = {
            "repo_id": int(row["repo_id"]),
            "candidate_order": rank,
            "prior_rejection_count": rank - 1,
            "terminal_event_count": sum(len(events) for events in terminal.values()),
            "predicate_recheck": predicate_recheck,
        }

    pending_stress: dict[str, Any] | None = None
    if len(stress_rows) < len(corpus50.STRESS_KEYS):
        pending_key = corpus50.STRESS_KEYS[len(stress_rows)]
        pending_order = corpus50.stress_candidate_order(
            frame_root, pending_key, excluded_repo_ids=prior_ids
        )
        pending_terminal: dict[int, list[dict[str, Any]]] = {}
        later_keys = set(corpus50.STRESS_KEYS[len(stress_rows) + 1 :])
        for record in records:
            if record.get("event_type") != "candidate_screened":
                continue
            candidate = record.get("candidate")
            outcome = record.get("outcome")
            if not isinstance(candidate, dict) or not isinstance(outcome, dict):
                continue
            if candidate.get("cohort") != "stress":
                continue
            record_key = candidate.get("stress_key")
            if record_key in later_keys and outcome.get("status") in {
                "rejected",
                "selected",
            }:
                raise corpus50.Corpus50Error(
                    f"stress terminal history exists before its fixed slot: {record_key}"
                )
            if record_key != pending_key or outcome.get("status") not in {
                "rejected",
                "selected",
            }:
                continue
            rank = int(candidate.get("candidate_order", -1))
            if rank < 1 or rank > len(pending_order) or not candidate_matches(
                candidate, pending_order[rank - 1], "candidate_order"
            ):
                raise corpus50.Corpus50Error(
                    f"pending stress {pending_key} ledger differs from frozen rank {rank}"
                )
            pending_terminal.setdefault(rank, []).append(record)
        if pending_terminal:
            maximum = max(pending_terminal)
            if set(pending_terminal) != set(range(1, maximum + 1)):
                raise corpus50.Corpus50Error(
                    f"pending stress {pending_key} terminal history is not a contiguous prefix"
                )
            selected_ranks: list[int] = []
            for rank, events in pending_terminal.items():
                signatures = {
                    (
                        str(item.get("outcome", {}).get("status")),
                        int(item.get("candidate", {}).get("repo_id", -1)),
                    )
                    for item in events
                }
                expected_repo_id = int(pending_order[rank - 1]["repo_id"])
                allowed = {("rejected", expected_repo_id), ("selected", expected_repo_id)}
                if len(signatures) != 1 or not signatures.issubset(allowed):
                    raise corpus50.Corpus50Error(
                        f"pending stress {pending_key} has conflicting history at rank {rank}"
                    )
                status, _repo_id = next(iter(signatures))
                if status == "selected":
                    selected_ranks.append(rank)
            if selected_ranks and selected_ranks != [maximum]:
                raise corpus50.Corpus50Error(
                    f"pending stress {pending_key} has a non-final or repeated selected outcome"
                )
            if selected_ranks:
                for rank in range(1, maximum):
                    statuses = {
                        str(item.get("outcome", {}).get("status"))
                        for item in pending_terminal[rank]
                    }
                    if statuses != {"rejected"}:
                        raise corpus50.Corpus50Error(
                            f"pending stress {pending_key} selected before rejecting its prefix"
                        )
            pending_stress = {
                "stress_key": pending_key,
                "terminal_prefix_count": maximum,
                "selected_transaction_pending": bool(selected_ranks),
            }

    base_check: dict[str, Any] | None = None
    if require_base:
        base_frame = corpus50.read_jsonl(frame_root / "frames" / "base-active.jsonl")
        for expected_rank, candidate in enumerate(base_frame, start=1):
            if int(candidate.get("base_rank", -1)) != expected_rank:
                raise corpus50.Corpus50Error("base-active ranks are not contiguous and one-based")
        eligible_path = frame_root / "frames" / "eligible-base.jsonl"
        selected_path = frame_root / "frames" / "base-selected.json"
        eligible = _read_rows(eligible_path)
        selected_document = corpus50.read_json(selected_path)
        if not isinstance(selected_document, dict) or selected_document.get("status") != "selected":
            raise corpus50.Corpus50Error("base-selected is not a completed solver document")
        recomputed = corpus50.solve_base_selection(
            eligible,
            active_frame_exhausted=selected_document.get("active_frame_exhausted") is True,
        )
        if recomputed.get("status") != "selected":
            raise corpus50.Corpus50Error("eligible-base no longer yields a selected solution")
        if corpus50.canonical_json_bytes(selected_document) != corpus50.canonical_json_bytes(
            recomputed
        ):
            raise corpus50.Corpus50Error(
                "base-selected is not the canonical recomputation from eligible-base"
            )
        stored_ids = [int(item["repo_id"]) for item in selected_document.get("selected", [])]
        recomputed_ids = [int(row["repo_id"]) for row in recomputed.get("selected", [])]
        if stored_ids != recomputed_ids:
            raise corpus50.Corpus50Error(
                "base-selected does not equal the recomputed first-feasible lexicographic solution"
            )

        terminal: dict[int, list[dict[str, Any]]] = {}
        for record in records:
            candidate = record.get("candidate")
            outcome = record.get("outcome")
            if not isinstance(candidate, dict) or not isinstance(outcome, dict):
                continue
            rank_value = candidate.get("base_rank")
            if rank_value is None:
                continue
            rank = int(rank_value)
            terminal_status: str | None = None
            if (
                record.get("event_type") == "candidate_screened"
                and candidate.get("cohort") == "base"
                and outcome.get("status") in {"rejected", "eligible"}
            ):
                terminal_status = str(outcome.get("status"))
            elif (
                record.get("event_type") == "base_candidate_removed_as_stress"
                and outcome.get("status") == "excluded"
            ):
                terminal_status = "excluded"
            if terminal_status is None:
                continue
            if rank < 1 or rank > len(base_frame):
                raise corpus50.Corpus50Error("base ledger has an out-of-frame terminal rank")
            if not candidate_matches(candidate, base_frame[rank - 1], "base_rank"):
                raise corpus50.Corpus50Error(
                    f"base ledger candidate differs from frozen rank {rank}"
                )
            terminal.setdefault(rank, []).append(record)

        exhausted = recomputed.get("active_frame_exhausted") is True
        final_rank = (
            len(base_frame)
            if exhausted
            else int(recomputed.get("first_feasible_base_rank") or 0)
        )
        if final_rank < 1:
            raise corpus50.Corpus50Error("selected base solution has no verified terminal prefix")
        later = sorted(rank for rank in terminal if rank > final_rank)
        if later:
            raise corpus50.Corpus50Error(
                f"base ledger has terminal outcomes after the decisive prefix: {later[:20]}"
            )
        missing_ranks = [rank for rank in range(1, final_rank + 1) if rank not in terminal]
        if missing_ranks:
            raise corpus50.Corpus50Error(
                f"base prefix lacks terminal ledger outcomes at ranks {missing_ranks[:20]}"
            )
        stress_ids = {int(row["repo_id"]) for row in stress_rows}
        ledger_eligible: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
        for rank in range(1, final_rank + 1):
            events = terminal[rank]
            signatures = {
                (
                    str(item.get("outcome", {}).get("status")),
                    int(item.get("candidate", {}).get("repo_id", -1)),
                )
                for item in events
            }
            if len(signatures) != 1:
                raise corpus50.Corpus50Error(
                    f"base rank {rank} has conflicting terminal history"
                )
            status, repo_id = next(iter(signatures))
            if repo_id in stress_ids and status != "excluded":
                raise corpus50.Corpus50Error(
                    f"selected stress member at base rank {rank} was not removed before solving"
                )
            if status == "excluded" and repo_id not in stress_ids:
                raise corpus50.Corpus50Error(
                    f"base rank {rank} was excluded without being a selected stress member"
                )
            if status == "eligible":
                for item in events:
                    head = str(item.get("measurements", {}).get("head", "")).casefold()
                    ledger_eligible.setdefault((repo_id, rank, head), []).append(item)

        eligible_keys: set[tuple[int, int, str]] = set()
        for row in eligible:
            rank = int(row.get("base_rank", -1))
            if rank < 1 or rank > final_rank or not candidate_matches(
                row, base_frame[rank - 1], "base_rank"
            ):
                raise corpus50.Corpus50Error(
                    f"eligible base row is not its frozen frame candidate: {row.get('name')}"
                )
            key = (
                int(row["repo_id"]),
                rank,
                str(row.get("head", "")).casefold(),
            )
            if key in eligible_keys:
                raise corpus50.Corpus50Error("eligible-base contains a duplicate durable row")
            eligible_keys.add(key)
            matching = ledger_eligible.get(key, [])
            if not matching:
                raise corpus50.Corpus50Error(
                    f"eligible base row lacks matching ledger evidence: {row.get('name')}"
                )
            expected_measurements = {
                "head": str(row.get("head", "")).casefold(),
                "first_parent_commit_count": int(row.get("first_parent_commit_count", -1)),
                "reachable_commit_count": int(row.get("reachable_commit_count", -1)),
                "primary_language": row.get("primary_language"),
                "language_stratum": row.get("language_stratum"),
                "layout_stratum": row.get("layout_stratum"),
            }
            for record in matching:
                measurements = record.get("measurements", {})
                observed_measurements = {
                    "head": str(measurements.get("head", "")).casefold(),
                    "first_parent_commit_count": int(
                        measurements.get("first_parent_commit_count", -1)
                    ),
                    "reachable_commit_count": int(
                        measurements.get("reachable_commit_count", -1)
                    ),
                    "primary_language": measurements.get("primary_language"),
                    "language_stratum": measurements.get("language_stratum"),
                    "layout_stratum": measurements.get("layout_stratum"),
                }
                if observed_measurements != expected_measurements:
                    raise corpus50.Corpus50Error(
                        f"eligible base row differs from ledger measurements: {row.get('name')}"
                    )
        if eligible_keys != set(ledger_eligible):
            raise corpus50.Corpus50Error(
                "eligible-base rows do not exactly equal eligible terminal ledger outcomes"
            )
        selected_by_id = {int(row["repo_id"]): row for row in eligible}
        for repo_id in recomputed_ids:
            _validate_selected_clone(selected_by_id[repo_id])
        base_check = {
            "eligible_count": len(eligible),
            "selected_count": len(recomputed_ids),
            "first_feasible_base_rank": final_rank,
            "active_frame_exhausted": exhausted,
            "selected_repo_ids": recomputed_ids,
        }

    evidence = {
        "schema_version": corpus50.SCHEMA_VERSION,
        "rule_id": corpus50.RULE_ID,
        "verified_at_utc": corpus50.utc_now(),
        "rule_freeze": _validate_rule_freeze(frame_root, ledger_path),
        "frame_verification": frame_evidence,
        "ledger_verification": ledger_evidence,
        "stress": stress_checks,
        "pending_stress": pending_stress,
        "base": base_check,
        "retained_anchors": _validate_anchor_freeze() if require_base else None,
    }
    corpus50.atomic_write_json(
        frame_root / "manifests" / "selection-provenance.json", evidence
    )
    return evidence


def _screen_output_path(frame_root: Path, cohort: str, key: str | int) -> Path:
    root = frame_root / "state" / "screen-outcomes"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{cohort}-{key}.json"


def _validate_pending_base_provenance(
    frame_root: Path,
    ledger_path: Path,
    stress_rows: list[dict[str, Any]],
    eligible_path: Path,
) -> dict[str, Any]:
    """Validate a resumable base prefix before the next candidate is screened."""
    corpus50.verify_hash_chain(ledger_path)
    records = corpus50.read_jsonl(ledger_path)
    frame = corpus50.read_jsonl(frame_root / "frames" / "base-active.jsonl")
    for rank, row in enumerate(frame, start=1):
        if int(row.get("base_rank", -1)) != rank:
            raise corpus50.Corpus50Error("base-active ranks are not contiguous and one-based")

    def matches(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
        actual_name = actual.get("name") or actual.get("clone_name")
        expected_name = expected.get("clone_name") or expected.get("name")
        return (
            int(actual.get("repo_id", -1)) == int(expected.get("repo_id", -2))
            and int(actual.get("base_rank", -1)) == int(expected.get("base_rank", -2))
            and str(actual.get("priority_key", ""))
            == str(expected.get("priority_key", ""))
            and str(actual_name).casefold() == str(expected_name).casefold()
        )

    terminal: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        candidate = record.get("candidate")
        outcome = record.get("outcome")
        if not isinstance(candidate, dict) or not isinstance(outcome, dict):
            continue
        status: str | None = None
        if (
            record.get("event_type") == "candidate_screened"
            and candidate.get("cohort") == "base"
            and outcome.get("status") in {"rejected", "eligible"}
        ):
            status = str(outcome.get("status"))
        elif (
            record.get("event_type") == "base_candidate_removed_as_stress"
            and outcome.get("status") == "excluded"
        ):
            status = "excluded"
        if status is None:
            continue
        rank = int(candidate.get("base_rank", -1))
        if rank < 1 or rank > len(frame) or not matches(candidate, frame[rank - 1]):
            raise corpus50.Corpus50Error(
                f"pending base ledger differs from frozen rank {rank}"
            )
        terminal.setdefault(rank, []).append(record)

    eligible = _read_rows(eligible_path)
    if not terminal:
        if eligible:
            raise corpus50.Corpus50Error(
                "eligible-base has rows but the base ledger has no terminal prefix"
            )
        evidence = {
            "rule_id": corpus50.RULE_ID,
            "verified_at_utc": corpus50.utc_now(),
            "terminal_prefix_count": 0,
            "eligible_count": 0,
            "eligible_transaction_pending": False,
        }
        corpus50.atomic_write_json(
            frame_root / "manifests" / "pending-base-provenance.json", evidence
        )
        return evidence

    maximum = max(terminal)
    if set(terminal) != set(range(1, maximum + 1)):
        raise corpus50.Corpus50Error("pending base terminal history is not a contiguous prefix")
    stress_ids = {int(row["repo_id"]) for row in stress_rows}
    ledger_eligible: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
    for rank, events in terminal.items():
        signatures = {
            (
                str(item.get("outcome", {}).get("status")),
                int(item.get("candidate", {}).get("repo_id", -1)),
            )
            for item in events
        }
        if len(signatures) != 1:
            raise corpus50.Corpus50Error(
                f"pending base rank {rank} has conflicting terminal history"
            )
        status, repo_id = next(iter(signatures))
        if repo_id in stress_ids and status != "excluded":
            raise corpus50.Corpus50Error(
                f"selected stress member at pending base rank {rank} was not excluded"
            )
        if status == "excluded" and repo_id not in stress_ids:
            raise corpus50.Corpus50Error(
                f"pending base rank {rank} excluded a non-stress member"
            )
        if status == "eligible":
            for record in events:
                head = str(record.get("measurements", {}).get("head", "")).casefold()
                ledger_eligible.setdefault((repo_id, rank, head), []).append(record)

    durable_keys: set[tuple[int, int, str]] = set()
    for row in eligible:
        rank = int(row.get("base_rank", -1))
        if rank < 1 or rank > maximum or not matches(row, frame[rank - 1]):
            raise corpus50.Corpus50Error(
                f"durable eligible row differs from pending base rank {rank}"
            )
        key = (int(row["repo_id"]), rank, str(row.get("head", "")).casefold())
        if key in durable_keys or key not in ledger_eligible:
            raise corpus50.Corpus50Error(
                f"durable eligible row lacks unique terminal evidence at rank {rank}"
            )
        durable_keys.add(key)
        expected_measurements = {
            "head": key[2],
            "first_parent_commit_count": int(row.get("first_parent_commit_count", -1)),
            "reachable_commit_count": int(row.get("reachable_commit_count", -1)),
            "primary_language": row.get("primary_language"),
            "language_stratum": row.get("language_stratum"),
            "layout_stratum": row.get("layout_stratum"),
        }
        for record in ledger_eligible[key]:
            measurements = record.get("measurements", {})
            observed = {
                "head": str(measurements.get("head", "")).casefold(),
                "first_parent_commit_count": int(
                    measurements.get("first_parent_commit_count", -1)
                ),
                "reachable_commit_count": int(
                    measurements.get("reachable_commit_count", -1)
                ),
                "primary_language": measurements.get("primary_language"),
                "language_stratum": measurements.get("language_stratum"),
                "layout_stratum": measurements.get("layout_stratum"),
            }
            if observed != expected_measurements:
                raise corpus50.Corpus50Error(
                    f"durable eligible row differs from ledger measurements at rank {rank}"
                )

    dangling = set(ledger_eligible) - durable_keys
    transaction_pending = False
    if dangling:
        durable_ranks = {key[1] for key in durable_keys}
        if (
            len(dangling) != 1
            or next(iter(dangling))[1] != maximum
            or maximum in durable_ranks
        ):
            raise corpus50.Corpus50Error(
                "pending base has eligible terminal evidence missing from durable rows"
            )
        transaction_pending = True
    evidence = {
        "rule_id": corpus50.RULE_ID,
        "verified_at_utc": corpus50.utc_now(),
        "terminal_prefix_count": maximum,
        "eligible_count": len(eligible),
        "eligible_transaction_pending": transaction_pending,
    }
    corpus50.atomic_write_json(
        frame_root / "manifests" / "pending-base-provenance.json", evidence
    )
    return evidence


def run_stress(args: argparse.Namespace) -> int:
    frame_root = args.frame_root.resolve()
    _verify_frozen_frames(frame_root)
    stress_selected_path = frame_root / "frames" / "stress-selected.jsonl"
    selected = _selected_stress(_read_rows(stress_selected_path))
    processed = 0

    if args.ledger.exists() and args.ledger.stat().st_size:
        _validate_selection_provenance(frame_root, args.ledger, require_base=False)

    if args.stress_key:
        target_index = corpus50.STRESS_KEYS.index(args.stress_key)
        missing_prior = [
            key for key in corpus50.STRESS_KEYS[:target_index] if key not in selected
        ]
        if missing_prior:
            raise corpus50.Corpus50Error(
                f"cannot screen {args.stress_key} before fixed prior slots: {missing_prior}"
            )
        if args.stress_key in selected:
            print(f"stress {args.stress_key}: already selected {selected[args.stress_key]['name']}")
            return 0

    for stress_key in corpus50.STRESS_KEYS:
        if stress_key in selected:
            print(f"stress {stress_key}: already selected {selected[stress_key]['name']}", flush=True)
            continue
        if args.stress_key and stress_key != args.stress_key:
            continue
        excluded_ids = [int(row["repo_id"]) for row in selected.values()]
        order_path = frame_root / "frames" / f"order-{stress_key}.jsonl"
        order = corpus50.stress_candidate_order(
            frame_root, stress_key, excluded_repo_ids=excluded_ids
        )
        corpus50.atomic_write_bytes(
            order_path,
            b"".join(corpus50.canonical_json_bytes(row) for row in order),
        )
        completed = _terminal_ranks(
            args.ledger, cohort="stress", stress_key=stress_key
        )
        durable_rows = {int(row["repo_id"]) for row in _read_rows(stress_selected_path)}
        # screen_candidate makes the terminal ledger append immediately before
        # the selected JSONL append. If interrupted in that narrow window,
        # re-run the same rank idempotently instead of skipping to a second
        # stress member.
        for rank, (status, repo_id) in _terminal_candidate_outcomes(
            args.ledger, cohort="stress", stress_key=stress_key
        ).items():
            if status == "selected" and repo_id not in durable_rows:
                completed.discard(rank)
        for candidate in order:
            rank = int(candidate["candidate_order"])
            if rank in completed:
                continue
            reviewed = (
                args.reviewed_predicate
                if stress_key == "non_english" and rank == args.reviewed_rank
                else None
            )
            included = [stress_selected_path] if stress_selected_path.exists() else []
            result = corpus50.screen_candidate(
                candidate,
                cohort="stress",
                stress_key=stress_key,
                frame_root=frame_root,
                project_root=PROJECT_ROOT,
                ledger_path=args.ledger,
                output_path=stress_selected_path,
                included_member_paths=included,
                account_paths=args.account_path,
                reviewed_predicate_path=reviewed,
            )
            _atomic_json(_screen_output_path(frame_root, stress_key, rank), result)
            processed += 1
            outcome = result["outcome"]
            print(
                json.dumps(
                    {
                        "stress_key": stress_key,
                        "candidate_order": rank,
                        "name": candidate.get("clone_name"),
                        "status": outcome.get("status"),
                        "reason": outcome.get("reason"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            if outcome.get("status") == "selected":
                selected = _selected_stress(_read_rows(stress_selected_path))
                _validate_selection_provenance(
                    frame_root, args.ledger, require_base=False
                )
                break
            if outcome.get("status") == "review_required":
                print(
                    "non-English evidence requires review; inspect the saved outcome and "
                    "rerun this rank with --reviewed-predicate",
                    file=sys.stderr,
                    flush=True,
                )
                return 3
            if args.max_candidates and processed >= args.max_candidates:
                return 4
        else:
            raise corpus50.Corpus50Error(f"stress frame exhausted for {stress_key}")

    if args.stress_key:
        return 0 if args.stress_key in selected else 4
    return 0 if len(selected) == len(corpus50.STRESS_KEYS) else 4


def _cleanup_unselected_base_if_needed(
    frame_root: Path, eligible_path: Path, selected_path: Path, ledger_path: Path
) -> None:
    eligible = _read_rows(eligible_path)
    selected_document = corpus50.read_json(selected_path)
    selected_ids = {
        int(row["repo_id"]) for row in selected_document.get("selected", [])
    }
    clone_root = PROJECT_ROOT / "corpus" / "_clones"
    needs_cleanup = any(
        int(row["repo_id"]) not in selected_ids
        and (clone_root / corpus50.slug_for_name(str(row.get("name") or row.get("clone_name")))).exists()
        for row in eligible
    )
    if needs_cleanup:
        corpus50.cleanup_unselected_base_clones(
            eligible_path=eligible_path,
            selected_path=selected_path,
            frame_root=frame_root,
            project_root=PROJECT_ROOT,
            ledger_path=ledger_path,
        )


def run_base(args: argparse.Namespace) -> int:
    frame_root = args.frame_root.resolve()
    _verify_frozen_frames(frame_root)
    listing_path = frame_root / "frames" / "base-active.jsonl"
    eligible_path = frame_root / "frames" / "eligible-base.jsonl"
    selected_path = frame_root / "frames" / "base-selected.json"
    stress_selected_path = frame_root / "frames" / "stress-selected.jsonl"
    stress_rows = _read_rows(stress_selected_path)
    stress_keys = [row.get("stress_key") for row in stress_rows]
    stress_ids = [row.get("repo_id") for row in stress_rows]
    if (
        len(stress_rows) != len(corpus50.STRESS_KEYS)
        or set(stress_keys) != set(corpus50.STRESS_KEYS)
        or len(set(stress_ids)) != len(corpus50.STRESS_KEYS)
    ):
        raise corpus50.Corpus50Error(
            "base screening requires exactly one distinct selected member for each fixed stress key"
        )
    _validate_selection_provenance(frame_root, args.ledger, require_base=False)
    _validate_pending_base_provenance(
        frame_root, args.ledger, stress_rows, eligible_path
    )

    if selected_path.exists():
        existing = corpus50.read_json(selected_path)
        if isinstance(existing, dict) and existing.get("status") == "selected":
            _validate_selection_provenance(frame_root, args.ledger, require_base=True)
            _cleanup_unselected_base_if_needed(
                frame_root, eligible_path, selected_path, args.ledger
            )
            _validate_selection_provenance(frame_root, args.ledger, require_base=True)
            print(json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

    durable_eligible = _read_rows(eligible_path)
    if len(durable_eligible) >= 35:
        recovered_solution = corpus50.solve_base_selection(durable_eligible)
        if recovered_solution.get("status") == "selected":
            _atomic_json(selected_path, recovered_solution)
            _validate_selection_provenance(
                frame_root, args.ledger, require_base=True
            )
            _cleanup_unselected_base_if_needed(
                frame_root, eligible_path, selected_path, args.ledger
            )
            _validate_selection_provenance(
                frame_root, args.ledger, require_base=True
            )
            print(
                json.dumps(
                    {
                        "solver_status": "selected",
                        "first_feasible_base_rank": recovered_solution.get(
                            "first_feasible_base_rank"
                        ),
                        "recovered_from_durable_prefix": True,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            return 0

    candidates = corpus50.read_jsonl(listing_path)
    completed = _terminal_ranks(args.ledger, cohort="base")
    durable_eligible_ids = {int(row["repo_id"]) for row in _read_rows(eligible_path)}
    for rank, (status, repo_id) in _terminal_candidate_outcomes(
        args.ledger, cohort="base"
    ).items():
        if status == "eligible" and repo_id not in durable_eligible_ids:
            completed.discard(rank)
    stress_by_id = {int(row["repo_id"]): row for row in stress_rows}
    processed = 0
    for candidate in candidates:
        rank = int(candidate["base_rank"])
        if rank in completed:
            continue
        repo_id = int(candidate["repo_id"])
        if repo_id in stress_by_id:
            corpus50.append_selection_event(
                args.ledger,
                {
                    "event_type": "base_candidate_removed_as_stress",
                    "candidate": {
                        "repo_id": repo_id,
                        "name": candidate.get("clone_name"),
                        "url": candidate.get("url"),
                        "cohort": "base",
                        "base_rank": rank,
                        "priority_key": candidate.get("priority_key"),
                        "stress_key": stress_by_id[repo_id].get("stress_key"),
                    },
                    "outcome": {
                        "status": "excluded",
                        "reason": "selected_stress_member_removed_before_base_margin_solver",
                    },
                },
            )
            print(
                json.dumps(
                    {
                        "base_rank": rank,
                        "name": candidate.get("clone_name"),
                        "status": "excluded",
                        "reason": "selected_stress_member_removed_before_base_margin_solver",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            continue
        result = corpus50.screen_candidate(
            candidate,
            cohort="base",
            stress_key=None,
            frame_root=frame_root,
            project_root=PROJECT_ROOT,
            ledger_path=args.ledger,
            output_path=eligible_path,
            included_member_paths=[stress_selected_path],
            account_paths=args.account_path,
        )
        _atomic_json(_screen_output_path(frame_root, "base", rank), result)
        processed += 1
        outcome = result["outcome"]
        eligible_count = len(_read_rows(eligible_path))
        print(
            json.dumps(
                {
                    "base_rank": rank,
                    "name": candidate.get("clone_name"),
                    "status": outcome.get("status"),
                    "reason": outcome.get("reason"),
                    "eligible_count": eligible_count,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )

        if outcome.get("status") == "eligible" and eligible_count >= 35:
            solution = corpus50.solve_base_selection(_read_rows(eligible_path))
            _atomic_json(selected_path, solution)
            print(
                json.dumps(
                    {
                        "solver_status": solution.get("status"),
                        "first_feasible_base_rank": solution.get("first_feasible_base_rank"),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if solution.get("status") == "selected":
                _validate_selection_provenance(
                    frame_root, args.ledger, require_base=True
                )
                _cleanup_unselected_base_if_needed(
                    frame_root, eligible_path, selected_path, args.ledger
                )
                _validate_selection_provenance(
                    frame_root, args.ledger, require_base=True
                )
                return 0
        if args.max_candidates and processed >= args.max_candidates:
            return 4

    solution = corpus50.solve_base_selection(
        _read_rows(eligible_path), active_frame_exhausted=True
    )
    _atomic_json(selected_path, solution)
    if solution.get("status") == "selected":
        _validate_selection_provenance(
            frame_root, args.ledger, require_base=True
        )
        _cleanup_unselected_base_if_needed(
            frame_root, eligible_path, selected_path, args.ledger
        )
        _validate_selection_provenance(
            frame_root, args.ledger, require_base=True
        )
        return 0
    return 5


def assemble(args: argparse.Namespace) -> int:
    frame_root = args.frame_root.resolve()
    provenance = _validate_selection_provenance(
        frame_root, args.ledger, require_base=True
    )
    output_path = PROJECT_ROOT / "corpus" / "CORPUS-50.json"
    manifest = corpus50.assemble_corpus_manifest(
        frame_root=frame_root,
        stress_selected_path=frame_root / "frames" / "stress-selected.jsonl",
        base_selected_path=frame_root / "frames" / "base-selected.json",
        project_root=PROJECT_ROOT,
        accounted_paths=args.account_path,
        output_path=output_path,
    )
    provenance_path = frame_root / "manifests" / "selection-provenance.json"
    provenance_digest, provenance_length = corpus50.sha256_file(provenance_path)
    ledger_digest, ledger_length = corpus50.sha256_file(args.ledger)
    manifest["selection_provenance"] = {
        "path": str(provenance_path.resolve()),
        "sha256": provenance_digest,
        "byte_length": provenance_length,
        "ledger_path": str(args.ledger.resolve()),
        "ledger_sha256": ledger_digest,
        "ledger_byte_length": ledger_length,
        "ledger_verification": provenance.get("ledger_verification"),
    }
    manifest["rule_freeze_provenance"] = provenance.get("rule_freeze")
    corpus50.atomic_write_json(output_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cleanup_rejected(args: argparse.Namespace) -> int:
    frame_root = args.frame_root.resolve()
    if not args.ledger.exists():
        return 0
    corpus50.verify_hash_chain(args.ledger)
    records = corpus50.read_jsonl(args.ledger)
    rejected: dict[int, dict[str, Any]] = {}
    retained: set[int] = set()
    for record in records:
        if record.get("event_type") != "candidate_screened":
            continue
        candidate = record.get("candidate")
        outcome = record.get("outcome")
        if not isinstance(candidate, dict) or not isinstance(outcome, dict):
            continue
        repo_id = candidate.get("repo_id")
        if repo_id is None:
            continue
        numeric_id = int(repo_id)
        if outcome.get("status") == "rejected":
            rejected[numeric_id] = candidate
        elif outcome.get("status") in {"selected", "eligible", "review_required"}:
            retained.add(numeric_id)

    clone_root = (PROJECT_ROOT / "corpus" / "_clones").resolve()
    ownership_root = frame_root / "state" / "screening-clones"
    failures = 0
    for repo_id, candidate in sorted(rejected.items()):
        if repo_id in retained:
            continue
        name = str(candidate["name"])
        slug = corpus50.slug_for_name(name)
        ownership_path = ownership_root / f"{slug}.json"
        if not ownership_path.exists():
            continue
        ownership = corpus50.read_json(ownership_path)
        destination = clone_root / slug
        if not destination.exists():
            continue
        size_before = corpus50._clone_size(destination)
        try:
            corpus50._safe_remove_owned_clone(destination, clone_root, ownership)
            ownership["status"] = "cleaned_after_rejection_retry"
            ownership["cleaned_at_utc"] = corpus50.utc_now()
            corpus50.atomic_write_json(ownership_path, ownership)
            corpus50.append_selection_event(
                args.ledger,
                {
                    "event_type": "screening_clone_cleanup",
                    "candidate": candidate,
                    "outcome": {
                        "status": "complete",
                        "reason": "retry_after_windows_readonly_pack_cleanup",
                    },
                    "measurements": {"reclaimed_bytes": size_before},
                    "artifacts": {"removed_path": str(destination)},
                },
            )
            print(f"cleaned {name} ({size_before} bytes)", flush=True)
        except Exception as error:
            failures += 1
            corpus50.append_selection_event(
                args.ledger,
                {
                    "event_type": "screening_clone_cleanup",
                    "candidate": candidate,
                    "outcome": {
                        "status": "failed",
                        "reason": type(error).__name__,
                        "detail": str(error),
                    },
                    "artifacts": {"retained_path": str(destination)},
                },
            )
            print(f"cleanup failed for {name}: {error}", file=sys.stderr, flush=True)
    return 0 if failures == 0 else 6


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("stress", "base", "assemble", "cleanup"))
    parser.add_argument("--frame-root", type=Path, default=DEFAULT_FRAME_ROOT)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument(
        "--account-path",
        action="append",
        type=Path,
        default=[PROJECT_ROOT / "exploratory" / "language-hole"],
    )
    parser.add_argument("--max-candidates", type=int, default=0)
    parser.add_argument("--stress-key", choices=corpus50.STRESS_KEYS)
    parser.add_argument("--reviewed-predicate", type=Path)
    parser.add_argument("--reviewed-rank", type=int)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if (args.reviewed_predicate is None) != (args.reviewed_rank is None):
            raise corpus50.Corpus50Error(
                "--reviewed-predicate and --reviewed-rank must be supplied together"
            )
        if args.phase == "stress":
            return run_stress(args)
        if args.phase == "base":
            return run_base(args)
        if args.phase == "cleanup":
            return cleanup_rejected(args)
        return assemble(args)
    except corpus50.Corpus50Error as error:
        print(f"corpus50_select: ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
