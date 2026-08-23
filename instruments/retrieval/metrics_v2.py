"""Pure aggregation for the corrected multi-tree retrieval benchmark.

The original scorer assumes one logical repository root.  V2 deliberately
does not: every run row carries the logical root against which that query was
executed.  This module contains no runner or filesystem code so that the
population rules can be tested independently of index construction.
"""

from __future__ import annotations

import math
import ntpath
import statistics
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


KS = (1, 5, 10, 20)
PRIMARY_ARMS = ("ripgrep", "bm25", "ident_first", "bm25_pathboost")
ALL_ARMS = (*PRIMARY_ARMS, "bm25_legacy")
FALLBACK_MODES = frozenset({"head_fallback", "non_git_current_fallback"})


def normalise_windows_path(path: str) -> str:
    """Return the case-insensitive lexical form used by the original scorer."""

    return ntpath.normcase(ntpath.normpath(path))


def inside_logical_root(path: str, logical_root: str) -> bool:
    candidate = normalise_windows_path(path)
    root = normalise_windows_path(logical_root)
    return bool(root) and (candidate == root or candidate.startswith(root + "\\"))


def truth_for_row(record: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[list[str], int]:
    """Filter a record's Read labels using this run row's own logical root."""

    logical_root = str(row.get("logical_root") or "")
    inside: list[str] = []
    seen: set[str] = set()
    outside = 0
    raw_reads = record.get("followed_by_read") or []
    for raw in raw_reads if isinstance(raw_reads, Sequence) and not isinstance(raw_reads, str) else []:
        path = normalise_windows_path(str(raw))
        if inside_logical_root(path, logical_root):
            if path not in seen:
                seen.add(path)
                inside.append(path)
        else:
            # Preserve score.py's arithmetic: outside labels are counted as
            # observed, while inside labels are de-duplicated for IR truth.
            outside += 1
    return inside, outside


def percentile_nearest_rank(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _empty_arm_metrics() -> dict[str, Any]:
    return {
        "queries": 0,
        "quality_queries": 0,
        "positive_queries": 0,
        "behavioral_failure_queries": 0,
        **{f"recall@{k}": None for k in KS},
        **{f"precision@{k}": None for k in KS},
        "failure@20": None,
        "response_bytes_total": 0,
        "response_bytes_mean": None,
        "estimated_tokens_mean": None,
        "execution_error_rate": None,
        "latency_median_ms": None,
        "latency_p95_ms": None,
        # This matches score.py: zero rows is an empty measurement, not an arm
        # whose every attempted execution failed.
        "available": True,
    }


def aggregate_arm_rows(
    records: Sequence[Mapping[str, Any]],
    rows_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute the SPEC metrics, using each row's logical root for truth."""

    if not records:
        return _empty_arm_metrics()

    recall_values: dict[int, list[float]] = {k: [] for k in KS}
    precision_values: dict[int, list[float]] = {k: [] for k in KS}
    failures = 0
    positive_queries = 0
    behavioral_failure_queries = 0
    quality_queries = 0
    bytes_values: list[int] = []
    latencies: list[float] = []
    errors = 0

    for record in records:
        record_id = str(record["id"])
        row = rows_by_id.get(record_id)
        if row is None:
            # Callers normally pass the paired population.  Keeping the same
            # skip behavior as score.py makes the helper independently useful.
            continue
        raw_ranking = row.get("ranked_paths") or []
        if not isinstance(raw_ranking, Sequence) or isinstance(raw_ranking, str):
            raw_ranking = []
        ranking = [normalise_windows_path(str(path)) for path in raw_ranking]
        truth, _ = truth_for_row(record, row)
        truth_set = set(truth)
        behavioral_failure = bool(record.get("followed_by_grep"))

        if truth_set:
            positive_queries += 1
            for k in KS:
                relevant = len(set(ranking[:k]) & truth_set)
                recall_values[k].append(relevant / len(truth_set))
        if behavioral_failure:
            behavioral_failure_queries += 1

        if truth_set or behavioral_failure:
            quality_queries += 1
            for k in KS:
                relevant = len(set(ranking[:k]) & truth_set)
                precision_values[k].append(relevant / k)
            if behavioral_failure or not set(ranking[: max(KS)]) & truth_set:
                failures += 1

        bytes_values.append(int(row.get("response_bytes", 0)))
        latencies.append(float(row.get("latency_ms", 0.0)))
        if row.get("error"):
            errors += 1

    if not bytes_values:
        return _empty_arm_metrics()

    query_count = len(bytes_values)
    unavailable = errors == query_count
    result: dict[str, Any] = {
        "queries": query_count,
        "quality_queries": quality_queries,
        "positive_queries": positive_queries,
        "behavioral_failure_queries": behavioral_failure_queries,
        **{
            f"recall@{k}": statistics.fmean(recall_values[k]) if recall_values[k] else None
            for k in KS
        },
        **{
            f"precision@{k}": statistics.fmean(precision_values[k]) if precision_values[k] else None
            for k in KS
        },
        "failure@20": failures / quality_queries if quality_queries else None,
        "response_bytes_total": sum(bytes_values),
        "response_bytes_mean": statistics.fmean(bytes_values),
        "estimated_tokens_mean": statistics.fmean(bytes_values) / 4,
        "execution_error_rate": errors / query_count,
        "latency_median_ms": statistics.median(latencies),
        "latency_p95_ms": percentile_nearest_rank(latencies, 0.95),
        "available": not unavailable,
    }
    if unavailable:
        for key in (
            *(f"recall@{k}" for k in KS),
            *(f"precision@{k}" for k in KS),
            "failure@20",
            "response_bytes_total",
            "response_bytes_mean",
            "estimated_tokens_mean",
            "latency_median_ms",
            "latency_p95_ms",
        ):
            result[key] = None
    return result


def signed_gap_distribution(values: Iterable[Any], *, expected_count: int | None = None) -> dict[str, Any]:
    """Summarize signed query-minus-commit gaps without discarding their sign."""

    numeric: list[float] = []
    supplied = 0
    for value in values:
        supplied += 1
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            numeric.append(number)

    total = supplied if expected_count is None else expected_count
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
    for gap in numeric:
        if gap < -604_800:
            buckets["lt_minus_7d"] += 1
        elif gap < -86_400:
            buckets["minus_7d_to_minus_1d"] += 1
        elif gap < -3_600:
            buckets["minus_1d_to_minus_1h"] += 1
        elif gap < 0:
            buckets["minus_1h_to_0"] += 1
        elif gap == 0:
            buckets["zero"] += 1
        elif gap <= 3_600:
            buckets["0_to_1h"] += 1
        elif gap <= 86_400:
            buckets["1h_to_1d"] += 1
        elif gap <= 604_800:
            buckets["1d_to_7d"] += 1
        else:
            buckets["gt_7d"] += 1

    return {
        "count": len(numeric),
        "missing": max(0, total - len(numeric)),
        "negative": sum(value < 0 for value in numeric),
        "zero": sum(value == 0 for value in numeric),
        "positive": sum(value > 0 for value in numeric),
        "min": min(numeric) if numeric else None,
        "median": statistics.median(numeric) if numeric else None,
        "mean": statistics.fmean(numeric) if numeric else None,
        "p95_nearest_rank": percentile_nearest_rank(numeric, 0.95),
        "p99_nearest_rank": percentile_nearest_rank(numeric, 0.99),
        "max": max(numeric) if numeric else None,
        "buckets": buckets,
        "bucket_definition": "signed query timestamp minus chosen commit timestamp, in seconds",
    }


def _provenance_summary(
    record_ids: Iterable[str],
    provenance_by_id: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    provenances = [provenance_by_id.get(record_id, {}) for record_id in record_ids]
    exact = [item for item in provenances if item.get("exact") is True]
    fallback = [
        item for item in provenances if str(item.get("mode") or "") in FALLBACK_MODES
    ]
    unscored = [
        item
        for item in provenances
        if item.get("exact") is not True
        and str(item.get("mode") or "") not in FALLBACK_MODES
    ]
    mode_counts = Counter(str(item.get("mode") or "unknown") for item in provenances)
    fallback_reasons = Counter(str(item.get("reason") or "unspecified") for item in fallback)
    unscored_reasons = Counter(str(item.get("reason") or "unspecified") for item in unscored)
    exact_commits = {str(item["commit"]) for item in exact if item.get("commit")}
    fallback_commits = {str(item["commit"]) for item in fallback if item.get("commit")}
    return {
        "queries": len(provenances),
        "exact_queries": len(exact),
        "fallback_queries": len(fallback),
        "unscored_or_unavailable_queries": len(unscored),
        "mode_counts": dict(sorted(mode_counts.items())),
        "fallback_reason_counts": dict(sorted(fallback_reasons.items())),
        "unscored_or_unavailable_reason_counts": dict(sorted(unscored_reasons.items())),
        "exact_unique_commits": len(exact_commits),
        "fallback_unique_commits": len(fallback_commits),
        "exact_gap_seconds": signed_gap_distribution(
            (item.get("gap_seconds") for item in exact), expected_count=len(exact)
        ),
        "fallback_gap_seconds": signed_gap_distribution(
            (item.get("gap_seconds") for item in fallback), expected_count=len(fallback)
        ),
        "unscored_or_unavailable_gap_seconds": signed_gap_distribution(
            (item.get("gap_seconds") for item in unscored), expected_count=len(unscored)
        ),
    }


def _population_summary(
    records: Sequence[Mapping[str, Any]],
    representative_rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, int]:
    raw_positive = 0
    scorable = 0
    emptied = 0
    outside_labels = 0
    inside_labels = 0
    behavioral_failures = 0
    quality_queries = 0
    for record in records:
        record_id = str(record["id"])
        row = representative_rows[record_id]
        truth, outside = truth_for_row(record, row)
        has_raw_positive = bool(record.get("followed_by_read"))
        behavioral_failure = bool(record.get("followed_by_grep"))
        raw_positive += has_raw_positive
        scorable += bool(truth)
        emptied += bool(has_raw_positive and not truth)
        outside_labels += outside
        inside_labels += len(truth)
        behavioral_failures += behavioral_failure
        quality_queries += bool(truth) or behavioral_failure
    return {
        "raw_positive_queries": raw_positive,
        "scorable_positive_queries": scorable,
        "positive_queries_emptied_by_tree_filter": emptied,
        "outside_read_labels_removed": outside_labels,
        "inside_unique_read_labels": inside_labels,
        "behavioral_next_grep_failures": behavioral_failures,
        "quality_queries": quality_queries,
    }


def head_to_head(arms_data: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    control = arms_data.get("ripgrep", {})
    control_recall = control.get("recall@20")
    control_bytes = control.get("response_bytes_mean")
    if control_recall is None or control_bytes is None:
        return {
            "verdict": None,
            "winners": [],
            "reason": "ripgrep recall@20 or mean response bytes unavailable",
        }
    winners = [
        arm
        for arm in PRIMARY_ARMS
        if arm != "ripgrep"
        and arms_data.get(arm, {}).get("recall@20") is not None
        and arms_data.get(arm, {}).get("response_bytes_mean") is not None
        and arms_data[arm]["recall@20"] > control_recall
        and arms_data[arm]["response_bytes_mean"] < control_bytes
    ]
    return {
        "verdict": bool(winners),
        "winners": winners,
        "reason": None,
        "ripgrep_recall@20": control_recall,
        "ripgrep_response_bytes_mean": control_bytes,
    }


def tokenization_deltas(arms_data: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    aware = arms_data.get("bm25", {})
    legacy = arms_data.get("bm25_legacy", {})
    metric_names = [
        *(f"recall@{k}" for k in KS),
        *(f"precision@{k}" for k in KS),
        "failure@20",
        "response_bytes_mean",
        "estimated_tokens_mean",
        "execution_error_rate",
        "latency_median_ms",
        "latency_p95_ms",
    ]
    deltas: dict[str, float | None] = {}
    for metric in metric_names:
        left = aware.get(metric)
        right = legacy.get(metric)
        deltas[metric] = left - right if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
    recall_20 = deltas["recall@20"]
    return {
        "available": recall_20 is not None,
        "direction": "identifier_aware_minus_legacy",
        "deltas": deltas,
        "recall@20_percentage_points": recall_20 * 100 if recall_20 is not None else None,
    }


def _index_provenance(
    provenance: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if isinstance(provenance, Mapping):
        return {str(key): value for key, value in provenance.items()}
    indexed: dict[str, Mapping[str, Any]] = {}
    for item in provenance:
        record_id = item.get("record_id", item.get("id"))
        if record_id is None:
            raise ValueError("provenance row lacks record_id/id")
        key = str(record_id)
        if key in indexed:
            raise ValueError(f"duplicate provenance for {key}")
        indexed[key] = item
    return indexed


def _normalised_root(value: Any) -> str:
    return normalise_windows_path(str(value or ""))


def aggregate_metrics_v2(
    records_by_window: Mapping[str | int, Sequence[Mapping[str, Any]]],
    run_rows: Sequence[Mapping[str, Any]],
    provenance_by_id: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    *,
    arms: Sequence[str] = ALL_ARMS,
) -> dict[str, Any]:
    """Aggregate all V2 windows over one common five-arm population.

    An ID is scored only when all requested arms have exactly one run row.
    Window membership is recomputed independently from ``records_by_window``.
    """

    arm_names = tuple(str(arm) for arm in arms)
    if not arm_names or len(set(arm_names)) != len(arm_names):
        raise ValueError("arms must be a non-empty sequence of unique names")

    provenance = _index_provenance(provenance_by_id)
    rows_by_arm: dict[str, dict[str, Mapping[str, Any]]] = {arm: {} for arm in arm_names}
    known_tree_ids: set[str] = {
        str(item["target_tree_id"])
        for item in provenance.values()
        if item.get("target_tree_id") not in (None, "")
    }
    ignored_run_rows = 0
    for row in run_rows:
        arm = str(row.get("arm") or "")
        if arm not in rows_by_arm:
            ignored_run_rows += 1
            continue
        if row.get("record_id") is None:
            raise ValueError("run row lacks record_id")
        record_id = str(row["record_id"])
        if record_id in rows_by_arm[arm]:
            raise ValueError(f"duplicate run row for {record_id}/{arm}")
        rows_by_arm[arm][record_id] = row
        if row.get("tree_id") not in (None, ""):
            known_tree_ids.add(str(row["tree_id"]))

    complete_ids = set.intersection(*(set(rows_by_arm[arm]) for arm in arm_names))
    result: dict[str, Any] = {
        "schema_version": 2,
        "arms": list(arm_names),
        "ks": list(KS),
        "pairing_rule": "record id must have one row for every requested arm",
        "ignored_run_rows_for_other_arms": ignored_run_rows,
        "windows": {},
    }

    def window_sort_key(key: str | int) -> tuple[int, str]:
        text = str(key)
        return (int(text), text) if text.isdigit() else (10**9, text)

    for raw_window in sorted(records_by_window, key=window_sort_key):
        window = str(raw_window)
        records = list(records_by_window[raw_window])
        by_id: dict[str, Mapping[str, Any]] = {}
        for record in records:
            if record.get("id") is None:
                raise ValueError(f"window {window} contains a record without id")
            record_id = str(record["id"])
            if record_id in by_id:
                raise ValueError(f"window {window} contains duplicate record id {record_id}")
            by_id[record_id] = record

        record_ids = set(by_id)
        target_tree_by_id = {
            record_id: str(provenance[record_id]["target_tree_id"])
            for record_id in record_ids
            if record_id in provenance and provenance[record_id].get("target_tree_id") not in (None, "")
        }
        mapped_ids = set(target_tree_by_id)
        outside_ids = record_ids - mapped_ids
        paired_ids = mapped_ids & complete_ids

        # A complete row set that disagrees on tree identity or logical root is
        # a benchmark bug, not a population to score plausibly.
        for record_id in sorted(paired_ids):
            expected_tree = target_tree_by_id[record_id]
            tree_ids = {str(rows_by_arm[arm][record_id].get("tree_id") or "") for arm in arm_names}
            roots = {_normalised_root(rows_by_arm[arm][record_id].get("logical_root")) for arm in arm_names}
            if tree_ids != {expected_tree}:
                raise ValueError(
                    f"run rows for {record_id} disagree with target tree {expected_tree!r}: {sorted(tree_ids)!r}"
                )
            if len(roots) != 1 or "" in roots:
                raise ValueError(f"run rows for {record_id} have inconsistent or empty logical roots")

        missing_arm_counts = Counter()
        unpaired_reason_counts = Counter()
        for record_id in mapped_ids - paired_ids:
            missing = [arm for arm in arm_names if record_id not in rows_by_arm[arm]]
            for arm in missing:
                missing_arm_counts[arm] += 1
            unpaired_reason_counts["missing:" + ",".join(missing)] += 1

        outside_reason_counts = Counter(
            str(provenance.get(record_id, {}).get("reason") or "missing_provenance")
            for record_id in outside_ids
        )
        paired_records = [by_id[record_id] for record_id in by_id if record_id in paired_ids]
        representative_rows = {record_id: rows_by_arm[arm_names[0]][record_id] for record_id in paired_ids}
        population_quality = _population_summary(paired_records, representative_rows)

        arms_data = {
            arm: aggregate_arm_rows(paired_records, rows_by_arm[arm])
            for arm in arm_names
        }

        trees: dict[str, Any] = {}
        for tree_id in sorted(known_tree_ids):
            retained_tree_ids = {record_id for record_id, target in target_tree_by_id.items() if target == tree_id}
            paired_tree_ids = paired_ids & retained_tree_ids
            tree_records = [by_id[record_id] for record_id in by_id if record_id in paired_tree_ids]
            tree_representative = {
                record_id: representative_rows[record_id]
                for record_id in paired_tree_ids
            }
            tree_arms = {
                arm: aggregate_arm_rows(tree_records, rows_by_arm[arm])
                for arm in arm_names
            }
            tree_population = {
                "retained_queries": len(retained_tree_ids),
                "mapped_queries": len(retained_tree_ids),
                "paired_scored_queries": len(paired_tree_ids),
                "paired_excluded_queries": len(retained_tree_ids - paired_tree_ids),
                **_population_summary(tree_records, tree_representative),
            }
            trees[tree_id] = {
                "population": tree_population,
                "reconstruction": _provenance_summary(sorted(paired_tree_ids), provenance),
                "arms": tree_arms,
                "head_to_head": head_to_head(tree_arms),
                "tokenization_ablation": tokenization_deltas(tree_arms),
            }

        mapped_reconstruction = _provenance_summary(sorted(mapped_ids), provenance)
        paired_reconstruction = _provenance_summary(sorted(paired_ids), provenance)
        result["windows"][window] = {
            "population": {
                "retained_queries": len(records),
                "mapped_queries": len(mapped_ids),
                "outside_any_indexed_tree": len(outside_ids),
                "outside_reason_counts": dict(sorted(outside_reason_counts.items())),
                "complete_five_arm_row_queries": len(record_ids & complete_ids),
                "paired_scored_queries": len(paired_ids),
                "paired_excluded_queries": len(mapped_ids - paired_ids),
                "missing_arm_counts": dict(sorted(missing_arm_counts.items())),
                "unpaired_reason_counts": dict(sorted(unpaired_reason_counts.items())),
                **population_quality,
            },
            "reconstruction": {
                **paired_reconstruction,
                "population": "paired_scored_queries",
                "all_mapped_queries": mapped_reconstruction,
            },
            "outside": {
                "queries": len(outside_ids),
                "reason_counts": dict(sorted(outside_reason_counts.items())),
            },
            "trees": trees,
            "arms": arms_data,
            "head_to_head": head_to_head(arms_data),
            "tokenization_ablation": tokenization_deltas(arms_data),
        }

    return result


# A concise alias for callers that treat this module as the V2 metrics backend.
aggregate_metrics = aggregate_metrics_v2
