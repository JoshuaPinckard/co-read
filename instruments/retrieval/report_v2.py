"""Render the corrected, multi-tree retrieval benchmark report.

This module deliberately does not execute queries.  It consumes the auditable
artifacts produced by ``run_v2.py``, independently recomputes the retrieval
metrics from eval, provenance, and run rows, and refuses to publish a final
report when the run was capped, marked as debug, incomplete, internally
inconsistent, or no longer matches its recorded artifact hashes.

Required runner summary schema (``retrieval-run-v2/1``)::

    {
      "schema_version": "retrieval-run-v2/1",
      "complete": true,
      "debug": false,
      "final": true,
      "windows_seconds": [60, 300, 900],
      "arms": ["ripgrep", "bm25", "ident_first", "bm25_pathboost",
               "bm25_legacy"],
      "query_cap": null,
      "artifacts": {
        "runs": {"path": "...jsonl", "sha256": "...", "rows": 123},
        "provenance": {"path": "...jsonl", "sha256": "...", "rows": 123},
        "metrics": {"path": "...json", "sha256": "..."},
        "catalog": {"path": "...json", "sha256": "..."},
        "retention": {"path": "...json", "sha256": "..."}
      },
      "execution": {
        "query_order_seed": 20260823,
        "warmup": {},
        "index_caps": {}
      },
      "reconstruction": {
        "non_git_fallback_queries_by_window": {"60": 0, "300": 0, "900": 0},
        "non_git_unscored_queries_by_window": {"60": 0, "300": 0, "900": 0},
        "dirty_state_unreconstructable_queries_by_window": {"60": 0, "300": 0,
                                                              "900": 0}
      },
      "arm_unavailability_by_tree": {"tree-id": {"arm": "reason"}}
    }

Relative artifact paths are resolved first against the summary directory and
then against the current working directory.  ``rows`` is optional, but is
checked against the non-blank JSONL row count when supplied.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping, Sequence

try:  # package import
    from .metrics_v2 import aggregate_metrics_v2
except ImportError:  # direct script execution
    from metrics_v2 import aggregate_metrics_v2


WINDOWS = (60, 300, 900)
PRIMARY_WINDOW = "300"
PRIMARY_ARMS = ("ripgrep", "bm25", "ident_first", "bm25_pathboost")
ALL_ARMS = (*PRIMARY_ARMS, "bm25_legacy")
FALLBACK_MODES = frozenset({"head_fallback", "non_git_current_fallback"})
KS = (1, 5, 10, 20)
RUN_SCHEMA = "retrieval-run-v2/1"
CATALOG_SCHEMA = "tree-catalog-v2/1"
EVAL_FILES = {
    "60": "evalset_60.jsonl",
    "300": "evalset.jsonl",
    "900": "evalset_900.jsonl",
}


class ReportInputError(ValueError):
    """Raised when publishing would turn an incomplete run into a result."""


@dataclass(frozen=True)
class ReportBundle:
    eval_dir: Path
    metrics_path: Path
    retention_path: Path
    catalog_path: Path
    summary_path: Path
    metrics: dict[str, Any]
    retention: dict[str, Any]
    catalog: dict[str, Any]
    summary: dict[str, Any]
    eval_ids: dict[str, set[str]]
    provenance: dict[str, dict[str, Any]]
    complete_run_ids: set[str]
    audit: dict[str, Any]


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReportInputError(f"missing {label}: {path}") from error
    except json.JSONDecodeError as error:
        raise ReportInputError(f"invalid JSON in {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReportInputError(f"{label} must contain a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_artifact_path(raw: str, summary_path: Path) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    local = (summary_path.parent / candidate).resolve()
    if local.exists():
        return local
    return (Path.cwd() / candidate).resolve()


def _artifact_paths(summary: Mapping[str, Any], summary_path: Path) -> dict[str, Path]:
    artifacts = summary.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ReportInputError("run summary lacks an artifacts object")
    required = ("runs", "provenance", "metrics", "catalog", "retention")
    result: dict[str, Path] = {}
    for name in required:
        descriptor = artifacts.get(name)
        if not isinstance(descriptor, Mapping):
            raise ReportInputError(f"run summary lacks artifacts.{name}")
        raw_path = descriptor.get("path")
        expected_hash = descriptor.get("sha256")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ReportInputError(f"artifacts.{name}.path must be a non-empty string")
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in expected_hash)
        ):
            raise ReportInputError(f"artifacts.{name}.sha256 must be a 64-digit SHA-256")
        path = _resolve_artifact_path(raw_path, summary_path)
        if not path.is_file():
            raise ReportInputError(f"artifacts.{name} does not exist: {path}")
        actual_hash = _sha256(path)
        if actual_hash.casefold() != expected_hash.casefold():
            raise ReportInputError(
                f"artifacts.{name} hash mismatch: expected {expected_hash}, got {actual_hash}"
            )
        result[name] = path
    return result


def _read_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ReportInputError(f"invalid JSON at {label}:{line_number}: {error}") from error
            if not isinstance(value, dict):
                raise ReportInputError(f"{label}:{line_number} is not a JSON object")
            result.append(value)
    return result


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReportInputError(f"{label} must be an integer >= {minimum}")
    return value


def _validate_summary(summary: Mapping[str, Any]) -> None:
    if summary.get("schema_version") != RUN_SCHEMA:
        raise ReportInputError(
            f"run summary schema must be {RUN_SCHEMA!r}, got {summary.get('schema_version')!r}"
        )
    if summary.get("complete") is not True:
        raise ReportInputError("refusing to report an incomplete benchmark run")
    if summary.get("debug") is not False:
        raise ReportInputError("refusing to report a debug benchmark run")
    if summary.get("final") is not True:
        raise ReportInputError("refusing to report a run not marked final")
    if summary.get("query_cap") is not None:
        raise ReportInputError("refusing to report a query-capped benchmark run")
    if summary.get("windows_seconds") != list(WINDOWS):
        raise ReportInputError(
            f"run summary windows_seconds must be {list(WINDOWS)!r} in prespecified order"
        )
    if summary.get("arms") != list(ALL_ARMS):
        raise ReportInputError(f"run summary arms must be exactly {list(ALL_ARMS)!r}")
    fingerprint = summary.get("fingerprint")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in fingerprint)
    ):
        raise ReportInputError("run summary fingerprint must be a 64-digit SHA-256")
    reconstruction = summary.get("reconstruction")
    if not isinstance(reconstruction, Mapping):
        raise ReportInputError("run summary lacks reconstruction counts")
    for field in (
        "exact_queries_by_window",
        "fallback_queries_by_window",
        "non_git_fallback_queries_by_window",
        "non_git_unscored_queries_by_window",
        "dirty_state_unreconstructable_queries_by_window",
        "unscored_or_unavailable_queries_by_window",
    ):
        values = reconstruction.get(field)
        if not isinstance(values, Mapping):
            raise ReportInputError(f"run summary lacks reconstruction.{field}")
        for window in map(str, WINDOWS):
            _require_int(values.get(window), f"reconstruction.{field}.{window}")


def _load_eval_records(eval_dir: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for window, filename in EVAL_FILES.items():
        path = eval_dir / filename
        rows = _read_jsonl(path, f"eval set {window}s")
        ids: set[str] = set()
        for sequence, row in enumerate(rows, 1):
            if row.get("id") is None:
                raise ReportInputError(f"{filename}:{sequence} lacks id")
            record_id = str(row["id"])
            if record_id in ids:
                raise ReportInputError(f"duplicate eval ID in {filename}: {record_id}")
            ids.add(record_id)
        result[window] = rows
    return result


def _load_eval_ids(eval_dir: Path) -> dict[str, set[str]]:
    return {
        window: {str(row["id"]) for row in rows}
        for window, rows in _load_eval_records(eval_dir).items()
    }


def _index_provenance(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for sequence, raw in enumerate(rows, 1):
        record_id = raw.get("record_id", raw.get("id"))
        if record_id is None:
            raise ReportInputError(f"provenance row {sequence} lacks record_id/id")
        key = str(record_id)
        if key in result:
            raise ReportInputError(f"duplicate provenance row for {key}")
        exact = raw.get("exact")
        if not isinstance(exact, bool):
            raise ReportInputError(f"provenance row for {key} lacks boolean exact status")
        if not isinstance(raw.get("mode"), str) or not str(raw.get("mode")).strip():
            raise ReportInputError(f"provenance row for {key} lacks reconstruction mode")
        gap = raw.get("gap_seconds")
        commit = raw.get("commit")
        if exact:
            if not isinstance(commit, str) or not commit:
                raise ReportInputError(f"exact provenance row for {key} lacks a commit")
            if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap):
                raise ReportInputError(f"exact provenance row for {key} lacks a finite time gap")
            if gap < 0:
                raise ReportInputError(
                    f"exact provenance row for {key} chose a commit after the query ({gap}s)"
                )
        elif commit is not None and (
            isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap)
        ):
            raise ReportInputError(f"fallback provenance row for {key} has a commit but no finite gap")
        result[key] = dict(raw)
    return result


def _complete_run_ids(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    arms_by_id: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for sequence, row in enumerate(rows, 1):
        if row.get("record_id") is None or row.get("arm") is None:
            raise ReportInputError(f"run row {sequence} lacks record_id or arm")
        record_id = str(row["record_id"])
        arm = str(row["arm"])
        if arm not in ALL_ARMS:
            raise ReportInputError(f"run row {sequence} names unexpected arm {arm!r}")
        pair = (record_id, arm)
        if pair in seen:
            raise ReportInputError(f"duplicate run row for {record_id}/{arm}")
        seen.add(pair)
        arms_by_id.setdefault(record_id, set()).add(arm)
    required = set(ALL_ARMS)
    return {record_id for record_id, arms in arms_by_id.items() if arms == required}


def _unavailable_run_ids(rows: Sequence[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for sequence, row in enumerate(rows, 1):
        if row.get("unavailable") is not True:
            continue
        reason = row.get("unavailable_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ReportInputError(
                f"unavailable run row {sequence} lacks an explicit reason"
            )
        for field in ("response_bytes", "response_sha256", "latency_ms"):
            if row.get(field) is not None:
                raise ReportInputError(
                    f"unavailable run row {sequence} fabricates {field}"
                )
        if row.get("error") is not None:
            raise ReportInputError(
                f"unavailable run row {sequence} must not be a timed error row"
            )
        result.add(str(row.get("record_id") or ""))
    result.discard("")
    return result


def _numeric_gaps(rows: Iterable[Mapping[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = row.get("gap_seconds")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value):
            values.append(float(value))
    return values


def _nearest_rank(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _gap_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = _numeric_gaps(rows)
    buckets = {
        "lt_minus_7d": 0,
        "minus_7d_to_minus_1d": 0,
        "minus_1d_to_minus_1h": 0,
        "minus_1h_to_0": 0,
        "zero": 0,
        "0_to_1h": 0,
        "1h_to_1d": 0,
        "1d_to_7d": 0,
        "gt_7d": 0,
    }
    for value in values:
        if value < -604_800:
            buckets["lt_minus_7d"] += 1
        elif value < -86_400:
            buckets["minus_7d_to_minus_1d"] += 1
        elif value < -3_600:
            buckets["minus_1d_to_minus_1h"] += 1
        elif value < 0:
            buckets["minus_1h_to_0"] += 1
        elif value == 0:
            buckets["zero"] += 1
        elif value <= 3_600:
            buckets["0_to_1h"] += 1
        elif value <= 86_400:
            buckets["1h_to_1d"] += 1
        elif value <= 604_800:
            buckets["1d_to_7d"] += 1
        else:
            buckets["gt_7d"] += 1
    return {
        "queries": len(rows),
        "count": len(values),
        "missing": len(rows) - len(values),
        "min": min(values) if values else None,
        "median": statistics.median(values) if values else None,
        "mean": statistics.fmean(values) if values else None,
        "p95": _nearest_rank(values, 0.95),
        "p99": _nearest_rank(values, 0.99),
        "max": max(values) if values else None,
        "buckets": buckets,
    }


def _provenance_audit(
    eval_ids: Mapping[str, set[str]], provenance: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for window, ids in eval_ids.items():
        rows = [provenance[record_id] for record_id in sorted(ids)]
        exact = [row for row in rows if row.get("exact") is True]
        fallback = [
            row for row in rows if str(row.get("mode") or "") in FALLBACK_MODES
        ]
        unscored = [
            row
            for row in rows
            if row.get("exact") is not True
            and str(row.get("mode") or "") not in FALLBACK_MODES
        ]
        result[window] = {
            "queries": len(rows),
            "exact_queries": len(exact),
            "fallback_queries": len(fallback),
            "unscored_or_unavailable_queries": len(unscored),
            "mode_counts": dict(sorted(Counter(str(row.get("mode")) for row in rows).items())),
            "fallback_reason_counts": dict(
                sorted(Counter(str(row.get("reason") or "unspecified") for row in fallback).items())
            ),
            "unscored_or_unavailable_reason_counts": dict(
                sorted(Counter(str(row.get("reason") or "unspecified") for row in unscored).items())
            ),
            "exact_gap_seconds": _gap_summary(exact),
            "fallback_gap_seconds": _gap_summary(fallback),
            "unscored_or_unavailable_gap_seconds": _gap_summary(unscored),
        }
    return result


def _validate_artifact_row_count(
    summary: Mapping[str, Any], name: str, observed: int
) -> None:
    descriptor = summary["artifacts"][name]
    if "rows" in descriptor:
        expected = _require_int(descriptor["rows"], f"artifacts.{name}.rows")
        if expected != observed:
            raise ReportInputError(
                f"artifacts.{name}.rows says {expected}, but the file contains {observed} rows"
            )


def _validate_partial_arm_rows(
    summary: Mapping[str, Any],
    catalog: Mapping[str, Any],
    provenance: Mapping[str, Mapping[str, Any]],
    run_rows: Sequence[Mapping[str, Any]],
) -> None:
    raw = summary.get("partial_arm_rows_by_tree") or {}
    if not isinstance(raw, Mapping):
        raise ReportInputError("partial_arm_rows_by_tree must be an object")
    expected: dict[str, dict[str, Counter[str]]] = {
        str(window): defaultdict(Counter) for window in WINDOWS
    }
    for row in run_rows:
        if row.get("unavailable") is True:
            continue
        record_id = str(row.get("record_id") or "")
        item = provenance.get(record_id, {})
        if item.get("target_tree_id") not in (None, "") or not item.get("partial_arms"):
            continue
        tree_id = str(row.get("tree_id") or "")
        arm = str(row.get("arm") or "")
        for window in item.get("windows_seconds") or []:
            if str(window) in expected and tree_id and arm:
                expected[str(window)][tree_id][arm] += 1
    normalised = {
        window: {
            str(tree): {str(arm): int(count) for arm, count in arms.items()}
            for tree, arms in (raw.get(window, {}) or {}).items()
        }
        for window in map(str, WINDOWS)
    }
    expected_plain = {
        window: {
            tree: dict(sorted(arms.items())) for tree, arms in sorted(trees.items())
        }
        for window, trees in expected.items()
    }
    if normalised != expected_plain:
        raise ReportInputError("partial_arm_rows_by_tree disagrees with provenance/run rows")
    catalog_by_id = _catalog_by_id(catalog)
    for window, trees in normalised.items():
        for tree_id, arms in trees.items():
            if tree_id not in catalog_by_id:
                raise ReportInputError(f"partial arm rows name unknown tree {tree_id}")
            targeted = int(
                (catalog_by_id[tree_id].get("target_counts") or {}).get(window, 0) or 0
            )
            for arm, count in arms.items():
                if arm not in ALL_ARMS or count < 0 or count > targeted:
                    raise ReportInputError(
                        f"invalid partial row count {tree_id}/{window}/{arm}={count}"
                    )


def _validate_metrics(
    metrics: Mapping[str, Any],
    retention: Mapping[str, Any],
    catalog: Mapping[str, Any],
    eval_ids: Mapping[str, set[str]],
    provenance: Mapping[str, Mapping[str, Any]],
    complete_run_ids: set[str],
    unavailable_run_ids: set[str],
) -> None:
    if metrics.get("schema_version") != 2:
        raise ReportInputError("metrics schema_version must be 2")
    if metrics.get("arms") != list(ALL_ARMS):
        raise ReportInputError(f"metrics arms must be exactly {list(ALL_ARMS)!r}")
    if metrics.get("ks") != list(KS):
        raise ReportInputError(f"metrics ks must be exactly {list(KS)!r}")
    windows = metrics.get("windows")
    if not isinstance(windows, Mapping) or set(windows) != set(map(str, WINDOWS)):
        raise ReportInputError("metrics must contain exactly the 60, 300, and 900 windows")
    if retention.get("complete") is not True:
        raise ReportInputError("retention extraction is not marked complete")
    retention_windows = retention.get("retention")
    if not isinstance(retention_windows, Mapping):
        raise ReportInputError("retention artifact lacks retention windows")
    if catalog.get("schema_version") != CATALOG_SCHEMA:
        raise ReportInputError(f"catalog schema_version must be {CATALOG_SCHEMA!r}")

    catalog_trees = catalog.get("trees")
    if not isinstance(catalog_trees, list):
        raise ReportInputError("catalog trees must be a list")
    catalog_ids: set[str] = set()
    for tree in catalog_trees:
        if not isinstance(tree, Mapping) or tree.get("tree_id") is None:
            raise ReportInputError("every catalog tree must have a tree_id")
        tree_id = str(tree["tree_id"])
        if tree_id in catalog_ids:
            raise ReportInputError(f"duplicate catalog tree_id: {tree_id}")
        catalog_ids.add(tree_id)
    for record_id, row in provenance.items():
        tree_id = row.get("target_tree_id")
        if tree_id not in (None, "") and str(tree_id) not in catalog_ids:
            raise ReportInputError(
                f"provenance for {record_id} names unknown target tree {tree_id!r}"
            )

    catalog_counts = catalog.get("counts")
    if not isinstance(catalog_counts, Mapping):
        raise ReportInputError("catalog lacks counts")
    record_counts = catalog_counts.get("records_by_window")
    if not isinstance(record_counts, Mapping):
        raise ReportInputError("catalog lacks counts.records_by_window")

    for window in map(str, WINDOWS):
        ids = eval_ids[window]
        retained = len(ids)
        retention_item = retention_windows.get(window)
        if not isinstance(retention_item, Mapping):
            raise ReportInputError(f"retention lacks window {window}")
        if retention_item.get("resolvable") != retained:
            raise ReportInputError(
                f"{window}s eval rows ({retained}) disagree with retention.resolvable "
                f"({retention_item.get('resolvable')})"
            )
        if record_counts.get(window) != retained:
            raise ReportInputError(
                f"{window}s catalog record count ({record_counts.get(window)}) disagrees with eval rows ({retained})"
            )
        block = windows[window]
        if not isinstance(block, Mapping) or not isinstance(block.get("population"), Mapping):
            raise ReportInputError(f"metrics window {window} lacks population")
        population = block["population"]
        mapped_ids = {
            record_id
            for record_id in ids
            if provenance[record_id].get("target_tree_id") not in (None, "")
        }
        unavailable_tree_ids = set(population.get("unavailable_tree_arms") or {})
        paired_ids = {
            record_id
            for record_id in mapped_ids & complete_run_ids
            if str(provenance[record_id].get("target_tree_id")) not in unavailable_tree_ids
            and record_id not in unavailable_run_ids
        }
        expected = {
            "retained_queries": retained,
            "mapped_queries": len(mapped_ids),
            "outside_any_indexed_tree": retained - len(mapped_ids),
            "complete_five_arm_row_queries": len(ids & complete_run_ids),
            "paired_scored_queries": len(paired_ids),
            "paired_excluded_queries": len(mapped_ids - paired_ids),
        }
        for field, expected_value in expected.items():
            if population.get(field) != expected_value:
                raise ReportInputError(
                    f"metrics {window}s population.{field}={population.get(field)!r}; "
                    f"artifact manifests imply {expected_value}"
                )

        tree_blocks = block.get("trees")
        if not isinstance(tree_blocks, Mapping):
            raise ReportInputError(f"metrics window {window} lacks trees")
        if not set(tree_blocks).issubset(catalog_ids):
            unknown = sorted(set(tree_blocks) - catalog_ids)
            raise ReportInputError(f"metrics window {window} contains unknown trees: {unknown}")
        paired_tree_total = 0
        for tree_id, tree_block in tree_blocks.items():
            if not isinstance(tree_block, Mapping) or not isinstance(tree_block.get("population"), Mapping):
                raise ReportInputError(f"metrics tree {tree_id}/{window} lacks population")
            expected_mapped = sum(
                provenance[record_id].get("target_tree_id") == tree_id for record_id in ids
            )
            expected_paired = sum(
                provenance[record_id].get("target_tree_id") == tree_id
                and record_id in complete_run_ids
                and tree_id not in unavailable_tree_ids
                and record_id not in unavailable_run_ids
                for record_id in ids
            )
            tree_population = tree_block["population"]
            if tree_population.get("retained_queries") != expected_mapped:
                raise ReportInputError(
                    f"metrics tree {tree_id}/{window} retained count disagrees with provenance"
                )
            if tree_population.get("paired_scored_queries") != expected_paired:
                raise ReportInputError(
                    f"metrics tree {tree_id}/{window} paired count disagrees with run rows"
                )
            paired_tree_total += expected_paired
        if paired_tree_total != len(paired_ids):
            raise ReportInputError(
                f"metrics {window}s tree populations sum to {paired_tree_total}, expected {len(paired_ids)}"
            )

        reconstruction = block.get("reconstruction")
        if not isinstance(reconstruction, Mapping):
            raise ReportInputError(f"metrics window {window} lacks reconstruction")
        mapped_reconstruction = reconstruction.get("all_mapped_queries")
        if not isinstance(mapped_reconstruction, Mapping):
            raise ReportInputError(f"metrics window {window} lacks all-mapped reconstruction")
        mapped_exact = sum(provenance[item].get("exact") is True for item in mapped_ids)
        if mapped_reconstruction.get("queries") != len(mapped_ids):
            raise ReportInputError(f"metrics {window}s mapped reconstruction query count is inconsistent")
        if mapped_reconstruction.get("exact_queries") != mapped_exact:
            raise ReportInputError(f"metrics {window}s mapped exact count is inconsistent")
        if mapped_reconstruction.get("fallback_queries") != len(mapped_ids) - mapped_exact:
            raise ReportInputError(f"metrics {window}s mapped fallback count is inconsistent")

        arms = block.get("arms")
        if not isinstance(arms, Mapping) or set(arms) != set(ALL_ARMS):
            raise ReportInputError(f"metrics window {window} lacks the exact five-arm table")
        for arm in ALL_ARMS:
            arm_metrics = arms[arm]
            if not isinstance(arm_metrics, Mapping):
                raise ReportInputError(f"metrics {window}s arm {arm} is not an object")
            if arm_metrics.get("queries") != len(paired_ids):
                raise ReportInputError(
                    f"metrics {window}s arm {arm} uses {arm_metrics.get('queries')} queries, "
                    f"but paired population is {len(paired_ids)}"
                )


def load_report_bundle(
    *,
    eval_dir: Path,
    metrics_path: Path,
    retention_path: Path,
    catalog_path: Path,
    summary_path: Path,
) -> ReportBundle:
    """Load, hash-check, and cross-check every artifact needed for reporting."""

    eval_dir = eval_dir.resolve()
    metrics_path = metrics_path.resolve()
    retention_path = retention_path.resolve()
    catalog_path = catalog_path.resolve()
    summary_path = summary_path.resolve()
    summary = _load_json(summary_path, "run summary")
    _validate_summary(summary)
    artifact_paths = _artifact_paths(summary, summary_path)
    if _sha256(metrics_path) != _sha256(artifact_paths["metrics"]):
        raise ReportInputError("--metrics does not match the metrics artifact in the run summary")
    if _sha256(catalog_path) != _sha256(artifact_paths["catalog"]):
        raise ReportInputError("--catalog does not match the catalog artifact in the run summary")
    if _sha256(retention_path) != _sha256(artifact_paths["retention"]):
        raise ReportInputError("--retention does not match the retention artifact in the run summary")

    metrics = _load_json(metrics_path, "metrics")
    retention = _load_json(retention_path, "retention")
    catalog = _load_json(catalog_path, "tree catalog")
    eval_records = _load_eval_records(eval_dir)
    eval_ids = {
        window: {str(row["id"]) for row in rows}
        for window, rows in eval_records.items()
    }
    union_ids = set().union(*eval_ids.values())
    provenance_rows = _read_jsonl(artifact_paths["provenance"], "provenance")
    run_rows = _read_jsonl(artifact_paths["runs"], "runs")
    fingerprint = str(summary["fingerprint"])
    if metrics.get("run_fingerprint") != fingerprint:
        raise ReportInputError(
            "metrics run_fingerprint does not match the run summary fingerprint"
        )
    mismatched_run_rows = sum(row.get("fingerprint") != fingerprint for row in run_rows)
    if mismatched_run_rows:
        raise ReportInputError(
            f"{mismatched_run_rows} run rows do not match the run summary fingerprint"
        )
    _validate_artifact_row_count(summary, "provenance", len(provenance_rows))
    _validate_artifact_row_count(summary, "runs", len(run_rows))
    provenance = _index_provenance(provenance_rows)
    missing = union_ids - set(provenance)
    extra = set(provenance) - union_ids
    if missing or extra:
        raise ReportInputError(
            "per-query provenance does not match the eval-set union: "
            f"missing={len(missing)}, extra={len(extra)}"
        )
    complete_run_ids = _complete_run_ids(run_rows)
    unavailable_run_ids = _unavailable_run_ids(run_rows)
    _validate_partial_arm_rows(summary, catalog, provenance, run_rows)
    recomputed_metrics = aggregate_metrics_v2(
        eval_records,
        run_rows,
        provenance,
        arms=ALL_ARMS,
    )
    stored_metrics_core = {
        key: value
        for key, value in metrics.items()
        if key not in {"run_fingerprint", "generated_utc"}
    }
    if stored_metrics_core != recomputed_metrics:
        raise ReportInputError(
            "metrics artifact disagrees with independent recomputation from "
            "eval, provenance, and run rows"
        )
    _validate_metrics(
        metrics,
        retention,
        catalog,
        eval_ids,
        provenance,
        complete_run_ids,
        unavailable_run_ids,
    )
    audit = {"provenance_by_window": _provenance_audit(eval_ids, provenance)}
    reconstruction_summary = summary["reconstruction"]
    for window in map(str, WINDOWS):
        measured = audit["provenance_by_window"][window]
        declared = {
            "exact_queries": reconstruction_summary["exact_queries_by_window"][window],
            "fallback_queries": reconstruction_summary["fallback_queries_by_window"][window],
            "unscored_or_unavailable_queries": reconstruction_summary[
                "unscored_or_unavailable_queries_by_window"
            ][window],
        }
        if any(measured[key] != value for key, value in declared.items()):
            raise ReportInputError(
                f"summary reconstruction class counts disagree with provenance for {window}s"
            )
        if sum(declared.values()) != len(eval_ids[window]):
            raise ReportInputError(
                f"summary reconstruction classes do not sum to eval rows for {window}s"
            )
    return ReportBundle(
        eval_dir=eval_dir,
        metrics_path=metrics_path,
        retention_path=retention_path,
        catalog_path=catalog_path,
        summary_path=summary_path,
        metrics=metrics,
        retention=retention,
        catalog=catalog,
        summary=summary,
        eval_ids=eval_ids,
        provenance=provenance,
        complete_run_ids=complete_run_ids,
        audit=audit,
    )


def _pct(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value * 100:.{digits}f}%"


def _num(value: Any, digits: int = 1) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:,.{digits}f}"


def _integer(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{int(value):,}"


def _cell(value: Any) -> str:
    return html.escape(str(value), quote=False).replace("|", "&#124;").replace("\n", " ")


def _code(value: Any) -> str:
    return f"<code>{_cell(value)}</code>"


def _metric_table(arms: Mapping[str, Any]) -> str:
    lines = [
        "| Arm | R@1 | R@5 | R@10 | R@20 | P@1 | P@5 | P@10 | P@20 | Mean bytes | Est. tokens | Failure@20 | Error rate | Median ms | p95 ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in ALL_ARMS:
        item = arms.get(arm, {}) if isinstance(arms.get(arm, {}), Mapping) else {}
        if item.get("available") is False:
            values = [f"`{arm}`", *("—" for _ in range(14))]
        else:
            values = [
                f"`{arm}`",
                *(_pct(item.get(f"recall@{k}")) for k in KS),
                *(_pct(item.get(f"precision@{k}")) for k in KS),
                _num(item.get("response_bytes_mean")),
                _num(item.get("estimated_tokens_mean")),
                _pct(item.get("failure@20")),
                _pct(item.get("execution_error_rate")),
                _num(item.get("latency_median_ms"), 2),
                _num(item.get("latency_p95_ms"), 2),
            ]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _head_to_head(block: Mapping[str, Any]) -> str:
    value = block.get("head_to_head")
    if not isinstance(value, Mapping) or value.get("verdict") is None:
        reason = value.get("reason") if isinstance(value, Mapping) else None
        suffix = f" Reason: {_cell(reason)}" if reason else ""
        return "The head-to-head verdict is unavailable because the ripgrep control could not be measured." + suffix
    winners = value.get("winners") or []
    if value.get("verdict") is True:
        rendered = ", ".join(f"`{name}`" for name in winners)
        return f"Yes: {rendered} beat ripgrep on both recall@20 and mean response bytes."
    return "No. Nothing beat ripgrep on both recall@20 and mean response size simultaneously."


def _ablation_delta(block: Mapping[str, Any]) -> float | None:
    value = block.get("tokenization_ablation")
    if not isinstance(value, Mapping):
        return None
    delta = value.get("recall@20_percentage_points")
    if isinstance(delta, bool) or not isinstance(delta, (int, float)) or not math.isfinite(delta):
        return None
    return float(delta)


def _ablation_largest(block: Mapping[str, Any]) -> bool | None:
    arms = block.get("arms")
    if not isinstance(arms, Mapping):
        return None
    aware = arms.get("bm25", {}).get("recall@20")
    legacy = arms.get("bm25_legacy", {}).get("recall@20")
    ident = arms.get("ident_first", {}).get("recall@20")
    pathboost = arms.get("bm25_pathboost", {}).get("recall@20")
    values = (aware, legacy, ident, pathboost)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return None
    token_gain = aware - legacy
    alternatives = (ident - aware, pathboost - aware)
    return token_gain > 0 and token_gain > max(0.0, *alternatives)


def _catalog_by_id(catalog: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(item["tree_id"]): item for item in catalog.get("trees", [])}


def _unavailability(summary: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    raw = summary.get("arm_unavailability_by_tree") or {}
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, dict[str, str]] = {}
    for tree_id, arms in raw.items():
        if not isinstance(arms, Mapping):
            continue
        result[str(tree_id)] = {
            str(arm): str(reason)
            for arm, reason in arms.items()
            if str(arm) in ALL_ARMS and str(reason).strip()
        }
    return result


def _tree_population_table(bundle: ReportBundle) -> tuple[str, list[tuple[str, str, str]]]:
    block = bundle.metrics["windows"][PRIMARY_WINDOW]
    metric_trees = block["trees"]
    catalog = _catalog_by_id(bundle.catalog)
    unavailability = _unavailability(bundle.summary)
    partial_by_window = bundle.summary.get("partial_arm_rows_by_tree") or {}
    partial_counts = (
        partial_by_window.get(PRIMARY_WINDOW, {})
        if isinstance(partial_by_window, Mapping)
        else {}
    )
    rows: list[tuple[int, int, str, list[str]]] = []
    unavailable_rows: list[tuple[str, str, str]] = []
    for tree_id, entry in catalog.items():
        target_counts = entry.get("target_counts") or {}
        targeted = int(target_counts.get(PRIMARY_WINDOW, 0) or 0)
        measured = metric_trees.get(tree_id, {})
        population = measured.get("population", {}) if isinstance(measured, Mapping) else {}
        scored = int(population.get("paired_scored_queries", 0) or 0)
        if targeted == 0 and scored == 0:
            continue
        scorable = int(population.get("scorable_positive_queries", 0) or 0)
        reconstruction = measured.get("reconstruction", {}) if isinstance(measured, Mapping) else {}
        exact = int(reconstruction.get("exact_queries", 0) or 0)
        fallback = int(reconstruction.get("fallback_queries", 0) or 0)
        reasons = unavailability.get(tree_id, {})
        tree_partial_counts = (
            partial_counts.get(tree_id, {}) if isinstance(partial_counts, Mapping) else {}
        )
        for arm, reason in sorted(reasons.items()):
            unavailable_rows.append((tree_id, arm, reason))
        arm_counts: list[str] = []
        measured_arms = measured.get("arms", {}) if isinstance(measured, Mapping) else {}
        for arm in ALL_ARMS:
            arm_metrics = measured_arms.get(arm, {}) if isinstance(measured_arms, Mapping) else {}
            arm_queries = arm_metrics.get("queries") if isinstance(arm_metrics, Mapping) else None
            arm_available = bool(arm_metrics.get("available")) if isinstance(arm_metrics, Mapping) else False
            arm_counts.append(
                f"{int(arm_queries):,}"
                if arm_available and isinstance(arm_queries, int) and arm_queries > 0 and arm not in reasons
                else (
                    f"{int(tree_partial_counts.get(arm, 0)):,}"
                    if int(tree_partial_counts.get(arm, 0) or 0) > 0 and arm not in reasons
                    else "—"
                )
            )
        if scored:
            status = "scored"
            if reasons:
                status = "partial; unavailable arm(s): " + ", ".join(sorted(reasons))
        elif tree_partial_counts:
            status = "partial current-tree control; index arms unavailable"
        else:
            mapping = str(entry.get("mapping_kind") or "unclassified")
            note = str(entry.get("note") or "no complete five-arm rows")
            status = f"not scored — {mapping}: {note}"
        logical_root = str(entry.get("logical_root") or tree_id)
        values = [
            _code(logical_root),
            f"{targeted:,}",
            f"{scored:,}",
            f"{max(0, targeted - scored):,}",
            f"{scorable:,}",
            f"{exact:,}",
            f"{fallback:,}",
            *arm_counts,
            _cell(status),
        ]
        rows.append((-scored, -targeted, logical_root.casefold(), values))

    population = block["population"]
    partial_arm_totals: Counter[str] = Counter()
    if isinstance(partial_counts, Mapping):
        for values in partial_counts.values():
            if isinstance(values, Mapping):
                for arm, count in values.items():
                    partial_arm_totals[str(arm)] += int(count or 0)
    total_targeted = sum(
        int((entry.get("target_counts") or {}).get(PRIMARY_WINDOW, 0) or 0)
        for entry in catalog.values()
    )
    total_values = [
        "**Total classified targets**",
        f"**{total_targeted:,}**",
        f"**{population['paired_scored_queries']:,}**",
        f"**{max(0, total_targeted - population['paired_scored_queries']):,}**",
        f"**{population['scorable_positive_queries']:,}**",
        f"**{block['reconstruction']['exact_queries']:,}**",
        f"**{block['reconstruction']['fallback_queries']:,}**",
        *(
            f"**{int(block['arms'][arm].get('queries', 0) or 0) + partial_arm_totals[arm]:,}**"
            if (
                (block["arms"][arm].get("available") and block["arms"][arm].get("queries"))
                or partial_arm_totals[arm]
            )
            else "—"
            for arm in ALL_ARMS
        ),
        "",
    ]
    lines = [
        "| Target tree | Retained targets | Paired scored | Not scored | Scorable Read-positive | Paired exact-time | Paired fallback | rg n | bm25 n | ident n | path n | legacy n | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        *("| " + " | ".join(values) + " |" for _, _, _, values in sorted(rows)),
        "| " + " | ".join(total_values) + " |",
    ]
    unassigned = population["retained_queries"] - total_targeted
    if unassigned:
        lines.append(
            f"\n{unassigned:,} retained queries had no catalogued target tree; they are included in the outside-index total."
        )
    return "\n".join(lines), unavailable_rows


def _reason_list(values: Mapping[str, Any], *, limit: int | None = None) -> str:
    items = sorted(
        ((str(reason), int(count)) for reason, count in values.items()),
        key=lambda item: (-item[1], item[0]),
    )
    if limit is not None:
        items = items[:limit]
    if not items:
        return "none reported"
    return "; ".join(f"{_code(reason)}: {count:,}" for reason, count in items)


def _gap_tables(audit: Mapping[str, Any]) -> str:
    exact = audit["exact_gap_seconds"]
    fallback = audit["fallback_gap_seconds"]
    unscored = audit["unscored_or_unavailable_gap_seconds"]
    lines = [
        "| Reconstruction class | Queries | Numeric gaps | Missing gap | Min s | Median s | Mean s | p95 s | p99 s | Max s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, value in (
        ("Exact-time", exact),
        ("Fallback", fallback),
        ("Unscored/unavailable", unscored),
    ):
        lines.append(
            "| "
            + " | ".join(
                [
                    label,
                    _integer(value.get("queries")),
                    _integer(value.get("count")),
                    _integer(value.get("missing")),
                    _num(value.get("min"), 1),
                    _num(value.get("median"), 1),
                    _num(value.get("mean"), 1),
                    _num(value.get("p95"), 1),
                    _num(value.get("p99"), 1),
                    _num(value.get("max"), 1),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "| Gap bucket (query timestamp − commit timestamp) | Exact-time | Fallback | Unscored/unavailable |",
            "|---|---:|---:|---:|",
        ]
    )
    labels = (
        ("lt_minus_7d", "< −7 days"),
        ("minus_7d_to_minus_1d", "−7 days to −1 day"),
        ("minus_1d_to_minus_1h", "−1 day to −1 hour"),
        ("minus_1h_to_0", "−1 hour to < 0"),
        ("zero", "0"),
        ("0_to_1h", "> 0 to 1 hour"),
        ("1h_to_1d", "> 1 hour to 1 day"),
        ("1d_to_7d", "> 1 day to 7 days"),
        ("gt_7d", "> 7 days"),
    )
    for key, label in labels:
        lines.append(
            f"| {label} | {exact['buckets'].get(key, 0):,} | {fallback['buckets'].get(key, 0):,} | {unscored['buckets'].get(key, 0):,} |"
        )
    return "\n".join(lines)


def _artifact_link_text(bundle: ReportBundle, name: str) -> str:
    descriptor = bundle.summary["artifacts"][name]
    return f"{_code(descriptor['path'])} (SHA-256 {_code(descriptor['sha256'])})"


def _render_unavailability(
    unavailable_rows: Sequence[tuple[str, str, str]], catalog: Mapping[str, Any]
) -> str:
    if not unavailable_rows:
        return "No runner-declared tree/arm unavailability was recorded."
    catalog_by_id = _catalog_by_id(catalog)
    lines = [
        "| Tree | Arm left empty | Reason |",
        "|---|---|---|",
    ]
    for tree_id, arm, reason in sorted(unavailable_rows):
        root = catalog_by_id.get(tree_id, {}).get("logical_root", tree_id)
        lines.append(f"| {_code(root)} | `{arm}` | {_cell(reason)} |")
    return "\n".join(lines)


def _retention_section(bundle: ReportBundle) -> list[str]:
    retention = bundle.retention
    diagnostic = retention.get("diagnostics", {})
    primary = retention["retention"][PRIMARY_WINDOW]
    prior_unique_calls = 13_327
    current_unique_calls = int(diagnostic.get("unique_grep_calls", 0) or 0)
    snapshot_delta = current_unique_calls - prior_unique_calls
    if snapshot_delta > 0:
        snapshot_comparison = (
            f"This fresh live-corpus snapshot contains {snapshot_delta:,} more unique calls than "
            f"the {prior_unique_calls:,} cited for the prior pass."
        )
    elif snapshot_delta < 0:
        snapshot_comparison = (
            f"This fresh live-corpus snapshot contains {-snapshot_delta:,} fewer unique calls than "
            f"the {prior_unique_calls:,} cited for the prior pass."
        )
    else:
        snapshot_comparison = "This fresh snapshot has the same unique-call count as the prior pass."
    lines = [
        "## Retention",
        "",
        f"The frozen corpus contained **{diagnostic.get('raw_grep_tool_uses', 0):,} raw Grep blocks** and "
        f"**{diagnostic.get('unique_grep_calls', 0):,} unique `(sessionId, toolUseId)` calls**.",
        "",
        f"At the authoritative 300-second window, **{primary['resolvable']:,} were retained "
        f"({_pct(primary['retention_rate'])})** and **{primary['all_excluded']:,} were excluded**: "
        f"{primary['excluded_abandonment']:,} abandonments, "
        f"{primary['excluded_missing_grep_result']:,} missing Grep results, and "
        f"{primary['excluded_unresolved_read_followup']:,} unresolved Read follow-ups. "
        f"The retained outcomes comprise {primary['positive_read']:,} Read positives and "
        f"{primary['failure_next_grep']:,} next-Grep failures.",
        "",
        "| Window | Unique Greps | Retained | Retention | Excluded | Abandoned | Missing Grep result | Unresolved Read |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for window in map(str, WINDOWS):
        value = retention["retention"][window]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{window}s",
                    f"{value['all_unique_grep_calls']:,}",
                    f"{value['resolvable']:,}",
                    _pct(value["retention_rate"]),
                    f"{value['all_excluded']:,}",
                    f"{value['excluded_abandonment']:,}",
                    f"{value['excluded_missing_grep_result']:,}",
                    f"{value['excluded_unresolved_read_followup']:,}",
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            f"The extractor froze {retention.get('corpus_bytes_at_snapshot', 0):,} input bytes at "
            f"{_code(retention.get('snapshot_utc', 'unknown'))}; the streamed snapshot hash is "
            f"{_code(retention.get('corpus_stream_sha256', 'unknown'))}. It is marked complete.",
            "",
            snapshot_comparison + " No eval or run artifact mixes the two snapshots.",
            "",
            f"Copied transcript history remains visible: {diagnostic.get('schema_ids_from_copied_history', 0):,} "
            "schema IDs came from copied history. No fitting or validation split is used in these descriptive metrics; "
            "if a validation split is introduced, it must be at session granularity.",
        ]
    )
    return lines


def _population_and_reconstruction_section(bundle: ReportBundle) -> list[str]:
    block = bundle.metrics["windows"][PRIMARY_WINDOW]
    population = block["population"]
    scored = population["paired_scored_queries"]
    retained = population["retained_queries"]
    mapped = population["mapped_queries"]
    outside = population["outside_any_indexed_tree"]
    catalog_outside = int(
        ((bundle.catalog.get("counts") or {}).get("outside_any_indexed_tree_by_window") or {}).get(
            PRIMARY_WINDOW, 0
        )
        or 0
    )
    snapshot_unavailable = max(0, outside - catalog_outside)
    paired_excluded = int(population["paired_excluded_queries"])
    paired_excluded_noun = "query" if paired_excluded == 1 else "queries"
    table, unavailable_rows = _tree_population_table(bundle)
    audit = bundle.audit["provenance_by_window"][PRIMARY_WINDOW]
    mapped_reconstruction = block["reconstruction"]["all_mapped_queries"]
    paired_reconstruction = block["reconstruction"]
    reconstruction_summary = bundle.summary["reconstruction"]
    mapping_kinds: Counter[str] = Counter()
    for entry in bundle.catalog.get("trees", []):
        if not isinstance(entry, Mapping):
            continue
        count = int((entry.get("target_counts") or {}).get(PRIMARY_WINDOW, 0) or 0)
        if count:
            mapping_kinds[str(entry.get("mapping_kind") or "unclassified")] += count
    lines = [
        "## Scored population per tree",
        "",
        f"**At 300 seconds, {scored:,} of {retained:,} retained queries ({_pct(scored / retained if retained else None)}) "
        "entered the common paired five-arm score population.** "
        f"{mapped:,} had a validated Git snapshot for the indexed arms. Empirical scope derivation put "
        f"{catalog_outside:,} queries outside every protected indexed tree; another {snapshot_unavailable:,} "
        "targeted a catalogued tree but lacked a reconstructable/available snapshot. "
        f"{paired_excluded:,} mapped {paired_excluded_noun} lacked a complete five-arm row set. "
        "All arms use the same paired IDs, so an unavailable arm cannot silently improve another arm's denominator.",
        "The empirically outside-index count includes available non-Git current trees because the protected index requires a Git worktree. "
        "Their ripgrep-only current-tree controls are shown in the per-tree `rg n` column but remain outside the global paired head-to-head denominator.",
        "",
        table,
        "",
        "Empirical target-mapping kinds: " + _reason_list(mapping_kinds) + ".",
        "",
        "An index is built for the assigned checkout/tree, while `scope_for_record` restricts each replay to its recorded "
        "subdirectory or file. A target-tree row therefore does not imply that every query searched the tree root.",
        "",
        f"Non-paired snapshot/unavailability reasons (including the {catalog_outside:,} empirically outside-index queries): "
        f"{_reason_list(population.get('outside_reason_counts', {}))}.",
        "Unpaired run-row reasons: "
        + _reason_list(population.get("unpaired_reason_counts", {}))
        + ".",
        "",
        "### Arm/tree availability",
        "",
        _render_unavailability(unavailable_rows, bundle.catalog),
        "",
        "An em dash in an arm table means the metric had no valid denominator or the arm was unavailable; it is not a zero. "
        "When a whole arm/tree combination could not run, the reason is listed above.",
        "",
        "### Repository-state reconstruction",
        "",
        "The per-query provenance artifact records an `exact` boolean, reconstruction mode, selected commit when one exists, "
        "and query-minus-commit time gap. Exact-time means the surviving recorded branch's first-parent line had a commit "
        "at or before the query and that clean commit tree was used; it does not prove the historical ref tip or recover "
        "contemporaneous dirty or untracked files.",
        "Branch resolution is necessarily an inference over refs that survive in the repositories today: a matching local branch "
        "is preferred before matching remote refs. Deleted or rewritten refs cannot be reconstructed from the surviving history.",
        "",
        "| Population (300s) | Queries | Exact-time | Fallback | Unscored/unavailable |",
        "|---|---:|---:|---:|---:|",
        f"| All retained queries | {audit['queries']:,} | {audit['exact_queries']:,} | {audit['fallback_queries']:,} | {audit['unscored_or_unavailable_queries']:,} |",
        f"| Mapped to an indexed tree | {mapped_reconstruction['queries']:,} | {mapped_reconstruction['exact_queries']:,} | {mapped_reconstruction['fallback_queries']:,} | {mapped_reconstruction.get('unscored_or_unavailable_queries', 0):,} |",
        f"| Paired five-arm score population | {paired_reconstruction['queries']:,} | {paired_reconstruction['exact_queries']:,} | {paired_reconstruction['fallback_queries']:,} | {paired_reconstruction.get('unscored_or_unavailable_queries', 0):,} |",
        "",
        "All-retained reconstruction modes: " + _reason_list(audit.get("mode_counts", {})) + ".",
        "",
        "Fallback reasons: " + _reason_list(audit.get("fallback_reason_counts", {})) + ".",
        "",
        "Unscored/unavailable reasons: "
        + _reason_list(audit.get("unscored_or_unavailable_reason_counts", {}))
        + ".",
        "",
        _gap_tables(audit),
        "",
        "Where no chosen commit exists, the gap is reported as missing, not zero. Unavailable and unscored queries are not called fallbacks.",
        "",
        "| Window | Actual fallbacks | Unscored/unavailable | Non-Git current-tree fallback | Non-Git outside paired score | Dirty state not reconstructable |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for window in map(str, WINDOWS):
        lines.append(
            f"| {window}s | "
            f"{reconstruction_summary['fallback_queries_by_window'][window]:,} | "
            f"{reconstruction_summary['unscored_or_unavailable_queries_by_window'][window]:,} | "
            f"{reconstruction_summary['non_git_fallback_queries_by_window'][window]:,} | "
            f"{reconstruction_summary['non_git_unscored_queries_by_window'][window]:,} | "
            f"{reconstruction_summary['dirty_state_unreconstructable_queries_by_window'][window]:,} |"
        )
    lines.extend(
        [
            "",
            "Per-query reconstruction evidence: " + _artifact_link_text(bundle, "provenance") + ".",
        ]
    )
    return lines


def _primary_metrics_section(bundle: ReportBundle) -> list[str]:
    block = bundle.metrics["windows"][PRIMARY_WINDOW]
    population = block["population"]
    return [
        "## Per-arm results (300 seconds)",
        "",
        f"All five rows use {population['paired_scored_queries']:,} paired queries. Recall uses "
        f"{population['scorable_positive_queries']:,} queries with at least one Read label inside that query's own target tree. "
        f"Precision and failure use {population['quality_queries']:,} quality queries, including "
        f"{population['behavioral_next_grep_failures']:,} next-Grep failures. "
        f"{population['outside_read_labels_removed']:,} Read labels outside the query's tree were removed; "
        f"{population['positive_queries_emptied_by_tree_filter']:,} raw-positive queries then had no IR label.",
        "",
        _metric_table(block["arms"]),
        "",
        "Recall and precision are macro means at the prespecified K values. `failure@20` covers scorable positives plus "
        "behavioral next-Grep failures. Mean bytes is the arithmetic mean of agent-visible response bytes; estimated tokens is exactly bytes/4. "
        "The prespecified response contracts are intentionally arm-specific: ripgrep replays each recorded `output_mode` and `head_limit`, "
        "whereas each index arm returns at most 20 fixed snippets. The size comparison therefore measures the specified systems' resulting "
        "payloads; it is not a symmetric same-K or same-snippet-budget comparison. Execution errors remain in the denominator and are not "
        "converted into successful empty responses.",
    ]


def _verdict_section(bundle: ReportBundle) -> list[str]:
    block = bundle.metrics["windows"][PRIMARY_WINDOW]
    control = block["arms"]["ripgrep"]
    lines = [
        "## Head-to-head verdict",
        "",
        "**" + _head_to_head(block) + "**",
        "",
        "The criterion is simultaneous and strict: recall@20 must be higher than ripgrep and arithmetic mean response bytes must be lower. "
        f"This verdict uses the corrected {block['population']['paired_scored_queries']:,}-query paired population, not the previous 20-query collapse.",
        "The verdict compares prespecified arm-specific response contracts. Ripgrep reproduces the recorded `output_mode` and `head_limit`; "
        "the index arms return up to 20 fixed snippets. It is therefore a comparison of the benchmark's proposed systems as specified, not a "
        "claim that the byte ordering would survive a symmetric response budget.",
        "",
        "| Candidate | Δ recall@20 vs ripgrep | Δ mean bytes vs ripgrep | Simultaneous win |",
        "|---|---:|---:|---|",
    ]
    control_recall = control.get("recall@20")
    control_bytes = control.get("response_bytes_mean")
    winners = set((block.get("head_to_head") or {}).get("winners") or [])
    for arm in PRIMARY_ARMS[1:]:
        item = block["arms"][arm]
        recall = item.get("recall@20")
        size = item.get("response_bytes_mean")
        recall_delta = (
            (recall - control_recall) * 100
            if isinstance(recall, (int, float)) and isinstance(control_recall, (int, float))
            else None
        )
        size_delta = (
            size - control_bytes
            if isinstance(size, (int, float)) and isinstance(control_bytes, (int, float))
            else None
        )
        lines.append(
            f"| `{arm}` | "
            f"{('—' if recall_delta is None else f'{recall_delta:+.2f} pp')} | "
            f"{('—' if size_delta is None else f'{size_delta:+,.1f}')} | "
            f"{'yes' if arm in winners else ('—' if recall_delta is None or size_delta is None else 'no')} |"
        )
    return lines


def _tokenisation_section(bundle: ReportBundle) -> list[str]:
    block = bundle.metrics["windows"][PRIMARY_WINDOW]
    aware = block["arms"]["bm25"]
    legacy = block["arms"]["bm25_legacy"]
    delta = _ablation_delta(block)
    largest = _ablation_largest(block)
    if delta is None:
        interpretation = (
            "The ablation could not be measured because identifier-aware BM25 or the legacy-tokeniser arm was unavailable. "
            "The previous small-sample claim is neither confirmed nor refuted."
        )
    elif delta > 0:
        interpretation = (
            f"The direction **confirms** the prior positive-sign result at scale, but not its magnitude: identifier-aware BM25 gained "
            f"**{delta:+.2f} percentage points** of recall@20 over the legacy tokenizer, versus +8.3 points in the collapsed 300-second pass."
        )
    else:
        interpretation = (
            f"The corrected population **refutes** the prior positive-sign result: identifier-aware BM25 changed recall@20 by **{delta:+.2f} percentage points**."
        )
    if largest is True:
        lever = "Among the tested retrieval changes, the specification's predicted largest lever held on recall@20."
    elif largest is False:
        lever = "The specification's predicted largest-lever claim did not hold on recall@20 among the tested changes."
    else:
        lever = "The largest-lever claim could not be evaluated because a required arm metric was unavailable."
    return [
        "## Tokenisation ablation",
        "",
        "The prior collapsed pass reported +8.3 points at 300 seconds and +9.1 points at 60 seconds, but recall was computed on only 12 queries. "
        "This comparison holds regions, ranking implementation, labels, and the paired query IDs fixed and changes only tokenisation.",
        "",
        "| Variant | R@1 | R@5 | R@10 | R@20 | Mean bytes | Failure@20 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        "| Identifier-aware `bm25` | "
        + " | ".join(
            [*(_pct(aware.get(f"recall@{k}")) for k in KS), _num(aware.get("response_bytes_mean")), _pct(aware.get("failure@20"))]
        )
        + " |",
        "| Legacy `/[a-z0-9_]+/` | "
        + " | ".join(
            [*(_pct(legacy.get(f"recall@{k}")) for k in KS), _num(legacy.get("response_bytes_mean")), _pct(legacy.get("failure@20"))]
        )
        + " |",
        "",
        interpretation + " " + lever,
    ]


def _window_section(bundle: ReportBundle) -> list[str]:
    lines = [
        "## Follow-up-window sensitivity",
        "",
        "The 60-, 300-, and 900-second eval files were loaded and validated as separate populations against their own retention counts. "
        "No window reuses another window's label population.",
        "",
        "| Window | Retained | Mapped | Outside index | Paired scored | Scorable positive | Exact-time | Fallback | Unscored/unavailable |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for window in map(str, WINDOWS):
        block = bundle.metrics["windows"][window]
        population = block["population"]
        audit = bundle.audit["provenance_by_window"][window]
        lines.append(
            f"| {window}s | {population['retained_queries']:,} | {population['mapped_queries']:,} | "
            f"{population['outside_any_indexed_tree']:,} | {population['paired_scored_queries']:,} | "
            f"{population['scorable_positive_queries']:,} | {audit['exact_queries']:,} | {audit['fallback_queries']:,} | {audit['unscored_or_unavailable_queries']:,} |"
        )
    for window in map(str, WINDOWS):
        block = bundle.metrics["windows"][window]
        retention = bundle.retention["retention"][window]
        delta = _ablation_delta(block)
        delta_text = "unavailable" if delta is None else f"{delta:+.2f} pp"
        lines.extend(
            [
                "",
                f"### {window} seconds",
                "",
                f"Retention: {retention['resolvable']:,}/{retention['all_unique_grep_calls']:,} "
                f"({_pct(retention['retention_rate'])}); {retention['all_excluded']:,} excluded. "
                f"Paired score population: {block['population']['paired_scored_queries']:,}.",
                "",
                _metric_table(block["arms"]),
                "",
                _head_to_head(block)
                + f" Identifier-aware minus legacy BM25 recall@20: **{delta_text}**.",
            ]
        )
    return lines


def _claims_sections(bundle: ReportBundle) -> list[str]:
    block = bundle.metrics["windows"][PRIMARY_WINDOW]
    population = block["population"]
    audit = bundle.audit["provenance_by_window"][PRIMARY_WINDOW]
    non_git = bundle.summary["reconstruction"]["non_git_unscored_queries_by_window"][PRIMARY_WINDOW]
    unavailable = _unavailability(bundle.summary)
    unavailable_count = sum(len(arms) for arms in unavailable.values())
    execution = bundle.summary.get("execution") or {}
    resumed_arm_rows = int(execution.get("resumed_arm_rows", 0) or 0)
    session_counts = Counter(
        record_id.split(":", 1)[0] for record_id in bundle.eval_ids[PRIMARY_WINDOW]
    )
    primary_rows = len(bundle.eval_ids[PRIMARY_WINDOW])
    largest_session_share = (
        max(session_counts.values()) / primary_rows if session_counts and primary_rows else None
    )
    top_ten_session_share = (
        sum(sorted(session_counts.values(), reverse=True)[:10]) / primary_rows
        if session_counts and primary_rows
        else None
    )
    claims = [
        f"- Exact historical working-tree performance could not be verified for {audit['fallback_queries']:,} actual fallback queries or {audit['unscored_or_unavailable_queries']:,} unavailable/unscored queries, and Git commits cannot recover dirty or untracked state for the {audit['exact_queries']:,} exact-time rows either.",
        f"- Non-Git historical state could not be reconstructed; {non_git:,} eligible non-Git current-tree fallback queries remained outside the paired five-arm score at 300 seconds. This narrow count excludes absent/failed non-Git targets, which appear in the unscored reason table. Available current trees have ripgrep-only control rows disclosed separately; the Git-bound index arms are empty.",
        "- Counterfactual agent behavior could not be verified. Read labels record what agents did after seeing the original Grep result, not what they would have read after another retriever.",
        "- Unread returned paths could not be proven irrelevant. Precision follows the specification and treats unjudged paths as nonrelevant.",
        "- True model-token counts could not be verified; the specified bytes/4 estimate is reported.",
        "- A symmetric-budget recall/size ranking could not be verified. Ripgrep replays recorded output modes and head limits, while index arms return up to 20 fixed snippets under the prespecified asymmetric contracts.",
        "- Latency portability and a general cold-cache workload could not be verified from one Windows host and one disclosed run policy.",
        "- Customer, cross-organisation, and cross-language portability could not be verified.",
    ]
    if unavailable_count:
        claims.append(
            f"- {unavailable_count:,} declared tree/arm combinations could not run; their cells are empty and their reasons are reported rather than imputed."
        )
    if resumed_arm_rows:
        claims.append(
            f"- Complete first-pass state warmup and index-refresh diagnostics could not be verified after the orchestration command lease expired. "
            f"The runner resumed from {resumed_arm_rows:,} durable arm rows without re-running them; rankings, payloads, and per-query timings survive, "
            "but aggregate refresh counts and state warmup details in the final summary cover the resumed pass only."
        )
    changes = [
        "- Explicit per-query commit SHAs plus frozen dirty/untracked worktrees would replace timestamp inference and could change both control and index recall.",
        "- Preserved Git histories or archived snapshots for unavailable and non-Git target trees would enlarge the paired population and could change the verdict.",
        "- Manual relevance judgments, or randomized exposure to retrievers, could change ranking conclusions by reducing Grep exposure bias.",
        "- Changing either prespecified arm-specific response contract (recorded Grep output/head limits, index K, snippet budget, or a byte cap) could change the simultaneous recall/size result. Any alternative must be fixed before labels are inspected.",
        "- Re-measurement on independent organisations, repositories, languages, and harnesses could change the external-validity conclusion.",
        "- A deployment dominated by cold repository scans could change the latency comparison.",
    ]
    confidence = [
        f"- **Retention — high.** Counts come from a completed frozen-size streaming pass over {bundle.retention.get('files_total', 0):,} files, with a corpus hash and every exclusion class counted.",
        f"- **Scored-population accounting and arithmetic — high.** The report independently matched eval IDs, per-query provenance, all five run-row arms, per-tree counts, and artifact hashes, then recomputed every metric, verdict, and ablation from those rows; {population['paired_scored_queries']:,} queries survive those checks.",
        "- **Tree assignment — moderate.** Absolute query scope is authoritative and assignments are empirical, but vanished worktrees and basename/epoch aliases require documented reconstruction judgments.",
        f"- **Historical state — moderate for the {audit['exact_queries']:,} timestamp-selected commits; low for the {audit['fallback_queries']:,} actual fallbacks; unavailable for {audit['unscored_or_unavailable_queries']:,} unscored rows.** Exact rows use local-first branch resolution over refs surviving today and choose a commit at or before the query, but rewritten/deleted refs and dirty state are unrecoverable.",
        f"- **Head-to-head — moderate within these repositories.** The arms are paired on {population['scorable_positive_queries']:,} scorable positives under prespecified arm-specific response contracts, but those contracts are asymmetric and relevance is implicit and exposure-biased.",
        "- **Response-byte arithmetic — high for this serializer; token-count confidence — low-to-moderate.** Bytes are directly measured, while tokens are only bytes/4.",
        "- **Tokenisation ablation — moderate.** It is paired and isolates the tokenizer, but it remains an implicit-feedback result from one organisation.",
        "- **Latency — moderate for this machine, low for deployment generalisation.** Query timing is measured under one recorded warmup/cache policy and local load; per-query timings survived the resume, while complete first-pass state warmup diagnostics did not.",
        "- **Cross-repository/customer generalisation — low.** Even a session-level split would not create independent organisations or repository families.",
    ]
    return [
        "## Claims that could NOT be verified",
        "",
        *claims,
        "",
        "The eval set is one organisation's transcripts, and **98.6% of transcript records come from a single transcript-source tree** "
        "(this is source concentration, not the empirical query-target distribution). "
        "Any session-level split would license within-repository generalisation and nothing more; the wider physical query-target distribution does not turn this into an independent multi-organisation sample.",
        "",
        f"At 300 seconds, the retained IDs span {len(session_counts):,} session prefixes; the largest supplies "
        f"{_pct(largest_session_share)} of rows and the ten largest supply {_pct(top_ten_session_share)}. "
        "No session-clustered uncertainty interval was prespecified, so close arm margins remain descriptive.",
        "",
        "## What would change this verdict",
        "",
        *changes,
        "",
        "## Per-claim confidence",
        "",
        *confidence,
    ]


def _reproduction_section(bundle: ReportBundle) -> list[str]:
    artifacts = bundle.summary["artifacts"]
    lines = [
        "## Reproduction and artifact integrity",
        "",
        "The report generator recomputed every required artifact hash before rendering and refused debug, capped, non-final, or incomplete input.",
        "",
        "| Artifact | Path | SHA-256 | Rows (if declared) |",
        "|---|---|---|---:|",
    ]
    for name in ("runs", "provenance", "metrics", "catalog", "retention"):
        descriptor = artifacts[name]
        rows = descriptor.get("rows")
        lines.append(
            f"| `{name}` | {_code(descriptor['path'])} | {_code(descriptor['sha256'])} | "
            f"{('—' if rows is None else f'{rows:,}')} |"
        )
    execution = bundle.summary.get("execution")
    lines.extend(
        [
            "",
            f"Run summary: {_code(bundle.summary_path)}. Generated UTC: "
            f"{_code(bundle.summary.get('generated_utc', 'not reported'))}.",
        ]
    )
    if isinstance(execution, Mapping):
        if "query_order_seed" in execution:
            lines.append(f" Query-order seed: {_code(execution['query_order_seed'])}.")
        if "index_caps" in execution:
            lines.append(
                " Fixed index caps: "
                + _code(json.dumps(execution["index_caps"], sort_keys=True, ensure_ascii=False))
                + "."
            )
        if "warmup" in execution:
            lines.append(
                " Warmup/cache policy: "
                + _code(json.dumps(execution["warmup"], sort_keys=True, ensure_ascii=False))
                + "."
            )
        resumed_arm_rows = int(execution.get("resumed_arm_rows", 0) or 0)
        if resumed_arm_rows:
            lines.append(
                f" Execution resumed from {resumed_arm_rows:,} durable arm rows after the orchestration command lease expired. "
                "Those rows were hash/fingerprint-validated and not rerun. Final aggregate full/incremental refresh counts and state warmup details therefore describe only the resumed pass; per-query result and timing rows remain intact."
            )
        policies = sorted(
            {
                str((state.get("index") or {}).get("fts_maintenance_policy"))
                for state in (execution.get("states") or [])
                if isinstance(state, Mapping)
                and (state.get("index") or {}).get("fts_maintenance_policy")
            }
        )
        if policies:
            lines.append(" FTS maintenance policy: " + _code("; ".join(policies)) + ".")
    notes = bundle.summary.get("notes")
    if isinstance(notes, Sequence) and not isinstance(notes, (str, bytes)) and notes:
        lines.extend(["", "Runner notes:", "", *(f"- {_cell(note)}" for note in notes)])
    return lines


def report_markdown(bundle: ReportBundle) -> str:
    """Render RESULTS-V2.md in the specification/user-requested order."""

    lines: list[str] = [
        "# Grep-replacement benchmark — corrected V2",
        "",
        "This rerun corrects query-tree scope and reconstructs repository state from each query timestamp and recorded branch where Git history permits.",
        "",
    ]
    for section in (
        _retention_section(bundle),
        _population_and_reconstruction_section(bundle),
        _primary_metrics_section(bundle),
        _verdict_section(bundle),
        _tokenisation_section(bundle),
        _window_section(bundle),
        _claims_sections(bundle),
        _reproduction_section(bundle),
    ):
        lines.extend(section)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[2]
    default_eval_dir = project_root / "exploratory" / "retrieval" / "v2"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", type=Path, default=default_eval_dir)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--retention", type=Path)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument(
        "--report",
        type=Path,
        default=project_root / "exploratory" / "retrieval" / "RESULTS-V2.md",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    eval_dir = args.eval_dir.resolve()
    bundle = load_report_bundle(
        eval_dir=eval_dir,
        metrics_path=args.metrics or eval_dir / "metrics-v2.json",
        retention_path=args.retention or eval_dir / "retention.json",
        catalog_path=args.catalog or eval_dir / "tree-catalog-v2.json",
        summary_path=args.summary or eval_dir / "run-summary-v2.json",
    )
    rendered = report_markdown(bundle)
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_suffix(report_path.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8", newline="\n")
    os.replace(temporary, report_path)
    print(
        json.dumps(
            {
                "report": str(report_path),
                "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "paired_queries_300s": bundle.metrics["windows"][PRIMARY_WINDOW]["population"][
                    "paired_scored_queries"
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALL_ARMS",
    "CATALOG_SCHEMA",
    "ReportBundle",
    "ReportInputError",
    "RUN_SCHEMA",
    "load_report_bundle",
    "main",
    "report_markdown",
]
